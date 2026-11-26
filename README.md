# CARE SEC 転倒・異常検知システム

介護施設の利用者の転倒・異常をAIカメラで検知するWebツール。
Django **6.1.2** / Python **3.13** / TensorFlow + MediaPipe / SQLite。
開発環境=WindowsPC(USBカメラ)、**本番=AWS EC2(Ubuntu 26.04 / t3.large / Pi配信カメラ)**。

## ドキュメントマップ(何を読むか)

| 文書 | 役割 |
|---|---|
| **README.md**(この文件) | 概要・起動・設定・権限・コマンドの正典 |
| `AWS Setup Guide.html` | AWS移行の設計と手順(VPC/SG/メディア保護/第8章=ログイン限定配信) |
| `AWS移行成功手順.html` | 2026-11-25/26 実走成功手順の完全記録+教訓14件 |
| `CAMERA_PIPELINE_SETUP.md` | Pi→EC2カメラ接続(WireGuardパターンW/S/T) |
| `INTEGRATION_STATUS.md` | **開発日誌(全50章)**。実測数値・失敗と対策の一次資料。ここが全ての根拠 |
| `URLS.md` / `テーブル設計書.md` | URL設計・DBスキーマ |
| `ai_models/README.md` | モデルファイルと再学習の現況 |
| `実装手順書.html` | 初期実装手順(歴史資料) |

## 起動

### 開発機(Windows)

```powershell
cd C:\Users\student\code\ai2
.venv\Scripts\Activate.ps1
python manage.py runserver 127.0.0.1:8000
```

- **runserver は常に1個**。二重起動すると両方がカメラを奪い合いハングする(`.ai_always_on.lock` が先取プロセス以外を止めるが、混線ログの原因になる)
- **停止は Ctrl+C**(強制KillはWindowsカメラスタックにゾンビを残す実測あり)
- 依存導入は初回のみ: `pip install -r requirements-windows.txt`(TF/mediapipeで数百MB)

### 本番(AWS EC2)

```bash
sudo systemctl status caresec          # 常駐確認(gunicorn workers=1 + AIスレッド)
sudo journalctl -u caresec -f          # 起動ログ([RealtimeAI] 解析稼働中…)
sudo systemctl restart caresec         # デプロイ後(モデル再ロード込み約15-20秒)
```

デプロイ=開発機で `git push` → サーバー `sudo -u caresec git pull` → `migrate`/`collectstatic` 必要なら実行 → restart。
**サーバー上のコードを手で編集しない**(pull衝突+幽霊差分の原因)。

## 環境変数(settings.py が読む)

| 変数 | 既定 | 効果 |
|---|---|---|
| `CARE_SEC_SECRET_KEY` | dev用固定値 | 本番では必ず生成して設定 |
| `CARE_SEC_DEBUG` | `1` | 本番は `0`(=静的はcollectstatic、メディアは`protected_media`配信) |
| `CARE_SEC_ALLOWED_HOSTS` | 空 | カンマ区切り。前後空白自動除去。localhost/127.0.0.1/testserverは常時許可 |
| `CARE_SEC_CSRF_ORIGINS` | 空 | 本番https origin(POST CSRF検証)。`https://caresec.duckdns.org` 等 |
| `CARE_SEC_ENV` | `dev` | `prod` にするとヘッダーバッジが緑「本番 AWS」(誤環境操作防止) |
| `CARE_SEC_EMAIL_*` | localhost | 既定はconsole backend。SMTP化時のみ |

本番設定の現物=`/etc/care-sec.env`(root:caresec 640)。

## 権限(2段階ロールのみ)

Django標準のgroups/user_permissionsは不使用。`core.User.is_admin` のみで判定する(`core/permissions.py`)。

| 画面 | 一般介護士 | 管理者 |
|---|---|---|
| ダッシュボード/異常/録画/報告/プロフィール | ✅ | ✅ |
| 設定ページ **AI稼働・デモ・データ削除** の3セクション | ❌(表示もPOSTも遮断。403) | ✅ |
| カメラデバッグ `/debug/camera/` 配下 | ❌(直URLも403) | ✅ |
| Django管理画面 `/admin/` | ❌(403) | ✅ |

- 403は理由付きページを返す(「壊れた」誤解を防ぐ)。未ログインは従来どおり`next`付きでログインへ
- アカウント作成: `/admin/core/user/add/`。`createsuperuser` は `is_admin=True` で作られる

## AI検知の現況(2026-11-26)

