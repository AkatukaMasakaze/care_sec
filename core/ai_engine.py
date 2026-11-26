"""CARE SEC 転倒・異常検知システム / リアルタイムAI解析エンジン

caregiver_safety/realtime_lstm.py の判定パイプライン

    カメラフレーム → MediaPipe Pose(33ランドマーク)
                   → 直近30フレームの姿勢系列(33×3=99値)
                   → 転倒判定LSTM / 状態判定LSTM

を **カメラ1台につき1スレッド** で実行し、骨格・判定overlayを描画したフレームを
/debug/camera/ のMJPEGストリームへ供給する。

本番想定の挙動:
- `AI_ALWAYS_ON = True` で、runserver 起動時に **検知できた全カメラ** の解析を開始する
- `/debug/camera/` を開いていない時・映像を「停止」した時も解析・異常検知・録画は走り続ける
- デバッグページでは任意のカメラを選んで「その様子を見る」だけ(見始めなくてもAIは動いている)

検知した異常は `core.models.Alert` に作成するため、
既存の /api/alerts/ ポーリング(通知音・トースト・OS通知)がそのまま効く。
自動で擬似アラートを作るシミュレーターは削除済みだが、設定ページのボタンから
**手動で1件作るデモ**は `core/demo_alerts.py` に入っている(押さない限り増えない)。

tensorflow / mediapipe / PIL が未導入の環境でも Webアプリ自体は起動できるよう、
外部ライブラリの import とモデル読み込みはすべて初回使用時(遅延)に行う。
"""
import math
import threading
import time
from collections import deque
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import camera_service
from . import readiness
from .models import AISettings, Alert, Camera, PersonState, Recording

# caregiver_safety/realtime_lstm.py と同じ判定パラメータ
SEQUENCE_LENGTH = 30
FALL_CONFIRM_COUNT = 3   # 旧単一人物版の既定。settings.AI_FALL_CONFIRM_COUNT(既定8)が優先

FOOT_INDICES = (27, 28, 29, 30, 31, 32)

# --- 複数人物検知(ai2.0 系統)---
# 顔ランドマーク(0〜10)は描画しない(AI入力にはそのまま使う)
BODY_LANDMARK_START = 11
# 肩・腰の visibility がこの値未満の人物は「顔だけ/体の欠け」扱いで対象外
BODY_VISIBILITY_THRESHOLD = 0.30
# 人物ごとの骨格色(display_id 順)。転倒中は赤に上書き
PERSON_COLORS = [
    (0, 255, 0),
    (255, 150, 0),
    (0, 255, 255),
    (255, 0, 255),
    (255, 255, 0),
]

# --- トラッキング(旧 code\ai の assign_ids/track_match_score 移植 / 2026-11-24) ---
# 旧ai2の「腰中心距離のみ」照合は、低fps/大きい移動でIDが切れて
# 人物系列が壊れる(=LSTM/速度ルールの入力が汚れる)問題があった。
# 旧aiの実績ある2段マッチング(IoU→中心距離で救済)へ全面置換する。
IOU_MATCH_THRESHOLD = 0.20     # IoU がこれ以上なら「同じ人」(1.0+iou で優先)
CENTER_MATCH_RATIO = 0.60      # IoU不足でも、中心距離が対角線×これ以内なら救済(スコア0台)
TRACK_REVIVE_STALE = 5         # これ以上消えて復活したIDは「別人化」防止で系列を破棄
# ふらつき(sway)表示の参考閾値(表示のみ。アラート化しない — 2026-11-24 決定)
SWAY_LATERAL_WARN = 0.30       # 胴体長比の横揺れ範囲
SWAY_TILT_WARN = 15.0          # 体幹傾きの変動幅(度)

# 無動作とみなす1フレームあたりのランドマーク移動量(正規化座標の平均)
# 0.004 ≒ 画面の0.4%。これ未満なら「動いていない」と判定する
STILLNESS_MOVE_THRESHOLD = 0.004

# caregiver_safety の状態ラベル → ai2 の PersonState
STATE_TO_PERSON_STATE = {
    "立っている": PersonState.STANDING,
    "歩いている": PersonState.STANDING,
    "立ち上がる": PersonState.STANDING,
    "座っている": PersonState.SITTING,
    "しゃがんでいる": PersonState.SITTING,
    "前かがみ": PersonState.SITTING,
    "横になっている": PersonState.LYING,
    "転倒": PersonState.LYING,
}

# 判定状態(caregiver_safety の current_status)→ 画面表示
STATUS_LABELS = {
    "NORMAL": "正常",
    "FALL": "転倒の疑い",
    "FALL !!!": "転倒",
    "NO PERSON": "人物なし",
}

FONT_PATH = "C:/Windows/Fonts/meiryo.ttc"


def _status_label(status):
    """判定状態の英語表記を画面表示用に変換する"""
    if status in STATUS_LABELS:
        return STATUS_LABELS[status]
    if status.startswith("WAITING"):
        return "学習中 " + status[len("WAITING"):].strip()
    return status


# --- 全スレッドで共有する重いリソース(初回使用時に読み込む) ---
_font = None
_font_loaded = False

_shared_lock = threading.Lock()
_shared = None              # numpy / Kerasモデル / 骨格接続 / 推論ロック
_landmarkers = {}           # device -> PoseLandmarker(スレッドセーフではないため1台1個)
_landmarkers_lock = threading.Lock()


class ModelLoadError(RuntimeError):
    """モデル・依存ライブラリを読み込めなかった"""


def _model_dir():
    return Path(getattr(settings, "AI_MODEL_DIR", settings.BASE_DIR / "ai_models"))


def _load_font():
    """日本語フォントを一度だけ読み込む(PILが無ければNone)"""
    global _font, _font_loaded
    if _font_loaded:
        return _font
    _font_loaded = True
    try:
        from PIL import ImageFont
        _font = ImageFont.truetype(FONT_PATH, 18)
    except Exception:
        _font = None
    return _font


def _load_shared():
    """Kerasモデル・numpy・骨格接続を一度だけ読み込み、全カメラで共有する

    MediaPipe の PoseLandmarker はスレッドセーフではないためここでは作らない
    (`_get_landmarker` がデバイスごとに1個作る)。

    所要時間は実測で10〜20秒(TensorFlow の import が最長)。`core/readiness.py` に
    途中経過を出して、ゲージが「検知だけ100%で映像は未表示」に見えないようにする。
    """
    global _shared
    with _shared_lock:
        if _shared is not None:
            return _shared

        began = time.monotonic()
        readiness.model_started("モデルファイルを確認中…")
        readiness.note("model", "AIモデルの準備を開始します")

        model_dir = _model_dir()
        pose_path = model_dir / "pose_landmarker_full.task"
        # 転倒モデルは settings.AI_FALL_MODEL_FILES の**存在する分だけアンサンブル**
        # (2026-11-24)。1件しかなければ従来どおりの単一モデル。
        fall_names = list(getattr(settings, "AI_FALL_MODEL_FILES", None) or [
            str(getattr(settings, "AI_FALL_MODEL_FILENAME", "fall_detection_lstm.keras"))])
        fall_paths = [model_dir / name for name in fall_names if (model_dir / name).exists()]
        if not fall_paths:
            # 旧名のフォールバック(設定ミスで現行モデルを壊さない)
            legacy = model_dir / "fall_detection_lstm.keras"
            if legacy.exists():
                fall_paths = [legacy]
        state_path = model_dir / "state_detection_lstm.keras"
        labels_path = model_dir / "state_labels.npy"

        check_paths = [pose_path, state_path, labels_path,
                       fall_paths[0] if fall_paths else model_dir / fall_names[0]]
        missing = [str(p) for p in check_paths if not p.exists()]
        if missing:
            message = ("モデルファイルが不足しています: " + ", ".join(missing) +
                       "(caregiver_safety の models/ dataset/ からコピーしてください)")
            readiness.model_failed(message)
            readiness.note("model", message, "error")
            raise ModelLoadError(message)

        readiness.model_step(10, "tensorflow / mediapipe を読み込み中…(ここが最長)")
        try:
            import numpy as np
            from mediapipe.tasks.python import vision
            from tensorflow.keras.models import load_model
        except Exception as exc:
            message = (
                f"AIライブラリを読み込めません: {type(exc).__name__}: {exc}"
                "(tensorflow / mediapipe / pillow を導入してください)"
            )
            readiness.model_failed(message)
            readiness.note("model", message, "error")
            raise ModelLoadError(message) from exc

        readiness.model_step(55, "Kerasモデル(LSTM)を読み込み中…")
        fall_models = [load_model(str(p)) for p in fall_paths]
        state_model = load_model(str(state_path))
        state_labels = [str(label) for label in np.load(str(labels_path), allow_pickle=True)]

        readiness.model_step(78, "推論関数を tf.function にコンパイル中…")
        fall_inferers = [_make_inferencer(m) for m in fall_models]

        def _fall_infer(inputs):
            """転倒確率 = 現存メンバーの平均(1件のときは従来動作と同一)"""
            if len(fall_inferers) == 1:
                return fall_inferers[0](inputs)
            return np.mean([f(inputs) for f in fall_inferers], axis=0)

        _shared = {
            "np": np,
            "pose_path": str(pose_path),
            "connections": vision.PoseLandmarksConnections.POSE_LANDMARKS,
            "fall_infer": _fall_infer,
            "fall_members": len(fall_inferers),
            "state_infer": _make_inferencer(state_model),
            "state_labels": state_labels,
            # Kerasモデルは複数スレッドから同時に呼ばない(推論は約7msなので直列化しても速い)
            "infer_lock": threading.Lock(),
        }
        seconds = round(time.monotonic() - began, 1)
        readiness.model_ready(seconds)
        readiness.note("model", "AIモデルの準備が完了しました(%.1f秒 / 転倒=%d名)" % (
            seconds, len(fall_inferers)), "ok")
        if bool(getattr(settings, "AI_CLIP_VERIFY", True)) and not _clip_state["loaded"]:
            _clip_warmup_async()
        return _shared


def _get_landmarker(device):
    """デバイスごとの MediaPipe PoseLandmarker を作ってキャッシュする

    注意: 解析停止時に close() しないこと。close すると同じモデルで
    2回目の解析が動かなくなる(実測)。
    """
    with _landmarkers_lock:
        if device in _landmarkers:
            return _landmarkers[device]

        shared = _load_shared()
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        landmarker = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=shared["pose_path"]),
                running_mode=vision.RunningMode.VIDEO,
                # 複数人物検知(ai2.0)。1にすると旧単一人物動作だが、
                # result.pose_landmarks はどのみちリストなのでエンジン側は共通処理
                num_poses=max(1, int(getattr(settings, "AI_MAX_POSES", 5))),
            )
        )
        _landmarkers[device] = landmarker
        readiness.note("model", "PoseLandmarker を準備しました(%s)" % _label(device))
        return landmarker


