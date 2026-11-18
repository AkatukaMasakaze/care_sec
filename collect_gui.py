# -*- coding: utf-8 -*-
r"""CARE SEC 学習データ収集GUI (Windows 11 / Tkinter)

ai2.0 の collect_data.py(コンソール操作)を、care_sec(ai2)の学習パイプラインに
**そのまま投入できる形式**で扱うGUIツール。

    起動:  .\.venv\Scripts\python.exe collect_gui.py
          (コンソールを出したくない場合は pythonw collect_gui.py)

書き出し仕様(core/management/commands/ai_collect_data.py と同一):
  - 保存先   : settings.AI_TRAIN_DATA_DIR(既定 train_data/)
  - 行       : 33ランドマーク ×(x,y,z)=99値 + ラベル + 人物番号
  - ファイル : normal_<動作名>.csv / fall_<動作名>.csv
              (2人以上検出された時だけ _personN サフィックスを付ける。
               1人ファイルはサフィックス無し = 状態モデルの
               STATE_BY_SUFFIX 対応が効く名前になる)
  - 収集後   : サーバー再起動で自動再学習が走る(指紋が変わるため)。
               GUI内の「再学習」ボタンでその場で実行もできる。

注意: AI常時稼働中の runserver はカメラを掴んでいるため、
      収集には使えません。サーバーを止めるか、別のindex/カメラを使ってください。
"""
import csv
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, str(BASE_DIR))

import django  # noqa: E402

django.setup()

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

from django.conf import settings  # noqa: E402

from core import training  # noqa: E402

# 収集仕様(ai2.0 collect_data.py / ai_collect_data.py と揃える)
MAX_POSES = 10
FRAME_MS = 33.33          # 学習データの時間刻み(30fps)と一致させる
MIN_USEFUL_FRAMES = 31    # 30フレーム窓を1つ作れないセッションは警告

# 骨格接続(描画のみ。ai2.0と同じ一覧)
CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8),
    (11, 12),
    (11, 13), (13, 15), (15, 17),
    (12, 14), (14, 16), (16, 18),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31),
    (24, 26), (26, 28), (28, 30), (30, 32),
]
PERSON_COLORS = [
    (0, 255, 0), (255, 150, 0), (0, 255, 255), (255, 0, 255), (255, 255, 0),
    (0, 128, 255), (255, 128, 0), (128, 255, 0), (0, 0, 255), (255, 0, 0),
]


