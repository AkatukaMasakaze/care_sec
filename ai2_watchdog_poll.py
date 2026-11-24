# -*- coding: utf-8 -*-
r"""ai2 サーバー監視ポーラー(ネイティブクラッシュ犯人特定用)

    C:\Users\student\code\ai2\.venv\Scripts\python.exe -X utf8 ai2_watchdog_poll.py

サーバー(process id/port8000)の生存と、/debug/camera/ai/ のエンジン状態を
15秒ごとに ai2_watchdog.log へ追記する。クラッシュしたら **最後の数行**が
死亡直前の状態(どのカメラが何fpsで、検知スレッドが走っていたか等)になる。
サーバーとは別プロセスなので、サーバーがネイティブクラッシュしても生き残る。
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE = "http://127.0.0.1:8000"
LOG = Path(r"C:\Users\student\code\ai2\ai2_watchdog.log")
INTERVAL = 15
COOKIE = Path(r"C:\Users\student\code\ai2\.watchdog_cookie")  # ui_check_tmp のセッション


def now():
    return datetime.now().strftime("%m-%d %H:%M:%S")


def log(line):
    with LOG.open("a", encoding="utf-8") as f:
        f.write("%s %s\n" % (now(), line))
        f.flush()


def http(path, cookie=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=8) as resp:
        set_cookie = resp.headers.get("Set-Cookie")
        return resp.status, resp.read().decode("utf-8", "replace"), set_cookie


def login():
    """ui_check_tmp でログインし Cookie 文字列を返す(csrf はクッキー連動)"""
    import http.cookiejar
    import urllib.parse

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    html = opener.open(BASE + "/accounts/login/", timeout=8).read().decode(
        "utf-8", "replace")
    import re
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not m:
        raise RuntimeError("csrfなし")
    data = urllib.parse.urlencode({
        "csrfmiddlewaretoken": m.group(1),
        "username": "ui_check_tmp",
        "password": "ui-check-2026!",
        "next": "/",
    }).encode()
    req = urllib.request.Request(BASE + "/accounts/login/", data=data,
                                 headers={"Referer": BASE + "/accounts/login/"})
    resp = opener.open(req, timeout=8)
    cookie = "; ".join("%s=%s" % (c.name, c.value) for c in jar)
    COOKIE.write_text(cookie, encoding="utf-8")
    return cookie


def main():
    global INTERVAL
    cookie = None
    if COOKIE.exists():
        cookie = COOKIE.read_text(encoding="utf-8")
    state = None
    alive_streak = 0
    while True:
        try:
            _status, body, _ = http("/debug/camera/ai/", cookie)
            data = json.loads(body)
            if data.get("redirect") or "login" in body[:400].lower():
                cookie = login()
                continue
            devs = data.get("devices", [])
            summary = " ".join(
                "dev%s:%s fps=%s pose=%s ppl=%s%s" % (
                    d.get("device"), d.get("phase"), d.get("fps"),
                    d.get("pose_fps"), d.get("people"),
                    (" ERR=" + str(d.get("error"))[:60]) if d.get("error") else "")
                for d in devs) or "(no devices)"
            alive_streak += 1
            if summary != state:  # 状態が変わったときだけ本体ログ
                log("ALIVE(%ds) %s" % (alive_streak * INTERVAL, summary))
                state = summary
            else:
                log("ALIVE tick")
        except urllib.error.URLError as exc:
            alive_streak = 0
            log("DOWN urlopen=%s" % (exc.reason,))
            try:
                cookie = login()
                log("DOWN re-login ok")
            except Exception as exc2:  # noqa: BLE001
                log("DOWN login-fail %s" % exc2)
        except json.JSONDecodeError:
            # HTML(=リダイレクト/ログイン切れ)が返った状態。再ログインで復旧する
            alive_streak = 0
            log("DOWN not-json(セッション切れ?) re-login")
            try:
                cookie = login()
            except Exception as exc2:  # noqa: BLE001
                log("DOWN login-fail %s" % exc2)
        except Exception as exc:  # noqa: BLE001
            alive_streak = 0
            log("ERR %s: %s" % (type(exc).__name__, exc))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
