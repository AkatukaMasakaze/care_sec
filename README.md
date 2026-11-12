# CARE SEC 転倒・異常検知システム(検証環境)

介護施設の利用者の転倒・異常をAIカメラで検知するWebツールです。
Django **6.1.2** / Python **3.13** / SQLite でセットアップ済みです。

## 起動方法

```powershell
cd C:\Users\student\code\ai2
.venv\Scripts\Activate.ps1     # venvを有効化(しなくても .venv\Scripts\python.exe 直指定でもOK)
python -m pip install -r requirements-windows.txt   # 初回のみ(依存ライブラリの導入)
python manage.py runserver 127.0.0.1:8000
```

依存ライブラリの導入には **tensorflow / mediapipe を含むため数分と数百MB** を要します
(`.venv` には導入済み)。Linux本番環境は `requirements.txt` を参照してください。

### requirements.txt / requirements-windows.txt の同期状態(2026-11-09 検証)

| パッケージ | Windows開発(実venvと一致) | Linux本番 |
|---|---|---|
| Django / asgiref / sqlparse / tzdata | 6.1.2 / 3.12.1 / 0.6.0 / 2026.5 | 同一 |
| opencv-contrib-python | 5.0.0.93 | 同一 |
| mediapipe / pillow / imageio-ffmpeg | 0.10.32 / 12.3.0 / 0.8.1 | 同一 |
| pandas / scikit-learn(再学習用) | 3.1.1 / 1.9.1 | 同一 |
| **tensorflow** | **2.21.0** | **2.20.0** |
| gunicorn | —(不要) | 23.0.0 |

- **意図した差分は tensorflow のみ**。`2.21.0` には **Linux用wheelが存在しない**(Linuxの最新は2.20.0)ため、
  本番は 2.20.0、開発のWindowsは 2.21.0 を使います(同一APIで動作)
- 検証方法: `pip install --dry-run --only-binary=:all: --platform <win_amd64|manylinux_*> --python-version 3.12/3.13 -r <file>`
  → Linux(py3.12/3.13)・Windows(cp313)の3通りで解決OK
- アプリのimport走査結果: `django / cv2 / PIL / numpy / tensorflow / mediapipe / imageio_ffmpeg` — すべて記載済み(`numpy`はTF/mediapipeの推移依存)
- **py-spy** は開発venvにのみ入っている調査用ツール(アプリ動作に不要、本番では入れない)

ブラウザで <http://127.0.0.1:8000/> を開く。

ローカルネットワークから使う場合は `runserver 0.0.0.0:8000` に加えて、
**そのマシン宛てのホスト名/IPを環境変数で渡す**必要があります(次節)。

## 環境変数(config/settings.py が読むもの)

| 変数 | 既定 | 効果 |
|---|---|---|
| `CARE_SEC_SECRET_KEY` | `dev-only-not-for-production` | `SECRET_KEY`。**本番では必ず設定**し、既定値を公開リポジトリに晒した場合は必ず回す |
| `CARE_SEC_DEBUG` | `1` (=DEBUG True) | `0` にすると DEBUG=False になる(静的ファイルは collectstatic が必要) |
| `CARE_SEC_ALLOWED_HOSTS` | 空 | カンマ区切りの追加ホスト。**各要素の前後の空白は自動で除かれる**(先頭スペースで無効化していた過去の実測バグの再発防止)。`127.0.0.1` / `localhost` / `testserver` は常に許可 |
| `CARE_SEC_EMAIL_HOST` / `CARE_SEC_EMAIL_PORT` / `CARE_SEC_FROM_EMAIL` | localhost / 25 / care-sec@localhost | `EMAIL_BACKEND` は既定が console(送信内容コンソール表示)。SMTPへ出したい時だけ設定する |

