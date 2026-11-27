"""全ページ共通コンテキスト(情報バーの未対応バッジ等)"""
import time

from .models import AISettings, Alert

# 静的ファイル(css/js)のキャッシュ更新用。サーバー起動の時刻で変わる。
# 版本無しで読んでいると、ブラウザが古い css/js を使い続けて変更が反映されない
# (デバッグページで実際に起きた)ため、全ページで {{ asset_ver }} を使えるようにする。
ASSET_VER = str(int(time.time()))


def asset_version(request):
    """全テンプレートから {{ asset_ver }} を参照できるようにする"""
    return {"asset_ver": ASSET_VER}


def env_badge(request):
    """環境識別バッジ。**本番では何も出さない**(2026-11-27 方針)。

    バッジは開発者向けの注意表示で、介護スタッフの画面に運用情報(開発/本番)を
    見せる必要はない。dev のときだけ橙バッジ、prod は空。
    """
    from django.conf import settings

    if settings.ENV_NAME == "dev":
        return {"env_badge": {"key": "dev", "label": "開発環境"}}
    return {"env_badge": None}


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
