"""起動準備の進行状況(カメラ検知 + AIモデル準備 + 初フレーム)を1本にまとめる

デバッグページ従来のゲージは **デバイス探索の進捗だけ** を出していました。
しかし実際に映像が出るまでには次が必要で、とくに AI モデル読込(TensorFlow の import
+ Keras の load + MediaPipe の landmarker 生成)が最長で、実測 10〜20 秒かかります。

    1. カメラ検知        (別スレッドで index を探す)
    2. AIモデル準備      (tensorflow / mediapipe / Keras / PoseLandmarker)
    3. カメラ確保        (VideoCapture を開く。USBは他カメラ使用中にハングすることがある)
    4. 初フレーム取得    (1枚目が読めたか)
    5. 解析稼働          (fps>0 で overlay 配信できる状態)

ゲージが100%なのに映像が出ない、という見え方の原因がこれです。
ここでは上記全体を1本の進捗にし、**いつ何に何秒かかったかのログ**も残します。
"""
import threading
import time
from collections import deque

# (キー, 表示名, 重み)。重みの合計は 100。
STAGES = [
    ("detect", "カメラ検知", 15),
    ("model", "AIモデル準備", 35),
    ("camera", "カメラ確保", 15),
    ("frame", "初フレーム取得", 20),
    ("analyze", "解析稼働", 15),
]

LOG_MAX = 240

_lock = threading.Lock()
_started_at = time.monotonic()
_log = deque(maxlen=LOG_MAX)

# ステージの内部状態
_detect = {"percent": 0.0, "detail": "検知を待っています"}
_model = {"percent": 0.0, "state": "pending", "detail": "未開始", "seconds": None}
_cameras = {}        # device -> {"percent": float, "state": str, "detail": str, "seconds": float|None}
_ready = False


def elapsed():
    return round(time.monotonic() - _started_at, 1)


def note(stage, message, level="info"):
    """ログを1行足す(stage は STAGES のキー、無ラベルなら "sys")"""
    with _lock:
        _log.append({
            "t": round(time.monotonic() - _started_at, 2),
            "stage": stage,
            "level": level,
            "msg": message,
        })


def reset(reason="再起動"):
    """テスト/再初期化用。ゲージとログを最初からにする"""
    global _started_at, _ready, _detect, _model, _cameras
    with _lock:
        _started_at = time.monotonic()
        _ready = False
        _detect = {"percent": 0.0, "detail": "検知を待っています"}
        _model = {"percent": 0.0, "state": "pending", "detail": "未開始", "seconds": None}
        _cameras = {}
        _log.clear()
        _log.append({"t": 0.0, "stage": "sys", "level": "info", "msg": "準備状態をリセット(%s)" % reason})


# ---------------- 各段階の状態更新 ----------------
def detect_percent(percent, detail):
    with _lock:
        _detect["percent"] = max(0.0, min(100.0, float(percent)))
        _detect["detail"] = detail


def _device_label(device):
    return "index %s" % device if isinstance(device, int) else str(device)


def model_started(detail="AIライブラリを読み込み中…"):
    global _model
    changed = False
    with _lock:
        if _model["state"] == "pending":
            changed = True
            _model = dict(_model, state="running", percent=max(_model["percent"], 5.0), detail=detail)
    if changed:
        note("model", detail)


def model_step(percent, detail):
    """モデル準備の途中経過(0〜100)。文言が変わればログにも残す"""
    global _model
    changed = False
    with _lock:
        if _model["detail"] != detail:
            changed = True
        _model = dict(_model, state="running",
                      percent=max(_model["percent"], float(percent)), detail=detail)
    if changed:
        note("model", "%s(%.0f%%)" % (detail, float(percent)))


def model_ready(seconds=None):
    global _model
    with _lock:
        already = _model["state"] == "ready"
        _model = {"percent": 100.0, "state": "ready", "detail": "準備完了", "seconds": seconds}
    if not already:
        note("model", "AIモデル準備 完了%s" %
             ("(%.1f秒)" % seconds if seconds is not None else ""), "ok")


def model_failed(message):
    global _model
    with _lock:
        _model = dict(_model, state="failed", detail=message[:200])


