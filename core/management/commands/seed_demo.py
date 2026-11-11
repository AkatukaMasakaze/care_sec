"""初期データ投入コマンド: python manage.py seed_demo [--reset]

ログインアカウント・カメラ台帳・利用者だけを投入します。

※ 以前あった「偽の異常検知・自動録画・解決報告」の投入は削除しました。
   異常検知は実AI(core/ai_engine.py)がカメラから検知したものだけが登録されます。
"""
from django.core.management.base import BaseCommand

from core.models import Camera, Resident, User


class Command(BaseCommand):
    help = "初期データ(アカウント/カメラ台帳/利用者)を投入する(--reset で既存を削除して再投入)"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="既存の台帳データを削除して再投入")

    def handle(self, *args, **options):
        if options["reset"]:
            Camera.objects.all().delete()
            Resident.objects.all().delete()

        # ---- 介護士ユーザー(権限機能なし) ----
        def make_user(username, last, first, password, is_admin=False):
            user, created = User.objects.get_or_create(
                username=username, defaults={"first_name": first, "last_name": last},
            )
            if created:
                user.set_password(password)
            user.is_admin = is_admin
            user.is_active = True
            user.save()
            return user

        make_user("staff1", "山田", "花子", "demo1234")
        make_user("staff2", "佐藤", "二郎", "demo1234")
        make_user("admin", "管理", "者", "admin1234", is_admin=True)

        # ---- 利用者 ----
        for name, room in [
            ("田中 義雄", "101"), ("鈴木 キヨ", "102"), ("高橋 ハル", "103"),
            ("伊藤 茂", "201"), ("渡辺 フミ", "202"), ("中村 清", "203"),
        ]:
            Resident.objects.get_or_create(name=name, defaults={"room_number": room})

        # ---- カメラ台帳 ----
        # stream_url は実運用のカメラ(USB/ネットワーク)に合わせて /admin/ から設定する。
        for name, location, online in [
            ("CAM-01", "1F 廊下", True),
            ("CAM-02", "1F 食堂", True),
            ("CAM-03", "2F 廊下", False),
            ("CAM-04", "1F トイレ前", True),
        ]:
            Camera.objects.get_or_create(
                name=name,
                defaults={"location": location, "is_online": online, "stream_url": ""},
            )

        self.stdout.write(self.style.SUCCESS(
            "初期データを投入しました。\n"
            "  ログイン: staff1 / demo1234 (山田花子)\n"
            "            staff2 / demo1234 (佐藤二郎)\n"
            "            admin  / admin1234 (管理者)\n"
            "  カメラ台帳: %d台 / 利用者: %d名\n"
            "  ※ 異常検知・録画は作っていません(実AIの検知のみが登録されます)" % (
                Camera.objects.count(), Resident.objects.count(),
            )
        ))