```powershell
# LAN からのアクセスを許可する例(このPCのIP 10.24.23.42 など)
$env:CARE_SEC_ALLOWED_HOSTS = "10.24.23.42, .local"
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

未指定で `0.0.0.0` にバインドすると、LAN IP でのアクセスは `DisallowedHost`(400)になります。
`DEBUG=True` でも `ALLOWED_HOSTS` に値があれば**厳格に適用**される点に注意(実測)。

## ログインアカウント(デモデータ)

| ユーザー名 | パスワード | 役割 |
|---|---|---|
| `staff1` | `demo1234` | 山田 花子(介護スタッフ) |
| `staff2` | `demo1234` | 佐藤 二郎(介護スタッフ) |
| `admin`  | `admin1234` | 管理画面(`/admin/`)も利用できる介護士 |

## 権限・グループ機能について(2段階ロールのみ)

Django標準の権限機能(groups / user_permissions)は使わず、**`core.User.is_admin` による
2段階だけ**で制御しています(`core/permissions.py`)。

| 項目 | 実装 |
|---|---|
| ユーザーモデル | `core.User`(Django標準User不使用)。`groups` / `user_permissions` を**持たない**。代わりに `is_admin` を持つ。`is_staff` / `is_superuser` は **`is_admin` を返すプロパティ** |
| 認証 | `core.auth.CareWorkerBackend`(パスワード認証のみ。`has_perm` は常に True = 権限表は参照しない) |
| **管理者**(`is_admin=True`) | 全機能。カメラデバッグ・Django管理画面も可利用 |
| **一般介護士**(`is_admin=False`) | 業務画面(異常検知・録画・報告・プロフィール・設定)のみ。**カメラデバッグと管理画面は 403** |
| カメラデバッグ | `/debug/camera/` とその配下6エンドポイント(`devices` / `progress` / `ai` / `readiness` / `stream`)は `@admin_required`。サイドバーのリンクも管理者にしか出ないが、**直URLでも入れない** |
| Django管理画面 | `CareSecAdminSite.has_permission` が `is_admin` を要求。加えて `admin_view` を上書きし、Django既定の「権限なし→ログイン画面へ302誘導」を**403に変換**している(確定しているユーザーにログインを要求しない) |
| 権限レコード | 全モデルに `default_permissions = ()` を設定し、`auth_permission` に**業務モデルの権限行を生成しない** |
| ユーザー追加 | `/admin/core/user/add/`(管理者のみ)。`manage.py createsuperuser` は `create_superuser()` 経由で `is_admin=True` を立てるので、この手順のまま管理者を作れる |

### 既存アカウントの格上げ / 降格

```powershell
python manage.py shell -c "from core.models import User; u=User.objects.get(username='user'); u.is_admin=True; u.save(update_fields=['is_admin'])"
```
管理画面 `/admin/core/user/` の「管理者使用する」チェックでも可。

### 403 の見え方
`templates/permission_denied.html` を返し、理由(「この画面は管理者のみアクセスできます」)と
現在のアカウント・管理者権限の有無を表示します。**ページ存在を隠すのではなく理由を出す**ので、
「カメラが壊れた」との誤解を防げます。未ログインの人は従来どおり `next` を引き継いでログイン画面へ誘導します。

> **注意**: `/accounts/settings/`(データ全削除・AI監視ON/OFF・異常検知デモ)は
> 現在**一般介護士も利用できます**。ここも管理者専用にしたい場合は言ってください
> (`@admin_required` を1行足すだけです)。

残る `django_content_type` / `auth_permission` / `auth_group` テーブルは
Djangoフレームワーク内部(管理画面の操作ログ用)のため残りますが、
**当システムの権限判定には一切使用していません**(判定は `is_admin` のみ)。

## 便利な管理コマンド

```powershell
python manage.py seed_demo           # 初期データ投入(アカウント3名/カメラ台帳4台/利用者6名)
python manage.py seed_demo --reset   # カメラ台帳・利用者を削除して再投入
```

> `seed_demo` は **アカウント・台帳・利用者だけ**を投入します。
> 偽の異常検知/録画/解決報告を作る処理は削除済みなので、実行してもアラートは増えません。

```powershell
python manage.py verify_pages        # 全ページ+「対応→解決→報告」フローの自動検証
```

## データベースについて

| 項目 | 内容 |
|---|---|
| DBファイル | `db.sqlite3`(SQLite) |
| 暗号化 | **テスト中は暗号化しない**(平文のまま運用) |
| 暗号化する場合 | `python encrypt_db.py encrypt` / 復号は `python encrypt_db.py decrypt` |
| グループ関連テーブル | `auth_group` / `auth_group_permissions` は**削除済み**(未使用のため) |

### テーブル構成(全12個)

**業務データ(本システム)**

| テーブル | 行数 | 目的 |
|---|---|---|
| `core_user` | 3 | 介護士アカウント。権限列なし |
| `core_camera` | 7 | 監視カメラ台帳(名前/設置場所/online/**stream_url=IPカメラ**) |
| `core_resident` | 6 | 介護施設の利用者名簿 |
| `core_alert` | 7 | 異常検知の記録(種別/状況/信頼度/時刻ログ/対応者)。**実AIの検知のみが登録される** |
| `core_recording` | 7 | 自動録画(検知後の映像とAI検知マーク) |
| `core_alertreport` | 0 | 解決報告(誰だったか/身体的問題/対応内容/コメント) |
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
| `/debug/camera/` | カメラデバッグ(デバイス一覧・**全カメラ一括カード表示**・現在時刻/各カメラの遅延) |
| `/debug/camera/devices/` | カメラデバイス一覧JSON |
| `/debug/camera/ai/` | リアルタイムAI解析の状態JSON(**`frame_age`=映像の遅延秒**、`devices`で全カメラ) |
| `/debug/camera/stream/` | MJPEGストリーム(AI解析overlay付き) |

> **注意**: `camera_service` の `_streams` / 検知キャッシュは**プロセス内の変数**です。
> 別プロセスから `import camera_service` して状態を覗くと空(`streams: []`)に見えて
> 誤診します。稼働中の状態は必ず `/debug/camera/ai/` から見る。

詳細なURL設計・コンテキスト変数の仕様は **URLS.md** を参照してください。

## ダッシュボードの「1ページに収める」レイアウトと一覧のスクロール

`@media (min-width: 901px)` で、ダッシュボード(`.main > .stats-grid` を持つページ)は
**画面高さ内に収める**設計です(`body { overflow:hidden }` + `.main { height: calc(100vh - topbar) }`)。

そのため、一覧が伸びるカードは次の連鎖で高さを受け取ります。

```
.main(100vh) → .grid-2(flex:1 1 0; min-height:0) → .card(min-height:0; overflow:hidden)
              → .mini-list(flex:1 1 auto; min-height:0)
```

**注意(実測で踏んだ)**: `.card` が `overflow:hidden` なので、**はみ出した行はスクロール先が無く切り捨てられて見えません**。
カメラ台数は `Camera.objects.all()` で無制限なので、カメラを増やすと下の方が見えない状態になっていました。
現在は `.mini-list` 自体に `overflow-y: auto` を持たせ、**その一覧だけ**がスクロールします
(`overscroll-behavior: contain` でページ本体には干渉しない)。

| 一覧 | 件数 | 挙動 |
|---|---|---|
| カメラ状態 | **無制限** | 少ないときは行を伸ばして埋める / `min-height:34px` までしか縮めない / 溢れたら一覧内スクロール。ヘッダに「N台」を表示 |
| 直近の異常 / 直近の録画 | 5件 / 4件で打ち切り | 同じ共通ルールで、縦が足りない環境ではスクロールに出る |

カメラを増やして見えない場合は、**ページ全体がスクロールできないのが正常**で、カード内を一覧スクロールします。

## リアルタイムAI解析の統合(caregiver_safety)

`/debug/camera/` のライブ表示に、介護者安全監視システム **caregiver_safety** の
リアルタイム判定パイプラインをそのまま載せています。

```
カメラ → MediaPipe Pose(33ランドマーク) → 直近30フレームの姿勢系列(99値)
       → 転倒判定LSTM / 状態判定LSTM
       → 骨格・判定overlayを描画してMJPEG配信
       → 転倒確定 / 無動作継続で Alert を作成(既存の通知フローへ)
