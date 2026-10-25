"""ZXTouch（rootless 分支：CodeTips/IOS-SimulateTouch）的極簡客戶端，純標準庫。

tweak 在手機上聽 127.0.0.1:6000，協議是純文字：
    送： "<task 2 位數>" + ";;".join(args) + "\r\n"
        注意 task 後面**直接**接第一個參數，中間沒有分隔符
        （tweak 用前 2 個字元當 task，剩下的才是資料：27 + "1;;rect;;…"）
    收： 開頭 "0" = 成功（後面用 ";;" 分隔）；"-1;;訊息" = 失敗
（格式取自上游 client.py / datahandler.py，已逐字核對；selftest.py 有跑過一次）

這一版 tweak（com.zjx.pccontrol 0.0.2 arm64e，repo 內附的 deb）**只實作了**：
    10 觸控、11 切前景、12 alert、18 usleep、19/20 播放／停止腳本、
    22 toast、25 裝置資訊、26 觸控指示、27 OCR
**沒有實作**（fork 把 Task.xm 裡的分支整段註解掉，dylib 裡也查不到字串）：
    13 shell、21 圖像比對、23 取色、28 找色
所以 pick_color / search_color / shell 一律直接丟 ZXUnsupported，不要傻等逾時。

座標全部是**螢幕像素**（iPhone 11 Pro：375x812 pt、3x → 1125x2436 px）：
  - getScreenSize 回傳像素（Tweak.xm 用 bounds.size * scale 去設）
  - 觸控是 x / device_screen_width 的正規化座標，所以要給像素
  - OCR 的 region 直接拿去切截圖的 CGImage（像素），回傳的 x/y/w/h 也是像素、
    而且是**左上角原點**（VKOcrManager 用 (1 - topLeft.y) 換算）
"""
from __future__ import annotations

import socket
import math
import threading
import time

TOUCH_UP = 0
TOUCH_DOWN = 1
TOUCH_MOVE = 2

TASK_TOUCH = 10
TASK_FOREGROUND = 11
TASK_ALERT = 12
TASK_SHELL = 13            # 這一版沒有
TASK_USLEEP = 18
TASK_PLAY_SCRIPT = 19
TASK_STOP_SCRIPT = 20
TASK_IMAGE_MATCH = 21      # 這一版沒有
TASK_TOAST = 22
TASK_COLOR_PICKER = 23     # 這一版沒有
TASK_DEVICE_INFO = 25
TASK_OCR = 27
TASK_COLOR_SEARCH = 28     # 這一版沒有

TOAST_HIDE = 0
TOAST_ERROR = 1
TOAST_WARNING = 2
TOAST_MESSAGE = 3
TOAST_SUCCESS = 4
TOAST_TOP = 0
TOAST_BOTTOM = 1

DEVICE_INFO_SCREEN_SIZE = 1
DEVICE_INFO_SCREEN_ORIENTATION = 2
DEVICE_INFO_SCREEN_SCALE = 3


class ZXTouchError(RuntimeError):
    pass


class ZXUnsupported(ZXTouchError):
    pass


class ZXConnectionError(ZXTouchError, ConnectionError):
    """The connection cannot be safely reused after a framing failure."""