def _label(device):
    """ログに出すカメラ表記(index / IPキー)"""
    return "index %s" % device if isinstance(device, int) else "IP %s" % device


def _make_inferencer(model):
    """Kerasモデルの推論関数を作る

    Kerasの `model.predict()` は1回あたりのオーバーヘッドが大きく(約160ms)、
    `model(x, training=False)` の直接呼び出しも約400msかかる。
    `tf.function` でグラフ化すると約7msになるため、それを採用する。
    tf.function が使えない環境では predict へフォールバックする。
    """
    compiled = None
    try:
        import tensorflow as tf
        compiled = tf.function(lambda x: model(x, training=False), reduce_retracing=True)
    except Exception:
        compiled = None

    state = {"compiled": compiled, "fallback": False}

    def infer(inputs):
        if state["fallback"]:
            return model.predict(inputs, verbose=0)
        if state["compiled"] is not None:
            try:
                return state["compiled"](inputs).numpy()
            except Exception:
                state["fallback"] = True
        return model.predict(inputs, verbose=0)

    return infer


def _draw_text(frame, cv2, text, position, scale=0.8, color=(255, 255, 255), thickness=2):
    """英語の文字描画(縁取り付き)"""
    x, y = position
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _draw_japanese_overlay(frame, cv2, lines):
    """複数の日本語テキストを **1回のPIL往復だけ** で描画する

    行ごとに BGR→RGB→PIL→BGR をやると1フレームで5回の画像変換になり、
    そのスレッドがGILを長時間握ってWebリクエストが止まる実測があったため、
    まとめ描画 + stroke_width(9回ループの縁取りの代わり)で軽量にしている。
    """
    font = _load_font()
    if font is None or not lines:
        return
    try:
        from PIL import Image, ImageDraw
        import numpy as np

        pil_image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_image)
        for text, position, color in lines:
            x, y = position
            draw.text(
                (x, y), text, font=font,
                fill=(color[2], color[1], color[0]),
                stroke_width=1, stroke_fill=(0, 0, 0),
            )
        frame[:] = cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)
    except Exception:
        return


def _mp_image(rgb_frame):
    """MediaPipe用の画像オブジェクトを生成する(mediapipe未導入時は例外)"""
    import mediapipe as mp
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)


# ================= 複数人物検知のヘルパー(ai2.0 realtime_lstm.py 準拠) =================
def _normalize_pose_vector(pose_data):
    """99値 → 腰中心原点・体幹尺度(core/training.normalize_landmarks と同一計算)

    grouped_normalized 転倒モデルはこれで学習されているため、推論も必ず同じ加工にする。
    """
    import numpy as np

    lm = np.asarray(pose_data, dtype=np.float32).reshape(33, 3).copy()
    hip_center = (lm[23] + lm[24]) / 2.0
    shoulder_center = (lm[11] + lm[12]) / 2.0
    lm -= hip_center
    torso_scale = float(np.linalg.norm(shoulder_center - hip_center))
    if torso_scale > 1e-6:
        lm /= torso_scale
    return lm.flatten().tolist()


def _person_ok(landmarks):
    """体幹(肩・腰)がちゃんと映っている人物か

    顔だけ/大きく欠けた検出は Person として数えない(ai2.0の is_body_visible 相当)。
    """
    for index in (11, 12, 23, 24):
        lm = landmarks[index]
        if getattr(lm, "visibility", 1.0) < BODY_VISIBILITY_THRESHOLD:
            return False
        if not (0.0 <= lm.x <= 1.0 and 0.0 <= lm.y <= 1.0):
            return False
    shoulder_y = (landmarks[11].y + landmarks[12].y) / 2.0
    hip_y = (landmarks[23].y + landmarks[24].y) / 2.0
    return abs(hip_y - shoulder_y) > 0.08


def _person_center(landmarks):
    """腰中心(トラッキングと「左からPerson番号」の並び順用)"""
    return ((landmarks[23].x + landmarks[24].x) / 2.0,
            (landmarks[23].y + landmarks[24].y) / 2.0)


def _person_bbox(landmarks, width, height):
    """可視ランドマークを囲む矩形(頭上ラベルの位置に使う)"""
    xs, ys = [], []
    for lm in landmarks:
        if getattr(lm, "visibility", 1.0) > BODY_VISIBILITY_THRESHOLD:
            xs.append(int(lm.x * width))
            ys.append(int(lm.y * height))
    if not xs or not ys:
        return None
    return max(0, min(xs)), max(0, min(ys)), min(width - 1, max(xs)), min(height - 1, max(ys))


def _foot_visible(landmarks):
    """足(27〜32)のうち4点以上が画面内"""
    visible = sum(
        1 for idx in FOOT_INDICES
        if getattr(landmarks[idx], "visibility", 1.0) > BODY_VISIBILITY_THRESHOLD
        and 0.0 <= landmarks[idx].x <= 1.0
        and 0.0 <= landmarks[idx].y <= 1.0
    )
    return visible >= 4


def _iou(box1, box2):
    """旧ai compute_iou の移植(ピクセルbboxタプル)"""
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - inter
    return inter / union if union > 0 else 0.0


def _track_match_score(det_box, track_box):
    """旧ai track_match_score: 1.0超=IoU一致 / 0〜1=中心距離救済 / 0=不一致"""
    if det_box is None or track_box is None:
        return 0.0
    iou = _iou(det_box, track_box)
    if iou >= IOU_MATCH_THRESHOLD:
        return 1.0 + iou
    cx1, cy1 = (det_box[0] + det_box[2]) / 2.0, (det_box[1] + det_box[3]) / 2.0
    cx2, cy2 = (track_box[0] + track_box[2]) / 2.0, (track_box[1] + track_box[3]) / 2.0
    dist = ((cx1 - cx2) ** 2 + (cy1 - cy2) ** 2) ** 0.5
    diag1 = ((det_box[2] - det_box[0]) ** 2 + (det_box[3] - det_box[1]) ** 2) ** 0.5
    diag2 = ((track_box[2] - track_box[0]) ** 2 + (track_box[3] - track_box[1]) ** 2) ** 0.5
    limit = CENTER_MATCH_RATIO * max(diag1, diag2)
    if limit > 0 and dist <= limit:
        return 1.0 - 0.5 * (dist / limit)
    return 0.0


def _sway_update(track, landmarks):
    """旧ai update_pose_track の移植: 胴体中心の横揺れ範囲と傾き変動幅

    戻り値 (sway_lateral[胴体長比], sway_tilt[度])。visibility不足フレームは
    履歴へ積まない(欠けた検出で指標を汚さない — 旧aiと同じ判断)。
    """
    ls, rs = landmarks[11], landmarks[12]
    lh, rh = landmarks[23], landmarks[24]
    if all(getattr(lm, "visibility", 1.0) > 0.3 for lm in (ls, rs, lh, rh)):
        shoulder_cx = (ls.x + rs.x) / 2.0
        shoulder_cy = (ls.y + rs.y) / 2.0
        hip_cx = (lh.x + rh.x) / 2.0
        hip_cy = (lh.y + rh.y) / 2.0
        torso_x = (shoulder_cx + hip_cx) / 2.0
        torso_len = math.hypot(hip_cx - shoulder_cx, hip_cy - shoulder_cy)
        if torso_len < 0.01:
            torso_len = 0.1
        tilt = math.degrees(math.atan2(abs(shoulder_cx - hip_cx),
                                       max(1e-6, abs(shoulder_cy - hip_cy))))
        track["sway_hist"].append((torso_x, tilt, torso_len))
    hist = track["sway_hist"]
    if len(hist) < 5:
        return track["sway_lateral"], track["sway_tilt"]
    xs = [h[0] for h in hist]
    tilts = [h[1] for h in hist]
    avg_tlen = sum(h[2] for h in hist) / len(hist)
    track["sway_lateral"] = (max(xs) - min(xs)) / max(avg_tlen, 0.01)
    track["sway_tilt"] = max(tilts) - min(tilts)
    return track["sway_lateral"], track["sway_tilt"]


def _torso_tilt(landmarks):
    """体幹の傾き(度): 0=直立, 90=水平。肩中心〜腰中心のベクトルから算出

    ランドマークは正規化座標(0..1)なので解像度に依存しない。
    垂直からの角なので、人物の大きさ・位置が変わっても安定する
    (train_data 実測でこの傾き変化のAUC=0.86、0.33秒ロールAUC=0.82)。
    """
    sx = (landmarks[11].x + landmarks[12].x) / 2.0
    sy = (landmarks[11].y + landmarks[12].y) / 2.0
    hx = (landmarks[23].x + landmarks[24].x) / 2.0
    hy = (landmarks[23].y + landmarks[24].y) / 2.0
    dx = abs(sx - hx)
    dy = max(hy - sy, 1e-6)      # 肩が腰より上 = 直立方向
    return math.degrees(math.atan2(dx, dy))


def _velocity_fall_check(tilt_hist, roll_thr, window, horiz_thr, stay_deg, stay_frames):
    """速度ルール: 急激に倒れ → 水平到達 → 起き上がらない を1つの条件にする

    tilt_hist: 人物ごとの体幹傾き(度)のフレーム履歴(deque)。
    実データ(train_data 38本)で評価した結果に基づく:
      - 速度だけ(腰降下単独)は AUC 0.61 で弱い → 角度系を使う
      - 「速く倒れた」だけでは 物を拾う動作(0.33sで47°)と衝突 →
        「水平到達」+「そのまま起き上がらない」で誤発火を実測1/25本まで抑制
    戻り値: (発火bool, 理由文字列)
    """
    arr = list(tilt_hist)
    n = len(arr)
    if n < window + stay_frames:
        return False, ""
    # いま水平に近い姿勢が stay_frames 続いている(=起き上がっていない)
    if min(arr[-stay_frames:]) < stay_deg:
        return False, ""
    # 履歴中に window フレームで roll_thr° 以上の急転倒イベントがあったか
    best_roll, ev = 0.0, -1
    for i in range(window, n):
        roll = arr[i] - arr[i - window]
        if roll > best_roll:
            best_roll, ev = roll, i
    if best_roll < roll_thr:
        return False, ""
    # そのイベントの終わりに水平(horizontal)へ到達しているか
    if arr[ev] < horiz_thr:
        return False, ""
    return True, "roll=%.0f°/%dF→%.0f°" % (best_roll, window, arr[ev])


