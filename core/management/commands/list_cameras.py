"""カメラデバイス総点検管理コマンド

Windows はバックエンド(MSMF / DSHOW)ごとにインデックスの割当順が違い、
さらに OpenCV の MSMF は使えないインデックスで約60秒ハングすることがある。
「2台あるはずなのに1台しか出ない」を切り分けるため、
OS列挙と各バックエンド/各インデックスの結果を全部表示する。

使い方:
    python manage.py list_cameras
    python manage.py list_cameras --shots      # 写っている画像も media/camtest/ に保存
"""
import time
from pathlib import Path

from django.core.management.base import BaseCommand

BACKEND_NAMES = ("MSMF", "DSHOW", "ANY")


class Command(BaseCommand):
    help = "接続カメラを一覧し、バックエンド別の開ける/映像が取れる結果を表示する"

    def add_arguments(self, parser):
        parser.add_argument(
            "--shots",
            action="store_true",
            help="読めたフレームを media/camtest/ にJPEG保存する(どれがどのカメラか確認用)",
        )
        parser.add_argument(
            "--max",
            type=int,
            default=4,
            help="試すインデックスの数(既定 4)",
        )
        parser.add_argument(
            "--simultaneous",
            action="store_true",
            help="検知できた全カメラを同時に開いて実効fpsを測る(複数台運用がhw的に可能かの確認)",
        )
        parser.add_argument(
            "--seconds",
            type=float,
            default=5,
            help="--simultaneous の測定秒数(既定 5)",
        )

    def handle(self, *args, **options):
        import cv2
        import subprocess

        max_index = options["max"]
        shots_dir = None
        if options["shots"]:
            from django.conf import settings

            shots_dir = Path(settings.MEDIA_ROOT) / "camtest"
            shots_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"OpenCV {cv2.__version__}")

        self.stdout.write("\n=== OS列挙(PnP: Camera / Image) ===")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -Class Camera, Image -PresentOnly -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty FriendlyName"],
            capture_output=True, text=True, timeout=30,
        )
        names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if names:
            for i, name in enumerate(names):
                self.stdout.write(f"  [{i}] {name}")
        else:
            self.stdout.write(self.style.WARNING("  (取得できませんでした)"))

        backends = [(n, getattr(cv2, f"CAP_{n}")) for n in BACKEND_NAMES if hasattr(cv2, f"CAP_{n}")]

        self.stdout.write("\n=== バックエンド別の開ける/映像が取れる ===")
        found = []
        for backend_name, backend in backends:
            self.stdout.write(f"\n-- {backend_name} --")
            for index in range(max_index):
                t0 = time.monotonic()
                cap = cv2.VideoCapture(index, backend)
                opened = cap.isOpened()
                shape = None
                saved = ""
                if opened:
                    for _ in range(3):
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            shape = frame.shape
                            if shots_dir is not None:
                                path = shots_dir / f"{backend_name}_{index}.jpg"
                                cv2.imwrite(str(path), frame)
                                saved = path.name
                            break
                        time.sleep(0.2)
                dt = time.monotonic() - t0
                cap.release()
                if shape:
                    found.append((backend_name, index, shape))
                    mark = self.style.SUCCESS("映像OK")
                elif opened:
                    mark = self.style.WARNING("開いたが映像なし")
                else:
                    mark = "開けない"
                self.stdout.write(
                    f"  index {index}: {mark}  {'' if shape is None else f'{shape[1]}x{shape[0]}'}  {dt:.2f}s  {saved}"
                )

        self.stdout.write("\n=== 結果 ===")
        if found:
            seen = set()
            for backend_name, index, shape in found:
                if index in seen:
                    continue
                seen.add(index)
                self.stdout.write(f"  使用可能: index {index} ({backend_name} / {shape[1]}x{shape[0]})")
            self.stdout.write(
                f"\n  映像を取できたインデックス数: {len({i for _b, i, _s in found})} / OS列挙 {len(names)} 台"
            )
            self.stdout.write(
                "  ※ バックエンドで割当順が違うため、検知・常時稼働は DSHOW→MSMF の順に試します。"
            )
        else:
            self.stdout.write(self.style.ERROR("  どのインデックスからも映像を取できませんでした(他アプリ使用中/プライバシー設定を確認)"))

        if options["simultaneous"]:
            self._measure_simultaneous(found, options["seconds"])

    def _measure_simultaneous(self, found, seconds):
        """検知できた全カメラを同時に開いて実効fpsを測る

        「1台ずつなら映るのに、アプリで2台同時にすると片方だけ動かない」は
        USB帯域/コントローラの制約が原因のことがある。ここで硬件側の実力を確認する。
        """
        import threading

        import cv2

        indices = sorted({index for _b, index, _s in found}, reverse=True)
        if len(indices) < 2:
            self.stdout.write("\n=== 同時ストリーム検査 ===")
            self.stdout.write("  カメラが1台しかないためスキップ")
            return

        self.stdout.write(f"\n=== 同時ストリーム検査({'+'.join(str(i) for i in indices)} を順に開放) ===")
        caps = {}
        for index in indices:
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                self.stdout.write(self.style.WARNING(f"  index {index}: 開けない"))
                continue
            deadline = time.monotonic() + 6
            frame = None
            while time.monotonic() < deadline:
                ok, frame = cap.read()
                if ok and frame is not None:
                    break
                time.sleep(0.05)
            if frame is None:
                cap.release()
                self.stdout.write(self.style.WARNING(f"  index {index}: 開いたが映像なし"))
                continue
            caps[index] = cap
            self.stdout.write(f"  index {index}: OK {frame.shape[1]}x{frame.shape[0]}")
            time.sleep(0.3)

        counts = {index: 0 for index in caps}
        stop = threading.Event()

        def worker(index, cap):
            while not stop.is_set():
                ok, frame = cap.read()
                if ok and frame is not None:
                    counts[index] += 1

        threads = [threading.Thread(target=worker, args=(i, c), daemon=True) for i, c in caps.items()]
        t0 = time.monotonic()
        for thread in threads:
            thread.start()
        time.sleep(seconds)
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        elapsed = time.monotonic() - t0

        working = 0
        for index in sorted(counts):
            fps = counts[index] / elapsed
            if fps >= 1.0:
                working += 1
                self.stdout.write(self.style.SUCCESS(f"  index {index}: {fps:.1f}fps"))
            else:
                self.stdout.write(self.style.ERROR(f"  index {index}: {fps:.1f}fps(実質映像なし)"))
        for cap in caps.values():
            cap.release()

        if working < len(caps):
            self.stdout.write(self.style.WARNING(
                f"  → {len(caps)}台中 {working}台しか同時に映像を出せません。"
                "USBコントローラ/ハブを分けて接続してください"
                "(アプリ側は settings.AI_CAMERA_PRIORITY で優先カメラを選べます)。"))
        else:
            self.stdout.write(self.style.SUCCESS("  → 全カメラ同時に映像が出ています。複数台運用OK"))
