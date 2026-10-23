# CARE SEC 転倒・異常検知システム(検証環境)

介護施設の利用者の転倒・異常をAIカメラで検知するWebツールです。
Django **6.1.2** / Python **3.13** / SQLite でセットアップ済みです。

## 起動方法

```powershell
cd C:\Users\student\code\ai2
.venv\Scripts\Activate.ps1     # venvを有効化(しなくても .venv\Scripts\python.exe 直指定でもOK)
python manage.py runserver 127.0.0.1:8000
```

ブラウザで <http://127.0.0.1:8000/> を開く。
ローカルネットワークからは `runserver 0.0.0.0:8000` で公開できます(防火墙設定が必要)。

## ログインアカウント(デモデータ)

| ユーザー名 | パスワード | 役割 |
|---|---|---|
| `staff1` | `demo1234` | 山田 花子(介護スタッフ) |
| `staff2` | `demo1234` | 佐藤 二郎(介護スタッフ) |
| `admin`  | `admin1234` | 管理画面(`/admin/`)も利用できる介護士 |

## 権限・グループ機能について(廃止済み)

本システムは**介護士のみが使用する**前提のため、権限機能を全面的に撤去しています。

| 項目 | 実装 |
|---|---|
| ユーザーモデル | `core.User`(Django標準User不使用)。`is_superuser` / `groups` / `user_permissions` を**持たない** |
| 認証 | `core.auth.CareWorkerBackend`(権限に依存しない専用バックエンド) |
| 管理画面 | `core.admin.CareSecAdminSite`。**ログイン中の介護士全員が同等にアクセス可能**(閲覧/追加/変更/削除の権限チェックは全て無効化) |
| 権限レコード | 全モデルに `default_permissions = ()` を設定し、`auth_permission` に**業務モデルの権限行を生成しない** |
| ユーザー追加 | `/admin/core/user/add/` から実施(パスワードはハッシュ化して保存) |

残る `django_content_type` / `auth_permission` / `auth_group` テーブルは
Djangoフレームワーク内部(管理画面の操作ログ用)のため残りますが、
**当システムの権限判定には一切使用していません**。

## 便利な管理コマンド

```powershell
python manage.py seed_demo           # 検証用デモデータを投入(カメラ4台/利用者6名/異常8件/録画4本/報告2件)
python manage.py seed_demo --reset   # デモデータを削除して再投入
python manage.py verify_pages        # 全ページ+「対応→解決→報告」フローの自動検証
```

## データベースについて

| 項目 | 内容 |
|---|---|
| DBファイル | `db.sqlite3`(SQLite) |
| 暗号化 | **テスト中は暗号化しない**(平文のまま運用) |
| 暗号化する場合 | `python encrypt_db.py encrypt` / 復号は `python encrypt_db.py decrypt` |
| グループ関連テーブル | `auth_group` / `auth_group_permissions` は**削除済み**(未使用のため) |

### テーブル構成(全13個)

**業務データ(本システム)**

| テーブル | 行数 | 目的 |
|---|---|---|
| `core_user` | 3 | 介護士アカウント。権限列なし |
| `core_camera` | 4 | 監視カメラ台帳(名前/設置場所/online) |
| `core_resident` | 6 | 介護施設の利用者名簿 |
| `core_alert` | 8 | 異常検知の記録(種別/状況/信頼度/時刻ログ/対応者) |
| `core_recording` | 4 | 自動録画(検知前後の映像とAI検知マーク) |
| `core_alertreport` | 2 | 解決報告(誰だったか/身体的問題/対応内容/コメント) |
| `core_persondetection` | 65 | AIのライブ人物検知状態(揮発性) |
| `core_aisettings` | 1 | AI検知パラメータ(シングルトン/誤検知FBで自動調整) |

**Djangoフレームワーク(内部管理用)**

| テーブル | 行数 | 目的 |
|---|---|---|
| `django_content_type` | 13 | モデルのレジストリ(管理画面の操作ログのFK先) |
| `auth_permission` | 20 | Django内部の権限定義。`migrate` 実行時に再生成されるため**削除不可**(当システムの判定には不使用) |
| `django_admin_log` | 1 | 管理画面の操作履歴(誰が何を変更したか) |
| `django_session` | 8 | ログインセッション |
| `django_migrations` | 19 | 適用済みマイグレーションの履歴 |

## アドレス(URL)一覧

| アドレス | 内容 |
|---|---|
| `/` | ダッシュボード |
| `/accounts/login/` `/accounts/logout/` | ログイン / ログアウト |
| `/accounts/profile/` | プロフィール+解決報告入力 |
| `/alerts/` | 異常検知(対応する / 解決・報告を入力) |
| `/recordings/` `/recordings/<id>/` | 自動録画の一覧 / 再生 |
| `/api/alerts/` | リアルタイム通知用JSON(5秒ポーリング) |
| `/admin/` | Django管理画面 |

詳細なURL設計・コンテキスト変数の仕様は **URLS.md** を参照してください。

## 構成

```
config/     … settings / urls(全URLは config/urls.py に集約)
core/       … models / views / admin / auth / context_processors / management commands
templates/  … 画面テンプレート(base.html が情報バー+メニュー+MAINの共通レイアウト)
static/     … css / js / img(依存ライブラリなし)
db.sqlite3  … SQLiteデータベース
```

## 動作のポイント

- **異常検知タブ**で「対応する」を押すと対応者が確定し、`/api/alerts/` ポーリングにより
  開いている全画面に「誰が対応中か」が自動反映されます(新規異常は通知音+トースト)。
- 対応者本人だけが「解決・報告を入力」を押せ、プロフィールページの報告フォームへ遷移します。
- 録画再生ページにはAI検知マーク(例: 「転倒の瞬間 00:18」)があり、クリックでその瞬間へジャンプします。

