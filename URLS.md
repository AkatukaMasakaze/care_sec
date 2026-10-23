# CARE SEC 転倒・異常検知システム — URL設計 & Django実装メモ

> **認証について**: 本システムは介護士のみが使用する前提のため権限機能を撤去しています。
> `AUTH_USER_MODEL = "core.User"` / `AUTHENTICATION_BACKENDS = ["core.auth.CareWorkerBackend"]` /
> 管理画面は `core.admin.CareSecAdminSite`(権限チェック無効)を使用します。

テンプレートは `templates/`、静的ファイルは `static/` に配置済みです。
このドキュメントの通りに `urls.py` / `settings.py` / ビューを用意すれば、そのまま動きます。

---

## 1. アドレス(URL)一覧

| アドレス (URL) | URL名 (`name`) | Method | 内容 | テンプレート |
|---|---|---|---|---|
| `/` | `dashboard` | GET | ダッシュボード(全体まとめ) | `dashboard.html` |
| `/accounts/login/` | `login` | GET/POST | ログイン | `accounts/login.html` |
| `/accounts/logout/` | `logout` | POST | ログアウト | `accounts/logout.html` |
| `/accounts/profile/` | `profile` | GET | プロフィール+解決報告入力 | `accounts/profile.html` |
| `/reports/create/` | `report_create` | POST | 解決報告の保存 | (profileへリダイレクト) |

| `/alerts/` | `alert_list` | GET | 異常検知タブ(`?status=open` 等でフィルタ) | `alerts/list.html` |
| `/alerts/<int:alert_id>/respond/` | `alert_respond` | POST | 「対応する」= 自分が対応者になる | (alert_listへリダイレクト) |
| `/alerts/<int:alert_id>/resolve/` | `alert_resolve` | POST | 「解決」= 解決済みにして報告入力へ | (profileへリダイレクト) |
| `/recordings/` | `recording_list` | GET | 自動録画の一覧(`?kind=fall` 等でフィルタ) | `recordings/list.html` |
| `/recordings/<int:recording_id>/` | `recording_detail` | GET | 録画再生(検知マークジャンプ付き) | `recordings/detail.html` |
| `/api/alerts/` | `api_alerts` | GET | 異常ポーリング用JSON(リアルタイム通知) | なし(JSON) |


ログイン必須にするビューには `@login_required` を付けてください
(`dashboard`, `profile`, `camera_live`, `alert_list`, `alert_respond`, `alert_resolve`, `recording_*`, `report_create`)。

---

## 2. settings.py への追記

```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

TEMPLATES[0]['DIRS'] = [BASE_DIR / 'templates']   # 既存のTEMPLATES設定内

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
# 本番環境では STATIC_ROOT = BASE_DIR / 'staticfiles' も追加

# 認証まわり
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'            # ログイン成功後はダッシュボード
LOGOUT_REDIRECT_URL = '/accounts/login/'
```

---

## 3. urls.py の例(プロジェクト直下に1本でまとめる場合)

```python
# config/urls.py (プロジェクトのルートurls)
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('admin/', admin.site.urls),

    # ダッシュボード
    path('', views.dashboard, name='dashboard'),

    # 認証
    path('accounts/login/',
         auth_views.LoginView.as_view(template_name='accounts/login.html'),
         name='login'),
    path('accounts/logout/',
         auth_views.LogoutView.as_view(template_name='accounts/logout.html'),
         name='logout'),
    path('accounts/profile/', views.profile, name='profile'),
    path('reports/create/', views.report_create, name='report_create'),



    # 異常検知
    path('alerts/', views.alert_list, name='alert_list'),
    path('alerts/<int:alert_id>/respond/', views.alert_respond, name='alert_respond'),
    path('alerts/<int:alert_id>/resolve/', views.alert_resolve, name='alert_resolve'),

    # 録画
    path('recordings/', views.recording_list, name='recording_list'),
    path('recordings/<int:recording_id>/', views.recording_detail, name='recording_detail'),

    # API
    path('api/alerts/', views.api_alerts, name='api_alerts'),

]
```

アプリ分割する場合(`accounts` / `alerts` / `cameras` / `recordings` アプリ等)は、
上記を各アプリの `urls.py` に振り分けて `include()` するだけでOKです。
テンプレート側は `{% url '名前' %}` の**名前解決**しか使っていないので、パスを変えても名前さえ同じなら動き続けます。

### ビューの実装ポイント

- `alert_respond`: POSTのみ許可。`alert.responder = request.user; alert.status = 'responding'` で保存し `alert_list` へリダイレクト。
- `alert_resolve`: POSTのみ許可かつ `alert.responder == request.user` のみ。`alert.status = 'resolved'` にして
  `redirect(f'/accounts/profile/?alert={alert.id}')` でプロフィール(報告フォーム)へ飛ばす。
