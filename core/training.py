"""CARE SEC 転倒・異常検知システム / AIモデルの再学習(追加学習)支援

caregiver_safety の学習パイプラインを ai2 に移植したもので、
`core/management/commands/ai_*.py` から呼ばれる共通処理を集めています。

    train_data/*.csv  ──ai_prepare_dataset──▶  train_dataset/X.npy,y.npy
                    ──ai_prepare_state_dataset▶ train_dataset/state_X.npy,state_y.npy,state_labels.npy
                    ──ai_train_fall / ai_train_state──▶ ai_models/candidates/*.keras
                    ──ai_train_* --promote──▶ ai_models/*.keras(旧モデルは ai_models/backup/ へ)

推論側(`core/ai_engine.py`)は `settings.AI_MODEL_DIR`(既定 `ai_models/`)から読むため、
`--promote` した後は **サーバーを再起動**しないと新しいモデルは使われません
(モデルは初回使用時にメモリへキャッシュされる)。
"""
import glob
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings

# caregiver_safety/realtime_lstm.py と同じ入力仕様
SEQUENCE_LENGTH = 30
FEATURES = 99
STRIDE = 5           # 転倒モデル: 5フレームずつずらして切り出す
STATE_STRIDE = 30    # 状態モデル: 重複なしで30フレームずつ切り出す

# 状態認識のクラス(ラベル名の順序はモデルの出力次元と一致させる)
STATE_LABELS = [
    "立っている",
    "座っている",
    "しゃがんでいる",
    "歩いている",
    "前かがみ",
    "横になっている",
    "立ち上がる",
    "転倒",
]

# 収集CSVファイル名(接頭辞を除いた部分)→ 状態ラベル
# 新しい行動を収集したら、ここに追加する。未登録の normal_* はスキップ対象として報告される
STATE_BY_SUFFIX = {
    "立っている": "立っている",
    "ゆっくり振り向く": "立っている",
    "腕をあげる、伸ばす": "立っている",
    "座っている": "座っている",
    "座っている腕組み": "座っている",
    "座って足を伸ばす": "座っている",
    "座って足を伸ばす腕組み": "座っている",
    "片足ずつ足を組む": "座っている",
    "背もたれにもたれて座る": "座っている",
    "背筋を伸ばして座る": "座っている",
    "足を床につけて座る": "座っている",
    "足を浮かして下ろす": "座っている",
    "しゃがんでいる": "しゃがんでいる",
    "歩いている": "歩いている",
    "前かがみ": "前かがみ",
    "物を拾う動作": "前かがみ",
    "ゆっくり横になる": "横になっている",
    "椅子から立ち上がる": "立ち上がる",
}


# ================= パス =================
def data_dir():
    return Path(getattr(settings, "AI_TRAIN_DATA_DIR", settings.BASE_DIR / "train_data"))


def dataset_dir():
    return Path(getattr(settings, "AI_TRAIN_DATASET_DIR", settings.BASE_DIR / "train_dataset"))


def model_dir():
    return Path(getattr(settings, "AI_MODEL_DIR", settings.BASE_DIR / "ai_models"))


def candidate_dir():
    path = model_dir() / "candidates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_root():
    path = model_dir() / "backup"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pose_model_path():
    """MediaPipe Pose のモデルファイル(収集・推論で共通)"""
    return model_dir() / "pose_landmarker_full.task"


def ensure_dirs():
    data_dir().mkdir(parents=True, exist_ok=True)
    dataset_dir().mkdir(parents=True, exist_ok=True)
    candidate_dir()
    backup_root()


# ================= CSV → シーケンス =================
def csv_files(directory=None):
    directory = Path(directory) if directory else data_dir()
    return sorted(glob.glob(str(directory / "*.csv")))


def fall_label_from_filename(path):
    """ファイル名の接頭辞から転倒ラベル(0=normal / 1=fall)を決める"""
    name = os.path.basename(path).lower()
    if name.startswith("normal"):
        return 0
    if name.startswith("fall"):
        return 1
    return None


