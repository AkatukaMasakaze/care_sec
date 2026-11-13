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
    def _process_birth(pid):
        """そのPIDの実プロセスの開始時刻を整数で返す。居なければ None

        単なる「生存チェック」にしない理由が2つある:

        1. Windows の OpenProcess は **終了済みだが親に回収されていないゾンビ**
           にも成功する実測がある(強制killした runserver の残骸で、
           次のサーバーが永久に常時稼働を始められなくなった)。
           → GetExitCodeProcess == STILL_ACTIVE まで確認する。
        2. ゾンビが回収された後に **同じPIDを別プロセスが使い回す**と、
           「生き残った自分」を先代の残りかすと誤認してしまう。
           → 開始時刻をロックファイルに書いておき、照合する。
        """
        import platform

        if platform.system() == "Windows":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                            ("dwHighDateTime", ctypes.c_uint32)]

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return None
            try:
                exit_code = ctypes.c_uint32(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                if exit_code.value != STILL_ACTIVE:
                    return None  # ゾンビ(終了済み・未回収)は居ないもの扱い
                creation = FILETIME()
                others = FILETIME()
                if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(others),
                    ctypes.byref(others), ctypes.byref(others),
                ):
                    return None
                return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            finally:
                kernel32.CloseHandle(handle)

        import os
        try:
            os.kill(int(pid), 0)
        except OSError:
            return None
        try:  # /proc があれば開始時刻を拾う(無い環境では 1 = 生存だけ返す)
            stat = open(f"/proc/{int(pid)}/stat", encoding="ascii").read().split()
            return int(stat[21])
        except (OSError, IndexError, ValueError):
            return 1

    @classmethod
    def _claim_ai_lock(cls):
        """AI常時稼働の占有宣言(サーバー二重起動でのカメラ争奪を防ぐ)

        別プロセスがカメラを掴んでいると、もう一方はカメラを開こうとして
        約60秒ハングし、Webアプリごと応答を失う。それを防ぐ。

        中身は「PID 開始時刻」の2列。PIDだけでは、
        ① 強制kill後のゾンビ(OpenProcessが成功してしまう)や
        ② ゾンビ回収後に同じPIDを別プロセスが使い回すケース
        で「死んでいる先代を生きている」と誤認し、以後すべてのサーバーが
        常時稼働を開始できない(実測: カメラが永久に動かない)ため、
        開始時刻まで照合する。
        """
        import atexit
        import os

        from django.conf import settings

        lock = Path(settings.BASE_DIR) / ".ai_always_on.lock"
        my_birth = cls._process_birth(os.getpid())
        try:
            if lock.exists():
                parts = (lock.read_text(encoding="utf-8") or "").split()
                other = int(parts[0]) if parts and parts[0].isdigit() else 0
                other_birth = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                if other and other != os.getpid():
                    live_birth = cls._process_birth(other)
                    if live_birth is not None and (
                        other_birth is None or live_birth == other_birth
                    ):
                        print(
                            f"[RealtimeAI] 別のサーバー(PID {other})が既にAI常時稼働を占有しています。"
                            "このプロセスでは常時稼働を開始しません。runserver の二重起動を止めてください。"
                        )
                        return False
                    if live_birth is not None:
                        # 同じPIDで開始時刻が違う = PIDの使い回し。この残骸は無関係
                        print(f"[RealtimeAI] ロック残骸(PID {other})は別プロセスに再利用済みでした。")
            lock.write_text(f"{os.getpid()} {my_birth or 0}", encoding="utf-8")
        except (OSError, ValueError):
            return True  # ロックを触れない環境では従来どおり起動する

        def _release():
            try:
                mine = lock.read_text(encoding="utf-8").split() if lock.exists() else []
                if mine[:2] == [str(os.getpid()), str(my_birth or 0)]:
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
