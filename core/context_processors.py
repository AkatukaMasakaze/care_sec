"""全ページ共通コンテキスト(情報バーの未対応バッジ等)"""
from .models import Alert


def alert_globals(request):
    if not request.user.is_authenticated:
        return {}
    open_alerts = Alert.objects.filter(status=Alert.Status.OPEN)
    return {
        "open_alert_count": open_alerts.count(),
        "latest_open_alert": open_alerts.order_by("-occurred_at").first(),
    }
