# リアルタイムAI統合 作業メモ(中断時の再開用)

2026-11-05 に **caregiver_safety のリアルタイムAI を ai2 の `/debug/camera/` へ統合**する作業を
実施し、中止した際の状況を記録します。次回はこのファイルと「再開手順」から再開してください。

---

## 1. 実装済み(コードは完成・動作確認済み)

| ファイル | 変更内容 |
|---|---|
| `core/ai_engine.py` | **新規**。リアルタイム解析エンジン(MediaPipe Pose → 30フレーム姿勢系列 → 転倒/状態LSTM)。骨格・日本語overlay描画、`Alert` 生成、バックグラウンドスレッド管理 |
| `core/camera_service.py` | `read_frame(index)` を追加(解析用にBGRフレームを取得) |
| `core/views.py` | `debug_camera_stream()` を解析済みJPEG配信に変更、`debug_camera_ai_status()` を追加 |
| `config/urls.py` | `/debug/camera/ai/` を追加 |
| `config/settings.py` | `AI_REALTIME_ENABLED` / `AI_MODEL_DIR` / `AI_ANALYSIS_FPS` / `AI_JPEG_QUALITY` / `AI_ALERT_CAMERA_ID` / `AI_SIMULATOR_ENABLED` を追加 |
| `core/apps.py` | `AI_SIMULATOR_ENABLED=False` でデモ用シミュレーターを起動しない |
| `templates/debug/camera.html` | 「リアルタイムAI解析」ステータスカードを追加 |
| `static/js/debug_camera.js` | AI状態の1秒ポーリング、停止時のMJPEG接続切断(データURI上書き) |
| `static/css/style.css` | `.ai-status-*` スタイル追加 |
| `ai_models/` | `pose_landmarker_full.task` / `fall_detection_lstm.keras` / `state_detection_lstm.keras` / `state_labels.npy` + `README.md` |
| `requirements.txt` / `requirements-windows.txt` | tensorflow / mediapipe / pillow / opencv-contrib-python を追加(headless版は mediapipe と競合するため削除) |
| `README.md` | 統合の仕組み・設定・注意点を追記 |

### 環境
- `ai2/.venv` に **導入済み**: tensorflow 2.21.0 / mediapipe 0.10.32 / pillow 12.3.0 / opencv-contrib-python 5.0.0.93
- Python 3.13.17(caregiver_safety の venv と同一)

### 確認済みの動作
- カメラ検知: `index 0 / Web Camera - HD / 480×640 / 30fps`(このマシンは1台のみ)
- `/debug/camera/` の「表示開始」→ 骨格 + 日本語overlay(状態/判定/危険度/足/無動作)付き映像を配信
- 解析 **9.5〜9.8fps**(`AI_ANALYSIS_FPS=10` に到達)
- 実AIによる転倒検知 → `Alert` 作成 → `/api/alerts/` ポーリング → 画面上の「🚨 異常検知」バナー通知を確認
- 「停止 → 表示開始」の再起動(セッションIDによる競合対策後)に成功
- `manage.py check` / `manage.py verify_pages` ともに **全ページOK**
- `AI_SIMULATOR_ENABLED=False` 起動時の切替確認 OK

### 中で見つけた重要ポイント(再実装時に再発させないこと)
1. **推論は `tf.function` でグラフ化すること**
   `model.predict()` は約160ms/フレーム、`model(x, training=False)` の直接呼び出しは約400ms。
   `tf.function` 化すると **約7ms**。これで10fpsが出ている。
2. **MediaPipe の landmarker を `close()` しないこと**
   `_models` にキャッシュして再利用するため、close すると2回目以降が動かない。
3. **`detect_for_video` のタイムスタンプは単調増加させること**
   `max(int(time.monotonic()*1000), 前回値+1)` を使用(再開時に0へ巻き戻さない)。
4. **変数名 `connections` は使わない**
   Django の `django.db.connections` と衝突した。MediaPipe 側は `pose_connections`。
   DB側は `from django.db import close_old_connections` を使う(`connections.close_old_connections()` は存在しない)。
5. **Alert の信頼度は 0.0〜1.0** で保存する(百分率で入れると `confidence_percent` が壊れる)。
6. **`attach()` はセッションIDを返し、`detach(device, session)` に渡す**
   「停止→表示開始」時、古い接続の停止処理が新しい解析を止めてしまうのを防ぐ。
7. **「停止」ボタンはブラウザがMJPEG接続を切断しない**ため、
   `img.src` を透明なデータURIに上書きしてから `removeAttribute` する必要がある。

---

## 2. 未完了・要確認

