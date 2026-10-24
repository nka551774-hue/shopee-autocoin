"""TouchSynthesis 遠端協定的電腦端用戶端（TCP 8347，4 位元組大端長度 + JSON）。

座標是 iOS 的 point（iPhone 11 Pro：375x812），截圖是原生像素（1125x2436）。
同一個 Wi-Fi 下直接連手機 IP（環境變數 TS_HOST）；USB 連著時也可以用
`pymobiledevice3 usbmux forward 8347 8347` 轉發後連 127.0.0.1。

用法：
    TS_HOST=192.168.31.196 python tsclient.py shot out.png
    python tsclient.py tap 187 400
    python tsclient.py swipe 187 650 187 250
"""
from __future__ import annotations

import base64
import json
import math
import os
import socket
import struct
import sys
import time
import threading
import uuid


class ProtocolError(RuntimeError):
    """A malformed response must not be treated as a successful device action."""


class TouchSynthesis:
    MAX_FRAME_BYTES = 32 * 1024 * 1024

    def __init__(self, host: str | None = None, port: int = 8347, timeout: float = 20):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        host = host or os.environ.get("TS_HOST", "127.0.0.1")
        self.timeout = timeout
        self._lock = threading.RLock()
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _remaining(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("TouchSynthesis response deadline exceeded")
        self.sock.settimeout(remaining)

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        # A growable buffer avoids quadratic copying and per-fragment retention.
        buffer = bytearray()
        while len(buffer) < n:
            self._remaining(deadline)
            chunk = self.sock.recv(n - len(buffer))
            if not chunk:
                raise ConnectionError("連線被關閉")
            buffer.extend(chunk)
        return bytes(buffer)

    def _recv_frame(self, deadline: float) -> bytes:
        (length,) = struct.unpack(">I", self._recv_exact(4, deadline))
        if not 0 < length <= self.MAX_FRAME_BYTES:
            raise ProtocolError(f"Invalid frame length: {length}")
        return self._recv_exact(length, deadline)

    def call(self, action: str, **params) -> dict:
        with self._lock:
            try:
                return self._call(action, params, time.monotonic() + self.timeout)
            except BaseException:
                # A partial frame or late reply makes subsequent actions ambiguous.
                self.close()
                raise

    def _call(self, action: str, params: dict, deadline: float) -> dict:
        cmd_id = uuid.uuid4().hex
        payload = json.dumps({"id": cmd_id, "action": action, "params": params}).encode()
        self._remaining(deadline)
        self.sock.sendall(struct.pack(">I", len(payload)) + payload)
        while True:
            frame = self._recv_frame(deadline)
            if frame[:1] == b"\xff":  # 串流中的 JPEG 畫面，不是這個指令的回應
                continue
            try:
                resp = json.loads(frame)
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError("Invalid JSON response") from exc
            if not isinstance(resp, dict):
                raise ProtocolError("Response must be a JSON object")
            if resp.get("id") == cmd_id:
                if resp.get("success") is not True:
                    raise RuntimeError(f"{action} 失敗：{resp.get('error')}")
                return resp

    def tap(self, x: float, y: float):
        self.call("tap", x=x, y=y)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.4):
        self.call("swipe", fromX=x1, fromY=y1, toX=x2, toY=y2, duration=duration)

    def press_home(self):
        self.call("pressButton", button=1)  # XCUIDeviceButtonHome

    def screenshot(self, quality: float = 0.8) -> bytes:
        """JPEG，走 XCTest 的截圖（新版 App）；舊版 App 會改走通道上的 DVT 截圖。"""
        data = self.call("screenshot", quality=quality).get("data")
        try:
            if not isinstance(data, str):
                raise ValueError("Screenshot data is not a string")
            return base64.b64decode(data, validate=True)
        except ValueError as exc:
            self.close()
            raise ProtocolError("Invalid screenshot encoding") from exc

    def launch_app(self, bundle_id: str):
        """把 App 叫到前景（沒開就啟動）。"""
        self.call("launchApp", bundleId=bundle_id)

    def tree(self, bundle_id: str) -> dict:
        """App 畫面的元件樹（XCUIElementSnapshot.dictionaryRepresentation）。"""
        return json.loads(self.call("tree", bundleId=bundle_id)["data"])

    def grab(self, quality: float = 0.6, skip: int = 1, timeout: float = 15) -> bytes:
        """JPEG，走 XCTest 的截圖（串流模式）。先丟掉 skip 張，避免拿到點擊前的舊畫面。"""
        if not isinstance(skip, int) or skip < 0:
            raise ValueError("skip must be a nonnegative integer")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        with self._lock:
            deadline = time.monotonic() + timeout
            try:
                self._call("startStream", {"quality": quality}, deadline)
                seen = 0
                while True:
                    data = self._recv_frame(deadline)
                    if data[:1] == b"\xff":
                        seen += 1
                        if seen > skip:
                            self._call("stopStream", {}, deadline)
                            return data
            except BaseException:
                # Closing also stops a stream when its stop acknowledgement times out.
                self.close()
                raise


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ts = TouchSynthesis()
    cmd, *args = sys.argv[1:]
    if cmd == "shot":
        data = ts.grab()
        open(args[0] if args else "shot.png", "wb").write(data)
        print(f"{len(data)} bytes")
    elif cmd == "tap":
        ts.tap(float(args[0]), float(args[1]))
    elif cmd == "swipe":
        ts.swipe(*map(float, args[:4]))
    elif cmd == "home":
        ts.press_home()
    ts.close()
