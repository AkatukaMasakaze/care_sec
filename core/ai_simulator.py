"""AI検知シミュレーター(5秒ごとにバックグラウンドで異常を生成)"""
import random
import threading
import time
from datetime import timedelta

from django.utils import timezone


class AISimulator:
    """AI検知をシミュレーションするクラス"""

    def __init__(self):
        self.running = False
        self.thread = None

    def start(self):
        """シミュレーターを起動"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print("[AI Simulator] 起動しました")

    def stop(self):
        """シミュレーターを停止"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        print("[AI Simulator] 停止しました")

    def _run(self):
        """メインループ(5秒ごとに検知をシミュレーション)"""
        from core.models import AISettings, Alert, Camera, PersonDetection, PersonState

        while self.running:
            try:
                settings = AISettings.load()
                if not settings.enabled:
                    time.sleep(5)
                    continue

                cameras = Camera.objects.filter(is_online=True)
                if not cameras.exists():
                    time.sleep(5)
                    continue

                # 各カメラについて検知をシミュレーション
                for camera in cameras:
                    self._simulate_detection(camera, settings)

                # クリーンアップ(古い検知データを削除)
                cutoff = timezone.now() - timedelta(seconds=60)
                PersonDetection.objects.filter(updated_at__lt=cutoff).delete()

            except Exception as e:
                print(f"[AI Simulator] エラー: {e}")

            time.sleep(5)

    def _simulate_detection(self, camera, settings):
        """1つのカメラについて検知をシミュレーション"""
        from core.models import Alert, PersonDetection, PersonState, Recording

        # ランダムに人物を検出(感度に基づいて確率調整)
        if random.random() > settings.sensitivity:
            return

        # 人物の状態をランダムに決定
        states = [PersonState.STANDING, PersonState.SITTING, PersonState.LYING]
        state = random.choice(states)
        confidence = random.uniform(0.7, 0.99)

        # 位置をランダムに決定(画面内の%)
        x = random.uniform(10, 80)
        y = random.uniform(20, 90)
        w = random.uniform(10, 20)
        h = random.uniform(30, 60)

        # 人物検知レコードを更新または作成
        detection, created = PersonDetection.objects.get_or_create(
            camera=camera,
            person_key=f"person_{camera.id}_{int(x)}_{int(y)}",
            defaults={
                "x": x, "y": y, "w": w, "h": h,
                "state": state, "confidence": confidence,
            }
        )
        if not created:
            detection.x = x
            detection.y = y
            detection.w = w
            detection.h = h
            detection.state = state
            detection.confidence = confidence
            detection.save()

        # 転倒検知(横たわっている状態)
        if state == PersonState.LYING and confidence >= settings.fall_threshold:
            self._create_alert(camera, Alert.Kind.FALL, confidence, state, detection)

        # 不動検知(一定時間動かない)
        if detection.still_seconds >= settings.stillness_seconds:
            self._create_alert(camera, Alert.Kind.SUSPICIOUS, confidence, state, detection)

        # ランダムにあやしい動き(低確率)
        if random.random() < 0.05:  # 5%の確率
            self._create_alert(camera, Alert.Kind.SUSPICIOUS, confidence, state, detection)

        # 誤検知シミュレーション(低確率、低信頼度)
        if random.random() < 0.02:  # 2%の確率で誤検知
            false_confidence = random.uniform(0.5, 0.7)  # 低い信頼度
            self._create_alert(camera, Alert.Kind.SUSPICIOUS, false_confidence, state, detection, is_false_positive=True)

    def _create_alert(self, camera, kind, confidence, state, detection, is_false_positive=False):
        """異常アラートを作成"""
        from core.models import Alert, Recording

        # クールダウンチェック
        recent = Alert.objects.filter(
            camera=camera,
            created_at__gte=timezone.now() - timedelta(seconds=300),
        ).exists()
        if recent:
            return

        # アラートを作成
        status = Alert.Status.FALSE_POSITIVE if is_false_positive else Alert.Status.OPEN
        alert = Alert.objects.create(
            kind=kind,
            confidence=confidence,
            detected_state=state,
            camera=camera,
            occurred_at=timezone.now(),
            created_at=timezone.now(),
            status=status,
            is_false_positive=is_false_positive,
            thumbnail_url="/static/img/thumb_fall.svg" if kind == Alert.Kind.FALL else "/static/img/thumb_suspicious.svg",
        )

        # 録画を作成
        Recording.objects.create(
            kind=Recording.Kind.FALL if kind == Alert.Kind.FALL else Recording.Kind.SUSPICIOUS,
            camera=camera,
            alert=alert,
            started_at=alert.occurred_at - timedelta(seconds=10),
            ended_at=alert.occurred_at + timedelta(seconds=35),
            video_url="https://www.w3schools.com/html/mov_bbb.mp4",
            thumbnail_url=alert.thumbnail_url,
            reason="AIが異常を検知したため自動録画",
            markers=[
                {"seconds": 5, "label": "あやしい動き"},
                {"seconds": 18, "label": "転倒の瞬間" if kind == Alert.Kind.FALL else "異常検知"},
            ],
        )

        # 検知をアラート済みにマーク
        detection.alerted = True
        detection.save()

        print(f"[AI Simulator] 異常検知: {kind} @ {camera.name} (信頼度: {confidence:.2f})")


# グローバルインスタンス
simulator = AISimulator()