```
カメラ(USB or Pi配信) → MediaPipe Pose(最大5人) → 人物ごと group化+腰中心/体幹スケール正規化(99値×30窓)
  転倒 = ①LSTMアンサンブル(3名の平均確率) ≥ AISettings.fall_threshold(既定0.50) を 8回連続
         ②速度ルール(roll≥25°/10F→水平→維持15F、発火は確率0.90でラッチ) ← ①②はOR
         ③(アラート確定瞬間)CLIP検証官: 「床に倒れている人」をゼロショット確認、
            否定したらアラート拒否(しゃがみ/前かがみ誤検知対策)。CLIP不在は素通し=検知を止めない
  無動作 = ランドマーク移動ほぼゼロが stillness_seconds(既定30秒)以上
→ Alert(人物ごと1件・人物単位クールダウン) → /api/alerts/ 5秒ポーリング → 通知音/トースト/OS通知
→ 検知後 AI_RECORDING_SECONDS(8秒) を自動録画(media/recordings) → **30日で自動削除**(下記)
```

- **検知の総当たり解消**: quick path+known_indices+プローブ/acquire対称抑止(同一index二重open禁止)。実測の失敗と対策は第46・49章+補足3
- **環境ツマミ**: `/admin/` → AI設定 → **カメラ源** = `auto`(両方)/`usb`(開発機)/`net`(本番。USB検知を走らせない)。30秒サイクルでライブ反映
- **録画のプライバシー保存期間**: `RECORDING_RETENTION_DAYS=30`。一覧は「残り◯日」のみ表示。超過はDB行+mp4/jpg実ファイルごと自動削除(`core/retention.py`。ページ表示時+常駐ループの両方で掃除)
- **メディアログイン限定**: 本番(DEBUG=0)の `/media/…` は `core/protected_media.py` が `@login_required` でのみ配信。nginxは静的配信しない(検証手順はAWS Setup Guide 8.6)
- **ふらつき(sway)**: 傾き変動/横揺れを計算しデバッグ画面に**表示のみ**(アラート化しない=2026-11-24決定)

### 現行モデルの精度(2026-11-24 再構築後・同一保持テスト集合)

| 指標 | 旧単一モデル | 現行3名アンサンブル(+反転aug・クラス重み) |
|---|---|---|
| 正解率 | 0.620 | **0.861** |
| 見逃し率(窓) | 34.8% | **4.3%** |
| 誤検知率(窓) | 38.8% | 16.1%(→8票連続+CLIPでアラートレベルに絞る) |

ファイル: `fall_detection_lstm.keras` + `_e2` + `_e3`(実在分だけ平均)。詳細数値・履歴は INTEGRATION_STATUS 第46〜49章。

## カメラ構成

### 本番(AWS)

| 台帳key | 配信元 | URL(WireGuardトンネル内) |
|---|---|---|
| cam:1 | Pi3 CSI(imx219) `pi-cam1.service` | `http://10.8.0.2:8080/stream`(picam_stream.py / libcamera MJPEG) |
| cam:2 | Pi3 USB(C270) `pi-cam2.service` | `http://10.8.0.2:8081/stream`(ustreamer) |

- 接続は **Pi→EC2 の一方通行のみ**(WireGuard。施設IPが動的でも自動追従、公网に映像ポートなし)
- ustreamer/picam_stream とも**映像本体は `/stream`**(`/mjpg`は404)。HEADでなくGET先頭200Bで判定する
- IPカメラは専有読取スレッドが**最新1枚だけ保持**(パイプ詰まり=遅延固定化の対策、第21章)。実測遅延0.0〜0.03秒
- Pi側のスクリプト/unitは**リポジトリに置かない**(運用指示)。台帳の `stream_url` だけ管理

### 開発機(Windows)の既知制約(要点のみ)

- このPCは**USBカメラ2台同時ストリームが非対称**(先に開いた方が優先/後発は1fps〜開けない)。USBコントローラ制約でソフト解決不可(第44章)
- 対策済み: 開く順序=**index昇順**・検知非同期+予算6秒・プローブ再試行抑止・見張り停止/自動復帰。**MSMFはハングするのでDSHOW優先**
- indexと名前はOS列挙順と一致しない → `DEBUG_CAMERA_NAME_MAP` で明示
- 詳細・実測表は INTEGRATION_STATUS 第36〜46章+補足2/3

## 設定(config/settings.py 主要)

| 設定 | 既定 | 内容 |
|---|---|---|
| `AI_REALTIME_ENABLED` / `AI_ALWAYS_ON` | True | 解析全体ON/OFF・画面非依存の常時稼働 |
| `AI_FALL_CONFIRM_COUNT` | 8 | 転倒確定に必要な連続投票数 |
| `AI_FALL_MODEL_FILES` | 3名 | アンサンブルメンバー(実在分だけ平均) |
| `AI_VELOCITY_*` | — | 速度ルール(第44章)。`AI_VELOCITY_FALL_RULE=False` で無効化可 |
| `AI_CLIP_VERIFY` / `AI_CLIP_*` | True | CLIP検証官(拒否権のみ・不在は素通し) |
| `AI_NORMALIZE_FALL_INPUT` | True | 転倒入力の腰中心/体幹正規化(推論・学習で必須対) |
| `RECORDING_RETENTION_DAYS` / `CLEANUP_MINUTES` | 30/30 | 録画の自動削除(0=無効) |
| `AI_RECORDING_SECONDS` | 8 | 検知後録画秒数 |
| `AI_ANALYSIS_FPS` | 30 | 解析レート上限(学習データが30fps前提) |
| `AI_ALWAYS_DEVICES` / `AI_CAMERA_PRIORITY` | None | 対象index固定/開く順序(開発機のみ) |
| `AI_FRAME_LOST_SECONDS` / `AI_STARVED_RETRY_SECONDS` | 20/300 | 映像断の監視停止と再試行 |
| `DEBUG_CAMERA_*` | — | 検知の範囲/キャッシュ/再試行(第46章系) |
| `AI_AUTO_RETRAIN` / `AI_RETRAIN_*` | True/… | 起動時自動再学習(指紋変更時のみ。aug+3名+重み) |

