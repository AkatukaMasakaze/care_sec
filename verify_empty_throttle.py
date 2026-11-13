# -*- coding: utf-8 -*-
r"""無人カメラの間引きゲートを、実時間・偽MediaPipeで検証する

エンジンのループはそのまま動かし、landmarker だけを偽物に差し替えて
「実際に detect_for_video が何回呼ばれたか」を数える。

    .\.venv\Scripts\python.exe -X utf8 verify_empty_throttle.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import numpy as np  # noqa: E402

from django.conf import settings  # noqa: E402

from core import ai_engine  # noqa: E402


class FakeResult:
    def __init__(self, landmarks):
        self.pose_landmarks = landmarks


class FakeLM:
    """少しだけ揺らす(無動作アラート経路に入らないため)"""

    def __init__(self, i, t):
        self.x = 0.4 + 0.01 * ((i + int(t * 10)) % 3)
        self.y = 0.5 + 0.01 * ((i * 7 + int(t * 13)) % 5)
        self.z = 0.0


class FakeLandmarker:
    """person_schedule: (秒, 有人bool) の切り替え。calls を数える"""

    def __init__(self, duration, person_schedule, jitter=False):
        self.duration = duration
        self.schedule = sorted(person_schedule)
        self.calls = 0
        self.person = False
        self.t0 = time.monotonic()
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)

    def detect_for_video(self, img, ts):
        el = time.monotonic() - self.t0
        for off, on in self.schedule:
            if el >= off:
                self.person = on
        self.calls += 1
        if self.person:
            return FakeResult([[FakeLM(i, el) for i in range(33)]])
        return FakeResult(None)


SHARED_KEYS = ("np", "connections", "fall_infer", "state_infer", "state_labels", "infer_lock")


def run_case(label, duration, person_schedule, optimize=True, probe=0.2, hold=2.0):
    settings.AI_EMPTY_CAMERA_OPTIMIZATION = optimize
    settings.AI_EMPTY_PROBE_SECONDS = probe
    settings.AI_PERSON_LOST_SECONDS = hold

    fake = FakeLandmarker(duration, person_schedule)
    rt = ai_engine._CameraRuntime("verify")

    shared = {
        "np": np,
        "connections": [],
        "fall_infer": lambda x: np.array([[0.01]]),
        "state_infer": lambda x: np.zeros((x.shape[0], 8)) + 0.1,
        "state_labels": ["立っている"] * 8,
        "infer_lock": threading.Lock(),
    }

    orig_read = ai_engine.camera_service.read_frame
    orig_save = ai_engine.camera_service.save_snapshot
    orig_alert = ai_engine._raise_alert
    ai_engine.camera_service.read_frame = lambda dev: fake.frame
    ai_engine.camera_service.save_snapshot = lambda dev, f: None
    # 検証が実DBへアラート/カメラ台帳行を書き込まないようにする(第39章の教訓)
    ai_engine._raise_alert = lambda *a, **k: None

    th = None
    try:
        th = threading.Thread(target=rt._loop, args=(rt.stop, shared, fake), daemon=True)
        th.start()
        time.sleep(duration)
        rt.stop.set()
        th.join(timeout=5)
    finally:
        ai_engine.camera_service.read_frame = orig_read
        ai_engine.camera_service.save_snapshot = orig_save
        ai_engine._raise_alert = orig_alert

    with rt.lock:
        snap = dict(rt.status)
    return fake.calls, snap


def judge(label, calls, snap, expect, expect_range=None):
    if expect_range:
        lo, hi = expect_range
        ok = lo <= calls <= hi
    else:
        ok = calls == expect
    print("  %-34s POSE=%3d回(期待 %s) ai_throttled=%s person_present=%s pose_fps=%s → %s" % (
        label, calls,
        ("%d-%d" % (lo, hi)) if expect_range else expect,
        snap.get("ai_throttled"), snap.get("person_present"), snap.get("pose_fps"),
        "OK" if ok else "NG"))
    return ok


def main():
    # 実運用では _get_landmarker がメインスレッドで mediapipe を読み込んでから
    # 解析スレッドを立てる。検証でも同じ順にしておく(スレッド内 lazy import は
    # joblib/loky の atexit 登録と競合して落ちることがある)
    ai_engine._mp_image(np.zeros((8, 8, 3), dtype=np.uint8))

    results = []

    print("=== A: 最初から無人 6秒(期待 ≈6/0.2=30回 ±10) ===")
    c, s = run_case("A", 6.0, [])
    results.append(judge("A 無人", c, s, None, expect_range=(18, 42)))

    print("=== B: 3秒無人→有人 計6秒(期待 15 + 88 ≈ 100 ±18) ===")
    c, s = run_case("B", 6.0, [(3.0, True)])
    results.append(judge("B 人が出る", c, s, None, expect_range=(80, 125)))

    print("=== C: 有人2秒→消える→猶予2秒→間引き2秒(期待 58+58+9 ≈ 125 ±20) ===")
    c, s = run_case("C", 6.0, [(0.0, True), (2.0, False)])
    results.append(judge("C 消える→間引き", c, s, None, expect_range=(100, 150)))

    print("=== D: 最適化OFF 無人6秒(期待 ≈175 = フル) ===")
    c, s = run_case("D", 6.0, [], optimize=False)
    results.append(judge("D OFF", c, s, None, expect_range=(150, 200)))

    print("=== E: 間引き中に人が出たら 0.5秒以内に有人へ復帰か ===")
    # 4秒無人(間引き状態)→有人2秒。復帰は probe(0.2s)以内のはず
    c, s = run_case("E", 6.0, [(4.0, True)])
    ok = s.get("person_present") is True and not s.get("ai_throttled")
    print("  %-34s POSE=%3d回 person_present=%s ai_throttled=%s → %s" % (
        "E 復帰", c, s.get("person_present"), s.get("ai_throttled"), "OK" if ok else "NG"))
    results.append(ok)

    print()
    print("結果: %d/%d 通過" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
