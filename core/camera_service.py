"""CARE SEC 転倒・異常検知システム / 実カメラキャプチャサービス(デバッグページ用)

サーバー起動マシンに接続されたカメラをOpenCVで開き、
ブラウザへMJPEGストリームとして配信するためのテスト機能。
デモ用データ(core.models.Camera 等)とは完全に独立しており、
今後ここに新しいAI認識モデルを接続していく予定。

OpenCVが未導入でもアプリ自体は起動できるよう、cv2のimportは遅延させる。
"""
import glob
import platform
import subprocess
import threading
import time
from pathlib import Path

from django.conf import settings

_cv2 = None
_cv2_error = ""

# 検知結果のキャッシュ / 配信中ストリーム(index単位)
_detect_cache = {"at": 0.0, "devices": None}
_detect_lock = threading.Lock()
_detect_thread = None      # 検知はバックグラウンドスレッドで実行(HTTPをブロックしない)
_detect_partial = []       # 検知中に見つかったデバイス(部分結果)
_streams = {}
_streams_lock = threading.Lock()

# デバイス検知の進捗(ブラウザの進行バー表示用)
# state: idle / running / done / error
_detect_progress = {
    "state": "idle",
    "stage": "",
    "current": 0,
    "total": 0,
    "started_at": None,
    "finished_at": None,
}
_detect_progress_lock = threading.Lock()


def _load_cv2():
    """cv2を一度だけimportする(未導入時はエラー文字列を返す)"""
    global _cv2, _cv2_error
    if _cv2 is None and not _cv2_error:
        try:
            import cv2  # OpenCVをオプション依存にするため遅延import
            _cv2 = cv2
        except Exception as exc:  # 環境依存(opencv-python-headless 未導入など)
            _cv2_error = f"{type(exc).__name__}: {exc}"
    return _cv2, _cv2_error


def _max_devices():
    return int(getattr(settings, "DEBUG_CAMERA_MAX_DEVICES", 4))


def _set_progress(**kwargs):
    """進捗情報を更新する(detect_devices の実行中だけ呼ばれる)"""
    with _detect_progress_lock:
        _detect_progress.update(kwargs)


def detect_progress():
    """デバイス検知の進捗を返す(ブラウザの進行バー用)

    検知中でも既に見つかったデバイス(`devices`)を返すので、
    1台目の確認に時間がかかっていても2台目以降を先に選べる。
    """
    with _detect_progress_lock:
        data = dict(_detect_progress)
    if data["state"] == "running" and data["started_at"] is not None:
        data["elapsed"] = round(time.monotonic() - data["started_at"], 1)
    else:
        data["elapsed"] = None
    data["devices"] = list(_detect_partial)
    return data


def _cache_seconds():
    return float(getattr(settings, "DEBUG_CAMERA_CACHE_SECONDS", 30))


def _friendly_names_windows(count):
    """Windows: PnPのカメラ一覧(FriendlyName)を列挙順で返す(ベストエフォート)"""
    return _os_camera_names()[:count]