DB側の運用パラメータ(`AISettings`: 閾値0.50・無動作秒・クールダウン・**カメラ源**)は管理画面から変更できる。

## コマンド

```powershell
python manage.py seed_demo [--reset]      # 開発用初期データ(アカ/台帳/利用者。偽アラートは作らない)
python manage.py verify_pages             # 全ページ+対応→解決→報告フロー検証
python manage.py list_cameras [--simultaneous|--shots]   # カメラ実測(同時開放fps検査)
python manage.py ai_retrain --status [--force]           # 自動再学習の状態/手動実行
python manage.py ai_collect_data ... / ai_prepare_dataset / ai_train_fall [--promote] / ai_check_model
python manage.py test core                # 37テスト(権限/速度ルール/保存期間/メディア保護/ツマミ等)
```

- 検証スクリプト(実エンジン系): `verify_multi_person.py`(12項目) / `verify_empty_throttle.py`(5) / `verify_scan_cache.py`(7)
- 学習データ収集GUI: `pythonw collect_gui.py`(サーバー停止中に。ラベル付きCSV→train_data)
- 監視系(開発機): `ai2_supervise.ps1`(サーバー常駐+クラッシュ記録) / `ai2_watchdog_poll.py`(15秒間隔の状態ログ)

## URL一覧(要点)

| URL | 内容 |
|---|---|
| `/` | ダッシュボード(1ページ収容。一覧はカード内スクロール) |
| `/accounts/…` | login/logout/profile(+edit)/settings(管理者セクションあり) |
| `/alerts/…` | 一覧/詳細/respond/resolve/false-positive |
| `/reports/…` | 解決報告(入力専用ページ) |
| `/recordings/…` | 録画一覧(**残り日数バッジ**)/再生(AIマークジャンプ) |
| `/media/…` | 録画・スナップショット(**本番はログイン限定**) |
| `/api/alerts/` | 通知ポーリングJSON |
| `/debug/camera/…` | カメラデバッグ+一括ライブ+readiness/ai/devices/progress/stream(管理者のみ) |
| `/admin/` | Django管理画面(カメラ台帳・AI設定・アカウント) |

詳細は `URLS.md`。

## 構成

```
config/          settings / urls / wsgi
core/            models / views / admin / auth / permissions
                 ai_engine(全カメラ常時解析) / camera_service(USB+IP配信)
                 training / retraining(自動再学習) / retention(30日自動削除)
                 protected_media(ログイン限定配信) / readiness / demo_alerts
                 management/commands(ai_* / seed_demo / verify_pages / list_cameras)
ai_models/       現行モデル+ candidates/ + backup/(ai_models/README.md)
train_data/      学習材料CSV(99値+ラベル+人物番号)   train_dataset/ 学習テンソル
templates/ static/  画面・CSS・JS(base.html=共通レイアウト+環境バッジ)
media/           録画mp4/jpg・スナップショット(gitignore)
db.sqlite3       SQLite(gitignore。本番はEBSスナップショットで守る)
```

## 本番運用メモ

- **gunicorn workers=1 必須**(AI常駐スレッドはプロセス内1つ。`_claim_ai_lock` が他プロセスを排除)
- 反映手順: push → pull → (migrate/collectstatic) → restart。`CARE_SEC_ENV=prod` でバッジが緑
- 監視: `journalctl -u caresec` に `[RealtimeAI] …解析稼働中(fps=…)` が証言。certbot timer生存確認は `systemctl list-timers`
- **未実施(次)**: EBSスナップショット(DLM日次)+`sqlite3 .backup` cron+復旧リハーサル(AWS Setup Guide 10章)
- `/health` エンドポイント未設置(ガイド11.1に2行の追記案)

## 動作のポイント

- 「対応する」で対応者確定→全画面に自動反映(新規異常は音+トースト+OS通知)
- 「⚠ 誤検知として報告」は未対応/対応中のみ(解決済みはボタンもPOSTも拒否)
- デモアラートは設定ページのボタン**押下時のみ**1件(自動生成機構は削除済み)
- 誤検知FBはAI感度に自動反映される(練習連打に注意)
