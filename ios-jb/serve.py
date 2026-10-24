"""「一鍵開始」的觸發器：在手機上跑一個只有本機連得到的網頁伺服器。

手機上跑（NewTerm）：
    python3 serve.py        # 前景跑，Ctrl-C 停
    sh start.sh             # 丟到背景跑（開機後跑一次就好）
然後 Safari → 開 http://127.0.0.1:8080/ → 分享 → 加入主畫面，把它命名成「開始領蝦幣」。
之後開啟主畫面那顆圖示，再按「開始」就會：
    1. 把 bot.py 丟到背景開始跑
    2. 用 tweak 把蝦皮叫回前景（com.shopee.tw）
    3. 顯示狀態頁（上面有「停止」）

GET 只讀取狀態；啟停需要同來源、帶驗證碼的 POST，避免其他網頁觸發本機操作。

為什麼要繞這一下：iOS 上沒有任何「桌面小工具可以執行程式」的官方路徑，
App Store 的 App 又會被系統凍結（凍結就沒辦法在蝦皮前景時繼續跑），
所以最省事的一鍵＝ Safari 書籤（點一下）＋ 背景的這支程式。

只綁 127.0.0.1:8080，外面連不進來；只用標準庫。
"""
from __future__ import annotations

import argparse
import html
import http.client
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOGS = ROOT / "logs"
STOP_FILE = LOGS / "bot.stop"
CONSOLE_LOG = LOGS / "bot-console.log"
SHOPEE = "com.shopee.tw"
PORT = 8080

_proc: subprocess.Popen | None = None
_starting = False
_stop_wanted = False
_lock = threading.Lock()
_cancel_start = threading.Event()
_stopping: subprocess.Popen | None = None
_csrf_token = secrets.token_urlsafe(32)


def running() -> bool:
    proc = _proc
    return proc is not None and proc.poll() is None


def start_bot() -> str:
    """點「開始」時呼叫：先叫回蝦皮、再啟動 bot（背景做，不卡住網頁回應）。

    先叫回蝦皮是因為 bot 需要蝦皮在前景；順序顛倒的話 bot 會先看到 Safari。
    """
    global _starting, _stop_wanted
    with _lock:
        if running() or _starting:
            return "已經在跑了"
        _starting = True
        _stop_wanted = False
        _cancel_start.clear()
    threading.Thread(target=_start_flow, daemon=True).start()
    return "開始中…（蝦皮會被叫到前景，約 2 秒後開始）"


def _start_flow():
    global _starting
    try:
        if not _cancel_start.is_set():
            bring_shopee_front()
        if not _cancel_start.wait(2):
            start_bot_now()
    finally:
        with _lock:
            _starting = False


def _copy_output(proc):
    """Drain the child continuously, retaining a bounded console log."""
    handler = RotatingFileHandler(CONSOLE_LOG, maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    try:
        with proc.stdout:
            while data := proc.stdout.readline(8192):
                record = logging.LogRecord("bot-console", logging.INFO, "", 0,
                                           data.decode("utf-8", "replace").rstrip(), (), None)
                handler.emit(record)
    finally:
        handler.close()


def start_bot_now() -> str:
    global _proc
    with _lock:
        if running():
            return "已經在跑了"
        if _stop_wanted:  # 還在 2 秒的啟動空檔就被按停止
            return "已取消（收到停止）"
        LOGS.mkdir(parents=True, exist_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        _proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "bot.py"), "run", "--stop-file", str(STOP_FILE)],
            cwd=str(HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)
        threading.Thread(target=_copy_output, args=(_proc,), daemon=True).start()
    return "開始了"


def bring_shopee_front():
    """用 tweak 的 task 11 把蝦皮叫回前景，省掉使用者自己切回去的那一下。"""
    try:
        from zxtouch import ZXTouch
        zx = ZXTouch("127.0.0.1", 6000, timeout=5)
        try:
            zx.foreground(SHOPEE)
        finally:
            zx.close()
    except Exception as e:  # 沒有 tweak 也要能跑（只是要自己切回蝦皮）
        print(f"叫不回蝦皮：{e}", file=sys.stderr)


def stop_bot() -> str:
    global _stop_wanted, _stopping
    with _lock:
        _stop_wanted = True
        _cancel_start.set()
        LOGS.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
        proc = _proc
        if proc is not None and proc.poll() is None and _stopping is not proc:
            _stopping = proc
            threading.Thread(target=_wait_kill, args=(proc,), daemon=True).start()
    return "停止中（停止後續操作；逾時會終止程序）"


def _wait_kill(proc):
    """Only ever terminate the process captured by this stop request."""
    global _stopping
    try:
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        with _lock:
            if _stopping is proc:
                _stopping = None


