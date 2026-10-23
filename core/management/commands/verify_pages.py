"""全ページの表示検証コマンド: python manage.py verify_pages"""
from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.utils import timezone

from core.models import Alert, AlertReport, Camera, Recording, User


class Command(BaseCommand):
    help = "全ページが200で描画できるか検証する"

    def handle(self, *args, **options):
        failures = []

        def check(method, url, expected, client, **kwargs):
            response = getattr(client, method)(url, **kwargs)
            ok = response.status_code == expected
            self.stdout.write(f"{'OK ' if ok else 'NG '} {method.upper():4} {url:45} -> {response.status_code}")
            if not ok:
                failures.append(url)
            return response

        anon = Client()
        check("get", "/accounts/login/", 200, anon)
        check("get", "/", 302, anon)  # 未ログインはログインへリダイレクト

        client = Client()
        client.force_login(User.objects.get(username="staff1"))
        for url in [
            "/", "/alerts/", "/alerts/?status=open", "/alerts/?status=responding",
            "/alerts/?status=resolved", "/recordings/", "/recordings/?kind=fall",
            "/accounts/profile/", "/api/alerts/",
        ]:
            check("get", url, 200, client)

        recording = Recording.objects.first()
        if recording:
            check("get", f"/recordings/{recording.id}/", 200, client)

        # ---- 対応→解決→報告 の一連フロー ----
        camera = Camera.objects.first()
        if camera:
            temp = Alert.objects.create(kind=Alert.Kind.FALL, camera=camera, occurred_at=timezone.now())
            check("post", f"/alerts/{temp.id}/respond/", 302, client)
            temp.refresh_from_db()
            assert temp.status == Alert.Status.RESPONDING and temp.responder.username == "staff1", "対応するフロー失敗"
            self.stdout.write("OK   対応するボタン -> 対応者=staff1 / 状態=対応中")

            response = check("post", f"/alerts/{temp.id}/resolve/", 302, client)
            assert f"alert={temp.id}" in response.headers.get("Location", ""), "解決->報告入力への遷移失敗"
            self.stdout.write("OK   解決ボタン -> プロフィール(報告フォーム)へ遷移")

            check("post", "/reports/create/", 302, client, data={
                "alert": temp.id, "resident": "", "physical_issue": "none",
                "injury_detail": "検証用", "action_taken": "検証用対応", "comment": "検証用コメント",
            })
            assert AlertReport.objects.filter(alert=temp).exists(), "報告保存失敗"
            self.stdout.write("OK   解決報告の保存")

            AlertReport.objects.filter(alert=temp).delete()
            temp.delete()

        check("post", "/accounts/logout/", 200, client)

        if failures:
            raise CommandError(f"検証失敗: {failures}")
        self.stdout.write(self.style.SUCCESS("全ページの検証に成功しました。"))