def camera_step(device, percent, state, detail, seconds=None):
    """1カメラ分の準備(確保→初フレーム→解析)。状態が変わったらログにも残す"""
    changed = False
    with _lock:
        cur = _cameras.get(device) or {"percent": 0.0, "state": "pending", "detail": "", "seconds": None}
        if cur["state"] != state:
            changed = True
        _cameras[device] = {
            "percent": max(cur["percent"], float(percent)),
            "state": state,
            "detail": detail,
            "seconds": seconds if seconds is not None else cur["seconds"],
        }
    if changed:
        level = "ok" if state in ("opened", "frame", "analyzing") else "info"
        note("camera", "%s: %s" % (_device_label(device), detail), level)


def camera_failed(device, message):
    with _lock:
        _cameras[device] = {"percent": 0.0, "state": "failed", "detail": message[:200], "seconds": None}
    note("camera", "%s: %s" % (_device_label(device), message), "error")


# ---------------- 集計 ----------------
def _camera_percent():
    """複数カメラは「一番進んだカメラ」をゲージに採用する(1台でも出れば表示できるため)"""
    with _lock:
        if not _cameras:
            return 0.0, "カメラ未確保"
        best = max(_cameras.items(), key=lambda item: item[1]["percent"])
        return best[1]["percent"], best[1]["detail"] or str(best[0])


def _detect_from_service():
    """camera_service の検知進捗をゲージの値に変換する(遅延importで循環参照を避ける)"""
    try:
        from . import camera_service
        data = camera_service.detect_progress()
    except Exception as exc:  # noqa: BLE001 - 進捗取れなければ0%のまま
        detect_percent(0.0, "検知状態を取得できません(%s)" % type(exc).__name__)
        return

    state = data.get("state")
    current = data.get("current") or 0
    total = data.get("total") or 0
    has_camera = bool(_cameras)
    if state == "done":
        detect_percent(100.0, "完了(%d台)" % len(data.get("devices") or []))
    elif state == "error":
        detect_percent(100.0, data.get("stage") or "検知に失敗")
    elif state == "running" and total:
        detect_percent(min(99.0, (current / total) * 100.0),
                       "%s (%d/%d)" % (data.get("stage") or "検知中", current, total))
    elif state == "running":
        detect_percent(10.0, data.get("stage") or "検知中")
    elif state == "idle" and has_camera:
        # 検知を待たずにカメラが確保できた(台帳のIPカメラだけ/既に開いている)場合、
        # 検知を0%のままにするとゲージが100%に届かない。実質完了として扱う
        detect_percent(100.0, "検知未実行(カメラはすでに確保済み)")
    else:
        detect_percent(0.0, "検知を待っています")


def percent():
    """重み付きの総合進捗(0〜100)"""
    with _lock:
        detect = _detect["percent"]
        model = _model["percent"]
    camera, _detail = _camera_percent()
    total = 0.0
    for key, _label, weight in STAGES:
        if key == "detect":
            value = detect
        elif key == "model":
            value = model
        elif key == "camera":
            value = min(100.0, camera)
        elif key == "frame":
            # カメラを確保できてから初めて「初フレーム待ち」に意味がある
            value = 100.0 if camera >= 70 else (40.0 if camera >= 40 else 0.0)
        else:  # analyze
            value = 100.0 if camera >= 100 else 0.0
        total += value * weight / 100.0
    return round(total, 1)


def snapshot():
    """デバッグページへ返すJSONの素"""
    global _ready
    _detect_from_service()

    with _lock:
        detect = dict(_detect)
        model = dict(_model)
        cameras = {str(k): dict(v) for k, v in _cameras.items()}
        log = list(_log)

    camera_value, camera_detail = _camera_percent()
    analyzing = any(v["state"] == "analyzing" for v in cameras.values())
    ready = model["state"] == "ready" and analyzing
    with _lock:
        _ready = ready

    camera_percent_value = min(100.0, camera_value)
    frame_value = 100.0 if camera_value >= 70 else (40.0 if camera_value >= 40 else 0.0)
    analyze_value = 100.0 if camera_value >= 100 else 0.0

    stages = []
    for key, label, weight in STAGES:
        if key == "detect":
            value, detail = detect["percent"], detect["detail"]
        elif key == "model":
            value, detail = model["percent"], model["detail"]
        elif key == "camera":
            value, detail = camera_percent_value, camera_detail
        elif key == "frame":
            value, detail = frame_value, "1枚目が読めたら100%"
        else:
            value, detail = analyze_value, "overlay配信できる状態"
        stages.append({"key": key, "label": label, "weight": weight,
                       "percent": round(value, 1), "detail": detail})

    return {
        "percent": percent(),
        "ready": ready,
        "elapsed": elapsed(),
        "stages": stages,
        "model": model,
        "cameras": cameras,
        "log": log,
    }
