"""CARE SEC 転倒・異常検知システム / ビュー(検証用実装)"""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import AISettings, Alert, AlertReport, Camera, PersonDetection, Recording, Resident


# ================= ダッシュボード =================
@login_required
def dashboard(request):
    now = timezone.now()
    today = now.date()
    alerts = Alert.objects.select_related("camera")

    total_alerts = alerts.count()
    false_positive_count = alerts.filter(status=Alert.Status.FALSE_POSITIVE).count()
    stats = {
        "today_alerts": alerts.filter(occurred_at__date=today).count(),
        "yesterday_alerts": alerts.filter(occurred_at__date=today - timedelta(days=1)).count(),
        "open_alerts": alerts.filter(status=Alert.Status.OPEN).count(),
        "residents": Resident.objects.count(),
        "cameras_online": Camera.objects.filter(is_online=True).count(),
        "cameras_total": Camera.objects.count(),
        "month_alerts": alerts.filter(
            occurred_at__year=today.year, occurred_at__month=today.month,
        ).count(),
        "open_count": alerts.filter(status=Alert.Status.OPEN).count(),
        "responding_count": alerts.filter(status=Alert.Status.RESPONDING).count(),
        "resolved_count": alerts.filter(status=Alert.Status.RESOLVED).count(),
        "false_positive_count": false_positive_count,
        "false_positive_rate": round(false_positive_count / total_alerts * 100, 1) if total_alerts > 0 else 0,
    }

    # 直近7日の検知傾向(棒グラフ用。最大値を100%とする)
    weekly_chart = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        weekly_chart.append({
            "label": f"{day.month}/{day.day}",
            "count": alerts.filter(occurred_at__date=day).count(),
        })
    max_count = max([p["count"] for p in weekly_chart] + [1])
    for point in weekly_chart:
        point["height_percent"] = int(point["count"] / max_count * 100)

    return render(request, "dashboard.html", {
        "stats": stats,
        "recent_alerts": alerts[:5],
        "weekly_chart": weekly_chart,
        "recent_recordings": Recording.objects.select_related("camera")[:4],
        "cameras": Camera.objects.all(),
        # ---- 集計・分析 ----
        "status_counts": {
            "open": alerts.filter(status=Alert.Status.OPEN).count(),
            "responding": alerts.filter(status=Alert.Status.RESPONDING).count(),
            "resolved": alerts.filter(status=Alert.Status.RESOLVED).count(),
        },
        "camera_counts": Camera.objects.annotate(alert_count=Count("alerts")).order_by("-alert_count"),
        "ai": _ai_summary(),
    })


def _ai_summary():
    """AIモデルの精度サマリ(誤検知フィードバック込み)"""
    ai = AISettings.load()
    total = Alert.objects.count()
    false_positives = Alert.objects.filter(is_false_positive=True).count()
    return {
        "settings": ai,
        "total": total,
        "false_positives": false_positives,
        "accuracy": round((1 - false_positives / total) * 100, 1) if total else 100.0,
    }





# ================= 異常検知 =================
@login_required
def alert_list(request):
    """異常一覧 + フィルタ(状況/種類/日付/対応者)"""
    from core.models import User

    current_status = request.GET.get("status", "")
    current_kind = request.GET.get("kind", "")
    current_responder = request.GET.get("responder", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")

    alerts = Alert.objects.select_related("camera", "responder")
    if current_status in Alert.Status.values:
        alerts = alerts.filter(status=current_status)
    if current_kind in Alert.Kind.values:
        alerts = alerts.filter(kind=current_kind)
    if current_responder.isdigit():
        alerts = alerts.filter(responder_id=int(current_responder))
    for value, lookup in ((date_from, "occurred_at__date__gte"), (date_to, "occurred_at__date__lte")):
        if value:
            try:
                day = datetime.strptime(value, "%Y-%m-%d").date()
                alerts = alerts.filter(**{lookup: day})
            except ValueError:
                pass

    return render(request, "alerts/list.html", {
        "alerts": alerts,
        "current_status": current_status,
        "current_kind": current_kind,
        "current_responder": current_responder,
        "date_from": date_from,
        "date_to": date_to,
        "kinds": Alert.Kind.choices,
        "responder_choices": User.objects.filter(responding_alerts__isnull=False).distinct(),
    })


@login_required
def alert_detail(request, alert_id):
    """過去の異常の詳細(検知情報/時刻ログ/対応/報告/録画)"""
    alert = get_object_or_404(Alert.objects.select_related("camera", "responder"), id=alert_id)
    recording = getattr(alert, "recording", None)
    report = getattr(alert, "report", None)
    return render(request, "alerts/detail.html", {
        "alert": alert,
        "recording": recording,
        "report": report,
    })


@login_required
@require_POST
def alert_respond(request, alert_id):
    """「対応する」: 自分が対応者になる(全アクセス者に共有される)"""
    alert = get_object_or_404(Alert, id=alert_id)
    if alert.status == Alert.Status.OPEN:
        alert.responder = request.user
        alert.status = Alert.Status.RESPONDING
        alert.responded_at = timezone.now()
        alert.save(update_fields=["responder", "status", "responded_at"])
        messages.success(request, f"「{alert.kind_label}」の対応を担当しました。")
    else:
        messages.warning(request, "この異常はすでに対応中です。")
    return redirect("alert_list")


@login_required
@require_POST
def alert_resolve(request, alert_id):
    """「解決・報告を入力」: 解決済みにしてプロフィールの報告フォームへ"""
    alert = get_object_or_404(Alert, id=alert_id)
    has_report = hasattr(alert, "report")
    if alert.status == Alert.Status.RESPONDING and alert.responder == request.user and not has_report:
        alert.status = Alert.Status.RESOLVED
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["status", "resolved_at"])
        return redirect(f"{reverse('profile')}?alert={alert.id}")
    messages.warning(request, "解決処理できませんでした(権限または状態を確認してください)。")
    return redirect("alert_list")


