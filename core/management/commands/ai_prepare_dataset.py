"""学習用データセット作成(train_data/*.csv → train_dataset/X.npy, y.npy)

caregiver_safety/prepare_dataset.py の移植。

使い方:
    python manage.py ai_prepare_dataset
    python manage.py ai_prepare_dataset --stride 5 --dry-run
"""
from django.core.management.base import BaseCommand

from core import training


class Command(BaseCommand):
    help = "収集したCSV(train_data/)から転倒判定モデルの学習データ(X.npy / y.npy)を作る"

    def add_arguments(self, parser):
        parser.add_argument("--stride", type=int, default=training.STRIDE,
                            help="シーケンスのずらし幅(既定 5)")
        parser.add_argument("--data-dir", default=None,
                            help="CSV格納先(既定 settings.AI_TRAIN_DATA_DIR)")
        parser.add_argument("--dry-run", action="store_true",
                            help="件数確認のみで保存しない")

    def handle(self, *args, **options):
        training.ensure_dirs()
        stride = options["stride"]

        self.stdout.write("=" * 60)
        self.stdout.write("転倒判定データセット作成(スライディングウィンドウ)")
        self.stdout.write("=" * 60)
        self.stdout.write(f"CSV格納先   : {options['data_dir'] or training.data_dir()}")
        self.stdout.write(f"保存先      : {training.dataset_dir()}")
        self.stdout.write(f"シーケンス長: {training.SEQUENCE_LENGTH}")
        self.stdout.write(f"特徴量数    : {training.FEATURES}")
        self.stdout.write(f"STRIDE      : {stride}")
        self.stdout.write("")

        X, y, rows, skipped = training.build_fall_dataset(options["data_dir"], stride=stride)

        for row in rows:
            label = "NORMAL" if row["label"] == 0 else "FALL"
            self.stdout.write(f"  [OK] {row['file']:44s} {label:6s} "
                              f"{row['frames']:5d}f → {row['sequences']:4d} seq")
        for name, reason in skipped:
            self.stdout.write(self.style.WARNING(f"  [SKIP] {name} → {reason}"))

        if X.shape[0] == 0:
            self.stdout.write(self.style.ERROR(
                "\nシーケンスが1つも作れませんでした。CSVの中身(数値列が99列以上か)を確認してください。"))
            return

        import numpy as np

        normal = int(np.sum(y == 0))
        fall = int(np.sum(y == 1))
        self.stdout.write("")
        self.stdout.write(f"X shape : {X.shape}")
        self.stdout.write(f"NORMAL  : {normal}")
        self.stdout.write(f"FALL    : {fall}  (比率 {fall / len(y) * 100:.1f}%)")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run のため保存しませんでした"))
            return

        np.save(training.dataset_path("X.npy"), X)
        np.save(training.dataset_path("y.npy"), y)
        self.stdout.write(self.style.SUCCESS(
            f"\n保存完了: {training.dataset_path('X.npy')} / {training.dataset_path('y.npy')}"))
        self.stdout.write("次: python manage.py ai_train_fall")
