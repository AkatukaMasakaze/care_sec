"""CARE SEC 転倒・異常検知システム / ビュー(検証用実装)"""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.contrib import messages
from django.conf import settings as django_settings  # views.settings(設定ページ)と名前が衝突するため別名
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import camera_service
from .ai_engine import ai_engine as realtime_ai
from .models import AISettings, Alert, AlertReport, Camera, Recording, Resident


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

    # 解決済み(報告が完了した異常)は誤検知にできない
    # 誤検知にすると「対応完了」の記録とAI閾値のフィードバックが食い違うため
    if alert.status == Alert.Status.RESOLVED:
        messages.warning(request, "解決済みの異常は誤検知として登録できません。")
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


def purge_media_files():
    """media ディレクトリ内の**ファイルをすべて削除**する(ディレクトリは残す)

    「全削除」ボタンは DB のレコードしか消していなかったため、実際の録画(mp4)や
    サムネイル(jpg)が media/ に残り続けていた。DB行を消した以上これらのファイルは
    誰からも参照されないため、**検知データ対応かどうかに関わらず全部消す**方式にする。

    注意点:
    - `alert_thumbs` / `camera_snapshots` / `recordings` などの**ディレクトリ自体は残す**。
      無れていると検知時の保存が失敗する(`mkdir` していない前提の箇所がある)ため。
    - 開いているファイルの削除に失敗しても処理を止めない(Windows は他プロセスが
      開いているファイルを消せないことがある)。件数をメッセージで返す。

    戻り値: (削除できた件数, 失敗した件数, 解放に失敗したファイル名のリスト)
    """
    root = Path(django_settings.MEDIA_ROOT)
    if not root.exists():
        return 0, 0, []

    deleted = 0
    failed = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            target = Path(dirpath) / name
            try:
                target.unlink()
                deleted += 1
            except OSError:
                failed.append(str(target.relative_to(root)))
    return deleted, len(failed), failed


@login_required
@require_POST
def settings(request):
    """設定ページのPOST処理（データ削除・AI監視のON/OFF）"""
    from core.models import AISettings

    ai = AISettings.load()
    action = request.POST.get("action")

    if action == "clear_all_data":
        # 全異常・録画・解決報告を削除
        AlertReport.objects.all().delete()
        Recording.objects.all().delete()
        Alert.objects.all().delete()

        # DBのレコードを消した以上、media/ の実ファイル(録画mp4・各種jpg)は
        # 誰からも参照されなくなる。まとめて削除する(ディレクトリは残す)
        deleted, failed, failed_names = purge_media_files()
        detail = "media 内のファイル %d件" % deleted
        if failed:
            detail += "(%d件は他のプログラムが開いていて削除できませんでした: %s)" % (
                failed, ", ".join(failed_names[:3]) + ("…" if failed > 3 else ""))
        messages.success(
            request,
            "すべての検知データ・録画・解決報告と、%s を削除しました。" % detail,
        )

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

    # ※ デモ用の「手動で異常検知を発生させる(manual_alert)」は削除しました。
    #    異常は実AI(core/ai_engine.py)の検知のみが登録します。

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


# ※ api_camera_detections(/api/camera/<id>/detections/) は削除しました。
#    実AIは解析済み映像(overlay付き)を配信する形で検知結果を出すため、
#    人物検知の座標JSONを使う仕組み自体が不要になりました。


# ================= カメラデバッグ(実カメラ環境テスト) =================
@login_required
def debug_camera(request):
    """実カメラ表示のデバッグページ(デモ機能とは独立)"""
    import time as _time

    return render(request, "debug/camera.html", {
        "ai_enabled": realtime_ai.enabled(),
        "ai_always_on": bool(getattr(django_settings, "AI_ALWAYS_ON", True)) and realtime_ai.enabled(),
        # デバッグページのJSは開発中に頻繁に更新するため、毎回読み直させる
        # (css/app.js 側は base.html の {{ asset_ver }} = サーバー起動時刻で更新される)
        "debug_asset_ver": int(_time.time()),
    })


@login_required
def debug_camera_devices(request):
    """カメラ一覧JSON(?force=1 で再検知)

    ローカル(USB)カメラに加え、台帳の `stream_url` から登録した
    **ネットワークカメラ(IPカメラ)** も同じ一覧に載せる。
    """
    force = request.GET.get("force") == "1"
    result = camera_service.detect_devices(force=force)

    # IPカメラ(台帳)とのマージは camera_service に一本化してある
    # (progress 側と同じ組み立てでないと、検知完了後の一覧からIPカメラが消える)
    result["devices"] = camera_service.merged_devices(result.get("devices", []))
    return JsonResponse(result)


