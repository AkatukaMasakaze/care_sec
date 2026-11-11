"""学習用データの収集(カメラ → MediaPipe Pose → train_data/*.csv)

caregiver_safety/collect_data.py の移植。
転倒/通常動作を収集して `train_data/` にCSVを追加し、
`ai_prepare_dataset` → `ai_train_fall` で追加学習できます。

使い方:
    python manage.py ai_collect_data                       # 窓キーで 1:normal 2:fall s:停止 q:終了
    python manage.py ai_collect_data --label fall --name 椅子から転落
    python manage.py ai_collect_data --device 1 --seconds 60 --no-gui

注意:
- **runserver を止めてから実行してください**(AI常時稼働がカメラを掴んでいると開けません)
- 保存CSVは 33ランドマーク×(x,y,z)=99列 + ラベル + 人物番号
"""
import csv
import os
import time

from django.core.management.base import BaseCommand, CommandError

from core import training

# 骨格描画用の接続(caregiver_safety/collect_data.py と同じ)
CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (11, 12),
    (11, 13), (13, 15), (15, 17),
    (12, 14), (14, 16), (16, 18),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31),
    (24, 26), (26, 28), (28, 30), (30, 32),
]

MAX_POSES = 10


class Command(BaseCommand):
    help = "カメラから姿勢データを収集して train_data/ にCSVを追加する(追加学習の材料作り)"

    def add_arguments(self, parser):
        parser.add_argument("--device", type=int, default=0, help="カメラインデックス(既定 0)")
        parser.add_argument("--backend", choices=("dshow", "msmf"), default="dshow",
                            help="OpenCVバックエンド(既定 dshow。このPCはdshowの方が速い)")
        parser.add_argument("--label", choices=("normal", "fall"), default=None,
                            help="開始時から収集するラベル(指定しないと窓キー 1/2 で切替)")
        parser.add_argument("--name", default="",
                            help="行動名(ファイル名の接頭辞に使う。状態学習の対応付けに便利)")
        parser.add_argument("--seconds", type=float, default=0,
                            help="この秒数で自動終了(0 = 手動終了まで継続)")
        parser.add_argument("--no-gui", action="store_true",
                            help="窓を出さずに収集のみ(Ctrl+C で終了。--label 必須)")

    def handle(self, *args, **options):
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"OpenCV が利用できません: {exc}")

        if options["no_gui"] and not options["label"]:
            raise CommandError("--no-gui では --label normal|fall の指定が必要です")

        pose_path = training.pose_model_path()
        if not pose_path.exists():
            raise CommandError(f"Poseモデルが見つかりません: {pose_path}")

        training.ensure_dirs()
        backend = cv2.CAP_DSHOW if options["backend"] == "dshow" else cv2.CAP_MSMF
        cap = cv2.VideoCapture(options["device"], backend)
        if not cap.isOpened():
            raise CommandError(
                f"カメラ index {options['device']}({options['backend']})を開けませんでした。\n"
                "  ・runserver(AI常時稼働)がカメラを掴んでいないか確認\n"
                "  ・python manage.py list_cameras で使えるindexを確認"
            )

        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        landmarker = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(pose_path)),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=MAX_POSES,
            )
        )

        current_label = options["label"]
        files, writers = {}, {}
        frame_count = 0
        limit_until = time.monotonic() + options["seconds"] if options["seconds"] else None
        written_rows = 0

        self.stdout.write("=" * 52)
        self.stdout.write("介護安全監視 / データ収集")
        self.stdout.write("=" * 52)
        self.stdout.write(f"保存先   : {training.data_dir()}")
        self.stdout.write(f"カメラ   : index {options['device']} ({options['backend']})")
        self.stdout.write(f"ラベル   : {current_label or '未選択(窓で 1:normal / 2:fall)'}")
        if not options["no_gui"]:
            self.stdout.write("窓キー   : 1=normal 2=fall s=停止 q=終了")
        else:
            self.stdout.write("終了     : Ctrl+C")
        self.stdout.write("")

        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.stdout.write(self.style.WARNING("映像を取得できませんでしたが、少し待って再試行します"))
                    time.sleep(0.2)
                    if limit_until and time.monotonic() > limit_until:
                        break
                    continue

                frame_count += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    int(frame_count * 33.33),
                )

                landmarks_list = result.pose_landmarks or []
                if current_label:
                    for person_id, landmarks in enumerate(landmarks_list):
                        row = []
                        for landmark in landmarks:
                            row.extend([landmark.x, landmark.y, landmark.z])
                        row.append(current_label)
                        row.append(person_id + 1)
                        writer = self._writer_for(files, writers, current_label, person_id,
                                                  options["name"])
                        writer.writerow(row)
                        written_rows += 1

                if not options["no_gui"]:
                    for landmarks in landmarks_list:
                        for start, end in CONNECTIONS:
                            a, b = landmarks[start], landmarks[end]
                            cv2.line(frame,
                                     (int(a.x * frame.shape[1]), int(a.y * frame.shape[0])),
                                     (int(b.x * frame.shape[1]), int(b.y * frame.shape[0])),
                                     (0, 255, 0), 2)
                        for landmark in landmarks:
                            cv2.circle(frame, (int(landmark.x * frame.shape[1]),
                                               int(landmark.y * frame.shape[0])), 4, (0, 255, 0), -1)
                    cv2.putText(frame, f"People: {len(landmarks_list)}", (20, 40),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                    cv2.putText(frame, f"Collecting: {(current_label or 'NONE').upper()}", (20, 75),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(frame, "1:Normal 2:Fall S:Stop Q:Quit", (20, 110),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                    cv2.imshow("Data Collection", frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("1"):
                        self._close(files)
                        current_label = "normal"
                        self.stdout.write(">>> NORMAL の収集を開始")
                    elif key == ord("2"):
                        self._close(files)
                        current_label = "fall"
                        self.stdout.write(">>> FALL の収集を開始")
                    elif key == ord("s"):
                        self._close(files)
                        current_label = None
                        self.stdout.write(">>> 収集を停止")
                    elif key == ord("q"):
                        break
                else:
                    time.sleep(1.0 / 30)

                if limit_until and time.monotonic() > limit_until:
                    break
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nCtrl+C で終了します"))
        finally:
            self._close(files)
            cap.release()
            landmarker.close()          # このプロセスだけで使うlandmarkerなので閉じてよい
            if not options["no_gui"]:
                cv2.destroyAllWindows()

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"収集を終了しました: {written_rows} 行 / {frame_count} フレーム"))
        self.stdout.write(f"保存先: {training.data_dir()}")
        self.stdout.write("次に: python manage.py ai_prepare_dataset && python manage.py ai_train_fall")

    # ---------------- 補助 ----------------
    def _writer_for(self, files, writers, label, person_id, name):
        """人物・ラベル単位のCSVライタ(同じファイルへ追記していく)"""
        if person_id in writers:
            return writers[person_id]
        stem = f"{label}_{name}_person{person_id + 1}" if name else f"{label}_person{person_id + 1}"
        path = os.path.join(str(training.data_dir()), f"{stem}.csv")
        handle = open(path, "a", newline="", encoding="utf-8")
        writers[person_id] = csv.writer(handle)
        files[person_id] = handle
        return writers[person_id]

    @staticmethod
    def _close(files):
        for handle in files.values():
            handle.close()
        files.clear()
