"""ロールに基づくアクセス制御

このシステムは権限機能(groups / user_permissions)を持たないカスタムUserを使うため、
**2段階だけ**を `core.User.is_admin` で扱う。

  - 管理者(is_admin=True) : 全機能(カメラデバッグ・Django管理画面を含む)
  - 一般介護士            : 異常検知・録画・報告などの業務画面のみ

`manage.py createsuperuser` は `create_superuser()` 経由で is_admin=True を立てる
(`core/models.py`)ので、既存の運用そのまま管理者を作れる。
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.shortcuts import render


def is_admin(user):
    """そのユーザーが管理者か(未ログインは False)"""
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and getattr(user, "is_admin", False))


def admin_required(view_func):
    """管理者のみを通すデコレータ

    - 未ログイン → ログイン画面へ誘導(next で元のURLを引き継ぐ)
    - 一般介護士 → 403。画面の存在を隠すのではなく理由を明示する
      (誤って「カメラが壊れた」と誤解するのを防ぐため)
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_admin(user):
            return render(
                request,
                "permission_denied.html",
                {"reason": "この画面は管理者のみアクセスできます。"},
                status=403,
            )
        return view_func(request, *args, **kwargs)

    return _wrapped
