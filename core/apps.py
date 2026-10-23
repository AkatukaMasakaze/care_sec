from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        """アプリ起動時にAIシミュレーターを自動起動"""
        import threading
        from .ai_simulator import simulator

        # バックグラウンドスレッドで起動
        threading.Thread(target=simulator.start, daemon=True).start()
