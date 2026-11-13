# -*- coding: utf-8 -*-
r"""LSTM推論のバッチ効率と、infer_lock 共有が台数をどう殺すかを実測する

    .\.venv\Scripts\python.exe -X utf8 profile_lstm_batch.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import numpy as np  # noqa: E402

from core import ai_engine  # noqa: E402

shared = ai_engine._load_shared()
fall = shared["fall_infer"]
state = shared["state_infer"]

print("=== バッチサイズごとの推論時間(fall+state を1回ずつ) ===")
print("  batch   1回あたりms   1枚あたりms   30fps×N台で必要な時間/秒")
measured = {}
for batch in (1, 2, 4, 8, 16):
    x = np.random.rand(batch, 30, 99).astype(np.float32)
    # ウォームアップ(トレース)
    for _ in range(5):
        fall(x)
        state(x)
    t0 = time.perf_counter()
    n = 30
    for _ in range(n):
        fall(x)
        state(x)
    ms = (time.perf_counter() - t0) / n * 1000.0
    per = ms / batch
    need = per * 30 / 1000.0 * batch  # batch台分を30fpsで流すのに必要な秒数/秒
    measured[batch] = (ms, per)
    print("  %5d   %9.2f      %9.2f     %.2f (=%.0f%% を1コアで)" % (
        batch, ms, per, need, need * 100))

print("\n=== infer_lock 共有のまま N台を30fpsで流すと ===")
ms1, per1 = measured[1]
ms8, per8 = measured[8]
for n_cam in (1, 2, 3, 4, 6, 8):
    # 現状: 1台ごとに (30,99) を1枚ずつ、ロックで直列
    serial = per1 * 30 * n_cam / 1000.0
    print("  %d台: LSTMだけで **%.0f%%** のコアを直列占有%s" % (
        n_cam, serial * 100, "  ← 100%超 = ここで詰まる" if serial >= 1.0 else ""))

print("\n=== バッチ化すると(8台を1回の (8,30,99) で) ===")
need8 = ms8 * 30 / 1000.0   # 8台分を30fpsで回すのに必要な秒/秒
print("  8台分: %.2f ms/回 × 30回/s = **%.0f%%** のコア" % (ms8, need8 * 100))
print("  1台あたり %.2f ms(直列方式 %.2f ms の %.0f%%)" % (
    ms8 / 8, per1, (ms8 / 8) / per1 * 100))

print("\n=== POSE の壁(1スレッドあたり) ===")
print("  実測 POSE = 31.6ms/枚 → **1スレッドで上限 %.1f fps**。" % (1000.0 / 31.6))
print("  30fps を保つにはカメラ1台に1スレッドが必要で、")
print("  そのスレッドは POSE 31.6 + LSTM %.1f + 周辺 ≈ %.1f ms → **%.1f fps が壁**" % (
    per1, 31.6 + per1 + 2.0, 1000.0 / (31.6 + per1 + 2.0)))
