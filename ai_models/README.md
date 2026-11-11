# ai_models(リアルタイムAI解析モデル)

`/debug/camera/` のリアルタイムAI解析(caregiver_safety 由来)が使うモデルファイルです。
**ai2 側で再学習(追加学習)できる**ようになっており、その成果物はここに反映されます。

| 種別 | ファイル / フォルダ | 用途 |
|---|---|---|
| 推論(現行) | `pose_landmarker_full.task` | MediaPipe Pose(33ランドマーク抽出) |
| 推論(現行) | `fall_detection_lstm.keras` | 転倒判定LSTM(入力 30フレーム×99値) |
| 推論(現行) | `state_detection_lstm.keras` | 状態判定LSTM(立っている/座っている/…/転倒の8クラス) |
| 推論(現行) | `state_labels.npy` | 状態ラベル(上記8クラスの並び順) |
| 学習の中間 | `candidates/` | `ai_train_*` が書き出す**未反映の候補モデル** |
| 自動退避 | `backup/<日時>/` | `--promote` 時に置き換わった**旧モデル** |

## 再学習(追加学習)の流れ

> **通常は自動化されています**(`settings.AI_AUTO_RETRAIN = True`)。
> `train_data/` のCSVが変わっている時だけ、**runserver 起動時に**
> 前処理 → 学習 → 現行モデルと比較 → 劣化がなければ反映、まで自動実行します
> (`core/retraining.py`)。変わっていなければ数秒で素通りするので、
> autoreload の再起動を繰り返しても無駄に学習しません。
> 状況確認は `python manage.py ai_retrain --status`、手動実行は下記コマンドを使います。

```powershell
# 1) 新しい材料を収集(runserver を止めてから。カメラを掴んでいると開けません)
python manage.py ai_collect_data                       # 窓キー 1:normal / 2:fall / s:停止 / q:終了
python manage.py ai_collect_data --label fall --name 椅子から転落 --seconds 30

# 2) 学習データを再生成(train_data/*.csv → train_dataset/*.npy)
python manage.py ai_prepare_dataset
python manage.py ai_prepare_state_dataset

# 3) 学習(既定は ai_models/candidates/ へ。現行モデルは壊れません)
python manage.py ai_train_fall
python manage.py ai_train_state

# 4) 候補を検証(現行モデルと同条件で比較できる)
python manage.py ai_check_model --path ai_models/candidates/fall_detection_lstm.keras
python manage.py ai_check_model                        # 現行モデル

# 5) 問題なければ反映(旧モデルは ai_models/backup/<日時>/ へ自動退避)
python manage.py ai_train_fall --promote
python manage.py ai_train_state --promote              # 状態は state_labels.npy も同時に反映

# 6) サーバー再起動(モデルは起動時にメモリへキャッシュされるため必須)
python manage.py runserver 127.0.0.1:8000
```

材料・学習データの格納先は `settings.AI_TRAIN_DATA_DIR`(`train_data/`)と
`settings.AI_TRAIN_DATASET_DIR`(`train_dataset/`)です。

ファイルが不足している場合、解析はエラーになりますが Webアプリ自体は起動できます
(エラー内容は `/debug/camera/` の「リアルタイムAI解析」欄に表示されます)。

## caregiver_safety から一式を最初から取り込む場合

```powershell
Copy-Item ..\caregiver_safety\models\pose_landmarker_full.task,`
          ..\caregiver_safety\models\fall_detection_lstm.keras,`
          ..\caregiver_safety\models\state_detection_lstm.keras .\
Copy-Item ..\caregiver_safety\dataset\state_labels.npy .\
Copy-Item ..\caregiver_safety\data\*.csv ..\train_data\
Copy-Item ..\caregiver_safety\dataset\*.npy ..\train_dataset\
```
