"""CARE SEC 管理画面(ロールは is_admin の2段階)

Django標準の権限機能(groups / user_permissions)は持たないので、
`core.User.is_admin` だけで制御する。

  - 管理者(is_admin=True) のみアクセス可能
  - 一般介護士は /admin/ 配下すべてで 403

`manage.py createsuperuser` は `create_superuser()` 内で is_admin=True を立てるため、
既存の手順のまま管理者を作れる。
"""
from functools import wraps

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib.auth.models import Group
from django.shortcuts import render

from .models import (
    AISettings, Alert, AlertReport, Camera, Recording, Resident, User,
)
from .permissions import is_admin


class CareSecAdminSite(admin.AdminSite):
    """管理者(is_admin)のみが入れる管理サイト"""

    site_header = "CARE SEC 管理画面"
    site_title = "CARE SEC 管理"
    index_title = "CARE SEC データ管理"

    def has_permission(self, request):
        """ログイン済み + 有効 + **管理者**であること

        Django はこの関数を管理画面の全リクエストで通すので、ここで絞れば
        一覧/変更/削除/追加すべてをまとめて止められる。
        """
        return bool(
            request.user.is_authenticated
            and request.user.is_active
            and is_admin(request.user)
        )

    def login(self, request, extra_context=None):
        """/admin/login/ を直接叩かれたときのための保険

        Django既定では「has_permission 不合格 = 未ログイン」扱いで /admin/login/ へ
        逃がすので、確定しているユーザーにログインを要求することになる。
        (実際の誘導は admin_view 側で起きるため、そこは下の admin_view で止める)
        """
        if request.user.is_authenticated and not self.has_permission(request):
            return self._denied(request, "管理画面は管理者のみアクセスできます。")
        return super().login(request, extra_context)

    def admin_view(self, view, cacheable=False):
        """管理者でないログインユーザーは 403(ログイン画面へ誘導しない)

        Django の `AdminSite.admin_view` は `has_permission` が False だと
        `redirect_to_login(..., admin:login)` を返すため、一般介護士が /admin/ を
        叩くと302で管理画面のログインフォームに連れて行かれてしまう。
        ここを「ユーザーが確定している以上は 403」に差し替える。
        """
        inner = super().admin_view(view, cacheable=cacheable)

        @wraps(inner)
        def _gate(request, *args, **kwargs):
            if request.user.is_authenticated and not self.has_permission(request):
                return self._denied(request, "管理画面は管理者のみアクセスできます。")
            return inner(request, *args, **kwargs)

        return _gate

    @staticmethod
    def _denied(request, reason):
        return render(request, "permission_denied.html", {"reason": reason}, status=403)


admin_site = CareSecAdminSite(name="admin")


class BaseModelAdmin(admin.ModelAdmin):
    """全モデル共通のベース

    権限の判定は `is_admin` に一本化している。管理画面自体は `has_permission` で
    入り口を絞っているが、別のAdminSiteから再利用したときに抜け穴が残らないようにする。
    """

    def has_module_permission(self, request):
        return is_admin(request.user)

    def has_view_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_add_permission(self, request):
        return is_admin(request.user)

    def has_change_permission(self, request, obj=None):
        return is_admin(request.user)

    def has_delete_permission(self, request, obj=None):
        return is_admin(request.user)


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


@admin.register(AISettings, site=admin_site)
class AISettingsAdmin(BaseModelAdmin):
    list_display = ("enabled", "fall_threshold", "stillness_seconds", "cooldown_seconds", "sensitivity")
