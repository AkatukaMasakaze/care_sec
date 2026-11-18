# -*- coding: utf-8 -*-
r"""骨格の「変化速度」に転倒分離力があるか、train_data の実データで測る

転倒は姿勢そのものより**変わり方の速さ**に特徴が出ると言われる:
  - 腰の急降下(画面yの+方向)が1フレームに大きく出る
  - 体幹の傾き(肩〜腰ベクトルの垂直からの角)が短時間で90°近くまで回る
このスクリプトは normal_* / fall_* 各CSVからそれを計算し、
AUC(=その1指標だけで両クラスをどれだけ分けられるか 0.5=無意味 1.0=完全)で評価する。

    .\.venv\Scripts\python.exe -X utf8 analyze_fall_velocity.py
    .\.venv\Scripts\python.exe -X utf8 analyze_fall_velocity.py --dir train_data

読み取り方:
  AUC 0.9 超 = 単独で実用級の分離力
  AUC 0.7〜0.9 = 補助特徴として有効(LSTMと併用)
  速い動作(座る/しゃがむ/物を拾う)が fall 側に寄るなら、閾値だけでは誤検知源
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core import training  # noqa: E402


def file_features(data):
    """(フレーム数, 99) → 速度系の特徴 dict"""
    lm = data.reshape(-1, 33, 3)
    hip_y = (lm[:, 23, 1] + lm[:, 24, 1]) / 2.0
    shoulder = (lm[:, 11, :2] + lm[:, 12, :2]) / 2.0
    hip = (lm[:, 23, :2] + lm[:, 24, :2]) / 2.0

    # 体幹の傾き(垂直=0°, 水平=90°)。肩が腰より上(dy負)が直立
    vec = shoulder - hip
    tilt = np.degrees(np.arctan2(np.abs(vec[:, 0]), np.maximum(-vec[:, 1], 1e-6)))
    tilt = np.clip(tilt, 0, 180)

    d_hip = np.diff(hip_y)          # + = 下がった
    d_tilt = np.abs(np.diff(tilt))  # 傾きの角度変化/フレーム

    # 「0.33秒(10フレーム)以内に45°以上倒れた」最大値 → 急激な転倒イベント
    window = 10
    roll10 = np.array([
        tilt[i + window] - tilt[i] if i + window < len(tilt) else 0.0
        for i in range(len(tilt))
    ])

    return {
        "max_hip_drop": float(np.max(d_hip)) if len(d_hip) else 0.0,
        "p95_hip_drop": float(np.percentile(d_hip, 95)) if len(d_hip) else 0.0,
        "max_tilt_rate": float(np.max(d_tilt)) if len(d_tilt) else 0.0,
        "max_tilt": float(np.max(tilt)) if len(tilt) else 0.0,
        "roll_10f": float(np.max(roll10)) if len(roll10) else 0.0,
    }


def auc(pos, neg):
    """Mann-Whitney AUC(実装依存を避けるため素朴に計算)"""
    if not pos or not neg:
        return float("nan")
    pos = np.asarray(pos)
    neg = np.asarray(neg)
    wins = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((wins + 0.5 * ties) / (len(pos) * len(neg)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="CSV格納フォルダ(既定 train_data)")
    args = ap.parse_args()

    files = training.csv_files(args.dir)
    if not files:
        print("CSVが見つかりません:", args.dir or training.data_dir())
        return 1

    feats = {"fall": [], "normal": []}
    names = {"fall": [], "normal": []}
    keys = ["max_hip_drop", "p95_hip_drop", "max_tilt_rate", "max_tilt", "roll_10f"]

    for path in files:
        label = training.fall_label_from_filename(path)
        if label is None:
            continue
        data, err = training.load_csv_features(path)
        if data is None or len(data) < 31:
            continue
        f = file_features(data)
        bucket = "fall" if label == 1 else "normal"
        feats[bucket].append(f)
        names[bucket].append(os.path.basename(path))

    print("対象: fall %d本 / normal %d本\n" % (len(feats["fall"]), len(feats["normal"])))

    print("=== 特徴ごとのAUC(fall vs normal) ===")
    for k in keys:
        a = auc([f[k] for f in feats["fall"]], [f[k] for f in feats["normal"]])
        mf = np.mean([f[k] for f in feats["fall"]]) if feats["fall"] else 0
        mn = np.mean([f[k] for f in feats["normal"]]) if feats["normal"] else 0
        print("  %-14s AUC=%.3f   fall平均=%.4f  normal平均=%.4f" % (k, a, mf, mn))

    print("\n=== 分布(fall側) ===")
    for k in keys:
        vals = sorted(f[k] for f in feats["fall"])
        if vals:
            print("  %-14s min=%.3f p25=%.3f 中央=%.3f p75=%.3f max=%.3f" % (
                k, vals[0], vals[len(vals) // 4], vals[len(vals) // 2],
                vals[3 * len(vals) // 4], vals[-1]))

    print("\n=== 分布(normal側でfall平均を超えるもの=誤検知候補) ===")
    for k in keys:
        if not feats["fall"] or not feats["normal"]:
            continue
        mf = np.mean([f[k] for f in feats["fall"]])
        over = [(n, round(f[k], 4)) for n, f in zip(names["normal"], feats["normal"])
                if f[k] > mf]
        print("  %-14s fall平均超のnormal本: %s" % (k, over[:6] if over else "無し"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
