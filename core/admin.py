"""CARE SEC 管理画面(権限・グループ機能なしのAdminSite)

介護士全員が同等に利用できる前提のため、
- モデルの閲覧/追加/変更/削除権限チェックを全て無効化
- ログイン中の(active)介護士であれば誰でもアクセス可能
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib.auth.models import Group

from .models import (
    AISettings, Alert, AlertReport, Camera, PersonDetection, Recording, Resident, User,
)


class CareSecAdminSite(admin.AdminSite):
    """権限チェックを行わない管理サイト"""

    site_header = "CARE SEC 管理画面"
    site_title = "CARE SEC 管理"
    index_title = "CARE SEC データ管理"

    def has_permission(self, request):
        """ログイン中かつ有効な介護士なら誰でもアクセス可能"""
        return bool(request.user.is_authenticated and request.user.is_active)


admin_site = CareSecAdminSite(name="admin")


class BaseModelAdmin(admin.ModelAdmin):
    """全モデル共通のベース(権限チェックを無効化)"""

    def has_module_permission(self, request):
        return True

    def has_view_permission(self, request, obj=None):
        return True

    def has_add_permission(self, request):
        return True

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


# 標準UserAdmin/GroupAdminを默认サイトから解除(ユーザーモデル差し替えのため)
for model in (get_user_model(), Group):
    try:
        admin.site.unregister(model)
    except admin.sites.NotRegistered:
        pass


# ================= 介護士アカウント =================
class CareWorkerCreationForm(UserCreationForm):
    """介護士の新規作成(パスワードをハッシュ化して保存)"""

    class Meta:
        model = User
        fields = ("username", "last_name", "first_name", "email", "is_active", "is_admin")


class CareWorkerChangeForm(UserChangeForm):
    """介護士の編集(パスワードはハッシュ済みのまま変更不可)"""

    class Meta:
        model = User
        fields = ("username", "last_name", "first_name", "email", "is_active", "is_admin")


@admin.register(User, site=admin_site)
class CareWorkerAdmin(BaseModelAdmin):
    add_form = CareWorkerCreationForm
    form = CareWorkerChangeForm

    def get_form(self, request, obj=None, **kwargs):
        """新規作成時はadd_form(パスワード1/2)、編集時はform(ハッシュ表示)を使う"""
        defaults = {"form": self.add_form if obj is None else self.form}
        defaults.update(kwargs)
        return super().get_form(request, obj, **defaults)

    def get_fieldsets(self, request, obj=None):
        """新規作成時はadd_fieldsets(パスワード1/2)を表示"""
        if obj is None:
            return self.add_fieldsets
        return self.fieldsets

    list_display = ("username", "last_name", "first_name", "email", "is_active", "is_admin", "date_joined")
    list_filter = ("is_active", "is_admin")
    search_fields = ("username", "last_name", "first_name", "email")
    ordering = ("username",)
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("個人情報", {"fields": ("last_name", "first_name", "email")}),
        ("利用状況", {"fields": ("is_active", "is_admin")}),
        ("日付", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "last_name", "first_name", "email", "is_active", "is_admin", "password1", "password2"),
        }),
    )


# ================= 業務データ =================
@admin.register(Resident, site=admin_site)
class ResidentAdmin(BaseModelAdmin):
    list_display = ("name", "room_number", "created_at")
    search_fields = ("name", "room_number")


@admin.register(Camera, site=admin_site)
class CameraAdmin(BaseModelAdmin):
    list_display = ("name", "location", "is_online")
    list_editable = ("is_online",)


@admin.register(Alert, site=admin_site)
class AlertAdmin(BaseModelAdmin):
    list_display = (
        "occurred_at", "kind", "status", "camera", "responder",
        "confidence", "detected_state", "is_false_positive",
    )
    list_filter = ("kind", "status", "is_false_positive")
    search_fields = ("camera__name",)
    readonly_fields = ("created_at", "responded_at", "resolved_at")


@admin.register(Recording, site=admin_site)
class RecordingAdmin(BaseModelAdmin):
    list_display = ("started_at", "kind", "camera", "duration_label", "alert")
    list_filter = ("kind",)


@admin.register(AlertReport, site=admin_site)
class AlertReportAdmin(BaseModelAdmin):
    list_display = ("created_at", "alert", "author", "resident", "physical_issue")
    list_filter = ("physical_issue",)


@admin.register(PersonDetection, site=admin_site)
class PersonDetectionAdmin(BaseModelAdmin):
    list_display = ("camera", "person_key", "state", "confidence", "still_seconds", "updated_at")
    list_filter = ("camera", "state")


@admin.register(AISettings, site=admin_site)
class AISettingsAdmin(BaseModelAdmin):
    list_display = ("enabled", "fall_threshold", "stillness_seconds", "cooldown_seconds", "sensitivity")