```

### 仕組み

| 項目 | 実装 |
|---|---|
| 解析本体 | `core/ai_engine.py`(**カメラ1台につき1スレッド**。`_CameraRuntime` が台数分並び、`RealtimeAIEngine` がまとめて管理) |
| 常時稼働 | `AI_ALWAYS_ON = True`(既定)で **runserver 起動時に検知できた全カメラ**の解析を開始。`/debug/camera/` を開かなくても・映像を「停止」しても異常検知と録画は動き続けます(`core/apps.py` がサーバー本体プロセスでのみ起動。後から差し込まれたカメラは定期スキャンで自動追加) |
| 映像の入出力 | `core/camera_service.py` のキャプチャを共有(カメラ毎に独立)。解析済みJPEGを `/debug/camera/stream/?device=N` が配信。**IPカメラは専有読取スレッドが最新1枚だけ保持**するので遅延が溜まらない(下記「IPカメラ」章) |
| デバイス検知 | **非同期**(検知は別スレッド)。`/debug/camera/devices/` は待たずに `{"pending": true}` を返し、`/debug/camera/progress/` を400msポーリングして進行バー+既に分かったカメラを先に表示。**完了時は progress が返す最終一覧を使い、devices/ を引き直さない**(引き直すと検知が再発走する) |
| 検知のハング抑制 | 存在しない/占有中の index を開くと Windows では約60秒ハングし、そのスレッドが溜まると**配信中のカメラまで映像が止まる**(実測)。そこで ①同じ index のプローブは生存中は再起動しない ②開けなかった index は `DEBUG_CAMERA_PROBE_RETRY_SECONDS`(既定120秒)再試行しない。③既に `camera_service` が掴んでいる index はプローブせず台帳情報で出す。「再検知」ボタンだけ抑止を解く |
| 検知範囲はOS実台数から | index を固定本数(0..N)で総当たりせず、**OSのカメラ列挙(PnP / video4linux)で「接続中何台か」を取ってその台数分だけ**探す(`DEBUG_CAMERA_AUTO_RANGE`)。Linux では codec/ISP ノードを除外し、同じ物理デバイスの複数ノードは1台に丸める。OS列挙が使えない場合だけ上限値へフォールバック。根拠は準備ログにも出る(`検知範囲: OS検出 1台 → index 0..0 を確認`) |
| モデル | `ai_models/`(caregiver_safety からコピー。詳細は `ai_models/README.md`)。Kerasモデルは全カメラ共有(推論はロックで直列化)、MediaPipe はスレッドセーフではないため **カメラごとに1個** 生成 |
| 状態表示 | `/debug/camera/ai/` を1秒ポーリング(`static/js/debug_camera.js`)。**`?device=N` でそのカメラ**、無しは最も稼働中のカメラ+全カメラの要約。ページ下部に「全カメラの稼働状況」一覧 |
| **全カメラ一括表示** | 「全カメラライブ(一括表示)」セクションが **検知した全カメラをカード形式で同時配信**(1台ずつ切り替える必要なし)。各カードに 判定/状態/解析fps/**遅延**/無動作/視聴 を出す。**接続するのは解析が `running` になったカメラだけ**(待機中のカメラへ先に接続すると初フレームまでHTTPがハングし、ブラウザの同一サーバー6接続上限を食う)。未接続のカードは理由を表示(「AIモデル準備中…」/「このカメラはまだ解析されていません」)。「すべて停止」で全接続をクローズ |
| 現在時刻 | ページ上部に `2026-11-10 14:28:31`(この端末のタイムゾーン)を**秒まで**表示(250ms更新)。各カードの右上は **そのフレームの撮像時刻(秒まで)** で、`現在時刻 − 撮像時刻 = 映像の遅延`。遅延が0.5秒超で黄・1.5秒超で赤くなる(カードと撮像時刻が色付く) |
| 接続数の注意 | MJPEGは1カード=1接続。ブラウザは同一サーバーへ同時6接続までなので、**7台以上は「すべて停止」して必要なカメラだけ**表示する(「すべて停止」で全接続を切る) |
| 依存ライブラリ | tensorflow / mediapipe / pillow / **imageio-ffmpeg**(H.264 mp4書き出し用。`requirements*.txt` に追加済み) |

### 異常検知(ai2 の方式)

| 検知 | Alert種別 | 条件 |
|---|---|---|
| 転倒 | `fall` | 転倒確率が **AI設定の転倒検知信頼度閾値**(既定0.75)以上で `FALL_CONFIRM_COUNT = 3` 回連続 |
| 無動作 | `suspicious` | ランドマークの移動がほぼゼロで **AI設定の無動作検知秒数**(既定30秒)以上 |

作成した `Alert` は `core_alert` に載るため、
**`/api/alerts/` の5秒ポーリング → 通知音・トースト・OS通知 → 異常検知一覧の対応/解決/報告**
がそのまま機能します。クールダウンは `AISettings.cooldown_seconds`(**現状10秒**で運用。既定モデル値は300秒)を適用します。

### 異常検知からの自動録画

検知(転倒・無動作)を確認した時点で **`AI_RECORDING_SECONDS`(既定8秒)間**フレームを確保し、
H.264 mp4 + サムネイルとして `media/recordings/` に書き出したうえ `Recording` を作成します。

- 録画中は `/debug/camera/` の「自動録画」欄に残り秒数が表示されます
- 書き出しは別スレッド(`ai-recording-writer`)で行うため、解析レート(約10fps)は落ちません
- 作成された録画は **異常検知一覧・詳細の「🎞 検知瞬間の録画を再生」リンク**や `/recordings/` から再生できます
- mp4は `MEDIA_ROOT`(`BASE_DIR / "media"`)へ保存し、`/media/` URLで配信します(`DEBUG=True` のみ)

### media/ の掃除(設定ページの「全削除」)

**/accounts/settings/ の「🗑️ 全異常・録画・解決報告を削除」** は、DB行を消すだけでなく
`media/` 配下の**ファイルをすべて削除**します(`alert_thumbs`/*.jpg、`camera_snapshots`/*.jpg、
`recordings`/*.mp4、入れ子ディレクトリの内側、`media` 直下のファイルも対象)。

- 検知データと対応付けられているかどうかでは**絞り込みません**(DB行を消した後はすべて残骸のため)
- **ディレクトリ自体は残します**(`recordings/` などが無くなると検知時の保存が失敗する)
- 削除できなかったファイル(他プロセスが開いている等)は処理を止めず、件数をメッセージで表示
- 実行後は次の検知でまたファイルが溜まっていきます。恒久的な運用にするなら
  cron 相当の定期削除や保存期間上限の追加が別途必要

実装は `core/views.py` の `purge_media_files()`。

アラートの紐づけ先カメラは `settings.AI_ALERT_CAMERA_ID` で指定でき、
未指定の場合はデバイスごとに「ライブカメラ0」等を自動作成します。

### 設定(config/settings.py)

| 設定 | 既定 | 内容 |
|---|---|---|
| `AI_REALTIME_ENABLED` | `True` | `False` にすると従来どおり生カメラ映像のみ |
| `AI_MODEL_DIR` | `BASE_DIR / "ai_models"` | モデルの格納先 |
| `AI_ANALYSIS_FPS` | `30` | 解析レート上限。**学習データ(collect_data)が30fps前提**なので、低くしすぎると時間スケールがずれる(実測: 2台同時で27fps出た) |
| `AI_JPEG_QUALITY` | `80` | 配信JPEG品質 |
| `AI_MIRROR_INPUT` | `False` | 映像を左右反転して推論・表示するか(**学習データは非反転なので既定Falseで揃える**) |
| `AI_ALERT_CAMERA_ID` | `None` | アラート紐づけカメラ(未指定=自動作成) |
| `DEBUG_CAMERA_NAME_MAP` | `{0: 内蔵…, 1: 外付け…}` | OpenCV index → 運用上のカメラ名(OS列挙順とindex順は一致しないため明示する) |
| `DEBUG_CAMERA_PROBE_RETRY_SECONDS` | `120` | **開けなかったindexを再試行するまでの間隔**。存在しないindexを開くとWindowsで約60秒ハングし、それが溜まると配信中のカメラまで止まるため、短期間で何度も突かない(「再検知」ボタンは即解除) |
| `DEBUG_CAMERA_AUTO_RANGE` | `True` | **検知するindex範囲をOS実台数から自動で決める**。OSが「接続中1台」と言えば index 0 だけを探す。存在しないindexを突かないので「カメラを開けませんでした(未接続の可能性)」という**存在しないものへのエラーが並ばない**(実測: 1台のPCで index 1..3 が毎回失敗していた) |
| `DEBUG_CAMERA_PROBE_EXTRA` | `0` | OS実台数に加えて余分に探すindex数。OpenCVの採番がOS列挙とずれる環境用の保険(大きくすると失敗ログが増える) |
| `DEBUG_CAMERA_MAX_DEVICES` | `8` | 探索indexの**上限**(OS台数はこちらで頭打ち)。6台以上載せるなら増やす。`AUTO_RANGE=False` にするとこの数まで一律に探す従来動作 |
| `DEBUG_CAMERA_CACHE_SECONDS` | `30` | 検知結果キャッシュ |
| `AI_ALWAYS_ON` | `True` | 画面を開いていなくても常時解析するか(runserver起動時に自動開始) |
| `AI_ALWAYS_DEVICES` | `None` | 常時稼働の対象index一覧(**None=検知できた全カメラを同時解析**) |
| `AI_CAMERA_PRIORITY` | `None` | カメラを**開く順序=優先度**(index降順)。USB的に同時不可なPCで効く。例 `[0, 1]` で内蔵優先 |
| `AI_ALWAYS_RETRY_SECONDS` | `30` | 常時稼働の再スキャン間隔(後差し込みカメラを自動追加 / 未接続でもアプリは正常) |
| `AI_FRAME_LOST_SECONDS` | `20` | 映像がこの秒数**来ない**カメラの解析を止める(1fpsでも来ていれば止まらない) |
| `AI_STARVED_RETRY_SECONDS` | `60` | 停止したカメラの再試行まで抑止する時間 |
| `AI_CAMERA_FIRST_FRAME_SECONDS` | `3.0` | 検知(一覧作成)で初フレームを待つ上限 |
| `AI_CAMERA_OPEN_SECONDS` | `10.0` | 配信・常時稼働で初フレームを待つ上限(2台目は約1秒遅れるため長め) |
| `AI_STREAM_IDLE_SECONDS` | `60` | ライブ配信がフレームを1つも取れないまま続ける上限(これ未満なら切らない) |
| `AI_RECORDING_SECONDS` | `8` | 検知後の録画秒数(0で録画しない) |

> **注意(2026-11-12 更新)**: 自動で擬似アラートを作る機構
> (`core/ai_simulator.py` / `core/ai_monitor.py`)は**削除したまま**です。
> 設定ページに **「🚨 デモの異常検知を1件発生させる」** ボタンだけを戻しました
> (`core/demo_alerts.py` / action `demo_alert`)。**押したときだけ**1件増え、
> 自動生成・定期実行はしません。
>
> - 通知経路(5秒ポーリング → トースト/音/OS通知)と「対応 → 解決・報告」の確認用
> - ランダムな種類(転倒/あやしい動き/立ち入り禁止ゾーン)と信頼度 70〜99%
> - `Recording` は作らない(実体のない mp4 をリンクしないため、サムネイルは代替SVG)
> - デモのアラートで「誤検知として報告」を押すと `AISettings` の感度が自動調整される
> - 作ったデモは設定ページの「全削除」でまとめて消せます

### 起動準備ゲージ(`/debug/camera/` の「起動準備」)

runserver を起動しても映像が出るまで**20秒前後**かかります。従来の「デバイス検知」ゲージは
index を探す進捗だけで、**AIモデル準備(TensorFlow の import + Keras load + MediaPipe 生成)**
と**初フレーム**を含んでいなかったため、100%なのに映像が出ないように見えていました。

`core/readiness.py` が工程全体を1本のゲージにまとめ、`/debug/camera/readiness/` から
1秒間隔で取れます(カードにゲージ・5段階の内訳・準備ログを出します)。

重み: カメラ検知15 / **AIモデル準備35** / カメラ確保15 / 初フレーム取得20 / 解析稼働15

実測した起動タイムライン(2回):
```
 0.0s  サーバー起動
 2.0s  カメラ占有ロックを確保
 2.0s  自動再学習を確認(変更なしなら約0.1秒)
 2.2s  index 3→2→1 を開こうとして失敗(未接続。合計0.2秒)
 3.2s  index 0 を確保(0.8秒)
 3.3s  AIモデル準備を開始
