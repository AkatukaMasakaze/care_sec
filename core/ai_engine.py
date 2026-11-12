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
FALL_CONFIRM_COUNT = 3

FOOT_INDICES = (27, 28, 29, 30, 31, 32)

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
        fall_path = model_dir / "fall_detection_lstm.keras"
        state_path = model_dir / "state_detection_lstm.keras"
        labels_path = model_dir / "state_labels.npy"

        missing = [str(p) for p in (pose_path, fall_path, state_path, labels_path) if not p.exists()]
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
        fall_model = load_model(str(fall_path))
        state_model = load_model(str(state_path))
        state_labels = [str(label) for label in np.load(str(labels_path), allow_pickle=True)]

        readiness.model_step(78, "推論関数を tf.function にコンパイル中…")
        _shared = {
            "np": np,
            "pose_path": str(pose_path),
            "connections": vision.PoseLandmarksConnections.POSE_LANDMARKS,
            "fall_infer": _make_inferencer(fall_model),
            "state_infer": _make_inferencer(state_model),
            "state_labels": state_labels,
            # Kerasモデルは複数スレッドから同時に呼ばない(推論は約7msなので直列化しても速い)
            "infer_lock": threading.Lock(),
        }
        seconds = round(time.monotonic() - began, 1)
        readiness.model_ready(seconds)
        readiness.note("model", "AIモデルの準備が完了しました(%.1f秒)" % seconds, "ok")
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
                num_poses=1,
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
            "fps": 0.0,
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

        sequence = deque(maxlen=SEQUENCE_LENGTH)
        started = time.monotonic()
        last_analysis = 0.0
        frame_timestamp = 0
        prev_points = None

        # AI設定(転倒閾値・無動作秒数・クールダウン)は2秒ごとに読み直す
        ai = AISettings.load()
        ai_loaded_at = started

        # 判定状態
        status = "WAITING (0/30)"
        state_label = "判定中"
        danger_percent = None
        foot_visible = False
        fall_count = 0
        fall_latched = False       # 転倒アラートを一度出したら解除まで抑制
        still_since = None         # 最後に「動いている」と判定した時刻
        still_alerted = False      # 無動作アラートを出したら移動が再開するまで抑制
        still_seconds = 0

        # 統計
        window_start = started
        window_count = 0
        current_fps = 0.0

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

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # MediaPipe VIDEOモードは「単調増加」のタイムスタンプを要求する。
            # 再開時に0へ巻き戻らないよう monotonic の時刻を使い、同値も防ぐ
            frame_timestamp = max(int(time.monotonic() * 1000), frame_timestamp + 1)
            result = landmarker.detect_for_video(_mp_image(rgb_frame), frame_timestamp)

            if result.pose_landmarks:
                landmarks = result.pose_landmarks[0]

                # 33ランドマーク × x,y,z = 99値
                pose_data = []
                points = []
                for landmark in landmarks:
                    pose_data.extend([landmark.x, landmark.y, landmark.z])
                    points.append((landmark.x, landmark.y))
                sequence.append(pose_data)

                # --- 無動作の判定(前フレームとの移動量) ---
                if prev_points is None:
                    moving = True
                else:
                    move = sum(
                        abs(a[0] - b[0]) + abs(a[1] - b[1])
                        for a, b in zip(points, prev_points)
                    ) / len(points)
                    moving = move >= STILLNESS_MOVE_THRESHOLD
                prev_points = points

                if moving:
                    still_since = time.monotonic()
                    still_alerted = False
                elif still_since is None:
                    still_since = time.monotonic()
                still_seconds = int(time.monotonic() - still_since)

                # --- 足が画面内にあるか ---
                visible_foot = sum(
                    1 for idx in FOOT_INDICES
                    if 0.0 <= landmarks[idx].x <= 1.0 and 0.0 <= landmarks[idx].y <= 1.0
                )
                foot_visible = visible_foot >= 4

                # --- 骨格の描画(見る人がいる / 録画中のときだけ) ---
                if draw:
                    for connection in pose_connections:
                        start, end = landmarks[connection.start], landmarks[connection.end]
                        x1, y1 = int(start.x * width), int(start.y * height)
                        x2, y2 = int(end.x * width), int(end.y * height)
                        if 0 <= x1 < width and 0 <= y1 < height and 0 <= x2 < width and 0 <= y2 < height:
                            cv2.line(frame, (x1, y1), (x2, y2), (0, 255, 255), 3, cv2.LINE_AA)
                    for landmark in landmarks:
                        x, y = int(landmark.x * width), int(landmark.y * height)
                        if 0 <= x < width and 0 <= y < height:
                            cv2.circle(frame, (x, y), 5, (0, 255, 0), -1, cv2.LINE_AA)

                # --- 30フレームたまったら予測 ---
                if len(sequence) == SEQUENCE_LENGTH:
                    inputs = np.expand_dims(np.array(sequence, dtype=np.float32), axis=0)
                    # Kerasモデルは複数カメラで共有しているため直列化(推論は約7ms)
                    with infer_lock:
                        probability = float(fall_infer(inputs)[0][0])
                        state_id = int(np.argmax(state_infer(inputs)[0]))
                    danger_percent = probability * 100.0

                    if probability >= threshold:
                        status = "FALL"
                        fall_count += 1
                    else:
                        status = "NORMAL"
                        fall_count = 0

                    if fall_count >= FALL_CONFIRM_COUNT:
                        status = "FALL !!!"

                    predicted_state = state_labels[state_id]
                    state_label = "転倒" if status == "FALL !!!" else predicted_state
                else:
                    status = f"WAITING ({len(sequence)}/{SEQUENCE_LENGTH})"
                    state_label = "判定中"
                    danger_percent = None
                    fall_count = 0
            else:
                # 人が映っていない
                sequence.clear()
                prev_points = None
                fall_count = 0
                danger_percent = None
                still_since = None
                still_seconds = 0
                foot_visible = False
                status = "NO PERSON"
                state_label = "判定中"

            # ---------------- 異常検知(Alert作成 → /api/alerts/ 通知) ----------------
            alert_info = None
            if status == "FALL !!!" and not fall_latched:
                # 信頼度は ai2 の規約どおり 0.0〜1.0 で保存する
                alert_info = _raise_alert(
                    device, Alert.Kind.FALL, (danger_percent or 0.0) / 100.0,
                    PersonState.LYING, ai, frame=frame,
                )
                fall_latched = True
            if fall_count == 0:
                fall_latched = False

            if (
                status not in ("FALL !!!", "FALL", "NO PERSON")
                and still_seconds >= ai.stillness_seconds
                and not still_alerted
                and danger_percent is not None
            ):
                confidence = min(0.99, 0.55 + still_seconds / 200)
                alert_info = _raise_alert(
                    device, Alert.Kind.SUSPICIOUS, confidence,
                    STATE_TO_PERSON_STATE.get(state_label, PersonState.UNKNOWN), ai, frame=frame,
                )
                still_alerted = True

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

            # ---------------- overlay描画 + JPEG化(見る人がいる時だけ) ----------------
            buf = None
            if draw:
                if status in ("FALL", "FALL !!!"):
                    status_display, status_color = "FALL", (0, 0, 255)
                elif status == "NORMAL":
                    status_display, status_color = "NORMAL", (0, 255, 0)
                else:
                    status_display, status_color = "NORMAL", (0, 255, 255)

                danger_text = f"危険度：{danger_percent:.2f}%" if danger_percent is not None else "危険度：--"
                foot_text = "足：OK" if foot_visible else "足：画面外"
                foot_color = (0, 255, 0) if foot_visible else (0, 165, 255)

                _draw_text(frame, cv2, "Caregiver Safety Monitor", (15, 22), scale=0.45, thickness=1)
                _draw_japanese_overlay(frame, cv2, [
                    (f"状態：{state_label}", (15, 30), (255, 255, 255)),
                    (f"判定：{status_display}", (15, 54), status_color),
                    (danger_text, (15, 78), (255, 255, 255)),
                    (foot_text, (15, 102), foot_color),
                    (f"無動作：{still_seconds}秒", (15, 126), (255, 255, 255)),
                ])

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
                window_start, window_count = now, 0
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
                "sequence_frames": len(sequence),
                "fps": current_fps,
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


