# -*- coding: utf-8 -*-
"""2台のUSBカメラが同時ストリーミングできるかの切り分け(サーバー停止中に実行)

    .\.venv\Scripts\python.exe -X utf8 probe_two_cameras.py
"""
import time

import cv2


def probe(idx, seconds=2.5, api=cv2.CAP_MSMF):
    cap = cv2.VideoCapture(idx, api)
    opened = cap.isOpened()
    n = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        ok = cap.grab()
        if ok:
            n += 1
    cap.release()
    return opened, n


def main():
    o0, n0 = probe(0)
    print("P1 内蔵(index0)単独: open=%s %d枚" % (o0, n0))
    o1, n1 = probe(1)
    print("P2 C270(index1)単独: open=%s %d枚" % (o1, n1))

    c0 = cv2.VideoCapture(0, cv2.CAP_MSMF)
    c1 = cv2.VideoCapture(1, cv2.CAP_MSMF)
    a0 = a1 = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3.0:
        if c0.grab():
            a0 += 1
        if c1.grab():
            a1 += 1
    c0.release()
    c1.release()
    print("P3 2台同時(0→1の順でopen): 内蔵%d枚 / C270%d枚" % (a0, a1))

    d1 = cv2.VideoCapture(1, cv2.CAP_MSMF)
    d0 = cv2.VideoCapture(0, cv2.CAP_MSMF)
    b0 = b1 = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3.0:
        if d1.grab():
            b1 += 1
        if d0.grab():
            b0 += 1
    d0.release()
    d1.release()
    print("P4 2台同時(1→0の順でopen): C270%d枚 / 内蔵%d枚" % (b1, b0))


if __name__ == "__main__":
    main()