@login_required
@require_POST
def alert_false_positive(request, alert_id):
    """誤検知として報告: ステータスを誤検知に変更し、AI閾値の自動調整に反映する"""
    alert = get_object_or_404(Alert, id=alert_id)
    if alert.status == Alert.Status.FALSE_POSITIVE:
        messages.info(request, "この異常はすでに誤検知登録済みです。")
        return redirect("alert_detail", alert_id=alert.id)

    alert.status = Alert.Status.FALSE_POSITIVE
    alert.is_false_positive = True
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["status", "is_false_positive", "resolved_at"])

    ai = AISettings.load()
    if alert.kind == Alert.Kind.FALL:
        ai.fall_threshold = min(0.95, round(ai.fall_threshold + 0.02, 2))
        ai.save()
        messages.success(
            request,
            f"誤検知として登録しました。フィードバックを反映し、転倒検知の値を {ai.fall_threshold} に引き上げました。",
        )
    else:
        ai.stillness_seconds = min(120, ai.stillness_seconds + 5)
        ai.save()
        messages.success(
            request,
            f"誤検知として登録しました。フィードバックを反映し、無動作検知の秒数を {ai.stillness_seconds} 秒に延長しました。",
        )
    return redirect("alert_detail", alert_id=alert.id)


# ================= プロフィール / 解決報告 =================
@login_required
def profile(request):
    target_alert = None
    alert_id = request.GET.get("alert")
    if alert_id and alert_id.isdigit():
        alert = Alert.objects.filter(
            id=int(alert_id), responder=request.user, status=Alert.Status.RESOLVED,
        ).first()
        if alert and not hasattr(alert, "report"):
            target_alert = alert

    my_reports = AlertReport.objects.filter(author=request.user).select_related("alert", "resident")
    return render(request, "accounts/profile.html", {
        "target_alert": target_alert,
        "residents": Resident.objects.all(),
        "my_reports": my_reports,
        "profile": {
            "role": "介護スタッフ",
            "facility": "さくら介護施設",
            "resolved_count": my_reports.count(),
        },
    })


@login_required
@require_POST
def settings(request):
    """設定ページのPOST処理（データ削除・AI切替・手動検知）"""
    from core.models import AISettings

    ai = AISettings.load()
    action = request.POST.get("action")

    if action == "clear_all_data":
        # 全異常・録画・解決報告・人物検知を削除
        AlertReport.objects.all().delete()
        Recording.objects.all().delete()
        Alert.objects.all().delete()
        PersonDetection.objects.all().delete()
        messages.success(request, "すべての検知データ・録画・解決報告を削除しました。")

    elif action == "toggle_ai_on":
        # AI自動検知をオンにする
        ai.enabled = True
        ai.save(update_fields=["enabled"])
        messages.success(request, "AI監視を稼働しました。")

    elif action == "toggle_ai_off":
        # AI自動検知をオフにする
        ai.enabled = False
        ai.save(update_fields=["enabled"])
        messages.success(request, "AI監視を停止しました。")

    elif action == "manual_alert":
        # 手動で異常検知を1件発生（カメラCAM-01に転倒を検知）
        camera = Camera.objects.filter(is_online=True).first()
        if camera:
            Alert.objects.create(
                kind=Alert.Kind.FALL,
                confidence=0.95,
                detected_state=Alert.Kind.FALL,
                camera=camera,
                occurred_at=timezone.now(),
                status=Alert.Status.OPEN,
                thumbnail_url="/static/img/thumb_fall.svg",
            )
            messages.success(request, f"手動で異常検知を発生させました（{camera.name}）。")
        else:
            messages.warning(request, "オンラインのカメラが見つかりません。")

    return redirect("settings")