def tail(path: Path, lines: int = 25) -> str:
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size - 65536))
            text = source.read(65536).decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return "(還沒有紀錄)"
    return "\n".join(text[-lines:]) or "(空)"


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>蝦皮自動領蝦幣</title>
<style>
 body {{ font: 17px -apple-system, sans-serif; margin: 0; padding: 24px; background: #111; color: #eee; }}
 h1 {{ font-size: 20px; }}
 button.b {{ display: block; width: 100%; border: 0; text-align: center; padding: 18px; margin: 12px 0; border-radius: 14px;
        text-decoration: none; font-weight: 700; color: #fff; }}
 .go {{ background: #ee4d2d; }}
 .stop {{ background: #444; }}
 pre {{ background: #000; padding: 12px; border-radius: 10px; overflow-x: auto; font-size: 12px; }}
 .state {{ color: {color}; font-weight: 700; }}
</style>
<h1>蝦皮自動領蝦幣</h1>
<p>狀態：<span class="state">{state}</span></p>
<form action="/start" method="post"><input type="hidden" name="token" value="{token}"><button class="b go">▶︎ 開始</button></form>
<form action="/stop" method="post"><input type="hidden" name="token" value="{token}"><button class="b stop">■ 停止</button></form>
<pre>{log}</pre>
"""


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def _send(self, body: str, code: int = 200, content_type: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Autocoin-Trigger", "1")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def page(self, note: str = ""):
        if running():
            state, color = "執行中", "#7f7"
        elif _starting:
            state, color = "啟動中…", "#fd8"
        else:
            state, color = "停止", "#f88"
        log = html.escape(tail(CONSOLE_LOG))
        if note:
            log = html.escape(note) + "\n\n" + log
        self._send(PAGE.format(color=color, state=state, log=log, token=_csrf_token))

    def _local_request(self) -> bool:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return (len(self.headers.get_all("Host", [])) == 1
                and len(self.headers.get_all("Origin", [])) <= 1
                and host in allowed and self.headers.get("Sec-Fetch-Site") != "cross-site"
                and (origin is None or origin == "http://" + host))

    def do_GET(self):
        if not self._local_request():
            self._send("禁止跨站存取", 403)
            return
        path = self.path.split("?")[0]
        if path in ("/start", "/stop"):
            self.page("請按下方按鈕操作；開啟書籤不會自動啟停。")
        elif path == "/status":
            head = "執行中\n" if running() else ("啟動中\n" if _starting else "停止\n")
            self._send(head + tail(CONSOLE_LOG, 40), content_type="text/plain; charset=utf-8")
        elif path == "/":
            self.page()
        else:
            self._send("找不到頁面", 404)

    def do_POST(self):
        if not self._local_request():
            self._send("禁止跨站存取", 403)
            return
        if self.path not in ("/start", "/stop"):
            self._send("找不到頁面", 404)
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send("需要 Content-Length", 411)
            return
        if not 0 < size <= 2048 or self.headers.get("Transfer-Encoding"):
            self._send("請求大小不正確", 413)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self._send("需要表單資料", 415)
            return
        data = self.rfile.read(size)
        if len(data) != size:
            self._send("請求內容不完整", 400)
            return
        try:
            form = parse_qs(data.decode("utf-8", "replace"), max_num_fields=8)
        except ValueError:
            self._send("表單欄位過多", 400)
            return
        supplied = form.get("token", [""])
        if len(supplied) != 1 or not secrets.compare_digest(supplied[0].encode("utf-8"), _csrf_token.encode("ascii")):
            self._send("驗證失敗，請重新開啟首頁", 403)
            return
        self.page(start_bot() if self.path == "/start" else stop_bot())

    def log_message(self, fmt, *args):  # 不要一直印到 stdout
        pass


class LocalServer(ThreadingHTTPServer):
    """Bound concurrent handlers, including clients that send incomplete requests."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _request(method: str, path: str, data=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {}
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        body = response.read(131073)
        if (response.status != 200 or response.getheader("X-Autocoin-Trigger") != "1"
                or len(body) > 131072):
            raise RuntimeError("8080 連接埠已被其他服務佔用，或觸發器回應不正確")
        return body.decode("utf-8")
    finally:
        conn.close()


def start_server() -> str:
    try:
        _request("GET", "/status")
        return "觸發器已經在跑了"
    except ConnectionRefusedError:
        pass
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / "serve.log"
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(log.with_suffix(".log.1"))
    with log.open("ab", buffering=0) as out:
        proc = subprocess.Popen([sys.executable, "-u", str(HERE / "serve.py")],
                                cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _request("GET", "/status")
            return f"觸發器啟動了（pid {proc.pid}）"
        except ConnectionRefusedError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    raise RuntimeError(f"觸發器啟動失敗，請檢查 {log}")


def request_stop() -> str:
    """Use the same stop path as Safari, including cancellation during startup."""
    try:
        page = _request("GET", "/")
        token = re.search(r'name="token" value="([A-Za-z0-9_-]+)"', page)
        if token is None:
            raise RuntimeError("觸發器未提供停止驗證碼")
        _request("POST", "/stop", urlencode({"token": token[1]}))
    except ConnectionRefusedError:
        LOGS.mkdir(parents=True, exist_ok=True)
        STOP_FILE.touch()
    return "已送出停止訊號"


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description="本機蝦幣執行控制頁")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--start", action="store_true", help="背景啟動控制頁，不會開始操作手機")
    actions.add_argument("--stop", action="store_true", help="停止執行中的 bot 與尚未完成的啟動")
    args = parser.parse_args()
    if args.start or args.stop:
        try:
            print(start_server() if args.start else request_stop())
            return 0
        except (OSError, http.client.HTTPException, RuntimeError) as e:
            print(f"失敗：{e}", file=sys.stderr)
            return 1
    print(f"觸發器：http://127.0.0.1:{PORT}/（只綁本機）")
    print("Safari 開這個網址 → 分享 → 加入主畫面，之後開啟頁面再按開始。")
    with LocalServer(("127.0.0.1", PORT), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            stop_bot()


if __name__ == "__main__":
    sys.exit(main())
