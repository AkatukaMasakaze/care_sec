"""デモ用AI監視シミュレータ(バックグラウンドスレッド)

実データのAI解析は検討段階のため、このモジュールが「人物検出 → 姿勢/状態推定 →
転倒/無動作/ゾーン侵入の検知 → アラート+自動録画」のパイプラインを模擬します。

実AIに差し替える場合は、このモジュールの `_tick()` が書いている
PersonDetection / Alert / Recording へ同じ形で書き込むアダプタを実装すれば
UI・通知・記録系はそのまま動きます。
"""
import random
import threading
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.utils import timezone

from .models import AISettings, Alert, Camera, PersonDetection, Recording

TICK_SECONDS = 2
_started = False

MARKERS = {
    Alert.Kind.FALL: [
        {"seconds": 5, "label": "あやしい動き(ふらつき)"},
        {"seconds": 18, "label": "転倒の瞬間"},
    ],
    Alert.Kind.SUSPICIOUS: [
        {"seconds": 8, "label": "あやしい動き(長時間の無動作)"},
    ],
    Alert.Kind.ZONE: [
        {"seconds": 6, "label": "立ち入り禁止ゾーン侵入"},
    ],
}


def start():
    global _started
    if _started:
        return
    _started = True
    thread = threading.Thread(target=_loop, name="ai-monitor", daemon=True)
    thread.start()


def _loop():
    while True:
        try:
            _tick()
        except Exception:  # シミュレータ停止でWeb側を道連れにしない
            pass
        time.sleep(TICK_SECONDS)


def _thumb(kind):
    name = "thumb_fall.svg" if kind == Alert.Kind.FALL else "thumb_suspicious.svg"
    return settings.STATIC_URL + "img/" + name


def _create_alert(camera, kind, state, confidence):
    """アラート発生 + 検知前後の自動録画を作成する"""
    now = timezone.now()
    alert = Alert.objects.create(
        kind=kind,
        camera=camera,
        occurred_at=now,
        confidence=round(confidence, 2),
        detected_state=state,
        thumbnail_url=_thumb(kind),
    )
    Recording.objects.create(
        kind=Recording.Kind.FALL if kind == Alert.Kind.FALL else Recording.Kind.SUSPICIOUS,
        camera=camera,
        alert=alert,
        started_at=now - timedelta(seconds=10),
        ended_at=now + timedelta(seconds=35),
        video_url=getattr(settings, "DEMO_VIDEO_URL", ""),
        thumbnail_url=_thumb(kind),
        reason="AIが異常を検知したため自動録画",
        markers=MARKERS.get(kind, []),
    )
    return alert


def _tick():
    connections.close_old_connections()
    ai = AISettings.load()
    if not ai.enabled:
        PersonDetection.objects.all().delete()
        return

    now = timezone.now()
    for camera in Camera.objects.filter(is_online=True):
        last_alert = Alert.objects.filter(camera=camera).order_by("-occurred_at").first()
        cooldown_ok = (
            last_alert is None
            or (now - last_alert.occurred_at).total_seconds() >= ai.cooldown_seconds
        )
        detections = list(PersonDetection.objects.filter(camera=camera))

        for det in detections:
            # 人物が画面から去る
            if random.random() < 0.02:
                det.delete()
                continue

            # --- 人物検出(バウンディングボックスのランダムウォーク) ---
            dx, dy = random.gauss(0, 1.4), random.gauss(0, 1.0)
            moved = abs(dx) + abs(dy)
            det.x = min(max(det.x + dx, 2), 96 - det.w)
            det.y = min(max(det.y + dy, 5), 88 - det.h)

            # --- 姿勢/状態推定(状態遷移) ---
            previous_state = det.state
            roll = random.random()
            if det.state == PersonState.STANDING:
                if roll < 0.010:
                    det.state = PersonState.SITTING
                elif roll < 0.016:
                    det.state = PersonState.LYING
            elif det.state == PersonState.SITTING:
                if roll < 0.020:
                    det.state = PersonState.STANDING
                elif roll < 0.024:
                    det.state = PersonState.LYING
            elif det.state == PersonState.LYING and random.random() < 0.06:
                det.delete()  # 検知後に人物が消える
                continue

            # --- 信頼度 ---
            if det.state == PersonState.LYING and previous_state != PersonState.LYING:
                det.confidence = round(random.uniform(max(0.70, ai.fall_threshold - 0.05), 0.99), 2)
            else:
                det.confidence = round(min(0.99, max(0.60, det.confidence + random.gauss(0, 0.02))), 2)

            # --- 無動作秒数の積算 ---
            det.still_seconds = det.still_seconds + TICK_SECONDS if moved < 1.0 else 0

            # --- 異常検知 ---
            if cooldown_ok and not det.alerted:
                if (
                    det.state == PersonState.LYING
                    and previous_state != PersonState.LYING
                    and det.confidence >= ai.fall_threshold
                ):
                    _create_alert(camera, Alert.Kind.FALL, det.state, det.confidence)
                    det.alerted = True
                elif det.still_seconds >= ai.stillness_seconds and det.state != PersonState.LYING:
                    confidence = min(0.99, 0.55 + det.still_seconds / 200)
                    _create_alert(camera, Alert.Kind.SUSPICIOUS, det.state, confidence)
                    det.alerted = True
                    det.still_seconds = 0
                elif det.x + det.w > 85 and random.random() < 0.25:
                    # 画面右端15%を立ち入り禁止ゾーンとみなす
                    _create_alert(camera, Alert.Kind.ZONE, det.state, round(random.uniform(0.7, 0.95), 2))
                    det.alerted = True

            det.save()

        # --- 人物の出現 ---
        desired = (1 if random.random() < ai.sensitivity else 0) + (1 if random.random() < 0.15 else 0)
        if len(detections) < desired:
            PersonDetection.objects.create(
                camera=camera,
                person_key=uuid.uuid4().hex[:8],
                x=round(random.uniform(10, 70), 1),
                y=round(random.uniform(20, 55), 1),
                w=round(random.uniform(12, 20), 1),
                h=round(random.uniform(30, 45), 1),
                state=PersonState.STANDING,
                confidence=round(random.uniform(0.75, 0.95), 2),
            )