- `profile`: `request.GET.get('alert')` があれば `target_alert` として渡す(自分が対応者で未報告のものに限る)。
- `report_create`: POSTを保存後、`profile` へリダイレクト。
- `api_alerts`: 下記JSONを返す(認証必須)。

```json
{
  "open_count": 2,
  "alerts": [
    {
      "id": 1,
      "kind_label": "転倒",
      "status": "open",
      "status_label": "未対応",
      "responder_name": null,
      "location": "1F 廊下"
    }
  ]
}
```

`status` / `status_label` の値は `open/未対応`・`responding/対応中`・`resolved/解決済み` の3種を想定しています
(CSSのチップ色 `chip--open / chip--responding / chip--resolved` がこれに対応)。

---

## 4. テンプレートが期待するコンテキスト変数

### 全ページ(base.html)
| 変数 | 型 | 内容 |
|---|---|---|
| `open_alert_count` | int | 未対応の異常件数(トップバー&サイドバーのバッジ) |
| `latest_open_alert` | object\|None | 最新未対応異常。`kind_label / location / occurred_at` を使用(バナー表示) |

> 全ページで使うため、コンテキストプロセッサで渡すのがおすすめです:
> ```python
> # core/context_processors.py
> def alert_globals(request):
>     if not request.user.is_authenticated:
>         return {}
>     latest = Alert.objects.filter(status='open').order_by('-occurred_at').first()
>     return {
>         'open_alert_count': Alert.objects.filter(status='open').count(),
>         'latest_open_alert': latest,
>     }
> # settings.py: TEMPLATES[0]['OPTIONS']['context_processors'] に 'core.context_processors.alert_globals' を追加
> ```

### dashboard.html
| 変数 | 内容 |
|---|---|
| `stats` | dict: `today_alerts / yesterday_alerts / open_alerts / residents / cameras_online / cameras_total` |
| `recent_alerts` | 直近の異常リスト(`occurred_at / location / kind_label / status / status_label`) |
| `weekly_chart` | 直近7日リスト: `{"label": "10/2", "count": 3, "height_percent": 60}`(height_percentは最大値を100とした比率) |
| `recent_recordings` | 直近の録画リスト(`id / started_at / kind_label / camera_name / duration_label`) |
| `cameras` | カメラリスト(`name / location / is_online`) |



### alerts/list.html
| 変数 | 内容 |
|---|---|
| `alerts` | `id / kind_label / occurred_at / camera_name / location / status / status_label / responder(User\|None) / thumbnail_url / recording_id` |
| `current_status` | 現在のフィルタ文字列(`'' / 'open' / 'responding' / 'resolved'`) |

### accounts/profile.html
| 変数 | 内容 |
|---|---|
| `profile` | dict: `role / facility / resolved_count`(任意。無くても表示は崩れません) |
| `target_alert` | 報告対象の異常(`resolve` 後のリダイレクト時)。`recording_id` も使用 |
| `residents` | 利用者リスト: `id / name / room_number`(報告フォームの選択肢) |
| `my_reports` | 自分の報告履歴: `created_at / alert.kind_label / resident_name / physical_issue / physical_issue_label / action_taken / comment` |

### recordings/list.html / detail.html
| 変数 | 内容 |
|---|---|
| `recordings` | `id / started_at / kind(fall\|suspicious) / kind_label / camera_name / location / duration_label / thumbnail_url / alert_id` |
| `current_kind` | フィルタ文字列(`'' / 'fall' / 'suspicious'`) |
| `recording` | 上記+`ended_at / video_url / reason` |
| `markers` | 検知マーク: `seconds / label / time_label`(例: `{"seconds": 12, "label": "転倒の瞬間", "time_label": "00:12"}`) |
| `related_alert` | 関連異常(任意) |
| `related_report` | 解決報告(任意): `author / resident_name / physical_issue_label / action_taken / comment` |

### accounts/login.html
Django標準の `AuthenticationForm` が渡す `form` / `next` をそのまま使用します
(`LoginView.as_view(template_name='accounts/login.html')` でそのまま動きます)。

---

## 5. リアルタイム性の拡張メモ

- 現状は **5秒間隔ポーリング**(`/api/alerts/`)で「新規異常の通知音+トースト」「対応者の全員共有表示」を実現しています。
- より即時性が必要になったら **Django Channels(WebSocket)** に差し替え可能です。
  `static/js/app.js` の `poll()` 部分を WebSocket購読に置き換えて、同じ `applyAlertData(data)` を呼べばUIはそのまま動きます。