class ZXTouch:
    MAX_RESPONSE = 4 * 1024 * 1024

    def __init__(self, host: str = "127.0.0.1", port: int = 6000, timeout: float = 20.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.host = host
        self.port = port
        self.timeout = timeout
        self._buffer = bytearray()
        self._lock = threading.RLock()
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.s.settimeout(timeout)

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    # ---------------- 底層 ----------------
    def _send(self, task: int, *args) -> None:
        if not isinstance(task, int) or not 10 <= task <= 99:
            raise ValueError("task must be a two-digit integer")
        values = [str(a) for a in args]
        if any("\r" in value or "\n" in value or ";;" in value for value in values):
            raise ValueError("ZXTouch arguments cannot contain protocol delimiters")
        payload = str(task) + ";;".join(values)
        with self._lock:
            try:
                self.s.sendall((payload + "\r\n").encode("utf-8"))
            except OSError:
                self.close()
                raise

    def _recv_raw(self) -> str:
        """讀到一個 \r\n 結尾的完整回應（整頁 OCR 可能好幾 KB，不能只 recv 一次）。"""
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                end = self._buffer.find(b"\r\n")
                if end >= 0:
                    if end > self.MAX_RESPONSE:
                        raise ZXConnectionError("ZXTouch 回應超過大小限制")
                    frame = bytes(self._buffer[:end])
                    del self._buffer[:end + 2]
                    return frame.decode("utf-8")
                if len(self._buffer) > self.MAX_RESPONSE + 1:
                    raise ZXConnectionError("ZXTouch 回應超過大小限制")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ZXTouch 回應逾時")
                self.s.settimeout(remaining)
                chunk = self.s.recv(min(65536, self.MAX_RESPONSE + 2 - len(self._buffer)))
                if not chunk:
                    raise ZXConnectionError("連線被關閉")
                self._buffer.extend(chunk)
        except UnicodeError as exc:
            self.close()
            self._buffer.clear()
            raise ZXConnectionError("ZXTouch 回應不是有效的 UTF-8") from exc
        except OSError:
            self.close()
            self._buffer.clear()
            raise
        finally:
            try:
                self.s.settimeout(self.timeout)
            except OSError:
                pass

    def _call(self, task: int, *args):
        """送一個會回應的任務，回傳 (ok, fields, raw)。"""
        with self._lock:
            self._send(task, *args)
            raw = self._recv_raw()
            fields = raw.split(";;")
            if fields[0] not in ("0", "-1"):
                self.close()
                raise ZXConnectionError("ZXTouch 回應格式不正確")
            return fields[0] == "0", fields[1:], raw

    def _must(self, task: int, *args):
        ok, fields, raw = self._call(task, *args)
        if not ok:
            raise ZXTouchError(raw[2:] or raw)
        return fields

    # ---------------- 觸控（像素） ----------------
    def touch(self, kind: int, finger: int, x: float, y: float) -> None:
        """單點觸控。ZXTouch 對觸控不回回應，所以這裡不讀。"""
        self._send(TASK_TOUCH, "1" + self._touch_data(kind, finger, x, y))

    @staticmethod
    def _touch_data(kind, finger, x, y):
        if kind not in (TOUCH_UP, TOUCH_DOWN, TOUCH_MOVE) or not isinstance(finger, int) or not 0 <= finger <= 99:
            raise ValueError("invalid touch type or finger")
        if not all(math.isfinite(v) and 0 <= v <= 9999.9 for v in (x, y)):
            raise ValueError("touch coordinates exceed the fixed-width protocol")
        return "{}{:02d}{:05d}{:05d}".format(kind, finger, int(x * 10), int(y * 10))

    def touch_list(self, events: list) -> None:
        """多指同時觸控。events: [{"type": TOUCH_DOWN, "finger_index": 1, "x": 300, "y": 300}, ...]"""
        if not 1 <= len(events) <= 9:
            raise ValueError("touch_list requires one to nine events")
        data = "".join(self._touch_data(e["type"], e["finger_index"], e["x"], e["y"]) for e in events)
        self._send(TASK_TOUCH, str(len(events)) + data)

    def tap(self, x: float, y: float, hold: float = 0.05) -> None:
        if not math.isfinite(hold) or not 0 <= hold <= 60:
            raise ValueError("invalid hold duration")
        self.touch(TOUCH_DOWN, 1, x, y)
        try:
            time.sleep(hold)
        finally:
            self.touch(TOUCH_UP, 1, x, y)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.4, steps: int = 12) -> None:
        if not isinstance(steps, int) or not 1 <= steps <= 1000 or not math.isfinite(duration) or not 0 <= duration <= 60:
            raise ValueError("invalid swipe duration or steps")
        self._touch_data(TOUCH_DOWN, 1, x1, y1)
        self._touch_data(TOUCH_UP, 1, x2, y2)
        self.touch(TOUCH_DOWN, 1, x1, y1)
        try:
            for i in range(1, steps + 1):
                t = i / steps
                self.touch(TOUCH_MOVE, 1, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
                time.sleep(duration / steps)
        finally:
            self.touch(TOUCH_UP, 1, x2, y2)

    # ---------------- 畫面 ----------------
    def screen_size(self):
        """回傳 (寬, 高)，單位是**像素**（1125x2436）。"""
        f = self._must(TASK_DEVICE_INFO, DEVICE_INFO_SCREEN_SIZE)
        try:
            w, h = map(float, f)
            if not all(math.isfinite(v) and v > 0 for v in (w, h)):
                raise ValueError
            return w, h
        except (TypeError, ValueError) as e:
            raise ZXTouchError("無效的螢幕尺寸") from e

    def screen_scale(self) -> float:
        f = self._must(TASK_DEVICE_INFO, DEVICE_INFO_SCREEN_SCALE)
        try:
            value, = map(float, f)
            if not math.isfinite(value) or value <= 0:
                raise ValueError
            return value
        except (TypeError, ValueError) as e:
            raise ZXTouchError("無效的螢幕比例") from e

    def screen_orientation(self) -> int:
        return int(self._must(TASK_DEVICE_INFO, DEVICE_INFO_SCREEN_ORIENTATION)[0])

    def ocr(self, region, languages=("zh-Hant",), level: int = 0, correct: int = 0,
            min_height: str = "", custom_words=()):
        """OCR 一塊區域。region = (x, y, width, height)，單位**像素**；給 (0,0,0,0) 就是整頁。

        回傳 (lines, raw)：lines 是 [{"text", "x", "y", "width", "height"}]，
        x/y 是文字的**左上角**、單位像素（跟傳進去的 region 同一組座標）。
        """
        x, y, w, h = region
        f = self._must(TASK_OCR, 1, ",,".join(str(int(v)) for v in (x, y, w, h)),
                       ",,".join(str(c) for c in custom_words), min_height, level,
                       ",,".join(languages), correct, "")
        lines = []
        for item in f:
            if not item:
                continue
            parts = item.rsplit(",,", 4)
            try:
                if len(parts) != 5:
                    raise ValueError("missing OCR geometry")
                values = [float(value) for value in parts[1:]]
                if not all(math.isfinite(value) and value >= 0 for value in values):
                    raise ValueError("invalid OCR geometry")
                lines.append(dict(zip(("text", "x", "y", "width", "height"), [parts[0], *values])))
            except ValueError as exc:
                self.close()
                self._buffer.clear()
                raise ZXConnectionError("OCR 回應不完整，無法安全判讀畫面") from exc
        return lines, f

    # ---------------- 其他 ----------------
    def foreground(self, bundle_id: str):
        """把 App 叫到前景（沒開就啟動）。"""
        return self._call(TASK_FOREGROUND, bundle_id)

    def toast(self, content: str, toast_type: int = TOAST_MESSAGE, duration: int = 2,
              position: int = TOAST_TOP, font_size: int = 0):
        """在畫面上顯示一行字（跟 Android 版的小黑條一樣的用途）。"""
        return self._call(TASK_TOAST, toast_type, content, duration, position, font_size)

    def alert(self, title: str, content: str, duration: int = 3):
        return self._call(TASK_ALERT, title, content, duration)

    def play_script(self, path: str):
        """播放 .bdl 腳本。這一版 tweak 跑 .py 需要 /bin/python3 + sudo + zxtouchb，
        這個 deb 都沒附，所以實務上只會跳錯誤——要一鍵請用 serve.py 那條路。"""
        return self._call(TASK_PLAY_SCRIPT, path)

    def stop_script(self):
        return self._call(TASK_STOP_SCRIPT)

    # ---------------- 這一版沒有的功能 ----------------
    def pick_color(self, x, y):
        raise ZXUnsupported("這一版 tweak 沒有取色（task 23 被註解掉了）")

    def search_color(self, region, rgb_range, skip: int = 0):
        raise ZXUnsupported("這一版 tweak 沒有找色（task 28 被註解掉了）")

    def shell(self, command: str):
        raise ZXUnsupported("這一版 tweak 沒有 shell（task 13 被註解掉了）")