def state_label_from_filename(path):
    """ファイル名から状態ラベルを決める(fall_* は 転倒)"""
    stem = os.path.splitext(os.path.basename(path))[0]
    if stem.startswith("fall_"):
        return "転倒"
    suffix = stem[len("normal_"):] if stem.startswith("normal_") else stem
    return STATE_BY_SUFFIX.get(suffix)


def _looks_numeric_row(line):
    """CSVの1行目が数値データか(ヘッダ行でない判定)"""
    for field in line.split(",")[:3]:
        try:
            float(field)
        except ValueError:
            return False
    return True


def load_csv_features(path):
    """CSVの先頭99列(33ランドマーク×xyz)を float32 の (フレーム数, 99) で読む

    caregiver_safety 由来のCSVはヘッダ行を持たないため、先頭行が数値なら
    `header=None` で読む(元スクリプトは先頭行をヘッダ扱いで1フレーム失っていた)。
    ※ この差で既存 train_dataset/X.npy と件数がずれることがあります。
       その場合は ai_prepare_dataset を再実行してください。
    """
    import numpy as np
    import pandas as pd

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
        header = None if first and _looks_numeric_row(first) else "infer"
        frame = pd.read_csv(path, header=header)
    except Exception as exc:  # noqa: BLE001 -壊れたCSVで全体を止めない
        return None, f"読み込み失敗: {type(exc).__name__}: {exc}"

    numeric = frame.select_dtypes(include=[np.number])
    if numeric.shape[1] < FEATURES:
        return None, f"数値列が不足しています({numeric.shape[1]}列 < {FEATURES})"

    return numeric.iloc[:, :FEATURES].values.astype(np.float32), ""


def create_sequences(data, length=SEQUENCE_LENGTH, stride=STRIDE):
    """(フレーム数, 99) を (個数, length, 99) のスライディングウィンドウに切る"""
    if len(data) < length:
        return []
    return [
        data[start:start + length]
        for start in range(0, len(data) - length + 1, stride)
        if data[start:start + length].shape == (length, FEATURES)
    ]


def build_fall_dataset(directory=None, stride=STRIDE):
    """train_data/*.csv → X (N,30,99) / y (N,) を作る

    戻り値: (X, y, rows, skipped)  rows=[{file,label,frames,sequences}]
    """
    import numpy as np

    X, y, rows, skipped = [], [], [], []
    for path in csv_files(directory):
        label = fall_label_from_filename(path)
        name = os.path.basename(path)
        if label is None:
            skipped.append((name, "normal_/fall_ で始まらない"))
            continue
        data, error = load_csv_features(path)
        if data is None:
            skipped.append((name, error))
            continue
        sequences = create_sequences(data, SEQUENCE_LENGTH, stride)
        if not sequences:
            skipped.append((name, f"フレーム数不足({len(data)} < {SEQUENCE_LENGTH})"))
            continue
        X.extend(sequences)
        y.extend([label] * len(sequences))
        rows.append({
            "file": name, "label": label, "frames": len(data),
            "sequences": len(sequences), "first": len(y) - len(sequences),
        })

    return (
        np.array(X, dtype=np.float32) if X else np.empty((0, SEQUENCE_LENGTH, FEATURES), np.float32),
        np.array(y, dtype=np.int64) if y else np.empty((0,), np.int64),
        rows,
        skipped,
    )