| # | 内容 | 状況 |
|---|---|---|
| 1 | **無動作(`suspicious`)アラートの実環境検証** | **未検証(ユーザー判断でスキップ)**。カメラの前で30秒静止 → `AISettings.stillness_seconds`(既定30秒)で発火するか、の確認が残る |
| 2 | `AI_REALTIME_ENABLED=False` の実機確認 | **✅ 検証済み(2026-11-06)**。`False` でAIカード非表示・生映像のみ配信を確認後、`True` に戻して解析復帰も確認 |
| 3 | 誤検知フィードバック経路 | **✅ 検証済み(2026-11-06)**。アラート詳細の「⚠ 誤検知として報告」→ `fall_threshold` が 0.77→0.79 に上がり、状況が「誤検知」に。**テストで上げた分は 0.77 に戻済み** |
| 4 | 無動作検知の感度 | `STILLNESS_MOVE_THRESHOLD = 0.004`(`ai_engine.py`)が実環境に合うか。無動作テスト(上記#1)と合わせて確認が必要 |
| 5 | 複数カメラ | このマシンは1台のみのため未検証 |
| 6 | `Recording`(自動録画)は**作成しない**仕様のまま | 要望があれば実装(実録画ファイルの保存先・コーデックの選定が必要) |

---

## 3. 現状の設定(DB・settings.py)(2026-11-06 時点)

| 項目 | 現状 | 備考 |
|---|---|---|
| `AISettings.enabled` | **True(AI監視 稼働中)** | 2026-11-06 に True へ戻済み |
| `core_alert` | 1件(#245、**誤検知扱い**) | デモデータが必要なら `python manage.py seed_demo` |
| `fall_threshold` | **0.77** | フィードバックテストで一時 0.79 になったが 0.77 に戻済み |
| `AI_SIMULATOR_ENABLED` | **False(変更済み)** | デモ通知は**停止**。実AIの解析結果だけが通知される。デモ通知が必要なら `config/settings.py` を `True` に戻す |
| `AI_REALTIME_ENABLED` | **True** | False→True の切替検証を実施し戻済み |

---

## 4. 再開手順

```powershell
# 1) サーバー起動
cd C:\Users\student\code\ai2
.venv\Scripts\Activate.ps1
python manage.py runserver 127.0.0.1:8000

# 2) ブラウザで http://127.0.0.1:8000/ を開く
#    ログイン: staff1 / demo1234  (他: staff2/demo1234, admin/admin1234)

# 3) 左メニュー「カメラデバッグ」→ デバイス(index 0)を選択 →「表示開始」
#    → 骨格・判定overlay付き映像 + AIステータスが流れる

# 4) 動作確認の自動検証
python manage.py check
python manage.py verify_pages
```

**確認ポイント**: AIステータスの `解析○fps`(10前後) / `状態` / `判定` / `危険度` /
`無動作 N/30秒`。異常が飛んだら「異常検知」メニューから対応→解決→報告の流れ。

### 2026-11-06 の再開作業で消化したもの
- `AISettings.enabled` を **True** に復旧(設定反映を確認)
- `AI_SIMULATOR_ENABLED = False` に変更し、**デモ通知停止・実AI通知のみ**を確認(ログにシミュレーター停止メッセージ)
- 無動作(30秒静止)テストは**スキップ**(ユーザー判断、未検証のまま)
- 誤検知フィードバック経路・`AI_REALTIME_ENABLED` 切替の実機検証(上記#3 / #2)
- `/debug/camera/` で 9.6〜9.7fps の解析・overlay配信を再確認
- `manage.py check` / `verify_pages` 全通過

### 次にやること(優先順)
1. **(任意)無動作検知の30秒静止テスト**(唯一残った動作未検証項目)
2. (任意)本メモの「2. 未完了」の#4・#5を消化

---

## 5. 2026-11-06 追加工事(常時稼働・自動録画・検知進行バー)

### (1) クールダウン短縮
- `AISettings.cooldown_seconds` を **300 → 10秒** に変更済み(通知がすぐ来るように)
- 通知の届くまでの遅延は **ブラウザの `/api/alerts/` ポーリング5秒間隔**(`static/js/app.js`)が残る

### (2) リアルタイムAIの常時稼働(`AI_ALWAYS_ON`)
- `/debug/camera/` を開いていない時・映像を「停止」した後も、**サーバー側で解析・異常検知・録画が走り続ける**
- 実装:`core/apps.py` が **runserverのワーカープロセスでのみ** `ai_engine.start_persistent()` を遅延起動
  (`--noreload`時は単一プロセス、autoreload時は `RUN_MAIN=true` の子だけ。migrate/test 等では起動しない)
- `camera_service.acquire()` でエンジンがカメラを常時確保(参照カウントなのでブラウザの「停止」でも解放されない)
- `detach()` は視聴者0でも `_persistent` 中なら解析を止めない
- 別デバイスへ「表示開始」すると常時稼働もそのデバイスへ追従する
- 状態APIに `persistent`、画面に「常時解析中 ○○fps」表示を追加
- 設定で止める場合:`AI_ALWAYS_ON = False` / 対象カメラは `AI_ALWAYS_DEVICES`(既定None=全カメラ)

### (3) 異常検知からの自動録画(8秒)
- **検知した時点から `AI_RECORDING_SECONDS`(既定8秒)間**のoverlay付きフレームを蓄積し、
  mp4(H.264)+ JPEGサムネイルを `media/recordings/ai_<kind>_<日時>_cam<id>.mp4` へ保存、`Recording` を作成
- 既存の「異常検知一覧/詳細の 🎞 録画を再生」リンク、`/recordings/` 一覧がそのまま使える
- 書き出しは別スレッド(`ai-recording-writer`)。失敗しても解析は止めない
- **依存に `imageio-ffmpeg` を追加**(ffmpegバイナリ同梱 / `requirements*.txt` 記載済み・導入済み)
- `MEDIA_ROOT = BASE_DIR/"media"`、`MEDIA_URL="/media/"` を追加し、`DEBUG` 時のみ `urls.py` で配信
- 合成JPEGによる書き出し検証済み(実カメラ不要で mp4+サムネイル+Recording 作成/削除 OK)

### (4) デバイス検知の進行バー
- `/debug/camera/progress/` を新設(`camera_service.detect_devices` が index確認/デバイス名取得の進捗を記録)
- `debug_camera.js` が400msポーリング、`templates/debug/camera.html` にバーUI、CSSは `.detect-progress*`

### 検証済み(persistent確認済)
- `/debug/camera/ai/` が `phase=running / persistent=true / fps=9.7`(画面を開いていない状態でも継続)

### 未検証
- 実際の転倒検知→自動録画mp4の再生(8秒録画の実環境確認 / 検知を人为起こす必要あり)
- 無動作30秒検知(前掲のとおりスキップ中)

---

## 6. 2026-11-06 カメラ検知「2台あるのに1台しか出ない」の解決

### 原因(実測)
| 方法 | index 0(Logi C270) | index 1(Web Camera - HD) |
|---|---|---|
| MSMF(旧実装の既定) | 0.7秒 映像OK | **54秒ハング**→ read失敗 → 「無し」扱いで一覧から消えていた |
| DSHOW | 0.7秒 映像OK | **0.4秒** 映像OK |

- 旧実装は MSMF 1回だけ / read失敗したら即「無し」
- `CAP_PROP_OPEN_TIMEOUT_MSEC`・`CAP_PROP_READ_TIMEOUT_MSEC` は **MSMF に効かない**(60秒ハング)
- バックエンドごとにインデックスの割当順が違う

### 対策(`core/camera_service.py`)
1. `_backends()` を **DSHOW → MSMF** の順に(速い方优先。DSHOW が見えないカメラだけ MSMF)
2. `_open_capture()` がバックエンドを総当たり。開けるが映像が無い場合は `available=False` で返す
3. `_probe()` / `acquire()` も同じ方式(acquire は DSHOW 優先で開く)
4. **検知の非同期化**: `detect_devices()` は検知スレッドを起こして即 `{"ok":true,"pending":true,"devices":部分結果}` を返す
   (`_run_detection` / `_detect_partial`。キャッシュは完了時に確定)
5. `detect_progress()` が `devices`(部分結果)も返す
6. 結果: **検知 63秒/1台 → 3.1秒/2台**(index 0 = Logi C270 HD WebCam / index 1 = Web Camera - HD)

### 画面(`static/js/debug_camera.js` / `templates/debug/camera.html`)
- pending の間は進行バーと「検知中ですでに分かったカメラを表示しています…」を出し続ける
- 部分結果を随時一覧に反映(1台目が重くても2台目を先に選べる)
- `available === false` のデバイスは取り消し線+「開けるが映像なし(他アプリ使用中の可能性)」。クリック不可
- 進行バーの段階にバックエンド名を表示(例 `カメラ index 1 を確認中…(DSHOW)(3/9) / 1.2秒`)
- デバッグページの JS は `?v=<timestamp>` でキャッシュ無効化

### 追加コマンド
`python manage.py list_cameras [--shots] [--max N]`
OS列挙(PnP)+バックエンド×インデックスの総当たり結果+実際に写った画像を `media/camtest/` に保存。
次回「1台しか出ない」が出たときにまずこれで切り分ける。

### AI常時稼働との関係
- 常時稼働は **検知できた全カメラ** を同時に解析する(`AI_ALWAYS_DEVICES` で明示指定も可)
- `/debug/camera/` の「表示開始」は**そのカメラを見るだけ**。見始めなくても・「停止」してもAIは走り続ける
- ただしこのPCのUSB帯域の制約で、2台同時だと後から開いた側が 0〜1fps になる(→ 第9章)

---

## 7. 2026-11-06 「USBカメラがまだ認識しない」の解決

### 真の原因
AI常時稼働が1台を占有していると、**もう1台を開こうとして約60秒ハングして失敗**する(Windows実測)。
旧実装はこれを「カメラは存在しない」と扱っていたため、一覧から消えていた。
(サーバー停止時は DSHOW で2台とも0.4〜0.7秒で映像OK。index 0=Logi C270 / index 1=Web Camera - HD を実画像で確認)

### 対策
| # | 内容 | 実装 |
|---|---|---|
| 1 | 1インデックス6秒の時間予算(ハングを待たない) | `camera_service._probe_with_budget()` |
| 2 | OpenCV で開けなくても **OS列挙(PnP)にあれば一覧に出す** | `_os_camera_names()` + `busy: true` デバイス |
| 3 | busy デバイスは**選択可能**(警告色・破線)。選べばAIが切り替わる | `debug_camera.js` / `.debug-device.is-busy` |
| 4 | 切り替え時に占有を解放してから開く | ~~`ai_engine.prepare_for(device)`~~ **第9章で廃止(全カメラ同時解析へ)** |
| 5 | 「表示開始」したカメラが常時稼働の対象として維持される | `start_persistent(index)` を同 view で呼ぶ |
| 6 | 使用中カメラにもOS名前を付ける | `_run_detection` の名前付与 |

### 検証済み(サーバー稼働中 = index 0 を常時稼働が占有した状態)
- 検知結果: `index 0 / カメラ名 / 480×640 / DSHOW / 配信中` + `index 1 / Web Camera - HD / 使用中のため映像を確認できません(選択するとAIがこのカメラへ切り替わります)` → **2台とも一覧に出る**
- index 1 を選択して「表示開始」→ **配信中 / 常時解析中 9.7fps / device=1 / persistent=true / 640×480 配信**
- `manage.py check` / `verify_pages` 全通過

### 残る注意
- ~~常時稼働の初期対象は `AI_ALWAYS_DEVICE_INDEX`(既定 0)~~ → **第9章で全カメラ同時解析に変更済み**
- 2台同時のfpsはUSB帯域で頭打ち(このPC実測: 30fps + 1fps)

---

## 8. 2026-11-06 「Webアプリにアクセスできなくなった」の解決

### 症状
`/debug/camera/` でAIが動き出した直後から、全ページが接続中断で開けなくなった。

### 原因(2つ重なっていた)
1. **runserver の二重起動**(11:09起動と11:21起動)。Windowsは `SO_REUSEADDR` で
   同じポート8000を両方がbindできてしまい、**両者のAI常時稼働がカメラを奪い合って
   互いに約60秒ハング**。`py-spy dump` ではHTTPスレッドがselectで待ち受け中に
   クライアント側がRSTを受ける状態だった。
2. **解析スレッドがGILを握り続けていた**。`py-spy` のホットスタックが
   `_draw_japanese → PIL Image.fromarray` を指していた。日本語overlayを
   **1フレーム5回(行ごとに)BGR→RGB→PIL→BGR**変換しており、これが全スレッドを飜わせていた。

### 対策
| # | 内容 | 実装 |
|---|---|---|
| 1 | overlay描画を**1フレーム1回のPIL往復**に統合。縁取りは9回ループ→`stroke_width` | `ai_engine._draw_japanese_overlay()` |
| 2 | **見る人がいない時は描画とJPEG化を省略**(検知は継続)。常時稼働の自己視聴は視聴者に数えない | `_has_browser_viewer()` / ループの `draw` フラグ |
| 3 | **runserver二重起動のガード**。先取したPIDだけが常時稼働を開始 | `core/apps.py` の `.ai_always_on.lock`(atexitで解放) |
| 4 | `acquire()` は**カメラ開きをロック外**で実行(ハングが他要求を巻き込まない) | `camera_service.acquire()` |
| 5 | 運用: 古いツリーを停止(ユーザー承認済) | — |

### カメラ未接続について(正常な状態)
USBカメラを外した状態でもアプリは正常。
- 検知は空一覧を即返す(`カメラデバイスが検知されませんでした` を表示)
- 常時稼働は開始を諦めて待機し、`AI_ALWAYS_RETRY_SECONDS`(既定30秒)毎に再試行
  → 接続されれば自動で始まる。失敗ログは初回のみで連発しない
- 実測: 内蔵カメラ1台のみで `検知 87ms` / `常時解析中 9.7fps` / HTTP 8〜20ms

### 検証結果(2026-11-06 再検証・実測)
- HTTP: 連続リクエスト **47〜66ms**(視聴者あり=描画ON時)/ 並列8本でも最大300ms。対策前は接続中断
- `py-spy`: `realtime-ai` スレッドは `active+gil`(PIL張り付き) → **idle**(レート制限の待ち受け)
- デバイス検知: **2台とも一覧に出る**(`index 0 Logi C270 HD WebCam / DSHOW` + `index 1 Web Camera - HD`)
- カメラ切り替え: index 1 を選んで「表示開始」→ **device=1 / persistent=true / 9.7fps**へ移行
- 「停止」後もAI継続(**停止中 / 9.8fps / persistent=true**)→ 再開始もOK(キャプチャリークなし)
- **検知→録画→再生 E2E**: 閾値を一時的に0.05へ下げて転倒を検知させ、
  `Recording` が `mp4 290KB / 640×480 / 7.8秒` + サムネイル + マーカー付きで生成、
  `/media/...mp4` は `200 / video/mp4` で配信、ブラウザの `<video>` で **再生確認済み**
  (異常検知一覧の「🎞 検知瞬間の録画を再生」→ 詳細ページも動作)
  → テストで出たアラート/録画/ファイルは削除し、閾値0.77・クールダウン10秒へ復元済み
- `manage.py check` / `verify_pages` 全通過

---

## 9. 2026-11-06 本番挙動へ(全カメラ同時解析・AIは常時稼働)

ユーザー指摘: **「本番はすべてのカメラを動かし、debugカメラでその様子を見れる機能であり、AI認識モデルは常に動いているのが正しい挙動」**
→ 以前の「1エンジン=1カメラ、見たいカメラへ切り替える」設計を廃止し、**カメラごとにスレッド**へ作り直した。

### `core/ai_engine.py` を再構築
| クラス | 役割 |
|---|---|
| `_CameraRuntime` | **1カメラ分**の解析スレッド・視聴者数・最新JPEG・状態・録画状態 |
| `RealtimeAIEngine` | device → `_CameraRuntime` の辞書を管理するシングルトン(attach/detach/status/start_persistent…) |
| `_load_shared()` / `_get_landmarker(device)` | Kerasモデルは**全カメラ共有**(推論は `infer_lock` で直列化、約7ms)。MediaPipeはスレッドセーフでないため**カメラ毎に1個**生成してキャッシュ |

- `attach(device)` / `detach(device, session)` / `latest_jpeg(device)` — すべて device 引数付き
- `status(device=None)` — `?device=N` でそのカメラ、無しは**一番稼働しているカメラ** + `devices`(全カメラ要約)
- `summary()` — 全カメラの phase/fps/状態/視聴者/エラー一覧(存在しないindexの幽霊項目は出さない)
- `target_devices()` — `AI_ALWAYS_DEVICES` 指定が無ければ**検知できた全カメラ**、**index降順**に開く
- `prepare_for()` は削除(切り替え方式の廃止)

### 常時稼働(`core/apps.py`)
- runserver起動 → 2秒後 → **全カメラの常時稼働を開始**、以後 `AI_ALWAYS_RETRY_SECONDS`(30秒)毎に再スキャン
  → **後から差し込まれたカメラも自動で解析に加わる**(未接続でもアプリは正常)
- `.ai_always_on.lock`(PID)でサーバー二重起動時のカメラ争奪を防止

### 映像を渡さないカメラの見張り(`_monitor_loop`)
`cap.read()` が長時間ブロックするとループ内のタイムアウトが発火しないため、**別スレッド**で監視し、
`AI_FRAME_LOST_SECONDS`(20秒)間フレームが無かったカメラは
解析を停止 + キャプチャを解放(他のカメラへ帯域を返す)+ `AI_STARVED_RETRY_SECONDS`(300秒)は再試行を抑止。

### 実測で分かったハードウェア制約(重要)
このPCで2台を同時に開くと **先に開いたカメラがUSB帯域を独占**し、後から開いた側は 0〜1fps
(320×240に落としても、バックエンドを分けても改善せず)。

| 開放順 | 結果 |
|---|---|
| index 0 → 1 | 1台目30fps / **2台目は開けないか1fps** |
| index 1 → 0 | **両方開く**(1台目30fps / 2台目1fps) |

→ ソフト側対策は「**index降順に全カメラを開く**」+「**飢えたカメラは見張りが解放**」。
安定して2台以上を同時解析するには **カメラを別のUSBコントローラに分散接続** する必要がある
(ハブに纏めない)。

### 検証結果(2026-11-06)
- 起動ログ: `[RealtimeAI] 常時稼働を開始しました: index 1` / `index 0` / `解析中のカメラ: index [0, 1]`
- 実行A: **2台同時解析**(`index 0 = 1fps`, `index 1 = 9.7fps` / ともに persistent=true)
- 実行B: index 0 が1コマも取れず → 見張りが停止(`index 0 停止 / 映像が取得できないため…`)、
  **index 1 は 9.7fps を維持**(帯域が戻った)
- デバッグページ: 「全カメラの稼働状況」一覧 + 停止理由の注記、ライブ表示が「停止中」でもAIは稼働
- `manage.py check` / `verify_pages` 全通過

---

## 10. 2026-11-09 requirements.txt / requirements-windows.txt の同期チェック

### やったこと
1. 2ファイルのピンを機械比較
2. アプリ全ソースをASTで走査し、importされているサードパーティが両ファイルに有るか確認
3. **`pip install --dry-run --only-binary=:all: --platform … --python-version …`** で
   Linux/Windowsそれぞれの環境で実際に解決できるか検証(PyPI解決試験)
4. 実venv(`pip freeze`)とピン表的

### 結果
| 項目 | 結果 |
|---|---|
| 2ファイルの内容差 | 共有9パッケージは**バージョン完全一致**。Linuxのみ `gunicorn`(意図どおり) |
| アプリimport網羅 | `django / cv2 / PIL / numpy / tensorflow / mediapipe / imageio_ffmpeg` すべて記載済(`numpy`は推移依存) |
| ⚠️ **見つかった不具合** | `requirements.txt` の `tensorflow==2.21.0` は **Linux用wheelが存在せずインストール不能**(Linuxの最新は2.20.0)。Windowsは2.21.0でOK |
| 其它のピン | `mediapipe==0.10.32` / `pillow==12.3.0` / `imageio-ffmpeg==0.8.1` / `opencv-contrib-python==5.0.0.93` は Linux py3.12/3.13 で解決OK |

### 対応(ユーザー選択: Linuxだけ2.20.0 / 現環境維持)
- `requirements.txt`: **`tensorflow==2.20.0`** に修正 + 理由をコメント化
- `requirements-windows.txt`: `tensorflow==2.21.0` のまま(実venvと一致)+ Linuxとの差分をコメント化
- 両ファイルに **py-spy は開発venv専用の調査ツール(本番不要)** を明記(導入自体はそのまま維持)
- README に同期状態の表を追記

### 修正後の検証
- Linux manylinux_2_28/2_17/2014 × py3.13 → 解決OK
- Linux × py3.12 → 解決OK
- Windows win_amd64 × cp313 → 解決OK
- 実venv vs `requirements-windows.txt` → **9/9 完全一致**
- 実venv vs `requirements.txt` → tensorflowのみ意図的差分(2.21.0 vs 2.20.0)、gunicornはWindows不要

---

## 11. 2026-11-09 追加学習(再学習)パイプラインを ai2 に移植

ユーザー指摘: 「`caregiver_safety/data` に追加学習の材料があったが実装したか。ai2 に入っていない」
→ **当時は推論用モデル4ファイルだけをコピーし、材料・学習スクリプトは未移植だった**ので移植した。

### 取り込んだ材料(ai2内)
| 場所 | 中身 |
|---|---|
| `train_data/` | 姿勢CSV **39個**(`fall_*` 13 / `normal_*` 26、23.2MB)。99値+ラベル+人物番号 |
| `train_dataset/` | 学習テンソル(X/y, state_X/state_y, state_labels)。元ファイルは `_caregiver_original/` に退避 |
| `ai_models/candidates/` `ai_models/backup/` | 学習候補 / 反映前の旧モデルの退避先 |

### 追加したコード
- `core/training.py` … 共通処理(パス・CSV読込・シーケンス生成・状態ラベル対応表 `STATE_BY_SUFFIX`・backup/promote)
- `core/management/commands/ai_collect_data.py` … カメラ→MediaPipe Pose→CSV収集(`--device --backend --label --name --seconds --no-gui`)
- `ai_prepare_dataset.py` / `ai_prepare_state_dataset.py` … CSV→.npy(`--dry-run` `--list-unmapped`)
- `ai_train_fall.py` / `ai_train_state.py` … LSTM再学習(`--epochs --batch-size --promote`)
  - **既定は `candidates/` へ出すので現行モデルは壊れない**。`--promote` で旧モデルを backup してから反映
  - 状態モデルは `state_labels.npy` も**セットで**反映(出力次元とラベル順の一致を守る)
- `ai_check_model.py` … 検証(精度/混同行列/CSV別誤判定。**既定で AI設定の転倒閾値で評価**、`--path` で候補も検証可)
- `settings.py`: `AI_TRAIN_DATA_DIR` / `AI_TRAIN_DATASET_DIR` を追加
- 依存に **pandas==3.1.1 / scikit-learn==1.9.1** を両requirementsへ追加(Linux/Windows cp313で解決確認済)

### 元スクリプトからの改善点
- `load_csv_features` が**先頭行が数値なら `header=None` で読む**
  (元の `prepare_dataset.py` は先頭行をヘッダ扱いして1フレーム失っていた)
  → その結果 転倒データは 2,278 → **2,288** シーケンス(状態は 301 のまま一致)
- 壊れたCSVがあっても全体を止めず `[SKIP]` 理由を列挙
- 学習 → 検証 → 反映 の順を強制(candidates + `--promote`)

### 検証結果(実測)
```
ai_prepare_dataset        : X (2288,30,99) NORMAL 1912 / FALL 376(16.4%)
ai_prepare_state_dataset  : state_X (301,30,99) 8クラス内訳OK
ai_check_model(現行, 閾値0.77): 正解率 96.68% / 誤検知率 0.26%(5件) / 見逃し率 18.88%(71件)
ai_train_fall --epochs 3  : 候補生成OK(検証Acc 86.9% / 0.77閾値 86.68%)→ 候補は廃棄
ai_train_state --epochs 2 : クラス重み計算・候補生成OK → 候補は廃棄
ai_check_model --path candidates/... : 候補検証OK
manage.py check / verify_pages : 全通過
```
- 現行モデル(`ai_models/*.keras`)は無変更(テスト候補は削除済み)

### 運用メモ
- **見逃し(FALL→NORMAL)18.9%が課題**。`fall_歩行中に転倒…` 系CSVで誤判定59〜91%
  → `ai_collect_data --label fall` で該当動作を追加収集 → 再学習が効く
- `ai_collect_data` は **runserverを止めてから**実行(AI常時稼働がカメラを掴むため)
- `--promote` 後は **サーバー再起動が必要**(モデルは初回使用時にキャッシュされる)

---

## 12. 2026-11-09 再学習をサーバー起動時の自動化

ユーザー指摘: 「やることは同じだし、コマンドというより DjangoでServer建てる時自動化してほしい」
→ 手動コマンドベースを **起動時自動**へ変更(`core/retraining.py`)。

### 仕組み
- `train_data/*.csv` の**指紋**(ファイル名+サイズ+mtime の sha256先頭16桁)を
  `train_dataset/retrain_state.json` と比較して **変わった時だけ**学習する
  - 変わらなければ数秒で素通り → **autoreload の再起動を繰り返しても無駄に学習しない**
- 変わった時: データセット再生成 → 候補を `ai_models/candidates/` へ学習 →
  **同じ保持データで現行モデルと比較** → 劣がなければ `ai_models/` へ反映(旧版は `ai_models/backup/<日時>/`) → 指紋記録
- 反映判定は AI設定の転倒閾値での **正解率 / 誤検知率 / 見逃し率** の3指標
  (`AI_RETRAIN_PROMOTE_MARGIN` 以内的劣化は許容)。劣る場合は**見送って候補だけ残す**
- 学習失敗時は現行モデルをそのまま使い、Webアプリと解析は止めない
- 呼び出しは `core/apps.py` の起動スレッド内、**常時稼働を開始する前**
  (学習がCPUを奪って解析と争うのを避ける。ロック取得後のため二重起動でも1プロセスしか学習しない)

### 追加/変更したファイル
| ファイル | 内容 |
|---|---|
| `core/retraining.py` | 新規。`needs_retrain()` / `run_startup_retraining()` / `last_run_summary()` |
| `core/training.py` | モデル定義・学習・評価を共通化(`build_*_model` / `train_fall` / `train_state` / `evaluate_fall_at_threshold` / `data_fingerprint` / `read_state` / `write_state`) |
| `core/management/commands/ai_retrain.py` | 新規。`--status` / `--force`(手動実行・確認用) |
| `ai_train_fall.py` / `ai_train_state.py` | 共通関数を使うよう整理(モデル定義の重複を排除) |
| `core/apps.py` | 起動時に自動再学習 → その後 常時稼働開始 |
| `core/views.py` | `/debug/camera/ai/` に `retrain`(最新実行情報)を追加 |
| `templates/debug/camera.html` `static/js/debug_camera.js` `static/css/style.css` | AIカード下に「自動再学習 最終: …」を表示 |
| `config/settings.py` | `AI_AUTO_RETRAIN` / `AI_RETRAIN_EPOCHS_FALL` / `AI_RETRAIN_EPOCHS_STATE` / `AI_RETRAIN_PROMOTE_MARGIN` |

### 検証結果(実測)
- `ai_retrain --status`: 指紋 `73d834dcedc96b35` / 39ファイル / 実行必要=はい(記録なし)
- **ガード検証**(`AI_RETRAIN_EPOCHS_*` を一時2にして `ai_retrain --force`):
  - 転倒: 現行 acc 96.94%・見逃し17.33% vs 候補 acc 88.43%・見逃し69.33% → **反映見送り**
  - 状態: 現行 72.13% vs 候補 14.75% → **反映見送り**
  - `ai_models/*.keras` のMD5は実行前後で**同一**(現行維持)、`backup/` は空、候補のみ生成、14.5秒
  - → テスト候補は削除、エポックは50へ復元
- 2回目以降: `[Retrain] データ変更なし(39ファイル)` で素通り
- runserver 起動ログ: `[Retrain] データ変更なし…` → 常時稼働開始 → `/debug/camera/` は 9.8fps
- UI表示: 「自動再学習 最終: 2026-11-09 11:00:58 / 材料 39件 / 転倒モデルは現行維持 / 状態モデルは現行維持 / 14.5秒」
- `manage.py check` / `verify_pages` 全通過
- 実運用で転倒検知→通知バナー(未対応4件)を確認

### 運用
- **材料を増やす操作だけユーザーが実施**: `python manage.py ai_collect_data`(またはCSVを `train_data/` に置く)
- 次回 `runserver` 起動時に自動で 前処理→学習→比較→反映
- 即時反映したい / 状況を見たいときだけ `python manage.py ai_retrain`(または `--status` / `--force`)

---

## 13. 2026-11-09 複数カメラ同時稼働の完全対応(2台とも常時解析)

ユーザー指摘: 「2台接続した。**複数のカメラを同時に動かし、すべてにリアルタイムAIを起動**するのが想定。
現状は1台のみ動いている。カメラデバッグタブにいなくても自動起動してほしい」

### 実機で切り分けた事実(専用テストで測定)
| 構成 | 結果 |
|---|---|
| DSHOW 順 1→0 | **両方開く**。index1=30fps / index0=**1.0fps** |
| DSHOW 順 0→1 | index1 は**開けない** |
| MSMF(どちらが先でも) | **1台のみ**。2台目は開けない/ハング |
| MJPG+320x240 を構築時パラメータで要求 | index1 は交渉できる(実320x240)が **index0 は640x480のまま**、かつ1.0fps不変 |
| open 後の `cap.set()` | 解像度・FOURCCとも**無効**(実フレームは640x480のまま) |
| 実フレーム画像 | index0=**黒画面** / index1=部屋全体が映っている |

→ **1台しか動かなかった直接原因はソフトウェア側**だった:
`camera_service.acquire()` が「初フレームを短時間に3回読む」前提で、
2台目は帯域の都合で初フレームが約1秒遅れるため**起動失敗扱い**にされ、
さらに見張りが `error` にして300秒間再試行しない、という状態だった。

### 修正
| # | 内容 | 実装 |
|---|---|---|
| 1 | 初フレーム待ちを用途別に分離。検知=短い(`AI_CAMERA_FIRST_FRAME_SECONDS` 3秒)/ 配信・常時稼働=長い(`AI_CAMERA_OPEN_SECONDS` 10秒) | `camera_service._open_capture(cv2, index, first_frame_seconds)` / `acquire()` |
| 2 | 常時稼働は**降順に1台ずつ順番に開く** | `RealtimeAIEngine.start_persistent()` |
| 3 | 低速(1fps)でもフレームが来ている間は**止めない**。0フレームのときだけ見張りが停止 | `_monitor_loop`(判定は `last_frame_at`) |
| 4 | 停止後の再試行を速くする(300秒→**60秒**) | `AI_STARVED_RETRY_SECONDS = 60` |
| 5 | 一覧に「1 fps(**低速/帯域不足**)」として明示。目標fpsの7割未満で `low_fps` | `summary()` / `debug_camera.js` / `.is-warn` |

**失敗した手** (再発防止のため記録):
- 全カメラを**並列スレッドで open** → 後発カメラが1フレームも返さず `error` に。
  DSHOW のフォーマット交渉は直列に行う必要があり、**逐次開放に戻して解決**。

### 検証結果(実測)
```
起動ログ : [RealtimeAI] 常時稼働を開始しました: index [0, 1]
検知一覧 : 0:Logi C270 HD WebCam(使用中)/DSHOW  1:Web Camera - HD(使用中)/DSHOW
AI状態   : index 0 = running 1fps pers=true low=true
           index 1 = running 9.7fps pers=true
チップ   : 「2台を常時解析中」
カメラデバッグタブを閉じた後も : 両方 running が継続(25秒後にも確認)
check / verify_pages : 全通過
```
- 実運用で index 1 の転倒検知→通知バナーも確認(未対応5件)

### 残る制約(ハードウェア側)
同一USBコントローラ/ハブに2台を繋ぐと2台目が1fpsへ落ちます(解像度を落としても改善せず)。
**满速で複数台運用するならカメラを別コントローラへ分散接続**してください。
ソフトウェアは1fpsでも検知・転倒判定・録画・通知を継続します。

---

## 14. 2026-11-09 ライブ表示の復旧(AIとフレームを取り合っていた)

ユーザー指摘: 「裏では動いているようですが**ライブ表示ができない**。すでにAIカメラが動いた上で
さらにカメラにアクセスしているのでエラーが出ているのでは」→ 推察どおりでした。

### 原因(2つ重なっていた)
1. **フレームの取り合い**: AI常時稼働が `camera_service` の同じキャプチャから `cap.read()` しているのに、
   配信側(`debug_camera_stream`)も `read_jpeg()` で読んでいた。
   1枚のフレームはどちらか一方しか取れないため、**1fpsのカメラでは必ずどちらかが空振り**になる。
2. **空振りで配信を切っていた**: 配信ループが `if frame is None: break` になっており、
   初回に1枚も取れないと即終了 → ブラウザは「エラー」表示。

### 修正
| # | 内容 | 実装 |
|---|---|---|
| 1 | 配信は **AIが描画済みのJPEGを優先**して流す。新フレーム判定は更新回数 `frame_seq` | `_CameraRuntime.frame_seq` / `RealtimeAIEngine.frame_seq(device)` |
| 2 | **AIがそのカメラを読んでいる間は生読みしない**(取り合い防止)。読んでいない時だけ `read_jpeg` | `RealtimeAIEngine.is_analyzing(device)` |
| 3 | フレームが一時的に無いだけでは**配信を切らない**。`AI_STREAM_IDLE_SECONDS`(既定60秒)連続取得不能で終了 | `views.debug_camera_stream` |

### 検証結果(実測)
```
index 1(30fpsカメラ): 配信サーバー側で 6秒に60フレーム = 10fps / 初フレーム 185ms
index 0(1fpsカメラ) : 12秒に13フレーム ≈ 1.1fps / 初フレーム 1.9秒 / 切断されず継続
UI: 両方「配信中」640x480、エラーなし。AIカードは「2台を常時解析中」
検知一覧: 0:Logi C270 HD WebCam(使用中) / 1:Web Camera - HD(使用中)
check / verify_pages : 全通過
```
- 1fpsカメラのライブはコマ落ちして見えますが、一覧に「**1 fps(低速/帯域不足)**」を表示しているので
  障害ではなく帯域制約だと分かります。

### 教訓(再発防止)
- **1つのキャプチャを複数スレッドで `read()` しない**こと。
  視聴者へは必ず「解析済みの最新1枚」を配る設計にする(=`latest_jpeg` + `frame_seq`)。
- 「1回読めなかった」を致命的エラーにしない(低速・起動直後・モデル読込中は正常な状態)。

---

## 15. 2026-11-09 indexとカメラ名の取り違え / 映像が反転していた問題

ユーザー指摘: 「index 0 のWebカメラを起動すると **index 1 のカメラの映像が反転したまま**流れる。
本来 index 0 がノートPC内蔵、index 1 が外付けwebカメラのはず」

### 原因1: 左右反転(推論と学習データの向きが違っていた)
| 元の実験コード | 挙動 |
|---|---|
| `caregiver_safety/realtime_lstm.py`(推論) | 検出前に `cv2.flip(frame, 1)` していた |
| `caregiver_safety/collect_data.py`(学習データ収集) | **反転していない** |

ai2 の解析ループは realtime_lstm.py を移植したため flip を引き継いでおり、
**学習時と違う向きで推論**していた(判定がずれるうえ、ライブ映像も反転して見える)。

→ **`AI_MIRROR_INPUT = False`(既定)で反転なし**にし、学習データの向きと揃えた。
ミラー表示したい場合だけ `True` にする(その場合は `ai_collect_data` も同じ向きで収集して再学習)。

### 原因2: OS列挙順とOpenCV index順の不一致を推測で埋めていた
`_run_detection` が PnP の列挙順(`Logi C270 → Web Camera - HD`)を index 0,1 に
そのまま当てはめていたが、**DSHOWのindex順は逆**だったため名前が入れ替わっていた。

→ 対応:
1. **複数台のときはOS名を当てはめない**(1台のときだけ一意的なので使用)
2. **`DEBUG_CAMERA_NAME_MAP`** で運用側が確定した名前を指定(本機は `0=内蔵 / 1=外付け` をユーザー確認済)
3. **一覧に実フレームのサムネイル**を表示(`media/camera_snapshots/cam{index}.jpg`)
   検知時(`_probe`)と解析開始時(`ai_engine` の初フレーム)の両方で保存するので、
   AIがカメラを掴んでいる状態でも目視でどれがどれか確認できる
4. サムネイルはCSS依存だとスタイルシートキャッシュで原寸表示になりレイアウトが崩れたため、
   **インラインでサイズ確定**(88×66)

### 検証結果(実測)
```
検知一覧: 0:ノートPC内蔵カメラ (Web Camera - HD)  src=map  snap=cam0.jpg
          1:外付けWebカメラ (Logi C270 HD WebCam) src=map  snap=cam1.jpg
サムネイル表示: 2枚 / 88x66 / レイアウト正常
ライブ表示(index 0): 配信中 640x480、反転なし(背景の "6B PC Room" の文字が正しく読める)
AIカード: 「2台を常時解析中」
check / verify_pages : 全通過
```

### 教訓
- **index と実機の対応は列挙順から推測しない**。運用名は設定で明示し、実フレームのサムネイルで検証できるようにする
- **学習データと推論の前処理は必ず同じ向き**にする(元コードの flip を無批判に移植したのが原因)

---

## 16. 2026-11-09 「内蔵カメラにアクセスできていない(LED消灯)」の切り分け

ユーザー報告: 「Webカメラ側の表示が出ていない。**物理アクセスランプが消えている**ので
ノートPC内蔵カメラにアクセスできていない」

### 実機で測った硬件側の事実
まず PnP を確認: 両カメラともUSB機器(`VID_046D`=Logi外付け / `VID_5986`=Chicony内蔵)。
単独開放では**どちらのカメラもどちらのバックエンドでも正常に映る**(内蔵=顔アップ / 外付け=部屋全体)。

同時に開いた場合:

| 開放順 | 結果 |
|---|---|
| 内蔵 idx0 → 外付け idx1 | 内蔵 30fps / **外付けは開けない** |
| 外付け idx1 → 内蔵 idx0 | 外付け 30fps / **内蔵 1fps(実質黒)** |
| 内蔵 MSMF 先行 → 外付け DSHOW | 内蔵 30fps / **外付けは開けない** |
| 両方 320×240 へ下げる | 結果同じ → **帯域不足ではなく同時ストリーム自体が不可** |
| 外付け **MJPG 320×240** / 内蔵 320×240 YUY2 | 外付け30fps / 内蔵1fps。合計≈2.6MB/s(USB2の1/10) → **帯域は原因でない**と確定 |
| 両方 MSMF 320×240 @15fps | 1台目OK / 2台目は映像なし・開けない |
| 外付け DSHOW MJPG640@10 + 内蔵 MSMF 320×240@10 | 2台同時不可(順不同でどちらもう片方が落ちる) |

**結論: この開発PCでは2台同時の映像取得はできない。**
現行の既定(降順=外付け先行)では内蔵が後発になり、
映像が0コマのときは見張りが解析を止めるため **アクセスランプが消えていた**(=報告どおり)。

### ソフトウェア側の対応
| # | 内容 | 実装 |
|---|---|---|
| 1 | **開く順序=優先度を明示設定化** `AI_CAMERA_PRIORITY`(既定None=降順。`[0,1]`で内蔵優先) | `RealtimeAIEngine.target_devices()` |
| 2 | 飢えたカメラの状態を**分かりやすいメッセージに**(制約と対処法を明記) | `_monitor_loop` の error 文言 |
| 3 | **同時ストリーム検査コマンド** `list_cameras --simultaneous [--seconds]`。全カメラを同時に開いて実効fpsを出し、硬件的に可能かを判定 | `core/management/commands/list_cameras.py` |

### 検証
```
list_cameras --simultaneous:
  index 1: OK 640x480 / index 0: OK 640x480
  index 1: 24.4fps / index 0: 1.0fps(実質映像なし)
  → 2台中1台だけが同時に映像を出せません。USBコントローラ/ハブを分けて接続してください

アプリ(外付け優先のまま):
  index 0 = running 1fps(低速/帯域不足) 解析中
  index 1 = running 9.6fps 解析中
  一覧: 0=ノートPC内蔵カメラ / 1=外付けWebカメラ + サムネイル2枚
check / verify_pages : 全通過
```

### 本番への引き継ぎ事項
- **複数カメラ運用の成立条件は硬件側**: カメラを別USBコントローラへ分散し、
  設置後に `python manage.py list_cameras --simultaneous` で
  「全カメラ同時に映像が出ています」を確認してから稼働させる
- 確認できない環境では `AI_CAMERA_PRIORITY` で主カメラを明示する(副カメラは低速/待機になる)
- アプリ側は「1台だけ動かす」ことも「N台同時に動かす」ことも同じ構成で対応済み

---

## 18. 2026-11-09 IPカメラ(RTSP / HTTP-MJPEG)常時解析の実装

USBの2台同時制約に対する解決策として(ユーザー選択: IPカメラ化)、
**台帳の `Camera.stream_url` からネットワークカメラを常時解析**できるようにした。

### 設計
- ソースキーを **USB=整数index / IP=`cam:<Camera.id>`** の2種で統一
  (`camera_service.is_network_key()` / `source_key()` / `snapshot_name()`)
- `camera_service`
  - `network_sources()`: 台帳から `stream_url` を持つオンラインカメラを列挙
    - **`.mp4/.mov/.avi/.mkv/.webm/.m4v` はライブ配信でないので自動除外**(デモ録画URLに多い)
    - `AI_IP_CAMERA_IDS` で明示指定した場合はこの制限も無視
  - `_acquire_network()`: URLを開いて初フレームを確認(ロック外・`AI_NETWORK_OPEN_SECONDS`)
  - `read_frame()`: IPソースは読み取り失敗時に `AI_NETWORK_RECONNECT_SECONDS` 間隔で**再接続**
  - `stream_keys()` / `acquire()` / `hold()` / `unhold()` / `release()` はキー型に非依存
- `ai_engine`
  - `target_devices()`: **IPキーを先頭**に、続いてUSB(降順/`AI_CAMERA_PRIORITY`)
  - `_camera_for()`: IPキーなら**台帳の実レコード**を返す(アラートのカメラ名/場所が正しく出る)
  - `status()`/`summary()`/`devices()`/`stop_persistent()`: intとstr混在でも壊れないよう
    **`key=str` でソート**、`int()`変換は「数値のときだけ」に修正
- `views`
  - `debug_camera_devices`: 一覧にIPカメラを追加(kind=ip / url / in_use / サムネイル)
  - `_parse_device_key()`: `?device=` を int または IPキーとして解釈
  - `debug_camera_stream`: 視聴参加と常時稼働開始を別tryに分ける
    (片方の失敗で `ai_session` を落として**ライブが黙って止まる**のを防止)
- `templates/debug/camera.html` + `static/js/debug_camera.js`: IPはラベルを `IP`、
  キー比較は `dataset.key`(文字列)、URLは `encodeURIComponent`

### 途中で踏んだバグ(再現注意)
| 症状 | 原因 | 修正 |
|---|---|---|
| `/debug/camera/ai/?device=cam:8` が **HTTP 500** | `status()` が無条件に `int(device)` していた | 数値化できる時だけ int |
| IPカメラのライブが**無反応(ハング)** | `start_persistent()` 内の `int(device)` が例外 → viewのexceptで `ai_session=False` → 「AI解析中だから生読みしない」分岐に入り**何も出さなかった** | int変換を撤去 + viewで視聴参加と常時稼働開始を分離 |
| summary/devices のソートで落ちるリスク | int と str の混在ソート | `key=str` で統一 |

### 検証(ローカルMJPEGサーバで実測)
```
OpenCV読取: 14.7fps(8秒で118フレーム)
台帳登録: Camera id=8 / stream_url=http://127.0.0.1:8899/mjpeg / online
常時稼働: cam:8 = running 9.7fps pers=true
   → USB2台と同時: 「3台を常時解析中」(0=1fps / 1=9.8fps / cam:8=9.7fps)
ライブ表示: overlay付きで 51フレーム/5秒 ≈ 10fps、初フレーム 0ms、320x240
一覧: index 0 / index 1 / IP 検証用IPカメラ(url表示) + サムネイル
check / verify_pages : 全通過
検証用カメラ(id=8)・テストMJPEGサーバ・一時スクリプトは削除済み
```

### 本番での使い方
1. `/admin/` で Camera の `stream_url` に `rtsp://…` や `http://…/mjpeg` を設定し「オンライン」に
2. `runserver` 起動 → USB/IPを問わず**全カメラを常時解析**
3. `/debug/camera/` で選んで「表示開始」(見なくても解析は継続)
4. 台数増加はUSBではなくネットワーク帯域に依存する(スイッチ/APの容量を確認)

### 複数台を実現する解決策(候補)
| 案 | 内容 | 評価 |
|---|---|---|
| 別USBコントローラへ分散 | 内蔵は内部USB直結なので、**外付けを別バスのポート/ドック/PCIe増設**へ | 最安。まず試す価値あり |
| 自己給電USB3ハブ+増設コントローラ | バス分離が目的(帯域ではない) | 効果は環境次第 |
| **IPカメラ(RTSP/MJPEG)化** | USBを介さないため台数がUSB制約から解放される。施設の本番運用として一般 | **最も確実 → 第18章で実装済み**(`Camera.stream_url` に登録するだけ) |
| マルチ入力キャプチャ集約機 | 複数カメラを1USB機器に纏める | 1デバイスに見えるので競合しない |
| 1台に絞る(`AI_ALWAYS_DEVICES=[1]`) | 確実に動く1台だけ常時解析 | 即できる。台数は1 |
| 交互ローテーション | 数秒ごとに開き替え | **転倒検知には不適**(切替中が死角) |

**実施済み**: IPカメラ(RTSP/MJPEG)取り込み → 第18章。USB制約から解放され、
実機で「内蔵USB + Piカメラ」の2台同時常時解析を確認した。

---

## 20. 2026-11-10 デモ用の擬似アラート生成をすべて削除

ユーザー指示: 「デモで問題を発生する機構はすべて削除。今後は必要ない」

### 削除したもの
| 対象 | 中身 |
|---|---|
| `core/ai_simulator.py` | **ファイル削除**。5秒ごとに人物検知・転倒・無動作・誤検知をランダム生成し `Alert`/`Recording`/`PersonDetection` へ書き込んでいた |
| `core/ai_monitor.py` | **ファイル削除**。擬似パイプライン。※`AI_MONITOR_ENABLED` は定義だけあって**どこからも起動されていない死んだコード**だった |
| `settings.py` | `AI_SIMULATOR_ENABLED` / `AI_MONITOR_ENABLED` / `DEMO_VIDEO_URL` を削除 |
| `core/apps.py` | シミュレーター起動ブロックを削除(起動するのは自動再学習と実AI常時稼働のみ) |
| `views.settings` | `action == "manual_alert"`(手動で異常検知を1件発生)を削除 |
| `templates/accounts/settings.html` | 「🚨 手動で異常検知を発生させる」ボタンとJSを削除。カード名を「デモ：AI自動検知」→「**AI監視の稼働**」に(ON/OFFは実AIの `ai.enabled` を制御する本物の機能なので残置) |
| `seed_demo.py` | 偽の異常8件・録画4本・解決報告2件の投入を削除。**アカウント/カメラ台帳/利用者の投入は残した**(初期セットアップ手段のため) |
| `PersonDetection` | **モデルごと削除**(第18章までで実AIは書かないと判明)。`core/migrations/0003_delete_persondetection.py` を適用済み。`/api/camera/<id>/detections/` とadmin画面も削除 |

### 結果
- **`core_alert` に登録されるのは実AI(`core/ai_engine.py`)の検知のみ**になった
- `core_*` テーブルは 7個(core_persondetection 消滅)、DBの総テーブル数 12
- 削除後の残存参照はゼロ(コメント内の経緯説明と、過去のマイグレーションのみ)

### 検証
```
manage.py makemigrations → 0003_delete_persondetection.py 生成
manage.py migrate        → Applying core.0003_delete_persondetection... OK
manage.py check          → no issues
manage.py verify_pages   → 全ページの検証に成功
views.py の未使用import  → なし(ASTで機械チェック)
seed_demo --help         → 正常(モジュール読み込みOK)
```

---

## 21. 2026-11-10 Piカメラの表示遅延(実測2.3秒)を専有読取スレッドで解消

ユーザー報告: 「ラズベリーパイ経由のカメラは6秒程度遅れて表示されるため検知に支障が出る」

### 結論(原因)
HTTP-MJPEG / RTSP は**バイトの管**で、古いフレームを捨てる意味がない。
Piが毎秒30枚作るのに ai2 の解析ループは 1枚36.2ms かかり **27.6fps しか消費できない**。
差分の毎秒2〜4枚が TCPバッファと ustreamer のクライアントキューに積み上がり、
`cap.read()` は**キューの先頭(=最も古い)**を返すので遅れが回復しない。
USBカメラで起きないのはドライバが常に最新1枚へ上書きするため。

### 測定で使った手法(再実行可能)
| 手法 | 何が変わる | 結果 |
|---|---|---|
| Pi側 `ss -tn state established \| grep 8080` の Send-Q | 送信キューの実測 | ai2接続 **2.37〜2.68MB 常時**(=約49枚=1.6〜1.8秒)。同時に生ソケットでフルドレインした接続は **25〜280KB** |
| `ustreamer /state` の clients_stat.fps | 配信が詰まっているか | ai2へは 21〜29fps(=backpressure)。対策後は 30.0fps |
| `ustreamer` の TCP統計 | どちら側が詰まめているか | `rwnd_limited 52.7%`、`rcv_wnd 64KB`、RTT 41.7ms。Piの `tcp_wmem` 上限 4MB = 80枚 = **2.7秒がソケット側の天井** |
| 同一ストリームを2系統で読み FFmpegデコード一致の sha1 で照合 | 消費レートを落とすと遅延が伸びるか | 8fps固定で **2.60s(10秒)→4.00s(60秒)、まだ増加中**。参照側は30.0fps・指紋不一致0 |
| PC画面の点滅 | (失敗) | カメラが画面を見ておらず輝度変化 7/255 のみ。**映像が変わらないので無意味**だった |
| 手を振る | (失敗) | motion 3.7 / ノイズ床 2.4 で相関 0.2 前後。Pi画角では動きが弱すぎる |
| **レンズを指で覆す**(輝度13→227) | 絶対遅延の分解 | 相関0.54で明確。**Pi実時間→ai2配信 = 2.3秒**、**ai2配信→ブラウザ描画 = 0.0秒**(Chromeは無罪) |

### 「6秒」の内訳(上記の実測値で一致)
```
2.3s  映像がai2に届いた時点で既に古い   ← 今回修正した部分
1.1s  LSTMが30枚の履歴を溜める(SEQUENCE_LENGTH=30 ÷ 27.6fps)
0.1s  転倒を3回連続で確認(FALL_CONFIRM_COUNT=3)
2.5s  /api/alerts/ を5秒間隔でポーリングする平均待ち(setInterval(poll, 5000))
────
6.0s  ≈ ユーザーの体感
```

### 修正
`core/camera_service.py` にネットワークカメラ専用の **読取スレッド**を追加した。
- `_acquire_network()` が開いた直後に `camera-reader-<key>` を起動。`cap` はこのスレッド専用
- ループは `cap.read()` を回し続けてパイプを空に保ち、`frame / seq / ts` の**最新1枚だけ**保持
- `read_frame()` / `read_jpeg()` は `cap` を触らず `_latest_frame()` / `_peek_frame()` で受け取る
  (他スレッドが `cap.read()` すると解析側とフレームを取り合いになる)
- 再接続もスレッド内だけで実施(`_reconnect_network` は cap の所有者が1人なので安全)
- 停止は `release()` / `unhold()` が `stop` を立てるだけ。**cap を閉じるのもスレッド自身**
  (read 中に他スレッドが release すると native層で use-after-free)
- `AI_NETWORK_FRAME_STALE_SECONDS`(既定3秒)より古いフレームなら None → 既存の映像途絶見張りが拾う
- 可視化: `frame_age` をエンジン状態へ追加し、`/debug/camera/` に **「映像の遅延」** 欄を出した
  (USBは「USB(行列なし)」= 専有スレッド不要なので構造的に溜まらない)

### 対策後の実測
| 指標 | 対策前 | 対策後 |
|---|---|---|
| Pi側 Send-Q(ai2接続) | 2.37〜2.68MB | **0〜48KB** |
| ustreamer→ai2 配信fps | 21〜29 | **30.0** |
| 解析中のフレームの古さ | 2.3秒 | **0.01〜0.03秒** |
| ai2 消費fps | 27.6 | 27.4(**変らない。届かない前提で設計した**) |

消費レートは改善させていない点が重要。**生成に届かないままでも遅延が積もらない**のが狙い。

### 失敗した手(再発防止)
- `CAP_PROP_BUFFERSIZE` を設定して解決 → **この構成では get/set とも -1(未対応)**。アプリ側で捨てるしかない
- FFmpeg に `fflags=nobuffer` を渡して解決 → OpenCV の VideoCapture は URL 経由ではオプションを渡せない
- ustreamer 側に遅延対策がある → `--drop-same-frames` 等はありますが**クライアントキューの上限設定は無い**
- ブラウザのMJPEG描画を疑った → 実測 0.0秒で無罪。`<img>` の多重接続(1ページ複数タイル)も影響なし

### 残っている遅延(設計上のもので、カメラ固有ではない)
1. **LSTMの30枚窓(≈1.1秒)**: 学習データが30fps前提なので短縮するには再学習が必要
2. **通知の5秒ポーリング(平均2.5秒)**: 1秒にするか SSE/WebSocket でプッシュすれば消える。未実施

### 副次的に分かった実運用上の問題
レンズを指で覆すテスト25分で **転倒アラート29件(信頼度0.77〜0.99)** が実AIから生成された。
Piカメラ19件 + 内蔵カメラ10件(腕を振っただけで0.88)。**カメラが隠されると誤報が連発する**。
遅延より深刻な可能性がある。未対応。

### 検証
```
manage.py check                        → no issues
manage.py verify_pages                 → 全ページの検証に成功
/debug/camera/ のJS                    → console error 0件。「映像の遅延 0.01 秒」表示
常時稼働                             → index 0 = 27.8fps / cam:9 = 27.4fps で同時稼働
```

---

## 22. 2026-11-10 カメラデバッグを「全カメラ一括カード表示」+ 現在時刻(秒)へ

ユーザー指示: 「1つのカメラずつ表示しているが、カード形式みたいにすべてのカメラを一気に表示する方式にし、現在時刻を秒まで表示するようにしてください」

### 変更
| 対象 | 内容 |
|---|---|
| `templates/debug/camera.html` | 単一ビューア(`ライブ表示` + 表示開始/停止 + デバイス選択)を**廃止**し、`全カメラライブ(一括表示)` セクションへ変更。**検知した全カメラをカードで同時配信**。ページ上部に `2026-11-10 14:31:05 この端末の時刻(Asia/Tokyo)` を追加 |
| `static/js/debug_camera.js` | カード生成(`createCard`/`renderGrid`/`destroyCard`)。デバイス一覧が増減しても**既存カードのMJPEG接続は張り直さない**(差分更新)。1秒ポーリングの `devices` を各カードへ反映(`updateCards`)。**「すべて停止」で全接続をクローズ**(視聴者数が下がりサーバの描画負荷も減る)。`beforeunload` でも全停止 |
| カードの中身 | 名前+キー / **撮像時刻(秒まで)** / 映像 / 判定・状態・解析fps・遅延・無動作・視聴。遅延0.5秒超で黄・1.5秒超で赤(カード枠も)。転倒判定中はカード枠が赤 |
| `static/css/style.css` | `.camera-grid` `.camera-card*` `.page-clock*` を追加。`.debug-viewer*`(単一ビューア用)と `.debug-device` の選択系スタイル(hover / is-selected / cursor)は削除 |
| `core/ai_engine.py` | `summary()` の行に `still_seconds` を追加(カードと一覧で無動作秒数を出せるように) |
| `core/context_processors.py`<br>`config/settings.py`<br>`templates/base.html` | **`asset_version` コンテキストプロセッサ新設**。`style.css` と `app.js` は版本無しで読まれていたため、変更してもブラウザが**古いCSS/JSを使い続けて反映されない**(今回の開発で実際に発生)。`?v={{ asset_ver }}`(サーバー起動時刻)を付与 |
| `core/views.py` | デバッグページ専用の `debug_asset_ver`(毎回更新)に分離。開発中は `debug_camera.js` を必ず読み直させ、css/app.js は起動単位で更新 |

### 撮像時刻の仕組み
`撮像時刻 = ブラウザの現在時刻 − frame_age`。`frame_age` は専有読取スレッドがそのフレームをデコードした時刻からの経過秒(第21章で追加した状態値)。**USBカメラは専有スレッドを持たないので `USB(行列なし)`** を表示する(ドライバが常に最新1枚へ上書きし、構造的に遅延が溜まらないため)。

### 実測(2台同時)
```
配信中 2台 / index 0 = 28fps・USB(行列なし) / IP cam:9 = 25.4fps・遅延 0.02秒
ページ時刻 14:31:05 と各カードの撮像時刻 14:31:05 が一致 ← 遅延がそのまま目視できる
「すべて停止」→ status=停止中 / img=1x1(BLANKへ差し替え) / 接続が閉じて視聴数が下がる
「すべて表示」→ 再接続して 配信中 2台 に戻る
console error 0件
```

### はまった点(再現性のある調べ方として記録)
`debug_camera.js` が **SyntaxError: Unexpected token ';'** で動かなかった。原因は三項演算子の `:` 分岐の書き忘れ。
```js
pageTz.textContent = zone ? "この端末の時刻(" + zone + ")";   // 誤(: 分岐なし)
```
`node` が無い環境なので、**ブラウザで本文を fetch して行単位に切り詰め、`new Function()` のエラーメッセージが
`Unexpected token ';'` になる最小の行**を線形探索して位置を特定した(748行中325行目)。
静的ファイルのキャッシュで古いJSが読まれていないかも、同じ方法(版本付きURLを `cache:'no-store'` で fetch)で切り分けた。

---

## 23. 2026-11-10 内蔵カメラのアクセスランプが消灯した問題(検知の再発走)

一括表示に切り替えた直後、ユーザー報告「**カメラデバッグにアクセスできなくなった。カメラの物理アクセスランプが点灯していない**」。

### 切り分け(アプリのせいか、ハード/ドライバのせいか)
サーバーを止めて単一プロセスから直接開いた:
```
OpenCV 5.0.0
MSMF   isOpened=True 読取=30枚 (16.1fps) 初フレーム=0.90s
DSHOW  isOpened=True 読取=30枚 (17.8fps) 初フレーム=0.74s
AUTO   isOpened=True 読取=30枚 (19.5fps) 初フレーム=0.57s
```
`Get-PnpDevice` では `Web Camera - HD` = **Status OK**、カメラのプライバシー設定 = Allow。
→ **カメラとドライバは健全**。アプリ側の「開き直し合戦」が原因。

### 原因(3つが重なっていた)
1. **常時稼働のスキャンが30秒ごとに `detect_devices()` を呼ぶ**。検知キャッシュのTTLも30秒なので毎回フル再検知が走っていた
2. 存在しない index(1〜3)を開こうとすると Windows では**約60秒ハング**する。予算(6秒)で待つのをやめても**裏のプローブスレッドは生き続ける**ので、再検知のたびに積上がる
3. ハングしたスレッドが溜まると Windows のカメラスタックが直列化され、**すでに配信中の index 0 までフレームが止まる**(=ランプ消灯)。実測で index 0 が `loading`→`error`→`loading` を無限往復
4. (フロントエンド)検知完了時に `/debug/camera/devices/` を**引き直していた**ため、完了→キャッシュ切れ→再発走→完了→…のループをページ自身が回していた

### 修正
| 場所 | 内容 |
|---|---|
| `core/camera_service.py` `_probe_with_budget` | 同じ index のプローブが**生存中は起動しない**(`"in_progress"`)。**開けなかった index は TTL まで再試行しない**(`"cached_fail"`)。成否を `_probe_state` / `_probe_threads` で管理し、プローブスレッドは自分で登録解除 |
| 同 `_run_detection` | 新しい戻り値を一覧へ反映(`in_progress`/`timeout` → `busy`、`cached_fail` → `available=False`) |
| 同 `detect_devices(force=True)` | 「再検知」ボタンでは `_probe_state` をクリア(新しいカメラをすぐ見つけられる) |
| `config/settings.py` | `DEBUG_CAMERA_PROBE_RETRY_SECONDS = 120` を追加 |
| `static/js/debug_camera.js` `updateProgress` | 検知完了時は**進捗APIが返す最終一覧**を使い、`/debug/camera/devices/` を引き直さない(再発走ループの断絶) |

トレードオフ: 新しく挿したカメラが「今まで失敗していた index」に出る場合、発見まで最大120秒かかる(「再検知」で即時)。

### 修正後の実測(150秒間、15秒毎に状態を採取)
```
  0s  0=error   cam:9=running fps=27.7 age=0.03
 45s  0=error   cam:9=running fps=27.9 age=0.03
 60s  0=running cam:9=running fps=27.1 age=0.02   ← 内蔵カメラが回復
135s  0=running cam:9=running fps=26.3 age=0.01

フェーズ推移:  index 0 = running 6/10(回復後は維持) / cam:9 = running 10/10
ブラウザ: 配信中 2台 / index 0 = 27.4fps・USB(行列なし) / cam:9 = 27.2fps・遅延 0.01秒
```

### 教訓
**Windows のカメラは「開こうとしただけで他を止める」直列資源**。常時稼働と検知が同じデバイスを
何度も開きに来る構成は壊れる。開けている camera_service の所有権(=`_streams`)を唯一の入口にし、
**開けなかったものは短期間で突かない**のが正解。

---

## 24. 引き継ぎ(2026-11-10 時点の状態)

### 今日やったこと(要約)
1. **デモ用の擬似アラート生成を全削除** → `core_alert` に並ぶのは実AIの検知のみ(第20章)
2. **通知が来ない原因を3つに分解して特定**(OS通知のみ切断。トースト/音は動作)。未実装(第20章末尾)
3. **Piカメラとの不通 = PCが別セグメントへ roaming した**ことが原因。同一ネットワーク化で解決
4. **Piカメラの遅延(体感6秒)を分解して解消**: TCP滞留 2.3秒を**専有読取スレッド**で消した(第21章)
5. **カメラデバッグを全カード一括表示 + 現在時刻(秒)表示**に変更(第22章)
6. **内蔵カメラのランプ消灯**(検知の再発走がプローブスレッドを溜めていた)を修正(第23章)

### 現在の稼働状態(実測)
```
index 0  ノートPC内蔵カメラ   running 27〜28fps / USB(行列なし)
cam:9    Piカメラ 6B PC Room  running 26〜28fps / 遅延 0.00〜0.04秒
Pi: ustreamer 30fps生成 → ai2へ 30fps配信(Send-Q 0〜48KB)
```

### 次にやること(優先順・未実装)
| # | 内容 | 効く理由 / 勘所 |
|---|---|---|
| 1 | **通知の5秒ポーリングを短縮 or プッシュ**(平均2.5秒の遅れが消える) | `static/js/app.js` の `setInterval(poll, 5000)`。1秒にするか SSE/WebSocket。検知自体は速いので通知経路だけの問題 |
| 2 | **レンズ覆し/強いぼやけでの転倒誤検知** | 25分で29件(0.77〜0.99)、腕を振っただけでも0.88。`ai_engine._loop` で「足が画面外+全面ぼやけ」は判定保留にする等の門番が有効。**施設でカメラを隠された時に誤報が連発する**実運用上のリスク |
| 3 | **OS通知が出ない件の実装** | `Notification.permission` が `denied` でも画面に出ない(`app.js` 100行付近)・`document.hidden` ゲート・LAN IP では原理的に使えない(非安全origin)・115行の `'Notification' in window` 無ガード。設定ページに権限状態+「通知を許可する」ボタンを置くのが本命 |
| 4 | `ALLOWED_HOSTS` の `" 133.106.32.114"` **先頭スペース**(無効になっている)+ LAN IP 未登録 | IP直アクセスが400 DisallowedHost になる |
| 5 | 常時稼働スキャンが30秒毎に `detect_devices()` を呼ぶ | 第23章で安くなったが、`DEBUG_CAMERA_CACHE_SECONDS` を上げる/差分検知にするとさらに静かになる |
| 6 | **テストで生成されたアラート/録画が残っている**(`core_alert` 77件・録画同数) | 設定ページの「データ削除(管理)」から消せる。`alert_thumbs` と `media/recordings/*.mp4` も連動削除済み |

### わかっている制約(再現済み・仕様)
- **USBカメラは2台同時不可**(このPC)。順序/解像度/バックエンド/プロセスを変えても先に開いた側が独占。→ 複数台運用は **IPカメラ(ustreamer/RTSP)** が本命。USB1台+IP複数台は実測で同時可
- `AI_ANALYSIS_FPS = 30` は**学習データの時間スケールに合わせるため**(collect_data.py が33.33ms前提)。下げるとLSTMの時間感覚がズレる
- `AI_MIRROR_INPUT = False`(学習データが非反転)。`realtime_lstm.py` の flip は無批判移植だった
- 転倒判定は30フレーム(≈1秒)の履歴が必要 → 検知に最低1.1秒かかるのは**モデルの仕様**
- `camera_service._streams` / 検知キャッシュは**プロセス内変数**。別プロセスから import して状態を覗くと空に見える(誤診した)

### Pi 側の構成(リポジトリ外)
```
systemd: /etc/systemd/system/pi-cam1.service   (enabled, User=student, Restart=always)
ExecStart=/usr/bin/ustreamer --device=/dev/video0 --resolution=640x480 --port=8080 --host=0.0.0.0
配信URL: http://10.24.23.232:8080/stream   ※ /?action=stream は404
状態  : http://10.24.23.232:8080/state     (captured_fps / queued_fps / clients_stat)
詰まり: ss -tn state established | grep 8080   ← Send-Q が滞留バイト数(第21章)
推奨  : DHCP予約でIP固定(stream_url がIP直書きのため)
```

### 検証に使えるコマンド
```powershell
python manage.py check
python manage.py verify_pages            # 全ページ+対応→解決→報告フロー
python manage.py list_cameras --shots    # カメラ台帳とサムネイル
python manage.py ai_retrain --status     # 自動再学習の状態
python manage.py seed_demo               # アカウント/台帳/利用者のみ投入(偽アラートは作らない)
```
`ai_collect_data` はカメラを占有するので **runserver 停止中**に実行する。

### 遅延測定スクリプト(再実行可・一時作業置き場)
次回「また遅くなった」となったとき、そのまま再実行すれば同じ分解ができる。
```
%TEMP%\opencode\probe_cover.py        レンズ覆しで絶対遅延を測る(R=生ソケット / S=ai2配信)
%TEMP%\opencode\analyze_cover.py      その相互相関(撮像→ai2配信→ブラウザ描画)
%TEMP%\opencode\backlog_probe2.py     遅い消費者を再現して滞留秒数の推移を出す
%TEMP%\opencode\cam_direct.py         サーバ停止中にカメラを単独で開けるかの確認
%TEMP%\opencode\watch_stability.py    /debug/camera/ai/ を15秒毎に採取して稼働を監視
%TEMP%\opencode\check_age.py          frame_age(映像の遅延)の確認
```
ブラウザ側の描画時刻を Python へ渡す受け口は `sink.py`(127.0.0.1:9099 で POST をファイル保存)。
**カメラの前で手を振るのは使えなかった**(画角によっては輝度変化が小さすぎて相関が壊れる)。
**レンズを指で覆す**(輝度13→227)なら確実に検出できる。

### 変更ファイル(本日分・すべて未コミット。このディレクトリはgitリポジトリではない)
```
core/camera_service.py     専有読取スレッド / 最新1枚 / プローブのハング抑制
core/ai_engine.py          frame_age(映像の遅延) / still_seconds を状態へ追加
core/views.py              debug_asset_ver 分離 / manual_alert 削除
core/apps.py               AIシミュレーター起動の削除
core/models.py             PersonDetection 削除
core/admin.py              同上
core/migrations/0003_delete_persondetection.py  生成・適用済み
core/management/commands/seed_demo.py           偽アラート投入の削除
config/settings.py         AI_NETWORK_* / DEBUG_CAMERA_PROBE_RETRY_SECONDS / asset_version 登録
config/urls.py             /api/camera/<id>/detections/ 削除
templates/base.html        css/app.js に ?v={{ asset_ver }}
templates/debug/camera.html  全カメラ一括カード + 現在時刻
templates/accounts/settings.html  手動アラート削除、AI監視ON/OFFのみ
static/css/style.css       .camera-grid / .camera-card* / .page-clock
static/js/debug_camera.js  カード生成・runningのみ接続・時刻表示
INTEGRATION_STATUS.md / README.md  第20〜24章・各節の更新
```

---

## 25. 2026-11-11 Piが「No signal」→ ustreamer をやめて libcamera 配信に切り替え

ユーザー報告: 「ラズベリーパイを再起動したら No signal になった。IPアドレスは同じ」

### 切り分け(上から順に潰した)
| 確認 | 結果 |
|---|---|
| PCのIP / 到達性 | `10.24.23.42` / ping OK / 8080 OK → **ネットワークは無傷** |
| `GET /state` | `online:false` `captured_fps:0` `queued_fps:1` → **ustreamerは動いているが映像が無い** |
| `ls /dev/video*` / `v4l2-ctl --list-devices` | `/dev/video0 = unicam(platform:3f801000.csi)` |
| `lsusb` | マウス・キーボードのみ。**USBカメラは1つも繋がっていない** |
| `journalctl -u pi-cam1` | 毎秒 `Can't start capturing: Invalid argument` |
| `dmesg` | **`unicam: Wrong width or height 640x480 (remote pad set to 3280x2464)` / `Failed to start media pipeline: -22`** |
| `media-ctl -p -d /dev/media0` | `imx219 10-0010` → `unicam-image:/dev/video0` の1本リンクのみ(pipelineにISPが入っていない) |
| `uname -r` / apt履歴 | 起動中 **6.18.60+rpt-rpi-v8**(6.18.50 から更新されていた)。`uptime -s = 2026-11-11 09:34:38` |
| `rpicam-vid --list-cameras` | imx219 の mode に **640x480 / 1640x1232 / 1920x1080 / 3280x2464** が並ぶ |
| `rpicam-still -o /tmp/shot.jpg` | **68KB の正常なJPEG**(色も正しい)→ カメラとISPは無傷 |

### 結論
**ustreamer(V4L2直接)ではこのCSIカメラから640x480を取れない。** unicam はセンサーの
mode と同じサイズしか受け付けず、しかも `/dev/video0` は **Bayer RAW 専用**(YUYV を要求すると
`No such input format: YUYV` で弾かれる)。libcamera(rpicam-vid)はセンサー mode を選べて
ISP 変換も通すので、そこだけが解決できる。

### 失敗した手(記録)
- `media-ctl --set-v4l2="'imx219 10-0010':0 [640x480/10]"` → `Invalid argument`。
  **文法は `[fmt:SRGGB8_1X8/640x480]`**(mbus code 必須)。直すとセットは成功したが、
  センサーを640x480にすると unicam は **YUYV を offering やめ、RAW だけ**になった → 色が壊れる。
  さらに **再起動で mode は 3280x2464 に戻る**ので恒久策にできない
- ustreamer を 3280x2464 で動かす → CPUのJPEG符号化が追いつかない(8MP)上、色も出ない
- `rpicam-vid --listen 8099` → `unrecognised option '--port'`、`--listen` は真偽値で**ポートを開かなかった**
- `--mode 1640x1232` / `1640x1232@30` / `1640x1232/30` → 全て `Invalid mode`。
  **正しい文法は `--mode W:H:bit-depth:packing`**(例 `1640:1232:10:P`)
- 「手を振る」「画面点滅」で遅延を測る → 画角により信号が弱く相関が壊れる(第21章)

### 実施
`/usr/local/bin/picam_stream.py`(新規・stdlibのみ)を置き、`pi-cam1.service` の
`ExecStart` を ustreamer からそれへ変更した。

- `rpicam-vid -t 0 -n --codec mjpeg --quality 80 --width 640 --height 480 --framerate 30 --mode 1640:1232:10:P -o -`
  の stdout を専有スレッドが読み、**JPEG境界(FFD8/FFD9)で切り出して最新1枚だけ保持**
- `/stream` は ustreamer と**同じヘッダ・同じ boundary(`boundarydonotcross`)**で multipart 配信
  → ai2(OpenCV/FFmpeg)とブラウザは無変更
- `/state` は ustreamer と同じ形のJSON(`online/captured_fps/frame_age/restarts`)
- rpicam-vid が死ぬ/10秒フレームが無いと**自動再起動**
- `mode=1640:1232:10:P` は**全画幅 binning**。640x480 mode だと中央クロップで画角が狭く、
  部屋がほぼ写らなかった(実測で確認)

**Pi側のファイルはリポジトリに置いていない**(運用指示どおり)。

### 実測
```
/state            online:true captured_fps:30 frame_age:0.001〜0.023秒(クライアント接続中も)
OpenCV 読取       30.1fps / 初フレーム 0.06s / JPEG 24.5KB(ustreamer時 50KB → 帯域半分以下)
Pi Send-Q(ai2)  20KB前後(第21章の 2.45MB と比較)
ai2 側            cam:9 running 27.8fps / 映像の遅延 0.03秒 / カードの撮像時刻=ページ時刻
```

### 未確認(ユーザーに依頼)
テスト中に撮った画像が**90度横向き**だった。ただしその瞬間は手でカメラを掴んでいる写りなので、
**設置し直した向きで確認**が必要。横向きのままなら `ExecStart` の rpicam-vid 引数へ
`--rotation 90|180|270`(または `--hflip 1` / `--vflip 1`)を追加する。

---

## 26. 2026-11-11 Piへの運用アクセス(SSH)と自己回復の実測

### 障害になったもの
GitHub 用の手順で `~/.ssh/id_ed25519` が**再生成**され、Pi の `authorized_keys` と不一致になった。
さらに新しい秘密鍵は**パスフレーズで暗号化**されており、`ssh-agent` が `Stopped/Disabled` だった
ため、非対話接続(`BatchMode=yes`)が鍵を復号できなかった。

```
Load key "C:\Users\student\.ssh\id_ed25519": incorrect passphrase supplied to decrypt private key
student@10.24.23.232: Permission denied (publickey,password)
```

**Pi側は無傷だった**(authorized_keys を疑う前にこちらを確定させた):
```
/home/student          drwx------   ← StrictModes 適合
/home/student/.ssh     drwx------
~/.ssh/authorized_keys -rw-------   2行・末尾 \n(\r なし)
```

### 復旧手順(実施済み)
```powershell
# 1) 鍵あきを自動起動にし、パスフレーズで解いた鍵を常駐させる(管理者PowerShellで1回)
Set-Service ssh-agent -StartupType Automatic
Start-Service ssh-agent
ssh-add $env:USERPROFILE\.ssh\id_ed25519

# 2) 新公開鍵を Pi へ登録(自分の端末でパスワード1回)
ssh student@10.24.23.232 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```
**注意**: `type ~/.ssh/id_ed25519.pub | ssh ... "cat >> authorized_keys"` は
PowerShell が **CRLF の `\r` を挟む**ため事故る。Pi側で `echo 'ssh-ed25519 AAAA...' >> ~/.ssh/authorized_keys`
と直接書くか、`tr -d '\r'` で除去する。

### Pi側に置いた運用許可(私が単独でメンテできるように)
```
/etc/sudoers.d/pi-cam1:
  student ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart pi-cam1.service, /usr/bin/systemctl stop pi-cam1.service
/usr/local/bin/picam_stream.py : 所有を student へ変更(sudo なしで更新できる)
```
範囲は **pi-cam1 の stop/restart とそのスクリプトだけ**(`visudo -c` で parsed OK 確認済み)。
`reboot` は入れていないので、リブートは人の操作が必要。

### 自己回復の実測(第25章の構成への検証)
```
rpicam-vid pid=3518 を kill
  10:46:57  [picam] rpicam-vid 終了
  10:46:59  [picam] rpicam-vid 起動 pid=3538      ← 2秒で復帰、配信は途切れない
/state    online:true  captured_fps:30  frame_age:0.003  restarts:1
```
`restarts` は rpicam-vid の**終了ごとに増える**(旧版はプロセス終了経路を数えていなかった)。

### リブート試験(実施・合格)
`sudo -n reboot` で再起動し、人の操作なしで復帰するかを時系列で計測した。

```
送信 10:49:10
  +61s  ping 応答
  +65s  8080 TCP 受付
  +75s  /state online:true captured_fps:30   ← 完全復帰
```

復帰後の実測(`up 1 minute`、`pi-cam1 = active`、`restarts:0`):
```
/state            frame_age 0.022〜0.032秒
PC側 OpenCV 読取  isOpened 0.03s / 30.1fps / 初フレーム 0.04s
クライアント接続中も frame_age 0.03秒(=行列を作らない設計が再起動後も有効)
```
→ **電源断・再起動に対して運用可能**。`enabled` + `WantedBy=multi-user.target` + `Restart=always`
の組み合わせで、rpicam-vid の落ち込みも 2秒で自己回復する(上記の実測)。

### まだ残っている確認
- カメラの**設置向き**(横向きなら `ExecStart` の rpicam-vid 引数へ `--rotation 90` を追加)

---

## 27. 2026-11-11 PiにUSBカメラを追加 → 2台同時配信・同時解析

ユーザー: 「ラズベリーパイにUSBカメラを追加した。2台搭載になった。できれば両方カメラデータを飛ばせたらいい」

### 「No signal」の真因がここで確定した
追加した USB カメラ(**Logitech C270**、`lsusb` で `046d:0825`)が `/dev/video0` を奪い、
CSI の unicam が `/dev/video2` へずれた。

```
C270 HD WEBCAM (usb-3f980000.usb-1.2): /dev/video0 /dev/video1
unicam (platform:3f801000.csi):        /dev/video2
```

つまり **昨日 ustreamer が読んでいた `/dev/video0` は C270 のこと**(YUYV 640x480@30、色も正しい)。
C270 が外れると `/dev/video0` が CSI の unicam にすり替わり、unicam は 640x480 を弾くので
「No signal」になっていた。**第25章の「なぜ昨日は動いていたのか」の答えがこれ**
(ustreamer の不具合ではなく、`/dev/videoN` の同一性が変わったことが原因)。

### 配信の割り当て(2台・2ポート)
| ポート | 機器 | 手段 | 理由 |
|---|---|---|---|
| **8080** | CSI imx219 | `picam_stream.py`(rpicam-vid / libcamera、mode `1640:1232:10:P`) | unicam raw では 640x480 と色が取れない |
| **8081** | USB C270 | **ustreamer**(`--format=MJPEG --encoder=HW --desired-fps=30`) | UVC は自前 MJPEG を持つので **CPUほぼ無料(パススルー)** |

`pi-cam2.service`(enabled / `Restart=always`)を追加。**device は必ず by-id パス**で指定した。

```
ExecStart=/usr/bin/ustreamer --device=/dev/v4l/by-id/usb-046d_C270_HD_WEBCAM_200901010001-video-index0 \
  --resolution=640x480 --format=MJPEG --encoder=HW --desired-fps=30 --port=8081 --host=0.0.0.0 --tcp-nodelay
```
`/dev/videoN` は機器構成で変わる(今回実際にずれた)ので **index 直書きは禁止**。
libcamera 側(rpicam-vid)は DT の entity で探すため index 変更の影響を受けない(実測で無傷)。

### ai2 側の登録と実測
台帳に `Camera id=10 / Piカメラ USB(C270) / http://10.24.23.232:8081/stream` を登録
(`http://…/state` を見て `online:true` を確認してから登録する手順にした)。

```
cam:9  (CSI)  running 26.5〜28.0fps  映像の遅延 0.00〜0.04秒
cam:10 (USB)  running 25.0〜28.0fps  映像の遅延 0.00〜0.04秒
Pi Send-Q     8080 = 20KB / 8081 = 54KB   (第21章の 2.45MB と比較にならない)
Pi 負荷       load 1.24 / 4core、rpicam-vid 31%、ustreamer はほぼ0
Pi メモリ     367MB / 904MB
```
`/debug/camera/` の一括カードで **2台が同時配信**され、overlay 経由のフレームも両方取得できた。

### 私が今回入れたバグと修正(重要)
第23章で「検知完了時に `/debug/camera/devices/` を引き直さない(再発走を防ぐ)」へ変えたが、
**IPカメラのマージはビュー側にしか無かった**ため、完了時にブラウザが受け取る
`/debug/camera/progress/` の一覧から cam:9 / cam:10 が消え、**カードが作られなくなった**(実測)。

修正: マージ処理を `camera_service` へ寄せて両エンドポイントで共有した。
- `network_devices()` … 台帳のIPカメラを一覧用dictに組み替える
- `merged_devices(devices)` … USB検知結果とIP台帳を重複なしで結合
- `detect_progress()` と `debug_camera_devices` の**両方**がこれを呼ぶ

**教訓**: 同じ「一覧」を返す場所が2つあるなら、組み立ては1か所に寄せる。片方だけ直すと
もう片方を使う経路が静かに壊れる。

### 残っているもの
- **CSIカメラ(cam:9)のレンズ前が塞がっている**(取得フレームが全面暗/ぼけ)。設置し直しが必要
- **CSIの向き**未確認(横向きなら `picam_stream.py` の rpicam-vid 引数へ `--rotation 90`)
- 視聴数が実数より多く出ることがある(ブラウザが離線したMJPEG接続の後始末が遅れる)。表示上の問題
- PC内蔵カメラ(index 0)は `phase=error`(他アプリ占有の疑い)。Pi 2台とは無関係

---

## 28. 2026-11-11 「cam:9 の色がおかしい」→ カメラ故障ではないと確定

ユーザー: 「cam9はレンズが塞がれたというより、色合いがおかしい。カメラ側の問題か?」

### 切り分け(配信処理のせいか、カメラ/シーンのせいか)
同じ `--mode 1640:1232:10:P` で3経路を5秒差で比較した。

| 経路 | 輝度 | B-R | ラプラシアン分散(鮮明度) |
|---|---|---|---|
| 私の配信(:8080 /stream) | 84.6 | **-4.1** | 126.9 |
| `rpicam-still`(静止画) | 81.0 | **-3.7** | 137.1 |
| `rpicam-vid` 直接(動画) | 61.3 | **-1.9** | 115.7 |

→ **3経路がほぼ同じ値**。配信サーバ(`picam_stream.py`)もai2のoverlayも色を変えていない。
ustreamer側(USB C270)は B-R -0.3 で中立。

### rpicam の設定を9通り振って確認(天井向きのまま)
| 設定 | 輝度 | B-R | 判定 |
|---|---|---|---|
| auto(既定) | 101.5 | -5.2 | 基準 |
| `--awb fluorescent` | 101.8 | -4.3 | ほぼ変化なし(AWBは既に近似値を選んでいる) |
| `--awb indoor` | 101.5 | -4.8 | 変化なし |
| `--awb daylight` | 103.2 | **-49.3** | 悪化 |
| `--awbgains 1.6,1.1` | 97.0 | **-78.9** | 大幅に悪化(ゲインの向きが逆) |
| `--ev 0.5` / `--ev 1.0` | 138 / 174 | -6.8 / -7.0 | 明るくなるだけ(色比は不変) |
| `--saturation 1.6` | 100.8 | -9.0 | 悪化 |
| `--contrast 1.2` | 94.9 | -5.9 | 変化なし |

→ **B-R ≈ -5/255(約2%)は寒色LED照明のオフィスでは正常域**。設定で改善しないことも確認した。

### 本当の原因
そのとき CSI モジュールは**天井を向いていた**(換気口・LED照明・モニター端が画面を占有)。
- 画面の大半が強い照明+白い天井 → 露出とAWBがそれに引っ張られ、他が濁って見える
- さらに**人/物がレンズ直前に来た**時間帯は全面暗+青く見えた(=最初に「塞がれている」と誤解した画)
- 現在の画は**ピントは合っている**(天井の目地がシャープ)。ただし Camera Module v2(imx219)は
  **焦点固定**なので、近接被写体は構造的にボケる

### 参考: 2台の実力差(鮮明度)
```
USB C270 : 939.7   (オートフォーカス+明るいレンズ)
CSI imx219: 115〜172 (焦点固定)
```
→ 転倒検知の主カメラは **C270(cam:10)** が有利。CSI(cam:9)は入口/廊下の固定監視に向く。

### 対処
1. **CSIの向きを整える**(部屋 or 入口方向)。色はそれで落ち着く
2. 設定で色をどうにかする必要は**ない**(上記の実測で効果なし)
3. 3台同時稼働時のfpsは index0 19〜23 / cam:10 22〜24 / cam:9 26〜27 に落ちた(CPU占有)。
   **遅延は 0.01〜0.04秒のまま**なので検知には影響なし(第21章の設計どおり)

---

## 29. 2026-11-11 プロセスがトレースバック無しで落ちる(ヒープ破損)を修正

ai2 が「何も出力せず死ぬ」ことがあり、Windowsイベントログを確認した。

```
2026-11-10 12:04:26  Application Error  python.exe  例外コード 0xc0000374  ntdll.dll
2026-11-11 10:51:13  Application Error  python.exe  例外コード 0xc0000374  ntdll.dll
                     (0xc0000374 = STATUS_HEAP_CORRUPTION)
```

**帰属は正しく分けておく**(過剰に結論しない):

| 事象 | 判定 |
|---|---|
| 11/10 12:04 のヒープ破損 | **本物のクラッシュ**。当時のコードは下記の aliasing 問題を持っていた |
| 11/11 10:51 のヒープ破損 | 私が `Stop-Process` で殺した**直後**の時刻。落とす最中に native スレッドが壊した可能性が高く、自発クラッシュとは限らない |
| 11/11 11:49 の停止 | **Application Error イベント無し** = クラッシュではなく外部終了(シェル側の記録消失と同時)。今回の修正とは無関係 |

### 実在したバグ(修正した)
`cv2.VideoCapture.read()` が返す配列は**内部バッファを参照しているだけ**で、
次の `read()` で再利用・解放される(OpenCVの既知の約束事: 「次に読むまでしか有効でない」)。

第21章で入れた専有読取スレッドは、その配列をスロットに**そのまま**保存し、
解析スレッドが**後から** `frame.copy()` して overlay を描画していた。
読取スレッドが先に読み進めると、解析スレッドが触るのは**解放済み/再利用済みのメモリ**
→ use-after-free → ヒープ破損。11/10 のクラッシュと整合する。

同じ危険はUSB側にもあった(`_loop` が `cap.read()` の配列へ overlay を in-place 描画)。

### 修正(`core/camera_service.py`)
| 場所 | 内容 |
|---|---|
| `_reader_loop` | `stream["frame"] = frame.copy()` — **読んだ直後にコピー**してスロットへ置く |
| `_acquire_network` | 初フレームも `frame.copy()` で保存 |
| `read_frame`(USB側) | `return frame.copy()` — 呼び出し側が後で描画・保存しても安全 |

代价は 1枚あたりコピー2回(640x480x3 ≒ 0.3ms × 2 × 30fps ≒ 1.8%/core)で、実測fpsは変らなかった。

### 修正後の稼働確認(3台同時)
```
 0s  index0 error(起動直後)  cam:10 error(起動直後)  cam:9 running 27.6fps age 0.03
15s  index0 running 25.2     cam:10 running 24.9      cam:9  running 24.7 age 0.03
30s  index0 running 26.3     cam:10 running 18.1      cam:9  running 21.9 age 0.03
45s  index0 running 24.9     cam:10 running 18.4      cam:9  running 25.4 age 0.03
```
**注意**: 起動直後の1巡目は `error`(1フレームも取れない)が出ることがあり、**15〜30秒で自動復帰**する。
常時稼働は1台ずつ順に開くため、USBカメラが落ち着くまでの一時的な状態。クラッシュと見間違えないこと。

### 再発時の確認手順
```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2} -MaxEvents 60 |
  Where-Object { $_.Message -match 'python' } |
  Select-Object TimeCreated, ProviderName, @{n='msg';e={ ($_.Message -split "`n")[0..4] -join ' | ' }}
```
- `0xc0000374` = ヒープ破損(native のメモリ所有権の問題。OpenCV/Driver周りを疑う)
- `0xc0000005` = アクセス違反
- **イベントが無いのに止まっている**なら、クラッシュではなく外部終了(シェル/OS/手動停止)