class Collector(threading.Thread):
    """カメラ読取 + MediaPipe + CSV書き出しのワーカースレッド

    UIスレッドとは queue でだけやり取りする(Tk変数は触らない)。
    """

    def __init__(self, device, backend, ui_queue, on_error):
        super().__init__(daemon=True, name="collect-worker")
        self.device = device
        self.backend = backend
        self.ui_queue = ui_queue
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.record_event = threading.Event()
        self.label = "normal"
        self.action = ""
        # 記録中はセッションごとに writers を開き、停止時に閉じる
        self._writers = {}
        self._handles = {}
        self._session_frames = 0
        self._persons_seen = set()

    # --- 制御(UI側から) ---
    def start_recording(self, label, action):
        self.label = label
        self.action = action
        self._session_frames = 0
        self._persons_seen = set()
        self.record_event.set()

    def stop_recording(self):
        self.record_event.clear()
        saved = self._finalize_files()
        return {"frames": self._session_frames,
                "persons": sorted(self._persons_seen), "saved": saved}

    def stats(self):
        return self._session_frames, len(self._persons_seen)

    def _close_writers(self):
        info = {"frames": self._session_frames, "persons": sorted(self._persons_seen)}
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        self._writers.clear()
        return info

    def _writer_for(self, person_id):
        if person_id in self._writers:
            return self._writers[person_id]
        stem = "%s_%s" % (self.label, self.action)
        path = Path(str(training.data_dir())) / ("%s.csv" % stem)
        # ファイル名は停止時に人物数で確定するので、記録中は一時ファイルへ
        tmp = Path(str(training.data_dir())) / ("%s.__rec%d.tmp" % (stem, person_id))
        handle = open(tmp, "w", newline="", encoding="utf-8")
        writer = csv.writer(handle)
        self._handles[person_id] = handle
        self._writers[person_id] = writer
        return writer

    def _finalize_files(self):
        """停止時: 1人物なら `label_動作名.csv`、複数なら `_personN` を付けて確定"""
        saved = []
        multi = len(self._persons_seen) > 1
        data_dir = Path(str(training.data_dir()))
        for person_id, handle in self._handles.items():
            handle.flush()
        self._close_writers_raw()
        for person_id in sorted(self._persons_seen):
            tmp = data_dir / ("%s_%s.__rec%d.tmp" % (self.label, self.action, person_id))
            if not tmp.exists():
                continue
            if multi:
                final = data_dir / ("%s_%s_person%d.csv" % (self.label, self.action, person_id + 1))
            else:
                final = data_dir / ("%s_%s.csv" % (self.label, self.action))
            if final.exists():
                # 既存ファイルには絶対に上書きしない(学習データを壊さない)
                n = 2
                while True:
                    suffix = "_person%d" % (person_id + 1) if multi else ""
                    final = data_dir / ("%s_%s_%d%s.csv" % (
                        self.label, self.action, n, suffix))
                    if not final.exists():
                        break
                    n += 1
            tmp.replace(final)
            saved.append(final)
        return saved

    def _close_writers_raw(self):
        for handle in self._handles.values():
            try:
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        self._writers.clear()

    # --- メインループ ---
    def run(self):
        try:
            import cv2
        except Exception as exc:  # noqa: BLE001
            self.on_error("OpenCV が利用できません: %s" % exc)
            return

        backend = cv2.CAP_DSHOW if self.backend == "dshow" else cv2.CAP_MSMF
        cap = cv2.VideoCapture(self.device, backend)
        if not cap.isOpened():
            cap.release()
            self.on_error(
                "カメラ index %s(%s) を開けませんでした。\n\n"
                "・runserver(AI常時稼働)がカメラを占有していませんか?\n"
                "  → サーバーを止めるか、別のindex/カメラを選んでください\n"
                "・他のアプリ(Teams/カメラアプリ等)が使用中の可能性もあります"
                % (self.device, self.backend)
            )
            return

        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision

            pose_path = training.pose_model_path()
            if not pose_path.exists():
                self.on_error("Poseモデルが見つかりません: %s" % pose_path)
                return
            landmarker = vision.PoseLandmarker.create_from_options(
                vision.PoseLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_path=str(pose_path)),
                    running_mode=vision.RunningMode.VIDEO,
                    num_poses=MAX_POSES,
                )
            )
        except Exception as exc:  # noqa: BLE001
            cap.release()
            self.on_error("MediaPipe の初期化に失敗: %s: %s" % (type(exc).__name__, exc))
            return

        frame_count = 0
        try:
            while not self.stop_event.is_set():
                t0 = time.perf_counter()
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.02)
                    continue

                frame_count += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                    int(frame_count * FRAME_MS),
                )
                landmarks_list = result.pose_landmarks or []

                # CSV書き出し(記録中のときだけ。ai_collect_data と同一の行形式)
                if self.record_event.is_set():
                    try:
                        for person_id, landmarks in enumerate(landmarks_list):
                            row = []
                            for landmark in landmarks:
                                row.extend([landmark.x, landmark.y, landmark.z])
                            row.append(self.label)
                            row.append(person_id + 1)
                            self._writer_for(person_id).writerow(row)
                            self._persons_seen.add(person_id)
                            self._session_frames += 1
                    except ValueError:
                        # UI側がちょうど停止してファイルを閉じた直後( race は無害 )
                        pass

                # プレビュー描画(骨格+Person番号+REC表示)
                for person_id, landmarks in enumerate(landmarks_list):
                    color = PERSON_COLORS[person_id % len(PERSON_COLORS)]
                    for start, end in CONNECTIONS:
                        s = landmarks[start]
                        e = landmarks[end]
                        cv2.line(frame,
                                 (int(s.x * frame.shape[1]), int(s.y * frame.shape[0])),
                                 (int(e.x * frame.shape[1]), int(e.y * frame.shape[0])),
                                 color, 2)
                    nose = landmarks[0]
                    cv2.putText(frame, "Person %d" % (person_id + 1),
                                (int(nose.x * frame.shape[1]) - 40,
                                 int(nose.y * frame.shape[0]) - 16),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                status = "REC %d frames / %d persons" % (
                    self._session_frames, len(self._persons_seen)) \
                    if self.record_event.is_set() else "待機中(検出 %d人)" % len(landmarks_list)
                cv2.rectangle(frame, (8, 8), (330, 34), (0, 0, 0), -1)
                cv2.putText(frame, status, (14, 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (80, 80, 255) if self.record_event.is_set() else (200, 200, 200), 2)

                okj, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if okj:
                    try:
                        # 古いフレームは捨てて常に最新だけ(遅延蓄積防止)
                        if self.ui_queue.full():
                            self.ui_queue.get_nowait()
                        self.ui_queue.put(jpeg.tobytes(), block=False)
                    except queue.Empty:
                        pass

                # 30fps相当の間隔に整える(学習データの時間刻みと一致)
                delay = (1.0 / 30.0) - (time.perf_counter() - t0)
                if delay > 0:
                    time.sleep(delay)
        finally:
            self._close_writers_raw()
            cap.release()
            # landmarker は close しない(ai2実測: closeすると再作成が失敗することがある)


class App:
    def __init__(self, root):
        self.root = root
        root.title("CARE SEC 学習データ収集")
        root.geometry("1080x680")

        self.collector = None
        self.ui_queue = queue.Queue(maxsize=2)
        self.jpeg = None

        # ---- 左: プレビュー ----
        left = ttk.Frame(root)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.preview = tk.Label(left, background="#111", text="カメラ未接続")
        self.preview.pack(fill=tk.BOTH, expand=True)

        # ---- 右: コントロール ----
        right = ttk.Frame(root, width=340)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)

        conn = ttk.LabelFrame(right, text="接続")
        conn.pack(fill=tk.X, pady=4)
        ttk.Label(conn, text="カメラindex").grid(row=0, column=0, sticky="w")
        self.device_var = tk.StringVar(value="0")
        ttk.Spinbox(conn, from_=0, to=10, textvariable=self.device_var,
                   width=6).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(conn, text="backend").grid(row=1, column=0, sticky="w")
        self.backend_var = tk.StringVar(value="msmf")
        ttk.Combobox(conn, textvariable=self.backend_var, values=("msmf", "dshow"),
                     width=8, state="readonly").grid(row=1, column=1, sticky="w", padx=4)
        ttk.Button(conn, text="接続", command=self.connect).grid(row=0, column=2, rowspan=2, padx=6)
        ttk.Button(conn, text="切断", command=self.disconnect).grid(row=0, column=3, rowspan=2)

        lab = ttk.LabelFrame(right, text="ラベル")
        lab.pack(fill=tk.X, pady=4)
        self.label_var = tk.StringVar(value="normal")
        ttk.Radiobutton(lab, text="normal(通常動作)", variable=self.label_var,
                        value="normal").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(lab, text="fall(転倒動作)", variable=self.label_var,
                        value="fall").grid(row=1, column=0, sticky="w")
        ttk.Label(lab, text="動作名").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.action_var = tk.StringVar(value="しゃがんでいる")
        self.action_combo = ttk.Combobox(
            lab, textvariable=self.action_var, width=24,
            values=sorted(set(training.STATE_BY_SUFFIX.keys()) | {
                "しゃがみ込み崩れる", "倒れるうつ伏せ", "倒れる上向き",
                "前方向倒れる", "後ろ方向に倒れる", "横方向に倒れる",
                "転倒後起き上がれず横になる",
            }))
        self.action_combo.grid(row=3, column=0, sticky="we", padx=4)
        ttk.Label(lab, foreground="#666", justify=tk.LEFT, text=(
            "normalの動作名は状態モデルの対応表(STATE_BY_SUFFIX)に\n"
            "ある名前を選ぶと、状態LSTMの学習にもそのまま使えます。"
        )).grid(row=4, column=0, sticky="w")

        rec = ttk.LabelFrame(right, text="収集")
        rec.pack(fill=tk.X, pady=4)
        self.rec_btn = ttk.Button(rec, text="▶ 記録開始", command=self.toggle_record)
        self.rec_btn.grid(row=0, column=0, padx=4, pady=4)
        self.stat_var = tk.StringVar(value="待機中")
        ttk.Label(rec, textvariable=self.stat_var, wraplength=300,
                  justify=tk.LEFT).grid(row=1, column=0, sticky="w", padx=6)

        files = ttk.LabelFrame(right, text="train_data のCSV")
        files.pack(fill=tk.BOTH, expand=True, pady=4)
        self.file_list = tk.Listbox(files, height=8)
        self.file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)
        fbtn = ttk.Frame(files)
        fbtn.pack(side=tk.RIGHT, fill=tk.Y)
        ttk.Button(fbtn, text="更新", command=self.refresh_files).pack(pady=2)
        ttk.Button(fbtn, text="削除", command=self.delete_file).pack(pady=2)
        ttk.Button(fbtn, text="フォルダ", command=self.open_folder).pack(pady=2)

        retrain = ttk.LabelFrame(right, text="再学習")
        retrain.pack(fill=tk.X, pady=4)
        ttk.Button(retrain, text="状態確認", command=lambda: self.run_cmd(
            ["ai_retrain", "--status"])).grid(row=0, column=0, padx=4, pady=4)
        ttk.Button(retrain, text="今すぐ再学習", command=self.retrain_now).grid(row=0, column=1)
        ttk.Label(retrain, foreground="#666", justify=tk.LEFT, text=(
            "収集したCSVは次回サーバー起動の自動再学習でも反映されます。\n"
            "ここで学習すると候補→比較→(劣化がなければ)自動反映されます。"
        )).grid(row=1, column=0, columnspan=2, sticky="w", padx=4)

        self.log = tk.Text(right, height=6, state=tk.DISABLED, wrap=tk.WORD)
        self.log.pack(fill=tk.X, pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_files()
        self.poll()

    # ---------- プレビューpoll ----------
    def poll(self):
        try:
            while True:
                self.jpeg = self.ui_queue.get_nowait()
        except queue.Empty:
            pass
        if self.jpeg and self.collector and self.collector.is_alive():
            try:
                from PIL import Image, ImageTk
                import io
                img = Image.open(io.BytesIO(self.jpeg))
                img.thumbnail((760, 600))
                self._photo = ImageTk.PhotoImage(img)
                self.preview.configure(image=self._photo, text="")
            except Exception:  # noqa: BLE001
                pass
        # 統計更新
        if self.collector and self.collector.record_event.is_set():
            frames, persons = self.collector.stats()
            self.stat_var.set("記録中: %d フレーム / %d 人" % (frames, persons))
        self.root.after(60, self.poll)

    # ---------- 接続 ----------
    def connect(self):
        if self.collector and self.collector.is_alive():
            return
        try:
            device = int(self.device_var.get())
        except ValueError:
            messagebox.showerror("エラー", "カメラindexは数値で入力してください")
            return
        self.collector = Collector(device, self.backend_var.get(), self.ui_queue,
                                   self._worker_error)
        self.collector.start()
        self.log_write("カメラ index %d (%s) 接続" % (device, self.backend_var.get()))

    def _worker_error(self, message):
        self.root.after(0, lambda: messagebox.showerror("カメラ接続失敗", message))
        self.root.after(0, lambda: self.log_write("接続失敗: " + message.splitlines()[0]))

    def disconnect(self):
        if self.collector:
            if self.collector.record_event.is_set():
                saved = self.collector._finalize_files()
                self.log_write("記録を停止して保存: %s" % ", ".join(p.name for p in saved))
            self.collector.stop_event.set()
            self.collector.join(timeout=5)
            self.collector = None
        self.preview.configure(image="", text="カメラ未接続")
        self.stat_var.set("待機中")

    # ---------- 記録 ----------
    def toggle_record(self):
        if not self.collector or not self.collector.is_alive():
            messagebox.showwarning("未接続", "先に「接続」してください(サーバー起動中は占有で開けません)")
            return
        if self.collector.record_event.is_set():
            saved = self.collector.stop_recording()
            frames = saved["frames"]
            names = ", ".join(p.name for p in saved["saved"])
            self.rec_btn.configure(text="▶ 記録開始")
            self.stat_var.set("待機中")
            if frames < MIN_USEFUL_FRAMES:
                messagebox.showwarning(
                    "フレーム不足",
                    "このセッションは %d フレームしか記録できません。\n"
                    "30フレーム以上ないと学習に使えません(短すぎ/人物が映っていなかった可能性)。"
                    % frames)
            else:
                self.log_write("保存: %s(%d フレーム / %d 人)" % (
                    names or "train_data", frames, len(saved["persons"])))
            self.refresh_files()
        else:
            action = self.action_var.get().strip()
            if not action:
                messagebox.showwarning("動作名", "動作名を入力してください")
                return
            label = self.label_var.get()
            target = Path(str(training.data_dir())) / ("%s_%s.csv" % (label, action))
            if target.exists():
                if not messagebox.askyesno(
                        "既存のファイル",
                        "%s が既にあります。\n"
                        "「はい」= 別名(_2等)で新規作成\n「いいえ」= キャンセル" % target.name):
                    return
                n = 2
                while (Path(str(training.data_dir())) /
                       ("%s_%s_%d.csv" % (label, action, n))).exists():
                    n += 1
                self.action_var.set("%s_%d" % (action, n))
            self.collector.start_recording(label, self.action_var.get().strip())
            self.rec_btn.configure(text="■ 記録停止")
            self.stat_var.set("記録中: 0 フレーム")

    # ---------- ファイル一覧 ----------
    def refresh_files(self):
        self.file_list.delete(0, tk.END)
        data_dir = Path(str(training.data_dir()))
        if not data_dir.exists():
            return
        rows = 0
        for path in sorted(data_dir.glob("*.csv")):
            try:
                with open(path, encoding="utf-8") as fh:
                    n = sum(1 for _ in fh)
            except OSError:
                n = -1
            rows += max(0, n)
            self.file_list.insert(tk.END, "%-52s %6d フレーム" % (path.name[:52], n))
        self.log_write("train_data: %d 件 / 合計 %d フレーム" % (
            len(self.file_list.get(0, tk.END)), rows))

    def delete_file(self):
        sel = self.file_list.curselection()
        if not sel:
            return
        name = self.file_list.get(sel[0]).split()[0]
        if not messagebox.askyesno("削除確認", "%s を削除しますか?(取り消せません)" % name):
            return
        try:
            (Path(str(training.data_dir())) / name).unlink()
            self.log_write("削除: %s" % name)
        except OSError as exc:
            messagebox.showerror("削除失敗", str(exc))
        self.refresh_files()

    def open_folder(self):
        os.startfile(str(training.data_dir()))  # noqa: S606 (Windows前提のGUIツール)

    # ---------- 再学習 ----------
    def retrain_now(self):
        if not messagebox.askyesno(
                "再学習",
                "収集CSVから再学習しますか?\n"
                "・数分CPUをフルに使います(AIサーバーは止めてください)\n"
                "・現行モデルより劣化していれば自動で反映は見送られます"):
            return
        self.run_cmd(["ai_retrain", "--force"])

    def run_cmd(self, args):
        def worker():
            cmd = [sys.executable, str(BASE_DIR / "manage.py")] + args
            proc = subprocess.Popen(
                cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                self.root.after(0, lambda s=line.rstrip(): self.log_write(s))
            proc.wait()
            self.root.after(0, lambda: self.log_write(
                "→ 完了(終了コード %s)%s" % (
                    proc.returncode,
                    " · サーバーを再起動すると新モデルが読み込まれます"
                    if args[:1] == ["ai_retrain"] and proc.returncode == 0 else "")))
        threading.Thread(target=worker, daemon=True, name="retrain-runner").start()

    # ---------- ログ/終了 ----------
    def log_write(self, text):
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def on_close(self):
        try:
            if self.collector:
                if self.collector.record_event.is_set():
                    self.collector._finalize_files()
                self.collector.stop_event.set()
        finally:
            self.root.destroy()


def main():
    training.ensure_dirs()
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")  # Windows 11 の標準らしい見た目に
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