# ================= CLIP アラート検証官(旧 code\ai 転用 / 2026-11-24) =================
# 旧aiはCLIPを主検知に使って「微妙」だった。使い方を**アラート瞬間だけの
# ゼロショット検証**に変えると効く: 学習不要で「床に倒れている人」の意味を確認し、
# しゃがみ/前かがみ誤検知を拒否できる。
# 設計方針: **フェイルオープンの拒否権のみ** — CLIPが居ない/壊れたときは
# 従来どおりアラートを通す(検知を止めない)。肯定で信頼度を上げることはしない。
_clip_state = {"loaded": False, "failed": False, "model": None, "processor": None}
# RLock: _clip_verify_lying が非ブロック取得後に _ensure_clip へ入る(同一スレッド再入)
_clip_lock = threading.RLock()

CLIP_PROMPTS = (
    "a photo of a person lying on the floor",
    "a photo of a person standing or sitting upright",
)


def _ensure_clip():
    """CLIPモデルを1回だけ読む込こむ(多重呼び出しは lock で直列化)"""
    if _clip_state["loaded"] or _clip_state["failed"]:
        return _clip_state["loaded"]
    with _clip_lock:
        if _clip_state["loaded"] or _clip_state["failed"]:
            return _clip_state["loaded"]
        try:
            from transformers import CLIPModel, CLIPProcessor
            name = str(getattr(settings, "AI_CLIP_MODEL", "openai/clip-vit-base-patch32"))
            began = time.monotonic()
            _clip_state["model"] = CLIPModel.from_pretrained(name)
            _clip_state["processor"] = CLIPProcessor.from_pretrained(name)
            _clip_state["loaded"] = True
            print("[RealtimeAI] CLIP検証官 準備完了: %s (%.1f秒)" % (name, time.monotonic() - began))
        except Exception as exc:  # noqa: BLE001 -検証官の失敗で検知を止めない
            _clip_state["failed"] = True
            print("[RealtimeAI] CLIP検証官を利用不可として扱います(アラートは従来どおり): "
                  "%s: %s" % (type(exc).__name__, exc))
    return _clip_state["loaded"]


def _clip_warmup_async():
    """起動時に裏スレッドでCLIPを読む込み込み。

    必要な理由: transformers/torch の初回 import とモデル読込(~600MB)は 10〜30秒。
    これを「最初のアラート瞬間」に解析ループ内でやると、そのカメラが数十秒止まる
    (スレッド内初回importは20秒級、という実測教訓と同じ罠)。裏で済ませる。
    途中でもアラートが来たら _clip_verify_lying 側は**待たずに素通し**する
    (検証官の準備中を検知遅延にしない=フェイルオープン原則)。
    """
    threading.Thread(target=_ensure_clip, daemon=True, name="clip-warmup").start()


