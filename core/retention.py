# -*- coding: utf-8 -*-
"""録画の自動削除(プライバシー保存期間 / 2026-11-24)

「全削除」ボタンを押されない環境でも、録画データが無限に蓄積しないようにする。
`settings.RECORDING_RETENTION_DAYS`(既定30日)を経過した Recording を、
DB行 + media/recordings/*.mp4 / *.jpg の実ファイルごと削除する。

走る場所(多重にしても軽量ガード付き):
  - 録画一覧ページ表示時(core/views.py: recording_list)
  - サーバー常駐ループ(core/apps.py の30秒サイクル)
どちらも ``cleanup_expired()`` を呼ぶだけ。间违えても15分間は2回目をスキップし、
削除対象クエリはインデックスなしでも録画件数が小さい実運用で1〜2ms。
"""
import logging
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.utils import timezone

from .models import Recording

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_run_at = [0.0]  # monotonic。多重起動(ページ+常駐ループ)の間引き用


def _media_root():
    return Path(str(django_settings.MEDIA_ROOT)).resolve()


def _delete_media_url(url):
    """/media/... URL の実ファイルを削除する。戻り値: ("deleted"|"absent"|"skip"|"fail")

    - 外部URL(http/https の S3 Presigned 等)はファイルを持たないので skip
    - MEDIA_ROOT の外を指す不正な相対パスは skip(パストラバーサル対策)
    - Windows で他プロセスが開いている場合は fail(次サイクルで再試行される)
    """
    if not url:
        return "skip"
    if url.startswith("http://") or url.startswith("https://"):
        return "skip"
    media_url = (django_settings.MEDIA_URL or "/media/").rstrip("/")
    if not url.startswith(media_url + "/"):
        return "skip"
    rel = urllib.parse.unquote(url[len(media_url) + 1:].split("?", 1)[0])
    root = _media_root()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)  # MEDIA_ROOT の外なら ValueError
    except ValueError:
        return "skip"
    if not target.exists():
        return "absent"
    try:
        target.unlink()
        return "deleted"
    except OSError:
        logger.warning("録画ファイルの削除失敗(他プロセスが開いている?): %s", target)
        return "fail"


def retention_days():
    return int(getattr(django_settings, "RECORDING_RETENTION_DAYS", 30) or 0)


def cleanup_expired(force=False):
    """保存期間超過の録画を削除する。実際に走ったら削除件数を返し、スキップ時 None

    force=True は間引きガードを無視して実行する(テスト・手動実行用)。
    """
    days = retention_days()
    if days <= 0:
        return None

    interval = max(1, int(getattr(django_settings, "RECORDING_CLEANUP_MINUTES", 30))) * 60
    now_mono = time.monotonic()
    if not force and now_mono - _last_run_at[0] < min(interval, 900):
        return None
    with _lock:
        if not force and now_mono - _last_run_at[0] < min(interval, 900):
            return None
        _last_run_at[0] = now_mono

        cutoff = timezone.now() - timedelta(days=days)
        expired = list(Recording.objects.filter(started_at__lt=cutoff))
        if not expired:
            return 0

        deleted_files = 0
        failed_files = 0
        for rec in expired:
            for url in (rec.video_url, rec.thumbnail_url):
                result = _delete_media_url(url)
                if result == "deleted":
                    deleted_files += 1
                elif result == "fail":
                    failed_files += 1
            # アラートのサムネイルが「これから消える録画のjpg」を指している場合、
            # 参照を残すと一覧・詳細が壊れた画像になる。空へ戻す(検知記録自体は残す)
            alert = getattr(rec, "alert", None)
            if alert is not None and alert.thumbnail_url and \
                    alert.thumbnail_url == rec.thumbnail_url:
                alert.thumbnail_url = ""
                alert.save(update_fields=["thumbnail_url"])
            rec.delete()

        print("[Retention] 保存期間%d日を経過した録画 %d件を自動削除しました"
              "(実ファイル %d件削除 / %d件は開かれていて残ったため次回再試行)" % (
                  days, len(expired), deleted_files, failed_files))
        return len(expired)
