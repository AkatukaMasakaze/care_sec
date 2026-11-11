"""全ページ共通コンテキスト(情報バーの未対応バッジ等)"""
import time

from .models import Alert

# 静的ファイル(css/js)のキャッシュ更新用。サーバー起動の時刻で変わる。
# 版本無しで読んでいると、ブラウザが古い css/js を使い続けて変更が見えない
# (デバッグページで実際に起きた)ため、全ページで {{ asset_ver }} を使えるようにする。
ASSET_VER = str(int(time.time()))


def asset_version(request):
    """全テンプレートから共通変数を参照できるようにする"""
    return {"asset_ver": ASSET_VER}


def alert_globals(request):
    if not request.user.is_authenticated:
        return {}
    open_alerts = Alert.objects.filter(status=Alert.Status.OPEN)
    return {
        "open_alert_count": open_alerts.count(),
        "latest_open_alert": open_alerts.order_by("-occurred_at").first(),
    }