18.4s  └ 完了(**15.1秒**。2回目はウォームキャッシュで 5.9秒)  ← ここの差が大きい
19.3s  PoseLandmarker 準備 / 初フレーム
20.8s  解析稼働(29.0fps) → 準備完了 21.2秒
```

ログには「何を何秒でやったか / 何を試して失敗したか」がそのまま出ます。
カメラが開けない時は `index 3: カメラを開けませんでした(未接続の可能性)` のように
対象ごとに出るので、**ゲージが止まった理由をログで特定できます**。

### 実装上の注意


- 推論は `tf.function` でグラフ化しており1フレーム約10ms。
  `model.predict()` のままだと約160ms/フレームで約1fpsしか出ません。
- tensorflow / mediapipe が無い環境でもアプリは起動でき、
  理由は `/debug/camera/` の「リアルタイムAI解析」欄に表示されます。
- **overlay描画はWebアプリの応答を巻き込まないこと**(実測で site が応答不能になった):
  - 日本語overlayは **1フレーム1回のPIL往復**にまとめて描画(`_draw_japanese_overlay`)。
    行ごとに BGR→RGB→PIL→BGR をやると5回の画像変換になり、解析スレッドがGILを握り続けて
    HTTPリクエストが詰まります(縁取りは `stroke_width` を使う)
  - **誰も映像を見ていない時(常時稼働の自己視聴のみ)は描画とJPEG化を省略**し、
    検知だけ続けてCPU/GILをWeb側へ空けます(`_has_browser_viewer()`)
- **runserver の二重起動防止**: 2プロセスが同じポート(Windowsは `SO_REUSEADDR` で
  重複bindできてしまう)で立つと、両方の常時稼働がカメラを奪い合って互いにハングします。
  `.ai_always_on.lock`(PIDファイル)で先取したプロセスだけが常時稼働を開始します。
- `camera_service.acquire()` は**カメラ開き(最大数十秒ハングし得る)をロック外**で実行します。
- **ライブ表示はAIと同じキャプチャから読まない**: AIが常時稼働でカメラを掴んでいるため、
  配信側が `cap.read()` を呼ぶと**フレームを取り合い**、低速カメラでは必ずどちらかが空振りします。
  配信はAIの描画済みJPEGを `frame_seq`(更新回数)で新フレーム判定して流し、
  AIがそのカメラを読んでいない時だけ生読みします(`is_analyzing()`)。
  またフレームが一時的に無いだけで配信を切らない(`AI_STREAM_IDLE_SECONDS` 既定60秒)。
- **カメラ未接続は正常な状態**です: 検知は空一覧を返し、常時稼働は待機して
  `AI_ALWAYS_RETRY_SECONDS` 毎に再試行、接続されると自動で開始します。

### カメラ検知(Windows)の落とし穴と対策

同じカメラでも **OpenCV のバックエンドごとにインデックスの割当順と起動速度が違う**ため、
「2台接続しているのに1台しか出ない」が起こり得ます(実測):

| 方法 | index 0 | index 1(内蔵カメラ) |
|---|---|---|
| MSMF | 0.7秒で映像OK | **54秒ハング**してから映像OK |
| DSHOW | 0.7秒で映像OK | **0.4秒**で映像OK |

さらに **開く順序が結果を左右します**(実測):

| 開放順 | 結果 |
|---|---|
| index 0 → 1 | 1台目は30fps、**2台目は開けない(または1fps)** |
| index 1 → 0 | **両方開き、両方からフレームが取れる**(1台目30fps / 2台目1fps) |

つまりこのPCは **2台同時だとUSB帯域が足りない**(解像度を320×240に落としても改善せず、
バックエンドを分けても同じ)。ソフトウェア側でできることは以下です:

1. **バックエンドは DSHOW → MSMF の順**(速い方优先。DSHOW が見えないカメラだけ MSMF)
2. **1インデックス6秒の時間予算**(`_probe_with_budget`)。ハングしたら待つのをやめる
3. **OS列挙(PnP)フォールバック**: OpenCV で開けなくても OS が認識していれば一覧に出す
4. **検知の非同期化**: `/debug/camera/devices/` は待たず `{"pending": true}` を返し、
   進捗APIが既に分かったカメラを随時返す(実測 63秒/1台 → 3秒/2台)
5. **常時稼働は index の降順に・1台ずつ順番に開く**(`RealtimeAIEngine.target_devices()` / `start_persistent`)
   - 降順: 先に開いた方が帯域を取れる順
   - **逐次必須**: 並列に `VideoCapture.open` すると後発カメラが1フレームも返さない(実測で確認)
6. **初フレームを待つ時間を用途別に分離**
   - 検知(一覧作成)は短く打ち切り `AI_CAMERA_FIRST_FRAME_SECONDS`(既定3秒)
   - 配信・常時稼働は長めに待つ `AI_CAMERA_OPEN_SECONDS`(既定10秒)
     → 2台目は帯域の都合で初フレームが約1秒遅れるため、短くすると「起動失敗」になっていた
7. **低速カメラも止めずに解析を続ける**: 1fpsでもフレームが来ている間は稼働させ、
   一覧に「1 fps(低速/帯域不足)」として表示(`summary()` の `low_fps`)
   完全に0フレームのカメラだけ見張り(`_monitor_loop`)が停止し、
   `AI_STARVED_RETRY_SECONDS`(既定60秒)後に再試行する

### カメラの「名前」と「左右反転」について(実測で修正した問題)

- **index と名前の対応は推測しない**: OSのPnP列挙順とOpenCV(DSHOW)のindex順は**一致しない**ことがあり、
  2台接続時に名前が入れ替わって表示されました(実測)。
  → 複数台のときはOS名を当てはめず、**`DEBUG_CAMERA_NAME_MAP` で明示**します(1台のときだけOS名を使ってよい)。
- **一覧に実フレームのサムネイル**を出す(`media/camera_snapshots/cam{index}.jpg`)。
  検知時(`_probe`)と解析開始時(`ai_engine` の初フレーム)の両方で保存するので、
  AIがカメラを掴んでいる状態でも「どれがどのカメラか」を目視で確認できます。
- **ライブ映像の左右反転**: 元の実験コード `realtime_lstm.py` は推論前に `cv2.flip` していましたが、
  学習データを作った `collect_data.py` は反転していません。
  学習時と違う向きで推論すると判定がずれるうえ、映像も反転して見えるため、
  **既定は反転なし**(`AI_MIRROR_INPUT = False`)にして学習データの向きと揃えました。
  自撮り風にミラー表示したい場合だけ `True` にしますが、その場合は
  `ai_collect_data` も同じ向きで収集し直して再学習してください。

### 複数カメラの同時ストリームは硬件依存(`--simultaneous` で確認する)

`python manage.py list_cameras --simultaneous` は、検知できた全カメラを**同時に開いて
実効fpsを測る**検査です(「1台ずつなら映るのに、アプリでは片方だけ動かない」の切り分け用)。

開発PCでの実測:

| 開放順 | 結果 |
|---|---|
| 内蔵(idx0) → 外付け(idx1) | 内蔵 30fps / **外付けは開けない** |
| 外付け(idx1) → 内蔵(idx0) | 外付け 30fps / **内蔵 1fps(実質黒)** |
| 両方とも320×240へ下げる | 結果同じ(**帯域不足ではなく同時ストリーム自体が不可**) |
| 外付け=**MJPG 320×240** / 内蔵=320×240 YUY2 | 外付け30fps / 内蔵1fps。合計約2.6MB/sでUSB2の1/10 → **帯域は原因でない**と確定 |
| 両方 MSMF / 320×240 @15fps | 1台目OK、2台目は**映像なし/開けない** |

内蔵カメラ(Chicony VID_5986)は**他のカメラが配信中は常に1.0fpsに落ちる**(解像度・形式・バックエンドに非依存)。
→ **USBコントローラ/ドライバ側の制約**であり、ソフトウェアでの解決は不可能です。

このPCでは**同時に1台しか映像を出せません**。アプリ側の挙動:

- 開く順序がそのまま優先度になる(既定は index の**降順** = `AI_CAMERA_PRIORITY = None`)
- 後発カメラが映像を1コマも取れないと、見張りが解析を止めて一覧に理由を表示し、
  `AI_STARVED_RETRY_SECONDS`(60秒)ごとに再試行します(カメラのアクセスランプが消えている状態)
- 内蔵を動かしたいときは `settings.AI_CAMERA_PRIORITY = [0, 1]` のように先頭へ

> **本番の複数台運用**: カメラを**別のUSBコントローラ/ハブ**に分散接続し、接続後に
> `python manage.py list_cameras --simultaneous` で「全カメラ同時に映像が出ています」を
> 確認してから運用してください。ここで「1台だけ」なら配り方(ハブ/コントローラ)の問題です。

### 複数カメラを実現する選択肢

| 案 | 内容 | 備考 |
|---|---|---|
| **別USBコントローラへ分散** | 内蔵カメラは内部USBに直結なので、**外付けを別バスのポート**(USB3側/ドック/PCIe増設)へ移す | 最安。まず `--simultaneous` で合否を判定 |
| **自己給電USB3ハブ+増設コントローラ** | ハブ自体を別バスに接続 | バス分離が目的(帯域ではない) |
| **IPカメラ(RTSP/MJPEG)へ切替** ★実装済 | USBでなくネットワークカメラにすれば台数はUSBに依存しない。施設運用の定番 | 下記「IPカメラ」を参照 |
| **マルチ入力キャプチャ集約機** | 複数カメラを1台のUSB機器に纏める装置(4chキャプチャ等) | 1デバイスに見えるのでUSB競合が起きない |
| **1台に絞る** | `AI_ALWAYS_DEVICES = [1]` で確実に1台だけ常時解析 | 副カメラは `AI_CAMERA_PRIORITY` で都度切替 |
| (非推奨)交互ローテーション | 数秒おきにカメラを開き替える | 切替中は死角になり**転倒検知には不適** |

調査用コマンド:
- `python manage.py list_cameras --shots` … OS列挙+バックエンド×indexの総当たり+実画像保存
- `python manage.py list_cameras --simultaneous` … **全カメラ同時開放で実効fpsを測定**(設置後の受け入れ検査)

## IPカメラ(RTSP / HTTP-MJPEG)の常時解析

USBの制約(このPCは2台同時不可)に縛られない複数カメラ運用として、**台帳のカメラを
ネットワーク経由で解析**できます。USBカメラと同じエンジン・同じ手順で動きます。

### 使い方
1. `/admin/` または台帳で **Camera の `stream_url` に配信URLを設定**し「オンライン」にする
   - 対応: `rtsp://…`(ONVIFカメラの多く)、`http://…/mjpeg` などのHTTPストリーム
   - **録画ファイル(`.mp4` 等)はライブ配信ではないため自動で対象外**になります
