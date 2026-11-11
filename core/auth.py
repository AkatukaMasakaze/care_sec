"""認証バックエンド(権限機能に依存しない介護士認証)

django.contrib.auth の ModelBackend は User.user_permissions を前提とするため、
PermissionsMixin を持たないカスタムUserでは利用できない。
そのため認証専用のバックエンドを自前で用意する。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend


class CareWorkerBackend(BaseBackend):
    """ユーザー名/パスワードによる認証のみを行う"""

    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None

        try:
            user = User.objects.get(**{User.USERNAME_FIELD: username})
        except User.DoesNotExist:
            # タイミング攻撃対策(存在しない場合もダミー演算を行う)
            User().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        return getattr(user, "is_active", True)

    def get_user(self, user_id):
        User = get_user_model()
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None

    # --- 権限機能を使わないためは全て許可 ---
    def has_perm(self, user_obj, perm, obj=None):
        return True

    def get_all_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()
