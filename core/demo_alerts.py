"""異常検知デモの手動生成(core/demo_alerts.py)

設定ページのボタンから**押されたときだけ**1件発生させる検証用モジュールです。
バックグラウンドで走ったり、時刻を見て自動生成したりすることは一切ありません
(第20章で自動シミュレーターは全削除済み。これはその「手動のみ版」の再開です)。

使途:
- 通知経路(5秒ポーリング → トースト / 通知音 / OS通知)の動作確認
- 異常検知一覧・詳細の表示確認
- 「対応する → 解決・報告を入力」のフロー練習

注意: デモで作ったアラートにも通常の操作が効きます。とくに「誤検知として報告」は
`AISettings` の感度自動調整に反映されるため、練習で連打すると実運用の閾値が動きます。
"""
import random
from datetime import timedelta

from django.utils import timezone

from .models import Alert, Camera, PersonState

# ランダム選択する検知種類
KINDS = [Alert.Kind.FALL, Alert.Kind.SUSPICIOUS, Alert.Kind.ZONE]

# 検知種類ごとに見せかける推定状態(None の種類はランダムに選ぶ)
DETECTED_BY_KIND = {
    Alert.Kind.FALL: PersonState.LYING,
    Alert.Kind.SUSPICIOUS: None,
    Alert.Kind.ZONE: PersonState.STANDING,
}

# プレースホルダのサムネイル(static/img に実在するものだけ)
PLACEHOLDER_THUMBS = {
    Alert.Kind.FALL: "/static/img/thumb_fall.svg",
    Alert.Kind.SUSPICIOUS: "/static/img/thumb_suspicious.svg",
    Alert.Kind.ZONE: "",       # zone 用のSVGは無い → 一覧は 🚨 プレースホルダ表示になる
}


def pick_camera():
    """デモを紐づけるカメラ(オンライン優先、無ければ何でも、それも無ければ None)"""
    camera = Camera.objects.filter(is_online=True).order_by("?").first()
    if camera is None:
        camera = Camera.objects.order_by("?").first()
    return camera


def create_demo_alert(kind=None, camera=None):
    """ランダムな異常検知を1件作る。カメラが無ければ None を返す

    実AI(`ai_engine`)が作る `Alert` と**同じ形**なので、一覧・詳細・通知・
    対応フローがそのまま通ります。`Recording` は作りません — 実体のない mp4 を
    リンクして再生を壊さないためで、サムネイルはプレースホルダSVGになります。
    """
    camera = camera or pick_camera()
    if camera is None:
        return None

    kind = kind or random.choice(KINDS)

    detected = DETECTED_BY_KIND.get(kind, PersonState.UNKNOWN)
    if detected is None:
        detected = random.choice([PersonState.SITTING, PersonState.LYING])

    return Alert.objects.create(
        kind=kind,
        camera=camera,
        status=Alert.Status.OPEN,
        occurred_at=timezone.now() - timedelta(seconds=random.randint(0, 15)),
        confidence=round(random.uniform(0.70, 0.99), 2),
        detected_state=detected,
        thumbnail_url=PLACEHOLDER_THUMBS.get(kind, ""),
    )
