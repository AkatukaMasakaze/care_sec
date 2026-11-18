# -*- coding: utf-8 -*-
r"""検知の総当たり解消(2026-11-18)の検証

    .\.venv\Scripts\python.exe -X utf8 verify_scan_cache.py

確認したいこと:
  1. フル検知は1回だけ全indexを開く
  2. その後の detect_devices(30秒サイクル)は **カメラを1つも開かない**(quick path)
  3. FULL_SCAN_SECONDS 経過/force/OS台数変化でのみフル検知が走る
  4. エンジンの target_devices は検知pendingでも 0..7 を総当たりしない
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from core import ai_engine, camera_service as cs  # noqa: E402

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s %s" % ("OK" if ok else "NG", name, detail))


def fake_probe_factory(state):
    def fake_probe(index, cv2, budget=6.0):
        state["probe"] += 1
        if index in (0, 1):
            return {
                "index": index, "name": "カメラ %d" % index,
                "width": 640, "height": 480, "fps": 30.0,
                "format": "MJPG", "backend": "MSMF",
            }
        return None
    return fake_probe


def main():
    # 30秒キャッシュに邪魔されず quick path を测试できるようにする
    settings.DEBUG_CAMERA_CACHE_SECONDS = 0.0
    state = {"probe": 0, "os_count": 2}

    cs._probe_with_budget = fake_probe_factory(state)
    cs._enumerate_os_cameras = lambda: (["A", "B"][:state["os_count"]], state["os_count"])
    cs._known_indices = []
    cs._known_devices = []
    cs._last_full_scan_at = 0.0
    cs._detect_cache["at"] = 0.0
    cs._detect_cache["devices"] = None

    print("=== 1) 最初の detect_devices = フル検知(既知が無いので総当たり) ===")
    r = cs.detect_devices()
    if cs._detect_thread:
        cs._detect_thread.join(10)
    first_probes = state["probe"]
    check("フル検知で index 0..1 を実際に開いた", first_probes == 2, "probe=%d" % first_probes)
    check("既知indexに記録された", cs.known_indices() == [0, 1], str(cs.known_indices()))

    print("=== 2) 以降の30秒サイクルはカメラを開かない(quick path) ===")
    before = state["probe"]
    for _ in range(5):  # 5サイクル分
        r = cs.detect_devices()
        check_flag = r.get("quick") is True and len(r.get("devices", [])) == 2
        if not check_flag:
            break
    check("5サイクル連続で quick:true / 開いたカメラ0", state["probe"] == before and check_flag,
          "probe増=%d" % (state["probe"] - before))

    print("=== 3) FULL_SCAN_SECONDS 経過でフル検知に戻る ===")
    settings.DEBUG_CAMERA_FULL_SCAN_SECONDS = 1.0
    time.sleep(1.1)
    before = state["probe"]
    r = cs.detect_devices()
    if cs._detect_thread:
        cs._detect_thread.join(10)
    check("期限切れフル検知が走った(再probe)", state["probe"] > before,
          "probe増=%d quick=%s" % (state["probe"] - before, r.get("quick")))

    print("=== 4) force(再検知ボタン)は常にフル検知 ===")
    settings.DEBUG_CAMERA_FULL_SCAN_SECONDS = 300
    r = cs.detect_devices(force=True)
    if cs._detect_thread:
        cs._detect_thread.join(10)
    check("forceでフル検知", not r.get("quick") and r.get("devices") is not None, str(r.get("stage", "")))

    print("=== 5) エンジンの target_devices: pendingでも 0..7 を総当たりしない ===")
    orig_detect = cs.detect_devices
    try:
        cs.detect_devices = lambda *a, **k: {"ok": True, "pending": True, "devices": []}
        targets = ai_engine.ai_engine.target_devices()
        usb = [t for t in targets if isinstance(t, int)]
        check("検知pending時でも既知indexのみ(0,1)", usb == [1, 0] or usb == [0, 1], str(usb))

        cs._known_indices = []
        cs._last_full_scan_at = time.monotonic()
        targets = ai_engine.ai_engine.target_devices()
        usb = [t for t in targets if isinstance(t, int)]
        check("初回(既知なし)だけ範囲探索(0..7)", usb == [7, 6, 5, 4, 3, 2, 1, 0], str(usb))
    finally:
        cs.detect_devices = orig_detect

    print()
    print("結果: %d/%d 通過" % (len(PASS), len(PASS) + len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