def _raise_alert(device, kind, confidence, person_state, ai, frame=None):
    """ai2 の方式でアラートを作成する(クールダウン適用 / 失敗しても解析は止めない)"""
    try:
        # 「AI監視」が停止中(設定ページ)はアラートを作らない
        if not ai.enabled:
            return None

        camera = _camera_for(device)
        if camera is None:
            # ネットワークカメラが台帳から消えた等のケース(解析は続けない)
            return None
        now = timezone.now()

        last = Alert.objects.filter(camera=camera).order_by("-occurred_at").first()
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
        )
        print(f"[RealtimeAI:{device}] 異常検知: {alert.get_kind_display()} @ {camera.name} "
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
    def target_devices():
        """常時稼働の対象カメラ一覧と、**開く順序**

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

        # 1) ネットワークカメラ(IP): USB总线を介さないので同時運用の制約を受けない
        ip_keys = [source["key"] for source in camera_service.network_sources()]

        # 2) ローカル(USB)カメラ
        if explicit is not None:
            candidates = sorted((int(index) for index in explicit), reverse=True)
        else:
            result = camera_service.detect_devices()
            found = [
                int(device["index"])
                for device in result.get("devices", [])
                if device.get("available", True)
            ]
            if not found:
                found = list(range(int(getattr(settings, "DEBUG_CAMERA_MAX_DEVICES", 4))))
            candidates = sorted(found, reverse=True)

        if priority:
            ordered = [int(index) for index in priority]
            candidates = ordered + [index for index in candidates if index not in ordered]

        # IP を先、USB を後(USB側は先に開いた方が帯域を取る)
        return ip_keys + candidates

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