def build_state_dataset(directory=None):
    """train_data/*.csv → state_X / state_y / state_labels を作る"""
    import numpy as np

    label_to_id = {label: i for i, label in enumerate(STATE_LABELS)}
    X, y, rows, skipped = [], [], [], []
    for path in csv_files(directory):
        state = state_label_from_filename(path)
        name = os.path.basename(path)
        if state is None:
            skipped.append((name, "状態の対応が未登録(core/training.py の STATE_BY_SUFFIX に追加)"))
            continue
        data, error = load_csv_features(path)
        if data is None:
            skipped.append((name, error))
            continue
        sequences = create_sequences(data, SEQUENCE_LENGTH, STATE_STRIDE)
        if not sequences:
            skipped.append((name, f"フレーム数不足({len(data)} < {SEQUENCE_LENGTH})"))
            continue
        X.extend(sequences)
        y.extend([label_to_id[state]] * len(sequences))
        rows.append({
            "file": name, "state": state, "frames": len(data),
            "sequences": len(sequences), "first": len(y) - len(sequences),
        })

    return (
        np.array(X, dtype=np.float32) if X else np.empty((0, SEQUENCE_LENGTH, FEATURES), np.float32),
        np.array(y, dtype=np.int64) if y else np.empty((0,), np.int64),
        rows,
        skipped,
    )


# ================= モデルの保存・反映 =================
def backup_current(filename):
    """現行モデルを ai_models/backup/<日時>/ に退避してそのパスを返す(無いならNone)"""
    current = model_dir() / filename
    if not current.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = backup_root() / stamp
    folder.mkdir(parents=True, exist_ok=True)
    return shutil.copy2(current, folder / filename)


def promote(filename):
    """candidates/<filename> を ai_models/<filename> へ反映する(旧版はbackupへ)"""
    src = candidate_dir() / filename
    if not src.exists():
        raise FileNotFoundError(f"候補モデルが見つかりません: {src}")
    backed_up = backup_current(filename)
    dst = model_dir() / filename
    shutil.copy2(src, dst)
    return dst, backed_up


def dataset_path(name):
    return dataset_dir() / name


# ================= モデル定義・学習・評価 =================
FALL_MODEL_FILENAME = "fall_detection_lstm.keras"
STATE_MODEL_FILENAME = "state_detection_lstm.keras"
STATE_LABELS_FILENAME = "state_labels.npy"


def build_fall_model(input_shape):
    """転倒判定LSTM(caregiver_safety/train_lstm.py と同一構成)"""
    from tensorflow.keras.layers import Dense, Dropout, LSTM
    from tensorflow.keras.models import Sequential

    model = Sequential([
        LSTM(64, input_shape=input_shape, return_sequences=True),
        Dropout(0.3),
        LSTM(32),
        Dropout(0.3),
        Dense(16, activation="relu"),
        Dense(1, activation="sigmoid"),
    ])
    model.compile(optimizer="adam", loss="binary_crossentropy", metrics=["accuracy"])
    return model


def build_state_model(input_shape, n_classes):
    """状態認識LSTM(caregiver_safety/train_state_lstm.py と同一構成)"""
    import tensorflow as tf

    model = tf.keras.Sequential([
        tf.keras.layers.Input(shape=input_shape),
        tf.keras.layers.LSTM(64, return_sequences=True),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.LSTM(32),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(32, activation="relu"),
        tf.keras.layers.Dropout(0.2),
        tf.keras.layers.Dense(n_classes, activation="softmax"),
    ])
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )
    return model


def train_fall(X, y, epochs=50, batch_size=16, test_size=0.2, seed=42, verbose=1):
    """転倒モデルを学習し (model, metrics, test分割) を返す"""
    import numpy as np
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import train_test_split
    from tensorflow.keras.callbacks import EarlyStopping

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=seed, stratify=y
    )
    model = build_fall_model((X.shape[1], X.shape[2]))
    model.fit(
        X_train, y_train,
        validation_data=(X_test, y_test),
        epochs=epochs, batch_size=batch_size,
        callbacks=[EarlyStopping(monitor="val_loss", patience=8, restore_best_weights=True)],
        verbose=verbose,
    )
    loss, accuracy = model.evaluate(X_test, y_test, verbose=0)
    prob = model.predict(X_test, verbose=0).reshape(-1)
    pred = (prob >= 0.5).astype(int)
    cm = confusion_matrix(y_test, pred)
    return model, {
        "val_loss": round(float(loss), 4),
        "val_accuracy": round(float(accuracy), 4),
        "confusion": [[int(cm[0][0]), int(cm[0][1])], [int(cm[1][0]), int(cm[1][1])]],
        "report": classification_report(y_test, pred, target_names=["NORMAL", "FALL"],
                                       zero_division=0, output_dict=True),
        "train_count": int(len(X_train)),
        "test_count": int(len(X_test)),
    }, (X_test, y_test, prob)


