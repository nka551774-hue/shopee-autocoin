"""手機連線（ADB + uiautomator2）與基本操作。"""
from __future__ import annotations

import logging
import random
import time

import adbutils
import uiautomator2 as u2

from .ui import PKG, Node, Snapshot

log = logging.getLogger(__name__)


class UnsafeAction(RuntimeError):
    """前景不符或動作結果不確定；不可自動重送。"""


class Device:
    def __init__(self, serial: str, host: str = "127.0.0.1", port: int = 5037):
        self.serial = serial
        # 不用 adbutils 預設的 client：它讀 ANDROID_ADB_SERVER_PORT，使用者環境裡是 5137
        self.adb = adbutils.AdbClient(host=host, port=port)
        self.d: u2.Device | None = None
        self.before_action = lambda: None
        self.connect()

    def connect(self):
        if ":" in self.serial:  # 無線偵錯
            try:
                self.adb.connect(self.serial, timeout=10)
            except Exception as e:
                log.warning("adb connect %s 失敗：%s", self.serial, e)
        device = self.adb.device(self.serial or None)
        self.serial = device.serial
        self.d = u2.connect(device)

    def _retry(self, fn):
        """連線偶爾會斷（Tailscale、uiautomator server 重啟），重連後再試。"""
        for attempt in range(3):
            self.before_action()
            try:
                return fn()
            except Exception as e:
                if attempt == 2:
                    raise
                log.warning("裝置操作失敗（%s: %s），重新連線後重試", type(e).__name__, e)
                time.sleep(3 * (attempt + 1))
                self.before_action()
                try:
                    self.connect()
                except Exception as e2:
                    log.warning("重新連線失敗：%s", e2)

    def shell(self, cmd: str) -> str:
        return self._retry(lambda: self.adb.device(self.serial).shell(cmd))

    # ---------- 讀取 ----------
    def snapshot(self) -> Snapshot:
        xml = self._retry(lambda: self.d.dump_hierarchy())
        cur = self.current()
        return Snapshot(xml, cur.get("package", ""), cur.get("activity", ""))

    def current(self) -> dict:
        try:
            return self._retry(lambda: self.d.app_current())
        except Exception:
            return {}

    def screen_on(self) -> bool:
        return "mWakefulness=Awake" in self.shell("dumpsys power | grep mWakefulness=")

    def locked(self) -> bool:
        return "isKeyguardShowing=true" in self.shell("dumpsys window | grep isKeyguardShowing")

    def home_package(self) -> str:
        out = self.shell("cmd package resolve-activity --brief -a android.intent.action.MAIN "
                         "-c android.intent.category.HOME").strip().splitlines()
        return out[-1].split("/")[0] if out else ""

    def last_toast(self) -> str:
        try:
            return self._retry(lambda: self.d.last_toast) or ""
        except Exception:
            return ""

    def clear_toast(self):
        try:
            self._retry(lambda: self.d.clear_toast())
        except Exception:
            pass

    def screenshot(self, path: str):
        self._retry(lambda: self.d.screenshot(path))

    # ---------- 操作 ----------
    # 抖動量：每次都點同一個像素、滑動時間一模一樣，是很明顯的機器特徵
    tap_jitter_px = 9
    swipe_jitter = 0.3

    def _jitter_xy(self, x: int, y: int) -> tuple[int, int]:
        r = self.tap_jitter_px
        return x + random.randint(-r, r), y + random.randint(-r, r)

    def _action(self, command: list[str]):
        self.before_action()
        if self.current().get("package") != PKG or self.locked():
            raise UnsafeAction("蝦皮不在前景或手機已鎖定，已停止操作")
        self._send_once(command)

    def _send_once(self, command: list[str]):
        self.before_action()
        try:
            # uiautomator2 的 jsonrpc_call 自帶斷線重送，click 也會被重送。
            # ADB 的公開 shell API 只送一次；連線錯誤即交給人確認結果。
            result = self.adb.device(self.serial).shell2(command, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(f"ADB 動作失敗，退出碼 {result.returncode}")
        except Exception as exc:
            raise UnsafeAction("裝置動作結果不明，已停止，請先確認手機畫面") from exc

    def tap(self, x: int, y: int):
        jx, jy = self._jitter_xy(x, y)
        self._action(["input", "tap", str(max(0, jx)), str(max(0, jy))])

    def tap_node(self, node: Node):
        if node is None or node.el.get("enabled") == "false":
            raise UnsafeAction("UI 節點不存在或尚未啟用")
        x, y = node.spot
        self._action(["input", "tap", str(x), str(y)])

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.4):
        dx, dy = random.randint(-12, 12), random.randint(-12, 12)
        dur = max(0.05, duration * random.uniform(1 - self.swipe_jitter, 1 + self.swipe_jitter))
        self._action(["input", "swipe", str(max(0, fx + dx)), str(max(0, fy + dy)),
                      str(max(0, tx + dx)), str(max(0, ty + dy)), str(round(dur * 1000))])

    def swipe_reference(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.4):
        """研究座標來自 1080×2400，依目前裝置尺寸縮放。"""
        width, height = self._retry(lambda: self.d.window_size())
        self.swipe(round(fx * width / 1080), round(fy * height / 2400),
                   round(tx * width / 1080), round(ty * height / 2400), duration)

    def back(self):
        self._action(["input", "keyevent", "4"])

    def wake(self):
        self._send_once(["input", "keyevent", "224"])

    def launch(self):
        self._send_once(["monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1"])

    def restart_app(self):
        self._action(["am", "force-stop", PKG])
        self.launch()
