"""iOS（jailbreak / ZXTouch）版主流程：往上滑換台找有「直播間蝦幣」的直播間、待著領取，
定時開面板領觀看任務與提領。

跟電腦版（autocoin/bot.py）、TouchSynthesis 版（ios/bot.py）同一套流程，差別只有「手機連線」：
  - 觸控與 OCR 都走手機上的 ZXTouch（127.0.0.1:6000），不用電腦、不用 VPN、不用 pairing。
  - 這一版 tweak 沒有取色，畫面上的「領取」按鈕改成 OCR 按鈕上的字（見 screens.py）。
  - 沒有「看更多」列表，只能一間一間滑。轉盤、紅包雨還沒研究，目前不處理。

手機上要先：裝好 ZXTouch（rootless deb）→ respring → 蝦皮停在「直播」分頁 →
「設定 > 螢幕顯示與亮度 > 自動鎖定」設成「永不」。

用法（在手機上用 NewTerm 跑，或在電腦上跑也可以——電腦版只是多一台機器）：
    python3 bot.py run [--hours 3] [--no-withdraw] [-v]
    python3 bot.py status      今天的統計
    python3 bot.py panel       開一次任務獎勵面板（領觀看任務、視情況提領）
    python3 bot.py look        印出目前畫面的判讀結果（除錯用）
    python3 bot.py tap 330 500 點一下（point）
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import re
import sys
import time
import zlib
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from autocoin.config import Config  # noqa: E402
from autocoin.state import StateError, Store, reward_now  # noqa: E402
from jitter import set_jitter, up, wobble, wobble_xy  # noqa: E402
from screens import (BACK, Panel, Room, Screen, Shot, detect, money, read_panel,  # noqa: E402
                     read_room, read_withdraw)
from zxtouch import TOAST_ERROR, TOAST_SUCCESS, ZXTouch  # noqa: E402

log = logging.getLogger("autocoin")

# App 內的提示文字（跟 autocoin/bot.py 同一組，只取比較短的片段，OCR 錯一兩個字也對得到）
MSG_RISK = ("不符合領取此獎勵資格", "偵測到風險", "多裝置使用上限", "裝置數限制")
MSG_DAILY_LIMIT = ("超過今天領取的次數", "達領取上限")
MSG_ROOM_DONE = ("已全部發放",)
POPUP_DISMISS = ("稍後再說",)  # 蓋住畫面的系統視窗，按這個關掉

REASONS = {
    "no_coins": "沒有直播間蝦幣",
    "room_done": "這場的蝦幣發完了",
    "daily_limit": "今天的直播間蝦幣次數用完了",
    "timeout": "待太久了",
    "watched": "觀看任務在這間已累計 10 分鐘",
    "lost": "畫面跑掉了",
}
# 因為這些原因離開的直播間，今天就不再進去
DONE_REASONS = {"no_coins", "room_done", "timeout", "watched"}

SCAN_ROOMS = 15        # 一輪最多往下滑幾間
WATCH_FROZEN = 90      # 觀看任務的倒數停住這麼久，代表這間的 10 分鐘已經算滿
SWIPE_NEXT = (187, 600, 187, 150, 0.25)


class StopBot(Exception):
    """遇到不該繼續的狀況（例如蝦皮回報帳號有風險），整個程式停下來。"""


def same(a: str, b: str) -> bool:
    """OCR 讀直播主名稱偶爾會差一兩個字（「大大的紫」讀成「大大、」），名字夠像就當同一間。"""
    a, b = re.sub(r"\W", "", a).lower(), re.sub(r"\W", "", b).lower()
    if not a or not b:
        return False
    if min(len(a), len(b)) >= 2 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.6


class _ZxProxy:
    """給 screens.Shot 用的小代理：每個呼叫都走 ZXPhone._call，斷線會自動重連。"""

    def __init__(self, phone: "ZXPhone"):
        self.phone = phone
        self._scale = None
        self._size = None

    def screen_scale(self) -> float:
        if self._scale is None:
            self._scale = self.phone._call(lambda zx: zx.screen_scale())
        return self._scale

    def screen_size(self):
        if self._size is None:
            self._size = self.phone._call(lambda zx: zx.screen_size())
        return self._size

    def ocr(self, region, **kw):
        return self.phone._call(lambda zx: zx.ocr(region, **kw))


class ZXPhone:
    """ZXTouch 連線。斷線就重連；tweak 被卸載或手機重開的話要重新 respring。"""

    GIVE_UP = 30 * 60

    def __init__(self, host: str = "127.0.0.1", port: int = 6000):
        self.host = host
        self.port = port
        self.zx: ZXTouch | None = None
        self.scale: float | None = None
        self.stop_check = None  # Bot.run() 會塞 time_up 進來，連不上時才不會傻等 30 分鐘
        self.tap_jitter = 5.0   # 點擊座標 ±point。蝦皮的「領取」只有約 40x24pt，抖太大會點出界
        self.dry = False        # True = 只讀畫面不點（--dry-run）
        self._proxy = _ZxProxy(self)

    def _call(self, fn, retry: bool = True):
        deadline = time.monotonic() + self.GIVE_UP
        warned = False
        while True:
            if self.stop_check and self.stop_check():
                raise StopBot("收到停止訊號")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StopBot("ZXTouch 一直連不上")
            try:
                if self.zx is None:
                    self.zx = ZXTouch(self.host, self.port, timeout=min(40, remaining))
                    if warned:
                        log.info("ZXTouch 連上了")
                return fn(self.zx)
            except OSError as e:  # 連線被拒、逾時、斷線（ConnectionError 也是 OSError）
                if self.zx:
                    self.zx.close()
                    self.zx = None
                self._proxy._scale = self._proxy._size = self.scale = None
                if not retry:
                    raise StopBot("觸控送出結果不明，停止以免重複操作") from e
                if not warned:
                    warned = True
                    log.warning("連不到 ZXTouch（%s）：tweak 還在嗎？裝完要 respring", e)
                if time.monotonic() >= deadline:
                    raise StopBot("ZXTouch 一直連不上") from e
                if self.stop_check and self.stop_check():
                    raise StopBot("收到停止訊號")
                retry_at = min(deadline, time.monotonic() + 10)
                while time.monotonic() < retry_at:
                    if self.stop_check and self.stop_check():
                        raise StopBot("收到停止訊號")
                    time.sleep(max(0, min(0.25, retry_at - time.monotonic())))

    def shot(self) -> Shot:
        s = Shot(self._proxy)
        self.scale = s.scale
        return s

    def _px(self, x: float, y: float):
        """point → 像素。scale 還沒問過 tweak 就問一次（非 3x 的機器才不會點歪）。"""
        if self.scale is None:
            self.scale = self._call(lambda zx: zx.screen_scale())
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise StopBot("螢幕比例不正確，停止以免點擊錯誤位置")
        return x * self.scale, y * self.scale

    def tap(self, x: float, y: float):
        x, y = wobble_xy(x, y, self.tap_jitter)  # 不要每次都點在同一個像素
        if self.dry:
            log.info("（dry-run）想點 (%.0f, %.0f)", x, y)
            return
        px, py = self._px(x, y)
        self._call(lambda zx: zx.tap(px, py), retry=False)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.4):
        dx, dy = wobble_xy(0, 0, 6)      # 每次都滑同一條線也是特徵
        duration = max(0.05, wobble(duration, 0.3))
        if self.dry:
            log.info("（dry-run）想滑 (%.0f, %.0f) → (%.0f, %.0f)", x1, y1, x2, y2)
            return
        a, b = self._px(x1 + dx, y1 + dy)
        c, d = self._px(x2 + dx, y2 + dy)
        self._call(lambda zx: zx.swipe(a, b, c, d, duration), retry=False)

    def toast(self, content: str, kind: int = TOAST_SUCCESS):
        if self.dry:
            return
        try:
            self._call(lambda zx: zx.toast(content, kind), retry=False)
        except Exception as e:
            log.debug("toast 失敗：%s", e)

    def close(self):
        if self.zx:
            self.zx.close()
            self.zx = None


class Bot:
    def __init__(self, cfg: Config, phone: ZXPhone, store: Store):
        self.cfg = cfg
        self.phone = phone
        self.store = store
        self.captures = Path(cfg.log_dir) / "captures-iosjb"
        self.captures.mkdir(parents=True, exist_ok=True)
        self.end: float | None = None      # run(hours=…) 的結束時間
        self.stop_file: Path | None = None  # 出現這個檔案就停（一鍵停止用）
        self.first: set[str] = set()       # 還沒研究過的畫面，每次執行各存一張
        self.dry = False                   # True = 只讀畫面不點（--dry-run）
        self._caps: dict[tuple[str, float], float] = {}  # 每個直播間抽一次的上限
        self._panel_key: float = -1.0      # 面板間隔的抖動只在每個週期抽一次
        self._panel_gap: float = 0.0
        self._caps_date = store.day.date
        self.phone.stop_check = self.time_up
        set_jitter(cfg.jitter)

    def _room_cap(self, key: str, base: float, pct: float = 0.25) -> float:
        """每個直播間用不同的上限，而且只往上抖（往下抖會讓觀看任務做不完）。"""
        k = (key, base)
        if self._caps_date != self.day.date:
            self._caps.clear()
            self._caps_date = self.day.date
        if k not in self._caps:
            self._caps[k] = up(base, pct)
        return self._caps[k]

    @property
    def day(self):
        return self.store.day

    # ---------------- 基本工具 ----------------
    def pause(self, seconds: float):
        """Poll cancellation during long waits instead of sleeping up to five minutes."""
        deadline = time.monotonic() + max(0.05, wobble(seconds, self.cfg.jitter))
        while True:
            if self.time_up():
                raise StopBot("收到停止訊號或執行時間已到")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def _check_risk(self, s: Shot):
        for text in s.texts():
            if any(message in text for message in MSG_RISK):
                self.capture("risk", s)
                raise StopBot(f"蝦皮回報「{text}」，為了帳號安全先停止")

    def look(self) -> tuple[Screen, Shot]:
        s = self.phone.shot()
        self._check_risk(s)
        # 系統的評分邀請（「喜歡「蝦皮購物」嗎？」）會蓋住整個畫面，提領完就跳出來過
        later = next((ln for ln in s.lines if ln.text in POPUP_DISMISS and ln.y < 560), None)
        if later and not self.dry:
            log.info("關掉彈出視窗（%s）", later.text)
            self.phone.tap(later.x, later.y)
            self.pause(1.5)
            s = self.phone.shot()
            self._check_risk(s)
        return detect(s), s

    def wait_for(self, wanted: set[Screen], timeout: float = 10) -> tuple[Screen, Shot]:
        deadline = time.monotonic() + timeout
        while True:
            scr, s = self.look()
            if scr in wanted or time.monotonic() >= deadline:
                return scr, s
            self.pause(0.7)

    def capture(self, tag: str, s: Shot | None = None):
        """存下目前畫面的判讀結果（這一版 tweak 拿不到截圖，所以只有 OCR 文字）。"""
        safe = re.sub(r"[^\w.-]+", "_", tag)   # 3.11 的 f-string 不能含反斜線，先算好
        base = self.captures / f"{reward_now():%m%d-%H%M%S}-{safe}"
        try:
            s = s or self.phone.shot()
            Path(f"{base}.txt").write_text(
                "\n".join(f"({ln.x:4.0f},{ln.y:4.0f}) {ln.text}" for ln in s.lines), encoding="utf-8")
        except Exception:
            log.debug("存畫面失敗", exc_info=True)
        for old in sorted(self.captures.glob("*.txt"))[:-300]:
            old.unlink(missing_ok=True)
        log.debug("已存畫面 %s", base.name)

    def capture_once(self, tag: str, s: Shot | None = None):
        if tag not in self.first:
            self.first.add(tag)
            self.capture(tag, s)

    def check_messages(self, s: Shot) -> str:
        """比對 App 的提示文字。遇到風控就停止；回傳 daily_limit / room_done / 空字串。"""
        texts = s.texts()

        def hit(keys):
            return next((t for t in texts if any(k in t for k in keys)), None)

        if t := hit(MSG_RISK):
            self.capture("risk", s)
            raise StopBot(f"蝦皮回報「{t}」，為了帳號安全先停止")
        if t := hit(MSG_DAILY_LIMIT):
            if not self.day.coin_limit_hit:
                log.info("直播間蝦幣今天的次數用完了（%s）", t)
                self.day.coin_limit_hit = True
                self.store.save()
            return "daily_limit"
        if hit(MSG_ROOM_DONE):
            return "room_done"
        return ""

    def known(self, name: str) -> str:
        """對到今天已經記錄過的直播間就沿用原本的名字。"""
        return next((k for k in self.day.rooms if same(k, name)), name)

    def done(self, key: str) -> bool:
        return self.day.room_done(key)

    # ---------------- 導航 ----------------
    def back_to_room(self, scr: Screen, s: Shot) -> bool:
        """收掉蓋在直播間上面的頁面（面板、提領頁）。回不到直播間就回傳 False。

        認不得的畫面先多看幾眼（OCR 偶爾整頁漏讀）；仍認不得就交還控制，
        不在未知 App 上嘗試固定座標的返回鍵。"""
        unknown = 0
        for _ in range(6):
            if scr is Screen.ROOM:
                return True
            if scr in (Screen.PANEL, Screen.WITHDRAW):
                self.phone.tap(*BACK)
            else:
                unknown += 1
                if unknown >= 3:
                    self.capture("unknown", s)
                    return False
            self.pause(2)
            scr, s = self.look()
        return scr is Screen.ROOM

    def go_room(self) -> Shot:
        """回到直播間。回不去就不亂點，等手機被切回蝦皮的「直播」分頁。"""
        scr, s = self.look()
        if scr is Screen.ROOM:
            return s
        if self.back_to_room(scr, s):
            return self.phone.shot()
        log.warning("回不到直播間（畫面已存檔）。不會亂點，請把手機切回蝦皮的「直播」分頁…")
        self.capture("lost", s)
        while not self.time_up():
            self.pause(20)
            scr, s = self.look()
            if scr is Screen.ROOM:
                log.info("回到直播間，繼續")
                return s
            if scr in (Screen.PANEL, Screen.WITHDRAW):
                self.back_to_room(scr, s)
        raise StopBot("時間到了，還是回不到直播間")

    def read_current(self) -> Room:
        """讀目前這間。任務框常常晚一點才出來（或閃一下不見），沒看到直播間蝦幣就再看一次。"""
        r = read_room(self.go_room())
        if not r.coin.present or not r.key:
            self.pause(2)
            again = read_room(self.go_room())
            if again.coin.present or not r.key:
                r = again
        return r

    def next_room(self):
        self.phone.swipe(*SWIPE_NEXT)
        self.pause(3)

    def find_room(self) -> tuple[str, str]:
        """從目前這間開始往上滑，找今天還沒處理過、有直播間蝦幣的直播間；回傳（目的, 直播主）。
        滑了一輪都沒有的話，觀看任務還沒做完就在最後那間累積觀看時間。"""
        r = Room()
        for i in range(SCAN_ROOMS):
            if self.time_up():
                raise StopBot("收到停止訊號或執行時間已到")
            if i:
                self.next_room()
            r = self.read_current()
            day = self.day
            key = self.known(r.key)
            # 沒有任務框的是短影音或沒參加活動的直播；有些直播間只有直播間蝦幣、沒有觀看任務
            if not (r.watch.present or r.coin.present) or self.done(key):
                log.debug("略過 %s（%s）", key or "?", "處理過了" if self.done(key) else "沒有任務框")
                continue
            if r.coin.present and not day.coin_limit_hit:
                log.info("→ %s 有直播間蝦幣（%s）", key or "?", r.coin.state or "?")
                return "coins", key
            if day.coin_limit_hit and not day.watch_done and not day.rooms.get(key, {}).get("watched"):
                log.info("→ %s，累積觀看任務", key or "?")
                return "watch", key
        key = self.known(r.key)
        if (not day.watch_done and r.watch.present and not self.done(key)
                and not day.rooms.get(key, {}).get("watched")):
            log.info("滑了 %d 間都沒有直播間蝦幣，在 %s 累積觀看任務", SCAN_ROOMS, key or "?")
            return "watch", key
        return "", ""

    # ---------------- 待在直播間 ----------------
    def stay(self, purpose: str, key: str) -> tuple[str, str]:
        """待在目前的直播間直到該走了。purpose：coins / watch。回傳（原因, 直播主）。"""
        cfg = self.cfg
        start = time.monotonic()
        coin_missing = odd_state = watch_stuck = None
        watch_last, watch_since = None, time.monotonic()
        while True:
            if self.time_up():
                return "timeout", key
            scr, s = self.look()
            if scr is not Screen.ROOM:
                if not self.back_to_room(scr, s):
                    return "lost", key
                continue
            r = read_room(s)
            # 不拿名字判斷有沒有被換台：iOS 只有我們自己滑才會換台，而名字的 OCR 偶爾會整串讀歪
            #（「ShhGirl女裝批發」讀成「ShhGirl*R}tt」）。直播結束的話，蝦幣不見或畫面認不得會處理
            key = key or self.known(r.key)
            day = self.day

            verdict = self.check_messages(s)
            if verdict == "room_done" or (verdict == "daily_limit" and purpose == "coins"):
                return verdict, key

            # 觀看任務：每間只算 10 分鐘，算滿之後倒數會停住不動
            secs = r.watch.seconds
            if secs != watch_last:
                watch_last, watch_since = secs, time.monotonic()
            watch_full = secs is not None and time.monotonic() - watch_since > WATCH_FROZEN
            # 到點會自己領，卡在「領取」太久才幫忙按
            if r.watch.claimable:
                watch_stuck = watch_stuck or time.monotonic()
                if time.monotonic() - watch_stuck > 15:
                    self.phone.tap(*r.watch.button)
                    watch_stuck = None
            else:
                watch_stuck = None

            if self.panel_due():
                self.panel_tasks()
                continue

            elapsed = time.monotonic() - start
            if elapsed > self._room_cap(key, cfg.max_room_minutes) * 60:
                return "timeout", key
            if purpose == "watch":
                if r.coin.present and not day.coin_limit_hit:
                    log.info("這間出現直播間蝦幣，改成領蝦幣")
                    purpose = "coins"
                    continue
                if watch_full or elapsed > self._room_cap(key, cfg.watch_room_minutes) * 60:
                    return "watched", key
                self.pause(20)
                continue

            # 直播間蝦幣
            if not r.coin.present:
                coin_missing = coin_missing or time.monotonic()
                if time.monotonic() - coin_missing > cfg.no_coin_grace:
                    return "no_coins", key
                self.pause(3)
                continue
            coin_missing = None
            if r.coin.claimable:
                if "成功" in r.claimed:  # 上一次的成功提示還在，按鈕還沒換掉
                    self.pause(1)
                else:
                    self.claim(r, key)
                continue
            secs = r.coin.seconds
            if secs is None:  # 剛領完的空檔、或 OCR 沒讀到
                odd_state = odd_state or time.monotonic()
                if time.monotonic() - odd_state > 45:
                    self.capture(f"coin-{r.coin.state or 'blank'}", s)
                    return f"coin_state:{r.coin.state or '?'}", key
                self.pause(2)
                continue
            odd_state = None
            nap = secs - random.randint(1, 6)  # 不要每次都剛好提前 3 秒醒
            self.pause(min(max(nap, 1), 20))

    def claim(self, r: Room, key: str):
        """按「直播間蝦幣」的領取，確認有成功。"""
        self.phone.tap(*r.coin.button)
        amount = None
        deadline = time.monotonic() + 6
        s = None
        while time.monotonic() < deadline:
            s = self.phone.shot()
            verdict = self.check_messages(s)
            if verdict:
                log.info("領取沒成功：%s", REASONS.get(verdict, verdict))
                return
            got = read_room(s).claimed
            if "成功" in got:
                amount = money(got)
                if amount is None:
                    log.warning("領取成功但金額沒讀到（%s）", got)
                    amount = 0.0
                break
            self.pause(0.35)
        if amount is None:
            # 沒看到成功提示：倒數已經重新開始的話也算成功（金額不明）
            after = read_room(s).coin if s else None
            if after and (after.seconds or 0) > 120:
                amount = 0.0
                log.warning("沒看到成功提示，但倒數重新開始了，當作有領到")
            else:
                log.warning("直播間蝦幣按了沒反應（現在顯示：%s）",
                            (after.state if after and after.present else "不見了") or "?")
                self.capture("claim-unknown", s)
                return
        day = self.day
        room = day.room(key)
        room["claims"] += 1
        room["coins"] = round(room["coins"] + amount, 2)
        day.coin_claims += 1
        day.coin_total = round(day.coin_total + amount, 2)
        self.store.save()
        log.info("✔ 直播間蝦幣 +%.2f｜%s 第 %d 次｜今天 %d 次，共 %.2f",
                 amount, key or "?", room["claims"], day.coin_claims, day.coin_total)
        # 成功提示出來後按鈕還會停在「領取」4 秒左右；等它換掉再走，免得又按一次
        #（那個位置按下去會打開任務獎勵面板）
        for _ in range(10):
            self.pause(0.8)
            if not read_room(self.phone.shot()).coin.claimable:
                break

    # ---------------- 任務獎勵面板 ----------------
    def panel_due(self) -> bool:
        day = self.day
        base = self.cfg.panel_every_minutes * 60
        # 門檻每個週期只抽一次，避免「每跑一圈都重抽」而提早觸發
        if self._panel_key != day.last_panel:
            self._panel_key = day.last_panel
            self._panel_gap = wobble(base, 0.25)
        if time.time() - day.last_panel > self._panel_gap:
            return True
        # 觀看任務的蝦幣要當天領，23:35~23:50 之間隨機挑一個時間點再開一次
        late = self._late_panel_at()
        return reward_now() >= late and day.last_panel < late.timestamp()

    def _late_panel_at(self) -> datetime:
        """每天固定的收尾時間（同一天內固定，換日才會變）。"""
        minute = 35 + (zlib.crc32(self.day.date.encode("ascii")) % 16)
        return reward_now().replace(hour=23, minute=minute, second=0, microsecond=0)

    def panel_tasks(self, final: bool = False):
        """開任務獎勵面板：把觀看任務的蝦幣領進待提領，需要時提領。
        直播間會縮成右下角浮窗，倒數不受影響。"""
        if self.dry:
            self.report()
            return
        day = self.day
        day.last_panel = time.time()
        self.store.save()
        scr, s = self.look()
        if scr is Screen.ROOM:
            r = read_room(s)
            if not r.banner:
                log.info("這間沒有任務框，晚點再開面板")
                return
            self.phone.tap(*r.banner)
            scr, s = self.wait_for({Screen.PANEL}, 15)  # 面板是網頁，有時載得比較慢
        if scr is not Screen.PANEL:
            log.warning("任務獎勵面板沒打開（目前在%s）", scr.value)
            self.capture("panel-not-open", s)
            self.back_to_room(scr, s)
            return
        self.pause(1.5)  # 等畫面載完
        s = self.phone.shot()
        self.check_messages(s)
        p = read_panel(s)
        if p.claim:
            before = p.pending
            self.phone.tap(*p.claim)
            self.pause(1.5)
            s = self.phone.shot()
            self.check_messages(s)
            confirmed = s.find("已發放至") is not None
            self.capture_once("panel-claimed", s)
            # 領完會跳出「0.21 蝦幣已發放至您的"待提領蝦幣數"」，提示還在的時候按別的都沒反應
            for _ in range(8):
                if not s.find("已發放至"):
                    break
                self.pause(1)
                s = self.phone.shot()
                self.check_messages(s)
            after = read_panel(s)
            delta = round(after.pending - before, 2) if before is not None and after.pending is not None else None
            confirmed = confirmed or (delta is not None and delta > 0)
            if confirmed:
                amount = delta if delta is not None and delta > 0 else p.claim_amount
                day = self.day
                day.watch_total = round(day.watch_total + (amount or 0.0), 2)
                log.info("✔ 觀看任務 +%s（領進待提領）", f"{amount:.2f}" if amount is not None else "?")
            else:
                log.warning("觀看任務領取結果未確認，不更新領取金額")
                self.capture("panel-claim-unknown", s)
            p = after
        # 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤；只剩一個 90 的話分不出是哪個，就不更新
        day = self.day
        if len(p.minutes) >= 2 or (p.minutes and p.minutes[0] != 90):
            day.watch_minutes = p.minutes[0]
        if p.done or day.watch_minutes >= 90:
            day.watch_done = True
        if p.pending is not None:
            day.pending = p.pending
        self.store.save()
        log.info("待提領 %.2f｜觀看任務 %d/90 分鐘", day.pending, day.watch_minutes)
        if self.should_withdraw(day.pending, final):
            self.withdraw(p)
        self.back_to_room(*self.look())

    def should_withdraw(self, pending: float, final: bool) -> bool:
        cfg, day = self.cfg, self.day
        if (not cfg.withdraw or day.withdrawal_pending or day.withdrawals >= cfg.withdraw_daily_limit
                or not math.isfinite(pending) or pending < 1):
            return False
        late = reward_now() >= self._late_panel_at()
        return pending >= cfg.withdraw_min or final or late

    def _withdrawal_rejected(self, scr: Screen, s: Shot) -> bool:
        """Only an explicit refusal can clear an in-flight withdrawal."""
        if scr not in (Screen.PANEL, Screen.WITHDRAW):
            return False
        limit = next((ln.text for ln in s.lines if "提領" in ln.text
                      and any(marker in ln.text for marker in ("已達", "已超過", "已用完", "不能再"))), None)
        if not limit:
            return False
        log.info("提領被拒：%s，今天不再提領", limit)
        day = self.day
        day.withdrawals = self.cfg.withdraw_daily_limit
        day.withdrawal_pending = False
        self.store.save()
        return True

    def withdraw(self, p: Panel):
        """在面板按「提領」，選不超過待提領的最大面額，送出並確認待提領有變少。"""
        day = self.day
        if self.dry or day.withdrawal_pending or not self.cfg.withdraw or day.withdrawals >= self.cfg.withdraw_daily_limit:
            return
        scr, s = self.look()
        if scr is not Screen.PANEL or self._withdrawal_rejected(scr, s):
            return
        p = read_panel(s)  # Do not act on coordinates from an older panel snapshot.
        if not p.withdraw:
            log.warning("面板上找不到「提領」")
            return
        for _ in range(2):  # 剛關掉的提示有時會吃掉第一下
            self.phone.tap(*p.withdraw)
            scr, s = self.wait_for({Screen.WITHDRAW}, 6)
            if scr is not Screen.PANEL:
                break
            if self._withdrawal_rejected(scr, s):
                return
            p = read_panel(s)
            if not p.withdraw:
                return
        if scr is not Screen.WITHDRAW:
            log.warning("提領頁沒打開（目前在%s）", scr.value)
            self.capture("withdraw-not-open", s)
            return
        self.pause(1)
        scr, s = self.look()
        if scr is not Screen.WITHDRAW or self._withdrawal_rejected(scr, s):
            return
        w = read_withdraw(s)
        pending = w.pending
        if pending is None or not math.isfinite(pending) or pending < 1:
            log.warning("讀不到目前可提領餘額，取消本次提領")
            self.capture("withdraw-balance-unknown", s)
            return
        options = [(a, pos) for a, pos in w.options if 0 < a <= pending]
        if not options:
            log.info("待提領 %.2f，沒有能選的提領金額", pending)
            self.phone.tap(*BACK)
            self.pause(2)
            return
        amount, pos = max(options)
        self.phone.tap(*pos)
        self.pause(1)
        scr, s = self.look()
        if scr is not Screen.WITHDRAW or self._withdrawal_rejected(scr, s):
            return
        w = read_withdraw(s)
        if w.pending is None or not math.isfinite(w.pending) or w.pending < amount:
            log.warning("送出前無法確認最新餘額，取消本次提領")
            self.capture("withdraw-balance-unknown", s)
            return
        pending = w.pending
        if not w.submit:
            log.warning("找不到「立即提領」")
            self.phone.tap(*BACK)
            self.pause(2)
            return
        self.capture_once("withdraw-selected", s)
        self.check_messages(s)
        day = self.day
        day.withdrawal_pending = True
        self.store.save()
        self.phone.tap(*w.submit)
        confirmed_once = False
        for i in range(10):
            self.pause(1)
            scr, s = self.look()
            if i == 1:
                self.capture_once("withdraw-submitted", s)
            self.check_messages(s)
            if self._withdrawal_rejected(scr, s):
                break
            confirm = next((ln for ln in s.lines if ln.text == "確認提領"), None)
            if scr is not Screen.WITHDRAW:
                confirm = None
            if confirm and not confirmed_once:
                self.phone.tap(confirm.x, confirm.y)
                confirmed_once = True
                continue
            if scr is Screen.WITHDRAW:
                after = read_withdraw(s).pending
            elif scr is Screen.PANEL:
                after = read_panel(s).pending
            else:
                after = None
            if (scr in (Screen.PANEL, Screen.WITHDRAW)
                    and any("提領成功" in text for text in s.texts()) and after is None):
                after = round(pending - amount, 2)
            if (after is not None and math.isfinite(after) and after >= 0
                    and abs(after - (pending - amount)) <= 0.01):
                day = self.day
                day.withdrawals += 1
                day.withdrawal_pending = False
                day.withdrawn += amount
                day.pending = after
                self.store.save()
                log.info("✔ 提領 %d 蝦幣到「我的蝦幣」（待提領剩 %.2f，今天第 %d 次）",
                         amount, after, day.withdrawals)
                break
        else:
            log.warning("提領結果不明，畫面已存檔")
            self.capture("withdraw-unknown", s)
        if scr is Screen.WITHDRAW:
            self.phone.tap(*BACK)
            self.pause(2)

    # ---------------- 主迴圈 ----------------
    def run(self, hours: float | None = None):
        if hours is not None and (not math.isfinite(hours) or hours <= 0):
            raise ValueError("hours must be finite and positive")
        self.end = time.monotonic() + hours * 3600 if hours is not None else None
        self.phone.stop_check = self.time_up
        log.info("開始：%s", self.store.summary())
        self.phone.toast("蝦皮自動領蝦幣：開始")
        try:
            while not self.time_up():
                try:
                    self.cycle()
                except (StopBot, StateError):
                    raise
                except Exception:
                    log.exception("出錯了，10 秒後繼續")
                    self.pause(10)
        except StopBot as e:
            log.error("停止：%s", e)
            self.phone.toast(f"蝦皮自動領蝦幣：停止（{e}）", TOAST_ERROR)
            return
        log.info("結束：%s", self.store.summary())
        self.phone.toast(f"蝦皮自動領蝦幣：結束｜{self.store.summary()}", TOAST_SUCCESS)

    def report(self):
        """dry-run 用：把現在畫面的判讀結果寫進紀錄檔。"""
        scr, s = self.look()
        log.info("畫面：%s", scr.value)
        if scr is Screen.ROOM:
            log.info("  直播間：%s", read_room(s))
        elif scr is Screen.PANEL:
            log.info("  面板：%s", read_panel(s))
        elif scr is Screen.WITHDRAW:
            log.info("  提領頁：%s", read_withdraw(s))
        for ln in s.lines[:12]:
            log.debug("  (%4.0f,%4.0f) %s", ln.x, ln.y, ln.text)

    def cycle(self):
        day = self.day
        if self.dry:
            self.report()
            self.pause(15)
            return
        if day.coin_limit_hit and day.watch_done:
            self.rest_until_tomorrow()
            return
        purpose, key = self.find_room()
        if not purpose:
            log.info("滑了 %d 間都沒有合適的直播間，1 分鐘後繼續往下找", SCAN_ROOMS)
            self.pause(60)
            self.next_room()
            return
        reason, key = self.stay(purpose, key)
        self.finish_room(key, reason, purpose)

    def finish_room(self, key: str, reason: str, purpose: str):
        day = self.day
        if key:
            room = day.room(key)
            if reason == "watched":
                room["watched"] = True
            if reason in DONE_REASONS or reason.startswith("coin_state"):
                room["done"] = True
            self.store.save()
        claims = day.rooms.get(key, {}).get("claims", 0)
        log.info("離開 %s：%s（這間領了 %d 次）｜%s", key or "?", REASONS.get(reason, reason),
                 claims, self.store.summary())

    def rest_until_tomorrow(self):
        day = self.day
        if not day.final_panel:
            self.go_room()
            self.panel_tasks(final=True)
            day.final_panel = True
            self.store.save()
        log.info("今天能領的都領完了，休息到明天：%s", self.store.summary())
        wake = (reward_now() + timedelta(days=1)).replace(hour=0, minute=3, second=0, microsecond=0)
        while reward_now() < wake and not self.time_up():
            self.pause(min(300, max(1, (wake - reward_now()).total_seconds())))

    def time_up(self) -> bool:
        if self.stop_file and self.stop_file.exists():
            return True
        return self.end is not None and time.monotonic() >= self.end


def setup_logging(log_dir: Path, verbose: bool):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    file = RotatingFileHandler(log_dir / "ios-jb.log", maxBytes=5_000_000, backupCount=3,
                               encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file)


def build_bot(args, cfg, store) -> Bot:
    return Bot(cfg, ZXPhone(args.host, args.port), store)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser(prog="bot.py", description="蝦皮直播自動領蝦幣（iOS jailbreak，ZXTouch）")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "status", "panel", "look", "tap", "swipe", "toast"],
                   help="run：開始自動領（預設）；status：今天的統計；panel：開一次任務獎勵面板；"
                        "look：印出目前畫面的判讀結果；tap/swipe/toast：手動測試")
    p.add_argument("args", nargs="*", type=float, help="tap x y / swipe x1 y1 x2 y2")
    p.add_argument("--host", default=os.environ.get("ZX_HOST", "127.0.0.1"), help="手機 IP（本機跑就是 127.0.0.1）")
    p.add_argument("--port", type=int, default=6000)
    p.add_argument("--hours", type=float, help="跑幾個小時後停止（預設一直跑）")
    p.add_argument("--no-withdraw", action="store_true", help="不要自動提領")
    p.add_argument("--dry-run", action="store_true", help="只讀畫面、不點不滑（第一次跑建議先用這個）")
    p.add_argument("--tap-jitter", type=float, default=5.0,
                   help="點擊座標 ±point（預設 5；蝦皮的按鈕很小，抖太大會點出界）")
    p.add_argument("--stop-file", default=os.environ.get("ZX_STOP_FILE", ""),
                   help="出現這個檔案就停下來（一鍵停止用）")
    p.add_argument("--wait", type=float, help="連不上 tweak 時最多等幾秒（run 預設 1800，其他指令 30）")
    p.add_argument("-v", "--verbose", action="store_true", help="畫面上也顯示除錯訊息")
    args = p.parse_args()
    for name in ("hours", "wait"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            p.error(f"--{name} 必須是有限的正數")
    if not math.isfinite(args.tap_jitter) or not 0 <= args.tap_jitter <= 5:
        p.error("--tap-jitter 必須在 0 到 5 之間，避免點出按鈕範圍")
    if not 1 <= args.port <= 65535:
        p.error("--port 必須在 1 到 65535 之間")
    expected = {"tap": 2, "swipe": 4}.get(args.command, 0)
    if len(args.args) != expected or not all(math.isfinite(v) and v >= 0 for v in args.args):
        p.error(f"{args.command} 需要 {expected} 個有限且非負的座標參數")

    cfg = Config()
    if args.no_withdraw:
        cfg.withdraw = False
    cfg.state_path = str(ROOT / "state-ios.json")  # iOS 跟 Android 是不同的蝦皮帳號，分開記
    cfg.log_dir = str(ROOT / cfg.log_dir)
    setup_logging(Path(cfg.log_dir), args.verbose)

    store = Store(cfg.state_path)
    if args.command == "status":
        print(store.summary())
        return

    phone = ZXPhone(args.host, args.port)
    phone.GIVE_UP = args.wait if args.wait is not None else (30 * 60 if args.command == "run" else 30)
    bot = Bot(cfg, phone, store)
    phone.tap_jitter = args.tap_jitter
    bot.stop_file = Path(args.stop_file) if args.stop_file else None
    bot.dry = phone.dry = args.dry_run
    try:
        with store.exclusive():
            if args.command == "look":
                scr, s = bot.look()
                print(scr.value)
                if scr is Screen.ROOM:
                    print(read_room(s))
                elif scr is Screen.PANEL:
                    print(read_panel(s))
                elif scr is Screen.WITHDRAW:
                    print(read_withdraw(s))
                for ln in s.lines:
                    print(f"  ({ln.x:4.0f},{ln.y:4.0f}) {ln.text}")
            elif args.command == "panel":
                if not args.dry_run:
                    bot.go_room()
                bot.panel_tasks()
                print(store.summary())
            elif args.command == "tap":
                phone.tap(*args.args[:2])
            elif args.command == "swipe":
                phone.swipe(*args.args[:4])
            elif args.command == "toast":
                phone.toast("ZXTouch 測試")
            else:
                bot.run(args.hours)
    except KeyboardInterrupt:
        logging.getLogger("autocoin").info("手動停止：%s", store.summary())
    except (StopBot, StateError) as e:
        logging.getLogger("autocoin").error("停止：%s", e)
        return 1
    finally:
        phone.close()


if __name__ == "__main__":
    sys.exit(main())
