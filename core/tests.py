# -*- coding: utf-8 -*-
"""プロフィール/報告のページ分離のテスト

設計(2026-11-17):
  - プロフィール(`/accounts/profile/`)は自分の情報+編集+統計+履歴だけ。報告フォームは置かない
  - 報告は入力専用ページ `/reports/<異常ID>/` だけに表示される(入口は異常詳細)
  - 報告保存後は異常詳細へ戻る
"""
import json

from django.test import TestCase
from django.utils import timezone

from core.models import (
    AISettings, Alert, AlertReport, Camera, Resident, User, UserProfile,
)


class SettingsTabRolesTest(TestCase):
    """設定タブ: AI監視の稼働 / 異常検知デモ / データ削除 は管理者のみ"""

    @classmethod
    def setUpTestData(cls):
        cls.nurse = User.objects.create_user("nurse_s", password="pw-verify-1")
        cls.admin = User.objects.create_admin("admin_s", password="pw-verify-1")

    MARKERS = {
        "AI監視の稼働": 'id="ai-form"',
        "異常検知デモ": 'value="demo_alert"',
        "データ削除": 'value="clear_all_data"',
    }

    def test_nurse_cannot_see_admin_sections(self):
        self.client.force_login(self.nurse)
        res = self.client.get("/accounts/settings/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("通知設定", body)  # 通知設定は一般介護士にも見える(従来のまま)
        for name, marker in self.MARKERS.items():
            self.assertNotIn(marker, body, name + " が一般介護士に見えている")

    def test_admin_sees_all_sections(self):
        self.client.force_login(self.admin)
        res = self.client.get("/accounts/settings/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        for name, marker in self.MARKERS.items():
            self.assertIn(marker, body, name + " が管理者に見えない")

    def test_nurse_post_is_403(self):
        """表示を隠すだけじゃなく、URL直POSTもサーバー側で止める"""
        self.client.force_login(self.nurse)
        for action in ("toggle_ai_off", "toggle_ai_on", "demo_alert", "clear_all_data"):
            res = self.client.post("/accounts/settings/action/", {"action": action})
            self.assertEqual(res.status_code, 403, action + " が通ってしまった")
            self.assertTemplateUsed(res, "permission_denied.html")

    def test_anonymous_post_goes_to_login(self):
        res = self.client.post("/accounts/settings/action/", {"action": "clear_all_data"})
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    def test_admin_post_ai_toggle_works(self):
        self.client.force_login(self.admin)
        res = self.client.post("/accounts/settings/action/", {"action": "toggle_ai_off"})
        self.assertRedirects(res, "/accounts/settings/")
        self.assertFalse(AISettings.load().enabled)
        res = self.client.post("/accounts/settings/action/", {"action": "toggle_ai_on"})
        self.assertRedirects(res, "/accounts/settings/")
        self.assertTrue(AISettings.load().enabled)


class VelocityFallRuleTest(TestCase):
    """速度ルール(第2の転倒検出器)の純粋関数テスト

    実装: core/ai_engine._velocity_fall_check
    条件: ①windowフレーム以内にroll_thr°以上の急転倒 ②水平到達 ③起き上がらない
    """

    def _check(self, tilts):
        from core.ai_engine import _velocity_fall_check
        return _velocity_fall_check(list(tilts), 25.0, 10, 60.0, 45.0, 15)

    def test_fast_collapse_stays_down(self):
        """速く倒れて水平のまま → 発火"""
        tilts = [5.0] * 10 + list(range(5, 70, 6)) + [68.0] * 15
        ok, why = self._check(tilts)
        self.assertTrue(ok, why)

    def test_slow_lean_not_fired(self):
        """ゆっくり倒れる(物を拾う程度の速度) → window内25°未満で発火しない"""
        tilts = [5.0 + i * 1.5 for i in range(30)]  # 10フレームで15°ずつ
        ok, _ = self._check(tilts)
        self.assertFalse(ok)

    def test_pick_up_then_stand_not_fired(self):
        """速く深く傾いたが直後に起き上がった → 維持条件で発火しない"""
        tilts = [5.0] * 5 + list(range(5, 70, 6)) + [65.0] * 5 + list(range(65, 5, -6)) + [5.0] * 5
        ok, _ = self._check(tilts)
        self.assertFalse(ok)

    def test_fast_to_horizontal_but_not_enough_history(self):
        """履歴が window+stay 未満のうちは発火を急がない(誤爆防止)"""
        tilts = [5.0] * 5 + list(range(5, 70, 6))
        ok, _ = self._check(tilts)
        self.assertFalse(ok)

    def test_torso_tilt_math(self):
        """_torso_tilt: 直立0° 水平≈90°(肩-腰ベクトルから)"""
        from core.ai_engine import _torso_tilt

        class LM:
            def __init__(self, x, y):
                self.x, self.y = x, y

        upright = [LM(0, 0)] * 33
        upright[11] = upright[12] = LM(0.5, 0.42)   # 肩
        upright[23] = upright[24] = LM(0.5, 0.68)   # 腰
        self.assertAlmostEqual(_torso_tilt(upright), 0.0, places=2)

        horizontal = [LM(0, 0)] * 33
        horizontal[11] = horizontal[12] = LM(0.3, 0.67)
        horizontal[23] = horizontal[24] = LM(0.56, 0.68)
        self.assertGreater(_torso_tilt(horizontal), 70.0)


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

    def test_api_alerts_payload_fields(self):
        """app.js の大判通知が必要とするフィールドが揃っていること

        api_alerts は「未解決のみ」を返すので、このテスト用に未対応を作る
        (setUpTestData の self.alert は解決済みで一覧から除外される)
        """
        open_alert = Alert.objects.create(
            kind=Alert.Kind.FALL, status=Alert.Status.OPEN,
            camera=self.camera, occurred_at=timezone.now(),
        )
        self.client.force_login(self.nurse)
        res = self.client.get("/api/alerts/")
        self.assertEqual(res.status_code, 200)
        payload = json.loads(res.content.decode("utf-8"))
        self.assertIn("open_count", payload)
        self.assertIn("ai_enabled", payload)
        entry = [a for a in payload["alerts"] if a["id"] == open_alert.id][0]
        for key in ("kind", "kind_label", "status", "status_label",
                    "responder_name", "location", "camera_name", "occurred_ms"):
            self.assertIn(key, entry, key + " が無い")
        self.assertEqual(entry["kind"], "fall")
        self.assertIsInstance(entry["occurred_ms"], int)
        open_alert.delete()

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


class RecordingRetentionTest(TestCase):
    """録画の30日自動削除(プライバシー保存期間 / 2026-11-24)

    「全削除」ボタンを押されない運用でも録画が溜まり続けないよう、
    Recording は開始から RECORDING_RETENTION_DAYS 日で DB行+実ファイルを削除する。
    一覧の表示は「残り日数」のみ。
    """

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self._patch = override_settings(MEDIA_ROOT=self._tmp)
        self._patch.enable()
        self.addCleanup(self._patch.disable)
        self.media_root = Path(self._tmp)
        self.camera = Camera.objects.create(name="保存期間カメラ", location="棟B")

    def _make(self, name, age_days, with_alert=False):
        from datetime import timedelta

        from core.models import Recording

        started = timezone.now() - timedelta(days=age_days)
        rec_dir = self.media_root / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / f"{name}.mp4").write_bytes(b"fake-video")
        (rec_dir / f"{name}.jpg").write_bytes(b"fake-thumb")
        alert = None
        if with_alert:
            alert = Alert.objects.create(
                kind=Alert.Kind.FALL, camera=self.camera,
                occurred_at=started, confidence=0.9,
                thumbnail_url=f"/media/recordings/{name}.jpg",
            )
        return Recording.objects.create(
            kind=Recording.Kind.FALL, camera=self.camera, alert=alert,
            started_at=started, ended_at=started + timedelta(seconds=8),
            video_url=f"/media/recordings/{name}.mp4",
            thumbnail_url=f"/media/recordings/{name}.jpg",
            reason="テスト",
        )

    def test_days_left_counts_down(self):
        """残り日数 = 30 - 経過日数(当日=30, 29日経過=1, 30日経過=0)"""
        self.assertEqual(self._make("fresh", 0).days_left, 30)
        self.assertEqual(self._make("nearly", 29).days_left, 1)
        self.assertEqual(self._make("due", 30).days_left, 0)

    def test_cleanup_removes_expired_rows_and_files(self):
        """期間超過のみ削除。未超過は行とファイルが残る"""
        from core import retention

        old = self._make("old", 31)
        fresh = self._make("fresh", 10, with_alert=True)
        removed = retention.cleanup_expired(force=True)
        self.assertEqual(removed, 1)
        self.assertFalse(Recording_exists(old.id))
        self.assertFalse((self.media_root / "recordings" / "old.mp4").exists())
        self.assertFalse((self.media_root / "recordings" / "old.jpg").exists())
        self.assertTrue(Recording_exists(fresh.id))
        self.assertTrue((self.media_root / "recordings" / "fresh.mp4").exists())

    def test_alert_thumbnail_reference_cleared(self):
        """削除される録画のjpgをアラートサムネイルが指していたら参照を外す(表示壊れ防止)"""
        from core import retention
        from core.models import Recording

        old = self._make("old2", 45, with_alert=True)
        alert = old.alert
        retention.cleanup_expired(force=True)
        alert.refresh_from_db()
        self.assertEqual(alert.thumbnail_url, "")

    def test_list_page_shows_days_left_only(self):
        """一覧は残り日数のみ表示。削除日などの文言は要求外なので出さない"""
        from core.models import Recording

        self._make("shown", 20)
        user = User.objects.create_user("ret_viewer", password="x", is_admin=True)
        self.client.force_login(user)
        res = self.client.get("/recordings/")
        self.assertContains(res, "残り10日")
        self.assertContains(res, "30日</strong>で自動的に削除")

    def test_zero_retention_disables_auto_delete(self):
        """RECORDING_RETENTION_DAYS=0 は「自動削除しない」設定も成立(運用逃口)"""
        from django.test import override_settings

        from core import retention
        from core.models import Recording

        old = self._make("keep", 400)
        with override_settings(RECORDING_RETENTION_DAYS=0):
            self.assertIsNone(retention.cleanup_expired(force=True))
        self.assertTrue(Recording.objects.filter(id=old.id).exists())


def Recording_exists(recording_id):
    from core.models import Recording

    return Recording.objects.filter(id=recording_id).exists()


class ProtectedMediaTest(TestCase):
    """本番メディア配信はログイン済みユーザーのみ(AWS Setup Guide 8.3)

    Djangoのテストクライアントは DEBUG=False で走るため、urls.py の
    protected_media ルートが有効 = 本番と同じ経路を検証できる。
    """

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, True)
        patch = override_settings(MEDIA_ROOT=self._tmp)
        patch.enable()
        self.addCleanup(patch.disable)
        rec = Path(self._tmp) / "recordings"
        rec.mkdir()
        (rec / "clip.mp4").write_bytes(b"fake-video-bytes")
        self.nurse = User.objects.create_user("media_nurse", password="x")

    def test_anonymous_redirected_to_login(self):
        """未ログインの /media/ アクセスは動画を見せずログインへ誘導する"""
        res = self.client.get("/media/recordings/clip.mp4")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    def test_logged_in_gets_file(self):
        self.client.force_login(self.nurse)
        res = self.client.get("/media/recordings/clip.mp4")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "video/mp4")
        self.assertIn("private", res.headers["Cache-Control"])

    def test_traversal_is_denied(self):
        """/media/../ 系は MEDIA_ROOT 外 = 404(FileResponseに到達しない)"""
        self.client.force_login(self.nurse)
        res = self.client.get("/media/../escape.txt")
        self.assertIn(res.status_code, (400, 404))

    def test_missing_file_is_404(self):
        self.client.force_login(self.nurse)
        res = self.client.get("/media/recordings/nope.mp4")
        self.assertEqual(res.status_code, 404)