2. `runserver` を起動 → 自動で **USBカメラ + IPカメラの全部**を常時解析
3. `/debug/camera/` の一覧に `IP 検証用IPカメラ … / IP / http://…` として出るので、選んで「表示開始」

```
一覧: index 0  ノートPC内蔵カメラ …
      index 1  外付けWebカメラ …
      IP       検証用IPカメラ   解像度不明 / IP / 配信中 / http://127.0.0.1:8899/mjpeg
AIカード: 「3台を常時解析中」
```

### 動作するもの(IPカメラでもUSBカメラと同じ)
- 骨格・判定overlay付きライブ配信(`/debug/camera/stream/?device=cam:<Camera.id>`)
- 転倒・無動作の検知 → `Alert` 作成 → `/api/alerts/` 通知(音・トースト・OS通知)
  ※ IPカメラのアラートは**台帳の実レコードに紐づく**ので、異常検知一覧のカメラ名/場所が正しく出ます
- 検知後 `AI_RECORDING_SECONDS` 秒の自動録画(mp4)
- 切断・待受けへの耐性: 読み取り失敗時は `AI_NETWORK_RECONNECT_SECONDS`(既定10秒)ごとに再接続

### 設定
| 設定 | 既定 | 内容 |
|---|---|---|
| `AI_IP_CAMERA_ENABLED` | `True` | IPカメラを常時解析対象に加えるか |
| `AI_IP_CAMERA_ONLINE_ONLY` | `True` | 台帳で「オンライン」のカメラだけ対象にする |
| `AI_IP_CAMERA_IDS` | `None` | 対象をIDで絞り込む(指定すると録画ファイル形式も許容) |
| `AI_NETWORK_OPEN_SECONDS` | `12.0` | 初フレームまで待つ上限 |
| `AI_NETWORK_RECONNECT_SECONDS` | `10.0` | 読み取り失敗時の再接続間隔 |
| `AI_NETWORK_WAIT_NEW_FRAME_SECONDS` | `0.05` | 解析が「新しい1枚」を待つ上限(待っても取れれば最新を返す) |
| `AI_NETWORK_FRAME_STALE_SECONDS` | `3.0` | これより古いフレームしかなければ映像途絶扱い(None) |

