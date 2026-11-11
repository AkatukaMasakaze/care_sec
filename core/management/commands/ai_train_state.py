"""状態認識LSTMの再学習(train_dataset/state_*.npy → ai_models/state_detection_lstm.keras)

caregiver_safety/train_state_lstm.py の移植版。**モデル定義は `core/training.py` に一本化**してあり、
サーバー起動時の自動再学習(`core/retraining.py`)と同じ処理を使います。
クラス不均衡へ class_weight(balanced)で対応します。

`--promote` では `state_detection_lstm.keras` と `state_labels.npy` を**セットで**反映します
(出力次元とラベルの並び順は必ず一致させる必要があるため)。

使い方:
    python manage.py ai_train_state
    python manage.py ai_train_state --epochs 40
    python manage.py ai_train_state --promote
"""
from django.core.management.base import BaseCommand, CommandError

from core import training


class Command(BaseCommand):
    help = "状態認識LSTMを再学習する(手動。通常は起動時に自動実行されます)"

    def add_arguments(self, parser):
        parser.add_argument("--epochs", type=int, default=50)
        parser.add_argument("--batch-size", type=int, default=16)
        parser.add_argument("--test-size", type=float, default=0.2)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--promote", action="store_true",
                            help="学習後に ai_models/ へ反映する(モデルとラベルを同時に)")

    def handle(self, *args, **options):
        import numpy as np

        training.ensure_dirs()
        paths = {name: training.dataset_path(name) for name in
                 ("state_X.npy", "state_y.npy", "state_labels.npy")}
        missing = [name for name, path in paths.items() if not path.exists()]
        if missing:
            raise CommandError(
                f"学習データがありません: {', '.join(missing)}。"
                "先に python manage.py ai_prepare_state_dataset を実行してください。"
            )

        X, y = np.load(paths["state_X.npy"]), np.load(paths["state_y.npy"])
        labels = np.load(paths["state_labels.npy"], allow_pickle=True)
        if len(X) == 0:
            raise CommandError("データセットが空です")

        self.stdout.write("=" * 30)
        self.stdout.write("状態認識LSTM 再学習")
        self.stdout.write("=" * 30)
        self.stdout.write(f"X : {X.shape}   クラス数 {len(labels)}")
        for i, label in enumerate(labels):
            self.stdout.write(f"  {i}: {label}: {int(np.sum(y == i))}")

        model, metrics, (X_val, y_val, pred) = training.train_state(
            X, y, len(labels),
            epochs=options["epochs"], batch_size=options["batch_size"],
            test_size=options["test_size"], seed=options["seed"],
        )
        self.stdout.write("")
        self.stdout.write(f"学習 {metrics['train_count']} / 検証 {metrics['test_count']}")
        self.stdout.write(f"検証 Loss     : {metrics['val_loss']:.4f}")
        self.stdout.write(f"検証 Accuracy : {metrics['val_accuracy'] * 100:.2f}%")
        self.stdout.write("クラス重み: " + ", ".join(
            f"{labels[int(cid)]}={w}" for cid, w in metrics["class_weight"].items()))

        # 混同行列(行=実際 / 列=予測)
        self.stdout.write("")
        self.stdout.write("混同行列")
        header = "実際\\予測".ljust(12) + "".join(str(label)[:4].ljust(7) for label in labels)
        self.stdout.write("  " + header)
        for i, row in enumerate(metrics["confusion"]):
            self.stdout.write("  " + str(labels[i])[:4].ljust(12) +
                              "".join(str(value).ljust(7) for value in row))

        model.save(training.candidate_dir() / training.STATE_MODEL_FILENAME)
        np.save(training.candidate_dir() / training.STATE_LABELS_FILENAME, labels)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"学習完了 / 候補: {training.candidate_dir() / training.STATE_MODEL_FILENAME}"))

        if options["promote"]:
            dst, backed_up = training.promote(training.STATE_MODEL_FILENAME)
            labels_dst, _ = training.promote(training.STATE_LABELS_FILENAME)
            self.stdout.write(self.style.SUCCESS(f"現行モデルへ反映しました: {dst}"))
            self.stdout.write(f"  ラベルも同時に反映: {labels_dst}")
            if backed_up:
                self.stdout.write(f"  旧モデルの退避先: {backed_up}")
            self.stdout.write(self.style.WARNING(
                "反映後は runserver を再起動してください(モデルは起動時にキャッシュされます)。"))
        else:
            self.stdout.write("反映するには: python manage.py ai_train_state --promote")
