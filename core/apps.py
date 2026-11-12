from pathlib import Path

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    @staticmethod
    def _is_runserver_worker():
        """runserver の「サーバー本体プロセス」だけを判定する

        - autoreload 有効時: 再起動担当の親と実ワークの子が1つずつ立ち、
          子だけ RUN_MAIN=true になる(親で起動すると二重にカメラを占有する)
        - --noreload 時: 単一プロセスで RUN_MAIN は未設定
        それ以外のmanageコマンド(test / migrate など)では起動しない。
        """
        import os
        import sys

        if len(sys.argv) < 2 or sys.argv[1] != "runserver":
            return False
        return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv

    @staticmethod
    def _pid_alive(pid):
        """そのPIDが生きているか(依存ライブラリなしで判定)"""
        import os
        import platform

        if platform.system() == "Windows":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(int(pid), 0)
        except OSError:
            return False
        return True

    @classmethod
    def _claim_ai_lock(cls):
        """AI常時稼働の占有宣言(サーバー二重起動でのカメラ争奪を防ぐ)

        別プロセスがカメラを掴んでいると、もう一方はカメラを開こうとして
        約60秒ハングし、Webアプリごと応答を失う。それを防ぐ。
        """
        import atexit
        import os

        from django.conf import settings

        lock = Path(settings.BASE_DIR) / ".ai_always_on.lock"
        try:
            if lock.exists():
                other = int((lock.read_text(encoding="utf-8").strip() or "0"))
                if other and other != os.getpid() and cls._pid_alive(other):
                    print(
                        f"[RealtimeAI] 別のサーバー(PID {other})が既にAI常時稼働を占有しています。"
                        "このプロセスでは常時稼働を開始しません。runserver の二重起動を止めてください。"
                    )
                    return False
            lock.write_text(str(os.getpid()), encoding="utf-8")
        except (OSError, ValueError):
            return True  # ロックを触れない環境では従来どおり起動する

        def _release():
            try:
                if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    lock.unlink()
            except OSError:
                pass

        atexit.register(_release)
        return True

    def ready(self):
        """アプリ起動時に 自動再学習 / リアルタイムAI常時稼働 を開始する

        デモ用の擬似アラート生成機構(AIシミュレーター)は削除したため、
        ここで起動するのは実カメラのAI解析だけ。
        """
        import threading

        from django.conf import settings

        # ---- リアルタイムAI解析の常時稼働(/debug/camera/ を離れても解析を続ける) ----
        if not self._is_runserver_worker():
            return

        if not getattr(settings, "AI_REALTIME_ENABLED", True):
            print("[RealtimeAI] 常時稼働は無効です(AI_REALTIME_ENABLED=False)")
            return
        if not getattr(settings, "AI_ALWAYS_ON", True):
            print("[RealtimeAI] 常時稼働は無効です(AI_ALWAYS_ON=False)")
            return

        def _boot():
            """モデル読み込み・カメラ確保をリクエスト処理の外で行う

            起動後も定期的に再スキャンし、**後から差し込まれたカメラを自動で解析に加える**
            (本番は全カメラ常時解析が正しい挙動)。
            カメラ未接続は正常な状態なので、その間もWebアプリは動き続ける。
            """
            import time

            from . import readiness
            from .ai_engine import ai_engine

            readiness.note("sys", "サーバー起動: 常時稼働の準備を始めます")
            retry_seconds = int(getattr(settings, "AI_ALWAYS_RETRY_SECONDS", 30))
            first = True
            time.sleep(2.0)
            if not CoreConfig._claim_ai_lock():
                readiness.note("sys", "別のサーバープロセスがカメラを占有しているため、"
                                      "このプロセスでは常時稼働を開始しません", "warn")
                return
            readiness.note("sys", "カメラ占有ロックを確保しました(%.1f秒)" % readiness.elapsed())

            # 常時稼働を始める前に自動再学習を確認する
            # (学習はCPUを多く使うため解析と同時走らを避け、
            #  またモデルは初回使用時にキャッシュされるので反映は先の方が安全)
            try:
                from core import retraining

                readiness.note("model", "自動再学習を確認中…(ここで数秒〜数十秒かかることがあります)")
                retraining.run_startup_retraining()
                readiness.note("model", "自動再学習の確認が完了しました(経過 %.1f秒)" % readiness.elapsed())
            except Exception as exc:  # noqa: BLE001 -学習失敗でWebアプリ/解析は止めない
                readiness.note("model", "自動再学習でエラー(現行モデル継続): %s: %s" % (
                    type(exc).__name__, exc), "error")
                print(f"[Retrain] 自動再学習でエラー(現行モデル継続): "
                      f"{type(exc).__name__}: {exc}", flush=True)

            while True:
                try:
                    ai_engine.start_persistent(verbose=first)
                except Exception as exc:  # noqa: BLE001 -起動失敗でもWebアプリは止めない
                    readiness.note("camera", "常時稼働の起動に失敗: %s: %s" % (
                        type(exc).__name__, exc), "error")
                    if first:
                        print(f"[RealtimeAI] 常時稼働の起動に失敗: {type(exc).__name__}: {exc}")
                if first:
                    if not ai_engine.devices():
                        readiness.note("camera", "現在カメラが見つかりません(接続されると自動で開始します)", "warn")
                        print("[RealtimeAI] 現在カメラが見つかりません(接続されると自動で開始します)")
                    first = False
                time.sleep(retry_seconds)

        threading.Thread(target=_boot, daemon=True, name="realtime-ai-boot").start()