def _clip_verify_lying(frame, cv2, bbox):
    """True=寝倒れ確認 / False=拒否 / None=素通し(準備中・CLIP不在・故障)"""
    if _clip_state["failed"]:
        return None
    if not _clip_state["loaded"]:
        # 起動時のwarmupが終わっていないだけ。ここで待たない(検知を止めない)
        if not _clip_lock.acquire(blocking=False):
            return None
        try:
            if not _ensure_clip():
                return None
        finally:
            _clip_lock.release()
    try:
        import torch
        from PIL import Image

        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        mx = int((x2 - x1) * 0.15)
        my = int((y2 - y1) * 0.15)
        crop = frame[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
        if crop.size == 0:
            return None
        pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        inputs = _clip_state["processor"](text=list(CLIP_PROMPTS), images=pil,
                                          return_tensors="pt", padding=True)
        with torch.no_grad():
            out = _clip_state["model"](**inputs)
            probs = torch.softmax(out.logits_per_image[0], dim=-1).tolist()
        return bool(float(probs[0]) >= float(getattr(settings, "AI_CLIP_MIN_LYING", 0.5)))
    except Exception as exc:  # noqa: BLE001 -検証官の失敗で検知を止めない
        _clip_state["failed"] = True
        print("[RealtimeAI] CLIP検証官を利用不可として扱います(アラートは従来どおり): "
              "%s: %s" % (type(exc).__name__, exc))
        return None


def _new_track(uid, center):
    """1人物分の追跡状態(ai2.0 の create_track 相当)

    sequence      : 正規化99値のdeque(転倒LSTM入力)
    state_sequence: 生99値のdeque(状態LSTM入力 — 状態モデルは非正規化で学習済み)
    tilt_hist     : 体幹傾きの履歴(速度ルール用)
    """
    return {
        "id": uid,
        "display_id": uid,
        "center": center,
        "box": None,
        "lost": 0,
        "sequence": deque(maxlen=SEQUENCE_LENGTH),
        "state_sequence": deque(maxlen=SEQUENCE_LENGTH),
        "tilt_hist": deque(maxlen=SEQUENCE_LENGTH),
        "velocity_fall": False,
        "velocity_why": "",
        "velocity_up_count": 0,
        "sway_hist": deque(maxlen=30),
        "sway_lateral": 0.0,
        "sway_tilt": 0.0,
        "clip_last_at": 0.0,
        "clip_rejects": 0,
        "status": "WAITING",
        "state_label": "判定中",
        "danger_percent": None,
        "fall_count": 0,
        "fall_latched": False,
        "foot_visible": False,
        "missing": 0,
        "inference_counter": 0,
        "prev_points": None,
        "still_since": None,
        "still_seconds": 0,
        "still_alerted": False,
        "smooth": None,
        "bbox": None,
    }


# ================= 1台分の解析ランタイム =================
class _CameraRuntime:
    """1カメラ分の解析スレッド・視聴者数・最新映像・状態を持つ"""

    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.session = 0          # 開始ごとに増えるID(古い接続の切断を誤って拾わないため)
        self.viewers = 0          # ブラウザで映像を見ている人数
        self.persistent = False   # 常時稼働(見る人がいなくても止まらない)
        self.jpeg = None
        self.frame_seq = 0          # 解析済みJPEGの更新回数(新しいフレームか否かの判定用)
        self.status = self._fresh_status()
        self.last_frame_at = time.monotonic()   # 実際にフレームを読めた最終時刻
        self.defer_until = 0.0                  # 映像が取れず停止したカメラの再試行抑止時刻

    # ---------------- 状態のやり取り ----------------
    @staticmethod
    def _fresh_status():
        return {
            "device": None,
            "phase": "stopped",   # stopped / loading / running / error
            "error": "",
            "model_ready": False,
            "status": "-",
            "status_label": "待機中",
            "state": "-",
            "danger_percent": None,
            "foot_visible": False,
            "still_seconds": 0,
            "sequence_frames": 0,
            "people": 0,
            "people_detail": [],
            "sway_lateral": 0.0,
            "sway_tilt": 0.0,
            "fps": 0.0,
            "person_present": False,
            "ai_throttled": False,
            "pose_fps": None,
            "frame_age": None,
            "last_alert": None,
            "recording_seconds_left": None,
        }

    def _set_status(self, **kwargs):
        with self.lock:
            self.status.update(kwargs)

    def _store_frame(self, jpeg, **status):
        with self.lock:
            self.jpeg = jpeg
            self.frame_seq += 1      # 配信側が「新フレームか」を判定できるよにする
            self.status.update(status)

    def snapshot(self):
        """画面へ返す状態のスナップショット"""
        with self.lock:
            data = dict(self.status)
            data["device"] = self.device
            data["running"] = bool(self.thread is not None and self.thread.is_alive())
            data["persistent"] = self.persistent
            data["viewers"] = self.viewers
        try:
            ai = AISettings.load()
            data["stillness_threshold"] = ai.stillness_seconds
            data["fall_threshold"] = round(float(ai.fall_threshold), 2)
        except Exception:  # DB未初期化などの場合は表示だけ省く
            data["stillness_threshold"] = None
            data["fall_threshold"] = None
        return data

    def has_browser_viewer(self):
        """ブラウザで映像を見ている人がいるか(常時稼働ぶんの自己視聴は数えない)"""
        with self.lock:
            return self.viewers > (1 if self.persistent else 0)

    # ---------------- ライフサイクル ----------------
    def attach(self):
        """視聴者として解析を開始(または参加)し、セッションIDを返す"""
        with self.lock:
            alive = self.thread is not None and self.thread.is_alive()
            if alive:
                self.viewers += 1
                return self.session
            self.stop = threading.Event()
            self.session += 1
            self.viewers = 1
            self.jpeg = None
            self.status = self._fresh_status()
            self.status.update(phase="loading", device=self.device)
            thread = threading.Thread(
                target=self._run,
                args=(self.stop,),
                name=f"realtime-ai-{self.device}",
                daemon=True,
            )
            self.thread = thread
            thread.start()
            return self.session

    def detach(self, session=None):
        """視聴者が離れたとき。常時稼働でなければ最後の1人で解析を止める"""
        with self.lock:
            if self.thread is None:
                return
            if session is not None and session != self.session:
                return
            self.viewers = max(0, self.viewers - 1)
            if self.viewers > 0 or self.persistent:
                return
            thread, self.thread = self.thread, None
            self.stop.set()
        if thread is not None:
            thread.join(timeout=30)
        with self.lock:
            if session is None or session == self.session:
                self.jpeg = None
                self.status = self._fresh_status()

    def stop_analysis(self):
        """常時稼働も含めて止める(テスト・シャットダウン用)"""
        with self.lock:
            self.persistent = False
            thread, self.thread = self.thread, None
            self.viewers = 0
            self.stop.set()
        if thread is not None:
            thread.join(timeout=30)
        with self.lock:
            self.jpeg = None
            self.status = self._fresh_status()

    # ---------------- 解析ループ ----------------
    def _run(self, stop):
        """解析スレッド本体"""
        try:
            shared = _load_shared()
            landmarker = _get_landmarker(self.device)
        except Exception as exc:
            self._set_status(phase="error", error=f"{type(exc).__name__}: {exc}")
            readiness.model_failed("%s: %s" % (type(exc).__name__, exc))
            readiness.note("model", "モデル読み込み失敗: %s" % exc, "error")
            print(f"[RealtimeAI:{self.device}] モデル読み込み失敗: {exc}")
            return

        self._set_status(phase="loading", model_ready=True)

        # キャプチャを「使用中」と宣言する。
        # この間 他スレッドが camera_service.release() を呼んでも cap を閉じさせない
        # (読み取り中に閉じると native層で use-after-free → プロセスごとクラッシュする)。
        held = camera_service.hold(self.device)
        if not held:
            self._set_status(
                phase="error",
                error="カメラが確保されていないため解析を開始できません",
            )
            print(f"[RealtimeAI:{self.device}] キャプチャが見つかりません")
            return

        try:
            self._loop(stop, shared, landmarker)
        except Exception as exc:
            self._set_status(phase="error", error=f"{type(exc).__name__}: {exc}")
            print(f"[RealtimeAI:{self.device}] 解析エラー: {exc}")
            return
        finally:
            camera_service.unhold(self.device)

        # landmarker はキャッシュして再利用するため close しない(close すると2回目が動かない)
        with self.lock:
            already_error = self.status.get("phase") == "error"
        if not already_error:
            self._set_status(phase="stopped")

    def _loop(self, stop, shared, landmarker):
        import cv2

        np = shared["np"]
        pose_connections = shared["connections"]
        fall_infer = shared["fall_infer"]
        state_infer = shared["state_infer"]
        state_labels = shared["state_labels"]
        infer_lock = shared["infer_lock"]

        device = self.device
        fps_limit = max(1, int(getattr(settings, "AI_ANALYSIS_FPS", 10)))
        interval = 1.0 / fps_limit
        quality = int(getattr(settings, "AI_JPEG_QUALITY", 80))

        started = time.monotonic()
        last_analysis = 0.0
        frame_timestamp = 0

        # --- 無人カメラの間引き(AI最適化)---
        # 実測で POSE が 31.6ms/枚(全工程 49.6ms の64%)。人が映っていないフレームで
        # 打っても空振りなので、無人が続く間だけ「probe 秒に1回探す」に落とす。
        # 人が出た瞬間(=ランドマークを拾えた瞬間)にフルレートへ戻る。
        # 無動作検知は「人がいて動かない」なので、有人判定中は一切間引かない。
        optimize_empty = bool(getattr(settings, "AI_EMPTY_CAMERA_OPTIMIZATION", True))
        probe_seconds = max(0.05, float(getattr(settings, "AI_EMPTY_PROBE_SECONDS", 0.2)))
        lost_hold = max(0.0, float(getattr(settings, "AI_PERSON_LOST_SECONDS", 2.0)))
        person_present = False       # 直近のPOSEで人が検出できたか
        last_person_seen = None      # 最後にランドマークを拾った時刻(monotonic)
        next_pose_at = 0.0           # 間引き中に次にPOSEを打つ時刻
        empty_throttling = False     # UI表示用(このカメラが今間引き中か)
        pose_count = 0               # 直近1秒ウィンドウで実際にPOSEを打った回数

        # AI設定(転倒閾値・無動作秒数・クールダウン)は2秒ごとに読み直す
        ai = AISettings.load()
        ai_loaded_at = started

        # 判定状態(人物単位。ai2.0 realtime_lstm.py の tracks 辞書相当)
        tracks = {}
        next_track_uid = 1
        # 画面全体の集約表示は「最悪の人物」を反映する
        status = "WAITING (0/30)"
        state_label = "判定中"
        danger_percent = None
        foot_visible = False
        still_seconds = 0
        sequence_frames = 0
        people = 0
        people_detail = []

        # --- ai2.0 統合パラメータ ---
        confirm_count = max(1, int(getattr(settings, "AI_FALL_CONFIRM_COUNT", FALL_CONFIRM_COUNT)))
        infer_interval = max(1, int(getattr(settings, "AI_INFERENCE_INTERVAL", 5)))
        # (旧)track_distance は 2026-11-24 の IoUトラッカー移植により未使用化
        track_max_missing = max(1, int(getattr(settings, "AI_TRACK_MAX_MISSING", 15)))
        normalize_fall = bool(getattr(settings, "AI_NORMALIZE_FALL_INPUT", True))
        process_w = max(64, int(getattr(settings, "AI_POSE_PROCESS_WIDTH", 320)))
        process_h = max(48, int(getattr(settings, "AI_POSE_PROCESS_HEIGHT", 240)))
        smoothing = min(1.0, max(0.0, float(getattr(settings, "AI_SKELETON_SMOOTHING_ALPHA", 0.35))))

        # --- 速度ルール(第2の転倒検出器: 急激な崩れをLSTMの安全網としてOR判定) ---
        velocity_rule = bool(getattr(settings, "AI_VELOCITY_FALL_RULE", True))
        v_roll_thr = float(getattr(settings, "AI_VELOCITY_ROLL_DEG", 25.0))
        v_window = max(2, int(getattr(settings, "AI_VELOCITY_ROLL_WINDOW", 10)))
        v_horiz_thr = float(getattr(settings, "AI_VELOCITY_HORIZONTAL_DEG", 60.0))
        v_stay_deg = float(getattr(settings, "AI_VELOCITY_STAY_DEG", 45.0))
        v_stay_frames = max(2, int(getattr(settings, "AI_VELOCITY_STAY_FRAMES", 15)))
        v_confidence = min(0.99, max(0.0, float(getattr(settings, "AI_VELOCITY_CONFIDENCE", 0.90))))
        v_release_frames = max(2, int(getattr(settings, "AI_VELOCITY_RELEASE_FRAMES", 10)))

        # CLIP検証官(旧ai転用 / 2026-11-24)
        clip_verify = bool(getattr(settings, "AI_CLIP_VERIFY", True))
        clip_interval = max(2.0, float(getattr(settings, "AI_CLIP_CHECK_SECONDS", 10)))

        # 統計
        window_start = started
        window_count = 0
        current_fps = 0.0
        pose_fps = None            # 実際にPOSEを打った回数/秒(間引き中は 1/probe 前後)

        # 起動準備ゲージ用(core/readiness.py)
        got_first_frame = False
        announced_running = False

        # 異常検知からの自動録画(検知後 N 秒間を保存して Recording にする)
        record_seconds = int(getattr(settings, "AI_RECORDING_SECONDS", 8))
        rec_until = None      # 録画終了予定時刻(monotonic)
        rec_frames = []       # JPEGバイト列のバッファ
        rec_meta = None       # 書き出しに必要な情報

        # 映像途絶の検出(カメラを抜いた時にハングしたcapを握り続けないため)
        lost_since = None
        lost_timeout = float(getattr(settings, "AI_FRAME_LOST_SECONDS", 20))
        snapshot_saved = False   # 一覧用のサムネイルをこのセッションで保存したか

        while not stop.is_set():
            close_old_connections()

            now = time.monotonic()
            if now - last_analysis < interval:
                time.sleep(0.005)
                continue
            last_analysis = now

            # 誰も映像を見ていない時は描画・JPEG化を省略する
            # (日本語overlay+JPEGを毎フレームやるとGILを握り続けてWebが止まる実測がある)
            # 録画中は保存用フレームが必要なので描画する。
            draw = self.has_browser_viewer() or rec_until is not None

            frame = camera_service.read_frame(device)
            if frame is None:
                lost_since = lost_since or now
                if now - lost_since >= lost_timeout:
                    # 抜去/故障。ハングしたcapを握らずスレッドを終える(unholdで閉じる)
                    self._set_status(
                        phase="error",
                        error=(
                            f"カメラ(index {device})からの映像が"
                            f"{int(lost_timeout)}秒以上途絶えました(接続を確認してください)"
                        ),
                    )
                    readiness.camera_failed(device, "映像が %d 秒以上途絶" % int(lost_timeout))
                    readiness.note("camera", "%s の映像が途絶しました" % _label(device), "error")
                    print(f"[RealtimeAI:{device}] 映像途絶 / 解析スレッドを終了します")
                    break
                time.sleep(0.05)
                continue
            lost_since = None
            self.last_frame_at = time.monotonic()

            if not got_first_frame:
                got_first_frame = True
                readiness.camera_step(
                    device, 70, "frame",
                    "初フレーム取得(サーバー起動から%.1f秒)" % readiness.elapsed(),
                )
                readiness.note("frame", "%s の初フレームを読みました" % _label(device), "ok")

            # 初フレームだけサムネイルを保存(一覧で「どれがどのカメラか」確認できる)
            # 検知はカメラを掴んでいると開けないので、解析側から残すのが確実
            if not snapshot_saved:
                snapshot_saved = True
                camera_service.save_snapshot(device, frame)

            # 左右反転(既定 OFF)
            # caregiver_safety/realtime_lstm.py は推論前に flip するが、
            # 学習データを作った collect_data.py は flip していない。
            # 学習時と違う向きで推論すると判定がずれ、映像も反転して見えるため、
            # ここでは **学習データの向きと揃える**(反転しない)のが既定。
            # 元の挙動に戻したい場合だけ settings.AI_MIRROR_INPUT = True にする。
            if bool(getattr(settings, "AI_MIRROR_INPUT", False)):
                frame = cv2.flip(frame, 1)
            height, width = frame.shape[:2]

            # 転倒判定の閾値は ai2 のAI設定に合わせる(誤検知FBで自動調整される)
            if now - ai_loaded_at >= 2.0:
                ai = AISettings.load()
                ai_loaded_at = now
            threshold = float(ai.fall_threshold)

            # --- 間引きゲート ---
            # 無人が続いている間は probe_seconds に1回だけ POSE を打つ。
            # 「人が消えてから lost_hold 秒」は猶予期間(遮挡や検出の不安定で
            # 転倒の瞬間を取り逃さないためフルレートを維持する)。
            run_pose = True
            if optimize_empty and not person_present:
                since_seen = (now - last_person_seen) if last_person_seen is not None else float("inf")
                if since_seen >= lost_hold:
                    if now < next_pose_at:
                        run_pose = False
                    else:
                        next_pose_at = now + probe_seconds

            # UI表示用: このカメラが今「無人間引きモード」に進入しているか
            # (猶予期間の2秒間は still フルレートなので empty とは数えない)
            if person_present:
                empty_throttling = False
            elif optimize_empty:
                since_seen = (now - last_person_seen) if last_person_seen is not None else float("inf")
                if since_seen >= lost_hold:
                    empty_throttling = True

            if run_pose:
                # ai2.0準拠: POSE は縮小画像(既定320x240)で打つ。
                # ランドマークは 0..1 の正規化座標なので、描画もAI入力も解像度に依存しない。
                if (width, height) != (process_w, process_h):
                    process_frame = cv2.resize(
                        frame, (process_w, process_h), interpolation=cv2.INTER_AREA)
                else:
                    process_frame = frame
                rgb_frame = cv2.cvtColor(process_frame, cv2.COLOR_BGR2RGB)
                # MediaPipe VIDEOモードは「単調増加」のタイムスタンプを要求する。
                # 再開時に0へ巻き戻らないよう monotonic の時刻を使い、同値も防ぐ
                frame_timestamp = max(int(time.monotonic() * 1000), frame_timestamp + 1)
                result = landmarker.detect_for_video(_mp_image(rgb_frame), frame_timestamp)
                pose_count += 1
            else:
                result = None

            # ---------------- 検出 → 人物トラック ----------------
            # 顔だけ/体幹欠けの検出は Person に数えない(ai2.0の is_body_visible 相当)
            detections = []
            if result is not None and result.pose_landmarks:
                for landmarks in result.pose_landmarks:
                    if not _person_ok(landmarks):
                        continue
                    bbox = _person_bbox(landmarks, width, height)
                    if bbox is None:
                        continue
                    detections.append({
                        "landmarks": landmarks,
                        "center": _person_center(landmarks),
                        "bbox": bbox,
                    })
            # 左から Person 1..N(ai2.0と同じ並び順)
            detections.sort(key=lambda d: d["center"][0])

            for track in tracks.values():
                track["missing"] += 1

            # --- 旧ai assign_ids 移植: IoU+中心距離の2段マッチング(高スコア順貪欲) ---
            track_list = list(tracks.values())
            pairs = []
            for di, det in enumerate(detections):
                for ti, cand in enumerate(track_list):
                    score = _track_match_score(det["bbox"], cand["box"])
                    if score > 0.0:
                        pairs.append((score, di, ti))
            pairs.sort(key=lambda pair: pair[0], reverse=True)

            assigned = {}
            matched_ti = set()
            for _score, di, ti in pairs:
                if di in assigned or ti in matched_ti:
                    continue
                assigned[di] = track_list[ti]
                matched_ti.add(ti)

            visible_tracks = []
            for display_index, detection in enumerate(detections, start=1):
                cx, cy = detection["center"]
                track = assigned.get(display_index - 1)
                if track is None:
                    track = _new_track(next_track_uid, (cx, cy))
                    tracks[next_track_uid] = track
                    next_track_uid += 1
                elif track["missing"] >= TRACK_REVIVE_STALE:
                    # 長期間消えて復活したトラック = 別人/テレポートの恐れ。
                    # 系列を捨ててやり直す(旧aiの「キャッシュ破棄で判定し直し」相当。
                    # 壊れた系列がLSTM/速度ルールに混入するのを防ぐ)
                    track["sequence"].clear()
                    track["state_sequence"].clear()
                    track["tilt_hist"].clear()
                    track["sway_hist"].clear()
                    track["prev_points"] = None
                    track["fall_count"] = 0
                    track["velocity_fall"] = False
                    track["velocity_up_count"] = 0
                    track["still_since"] = None
                    track["smooth"] = None
                track["missing"] = 0

                landmarks = detection["landmarks"]
                track["display_id"] = display_index
                track["center"] = (cx, cy)
                track["bbox"] = detection["bbox"]
                track["box"] = detection["bbox"]

                # 33ランドマーク × x,y,z = 99値
                pose_data = []
                points = []
                for lm in landmarks:
                    pose_data.extend([lm.x, lm.y, lm.z])
                    points.append((lm.x, lm.y))
                # 転倒系列は正規化(新モデルの学習仕様)、状態系列は生の99値
                track["sequence"].append(
                    _normalize_pose_vector(pose_data) if normalize_fall else pose_data)
                track["state_sequence"].append(pose_data)

                # --- 人物ごとの無動作(前フレームとの移動量) ---
                if track["prev_points"] is None:
                    moving = True
                else:
                    move = sum(
                        abs(a[0] - b[0]) + abs(a[1] - b[1])
                        for a, b in zip(points, track["prev_points"])
                    ) / max(1, len(points))
                    moving = move >= STILLNESS_MOVE_THRESHOLD
                track["prev_points"] = points
                if moving:
                    track["still_since"] = now
                    track["still_alerted"] = False
                elif track["still_since"] is None:
                    track["still_since"] = now
                track["still_seconds"] = int(now - track["still_since"])

                track["foot_visible"] = _foot_visible(landmarks)

                # ふらつき計測(旧ai update_pose_track 移植。表示専用の指標)
                _sway_update(track, landmarks)

                # 速度ルール: 体幹傾きの履歴を積んで毎フレーム評価(引き算だけのコスト)
                # 一度発火したら「倒れたまま」の間はラッチする。
                # 理由: 急転倒イベント自体は30フレーム(~1秒)で履歴窓から滑り落ちるため、
                # 一時的信号のままだとLSTM確認(8回≈1.3秒)に到底届かない。
                # 「倒れた→水平のまま」は物理的に転倒継続なのでラッチが正しい。
                if velocity_rule:
                    tilt_now = _torso_tilt(landmarks)
                    track["tilt_hist"].append(tilt_now)
                    if track["velocity_fall"]:
                        if tilt_now < v_stay_deg:
                            track["velocity_up_count"] += 1
                            if track["velocity_up_count"] >= v_release_frames:
                                # 起き上がった(立て直しが一定フレーム続く)
                                track["velocity_fall"] = False
                                track["velocity_why"] = ""
                                track["velocity_up_count"] = 0
                        else:
                            track["velocity_up_count"] = 0
                    else:
                        ok, why = _velocity_fall_check(
                            track["tilt_hist"], v_roll_thr, v_window,
                            v_horiz_thr, v_stay_deg, v_stay_frames)
                        if ok:
                            track["velocity_fall"] = True
                            track["velocity_why"] = why
                            track["velocity_up_count"] = 0
                            print(f"[RealtimeAI:{device}] 速度ルール発火: "
                                  f"Person {track['display_id']} ({why})")

                # 骨格描画用のEMA平滑化(AI入力には影響しない)
                current = np.asarray(pose_data, dtype=np.float32).reshape(33, 3)
                if track["smooth"] is None:
                    track["smooth"] = current.copy()
                else:
                    track["smooth"] = track["smooth"] * (1.0 - smoothing) + current * smoothing

                visible_tracks.append(track)

            # しばらく検出されなかった人物は削除(一時的な遮挡は系列ごと保持される)
            for uid in [t["id"] for t in tracks.values() if t["missing"] > track_max_missing]:
                del tracks[uid]

            person_present = bool(visible_tracks)
            if person_present:
                last_person_seen = now

            # ---------------- 人物まとめてバッチ推論 ----------------
            # 1人ずつ毎回ではなく、READYになった人物を **1回の (N,30,99) 推論**で処理。
            # infer_lock の直列占有が台数×人数から (人数/interval) 分の1に減る
            # (第37章の実測「4台でLSTM直列110%」へのai2.0側の回答)。
            ready_tracks = []
            for track in visible_tracks:
                if len(track["sequence"]) < SEQUENCE_LENGTH:
                    track["status"] = "WAITING"
                    track["fall_count"] = 0
                    continue
                track["inference_counter"] += 1
                if track["inference_counter"] >= infer_interval:
                    track["inference_counter"] = 0
                    ready_tracks.append(track)

            if ready_tracks:
                fall_inputs = np.asarray(
                    [list(t["sequence"]) for t in ready_tracks], dtype=np.float32)
                state_inputs = np.asarray(
                    [list(t["state_sequence"]) for t in ready_tracks], dtype=np.float32)
                with infer_lock:
                    fall_probs = fall_infer(fall_inputs)
                    state_probs = state_infer(state_inputs)
                for i, track in enumerate(ready_tracks):
                    probability = float(fall_probs[i][0])
                    # 速度ルール発火は「確実な転倒票」としてLSTMとOR判定する
                    # (信頼度は表示上も v_confidence 以上に見えるようにする)
                    if velocity_rule and track["velocity_fall"]:
                        probability = max(probability, v_confidence)
                    track["danger_percent"] = probability * 100.0
                    if probability >= threshold:
                        track["fall_count"] += 1
                        track["status"] = "FALL"
                    else:
                        track["fall_count"] = 0
                        track["status"] = "NORMAL"
                    if track["fall_count"] >= confirm_count:
                        track["status"] = "FALL !!!"
                    state_id = int(np.argmax(state_probs[i]))
                    predicted_state = (
                        state_labels[state_id] if state_id < len(state_labels) else "不明")
                    track["state_label"] = (
                        "転倒" if track["status"] == "FALL !!!" else predicted_state)

            # ---------------- 画面全体の表示は「最悪の人物」 ----------------
            people = len(visible_tracks)
            if visible_tracks:
                fall_tracks = [t for t in visible_tracks if t["status"] == "FALL !!!"]
                danger_tracks = [t for t in visible_tracks if t["status"] == "FALL"]
                if fall_tracks:
                    worst = fall_tracks[0]
                    status = "FALL !!!"
                elif danger_tracks:
                    worst = danger_tracks[0]
                    status = "FALL"
                else:
                    judged = [t for t in visible_tracks if t["status"] == "NORMAL"]
                    if judged:
                        worst = max(judged, key=lambda t: t["danger_percent"] or 0.0)
                        status = "NORMAL"
                    else:
                        worst = max(visible_tracks, key=lambda t: len(t["sequence"]))
                        status = "WAITING"
                state_label = worst["state_label"]
                danger_percent = worst["danger_percent"]
                foot_visible = any(t["foot_visible"] for t in visible_tracks)
                still_seconds = max(t["still_seconds"] for t in visible_tracks)
                sequence_frames = max(len(t["sequence"]) for t in visible_tracks)
                if status == "WAITING":
                    status = f"WAITING ({sequence_frames}/{SEQUENCE_LENGTH})"
                people_detail = [
                    {
                        "id": t["display_id"],
                        "status": t["status"],
                        "state": t["state_label"],
                        "danger": round(t["danger_percent"], 1)
                        if t["danger_percent"] is not None else None,
                        "velocity_fall": bool(t["velocity_fall"]),
                        "sway_lateral": round(t["sway_lateral"], 2),
                        "sway_tilt": round(t["sway_tilt"], 1),
                    }
                    for t in visible_tracks
                ]
            else:
                # 人が映っていない(または間引き中で POSE を打っていない)
                # トラック自体は track_max_missing まで系列を保持するので、
                # 人が戻っても WAITING からやり直しにならない
                status = "NO PERSON"
                state_label = "判定中"
                danger_percent = None
                foot_visible = False
                still_seconds = 0
                sequence_frames = 0
                people_detail = []

            # ---------------- 異常検知(Alert作成 → /api/alerts/ 通知) ----------------
            alert_info = None
            # 転倒: **人物ごとに1件**(ラッチも人物単位。2人目の転倒は黙らない)
            for track in visible_tracks:
                if track["status"] == "FALL !!!" and not track["fall_latched"]:
                    # CLIP検証官(旧ai転用): アラート瞬間だけゼロショットで
                    # 「床に倒れている」か確認し、否定したら今回のアラートを拒否。
                    # 人物あたり clip_interval 秒に1回まで(検証1回=CPUで100〜300ms)。
                    # None=CLIP不在=素通し(フェイルオープン。検知を止めない)
                    if clip_verify and track["bbox"] is not None:
                        if now - track["clip_last_at"] >= clip_interval:
                            track["clip_last_at"] = now
                            verdict = _clip_verify_lying(frame, cv2, track["bbox"])
                        else:
                            verdict = False if track["clip_rejects"] else None
                        if verdict is False:
                            track["clip_rejects"] += 1
                            print(f"[RealtimeAI:{device}] CLIPが転倒を拒否"
                                  f"(しゃがみ/前かがみの可能性): Person {track['display_id']}"
                                  f" 累計{track['clip_rejects']}回")
                            continue
                    # 信頼度は ai2 の規約どおり 0.0〜1.0 で保存する
                    alert_info = _raise_alert(
                        device, Alert.Kind.FALL, (track["danger_percent"] or 0.0) / 100.0,
                        PersonState.LYING, ai, frame=frame,
                        person_id=track["display_id"],
                    ) or alert_info
                    track["fall_latched"] = True
                if track["fall_count"] == 0:
                    track["fall_latched"] = False

            # 無動作: いずれかの人物が閾値超(その人物で1件)
            for track in visible_tracks:
                if (
                    track["status"] == "NORMAL"
                    and track["still_seconds"] >= ai.stillness_seconds
                    and not track["still_alerted"]
                ):
                    confidence = min(0.99, 0.55 + track["still_seconds"] / 200)
                    alert_info = _raise_alert(
                        device, Alert.Kind.SUSPICIOUS, confidence,
                        STATE_TO_PERSON_STATE.get(track["state_label"], PersonState.UNKNOWN),
                        ai, frame=frame, person_id=track["display_id"],
                    ) or alert_info
                    track["still_alerted"] = True

            # 検知できたら録画開始(検知後 record_seconds 秒間)
            if alert_info is not None and record_seconds > 0:
                rec_until = now + record_seconds
                rec_frames = []
                rec_meta = {
                    "alert_id": alert_info.get("id"),
                    "raw_kind": alert_info.get("raw_kind", Alert.Kind.FALL),
                    "camera_id": alert_info.get("camera_id"),
                    "occurred_at": alert_info.get("occurred_at"),
                    "fps": max(1, fps_limit),
                    "width": width,
                    "height": height,
                }

            # ---------------- JPEG化(見る人がいる時だけ) ----------------
            # 2026-11-17 ご要望により、映像への上書きテキスト(タイトル/状態/判定/
            # 危険度/足/無動作、人物ごとの頭上ラベル)は描画しない。
            # 骨格・バウンディングボックス・転倒中の赤枠は「写っているもの」の
            # 可視化なので残す。ページ側(デバッグカード等)の状態表示は無変更。
            buf = None
            if draw:
                for track in visible_tracks:
                    is_fall = track["status"] == "FALL !!!"
                    color = (0, 0, 255) if is_fall else PERSON_COLORS[
                        (track["display_id"] - 1) % len(PERSON_COLORS)]
                    smooth = track["smooth"]

                    # 骨格線(顔の線は描かない — AI入力には顔を含むまま使用)
                    for connection in pose_connections:
                        s_idx, e_idx = connection.start, connection.end
                        if s_idx < BODY_LANDMARK_START or e_idx < BODY_LANDMARK_START:
                            continue
                        x1, y1 = int(smooth[s_idx][0] * width), int(smooth[s_idx][1] * height)
                        x2, y2 = int(smooth[e_idx][0] * width), int(smooth[e_idx][1] * height)
                        if 0 <= x1 < width and 0 <= y1 < height and 0 <= x2 < width and 0 <= y2 < height:
                            cv2.line(frame, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
                    for point_index in range(BODY_LANDMARK_START, 33):
                        x, y = int(smooth[point_index][0] * width), int(smooth[point_index][1] * height)
                        if 0 <= x < width and 0 <= y < height:
                            cv2.circle(frame, (x, y), 3, color, -1, cv2.LINE_AA)

                    bbox = track["bbox"]
                    if bbox:
                        x_min, y_min, x_max, y_max = bbox
                        cv2.rectangle(frame, (x_min, y_min), (x_max, y_max), color, 2)

                if status == "FALL !!!":
                    cv2.rectangle(frame, (5, 5), (width - 6, height - 6), (0, 0, 255), 6)

                ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                if ok:
                    buf = encoded.tobytes()

            # ---------------- 自動録画のフレーム収集 ----------------
            if rec_until is not None and buf is not None:
                rec_frames.append(buf)
                if now >= rec_until:
                    _finish_recording(rec_frames, rec_meta)
                    rec_until, rec_frames, rec_meta = None, [], None

            window_count += 1
            if now - window_start >= 1.0:
                current_fps = round(window_count / (now - window_start), 1)
                pose_fps = round(pose_count / (now - window_start), 1)
                window_start, window_count = now, 0
                pose_count = 0
                if current_fps > 0 and not announced_running:
                    announced_running = True
                    readiness.camera_step(
                        device, 100, "analyzing",
                        "%s 解析稼働中(%s fps)" % (_label(device), current_fps),
                    )
                    readiness.note("analyze", "%s の解析が始まりました(%s fps)" % (
                        _label(device), current_fps), "ok")

            status_kwargs = {
                "phase": "running",
                "error": "",
                "status": status,
                "status_label": _status_label(status),
                "state": state_label,
                "danger_percent": round(danger_percent, 2) if danger_percent is not None else None,
                "foot_visible": foot_visible,
                "still_seconds": still_seconds,
                "sequence_frames": sequence_frames,
                # 複数人物検知(ai2.0): 人数と人物ごとの内訳
                "people": people,
                "people_detail": people_detail,
                # ふらつき(表示専用): 最も揺れている人物の値
                "sway_lateral": round(max([t["sway_lateral"] for t in visible_tracks], default=0.0), 2),
                "sway_tilt": round(max([t["sway_tilt"] for t in visible_tracks], default=0.0), 1),
                "fps": current_fps,
                # AI間引きの状況: 無人が続いているカメラは POSE を 1/probe 秒間隔に落とす
                "person_present": person_present,
                "ai_throttled": bool(empty_throttling),
                "pose_fps": pose_fps,
                # 今解析しているフレームが撮像から何秒経っているか(ネットワークのみ)
                # 0.1秒前後が正常。数秒あれば経路でフレームが積み上がっている証拠。
                "frame_age": camera_service.frame_age_seconds(device),
            }
            if alert_info is not None:
                status_kwargs["last_alert"] = alert_info
            status_kwargs["recording_seconds_left"] = (
                max(0.0, round(rec_until - now, 1)) if rec_until is not None else None
            )

            if buf is not None:
                self._store_frame(buf, **status_kwargs)
            else:
                self._set_status(**status_kwargs)

        # 解析停止時に録画が残っていたら、そこまでの分で保存する
        if rec_until is not None and rec_frames:
            _finish_recording(rec_frames, rec_meta)
        # 終了ステータスは _run 側でセットする(映像途絶のエラーを上書きしないため)


# ================= アラート作成 =================
def _camera_for(device):
    """アラートの紐づけ先カメラ(指定が無ければデバイス用の台帳を作成する)

    ネットワークカメラ(`cam:<Camera.id>`)は台帳の既存レコードを使う。
    """
    if camera_service.is_network_key(device):
        camera_id = str(device).split(":", 1)[1]
        camera = Camera.objects.filter(pk=camera_id).first()
        if camera is not None:
            return camera
        return None
    camera_id = getattr(settings, "AI_ALERT_CAMERA_ID", None)
    if camera_id:
        camera = Camera.objects.filter(id=camera_id).first()
        if camera is not None:
            return camera
    name = f"ライブカメラ{device}"
    camera, _ = Camera.objects.get_or_create(
        name=name,
        defaults={
            "location": f"サーバー接続カメラ(index {device})",
            "stream_url": "",
            "is_online": True,
        },
    )
    return camera


def _save_alert_thumbnail(kind, frame):
    """検知した瞬間のフレームをアラート用サムネイルに保存する

    従来は `/static/img/thumb_fall.svg` のプレースホルダだけで、
    異常検知一覧に「何が写ったか」が出ませんでした。
    """
    try:
        import cv2

        folder = Path(settings.MEDIA_ROOT) / "alert_thumbs"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        path = folder / f"alert_{kind}_{stamp}_{int(time.time() * 1000) % 1000}.jpg"
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if not ok:
            return None
        path.write_bytes(buf.tobytes())
        media_url = settings.MEDIA_URL or "/media/"
        return f"{media_url}alert_thumbs/{path.name}"
    except Exception:  # noqa: BLE001 -サムネイル失敗で作成自体は止めない
        return None


def _raise_alert(device, kind, confidence, person_state, ai, frame=None, person_id=None):
    """ai2 の方式でアラートを作成する(クールダウン適用 / 失敗しても解析は止めない)

    person_id(複数人物検知のPerson番号)を渡すと、
    **クールダウンも人物単位**で判定する(2人目が転倒したら即別アラート)。
    単一人物/旧データ相当の None はカメラ単位の従来判定。
    """
    try:
        # 「AI監視」が停止中(設定ページ)はアラートを作らない
        if not ai.enabled:
            return None

        camera = _camera_for(device)
        if camera is None:
            # ネットワークカメラが台帳から消えた等のケース(解析は続けない)
            return None
        now = timezone.now()

        cooldown_scope = Alert.objects.filter(camera=camera)
        if person_id is not None:
            cooldown_scope = cooldown_scope.filter(person_id=person_id)
        last = cooldown_scope.order_by("-occurred_at").first()
        if last is not None and (now - last.occurred_at).total_seconds() < ai.cooldown_seconds:
            return None

        # 検知時の実フレームをサムネイル化(取れないときだけプレースホルダ)
        thumbnail = _save_alert_thumbnail(kind, frame) if frame is not None else None
        if not thumbnail:
            thumbnail = "/static/img/" + (
                "thumb_fall.svg" if kind == Alert.Kind.FALL else "thumb_suspicious.svg"
            )
        alert = Alert.objects.create(
            kind=kind,
            camera=camera,
            occurred_at=now,
            confidence=round(float(confidence), 2),
            detected_state=person_state,
            thumbnail_url=thumbnail,
            person_id=person_id,
        )
        print(f"[RealtimeAI:{device}] 異常検知: {alert.get_kind_display()}"
              f"{f'({alert.person_label})' if person_id else ''} @ {camera.name} "
              f"(信頼度: {alert.confidence})")
        return {
            "id": alert.id,
            "kind": alert.get_kind_display(),
            "confidence": alert.confidence,
            "at": now.isoformat(),
            # 自動録画で使う情報
            "raw_kind": alert.kind,
            "camera_id": camera.id,
            "occurred_at": now,
        }
    except Exception as exc:
        print(f"[RealtimeAI:{device}] アラート作成に失敗: {type(exc).__name__}: {exc}")
        return None
    finally:
        close_old_connections()


# ================= 異常検知からの自動録画 =================
def _finish_recording(frames, meta):
    """検知後 N 秒間のフレーム列を別スレッドで mp4 化し Recording に登録する

    書き出し(数秒)で解析ループを止めないため、必ずスレッドに逃がす。
    """
    if not frames or not meta:
        return
    threading.Thread(
        target=_write_recording,
        args=(list(frames), dict(meta)),
        name="ai-recording-writer",
        daemon=True,
    ).start()


def _write_recording(frames, meta):
    """JPEGフレーム列 → media/recordings/*.mp4 + サムネイル → Recording 作成"""
    try:
        from datetime import timedelta

        import imageio_ffmpeg
        import numpy as np
        from PIL import Image
        import io

        camera = Camera.objects.filter(pk=meta.get("camera_id")).first()
        if camera is None:
            return
        alert = Alert.objects.filter(pk=meta.get("alert_id")).first()

        fps = max(1, int(meta.get("fps") or 10))
        width = int(meta.get("width") or 640)
        height = int(meta.get("height") or 480)
        seconds = round(len(frames) / fps, 1)

        started = meta.get("occurred_at") or timezone.now()
        ended = started + timedelta(seconds=seconds)

        raw_kind = meta.get("raw_kind", Alert.Kind.FALL)
        rec_kind = Recording.Kind.FALL if raw_kind == Alert.Kind.FALL else Recording.Kind.SUSPICIOUS
        label = "転倒" if rec_kind == Recording.Kind.FALL else "あやしい動き"

        save_dir = Path(settings.MEDIA_ROOT) / "recordings"
        save_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.localtime(started).strftime("%Y%m%d-%H%M%S")
        stem = f"ai_{rec_kind}_{stamp}_cam{camera.pk}"
        video_path = save_dir / f"{stem}.mp4"
        thumb_path = save_dir / f"{stem}.jpg"

        gen = imageio_ffmpeg.write_frames(
            str(video_path),
            size=(width, height),
            fps=fps,
            codec="libx264",
            pix_fmt_in="rgb24",
            output_params=["-movflags", "+faststart"],
        )
        gen.send(None)
        first_saved = False
        for jpeg in frames:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
            if image.size != (width, height):
                image = image.resize((width, height))
            if not first_saved:
                image.save(thumb_path, "JPEG", quality=80)
                first_saved = True
            gen.send(np.asarray(image, dtype=np.uint8).tobytes())
        gen.close()

        media_url = settings.MEDIA_URL or "/media/"
        recording = Recording.objects.create(
            kind=rec_kind,
            camera=camera,
            alert=alert,
            started_at=started,
            ended_at=ended,
            video_url=f"{media_url}recordings/{stem}.mp4",
            thumbnail_url=f"{media_url}recordings/{stem}.jpg",
            reason=f"AIが{label}を検知したため検知後{seconds}秒間を録画",
            markers=[{"seconds": 0, "label": f"{label}の瞬間"}],
        )
        # アラートのサムネイルがプレースホルダのままなら、録画の実フレームへ差し替える
        # (一覧・詳細で「何が写ったか」が必ず見えるように)
        if alert is not None and (not alert.thumbnail_url or alert.thumbnail_url.endswith(".svg")):
            alert.thumbnail_url = recording.thumbnail_url
            alert.save(update_fields=["thumbnail_url"])

        print(f"[RealtimeAI] 自動録画を保存: {recording} → {recording.video_url}")
    except Exception as exc:  # noqa: BLE001 -録画失敗で解析は止めない
        print(f"[RealtimeAI] 自動録画の保存に失敗: {type(exc).__name__}: {exc}")
    finally:
        close_old_connections()


# ================= エンジン(全カメラの管理者) =================
class RealtimeAIEngine:
    """カメラごとの解析ランタイムを管理する(アプリ内シングルトン)"""

    def __init__(self):
        self._lock = threading.Lock()      # ランタイム辞書の保護
        self._runtimes = {}               # device -> _CameraRuntime
        self._monitor_thread = None       # 映像欠落カメラの見張り

    # ---------------- 公開API ----------------
    @staticmethod
    def enabled():
        return bool(getattr(settings, "AI_REALTIME_ENABLED", True))

    def _runtime(self, device):
        """対象デバイスのランタイム(無ければ作成)"""
        with self._lock:
            runtime = self._runtimes.get(device)
            if runtime is None:
                runtime = _CameraRuntime(device)
                self._runtimes[device] = runtime
            return runtime

    def attach(self, device):
        """そのカメラの解析を開始/参加し、セッションIDを返す(中止時は False)"""
        if not self.enabled():
            return False
        return self._runtime(device).attach()

    def detach(self, device, session=None):
        """そのカメラの視聴者が離れたとき(常時稼働でなければ最後の1人で停止)"""
        with self._lock:
            runtime = self._runtimes.get(device)
        if runtime is not None:
            runtime.detach(session)

    def latest_jpeg(self, device):
        """解析済みフレームのJPEG(そのカメラに無ければNone)"""
        with self._lock:
            runtime = self._runtimes.get(device)
        if runtime is None:
            return None
        with runtime.lock:
            return runtime.jpeg

    def frame_seq(self, device):
        """解析済みフレームが更新された回数(新しいフレームかどうかの判定に使う)"""
        with self._lock:
            runtime = self._runtimes.get(device)
        if runtime is None:
            return 0
        with runtime.lock:
            return runtime.frame_seq

    def is_analyzing(self, device):
        """そのカメラを今AIが読み取り中か

        配信中にブラウザ側が `camera_service.read_jpeg` を呼ぶと、
        AIと同じキャプチャからフレームを取り合ってしまう(1fpsのカメラでは
        どちらかが必ずNoneになる)。そのため配信側が読みに行ってよいか判断する。
        """
        with self._lock:
            runtime = self._runtimes.get(device)
        if runtime is None:
            return False
        with runtime.lock:
            return bool(runtime.thread is not None and runtime.thread.is_alive())

    def status(self, device=None):
        """画面へ返す状態。device指定はそこのカメラ、無しは最初のカメラ + 全カメラ要約"""
        summary = self.summary()
        if device is None:
            # 未選択ときは「一番actively解析しているカメラ」の状態を出す
            active = [row for row in summary if row["phase"] == "running"]
            pick = max(active, key=lambda row: row["fps"] or 0) if active else (summary[0] if summary else None)
            device = pick["device"] if pick else None
        else:
            # 数値ならUSB index、そうでなければ IPキー("cam:12")として扱う
            try:
                device = int(device)
            except (TypeError, ValueError):
                device = str(device)

        with self._lock:
            runtime = self._runtimes.get(device) if device is not None else None

        if runtime is not None:
            snapshot = runtime.snapshot()
        else:
            snapshot = _CameraRuntime._fresh_status()
            snapshot.update(device=device, running=False, persistent=False, viewers=0)
            try:
                ai = AISettings.load()
                snapshot["stillness_threshold"] = ai.stillness_seconds
                snapshot["fall_threshold"] = round(float(ai.fall_threshold), 2)
            except Exception:
                snapshot["stillness_threshold"] = None
                snapshot["fall_threshold"] = None

        snapshot["enabled"] = self.enabled()
        snapshot["devices"] = summary
        return snapshot

    def summary(self):
        """全カメラの稼働状態一覧(デバッグページのまとめ表示用)"""
        with self._lock:
            runtimes = list(self._runtimes.items())
        rows = []
        target_fps = max(1, int(getattr(settings, "AI_ANALYSIS_FPS", 10)))
        # キーは USBのint と IPのstr が混在するので文字列表記で揃えて並べる
        for device, runtime in sorted(runtimes, key=lambda item: str(item[0])):
            with runtime.lock:
                row = {
                    "device": device,
                    "phase": runtime.status.get("phase"),
                    "state": runtime.status.get("state"),
                    "status": runtime.status.get("status"),
                    "status_label": runtime.status.get("status_label"),
                    "fps": runtime.status.get("fps"),
                    "frame_age": runtime.status.get("frame_age"),
                    "still_seconds": runtime.status.get("still_seconds"),
                    # AI間引き: 無人カメラは POSE を間引いている(pose_fps が 1/probe 秒前後になる)
                    "person_present": runtime.status.get("person_present", False),
                    "ai_throttled": runtime.status.get("ai_throttled", False),
                    "pose_fps": runtime.status.get("pose_fps"),
                    # 複数人物検知(ai2.0)
                    "people": runtime.status.get("people", 0),
                    "sway_lateral": runtime.status.get("sway_lateral", 0.0),
                    "sway_tilt": runtime.status.get("sway_tilt", 0.0),
                    "persistent": runtime.persistent,
                    # 常時稼働ぶんの自己視聴は除いて表示する
                    "viewers": max(0, runtime.viewers - (1 if runtime.persistent else 0)),
                    "running": bool(runtime.thread is not None and runtime.thread.is_alive()),
                    "recording_seconds_left": runtime.status.get("recording_seconds_left"),
                    "error": runtime.status.get("error"),
                }
            # 目標fpsの7割未満なら「低速」(USB帯域競合などの合図)
            row["low_fps"] = bool(
                row["phase"] == "running" and (row["fps"] or 0) < target_fps * 0.7
            )
            row["target_fps"] = target_fps
            # 存在しないindex(開けたことがなく待機中のだけ)は一覧に出さない
            if not (row["persistent"] or row["running"] or row["viewers"] or row["phase"] == "error"):
                continue
            rows.append(row)
        return rows

    def devices(self):
        """解析対象にしているキー一覧(USB index と IPキーが混在するので文字列順)"""
        with self._lock:
            return sorted(self._runtimes.keys(), key=str)

    # ---------------- 常時稼働 ----------------
    @staticmethod
    def _camera_source_mode():
        """AISettings.camera_source(自動/USBのみ/ネットワークのみ)。DBエラー時はauto"""
        try:
            from .models import AISettings

            return str(AISettings.load().camera_source or "auto")
        except Exception:  # noqa: BLE001 -ツマミ読めなくても検知は止めない
            return "auto"

    @staticmethod
    def target_devices():
        """常時稼働の対象カメラ一覧と、**開く順序**

        - `AISettings.camera_source`(管理画面のカメラ源ツマミ)で源を絞る:
            net = Pi等のネットワークカメラのみ(USB検知自体走らせない=AWS本番)
            usb = USBのみ(stream_url 無視=開発機)
            auto = 両方(既定)
        - `AI_ALWAYS_DEVICES` を指定していればそれを対象にする(運用で絞れる)
        - 無ければ **検知できたカメラ全て**(検知中/未接続なら探索範囲を素で試す)
        - 開く順序は `AI_CAMERA_PRIORITY`(list)が最優先。
          既定は index の降順で、実測で「先に開いたカメラが映像を独占し、
          後から開いたカメラは 0〜1fps または開けない」ため、
          **USB帯域が足りないPCではこの順がそのまま優先度になる**。
          内蔵カメラを止めたい/動かしたい運用では AI_CAMERA_PRIORITY で明示する。
        """
        explicit = getattr(settings, "AI_ALWAYS_DEVICES", None)
        priority = getattr(settings, "AI_CAMERA_PRIORITY", None)
        mode = ai_engine._camera_source_mode()

        # 1) ネットワークカメラ(IP): USB总线を介さないので同時運用の制約を受けない
        ip_keys = []
        if mode in ("auto", "net"):
            ip_keys = [source["key"] for source in camera_service.network_sources()]

        # 2) ローカル(USB)カメラ
        if mode == "net":
            return ip_keys          # AWS本番: 総当たりprobe自体を走らせない
        if explicit is not None:
            candidates = sorted(int(index) for index in explicit)
        else:
            result = camera_service.detect_devices()
            found = [
                int(device["index"])
                for device in result.get("devices", [])
                if device.get("available", True) or device.get("busy")
            ]
            if not found:
                # 検知中(pending)や結果なしのときに 0..N の総当たりへ転けない。
                # 過去に見つけた index だけ準備する(2026-11-18: ログの
                # 「index 7: 開けませんでした」連発はここが毎回総当たりだったため)
                found = [int(index) for index in camera_service.known_indices()]
            if not found:
                # まだ何も見つかっていない(起動直後の初回サイクルだけ):
                # **エンジンスレッドで 0..N を直接開きには行かない**。検知スレッドを
                # 走らせて次の30秒サイクルに譲る。ここで総当たりすると検知プローブと
                # 同一indexを二重openし、両方がハングして永久にblindになる(2026-11-24実測)。
                camera_service.detect_devices()
                return ip_keys
            # 開く順序は **index 昇順**(2026-11-24 変更)。
        # 実測: このPCはUSBカメラ2台同時ストリームで「後発が飢える」ため
        # (第44章)、降順だと内蔵(index0)が常に後発になって開けない/0fpsの
        # ping-pongが起きた。昇順=内蔵が先、外付け(C270等)が後。
        # 帯域が足りる環境では順序の実利はないので、安全側で昇順に固定する。
        candidates = sorted(found)

        if priority:
            ordered = [int(index) for index in priority]
            candidates = ordered + [index for index in candidates if index not in ordered]

        # IP を先、USB を後(USB側は先に開いた方が帯域を取る)
        return ip_keys + candidates

    def _enforce_camera_source(self):
        """カメラ源ツマミの対象外ランタイムを常時稼働から外す(停止+解放)

        stop_persistent は self._lock を内部で取るため、ここでは lock を跨がない。
        """
        mode = self._camera_source_mode()
        if mode == "auto":
            return
        with self._lock:
            devices = list(self._runtimes.keys())
        for device in devices:
            is_net = isinstance(device, str) and device.startswith(camera_service.NETWORK_PREFIX)
            out_of_scope = (mode == "net" and not is_net) or (mode == "usb" and is_net)
            if not out_of_scope:
                continue
            with self._lock:
                runtime = self._runtimes.get(device)
                persistent = runtime is not None and runtime.persistent
            if persistent:
                print("[RealtimeAI] カメラ源=%s のため %s の常時稼働を停止します" % (mode, device))
            self.stop_persistent(device)   # 対象外は常に確実に外す(冪等)

    def start_persistent(self, device=None, verbose=True):
        """バックグラウンド解析を開始して止めない状態にする(画面を離れても動く)

        device=None → 対象カメラ **全て** を起動する(本番想定の挙動)。
        AI_REALTIME_ENABLED / AI_ALWAYS_ON が False なら何もしない。
        verbose=False にすると失敗時の出力を抑える(定期再試行ループ用)。
        """
        if not self.enabled():
            return False
        if not bool(getattr(settings, "AI_ALWAYS_ON", True)):
            return False

        if device is None:
            # 源ツマミのライブ反映: net/usb に切られた側の既存常時稼働を止める
            # (30秒サイクルごとにここを通るので、管理画面変更だけで環境が切り替わる)
            self._enforce_camera_source()
            targets = self.target_devices()
        else:
            # USB index(int)と IPキー(str)の両方を受け取る(int変換はしない)
            targets = [device]
        results = {}

        # 複数カメラは **1台ずつ順に開く**こと。実測で、同時に開こうとすると
        # 後発のカメラが1フレームも返さなくなる(DSHOWのフォーマット交渉が直列なため)。
        # 降順(target_devices でソート済みにして)先に開いたカメラが帯域を取る。
        for index in targets:
            results[index] = self._start_one(index, verbose=verbose)

        started = sorted((index for index, ok in results.items() if ok), key=str)
        failed = sorted((index for index, ok in results.items() if not ok), key=str)
        if started and verbose:
            print(f"[RealtimeAI] 常時稼働を開始しました: index {started} / 画面を離れても解析を続けます")
        if failed and verbose:
            print(f"[RealtimeAI] 開けなかったカメラ: index {failed}"
                  " (未接続または他アプリ占有中。次回スキャンで再試行します)")
        return bool(started)

    def _start_one(self, device, verbose=True):
        """1台分の常時稼働を開始(カメラを開けなければ False)"""
        runtime = self._runtime(device)
        with runtime.lock:
            if runtime.defer_until > time.monotonic():
                return False    # 映像が取れなかったカメラは抑止時間あけまで再試行しない
            if runtime.persistent and runtime.thread is not None and runtime.thread.is_alive():
                return True
        # カメラキャプチャをエンジン側で常時確保する
        # (ブラウザの「停止」で camera_service.release が呼ばれても参照カウントで残る)
        if not isinstance(device, str) and camera_service.probe_running(device):
            # 検知プローブ(ハング含む)が終わるまで見送る。次30秒サイクルで再試行
            if verbose:
                print(f"[RealtimeAI:{device}] 検知プローブ中のため確保を次サイクルで見送ります")
            return False
        began = time.monotonic()
        readiness.camera_step(device, 10, "opening", "%s を開いています…" % _label(device))
        if not camera_service.acquire(device):
            readiness.camera_failed(device, "カメラを開けませんでした(未接続の可能性)")
            if verbose:
                print(f"[RealtimeAI:{device}] カメラを開けません(未接続の可能性)")
            return False
        readiness.camera_step(
            device, 40, "opened",
            "カメラを確保しました(%.1f秒)" % (time.monotonic() - began),
            seconds=round(time.monotonic() - began, 1),
        )
        with runtime.lock:
            runtime.persistent = True
            runtime.last_frame_at = time.monotonic()
        runtime.attach()
        self._ensure_monitor()
        return True

    def _ensure_monitor(self):
        """映像欠落の見張りスレッドを1本だけ立てる"""
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, name="realtime-ai-watchdog", daemon=True
            )
            self._monitor_thread.start()

    def _monitor_loop(self):
        """映像を1コマも渡せないカメラを止めて、帯域を他のカメラへ返す

        実測でこのマシンは2台同時開放すると後から開いた側が 0〜1fps になり
        (USB帯域/コントローラの制約。解像度を変えても変わらない)、
        `cap.read()` が長時間ブロックすると解析ループ内のタイムアウトが
        発火しないので、別スレッドから監視する。
        """
        limit = float(getattr(settings, "AI_FRAME_LOST_SECONDS", 20))
        backoff = float(getattr(settings, "AI_STARVED_RETRY_SECONDS", 300))
        while True:
            time.sleep(5)
            now = time.monotonic()
            with self._lock:
                runtimes = list(self._runtimes.items())
            for device, runtime in runtimes:
                with runtime.lock:
                    alive = runtime.thread is not None and runtime.thread.is_alive()
                    persistent = runtime.persistent
                    since = now - runtime.last_frame_at
                    deferred = runtime.defer_until > now
                if not alive or not persistent or deferred or since <= limit:
                    continue
                print(
                    f"[RealtimeAI:{device}] 映像が{int(since)}秒来ないため解析を停止します"
                    f"(他のカメラに帯域を譲ります / {int(backoff)}秒後に再試行)"
                )
                with runtime.lock:
                    runtime.defer_until = now + backoff
                    runtime.persistent = False
                runtime.stop_analysis()
                camera_service.release(device)
                runtime._set_status(
                    phase="error",
                    error=(
                        "映像が1フレームも取れないため解析を停止しました。"
                        "このPCはUSB帯域/コントローラの制約で複数カメラ同時ストリームができず、"
                        "先に開いたカメラが映像を独占します"
                        "(settings.AI_CAMERA_PRIORITY で優先するカメラを指定してください)"
                    ),
                )

    def stop_persistent(self, device=None):
        """常時稼働を止める(指定が無ければ全カメラ)。テスト・終了処理用"""
        with self._lock:
            targets = [device] if device is not None else sorted(self._runtimes.keys(), key=str)
        for index in targets:
            with self._lock:
                runtime = self._runtimes.get(index)
            if runtime is None:
                continue
            with runtime.lock:
                watching = runtime.viewers > (1 if runtime.persistent else 0)
                runtime.persistent = False
            runtime.stop_analysis()
            if not watching:
                camera_service.release(index)


ai_engine = RealtimeAIEngine()