@login_required
def settings_page(request):
    """設定ページ（GET）"""
    from core.models import AISettings
    ai = AISettings.load()
    return render(request, "accounts/settings.html", {
        "ai_settings": ai,
    })


@login_required
@require_POST
def report_create(request):
    alert = get_object_or_404(
        Alert, id=request.POST.get("alert", ""),
        responder=request.user, status=Alert.Status.RESOLVED,
    )
    if hasattr(alert, "report"):
        messages.warning(request, "この異常の報告はすでに入力済みです。")
        return redirect("profile")

    resident_id = request.POST.get("resident") or None
    resident = Resident.objects.filter(id=resident_id).first() if resident_id else None
    physical_issue = request.POST.get("physical_issue", "")
    if physical_issue not in AlertReport.PhysicalIssue.values:
        physical_issue = AlertReport.PhysicalIssue.UNKNOWN

    AlertReport.objects.create(
        alert=alert,
        author=request.user,
        resident=resident,
        physical_issue=physical_issue,
        injury_detail=request.POST.get("injury_detail", "").strip(),
        action_taken=request.POST.get("action_taken", "").strip(),
        comment=request.POST.get("comment", "").strip(),
    )
    messages.success(request, "解決報告を保存しました。")
    return redirect("profile")


# ================= 録画 =================
@login_required
def recording_list(request):
    current_kind = request.GET.get("kind", "")
    recordings = Recording.objects.select_related("camera")
    if current_kind in Recording.Kind.values:
        recordings = recordings.filter(kind=current_kind)
    return render(request, "recordings/list.html", {
        "recordings": recordings,
        "current_kind": current_kind,
    })


@login_required
def recording_detail(request, recording_id):
    recording = get_object_or_404(Recording.objects.select_related("camera", "alert"), id=recording_id)

    markers = []
    for marker in recording.markers or []:
        seconds = int(marker.get("seconds", 0))
        markers.append({
            "seconds": seconds,
            "label": marker.get("label", ""),
            "time_label": f"{seconds // 60:02d}:{seconds % 60:02d}",
        })

    related_alert = recording.alert
    related_report = None
    if related_alert is not None:
        related_report = getattr(related_alert, "report", None)

    return render(request, "recordings/detail.html", {
        "recording": recording,
        "markers": markers,
        "related_alert": related_alert,
        "related_report": related_report,
    })


# ================= ログアウト =================
def logout_view(request):
    """POSTでログアウトし、ログアウト完了ページを表示する"""
    if request.method == "POST":
        auth_logout(request)
        return render(request, "accounts/logout.html")
    return redirect("login")


# ================= API =================
@login_required
def api_alerts(request):
    """異常検知ポーリング用JSON(static/js/app.js が5秒間隔で呼ぶ)"""
    unresolved = Alert.objects.exclude(status=Alert.Status.RESOLVED).select_related("camera", "responder")[:50]
    return JsonResponse({
        "open_count": Alert.objects.filter(status=Alert.Status.OPEN).count(),
        "alerts": [
            {
                "id": alert.id,
                "kind_label": alert.kind_label,
                "status": alert.status,
                "status_label": alert.status_label,
                "responder_name": (
                    alert.responder.get_full_name() or alert.responder.username
                ) if alert.responder else None,
                "location": alert.location,
            }
            for alert in unresolved
        ],
    })


@login_required
def api_camera_detections(request, camera_id):
    """AI人物検知のライブ結果(カメラoverlayの検知枠表示用JSON)"""
    camera = get_object_or_404(Camera, id=camera_id)
    detections = PersonDetection.objects.filter(
        camera=camera, updated_at__gte=timezone.now() - timedelta(seconds=10),
    )
    return JsonResponse({
        "detections": [
            {
                "person_key": d.person_key,
                "x": d.x, "y": d.y, "w": d.w, "h": d.h,
                "state": d.state,
                "state_label": d.get_state_display(),
                "confidence": d.confidence,
            }
            for d in detections
        ],
    })