### 遅延対策: ネットワークカメラは専有スレッドが「最新1枚だけ」保持する

HTTP-MJPEG / RTSP は**バイトの管**で、古いフレームを捨てる意味を持っていません。
消費レートが生成レートを1フレームでも下回ると、その差分はTCPバッファと配信側の
クライアントキューに積み上がり、`cap.read()`は**キューの中で一番古い**フレームを
返し続けるため、遅れは回復せず一定化します。USBカメラで起きないのは、ドライバが
常に最新1枚へ上書きするからです。

実測(Raspberry Pi + ustreamer、640x480/30fps、50KB/枚):

| | 対策前 | 対策後 |
|---|---|---|
| Pi生成 | 30fps | 30fps |
| ai2消費 | 27.6fps(1枚36.2ms > 予算33.3ms) | 27.4fps(**変らない**) |
| ustreamer→ai2 配信 | 21〜29fps(詰まる) | **30.0fps** |
| Pi側のSend-Q(ai2接続) | **2.37〜2.68MB 常時** = 約49枚 = 1.6〜1.8秒 | **0〜48KB** |
| 解析中のフレームの古さ | 実測2.3秒(レンズ覆しの相関) | **0.01〜0.03秒** |

`core/camera_service.py` の実装:
- ネットワークカメラを開いたとき **専有読取スレッド**(`camera-reader-<key>`)を起動し、
  パイプを空に保ちながら読み進めて `frame / seq / ts` の**最新1枚だけ**を保持する
