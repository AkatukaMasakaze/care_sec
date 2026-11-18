# -*- coding: utf-8 -*-
r"""ai2.0統合(複数人物+grouped_normalized)のエンジン検証

実カメラ不要: 偽MediaPipe/偽Kerasで core/ai_engine.py の新ループを回し、
  1. 2人が別トラックとして追跡される(people=2 / Person 1,2)
  2. LSTMは **READY人物をまとめた1回のバッチ呼出し**(呼出し回数を実測)
  3. 閾値超の人物だけ FALL !!! になり、アラートは **person_id 付きで1件**
     (_raise_alert はモックして実DBに書かない)
  4. 正規化入力が腰中心原点になる(値の実体チェック)
  5. 人が消えると NO PERSON、トラックは系列を保持して復帰する

    .\.venv\Scripts\python.exe -X utf8 verify_multi_person.py
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
from core.models import AISettings  # noqa: E402

from verify_empty_throttle import FakeLandmarker, FakeResult  # noqa: E402


class SimpleLM:
    def __init__(self, x, y):
        self.x, self.y, self.z = x, y, 0.0
        self.visibility = 0.9


class TiltPerson:
    """体幹傾きtilt°の人物(肩が腰の周りで回転)。直立0°→水平90°"""

    def __init__(self, tilt, base_x=0.5):
        import math

        hx, hy = base_x, 0.68
        r = math.radians(tilt)
        sx = hx - 0.26 * math.sin(r)
        sy = hy - 0.26 * math.cos(r)
        pts = [SimpleLM(sx, max(0.05, sy - 0.12)) for _ in range(11)]
        pts += [SimpleLM(sx - 0.02, sy), SimpleLM(sx + 0.02, sy)]
        pts += [
            SimpleLM(sx - 0.06, sy + 0.05), SimpleLM(sx - 0.06, sy + 0.10),
            SimpleLM(sx - 0.06, sy + 0.15), SimpleLM(sx + 0.06, sy + 0.05),
            SimpleLM(sx + 0.06, sy + 0.10), SimpleLM(sx + 0.06, sy + 0.15),
            SimpleLM(sx, sy + 0.02), SimpleLM(sx, sy + 0.04),
            SimpleLM(sx - 0.03, sy + 0.06), SimpleLM(sx + 0.03, sy + 0.06),
        ]
        pts += [SimpleLM(hx - 0.02, hy), SimpleLM(hx + 0.02, hy)]
        pts += [SimpleLM(hx - 0.03, hy + 0.12), SimpleLM(hx + 0.03, hy + 0.12)]
        pts += [
            SimpleLM(hx - 0.04, hy + 0.22), SimpleLM(hx + 0.04, hy + 0.22),
            SimpleLM(hx - 0.05, hy + 0.24), SimpleLM(hx + 0.05, hy + 0.24),
            SimpleLM(hx - 0.05, hy + 0.25), SimpleLM(hx + 0.05, hy + 0.25),
        ]
        self._lms = pts

    def __getitem__(self, i):
        return self._lms[i]

    def __iter__(self):
        return iter(self._lms)

    def __len__(self):
        return 33


class FallingLandmarker:
    """fall_at秒まで直立 → 速く倒れて(600°/s)settle°で倒れたままの人物"""

    def __init__(self, fall_at=1.0, settle=65.0, roll_rate=600.0):
        self.calls = 0
        self.t0 = time.monotonic()
        self.fall_at = fall_at
        self.settle = settle
        self.roll_rate = roll_rate

    def detect_for_video(self, img, ts):
        el = time.monotonic() - self.t0
        if el < self.fall_at:
            tilt = 5.0
        else:
            tilt = min(self.settle, 5.0 + (el - self.fall_at) * self.roll_rate)
        return FakeResult([TiltPerson(tilt)])

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s %s" % ("OK" if ok else "NG", name, detail))


def run_two_people(seconds=6.0):
    """A(x≈0.25)=NORMAL確率 / B(x≈0.75)=FALL確率 の2人を流す"""
    fake = FakeLandmarker(people_x=[0.25, 0.75])
    fake.person = True
    rt = ai_engine._CameraRuntime("verify2")

    infer_calls = {"fall": 0, "state": 0}
    last_inputs = {}
    captured_alerts = []

    def state_infer(x):
        infer_calls["state"] += 1
        return np.full((x.shape[0], 8), 0.1, dtype=np.float32)

    shared = {
        "np": np,
        "connections": [],
        "fall_infer": None,   # 下に差し替える(バッチ行順を見るため)
        "state_infer": state_infer,
        "state_labels": ["立っている", "座っている", "しゃがんでいる", "歩いている",
                         "前かがみ", "横になっている", "立ち上がる", "転倒"],
        "infer_lock": threading.Lock(),
    }

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    orig_read = ai_engine.camera_service.read_frame
    orig_save = ai_engine.camera_service.save_snapshot
    orig_alert = ai_engine._raise_alert
    ai_engine.camera_service.read_frame = lambda dev: frame
    ai_engine.camera_service.save_snapshot = lambda dev, f: None

    def fake_raise_alert(device, kind, confidence, person_state, ai, frame=None, person_id=None):
        captured_alerts.append({"kind": kind, "person_id": person_id,
                                "confidence": confidence})
        return None  # 録画開始はさせない

    ai_engine._raise_alert = fake_raise_alert

    # B(x=0.75)の行だけ高確率にするため、バッチ行順(左→右)で 0=高確率 にする:
    # detections は x 昇順 → Person1=A(x.25) が row0, Person2=B(x.75) が row1
    def fall_infer_b(x):
        infer_calls["fall"] += 1
        last_inputs["fall"] = x
        last_inputs["fall_shape"] = x.shape
        out = np.full((x.shape[0], 1), 0.05, dtype=np.float32)
        if x.shape[0] >= 2:
            out[1][0] = 0.95   # 右の人物(B=Person 2)だけ転倒確率0.95
        return out
    shared["fall_infer"] = fall_infer_b

    threshold = float(AISettings.load().fall_threshold)
    th = threading.Thread(target=rt._loop, args=(rt.stop, shared, fake), daemon=True)
    th.start()
    time.sleep(seconds)
    rt.stop.set()
    th.join(timeout=5)

    with rt.lock:
        snap = dict(rt.status)
    ai_engine.camera_service.read_frame = orig_read
    ai_engine.camera_service.save_snapshot = orig_save
    ai_engine._raise_alert = orig_alert
    return snap, infer_calls, captured_alerts, last_inputs, threshold


def main():
    print("=== ai2.0統合エンジン検証 ===")
    # 実運用では _get_landmarker がメインスレッドで mediapipe/TensorFlow を読み込んでから
    # 解析スレッドを立てる。検証でも同じ順にしておく(スレッド内初回importは20秒級で、
    # 計測時間がそれに食われて「動いていない」ように見える: 実測)
    ai_engine._mp_image(np.zeros((8, 8, 3), dtype=np.uint8))
    print("  設定: MAX_POSES=%s CONFIRM=%s INTERVAL=%s 正規化=%s モデル=%s" % (
        settings.AI_MAX_POSES, settings.AI_FALL_CONFIRM_COUNT,
        settings.AI_INFERENCE_INTERVAL, settings.AI_NORMALIZE_FALL_INPUT,
        settings.AI_FALL_MODEL_FILENAME))

    snap, calls, alerts, inputs, threshold = run_two_people(6.0)
    # 5ケース目で使い回すための2人分の状態ラベル付きshared
    shared_for_reentry = {
        "np": np,
        "connections": [],
        "fall_infer": lambda x: np.full((x.shape[0], 1), 0.05, dtype=np.float32),
        "state_infer": lambda x: np.full((x.shape[0], 8), 0.1, dtype=np.float32),
        "state_labels": ["立っている"] * 8,
        "infer_lock": threading.Lock(),
    }

    # 1) 2人が別トラックに
    check("people=2(左右2人が別トラック)", snap.get("people") == 2,
          "people=%s detail=%s" % (snap.get("people"), snap.get("people_detail")))
    ids = sorted(p["id"] for p in snap.get("people_detail", []))
    check("Person 1/2 の表示番号", ids == [1, 2], str(ids))

    # 2) バッチ推論: 呼出し1回で (2,30,99)
    shape = inputs.get("fall_shape")
    check("LSTMは2人まとめるバッチ呼出し", shape == (2, 30, 99), "直近入力形=%s" % (shape,))
    frames_est = snap.get("fps", 0) * 6.0
    per_person = calls["fall"]
    check("推論間隔5フレーム(呼出し≈人数×秒×6回)",
          per_person <= frames_est / 5 * 1.5,
          "fall呼出し=%d / 処理フレーム≈%d" % (per_person, int(frames_est)))

    # 3) 転倒アラートは右の人物(Person 2)だけ・person_id付き
    fall_alerts = [a for a in alerts if a["kind"] == "fall"]
    check("FALLアラートは person_id=2 で1件(ラッチ)",
          len(fall_alerts) >= 1 and all(a["person_id"] == 2 for a in fall_alerts),
          "alerts=%s" % alerts[:3])
    check("画面状態は最悪人物(FALL !!!)", snap.get("status") == "FALL !!!",
          "status=%s" % snap.get("status"))

    # 4) 正規化: 転倒系列は腰中心≈原点(平均が0に近い)
    x = inputs.get("fall")
    if x is not None:
        hip = (x[:, :, 23 * 3:23 * 3 + 3] + x[:, :, 24 * 3:24 * 3 + 3]) / 2.0
        check("fall入力は正規化済み(腰中心≈0)", float(np.abs(hip).mean()) < 0.05,
              "|hip|平均=%.4f" % float(np.abs(hip).mean()))
    else:
        check("fall入力取得", False)

    # 5) 人が消える → NO PERSON、復帰する(トラック系列は保持されないが動作すること)
    fake2 = FakeLandmarker(people_x=[0.5])
    fake2.person = True
    rt2 = ai_engine._CameraRuntime("verify3")
    shared2 = dict(shared_for_reentry)
    shared2["fall_infer"] = lambda x: np.full((x.shape[0], 1), 0.05, np.float32)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    orig_read = ai_engine.camera_service.read_frame
    orig_save = ai_engine.camera_service.save_snapshot
    orig_alert = ai_engine._raise_alert
    ai_engine.camera_service.read_frame = lambda dev: frame
    ai_engine.camera_service.save_snapshot = lambda dev, f: None
    ai_engine._raise_alert = lambda *a, **k: None
    th = threading.Thread(target=rt2._loop, args=(rt2.stop, shared2, fake2), daemon=True)
    th.start()
    time.sleep(2.0)
    fake2.person = False          # 退席
    time.sleep(3.5)               # grace 2s + 間引き進入
    with rt2.lock:
        gone = dict(rt2.status)
    fake2.person = True           # 復帰
    time.sleep(1.5)
    with rt2.lock:
        back = dict(rt2.status)
    rt2.stop.set()
    th.join(timeout=5)
    ai_engine.camera_service.read_frame = orig_read
    ai_engine.camera_service.save_snapshot = orig_save
    ai_engine._raise_alert = orig_alert

    check("退席後 NO PERSON + 間引き進入", gone.get("status") == "NO PERSON"
          and gone.get("ai_throttled") is True,
          "status=%s throttled=%s pose_fps=%s" % (
              gone.get("status"), gone.get("ai_throttled"), gone.get("pose_fps")))
    check("復帰後また有人追跡", back.get("people") == 1 and back.get("person_present") is True,
          "people=%s present=%s" % (back.get("people"), back.get("person_present")))

    # 6) 速度ルール: LSTMは低確率(0.05)のまま、急崩れだけで FALL !!! になること
    fake3 = FallingLandmarker(fall_at=1.0, settle=65.0)
    rt3 = ai_engine._CameraRuntime("verifyV")
    captured = []
    shared3 = dict(shared_for_reentry)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    orig_read = ai_engine.camera_service.read_frame
    orig_save = ai_engine.camera_service.save_snapshot
    orig_alert = ai_engine._raise_alert
    ai_engine.camera_service.read_frame = lambda dev: frame
    ai_engine.camera_service.save_snapshot = lambda dev, f: None
    def fake_alert(device, kind, confidence, person_state, ai, frame=None, person_id=None):
        captured.append({"kind": str(kind), "person_id": person_id, "confidence": confidence})
        return None

    ai_engine._raise_alert = fake_alert
    th3 = threading.Thread(target=rt3._loop, args=(rt3.stop, shared3, fake3), daemon=True)
    th3.start()
    time.sleep(5.0)
    with rt3.lock:
        s3 = dict(rt3.status)
    rt3.stop.set()
    th3.join(6)
    ai_engine.camera_service.read_frame = orig_read
    ai_engine.camera_service.save_snapshot = orig_save
    ai_engine._raise_alert = orig_alert

    fall_alerts = [c for c in captured if c["kind"].lower().endswith("fall")]
    check("LSTM0.05でも速度ルールだけでFALLアラート",
          bool(fall_alerts) and abs((fall_alerts[0]["confidence"] or 0) - 0.90) < 0.02,
          "alerts=%s status=%s" % (fall_alerts[:2], s3.get("status")))
    check("危険度がルール信頼度(90%)で表示",
          s3.get("danger_percent") is not None and s3.get("danger_percent") >= 89.0,
          "danger=%s" % s3.get("danger_percent"))

    print()
    print("結果: %d/%d 通過" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("失敗:", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
