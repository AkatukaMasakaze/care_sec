"""状態認識データセット作成(train_data/*.csv → state_X.npy, state_y.npy, state_labels.npy)

caregiver_safety/prepare_state_dataset.py の移植。
CSVファイル名 → 状態ラベルの対応表は `core/training.py` の `STATE_BY_SUFFIX` にあります。

使い方:
    python manage.py ai_prepare_state_dataset
    python manage.py ai_prepare_state_dataset --list-unmapped
"""
from django.core.management.base import BaseCommand

from core import training


class Command(BaseCommand):
    help = "収集したCSVから状態認識モデルの学習データ(state_X / state_y / state_labels)を作る"

    def add_arguments(self, parser):
        parser.add_argument("--data-dir", default=None,
                            help="CSV格納先(既定 settings.AI_TRAIN_DATA_DIR)")
        parser.add_argument("--list-unmapped", action="store_true",
                            help="状態の対応が未登録なCSVを列挙する(学習はしない)")
        parser.add_argument("--dry-run", action="store_true",
                            help="件数確認のみで保存しない")

    def handle(self, *args, **options):
        training.ensure_dirs()

        if options["list_unmapped"]:
            import os

            missing = [p for p in training.csv_files(options["data_dir"])
                       if training.state_label_from_filename(p) is None]
            self.stdout.write(f"状態の対応が未登録のCSV: {len(missing)}件")
            for path in missing:
                self.stdout.write(f"  {os.path.basename(path)}")
            if missing:
                self.stdout.write("")
                self.stdout.write("core/training.py の STATE_BY_SUFFIX へ追加する例:")
                for path in missing[:3]:
                    stem = os.path.splitext(os.path.basename(path))[0]
                    suffix = stem[len("normal_"):] if stem.startswith("normal_") else stem
                    self.stdout.write(f'    "{suffix}": "立っている",   # 実態に合う状態名へ変更')
            return

        X, y, rows, skipped = training.build_state_dataset(options["data_dir"])

        for row in rows:
            self.stdout.write(f"  [OK] {row['file']:44s} {row['state']:8s} "
                              f"{row['frames']:5d}f → {row['sequences']:4d} seq")
        for name, reason in skipped:
            self.stdout.write(self.style.WARNING(f"  [SKIP] {name} → {reason}"))

        if X.shape[0] == 0:
            self.stdout.write(self.style.ERROR("\nシーケンスが1つも作れませんでした"))
            return

        import numpy as np

        self.stdout.write("")
        self.stdout.write(f"X shape : {X.shape}")
        self.stdout.write(f"y shape : {y.shape}")
        self.stdout.write("クラス内訳:")
        for i, label in enumerate(training.STATE_LABELS):
            self.stdout.write(f"  {i}: {label}: {int(np.sum(y == i))}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run のため保存しませんでした"))
            return

        np.save(training.dataset_path("state_X.npy"), X)
        np.save(training.dataset_path("state_y.npy"), y)
        np.save(training.dataset_path("state_labels.npy"), np.array(training.STATE_LABELS))
        self.stdout.write(self.style.SUCCESS(
            f"\n保存完了: {training.dataset_dir()} へ state_X / state_y / state_labels"))
        self.stdout.write("次: python manage.py ai_train_state")
