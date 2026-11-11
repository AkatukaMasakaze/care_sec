"""学習済みモデルの検証(精度・混同行列・誤判定一覧をCSV単位で出す)

caregiver_safety/check_fall_model.py の移植。
**ai2 の運用閾値(AISettings.fall_threshold)でも評価する**のが元のスクリプトと違う点です
(実運用では 0.5 でなくこの閾値で判定されるため)。

使い方:
    python manage.py ai_check_model                       # 現行の転倒モデル
    python manage.py ai_check_model --path ai_models/candidates/fall_detection_lstm.keras
    python manage.py ai_check_model --threshold 0.77 --top 20
    python manage.py ai_check_model --model state
"""
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import training

FALL_MODEL = "fall_detection_lstm.keras"
STATE_MODEL = "state_detection_lstm.keras"


class Command(BaseCommand):
    help = "転倒/状態の学習済みモデルを検証する(誤判定の内訳をCSV単位で確認できる)"

    def add_arguments(self, parser):
        parser.add_argument("--model", choices=("fall", "state"), default="fall",
                            help="検証するモデルの種類(既定 fall)")
        parser.add_argument("--path", default=None,
                            help="モデルファイルのパス(既定は ai_models/ の現行版)")
        parser.add_argument("--threshold", type=float, default=None,
                            help="転倒と判定する確率の閾値(既定はAI設定の転倒検知信頼度閾値)")
        parser.add_argument("--top", type=int, default=25,
                            help="誤判定一覧の最大表示件数(既定 25)")

    def handle(self, *args, **options):
        import numpy as np
        from tensorflow.keras.models import load_model

        training.ensure_dirs()
        kind = options["model"]

        if options["path"]:
            model_path = Path(options["path"]) if os.path.isabs(options["path"]) \
                else training.model_dir().parent / options["path"]
        else:
            model_path = training.model_dir() / (FALL_MODEL if kind == "fall" else STATE_MODEL)
        if not model_path.exists():
            raise CommandError(f"モデルが見つかりません: {model_path}")

        threshold = options["threshold"]
        if threshold is None and kind == "fall":
            try:
                from core.models import AISettings

                threshold = float(AISettings.load().fall_threshold)
            except Exception:  # noqa: BLE001 -DBなしでも検証したい
                threshold = 0.5
        if threshold is None:
            threshold = 0.5

        self.stdout.write("=" * 60)
        self.stdout.write(f"{('転倒判定' if kind == 'fall' else '状態認識')}モデル検証")
        self.stdout.write("=" * 60)
        self.stdout.write(f"モデル   : {model_path}")
        self.stdout.write(f"CSV      : {training.data_dir()}")
        if kind == "fall":
            self.stdout.write(f"閾値     : {threshold:.2f}")
        self.stdout.write("")

        model = load_model(str(model_path))

        if kind == "fall":
            self._check_fall(model, threshold, options["top"])
        else:
            self._check_state(model, options["top"])

    # ---------------- 転倒 ----------------
    def _check_fall(self, model, threshold, top):
        import numpy as np

        X, y, rows, skipped = training.build_fall_dataset()
        if len(X) == 0:
            raise CommandError("検証用シーケンスが作れません(train_data/*.csv を確認してください)")

        x_path = training.dataset_path("X.npy")
        if x_path.exists():
            stored = np.load(x_path)
            if len(stored) != len(X):
                self.stdout.write(self.style.WARNING(
                    f"注意: train_dataset/X.npy({len(stored)}) と CSVから再構築した件数({len(X)})"
                    " が違います。ai_prepare_dataset の再実行を推奨します。"))
        for name, reason in skipped:
            self.stdout.write(self.style.WARNING(f"  [SKIP] {name} → {reason}"))

        self.stdout.write(f"検証対象: {len(X)} シーケンス")
        prob = model.predict(X, verbose=0).reshape(-1)
        pred = (prob >= threshold).astype(int)

        correct = int(np.sum(pred == y))
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write("結果")
        self.stdout.write("=" * 60)
        self.stdout.write(f"正解   : {correct} / {len(y)}  ({correct / len(y) * 100:.2f}%)")

        tn = int(np.sum((y == 0) & (pred == 0)))
        fp = int(np.sum((y == 0) & (pred == 1)))
        fn = int(np.sum((y == 1) & (pred == 0)))
        tp = int(np.sum((y == 1) & (pred == 1)))
        self.stdout.write("")
        self.stdout.write("混同行列")
        self.stdout.write(f"           予測NORMAL  予測FALL")
        self.stdout.write(f"実際NORMAL      {tn:5d}      {fp:5d}   ← 誤検知(アラート空振り)")
        self.stdout.write(f"実際FALL        {fn:5d}      {tp:5d}   ← 見逃し(危険)")
        if tn + fp:
            self.stdout.write(f"誤検知率(NORMALがFALL扱い): {fp / (tn + fp) * 100:.2f}%")
        if fn + tp:
            self.stdout.write(f"見逃し率  (FALLがNORMAL扱い): {fn / (fn + tp) * 100:.2f}%")

        # 1シーケンス分のフレーム数から誤判定の時刻手掛かりを出す
        stride = training.STRIDE
        errors = np.where(pred != y)[0]
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write(f"誤判定一覧({len(errors)}件 / 先頭 {top}件)")
        self.stdout.write("=" * 60)
        file_index = self._file_index(rows, stride)
        for i in errors[:top]:
            info = file_index[int(i)]
            actual = "NORMAL" if y[i] == 0 else "FALL"
            predicted = "NORMAL" if pred[i] == 0 else "FALL"
            self.stdout.write(
                f"  {info['file']:44s} seq={info['seq']:4d} "
                f"frames={info['start_frame']:5d}-{info['end_frame']:5d} "
                f"実際={actual:6s} 予測={predicted:6s} FALL確率={prob[i] * 100:6.2f}%"
            )

        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write("CSVごとの誤判定")
        self.stdout.write("=" * 60)
        for row in rows:
            idx = range(row["first"], row["first"] + row["sequences"])
            bad = [i for i in idx if pred[i] != y[i]]
            rate = len(bad) / row["sequences"] * 100 if row["sequences"] else 0
            mark = self.style.WARNING if bad else self.style.SUCCESS
            self.stdout.write(mark(
                f"  {row['file']:44s} {len(bad):4d}/{row['sequences']:4d} ({rate:5.1f}%) "
                f"{'NORMAL' if row['label'] == 0 else 'FALL'}"))

    # ---------------- 状態 ----------------
    def _check_state(self, model, top):
        import numpy as np

        X, y, rows, skipped = training.build_state_dataset()
        if len(X) == 0:
            raise CommandError("検証用シーケンスが作れません")
        labels = np.load(training.model_dir() / "state_labels.npy", allow_pickle=True)

        for name, reason in skipped:
            self.stdout.write(self.style.WARNING(f"  [SKIP] {name} → {reason}"))

        self.stdout.write(f"検証対象: {len(X)} シーケンス / クラス {len(labels)}")
        pred = np.argmax(model.predict(X, verbose=0), axis=1)
        correct = int(np.sum(pred == y))
        self.stdout.write("")
        self.stdout.write("=" * 60)
        self.stdout.write("結果")
        self.stdout.write("=" * 60)
        self.stdout.write(f"正解   : {correct} / {len(y)}  ({correct / len(y) * 100:.2f}%)")
        self.stdout.write("")
        self.stdout.write("クラス別:")
        for i, label in enumerate(labels):
            total = int(np.sum(y == i))
            if not total:
                continue
            hit = int(np.sum((y == i) & (pred == i)))
            self.stdout.write(f"  {str(label):10s}: {hit:4d}/{total:4d} ({hit / total * 100:5.1f}%)")

        errors = np.where(pred != y)[0]
        stride = training.STATE_STRIDE
        file_index = self._file_index(rows, stride)
        self.stdout.write("")
        self.stdout.write(f"誤判定一覧({len(errors)}件 / 先頭 {top}件)")
        for i in errors[:top]:
            info = file_index[int(i)]
            self.stdout.write(
                f"  {info['file']:44s} seq={info['seq']:4d} "
                f"実際={labels[y[i]]:8s} 予測={labels[pred[i]]:8s}")

    # ---------------- 補助 ----------------
    @staticmethod
    def _file_index(rows, stride):
        """X内の各行(シーケンス)→ 元CSVとフレーム範囲 の対応表を作る"""
        index = []
        for row in rows:
            for seq in range(row["sequences"]):
                start = seq * stride + 1
                index.append({
                    "file": row["file"],
                    "seq": seq + 1,
                    "start_frame": start,
                    "end_frame": start + training.SEQUENCE_LENGTH - 1,
                })
        return index