def train_state(X, y, n_classes, epochs=50, batch_size=16, test_size=0.2, seed=42, verbose=1):
    """状態モデルを学習し (model, metrics, test分割) を返す(クラス重み付き)"""
    import numpy as np
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import train_test_split
    from sklearn.utils.class_weight import compute_class_weight
    from tensorflow.keras.callbacks import EarlyStopping

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=test_size, random_state=seed, stratify=y
    )
    classes = np.unique(y_train)
    weights = compute_class_weight("balanced", classes=classes, y=y_train)
    class_weight = {int(c): float(w) for c, w in zip(classes, weights)}

    model = build_state_model((X.shape[1], X.shape[2]), n_classes)
    model.fit(
        X_train, y_train,
        validation_data=(X_val, y_val),
        epochs=epochs, batch_size=batch_size, class_weight=class_weight,
        callbacks=[EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True)],
        verbose=verbose,
    )
    loss, accuracy = model.evaluate(X_val, y_val, verbose=0)
    prob = model.predict(X_val, verbose=0)
    pred = np.argmax(prob, axis=1)
    cm = confusion_matrix(y_val, pred, labels=np.arange(n_classes))
    return model, {
        "val_loss": round(float(loss), 4),
        "val_accuracy": round(float(accuracy), 4),
        "class_weight": {str(k): round(v, 3) for k, v in class_weight.items()},
        "confusion": [[int(value) for value in row] for row in cm],
        "report": classification_report(y_val, pred, labels=np.arange(n_classes),
                                       zero_division=0, output_dict=True),
        "train_count": int(len(X_train)),
        "test_count": int(len(X_val)),
    }, (X_val, y_val, pred)


def evaluate_fall_at_threshold(model, X, y, threshold):
    """運用閾値での正解率・誤検知率・見逃し率を返す"""
    import numpy as np

    prob = model.predict(X, verbose=0).reshape(-1)
    pred = (prob >= threshold).astype(int)
    tn = int(np.sum((y == 0) & (pred == 0)))
    fp = int(np.sum((y == 0) & (pred == 1)))
    fn = int(np.sum((y == 1) & (pred == 0)))
    tp = int(np.sum((y == 1) & (pred == 1)))
    total = tn + fp + fn + tp
    return {
        "threshold": round(float(threshold), 2),
        "accuracy": round((tn + tp) / total, 4) if total else None,
        "false_positive_rate": round(fp / (tn + fp), 4) if (tn + fp) else None,
        "miss_rate": round(fn / (fn + tp), 4) if (fn + tp) else None,
        "counts": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def load_model(path):
    from tensorflow.keras.models import load_model as keras_load

    return keras_load(str(path))


# ================= データ変更検知(自動再学習用) =================
def state_file():
    return dataset_dir() / "retrain_state.json"


def read_state():
    path = state_file()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(data):
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def data_fingerprint():
    """train_data/*.csv の指紋(名前・サイズ・更新時刻)。データが変わったかの判定に使う"""
    digest = hashlib.sha256()
    files = csv_files()
    for path in files:
        stat = os.stat(path)
        digest.update(os.path.basename(path).encode("utf-8"))
        digest.update(str(int(stat.st_mtime)).encode("ascii"))
        digest.update(str(int(stat.st_size)).encode("ascii"))
    return {
        "hash": digest.hexdigest()[:16],
        "files": len(files),
        "bytes": sum(int(os.stat(p).st_size) for p in files),
    }
