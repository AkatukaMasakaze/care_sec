"""CARE SEC 転倒・異常検知システム / URL設計(URLS.md 準拠)"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views
from core.admin import admin_site

urlpatterns = [
    path("admin/", admin_site.urls),

    # ダッシュボード
    path("", views.dashboard, name="dashboard"),

    # 認証
    path("accounts/login/",
         auth_views.LoginView.as_view(
             template_name="accounts/login.html",
             redirect_authenticated_user=True,
         ),
         name="login"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("accounts/profile/", views.profile, name="profile"),
    path("accounts/settings/", views.settings_page, name="settings"),
    path("accounts/settings/action/", views.settings, name="settings_action"),
    path("reports/create/", views.report_create, name="report_create"),



    # 異常検知
    path("alerts/", views.alert_list, name="alert_list"),
    path("alerts/<int:alert_id>/", views.alert_detail, name="alert_detail"),
    path("alerts/<int:alert_id>/respond/", views.alert_respond, name="alert_respond"),
    path("alerts/<int:alert_id>/resolve/", views.alert_resolve, name="alert_resolve"),
    path("alerts/<int:alert_id>/false-positive/", views.alert_false_positive, name="alert_false_positive"),

    # 録画
    path("recordings/", views.recording_list, name="recording_list"),
    path("recordings/<int:recording_id>/", views.recording_detail, name="recording_detail"),

    # API
    path("api/alerts/", views.api_alerts, name="api_alerts"),

    # カメラデバッグ(実カメラ環境テスト用。ログイン必須)
    path("debug/camera/", views.debug_camera, name="debug_camera"),
    path("debug/camera/devices/", views.debug_camera_devices, name="debug_camera_devices"),
    path("debug/camera/progress/", views.debug_camera_detect_progress, name="debug_camera_detect_progress"),
    path("debug/camera/ai/", views.debug_camera_ai_status, name="debug_camera_ai_status"),
    path("debug/camera/stream/", views.debug_camera_stream, name="debug_camera_stream"),
]

# AI自動録画(mp4)などのメディアファイル配信(開発用。本番ではWebサーバー側で配信する)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
