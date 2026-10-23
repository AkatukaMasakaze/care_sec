"""検証用デモデータ投入コマンド: python manage.py seed_demo [--reset]"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Alert, AlertReport, Camera, Recording, Resident, User

VIDEO_URL = "https://www.w3schools.com/html/mov_bbb.mp4"  # 検証用サンプル動画


class Command(BaseCommand):
    help = "検証用デモデータを投入する(--reset で既存データを削除して再投入)"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="既存データを削除して再投入")

    def handle(self, *args, **options):
        if options["reset"]:
            AlertReport.objects.all().delete()
            Recording.objects.all().delete()
            Alert.objects.all().delete()
            Camera.objects.all().delete()
            Resident.objects.all().delete()

        if Camera.objects.exists():
            self.stdout.write(self.style.WARNING("デモデータは投入済みです。再投入は --reset を付けてください。"))
            return

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

        staff1 = make_user("staff1", "山田", "花子", "demo1234")
        staff2 = make_user("staff2", "佐藤", "二郎", "demo1234")
        make_user("admin", "管理", "者", "admin1234", is_admin=True)

        # ---- 利用者 ----
        residents = [
            Resident.objects.create(name=name, room_number=room)
            for name, room in [
                ("田中 義雄", "101"), ("鈴木 キヨ", "102"), ("高橋 ハル", "103"),
                ("伊藤 茂", "201"), ("渡辺 フミ", "202"), ("中村 清", "203"),
            ]
        ]

        # ---- カメラ ----
        cameras = []
        for name, location, online in [
            ("CAM-01", "1F 廊下", True),
            ("CAM-02", "1F 食堂", True),
            ("CAM-03", "2F 廊下", False),
            ("CAM-04", "1F トイレ前", True),
        ]:
            camera = Camera.objects.create(name=name, location=location, is_online=online)
            cameras.append(camera)
        cam1, cam2, cam3, cam4 = cameras

        now = timezone.now()
        thumb_fall = settings.STATIC_URL + "img/thumb_fall.svg"
        thumb_suspicious = settings.STATIC_URL + "img/thumb_suspicious.svg"

        def make_alert(kind, status, camera, delta, responder=None, thumb="", recording=None):
            alert = Alert.objects.create(
                kind=kind, status=status, camera=camera,
                occurred_at=now - delta, thumbnail_url=thumb, responder=responder,
            )
            if recording:
                Recording.objects.create(
                    kind=recording["kind"], camera=camera, alert=alert,
                    started_at=alert.occurred_at - timedelta(seconds=10),
                    ended_at=alert.occurred_at + timedelta(seconds=recording["after"]),
                    video_url=VIDEO_URL, thumbnail_url=thumb,
                    reason="AIが異常を検知したため自動録画",
                    markers=recording["markers"],
                )
            return alert

        # ---- 異常検知 + 自動録画 ----
        a1 = make_alert(
            Alert.Kind.FALL, Alert.Status.OPEN, cam1, timedelta(minutes=25),
            thumb=thumb_fall,
            recording={"kind": Recording.Kind.FALL, "after": 35, "markers": [
                {"seconds": 5, "label": "あやしい動き(ふらつき)"},
                {"seconds": 18, "label": "転倒の瞬間"},
            ]},
        )
        a2 = make_alert(
            Alert.Kind.SUSPICIOUS, Alert.Status.RESPONDING, cam2, timedelta(hours=2),
            responder=staff2, thumb=thumb_suspicious,
            recording={"kind": Recording.Kind.SUSPICIOUS, "after": 40, "markers": [
                {"seconds": 8, "label": "あやしい動き(長時間うずくまり)"},
            ]},
        )
        a3 = make_alert(
            Alert.Kind.FALL, Alert.Status.RESOLVED, cam4, timedelta(days=1, hours=3),
            responder=staff1, thumb=thumb_fall,
            recording={"kind": Recording.Kind.FALL, "after": 30, "markers": [
                {"seconds": 12, "label": "転倒の瞬間"},
            ]},
        )
        a4 = make_alert(
            Alert.Kind.ZONE, Alert.Status.RESOLVED, cam3, timedelta(days=2, hours=5),
            responder=staff2, thumb=thumb_suspicious,
            recording={"kind": Recording.Kind.SUSPICIOUS, "after": 25, "markers": [
                {"seconds": 6, "label": "立ち入り禁止ゾーン侵入"},
            ]},
        )
        # グラフ用の過去データ
        make_alert(Alert.Kind.FALL, Alert.Status.RESOLVED, cam1, timedelta(days=3, hours=1), responder=staff1)
        make_alert(Alert.Kind.SUSPICIOUS, Alert.Status.RESOLVED, cam2, timedelta(days=3, hours=6), responder=staff2)
        make_alert(Alert.Kind.FALL, Alert.Status.RESOLVED, cam4, timedelta(days=5, hours=2), responder=staff1)
        make_alert(Alert.Kind.ZONE, Alert.Status.RESOLVED, cam3, timedelta(days=6, hours=4), responder=staff2)

        # ---- 解決報告 ----
        AlertReport.objects.create(
            alert=a3, author=staff1, resident=residents[0],
            physical_issue=AlertReport.PhysicalIssue.NONE,
            injury_detail="打撲なし・痛み訴えなし",
            action_taken="起こし介助、バイタル測定、看護師へ連絡",
            comment="靴が滑りやすかったため、滑り止め靴へ交換済み",
        )
        AlertReport.objects.create(
            alert=a4, author=staff2, resident=residents[1],
            physical_issue=AlertReport.PhysicalIssue.SUSPECTED,
            injury_detail="軽度の擦り傷(右肘)",
            action_taken="消毒処置、家族へ報告",
            comment="夜間の徘徊傾向あり。見回りを強化中",
        )

        self.stdout.write(self.style.SUCCESS(
            "デモデータ投入完了。\n"
            "  ログイン: staff1 / demo1234 (山田花子)\n"
            "            staff2 / demo1234 (佐藤二郎)\n"
            "            admin  / admin1234 (管理者)"
        ))