def _os_camera_names():
    """OSが認識しているカメラ名を列挙順に返す

    OpenCV はカメラを開けなければ存在を検知できないが、他カメラの使用中は
    開こうとしてもハングする(実測 約60秒)ため、OS列挙側でも名前を取って
    「存在するが映像を確認できない」カメラを一覧に出す。
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -Class Camera, Image -PresentOnly -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty FriendlyName"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _friendly_names_linux():
    """Linux: /sys/class/video4linux/videoN/name からインデックス対応の名前を返す"""
    names = {}
    for path in sorted(glob.glob("/sys/class/video4linux/video*")):
        suffix = path.rsplit("video", 1)[-1]
        if not suffix.isdigit():
            continue
        try:
            with open(f"{path}/name", encoding="utf-8", errors="replace") as fh:
                names[int(suffix)] = fh.read().strip()
        except OSError:
            continue
    return names


def _capture_info(cap, index, backend_name=None):
    """開いたキャプチャから解像度等の情報を取り出す"""
    height, width = cap.get(3), cap.get(4)  # CAP_PROP_FRAME_WIDTH / HEIGHT
    fps = cap.get(5)  # CAP_PROP_FPS(取得できない backend では負値になる)
    return {
        "index": index,
        "name": f"カメラ {index}",
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "fps": round(float(fps), 1) if fps and fps > 0 else None,
        "backend": backend_name or cap.getBackendName(),
    }


def _open_params(cv2):
    """開き/読みのタイムアウト(ms)

    OpenCV の既定値は非常に長く、使えないインデックスで数十秒ハングする
    (実測で MSMF index 1 が62秒)。打ち切って検知を続けられるようにする。
    ※ MSMF はこの指定を無視することがあるため、バックエンド順(DSHOW優先)で対策する。
    """
    if platform.system() != "Windows":
        return []
    open_ms = int(getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", -1))
    read_ms = int(getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", -1))
    if open_ms < 0 or read_ms < 0:
        return []
    return [open_ms, 2500, read_ms, 1500]


def _backends(cv2):
    """試すバックエンド(Windowsは DSHOW / MSMF の順)

    インデックスの割当順はバックエンドごとに異なるため両方必要だが、
    実測で **DSHOW の方が圧倒的に速い**(同じカメラが MSMF では最初の1フレームに
    約54秒、DSHOW では0.4秒)。タイムアウト指定も MSMF に効かないため、
    速い DSHOW を先に試し、DSHOW が見えないカメラだけ MSMF で拾う。
    """
    if platform.system() == "Windows":
        return [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF)]
    return [("既定", cv2.CAP_ANY)]


def _open_capture(cv2, index, first_frame_seconds=None):
    """インデックスを順に開き、フレームを読めた (cap, backend名, frame) を返す

    どれでも読めなければ (None, None, None)。
    「開けるが映像が無い」場合は (None, None, "blocked") を返す。

    first_frame_seconds: 初フレームを待つ上限秒数。
      他カメラと同時運用になると帯域の都合で初フレームが極端に遅くなる
      (実測: 2台目は約1秒間隔でしかフレームが来ない)ため、
      配信用の acquire では長めに待つ必要がある。
    """
    params = _open_params(cv2)
    if first_frame_seconds is None:
        first_frame_seconds = float(getattr(settings, "AI_CAMERA_FIRST_FRAME_SECONDS", 3.0))
    blocked = False
    for backend_name, backend in _backends(cv2):
        try:
            cap = cv2.VideoCapture(index, backend, params) if params else cv2.VideoCapture(index, backend)
        except Exception:  # noqa: BLE001 -パラメータ非対応の環境では素で開く
            cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        deadline = time.monotonic() + first_frame_seconds
        frame = None
        while True:
            ok, frame = cap.read()
            if ok and frame is not None:
                return cap, backend_name, frame
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        cap.release()
        blocked = True  # 開けたが映像が無い
    return None, None, "blocked" if blocked else None


def _probe(index, cv2):
    """インデックスを開いて1フレーム読めるバックエンドを探す

    開けるが映像が無いカメラも `available=False` で返す(一覧には出す)。
    何も開けられなければ None。
    """
    cap, backend_name, frame = _open_capture(cv2, index)
    try:
        if cap is not None:
            info = _capture_info(cap, index, backend_name)
            info["snapshot_url"] = save_snapshot(index, frame)
            return _name_device(info)
    finally:
        if cap is not None:
            cap.release()
    if frame == "blocked":
        return _name_device({"index": index, "available": False})
    return None


def save_snapshot(index, frame):
    """そのカメラに写っている実フレームを保存し、一覧で「どれがどのカメラか」確認できるようにする

    OpenCV のインデックス割当はOSの列挙順と一致しないことがあるため、
    名前だけだと取り違えに気づけない(実測で index 0 と 1 の名前が入れ替わった)。
    検知時(`_probe`)と解析開始時(`ai_engine`)の両方から呼ぶ。
    """
    try:
        from django.conf import settings as _settings

        folder = Path(_settings.MEDIA_ROOT) / "camera_snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / snapshot_name(index)
        cv2, _ = _load_cv2()
        if cv2 is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if not ok:
            return None
        path.write_bytes(buf.tobytes())
        media_url = _settings.MEDIA_URL or "/media/"
        return f"{media_url}camera_snapshots/{path.name}?t={int(path.stat().st_mtime)}"
    except Exception:  # noqa: BLE001 -サムネイル失敗で検知を止めない
        return None


def _name_device(info):
    """デバイス表示名を決める

    1. `DEBUG_CAMERA_NAME_MAP`(index→名前)を最優先(実機で確認済みの名前を指定できる)
    2. カメラが1台だけのときはOS列挙名を使ってよい(対応付けが一意的)
    3. 複数台のときは推測しない(OS列挙順とOpenCV index順は一致しないことがある)
    """
    # ネットワークカメラは台帳の名前が正(推測不要)
    if is_network_key(info.get("key", info.get("index"))):
        info["name"] = info.get("name") or f"IPカメラ {info.get('key')}"
        info["name_source"] = "camera_table"
        return info

    name_map = getattr(settings, "DEBUG_CAMERA_NAME_MAP", None) or {}
    index = info["index"]
    if str(index) in name_map or index in name_map:
        info["name"] = (name_map.get(index) or name_map.get(str(index)))
        info["name_source"] = "map"
        return info
    if info.get("name") and info.get("name_source") == "os":
        return info
    info.setdefault("name", f"カメラ {index}")
    info["name_source"] = "index"
    return info


# プローブの管理(ハングスレッドの蓄積を防ぐ)
_probe_state = {}     # index -> {"at": monotonic, "kind": "fail"/"timeout"}
_probe_threads = {}   # index -> 実行中のプローブスレッド
_probe_lock = threading.Lock()


def _probe_retry_seconds():
    return float(getattr(settings, "DEBUG_CAMERA_PROBE_RETRY_SECONDS", 120))


def reset_probe_cache():
    """「再検知」で再試行抑止を解く(生存中のプローブスレッドは解けないので待つ)"""
    with _probe_lock:
        _probe_state.clear()


def _remember_probe(index, kind):
    with _probe_lock:
        _probe_state[index] = {"at": time.monotonic(), "kind": kind}


def _forget_probe(index):
    with _probe_lock:
        _probe_state.pop(index, None)


def _probe_with_budget(index, cv2, budget=6.0):
    """`_probe` を時間予算つきで実行する

    他カメラが使用中だと Windows では開くこと自体が約60秒ハングする。
    検知を止めないため予算を過ぎたら待つのをやめるが、**裏のスレッドはそのまま
    生き続ける**。毎回プローブを積むとハングしたスレッドが溜まり、Windowsの
    カメラスタックが直列化されて、**すでに配信できているカメラまで映像が止まる**
    (実測: 常時稼働+30秒毎の再検知で内蔵カメラのアクセスランプが消灯した)。

    そこで
      - 同じ index のプローブが走っている間は起動しない -> "in_progress"
      - 直近で開けなかった index は TTL まで再試行しない  -> "cached_fail"
    とする。戻り値は 情報dict / None / "timeout" / "in_progress" / "cached_fail"。
    """
    with _probe_lock:
        running = _probe_threads.get(index)
        if running is not None and running.is_alive():
            return "in_progress"
        state = _probe_state.get(index)
        if state is not None and time.monotonic() - state["at"] < _probe_retry_seconds():
            return "cached_fail"

    box = {}

    def work():
        try:
            box["value"] = _probe(index, cv2)
        except Exception as exc:  # noqa: BLE001 -検知は続けたい
            box["value"] = None
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with _probe_lock:
                if _probe_threads.get(index) is threading.current_thread():
                    _probe_threads.pop(index, None)

    thread = threading.Thread(target=work, name=f"camera-probe-{index}", daemon=True)
    with _probe_lock:
        # チェックから起動までの窓で二重起動しないようここで確保する
        running = _probe_threads.get(index)
        if running is not None and running.is_alive():
            return "in_progress"
        _probe_threads[index] = thread
    thread.start()
    thread.join(budget)

    if thread.is_alive():
        _remember_probe(index, "timeout")
        return "timeout"
    value = box.get("value")
    if not value or value.get("available") is False:
        _remember_probe(index, "fail")
    else:
        _forget_probe(index)
    return value


def detect_devices(force=False):
    """カメラデバイス検知を開始し、結果を待たずに返す(非同期)

    - 見つかったデバイスはキャッシュされる
    - 検知中は `{"ok": True, "pending": True, "devices": 部分結果}` を返す
    - 進捗と部分結果は `detect_progress()` からも取れる

    MSMF は使えないインデックスで約60秒ハングすることが実測されたため
    (CAP_PROP_OPEN/READ_TIMEOUT_MSEC も効かない)、HTTPリクエストをブロックせず、
    見つかった分だけ先に一覧へ出せるようにする。
    """
    global _detect_thread, _detect_partial

    if force:
        # 「再検知」は再試行抑止も解く(新しいカメラをすぐ見つけられるようにする)
        reset_probe_cache()

    cv2, error = _load_cv2()
    if cv2 is None:
        _set_progress(
            state="error",
            stage="OpenCVを利用できません",
            current=0,
            total=0,
            finished_at=time.monotonic(),
        )
        return {
            "ok": False,
            "error": f"OpenCV(opencv-python-headless)を利用できません: {error}",
            "devices": [],
        }

    with _detect_lock:
        now = time.monotonic()
        cached = _detect_cache["devices"]
        if not force and cached is not None and now - _detect_cache["at"] < _cache_seconds():
            _set_progress(state="done", stage="キャッシュ済み", finished_at=now)
            return {"ok": True, "cached": True, "devices": cached}

        if _detect_thread is not None and _detect_thread.is_alive():
            # すでに検知中 → 進捗だけ返す
            return {"ok": True, "pending": True, "devices": list(_detect_partial)}

        _detect_partial = []
        _detect_thread = threading.Thread(
            target=_run_detection,
            args=(cv2,),
            name="camera-detect",
            daemon=True,
        )
        _detect_thread.start()
        return {"ok": True, "pending": True, "devices": []}


def _run_detection(cv2):
    """検知本体(バックグラウンドスレッド)"""
    global _detect_partial

    max_devices = _max_devices()
    started = time.monotonic()
    total = max_devices + 1  # +1 = OS列挙(デバイス名)ステップ
    _set_progress(
        state="running",
        stage="カメラを確認しています…",
        current=0,
        total=total,
        started_at=started,
        finished_at=None,
    )

    # OSが認識しているカメラ名を先に取る(開けなくても「存在する」ことを示すため)
    if platform.system() == "Windows":
        os_names = _os_camera_names()
    else:
        linux_names = _friendly_names_linux()
        os_names = [linux_names.get(i, "") for i in range(max_devices)]

    devices = []
    for index in range(max_devices):
        _set_progress(
            stage=f"カメラ index {index} を確認中…",
            current=index,
        )
        with _streams_lock:
            active = _streams.get(index)
        if active:
            # 配信中のデバイスは開き直せず検知できないため、配信中として報告する
            info = _name_device(dict(active["info"], in_use=True))
            # 配信中(=AIが掴んでいる)カメラは開き直せないため、
            # 過去の検知で保存したサムネイルがあればそれを使う
            thumb = Path(settings.MEDIA_ROOT) / "camera_snapshots" / snapshot_name(index)
            if thumb.exists():
                media_url = settings.MEDIA_URL or "/media/"
                info["snapshot_url"] = f"{media_url}camera_snapshots/{thumb.name}?t={int(thumb.stat().st_mtime)}"
            devices.append(info)
            _detect_partial = list(devices)
            continue

        info = _probe_with_budget(index, cv2)
        if info in ("timeout", "in_progress"):
            # 開こうとしてハング(他カメラ使用中に多い)。OS列挙にあれば一覧には出す
            if index < len(os_names):
                devices.append({
                    "index": index,
                    "name": os_names[index] or f"カメラ {index}",
                    "available": False,
                    "busy": True,
                })
        elif info == "cached_fail":
            # 直近で開けなかった index は TTL まで再試行しない(ハングスレッドの蓄積防止)
            if index < len(os_names):
                devices.append({
                    "index": index,
                    "name": os_names[index] or f"カメラ {index}",
                    "available": False,
                })
        elif info:
            devices.append(info)
        elif index < len(os_names):
            # OpenCV では開けないが OS には存在するカメラ
            devices.append({
                "index": index,
                "name": os_names[index] or f"カメラ {index}",
                "available": False,
            })
        _set_progress(current=index + 1)
        # 見つかった分は随時公開(ハングしていても既に分かったカメラは表示できる)
        _detect_partial = list(devices)

    # デバイス名の付与
    #  OS列挙順とOpenCVインデックス順は一致しないことがある(実測で2台が入れ替わった)ため、
    #  **1台のときだけ** OS名を当てはめ、複数台は DEBUG_CAMERA_NAME_MAP か「カメラ N」にする。
    _set_progress(stage="デバイス名を取得中…", current=total - 1)
    if platform.system() == "Windows":
        if len(devices) == 1 and os_names:
            only = devices[0]
            if only["index"] < len(os_names) and os_names[only["index"]]:
                only["name"] = os_names[only["index"]]
                only["name_source"] = "os"
    else:
        linux_names = _friendly_names_linux()
        for device in devices:
            if device["index"] in linux_names:
                device["name"] = linux_names[device["index"]]
                device["name_source"] = "os"

    for device in devices:
        _name_device(device)

    with _detect_lock:
        _detect_cache["at"] = time.monotonic()
        _detect_cache["devices"] = devices
    _detect_partial = list(devices)
    _set_progress(state="done", stage="完了", current=total, finished_at=time.monotonic())


# ================= ネットワークカメラ(IPカメラ) =================
# USBカメラは「同時に1台だけ」等の硬件制約があるが、IPカメラ(RTSP / HTTP-MJPEG)は
# USBバスを介さないため台数制約から解放される。台帳の Camera.stream_url をソースに使う。

NETWORK_PREFIX = "cam:"


def stream_keys():
    """現在キャプチャを掴んでいるソースのキー一覧(USB index と IPキー混在可)"""
    with _streams_lock:
        return list(_streams.keys())


def is_network_key(key):
    """運用キーがネットワークカメラを指しているか"""
    return isinstance(key, str) and key.startswith(NETWORK_PREFIX)


def source_key(camera_id):
    """台帳の Camera.id → 運用キー"""
    return f"{NETWORK_PREFIX}{camera_id}"


def snapshot_name(key):
    """一覧用サムネイルのファイル名(USB: cam3.jpg / IP: ip12.jpg)"""
    if is_network_key(key):
        return f"ip{key[len(NETWORK_PREFIX):]}.jpg"
    return f"cam{key}.jpg"


def network_sources():
    """台帳のうち stream_url を持つカメラをネットワークソース一覧として返す"""
    if not bool(getattr(settings, "AI_IP_CAMERA_ENABLED", True)):
        return []

    from .models import Camera

    qs = Camera.objects.exclude(stream_url="").exclude(stream_url=None)
    if bool(getattr(settings, "AI_IP_CAMERA_ONLINE_ONLY", True)):
        qs = qs.filter(is_online=True)
    wanted = getattr(settings, "AI_IP_CAMERA_IDS", None)
    if wanted is not None:
        qs = qs.filter(id__in=[int(item) for item in wanted])

    # 録画ファイル(mp4等)はライブ配信ではないので対象外。
    # rtsp:// や http(s):// のライブストリーム(mjpeg等)だけを扱う。
    # 明示的に AI_IP_CAMERA_IDS で指定されたIDはこの制限を無視する。
    video_ext = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
    wanted_ids = {int(item) for item in (wanted or [])} if wanted is not None else None

    sources = []
    for camera in qs.order_by("id"):
        url = (camera.stream_url or "").strip().lower()
        if wanted_ids is None and url.endswith(video_ext):
            continue
        sources.append({
            "key": source_key(camera.id),
            "camera_id": camera.id,
            "name": camera.name,
            "url": camera.stream_url,
            "location": camera.location,
            "kind": "ip",
        })
    return sources


def _network_url(key):
    for source in network_sources():
        if source["key"] == key:
            return source["url"]
    return None


def _network_info(key, url, frame, cap):
    height, width = (frame.shape[0], frame.shape[1]) if frame is not None else (
        int(cap.get(4) or 0), int(cap.get(3) or 0))
    return {
        "key": key,
        "index": key,
        "name": None,   # 台帳の名前を使う(_name_device で埋める)
        "kind": "ip",
        "url": url,
        "width": width or None,
        "height": height or None,
        "fps": None,
        "backend": "IP",
    }


def _acquire_network(cv2, key):
    """ネットワークカメラを開く(視聴者数を管理)

    開いたら **専用の読取スレッド**を走らせる。以降 `cap` はそのスレッドだけが
    触り、他は常に「最後に読めた1枚」を受け取る(README/第21章参照)。
    """
    url = _network_url(key)
    if not url:
        return False
    with _streams_lock:
        stream = _streams.get(key)
        if stream is not None:
            stream["viewers"] += 1
            return True

    # 開く処理はロック外(ネットワーク応答待ちがあっても他要求を巻き込まない)
    cap = cv2.VideoCapture(url)
    if not cap.isOpened():
        cap.release()
        return False
    deadline = time.monotonic() + float(getattr(settings, "AI_NETWORK_OPEN_SECONDS", 12.0))
    frame = None
    while time.monotonic() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None:
            break
        time.sleep(0.1)
    if frame is None:
        cap.release()
        return False

    with _streams_lock:
        existing = _streams.get(key)
        if existing is not None:
            existing["viewers"] += 1
            cap.release()
            return True
        stream = {
            "cap": cap,
            "lock": threading.Lock(),
            "viewers": 1,
            "holds": 0,
            "network": True,
            "url": url,
            "last_reconnect": 0.0,
            "info": _network_info(key, url, frame, cap),
            # ---- 最新1枚だけを残す枠(読取スレッドが更新する) ----
            "frame": frame,
            "seq": 1,
            "ts": time.monotonic(),
            "served": 0,
            "frame_lock": threading.Lock(),
            "stop": threading.Event(),
            "reader": None,
        }
        _streams[key] = stream
    _start_reader(cv2, key, stream)
    return True


# ================= ネットワークカメラの専有読取スレッド =================
#
# HTTP MJPEG / RTSP は「バイトの管」で、古いフレームを捨てる意味を持っていない。
# 消費レートが生成レートを1フレームでも下回ると差分は TCP バッファと配信側の
# クライアントキューに積み上がり、遅延は減らないまま張り付く(実測 2.3秒)。
# そこで専有スレッドがパイプを空に保ちながら読み進め、**最新の1枚だけ**を保持する。
# 解析側は cap を触らずその1枚を受け取るため、遅れが作られない。
def _start_reader(cv2, key, stream):
    thread = threading.Thread(
        target=_reader_loop, args=(cv2, key, stream),
        name=f"camera-reader-{key}", daemon=True,
    )
    stream["reader"] = thread
    thread.start()


def _reader_loop(cv2, key, stream):
    stop = stream["stop"]
    while not stop.is_set():
        cap = stream.get("cap")
        if cap is None:
            _reconnect_network(cv2, key, stream)
            time.sleep(0.2)
            continue
        try:
            ok, frame = cap.read()
        except Exception as exc:  # noqa: BLE001 - 読取失敗でスレッドを死なせない
            ok, frame = False, None
            print(f"[CameraReader:{key}] 読取例外 {type(exc).__name__}: {exc}")
        if ok and frame is not None:
            with stream["frame_lock"]:
                stream["frame"] = frame
                stream["seq"] += 1
                stream["ts"] = time.monotonic()
            continue
        if stop.is_set():
            break
        # 映像が途絶えた(切断/待機)。間隔を空けて再接続する
        _reconnect_network(cv2, key, stream)
        time.sleep(0.2)

    # cap はこのスレッド専用。閉じるのもここ(他スレッドが read 中に release すると
    # native層で use-after-free になるため、release() は stop を立てるだけ)。
    cap = stream.pop("cap", None)
    if cap is not None:
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass


def _peek_frame(stream):
    """最新フレームをコピーして返す(消費カウントは動かさない)"""
    with stream["frame_lock"]:
        frame, ts = stream["frame"], stream["ts"]
    if frame is None:
        return None
    stale = float(getattr(settings, "AI_NETWORK_FRAME_STALE_SECONDS", 3.0))
    if time.monotonic() - ts > stale:
        return None
    return frame.copy()


def _latest_frame(stream):
    """解析向けの1枚。新フレームを少しだけ待ち、取れなければ最新を返す"""
    wait = float(getattr(settings, "AI_NETWORK_WAIT_NEW_FRAME_SECONDS", 0.05))
    deadline = time.monotonic() + wait
    while True:
        with stream["frame_lock"]:
            seq, frame = stream["seq"], stream["frame"]
            newer = seq != stream["served"]
            if newer:
                stream["served"] = seq
        if newer or time.monotonic() >= deadline:
            break
        time.sleep(0.004)
    return _peek_frame(stream)


def frame_age_seconds(index):
    """今使えるフレームが撮像から何秒経っているか(=経路の遅延そのもの)

    読取スレッドはパイプを空に保つので、この値は実質「カメラ→サーバ」の片道時間。
    USBカメラ(専有スレッドなし)は None。
    """
    with _streams_lock:
        stream = _streams.get(index)
    if stream is None or stream.get("reader") is None:
        return None
    with stream["frame_lock"]:
        if stream["frame"] is None:
            return None
        return round(time.monotonic() - stream["ts"], 2)



def acquire(index):
    """ストリーム用にキャプチャを開く(視聴者数を管理)。開けなければFalse

    「開く」処理は他カメラが使用中だと数十秒ハングすることがあるため、
    `_streams_lock` を持たずにやる(ハングが他の要求まで巻き込むのを防ぐ)。
    """
    cv2, _ = _load_cv2()
    if cv2 is None:
        return False
    if is_network_key(index):
        return _acquire_network(cv2, index)
    if not 0 <= index < _max_devices():
        return False
    with _streams_lock:
        stream = _streams.get(index)
        if stream is not None:
            stream["viewers"] += 1
            return True

    # MSMF で映像を返さないカメラも別バックエンドで取れるため総当たりする(ロック外)。
    # 配信開始の待ち合わせは長めにし、2台目(帯域が遅いカメラ)も起動できるようにする。
    patience = float(getattr(settings, "AI_CAMERA_OPEN_SECONDS", 10.0))
    cap, backend_name, frame = _open_capture(cv2, index, first_frame_seconds=patience)
    if cap is None:
        return False
    with _streams_lock:
        existing = _streams.get(index)
        if existing is not None:
            # 開いている間に別スレッドが確保していた → 余分に開けた方は閉じる
            existing["viewers"] += 1
            cap.release()
            return True
        _streams[index] = {
            "cap": cap,
            "lock": threading.Lock(),
            "viewers": 1,
            "holds": 0,
            "info": _capture_info(cap, index, backend_name),
        }
        return True


def read_jpeg(index, quality=80):
    """1フレームをJPEGバイト列で返す(失敗時はNone)

    ネットワークカメラは専有スレッドが読むので、ここでは**最新1枚をエンコードするだけ**。
    `cap.read()` をここで呼ぶと解析側とフレームを取り合いになる。
    """
    cv2, _ = _load_cv2()
    if cv2 is None:
        return None
    with _streams_lock:
        stream = _streams.get(index)
    if stream is None:
        return None
    if stream.get("reader") is not None:
        frame = _peek_frame(stream)
        if frame is None:
            return None
        encoded, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if encoded else None
    with stream["lock"]:
        ok, frame = stream["cap"].read()
        if not ok or frame is None:
            return None
        encoded, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not encoded:
            return None
        return buf.tobytes()


def read_frame(index):
    """1フレームをBGRのndarrayで返す(リアルタイムAI解析用。失敗時はNone)

    ネットワークカメラは専有スレッドが保つ「最新の1枚」を返す。
    `cap.read()` を解析スレッドで直接やると、消費が生成に届かない分だけ
    古いフレームを返すことになり、遅延が恒久化する(実測 2.3秒)。
    """
    cv2, _ = _load_cv2()
    if cv2 is None:
        return None
    with _streams_lock:
        stream = _streams.get(index)
    if stream is None:
        return None
    if stream.get("reader") is not None:
        return _latest_frame(stream)
    with stream["lock"]:
        ok, frame = stream["cap"].read()
        if ok and frame is not None:
            return frame
    return None


def _reconnect_network(cv2, key, stream):
    """RTSP/ストリームの再接続(短時間に連発しない)

    呼ぶのは専有読取スレッドだけ。`cap` を差し替えても安全なのはそのため。
    """
    now = time.monotonic()
    wait = float(getattr(settings, "AI_NETWORK_RECONNECT_SECONDS", 10.0))
    if now - stream.get("last_reconnect", 0.0) < wait:
        return False
    stream["last_reconnect"] = now
    old = stream.pop("cap", None)
    if old is not None:
        try:
            old.release()
        except Exception:  # noqa: BLE001
            pass
    url = stream.get("url") or _network_url(key)
    if not url:
        return False
    cap = cv2.VideoCapture(url)
    if cap.isOpened():
        stream["cap"] = cap
        print(f"[CameraReader:{key}] 再接続しました({url})")
        return True
    return False


def _retire(index, stream):
    """台帳から外す。ネットワークは専有スレッドに cap を閉じさせる"""
    _streams.pop(index, None)
    if stream.get("reader") is not None:
        stream["stop"].set()      # スレッド側で cap.release() される
        return None
    return stream.pop("cap", None)


def release(index):
    """視聴者1名の切断時に呼ぶ。視聴者0かつ解析中も無くなったらキャプチャを閉じる

    解析スレッドが `cap.read()` 中に他スレッドが `cap.release()` すると
    native層で use-after-free(プロセスごとクラッシュ)し得るため、
    解析中(`holds`が0より大きい)間は閉じずに待つ。
    """
    cap = None
    with _streams_lock:
        stream = _streams.get(index)
        if stream is None:
            return
        stream["viewers"] -= 1
        if stream["viewers"] > 0:
            return
        if stream.get("holds", 0) > 0:
            return  # 解析スレッドが使い終わったら unhold 側で閉じる
        cap = _retire(index, stream)
    if cap is not None:
        cap.release()


def hold(index):
    """解析スレッドがこのキャプチャを使い始めることを宣言する"""
    with _streams_lock:
        stream = _streams.get(index)
        if stream is None:
            return False
        stream["holds"] = stream.get("holds", 0) + 1
        return True


def unhold(index):
    """解析スレッドが使い終わった宣言。視聴者も0ならここで閉じる"""
    cap = None
    with _streams_lock:
        stream = _streams.get(index)
        if stream is None:
            return
        stream["holds"] = max(0, stream.get("holds", 0) - 1)
        if stream["holds"] > 0 or stream["viewers"] > 0:
            return
        cap = _retire(index, stream)
    if cap is not None:
        cap.release()
