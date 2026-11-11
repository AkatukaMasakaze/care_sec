"""自動再学習の手動実行 / 状態確認

サーバー起動時に自動で走る処理(`core/retraining.py`)を、その場で実行・確認するためのコマンド。
通常は **起動時に自動**なので、これは検証・障害対応用です。

使い方:
    python manage.py ai_retrain --status      # 指紋と前回実行情報を表示
    python manage.py ai_retrain               # 変更があれば再学習
    python manage.py ai_retrain --force       # 変更有無に関係なく再学習
"""
from django.core.management.base import BaseCommand

from core import retraining, training


class Command(BaseCommand):
    help = "サーバー起動時の自動再学習と同じ処理を実行する(または状態を確認する)"

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true",
                            help="データ指紋・実行条件・前回結果を表示するだけ")
        parser.add_argument("--force", action="store_true",
                            help="指紋が変わっていなくても再学習を実行")

    def handle(self, *args, **options):
        need, finger, reason = retraining.needs_retrain()

        if options["status"]:
            self.stdout.write(f"自動再学習   : {'有効' if retraining.enabled() else '無効'}"
                              " (settings.AI_AUTO_RETRAIN)")
            self.stdout.write(f"収集CSV      : {finger['files']} ファイル / "
                              f"{round(finger['bytes'] / 1048576, 1)} MB")
            self.stdout.write(f"現在の指紋   : {finger['hash']}")
            state = training.read_state()
            self.stdout.write(f"記録された指紋: {state.get('fingerprint', '(なし)')}")
            self.stdout.write(f"実行が必要か : {'はい' if need else 'いいえ'}  ({reason})")
            if state:
                self.stdout.write("")
                self.stdout.write(f"前回: {state.get('started_at')} → {state.get('finished_at')}"
                                  f" / {state.get('elapsed_seconds')}秒")
                for key in ("fall", "state"):
                    block = state.get(key)
                    if not block:
                        continue
                    self.stdout.write(f"  {key}: sequences={block.get('sequences')} "
                                      f"promoted={block.get('promoted')} "
                                      f"val_accuracy={block.get('val_accuracy')}")
                if state.get("error"):
                    self.stdout.write(self.style.ERROR(f"  error: {state['error']}"))
            return

        ran = retraining.run_startup_retraining(force=options["force"])
        if not ran:
            self.stdout.write(self.style.SUCCESS(f"再学習は不要です({reason})"))
            return
        self.stdout.write(self.style.SUCCESS("再学習処理が完了しました(詳細は上のログ)"))
        self.stdout.write("反映済みなら runserver 再起動で新モデルが使われます(起動時キャッシュ)。")
