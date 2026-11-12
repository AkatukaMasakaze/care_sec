"""全ページ共通コンテキスト(情報バーの未対応バッジ等)"""
from .models import AISettings, Alert


def alert_globals(request):
    # 未ログイン(ログイン画面など)では何も渡さない
    if not request.user.is_authenticated:
        return {}
    open_alerts = Alert.objects.filter(status=Alert.Status.OPEN)
    return {
        "open_alert_count": open_alerts.count(),
        "latest_open_alert": open_alerts.order_by("-occurred_at").first(),
        # サイドバーの「AI監視 稼働中/停止中」表示に使う
        "ai_monitor_enabled": AISettings.load().enabled,
    }