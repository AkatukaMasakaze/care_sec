# -*- coding: utf-8 -*-
r"""1カメラ分の処理を工程別に計測し、CPU使用率の実態と台数の頭打ち要因を出す

    .\.venv\Scripts\python.exe -X utf8 profile_ai_stage.py                 # USB index 0
    .\.venv\Scripts\python.exe -X utf8 profile_ai_stage.py --device cam:9  # Piカメラ
    .\.venv\Scripts\python.exe -X utf8 profile_ai_stage.py --frames 300 --no-view

出すもの:
  - 工程別 ms/枚(read / cvtColor / **POSE** / LSTM / draw / JPEG)
  - 実時間(wall)1枚あたり と、そこに消費した **CPU時間**
  - CPU倍率 = 1枚の処理に何コア分使っているか(=台数が増えたとき何コア食うか)
  - 環境変数未設定時、MediaPipe/TensorFlow が内部で何スレッド張っているか
"""
import argparse
import multiprocessing
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import cv2  # noqa: E402
from django.conf import settings  # noqa: E402

from core import ai_engine, camera_service  # noqa: E402


class Timer:
    def __init__(self):
        self.acc = {}
        self.n = 0
        self.enabled = True

    def __call__(self, key, started):
        if not self.enabled:
            return
        self.acc[key] = self.acc.get(key, 0.0) + (time.perf_counter() - started)

    def ms(self, key):
        return self.acc.get(key, 0.0) / max(1, self.n) * 1000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="0")
    ap.add_argument("--frames", type=int, default=150)
    ap.add_argument("--warmup", type=int, default=40,
                    help="計測から除く枚数(tf.function のトレースや MediaPipe 初期化が"
                         "平均に混ざると、LSTM が実態の2〜3倍に見える)")
    ap.add_argument("--no-view", action="store_true", help="描画とJPEG化を回さない(見る人がいない状態)")
    args = ap.parse_args()

    device = int(args.device) if args.device.isdigit() else args.device
    physical = multiprocessing.cpu_count()
    logical = os.cpu_count() or physical

    shared = ai_engine._load_shared()
    landmarker = ai_engine._get_landmarker(device)
    np = shared["np"]
    from mediapipe import Image as MPImage, ImageFormat

    print("device=%s / 物理%sコア・論理%sスレッド / AI_ANALYSIS_FPS=%s" % (
        device, physical, logical, settings.AI_ANALYSIS_FPS))

    if not camera_service.acquire(device):
        print("カメラを開けません")
        return
    try:
        frame = None
        for _ in range(80):
            frame = camera_service.read_frame(device)
            if frame is not None:
                break
            time.sleep(0.05)
        if frame is None:
            print("フレームが取れません")
            return

        tm = Timer()
        tm.enabled = False          # ウォームアップ中は計測しない
        ts = 0
        pose_ok = 0
        done = 0
        wall0 = time.perf_counter()
        cpu0 = time.process_time()

        while done < args.frames + args.warmup:
            t = time.perf_counter()
            f = camera_service.read_frame(device)
            tm("read", t)
            if f is None:
                time.sleep(0.01)
                continue
            done += 1
            if done == args.warmup:
                # ここから本計測(タイマーも wall/CPU もリセット)
                tm.enabled = True
                tm.n = 0
                wall0 = time.perf_counter()
                cpu0 = time.process_time()

            if tm.enabled:
                tm.n += 1

            t = time.perf_counter()
            rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
            tm("cvtColor", t)

            ts = max(int(time.monotonic() * 1000), ts + 1)
            t = time.perf_counter()
            res = landmarker.detect_for_video(MPImage(image_format=ImageFormat.SRGB, data=rgb), ts)
            tm("POSE", t)

            t = time.perf_counter()
            if res.pose_landmarks:
                if tm.enabled:
                    pose_ok += 1
                vec = []
                for lm in res.pose_landmarks[0]:
                    vec.extend([lm.x, lm.y, lm.z])
                inputs = np.expand_dims(np.array([vec] * 30, dtype=np.float32), axis=0)
                with shared["infer_lock"]:
                    probability = float(shared["fall_infer"](inputs)[0][0])
                    state_id = int(np.argmax(shared["state_infer"](inputs)[0]))
            tm("LSTM", t)

            if not args.no_view:
                # 実運用で「見ている人がいる」ときと同じ経路(骨格+日本語overlay+JPEG)
                t = time.perf_counter()
                h, w = f.shape[:2]
                for i in range(20):
                    cv2.line(f, (10 + i * 5, 10), (60 + i * 5, h - 10), (0, 255, 255), 3, cv2.LINE_AA)
                ai_engine._draw_japanese_overlay(f, cv2, [
                    ("状態：歩いている", (15, 30), (255, 255, 255)),
                    ("判定：NORMAL", (15, 54), (0, 255, 0)),
                ])
                tm("draw", t)
                t = time.perf_counter()
                cv2.imencode(".jpg", f, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                tm("JPEG", t)

        wall = time.perf_counter() - wall0
        cpu_used = time.process_time() - cpu0
    finally:
        camera_service.release(device)

    measured = max(1, tm.n)
    keys = ["read", "cvtColor", "POSE", "LSTM", "draw", "JPEG"]
    print("\n=== 1枚あたり(ms) ※ウォームアップ%d枚を除いた実測 ===" % args.warmup)
    total = 0.0
    for k in keys:
        v = tm.ms(k)
        total += v
        bar = "#" * int(min(40, v / 1.0))
        print("  %-9s %7.2f ms %s" % (k, v, bar))
    print("  %-9s %7.2f ms  (合計)" % ("TOTAL", total))

    per_wall = wall / measured * 1000.0
    per_cpu = cpu_used / measured * 1000.0
    print("\n=== まとめ ===")
    print("  処理枚数              : %d(人が映っていた %d / %.0f%%)" % (
        measured, pose_ok, pose_ok / measured * 100))
    print("  実時間 1枚            : %.2f ms  → 1スレッドで回すと **%.1f fps**" % (per_wall, 1000.0 / per_wall))
    print("  CPU時間 1枚           : %.2f ms" % per_cpu)
    print("  CPU倍率               : **%.2f**  ← 1枚の処理に何コア分使っているか" % (cpu_used / wall))
    print("  プロセスのスレッド数  : %d" % threading.active_count())
    print("  スレッド数設定        : OMP=%s TF_INTRA=%s TF_INTER=%s (未設定=ライブラリが全スレッドを使う)" % (
        os.environ.get("OMP_NUM_THREADS", "-"),
        os.environ.get("TF_NUM_INTRAOP_THREADS", "-"),
        os.environ.get("TF_NUM_INTEROP_THREADS", "-")))

    print("\n=== 台数の見積(このマシン / 30fps維持) ===")
    cores_busy = cpu_used / wall
    print("  今の1台で常時使っているコア数 : %.2f" % cores_busy)
    print("  論理%sスレッド ÷ %.2f = **%.1f台** がスレッド数の上限見積" % (
        logical, cores_busy, logical / cores_busy))
    print("  POSEだけを分離できた場合      : POSE %.2fms/枚 → %.1f fps/1スレッド" % (
        tm.ms("POSE"), 1000.0 / max(0.01, tm.ms("POSE"))))
    print("  人が映っていないフレームは     : POSEが空振りでも走る(=ゲートで %.0f%% 削減余地)" % (
        max(0.0, 100 - pose_ok / measured * 100)))


if __name__ == "__main__":
    main()
