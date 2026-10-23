"""CARE SEC 転倒・異常検知システム / データモデル(検証用実装)"""
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """介護士アカウント作成マネージャ(権限・グループ機能なし)"""

    def create_user(self, username, email=None, password=None, **extra_fields):
        if not username:
            raise ValueError("ユーザー名は必須です")
        email = self.normalize_email(email) if email else ""
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_admin(self, username, email=None, password=None, **extra_fields):
        """管理画面にアクセスできる介護士"""
        extra_fields.setdefault("is_admin", True)
        return self.create_user(username, email, password, **extra_fields)


class User(AbstractBaseUser):
    """介護士アカウント

    グループ・権限(superuser/permission)機能は使用しないため、
    Django標準のUserモデルの代わりに本モデルを使用する。
    """

    username = models.CharField("ユーザー名", max_length=150, unique=True)
    first_name = models.CharField("名", max_length=50, blank=True)
    last_name = models.CharField("姓", max_length=50, blank=True)
    email = models.EmailField("メールアドレス", blank=True)
    is_active = models.BooleanField("有効", default=True)
    is_admin = models.BooleanField("管理画面を利用できる", default=False)
    date_joined = models.DateTimeField("登録日", default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "介護士"
        verbose_name_plural = "介護士"
        default_permissions = ()  # 権限レコードを作らない

    def __str__(self):
        return self.get_full_name() or self.username

    def get_full_name(self):
        return f"{self.last_name} {self.first_name}".strip()

    def get_short_name(self):
        return self.first_name or self.username

    # --- django.contrib.admin 互換のためのプロパティ ---
    @property
    def is_staff(self):
        return self.is_admin

    @property
    def is_superuser(self):
        return self.is_admin

    # --- 権限チェックは全て許可(権限機能を使わないため) ---
    def has_perm(self, perm, obj=None):
        return True

    def has_perms(self, perm_list, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True


class PersonState(models.TextChoices):
    """AIが推定する人物の状態"""
    STANDING = "standing", "立っている"
    SITTING = "sitting", "座っている"
    LYING = "lying", "横たわっている"
    UNKNOWN = "unknown", "不明"


class Resident(models.Model):
    """介護施設の利用者"""
    name = models.CharField("氏名", max_length=64)
    room_number = models.CharField("部屋番号", max_length=16)
    created_at = models.DateTimeField("登録日", auto_now_add=True)

    class Meta:
        verbose_name = "利用者"
        verbose_name_plural = "利用者"
        default_permissions = ()  # 権限レコードを作らない

    def __str__(self):
        return f"{self.name}({self.room_number}号室)"


class Camera(models.Model):
    """AI解析対象の監視カメラ"""
    name = models.CharField("カメラ名", max_length=64)
    location = models.CharField("設置場所", max_length=128)
    # 実カメラ接続時はPresigned URLなど長くなるため text にする
    stream_url = models.TextField("ストリームURL", blank=True)
    is_online = models.BooleanField("online", default=True)

    class Meta:
        verbose_name = "カメラ"
        verbose_name_plural = "カメラ"
        default_permissions = ()

    def __str__(self):
        return self.name


class Alert(models.Model):
    """AIによる異常検知"""

    class Kind(models.TextChoices):
        FALL = "fall", "転倒"
        SUSPICIOUS = "suspicious", "あやしい動き"
        ZONE = "zone", "立ち入り禁止ゾーン"

    class Status(models.TextChoices):
        OPEN = "open", "未対応"
        RESPONDING = "responding", "対応中"
        RESOLVED = "resolved", "解決済み"
        FALSE_POSITIVE = "false_positive", "誤検知"

    kind = models.CharField("種類", max_length=16, choices=Kind.choices)
    status = models.CharField("状況", max_length=16, choices=Status.choices, default=Status.OPEN)
    camera = models.ForeignKey(Camera, verbose_name="カメラ", on_delete=models.CASCADE, related_name="alerts")
    occurred_at = models.DateTimeField("発生日時")
    confidence = models.FloatField("AI信頼度", null=True, blank=True, help_text="0.0〜1.0")
    detected_state = models.CharField(
        "検知時の人物状態", max_length=16,
        choices=PersonState.choices, default=PersonState.UNKNOWN,
    )
    created_at = models.DateTimeField("アラート発生時刻", default=timezone.now)
    responded_at = models.DateTimeField("対応開始時刻", null=True, blank=True)
    resolved_at = models.DateTimeField("対応完了時刻", null=True, blank=True)
    is_false_positive = models.BooleanField("誤検知", default=False)
    # S3のPresigned URL(署名付き)は500〜1,000文字になるため text にする
    thumbnail_url = models.TextField("サムネイルURL", blank=True)
    responder = models.ForeignKey(
        User, verbose_name="対応者",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="responding_alerts",
    )

    class Meta:
        verbose_name = "異常検知"
        verbose_name_plural = "異常検知"
        ordering = ["-occurred_at"]
        default_permissions = ()

    def __str__(self):
        return f"[{self.get_status_display()}] {self.get_kind_display()} @ {self.camera.name}"

    # ---- テンプレートから使うプロパティ ----
    @property
    def kind_label(self):
        return self.get_kind_display()

    @property
    def status_label(self):
        return self.get_status_display()

    @property
    def camera_name(self):
        return self.camera.name

    @property
    def location(self):
        return self.camera.location

    @property
    def recording_id(self):
        try:
            return self.recording.id
        except Recording.DoesNotExist:
            return None

    @property
    def confidence_percent(self):
        return int(round(self.confidence * 100)) if self.confidence is not None else None


class PersonDetection(models.Model):
    """AIによる人物検知のライブ結果(カメラoverlay表示用)"""
    camera = models.ForeignKey(Camera, verbose_name="カメラ", on_delete=models.CASCADE, related_name="detections")
    person_key = models.CharField("人物キー", max_length=32)
    x = models.FloatField("X%")
    y = models.FloatField("Y%")
    w = models.FloatField("幅%")
    h = models.FloatField("高さ%")
    state = models.CharField("状態", max_length=16, choices=PersonState.choices, default=PersonState.STANDING)
    confidence = models.FloatField("信頼度", default=0.8)
    still_seconds = models.IntegerField("無動作秒数", default=0)
    alerted = models.BooleanField("アラート済", default=False)
    updated_at = models.DateTimeField("更新時刻", auto_now=True)

    class Meta:
        verbose_name = "人物検知"
        verbose_name_plural = "人物検知"
        default_permissions = ()

    def __str__(self):
        return f"{self.camera.name} person={self.person_key} {self.get_state_display()}"


class AISettings(models.Model):
    """AI検知の設定(誤検知フィードバックで自動調整される)"""
    enabled = models.BooleanField("AI監視を稼働する", default=True)
    fall_threshold = models.FloatField("転倒検知の信頼度閾値", default=0.75)
    stillness_seconds = models.IntegerField("無動作検知の秒数", default=30)
    cooldown_seconds = models.IntegerField("アラート間クールダウン(秒)", default=300)
    sensitivity = models.FloatField("人物検出の感度", default=0.8)

    class Meta:
        verbose_name = "AI設定"
        verbose_name_plural = "AI設定"
        default_permissions = ()

    def __str__(self):
        return "AI設定"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Recording(models.Model):
    """AI監視による自動録画(転倒の瞬間・あやしい場面)"""

    class Kind(models.TextChoices):
        FALL = "fall", "転倒の瞬間"
        SUSPICIOUS = "suspicious", "あやしい場面"

    kind = models.CharField("種類", max_length=16, choices=Kind.choices)
    camera = models.ForeignKey(Camera, verbose_name="カメラ", on_delete=models.CASCADE, related_name="recordings")
    alert = models.OneToOneField(
        Alert, verbose_name="関連する異常",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="recording",
    )
    started_at = models.DateTimeField("録画開始")
    ended_at = models.DateTimeField("録画終了")
    # S3のPresigned URL(署名付き)は500〜1,000文字になるため text にする
    video_url = models.TextField("動画URL")
    thumbnail_url = models.TextField("サムネイルURL", blank=True)
    reason = models.CharField("録画理由", max_length=255, blank=True)
    # [{"seconds": 18, "label": "転倒の瞬間"}, ...] 形式のAI検知マーク
    markers = models.JSONField("AI検知マーク", default=list, blank=True)

    class Meta:
        verbose_name = "自動録画"
        verbose_name_plural = "自動録画"
        ordering = ["-started_at"]
        default_permissions = ()

    def __str__(self):
        return f"{self.started_at:%Y/%m/%d %H:%M} {self.get_kind_display()} @ {self.camera.name}"

    @property
    def kind_label(self):
        return self.get_kind_display()

    @property
    def camera_name(self):
        return self.camera.name

    @property
    def location(self):
        return self.camera.location

    @property
    def duration_label(self):
        delta = max(int((self.ended_at - self.started_at).total_seconds()), 0)
        h, rem = divmod(delta, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


class AlertReport(models.Model):
    """解決報告(誰だったか/身体的問題/対応内容/コメント)"""

    class PhysicalIssue(models.TextChoices):
        NONE = "none", "なし"
        SUSPECTED = "suspected", "あり(疑い)"
        CONFIRMED = "confirmed", "あり(確認)"
        UNKNOWN = "unknown", "不明"

    alert = models.OneToOneField(Alert, verbose_name="対象の異常", on_delete=models.CASCADE, related_name="report")
    author = models.ForeignKey(
        User, verbose_name="報告者",
        on_delete=models.CASCADE, related_name="reports",
    )
    resident = models.ForeignKey(
        Resident, verbose_name="利用者",
        null=True, blank=True, on_delete=models.SET_NULL, related_name="reports",
    )
    physical_issue = models.CharField(
        "身体的な問題", max_length=16,
        choices=PhysicalIssue.choices, default=PhysicalIssue.UNKNOWN,
    )
    injury_detail = models.CharField("怪我・体調の詳細", max_length=255, blank=True)
    action_taken = models.TextField("対応内容", blank=True)
    comment = models.TextField("その他コメント", blank=True)
    created_at = models.DateTimeField("報告日", auto_now_add=True)

    class Meta:
        verbose_name = "解決報告"
        verbose_name_plural = "解決報告"
        ordering = ["-created_at"]
        default_permissions = ()

    def __str__(self):
        return f"{self.alert} の報告 by {self.author}"

    @property
    def physical_issue_label(self):
        return self.get_physical_issue_display()

    @property
    def resident_name(self):
        return self.resident.name if self.resident else ""
