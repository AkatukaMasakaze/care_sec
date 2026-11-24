# -*- coding: utf-8 -*-
"""ai2本番venvで「カメラ稼働中に別カメラopen」のネイティブクラッシュを再現测试

用途: 0xc0000374(heap corruption)の犯人特定。サーバー起動せず単体で。
    $env:AI2_CRASH_MODE="1"  ...  順に 1→2→3→4 で実行し exit code を見る
クラッシュ時 exit code: 3221225474(0xC0000005) / 3221226260(0xC0000374) 等

  1 = cam0 read → 閉じる → cam1 open/read(cam0非稼働中: ベースライン)
  2 = cam0 read 継続スレッド + 別スレッドで cam1 open 2回(1台稼働中のopen試行)
  3 = モデル+mediapipeロード後 に 2 と同じ(TF/MediaPipeのネイティブ共存チェック)
  4 = 2台同時open両方read(スレッド)— 本番構成そのもの
"""
import os
import sys
import threading
import time

sys.path.insert(0, r"C:\Users\student\code\ai2")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def log(msg):
    print("STEP %s" % msg, flush=True)


def read_loop(stop, idx, seconds=3.0):
    import cv2

    cap = cv2.VideoCapture(idx)
    n = 0
    t0 = time.perf_counter()
    while not stop.is_set() and time.perf_counter() - t0 < seconds:
        ok, _ = cap.read()
        if ok:
            n += 1
    cap.release()
    log("cam%d read done frames=%d" % (idx, n))


def try_open(stop, idx, tag=""):
    import cv2

    cap = cv2.VideoCapture(idx)
    opened = cap.isOpened()
    n = 0
    if opened:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 1.5:
            ok, _ = cap.read()
            if ok:
                n += 1
    cap.release()
    log("cam%d%s open=%s frames=%d" % (idx, tag, opened, n))


def main():
    mode = os.environ.get("AI2_CRASH_MODE", "1")
    log("mode=%s" % mode)

    if mode == "1":
        stop = threading.Event()
        read_loop(stop, 0)
        try_open(stop, 1)
        log("OK")

    elif mode == "2":
        stop = threading.Event()
        th0 = threading.Thread(target=read_loop, args=(stop, 0, 8.0), daemon=True)
        th0.start()
        time.sleep(1.0)
        try_open(stop, 1, "(while cam0 active)")
        try_open(stop, 1, "(2nd attempt)")
        stop.set()
        th0.join(10)
        log("OK")

    elif mode == "3":
        import django

        django.setup()
        import mediapipe as mp  # noqa: F401
        import cv2

        from core import ai_engine

        shared = ai_engine._load_shared()
        log("models=%s" % shared.get("fall_members"))
        img = cv2.imread(r"C:\Users\student\code\ai2\static\img\favicon.ico")
        if img is not None:
            ai_engine._mp_image(img)
        stop = threading.Event()
        th0 = threading.Thread(target=read_loop, args=(stop, 0, 8.0), daemon=True)
        th0.start()
        time.sleep(1.0)
        try_open(stop, 1, "(with TF+MP loaded)")
        stop.set()
        th0.join(10)
        log("OK")

    elif mode == "4":
        import django

        django.setup()
        import mediapipe as mp  # noqa: F401

        from core import ai_engine

        shared = ai_engine._load_shared()
        log("models=%s" % shared.get("fall_members"))
        stop = threading.Event()
        th0 = threading.Thread(target=read_loop, args=(stop, 0, 10.0), daemon=True)
        th1 = threading.Thread(target=read_loop, args=(stop, 1, 10.0), daemon=True)
        th0.start()
        th1.start()
        th0.join(15)
        th1.join(15)
        log("OK both")
    elif mode == "5":
        # 本番の10〜5分サイクル相当: engine側(camera_service)がindex0を
        # DSHOWで保持している最中に、別スレッドの detect_devices が
        # MSMF/DSHOW総当たりプロブを実行する(クラッシュ直前に走っていた処理)
        import django

        django.setup()
        from core import ai_engine, camera_service

        log("acquire0=%s" % camera_service.acquire(0))
        stop = threading.Event()
        th0 = threading.Thread(target=read_loop, args=(stop, 0, 20.0), daemon=True)
        th0.start()
        time.sleep(1.0)
        for r in range(3):
            camera_service.reset_probe_cache()
            res = camera_service.detect_devices(force=True)
            log("probe-round%d started" % (r + 1))
            # 非同期検知なので完了を待つ(最大25秒)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 25:
                time.sleep(0.5)
                prog = camera_service.detect_progress()
                if prog.get("state") == "done":
                    break
            log("probe-round%d done devices=%s" % (
                r + 1, [d.get("index") for d in prog.get("devices", [])]))
        stop.set()
        th0.join(6)
        camera_service.release(0)
        log("OK probe")

    elif mode == "7":
        # torch+TF+DSHOW 同プロセスで heap corruption(0xc0000374)が出るか?
        # (srv5=CLIPウォームアップ後3.5分、ユーザー環境=CLIP初回読込後にクラッシュ)
        import django

        django.setup()
        from core import ai_engine, camera_service

        shared = ai_engine._load_shared()
        log("models=%s" % shared.get("fall_members"))
        t0 = time.monotonic()
        stop = threading.Event()
        th0 = threading.Thread(target=read_loop, args=(stop, 0, 60.0), daemon=True)
        th0.start()
        time.sleep(2.0)
        import cv2
        import numpy as np

        img = np.zeros((480, 640, 3), np.uint8)
        for r in range(60):
            ai_engine._clip_verify_lying(img, cv2, (100, 100, 300, 400))
            time.sleep(0.2)
        log("clip calls done (torch+TF共存 %.0f秒)" % (time.monotonic() - t0))
        time.sleep(20)
        stop.set()
        th0.join(8)
        log("OK coexist")

    elif mode == "8":
        # サーバー起動21秒クラッシュ(0xc0000374)の再現試行:
        # 実エンジングループ(MediaPipe pose×2台)稼働中に CLIPウォームアップ
        # (torch import+推論)が並走する = 本番と同一のネイティブ同居順
        import django

        django.setup()
        import cv2
        import numpy as np

        from core import ai_engine, camera_service

        shared = ai_engine._load_shared()
        log("models=%s warmup-started=%s" % (
            shared.get("fall_members"),
            not ai_engine._clip_state["loaded"]))
        rts = []
        for idx in (0, 1):
            if camera_service.acquire(idx):
                rt = ai_engine._CameraRuntime(idx)
                lm = ai_engine._get_landmarker(idx)
                th = threading.Thread(target=rt._loop, args=(rt.stop, shared, lm),
                                      daemon=True)
                th.start()
                rts.append((idx, rt, th))
        log("engines=%s" % [i for i, _, _ in rts])
        # 本番のwarmup相当: 裏でtorch import→CLIPロード→推論
        t0 = time.monotonic()
        for r in range(200):
            img = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
            v = ai_engine._clip_verify_lying(img, cv2, (100, 100, 300, 400))
            if r % 40 == 0:
                log("clip#%d verdict=%s elapsed=%.0fs" % (r, v, time.monotonic() - t0))
            time.sleep(0.1)
        for _, rt, th in rts:
            rt.stop.set()
            th.join(6)
            camera_service.release(rt.device)
        log("OK engines+clip")

    else:
        log("usage: AI2_CRASH_MODE=1..4")


if __name__ == "__main__":
    main()