- `read_frame()` / `read_jpeg()` は `cap` を触らずその1枚を受け取る(`cap` は読取スレッド専用。
  他スレッドが読むとフレームの取り合いになる)
- 再接続も読取スレッド内だけで行う(`cap` の差し替えが安全なのは単一所有者のため)
- 視聴者0かつ解析中でなくなったら `stop` を立てる。**capを閉じるのも読取スレッド**
  (他スレッドが `cap.read()` 中に `release()` すると native層で use-after-free)
- デバッグページに **「映像の遅延」** として常時表示(`frame_age`。USBは「行列なし」)

`CAP_PROP_BUFFERSIZE` はこの構成では `-1`(未対応)で調整できないため、**アプリ側で
古いフレームを捨てる**のが唯一の確実な対策です。

### 検証(ローカルMJPEGサーバで実測)
`http://127.0.0.1:8899/mjpeg` を配信するテストカメラを台帳に登録して確認:
- OpenCV の読み取り: **14.7fps**
- 常時稼働: `cam:8 = running 9.7fps`(USB2台と**同時に**解析 → 「3台を常時解析中」)
- ライブ表示: overlay付きで **51フレーム/5秒 ≈ 10fps**、初フレーム即座
- USBカメラが2台同時不可でも IPカメラは影響を受けないことを確認

### Pi側の配信スタック(ustreamer → libcamera に変更 / 2026-11-11)

**ustreamer は Raspberry Pi の CSI カメラ(Camera Module v2 = imx219)を kernel 6.18 で
640x480 で開けない。** ustreamer は V4L2 を直接開くが、unicam はセンサーの mode と
同じサイズしか受け付けず、しかも出力は Bayer RAW なので ISP による色変換が通らない。

```
unicam 3f801000.csi: Wrong width or height 640x480 (remote pad set to 3280x2464)
unicam 3f801000.csi: Failed to start media pipeline: -22
```

ustreamer は「NO SIGNAL」の黒画面を流し続けるので、ai2 側は `source.online=false` /
`captured_fps=0` になり、異常検知一覧は「映像が取得できない」扱いになる。

**対策**: libcamera 経由(`rpicam-vid`)で MJPEG を作って HTTP 配信する。
センサー mode を選べるので **広い画角のまま 640x480 を出力できる**(640x480 mode は
中央クロップで画角が狭くなるため `1640:1232:10:P` を使う)。

```
/usr/local/bin/picam_stream.py          # rpicam-vid の stdout を配信するだけの小さなHTTPサーバ
pi-cam1.service の ExecStart:
  /usr/bin/python3 /usr/local/bin/picam_stream.py \
    --port 8080 --width 640 --height 480 --quality 80 --framerate 30 --mode 1640:1232:10:P
```

- `/stream` のヘッダと境界(`boundarydonotcross`)は ustreamer と**同じ形式**なので、
  ai2(OpenCV/FFmpeg の mpjpeg demuxer)とブラウザは**無変更でそのまま使える**
- **最新1枚だけ保持**する設計(第21章と同じ原則)。遅いクライアントに対して古いコマを積まない
- `rpicam-vid` が落ちる/10秒フレームが無ければ自動で再起動する
- `/state` は ustreamer と同じ形のJSON(`online`/`captured_fps`/`frame_age`/`restarts`)

実測(2026-11-11):

| | ustreamer | libcamera 配信 |
|---|---|---|
| `/state` | `online:false / captured_fps:0` | **`online:true / captured_fps:30`** |
| OpenCV 読取 | 0fps(黒画面) | **30.1fps**、初フレーム 0.06秒 |
| JPEG 1枚 | 50KB | **24.5KB**(帯域が半分以下) |
| ai2 の `frame_age` | – | **0.00〜0.03秒** |
| Pi の Send-Q(ai2接続) | 2.45MB(第21章) | **20KB前後** |

**Pi側のファイルはリポジトリに置かない**(運用指示)。`picam_stream.py` は
`/usr/local/bin/` に、unit は `/etc/systemd/system/pi-cam1.service` にある。

### Piに2台のカメラを載せる運用(2026-11-11 実測)

| ポート | 機器 | 手段 | 備考 |
|---|---|---|---|
| 8080 | CSI Camera Module v2 (imx219) | `picam_stream.py` + `rpicam-vid` (libcamera) | mode `1640:1232:10:P`(全画幅binning。640x480 mode は中央クロップで画角が狭い) |
| 8081 | USB Logitech C270 | `ustreamer --format=MJPEG --encoder=HW` | UVC自前のMJPEGをパススルーするので **CPUほぼ0**。`pi-cam2.service` |

- **`/dev/videoN` を書かない**。機器を増やすと index が総入れ替えされる(C270 を足したら CSI が
  video0 → video2 になった)。USBは `/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_…-video-index0`、
  CSIは libcamera が DT の entity で探すため影響なし
- ai2 側は台帳 `Camera.stream_url` に各ポートを入れるだけ。常時稼働が自動で両方を解析する
- 実測: cam:9 = 26.5〜28fps / cam:10 = 25〜28fps、**両方とも 映像の遅延 0.00〜0.04秒**、
  Pi の Send-Q は 20〜54KB、Pi 負荷 load 1.24(4コア)

**注意**: `/debug/camera/` の一括カードは「そのカメラの解析が `running` になってから接続」する。
未接続のカードは理由を表示する(`AIモデル準備中…` など)。

**映像が横向き/逆さまなとき**: カメラの取付向きに合わせて
`--rotation 90|180|270`(または `--hflip 1` / `--vflip 1`)を `ExecStart` の
`rpicam-vid` 引数へ足す(`picam_stream.py` の `build_command`)。
`media-ctl` で sensor fmt を直接書き換える手は**再起動で戻る**ので使わない。

## AIモデルの再学習(追加学習)

caregiver_safety の学習パイプラインを ai2 に移植し、**サーバー起動時に自動で走る**ようにしました
(`core/retraining.py`)。コマンドは手動実行・検証用の逃げ道として残しています。

### 起動時の自動再学習(`AI_AUTO_RETRAIN = True`)