@login_required
def debug_camera_detect_progress(request):
    """デバイス検知の進捗JSON(ブラウザの進行バー表示用)"""
    return JsonResponse(camera_service.detect_progress())


def _parse_device_key(raw):
    """?device= の値を運用キーに変換する(数値ならUSB index、`cam:N` ならIPカメラ)"""
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw  # "cam:12" など


@login_required
def debug_camera_ai_status(request):
    """リアルタイムAI解析の状態JSON(?device=キー / 無しは全カメラの要約から1台)"""
    data = realtime_ai.status(_parse_device_key(request.GET.get("device")))
    # サーバー起動時の自動再学習の最新結果も添える(有無が分かるように)
    from core import retraining

    data["retrain"] = retraining.last_run_summary()
    return JsonResponse(data)


@login_required
def debug_camera_stream(request):
    """選択デバイスのMJPEGストリーム(?device=インデックス)

    AI解析が有効な場合は、骨格・判定overlayを描画したフレームを配信する。
    """
    index = _parse_device_key(request.GET.get("device"))
    if index is None:
        return HttpResponse("deviceパラメータが不正です", status=400)

    if not camera_service.acquire(index):
        return HttpResponse(
            "カメラを開けませんでした(接続確認 / 他アプリ使用中 / IPカメラはstream_urlを確認)",
            status=503,
        )

    def generate():
        # リアルタイムAI解析への参加(失敗しても生配信は続ける)
        ai_session = False
        try:
            ai_session = realtime_ai.attach(index)
        except Exception as exc:  # noqa: BLE001 -配信を止めない
            print(f"[DebugCamera] AI解析への参加に失敗: {type(exc).__name__}: {exc}")
            ai_session = False

        # 常時稼働設定なら、このカメラもエンジン側で保持し続ける
        # → ブラウザを閉じたり「停止」しても解析・検知・録画が止まらない
        #    (失敗しても視聴参加自体は生きているので ai_session は落とさない)
        if bool(getattr(django_settings, "AI_ALWAYS_ON", True)):
            try:
                realtime_ai.start_persistent(index)
            except Exception as exc:  # noqa: BLE001
                print(f"[DebugCamera] 常時稼働の開始に失敗: {type(exc).__name__}: {exc}")

        interval = 1.0 / 15  # 配信レート上限(約15fps)
        # フレームが一時的に無いだけで配信をやめない待機上限(1fpsのカメラ・モデル読込中対策)
        idle_limit = float(getattr(django_settings, "AI_STREAM_IDLE_SECONDS", 60))
        last_yield = 0.0
        last_seq = -1
        last_ok = time.monotonic()
        try:
            while True:
                now = time.monotonic()
                if now - last_yield < interval:
                    time.sleep(min(0.02, interval - (now - last_yield)))
                    continue

                frame = None
                # 1) AIが描画済みの「新しいフレーム」を優先して流す
                #    (ブラウザ側で cap.read() を呼ぶと AI と同じキャプチャを
                #     取り合い、1fpsのカメラでは必ずどちらかが空振りになる)
                if ai_session:
                    seq = realtime_ai.frame_seq(index)
                    if seq != last_seq:
                        jpeg = realtime_ai.latest_jpeg(index)
                        if jpeg is not None:
                            frame = jpeg
                            last_seq = seq

                # 2) AIがこのカメラを読み取っていない時だけ生配信にフォールバック
                if frame is None and not realtime_ai.is_analyzing(index):
                    frame = camera_service.read_jpeg(index)

                if frame is None:
                    if now - last_ok > idle_limit:
                        break
                    time.sleep(0.05)
                    continue

                last_ok = now
                last_yield = now
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        finally:
            if ai_session:
                try:
                    # セッションIDを渡し、古い接続の切断が新しい解析を止めないようにする
                    realtime_ai.detach(index, ai_session)
                except Exception as exc:  # noqa: BLE001
                    print(f"[DebugCamera] AI解析の停止に失敗: {type(exc).__name__}: {exc}")
            camera_service.release(index)

    response = StreamingHttpResponse(
        generate(),
        content_type="multipart/x-mixed-replace; boundary=frame",
    )
    response["Cache-Control"] = "no-store"
    return response



