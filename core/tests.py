# -*- coding: utf-8 -*-
"""プロフィール/報告のページ分離のテスト

設計(2026-11-17):
  - プロフィール(`/accounts/profile/`)は自分の情報+編集+統計+履歴だけ。報告フォームは置かない
  - 報告は入力専用ページ `/reports/<異常ID>/` だけに表示される(入口は異常詳細)
  - 報告保存後は異常詳細へ戻る
"""
from django.test import TestCase
from django.utils import timezone

from core.models import Alert, AlertReport, Camera, Resident, User, UserProfile


class ProfileAndReportSplitTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.nurse = User.objects.create_user("nurse_a", password="pw-verify-1")
        cls.other = User.objects.create_user("nurse_b", password="pw-verify-1")
        cls.camera = Camera.objects.create(name="テストカメラ", location="棟A")
        cls.alert = Alert.objects.create(
            kind=Alert.Kind.FALL,
            status=Alert.Status.RESOLVED,
            camera=cls.camera,
            occurred_at=timezone.now(),
            resolved_at=timezone.now(),
            responder=cls.nurse,
        )

    # ---------- プロフィール ----------
    def test_profile_shows_history_not_report_form(self):
        """プロフィールは200。報告フォーム(providers: includes/report_form)を含まない"""
        self.client.force_login(self.nurse)
        res = self.client.get("/accounts/profile/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("プロフィール", body)
        self.assertIn("これまでに対応した異常", body)           # 履歴
        self.assertIn("報告を入力", body)                       # 専用ページへ飛ぶCTAだけ
        self.assertIn("提出した解決報告", body)                 # 一覧(読む専用)
        self.assertNotIn('action="/reports/create/"', body)     # 報告フォームは置いていない
        # 編集は別ページ方式(画面内パネルはスクロールを壊すため廃止)
        self.assertIn('href="/accounts/profile/edit/"', body)   # 編集ページへのリンクだけ
        self.assertNotIn('id="profile-edit"', body)
        self.assertNotIn('name="last_name"', body)              # 編集フォーム自体は载せない

    def test_onepage_layout_only_on_dashboard(self):
        """「1画面固定」はダッシュボードだけ。他のページで外れるとスクロール不能になる
        (プロフィールの編集パネルで実測したバグの再発防止)"""
        self.client.force_login(self.nurse)
        res = self.client.get("/")
        self.assertIn('class="main main--onepage', res.content.decode("utf-8"))
        for path in ("/accounts/profile/", "/alerts/", "/recordings/"):
            res = self.client.get(path)
            body = res.content.decode("utf-8")
            self.assertNotIn("main--onepage", body, path + " が1画面固定になっている")

    def test_profile_edit_saves_user_and_profile(self):
        self.client.force_login(self.nurse)
        res = self.client.post("/accounts/profile/update/", {
            "last_name": "山田", "first_name": "太郎",
            "email": "taro@example.com",
            "facility": "ひまわり介護棟", "job_title": "リーダー",
            "phone": "090-0000-1111",
        })
        self.assertRedirects(res, "/accounts/profile/")
        self.nurse.refresh_from_db()
        prof = UserProfile.for_user(self.nurse)
        self.assertEqual(self.nurse.last_name, "山田")
        self.assertEqual(self.nurse.email, "taro@example.com")
        self.assertEqual(prof.facility, "ひまわり介護棟")
        self.assertEqual(prof.job_title, "リーダー")

    def test_profile_edit_invalid_email_stays_on_edit_page(self):
        """メール形式エラーは編集ページに留まり(リダイレクトしない)、他は保存される"""
        self.client.force_login(self.nurse)
        res = self.client.post("/accounts/profile/update/", {
            "last_name": "山田", "first_name": "次郎", "email": "not-an-email",
            "facility": "テスト施設", "job_title": "介護スタッフ", "phone": "0800",
        })
        self.assertTemplateUsed(res, "accounts/profile_edit.html")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "メールアドレスの形が正しくありません")
        self.nurse.refresh_from_db()
        self.assertEqual(self.nurse.last_name, "山田")       # 他は保存されている
        self.assertEqual(self.nurse.email, "")                # 不正メールは入らない

    def test_profile_edit_page(self):
        """編集は独立ページ(GET表示 / 未ログイン302 / 保存後はプロフィールへ)"""
        res = self.client.get("/accounts/profile/edit/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

        self.client.force_login(self.nurse)
        res = self.client.get("/accounts/profile/edit/")
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "accounts/profile_edit.html")
        body = res.content.decode("utf-8")
        self.assertIn('action="/accounts/profile/update/"', body)   # POST先
        self.assertIn('href="/accounts/profile/"', body)            # 保存せず戻る

    def test_profile_requires_login(self):
        res = self.client.get("/accounts/profile/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    # ---------- 報告ページ ----------
    def test_report_page_for_resolved_owner(self):
        """自分が解決した異常 = 報告フォームが見える"""
        self.client.force_login(self.nurse)
        res = self.client.get("/reports/%d/" % self.alert.id)
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "reports/form.html")
        self.assertContains(res, "誰だったか(利用者)")

    def test_report_page_rejects_non_owner(self):
        self.client.force_login(self.other)
        res = self.client.get("/reports/%d/" % self.alert.id)
        self.assertRedirects(res, "/alerts/%d/" % self.alert.id)

    def test_report_page_rejects_unresolved(self):
        self.alert.status = Alert.Status.RESPONDING
        self.alert.save(update_fields=["status"])
        self.client.force_login(self.nurse)
        res = self.client.get("/reports/%d/" % self.alert.id)
        self.assertRedirects(res, "/alerts/%d/" % self.alert.id)

    def test_report_create_redirects_to_detail_and_blocks_double(self):
        """報告保存は異常詳細へ戻る。同じ異常の二重入力は弾く"""
        self.client.force_login(self.nurse)
        res = self.client.post("/reports/create/", {
            "alert": self.alert.id,
            "physical_issue": "confirmed",
            "injury_detail": "額を打っていた",
            "action_taken": "バイタル測定",
            "comment": "看護師連絡済み",
        })
        self.assertRedirects(res, "/alerts/%d/" % self.alert.id)
        self.assertTrue(AlertReport.objects.filter(alert=self.alert).exists())

        res2 = self.client.post("/reports/create/", {"alert": self.alert.id})
        self.assertRedirects(res2, "/alerts/%d/" % self.alert.id)
        self.assertEqual(AlertReport.objects.filter(alert=self.alert).count(), 1)

        # 入力済みなら報告ページも開けない(詳細へ)
        res3 = self.client.get("/reports/%d/" % self.alert.id)
        self.assertRedirects(res3, "/alerts/%d/" % self.alert.id)

    # ---------- 遷移のつながり ----------
    def test_resolve_redirects_to_report_page(self):
        """「解決・報告を入力」→ 報告専用ページへ"""
        a = Alert.objects.create(
            kind=Alert.Kind.SUSPICIOUS, status=Alert.Status.RESPONDING,
            camera=self.camera, occurred_at=timezone.now(), responder=self.nurse,
        )
        self.client.force_login(self.nurse)
        res = self.client.post("/alerts/%d/resolve/" % a.id)
        self.assertRedirects(res, "/reports/%d/" % a.id)

    def test_alert_detail_has_report_button(self):
        """解決済み・未報告・自分の異常 → 詳細に「報告を入力」ボタン"""
        self.client.force_login(self.nurse)
        res = self.client.get("/alerts/%d/" % self.alert.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn("報告を入力", res.content.decode("utf-8"))
        self.assertIn("/reports/%d/" % self.alert.id, res.content.decode("utf-8"))