```powershell
python manage.py ai_collect_data     # 材料を増やすのはこれだけ(またはCSVを train_data/ に置く)
python manage.py runserver ...       # 次回起動時に 前処理→学習→現行と比較→反映 を自動で実行
```

`runserver` 起動時に `train_data/*.csv` の**指紋(ファイル名・サイズ・更新時刻のハッシュ)**を
`train_dataset/retrain_state.json` と比較し、次のように動きます。

| 状況 | 動作 |
|---|---|
| データが変わっていない | **数秒で素通り**(ログ `[Retrain] データ変更なし(Nファイル)`)。autoreload の再起動でも安全 |
| データ変更あり / モデル未作成 | データセット再生成 → 候補を `ai_models/candidates/` へ学習 → **同じ保持データで現行と比較** → 劣化がなければ `ai_models/` へ反映(旧モデルは `ai_models/backup/<日時>/` へ自動退避) → 指紋を記録 |
| 候補が現行より劣る | **反映を見送り**候補だけ残す(acc・見逃し率を両方ログ表示) |
| 学習中の例外 | 現行モデルはそのまま。Webアプリと解析は止めずログのみ |

- 反映判定は **AI設定の転倒閾値での 正解率 / 誤検知率 / 見逃し率** の3指標。許容する劣化幅は `AI_RETRAIN_PROMOTE_MARGIN`(既定 0.005)
- 学習は **常時稼働を開始する前** に走らせる(CPU争避のため。時間がかかるのはデータ追加時のみ)
- `/debug/camera/` のAIカード下に「自動再学習 最終: … / 転倒モデル更新…」と表示
- 設定: `AI_AUTO_RETRAIN` / `AI_RETRAIN_EPOCHS_FALL` / `AI_RETRAIN_EPOCHS_STATE` / `AI_RETRAIN_PROMOTE_MARGIN`
- 手動確認・実行: `python manage.py ai_retrain --status` / `--force`

### コマンドで手動で行う場合

```powershell
python manage.py ai_collect_data            # ① 収集:カメラ→MediaPipe Pose→train_data/*.csv
python manage.py ai_prepare_dataset         # ② 前処理:CSV→train_dataset/X.npy,y.npy(転倒)
python manage.py ai_prepare_state_dataset   # ② 前処理:CSV→state_X/state_y/state_labels(状態)
python manage.py ai_train_fall              # ③ 学習:ai_models/candidates/ へ(現行は壊れない)
python manage.py ai_train_state
python manage.py ai_check_model             # ④ 検証:精度/混同行列/CSV別誤判定/AI設定閾値での評価
python manage.py ai_train_fall --promote    # ⑤ 反映:旧モデルは ai_models/backup/<日時>/ へ退避
python manage.py runserver 127.0.0.1:8000   # ⑥ 再起動(モデルは起動時にキャッシュされる)
```

| コマンド | 内容 |
|---|---|
| `ai_collect_data` | 姿勢データ収集。`--device` / `--backend dshow` / `--label normal\|fall` / `--name 行動名` / `--seconds` / `--no-gui`。**runserverを止めてから**実行(カメラ占有と衝突する) |
| `ai_prepare_dataset` | `train_data/*.csv` → `train_dataset/X.npy,y.npy`。`--stride`(既定5) `--dry-run` |
| `ai_prepare_state_dataset` | → `state_X.npy,state_y.npy,state_labels.npy`。`--list-unmapped` で未登録の行動名を確認 |
| `ai_train_fall` / `ai_train_state` | LSTM再学習(状態はクラス重み付き)。`--epochs --batch-size --promote` |
| `ai_check_model` | 検証。`--model fall\|state` `--path <keras>` `--threshold`(既定=AI設定の転倒閾値) `--top` |

- 材料の格納先: `train_data/`(姿勢CSV 39個 = fall 13 / normal 26)、`train_dataset/`(学習テンソル)
  ※ caregiver_safety の `data/`・`dataset/` をコピー済み。元ファイルは `train_dataset/_caregiver_original/` に退避してあります
- 新しい行動CSVを状態学習に使うには `core/training.py` の `STATE_BY_SUFFIX` へ登録します
- **`--promote` 後はサーバー再起動が必要**(モデルは初回使用時にメモリへキャッシュされるため)

### 現行モデルの検証結果(参考・`ai_check_model` 実測)

AI設定の閾値 **0.77** で 2,288 シーケンスを検証:

| 指標 | 値 |
|---|---|
| 正解率 | 96.68%(2212/2288) |
| 誤検知率(NORMALがFALL扱い=アラート空振り) | **0.26%**(5件) |
| 見逃し率(FALLがNORMAL扱い=危険) | **18.88%**(71件) |

`fall_歩行中に転倒…` 系のCSVで誤判定率 59〜91% と高く、**追加学習で最も効くのは「見逃し」側**
です。`ai_collect_data --label fall` で該当動作を追加収集して再学習してください。

## 構成

```
config/        … settings / urls(全URLは config/urls.py に集約)
core/          … models / views / admin / auth / ai_engine(全カメラ常時解析) / camera_service
                 / training(再学習の共通処理) / management commands
ai_models/     … 推論モデル + candidates/(学習候補) + backup/(反映前の旧モデル)
train_data/    … 追加学習の材料: 姿勢CSV(33landmark×xyz=99値 + ラベル + 人物番号)
train_dataset/ … 学習入力テンソル(X/y, state_X/state_y/state_labels)
templates/     … 画面テンプレート(base.html が情報バー+メニュー+MAINの共通レイアウト)
static/        … css / js / img
media/         … AI自動録画(mp4 / jpg) などアップロード系
db.sqlite3     … SQLiteデータベース
```

## 動作のポイント

- **異常検知タブ**で「対応する」を押すと対応者が確定し、`/api/alerts/` ポーリングにより
  開いている全画面に「誰が対応中か」が自動反映されます(新規異常は通知音+トースト)。
- 対応者本人だけが「解決・報告を入力」を押せ、プロフィールページの報告フォームへ遷移します。
- **「⚠ 誤検知として報告」は 未対応/対応中 のときだけ**表示します。
  解決済み(報告が完了した異常)ではボタンを出さず「対応が完了したため、誤検知登録はできません」と表示し、
  POSTでも受け付けません(解決記録とAI閾値フィードバックが食い違うのを防ぐ)。
- 録画再生ページにはAI検知マーク(例: 「転倒の瞬間 00:18」)があり、クリックでその瞬間へジャンプします。

