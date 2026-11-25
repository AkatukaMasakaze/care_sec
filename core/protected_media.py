# -*- coding: utf-8 -*-
"""media/ 配下のファイルを『ログイン済みユーザーだけ』に配信する(AWS Setup Guide 8.3)

開発の runserver(DEBUG=1)では urls.py の static() がそのまま配信するが、
本番(DEBUG=0)ではこの view が /media/ を受ける。nginx 側は /media/ を
静态配信しないこと(ガイド8.4)。配信対象は MEDIA_ROOT の内側に限定し、
パストラバーサル(/media/../db.sqlite3 等)は 404 で弾く。

録画は1本数百KBの短尺(mp4)なので FileResponse で十分。Range シークが
欲しくなったら nginx X-Accel-Redirect 版(ガイド8.5)へ差し替える。
"""
import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404


@login_required
def protected_media(request, path):
    root = Path(str(settings.MEDIA_ROOT)).resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)          # MEDIA_ROOT の外なら ValueError
    except ValueError:
        raise Http404("media 外を指定できません")
    if not target.is_file():
        raise Http404("ファイルが存在しません")

    ctype, _ = mimetypes.guess_type(str(target))
    response = FileResponse(
        target.open("rb"),
        content_type=ctype or "application/octet-stream",
    )
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Cache-Control"] = "private, max-age=300"   # ブラウザだけ短期キャッシュ
    return response
