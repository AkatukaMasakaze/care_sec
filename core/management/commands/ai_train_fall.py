"""転倒判定LSTMの再学習(train_dataset/X.npy,y.npy → ai_models/fall_detection_lstm.keras)

caregiver_safety/train_lstm.py の移植版。**モデル定義は `core/training.py` に一本化**してあり、
サーバー起動時の自動再学習(`core/retraining.py`)と同じ処理を使います。

安全策として既定では `ai_models/candidates/` へ書き出し、`--promote` で現行へ反映します
(反映前の現行モデルは `ai_models/backup/<日時>/` へ自動退避)。

使い方:
    python manage.py ai_train_fall
    python manage.py ai_train_fall --epochs 30
    python manage.py ai_train_fall --promote
"""
from django.core.management.base import BaseCommand, CommandError

from core import training


class Command(BaseCommand):
    help = "転倒判定LSTMを再学習する(手動。通常は起動時に自動実行されます)"

    def add_arguments(self, parser):
        parser.add_argument("--epochs", type=int, default=50)
        parser.add_argument("--batch-size", type=int, default=16)
        parser.add_argument("--test-size", type=float, default=0.2)
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--out", default=training.FALL_MODEL_FILENAME,
                            help=f"出力ファイル名(既定 {training.FALL_MODEL_FILENAME})")
        parser.add_argument("--promote", action="store_true",
                            help="学習後に ai_models/ へ反映する(旧モデルはbackupへ退避)")

    def handle(self, *args, **options):
        import numpy as np

        training.ensure_dirs()
        x_path, y_path = training.dataset_path("X.npy"), training.dataset_path("y.npy")
        if not x_path.exists() or not y_path.exists():
            raise CommandError(
                f"{x_path.name} / {y_path.name} がありません。"
                "先に python manage.py ai_prepare_dataset を実行してください。"
            )

        X, y = np.load(x_path), np.load(y_path)
        if len(X) == 0:
            raise CommandError("データセットが空です(ai_prepare_dataset を再実行してください)")

        self.stdout.write("=" * 30)
        self.stdout.write("転倒判定LSTM 再学習")
        self.stdout.write("=" * 30)
        self.stdout.write(f"X : {X.shape}   NORMAL {int(np.sum(y == 0))} / FALL {int(np.sum(y == 1))}")

        model, metrics, (X_test, y_test, _prob) = training.train_fall(
            X, y,
            epochs=options["epochs"], batch_size=options["batch_size"],
            test_size=options["test_size"], seed=options["seed"],
        )
        self.stdout.write("")
        self.stdout.write(f"学習 {metrics['train_count']} / 検証 {metrics['test_count']}")
        self.stdout.write(f"検証 Loss     : {metrics['val_loss']:.4f}")
        self.stdout.write(f"検証 Accuracy : {metrics['val_accuracy']:.4f}")
        tn, fp = metrics["confusion"][0]
        fn, tp = metrics["confusion"][1]
        self.stdout.write(f"混同行列(0.5) : NORMAL→NORMAL {tn} / NORMAL→FALL {fp} / "
                          f"FALL→NORMAL {fn} / FALL→FALL {tp}")

        try:
            from core.models import AISettings

            threshold = float(AISettings.load().fall_threshold)
        except Exception:  # noqa: BLE001 -DBが使えない環境でも学習は続けたい
            threshold = 0.5
        at = training.evaluate_fall_at_threshold(model, X_test, y_test, threshold)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"AI設定の閾値 {threshold:.2f} / 正解率 {at['accuracy'] * 100:.2f}% / "
            f"誤検知率 {at['false_positive_rate'] * 100:.2f}% / "
            f"見逃し率 {at['miss_rate'] * 100:.2f}%"))

        model.save(training.candidate_dir() / options["out"])
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"学習完了 / 候補モデル: {training.candidate_dir() / options['out']}"))

        if options["promote"]:
            dst, backed_up = training.promote(options["out"])
            self.stdout.write(self.style.SUCCESS(f"現行モデルへ反映しました: {dst}"))
            if backed_up:
                self.stdout.write(f"  旧モデルの退避先: {backed_up}")
            self.stdout.write(self.style.WARNING(
                "反映後は runserver を再起動してください(モデルは起動時にキャッシュされます)。"))
        else:
            self.stdout.write("検証して問題なければ反映してください: "
                              "python manage.py ai_check_model --path ai_models/candidates/"
                              f"{options['out']}")
