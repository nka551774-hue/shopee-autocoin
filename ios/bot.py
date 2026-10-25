"""iOS 版主流程：往上滑換台找有「直播間蝦幣」的直播間、待著領取，定時開面板領觀看任務與提領。

跟電腦版（autocoin/bot.py）同一套流程，差別是 iOS 讀不到元件樹，畫面全靠 OCR＋顏色判斷，
而且沒有「看更多」列表，只能一間一間滑。轉盤、紅包雨在 iOS 上還沒研究，目前不處理。

手機上要先開 TouchSynthesis（Start UI Automation → Start Server），停在蝦皮的「直播」分頁，
「設定 > 螢幕顯示與亮度 > 自動鎖定」設成「永不」。

用法：
    set TS_HOST=192.168.31.196
    python ios/bot.py run [--hours 3] [--no-withdraw] [-v]
    python ios/bot.py status    今天的統計
    python ios/bot.py panel     開一次任務獎勵面板（領觀看任務、視情況提領）
    python ios/bot.py look      印出目前畫面的判讀結果（除錯用）
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autocoin.config import Config  # noqa: E402
from autocoin.state import StateError, Store, reward_now  # noqa: E402
from ocr import _get_engine  # noqa: E402
from screens import (BACK, Panel, Room, Screen, Shot, detect, money, read_panel,  # noqa: E402
                     read_room, read_withdraw)
from tsclient import TouchSynthesis  # noqa: E402

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


class Phone:
    """TouchSynthesis 連線。斷線就重連；手機上的 App 被殺掉的話要手動再按 Start Server。"""

    GIVE_UP = 30 * 60

    def __init__(self, host: str):
        self.host = host
        self.ts: TouchSynthesis | None = None
        self.deadline: float | None = None

    def _call(self, fn, *, retry: bool = True):
        since = None
        while True:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise StopBot("設定的執行時間已到")
            started = False
            try:
                if self.ts is None:
                    timeout = (min(40, max(.01, self.deadline - time.monotonic()))
                               if self.deadline is not None else 40)
                    self.ts = TouchSynthesis(self.host, timeout=timeout)
                    if since:
                        log.info("TouchSynthesis 連上了")
                started = True
                if self.deadline is not None:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise StopBot("設定的執行時間已到")
                    self.ts.timeout = min(40, remaining)
                return fn(self.ts)
            except OSError as e:  # 連線被拒、逾時、斷線（ConnectionError 也是 OSError）
                if self.ts:
                    self.ts.close()
                    self.ts = None
                if not retry:
                    reason = "觸控指令的回覆遺失，結果不明" if started else "無法連線，畫面座標可能已過期"
                    raise StopBot(f"{reason}，停止以避免重複或錯誤操作") from e
                if since is None:
                    since = time.monotonic()
                    log.warning("連不到 TouchSynthesis（%s）：手機上的 App 還開著嗎？Server 有啟動嗎？", e)
                if time.monotonic() - since > self.GIVE_UP:
                    raise StopBot("TouchSynthesis 一直連不上") from e
                time.sleep(min(10, max(0, self.deadline - time.monotonic()))
                           if self.deadline is not None else 10)
            except (RuntimeError, ValueError) as e:
                self.close()
                raise StopBot(f"TouchSynthesis 回覆無效或操作失敗：{e}") from e

    def shot(self) -> Shot:
        data = self._call(lambda ts: ts.screenshot())
        try:
            return Shot(data)
        except (OSError, ValueError) as exc:
            raise StopBot(f"截圖無法安全判讀：{exc}") from exc

    def tap(self, x: float, y: float):
        self._call(lambda ts: ts.tap(x, y), retry=False)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.4):
        self._call(lambda ts: ts.swipe(x1, y1, x2, y2, duration), retry=False)

    def close(self):
        if self.ts:
            self.ts.close()
            self.ts = None


class Bot:
    def __init__(self, cfg: Config, phone: Phone, store: Store):
        self.cfg = cfg
        self.phone = phone
        self.store = store
        self.captures = Path(cfg.log_dir) / "captures-ios"
        self.captures.mkdir(parents=True, exist_ok=True)
        self.end: float | None = None  # run(hours=…) 的結束時間
        self.first: set[str] = set()   # 還沒研究過的畫面，每次執行各存一張

    @property
    def day(self):
        return self.store.day

    # ---------------- 基本工具 ----------------
    def ensure_running(self):
        if self.time_up():
            raise StopBot("設定的執行時間已到")

    def pause(self, seconds: float):
        self.ensure_running()
        if self.end is not None:
            seconds = min(seconds, max(0, self.end - time.monotonic()))
        time.sleep(seconds)
        self.ensure_running()

    def snapshot(self) -> Shot:
        self.ensure_running()
        s = self.phone.shot()
        self.check_risk(s)
        return s

    def look(self) -> tuple[Screen, Shot]:
        s = self.snapshot()
        scr = detect(s)
        # 系統的評分邀請（「喜歡「蝦皮購物」嗎？」）會蓋住整個畫面，提領完就跳出來過
        later = next((ln for ln in s.lines if ln.text in POPUP_DISMISS), None)
        if later and scr is not Screen.UNKNOWN:
            log.info("關掉彈出視窗（%s）", later.text)
            self.phone.tap(later.x, later.y)
            self.pause(1.5)
            s = self.snapshot()
        return detect(s), s

    def wait_for(self, wanted: set[Screen], timeout: float = 10) -> tuple[Screen, Shot]:
        deadline = time.monotonic() + timeout
        while True:
            scr, s = self.look()
            if scr in wanted or time.monotonic() >= deadline:
                return scr, s
            self.pause(0.7)

    def capture(self, tag: str, s: Shot | None = None):
        """存下目前畫面（截圖＋OCR 文字），之後分析沒看過的狀態用。"""
        safe_tag = re.sub(r"[^\w.-]+", "_", tag)
        base = self.captures / f"{reward_now():%Y%m%d-%H%M%S-%f}-{safe_tag}"
        try:
            s = s or self.phone.shot()
            Path(f"{base}.jpg").write_bytes(s.data)
            Path(f"{base}.txt").write_text(
                "\n".join(f"({ln.x:4.0f},{ln.y:4.0f}) {ln.text}" for ln in s.lines), encoding="utf-8")
        except Exception:
            log.debug("存畫面失敗", exc_info=True)
        try:
            old_captures = sorted(self.captures.glob("*.jpg"))[:-300]
        except OSError:
            log.debug("列出舊畫面失敗", exc_info=True)
            old_captures = []
        for old in old_captures:
            for path in (old, old.with_suffix(".txt")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # Diagnostics are best-effort.  In particular, a locked
                    # old capture must never mask a risk StopBot.
                    log.debug("清理舊畫面失敗", exc_info=True)
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

        self.check_risk(s)
        if t := hit(MSG_DAILY_LIMIT):
            if not self.day.coin_limit_hit:
                log.info("直播間蝦幣今天的次數用完了（%s）", t)
                self.day.coin_limit_hit = True
                self.store.save()
            return "daily_limit"
        if hit(MSG_ROOM_DONE):
            return "room_done"
        return ""

    def check_risk(self, s: Shot):
        text = next((t for t in s.texts() if any(k in t for k in MSG_RISK)), None)
        if text:
            self.capture("risk", s)
            raise StopBot(f"蝦皮回報「{text}」，為了帳號安全先停止")

    def known(self, name: str) -> str:
        """對到今天已經記錄過的直播間就沿用原本的名字。"""
        return next((k for k in self.day.rooms if same(k, name)), name)

    def done(self, key: str) -> bool:
        return self.day.room_done(key)

    # ---------------- 導航 ----------------
    def back_to_room(self, scr: Screen, s: Shot) -> bool:
        """收掉蓋在直播間上面的頁面（面板、提領頁）。回不到直播間就回傳 False。

        認不得的畫面先多看幾眼（OCR 偶爾整頁漏讀）；仍無法辨識時停止導航，
        因為畫面可能已經切換至其他 App。"""
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
        """回到直播間。回不去就只等、不亂點，直到有人把畫面切回蝦皮的直播間。"""
        scr, s = self.look()
        if scr is Screen.ROOM:
            return s
        if self.back_to_room(scr, s):
            return self.snapshot()
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
        day = self.day
        r = Room()
        for i in range(SCAN_ROOMS):
            if i:
                self.next_room()
            r = self.read_current()
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
            if elapsed > cfg.max_room_minutes * 60:
                return "timeout", key
            if purpose == "watch":
                if r.coin.present and not day.coin_limit_hit:
                    log.info("這間出現直播間蝦幣，改成領蝦幣")
                    purpose = "coins"
                    continue
                if watch_full or elapsed > cfg.watch_room_minutes * 60:
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
            self.pause(min(max(secs - 3, 1), 20))

    def claim(self, r: Room, key: str):
        """按「直播間蝦幣」的領取，確認有成功。"""
        self.ensure_running()
        if (not r.coin.claimable or r.coin.button is None or r.claimed
                or self.day.coin_limit_hit or self.day.coin_claims >= 100):
            return
        self.phone.tap(*r.coin.button)
        amount = None
        deadline = time.monotonic() + 6
        s = None
        while time.monotonic() < deadline:
            s = self.snapshot()
            got = read_room(s).claimed
            if "成功" in got:
                amount = money(got)
                if amount is None:
                    log.warning("領取成功但金額沒讀到（%s）", got)
                    amount = 0.0
                break
            verdict = self.check_messages(s)
            if verdict:
                log.info("領取沒成功：%s", REASONS.get(verdict, verdict))
                return
            self.pause(0.25)
        if amount is None:
            self.capture("claim-unknown", s)
            raise StopBot("直播間領取結果不明，停止以避免重複操作；未計入成功")
        day = self.day
        room = day.room(key)
        room["claims"] += 1
        room["coins"] = round(room["coins"] + amount, 2)
        day.coin_claims += 1
        if day.coin_claims >= 100:
            day.coin_limit_hit = True
        day.coin_total = round(day.coin_total + amount, 2)
        self.store.save()
        log.info("✔ 直播間蝦幣 +%.2f｜%s 第 %d 次｜今天 %d 次，共 %.2f",
                 amount, key or "?", room["claims"], day.coin_claims, day.coin_total)
        # 成功提示出來後按鈕還會停在「領取」4 秒左右；等它換掉再走，免得又按一次
        # （那個位置按下去會打開任務獎勵面板）
        for _ in range(10):
            self.pause(0.8)
            if not read_room(self.snapshot()).coin.claimable:
                break

    # ---------------- 任務獎勵面板 ----------------
    def panel_due(self) -> bool:
        day = self.day
        if time.time() - day.last_panel > self.cfg.panel_every_minutes * 60:
            return True
        # 觀看任務的蝦幣要當天領，23:40 再開一次
        late = reward_now().replace(hour=23, minute=40, second=0, microsecond=0)
        return reward_now() >= late and day.last_panel < late.timestamp()

    def panel_tasks(self, final: bool = False):
        """開任務獎勵面板：把觀看任務的蝦幣領進待提領，需要時提領。
        直播間會縮成右下角浮窗，倒數不受影響。"""
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
        s = self.snapshot()
        if detect(s) is not Screen.PANEL:
            raise StopBot("任務面板已改變，停止使用舊畫面")
        p = read_panel(s)
        if p.claim:
            before = p.pending
            previous_notice = s.find("已發放至")
            self.phone.tap(*p.claim)
            self.pause(1.5)
            s = self.snapshot()
            self.capture_once("panel-claimed", s)
            notice = s.find("已發放至") if previous_notice is None else None
            # 領完會跳出「0.21 蝦幣已發放至您的"待提領蝦幣數"」，提示還在的時候按別的都沒反應
            for _ in range(8):
                if not s.find("已發放至"):
                    break
                self.pause(1)
                s = self.snapshot()
            if detect(s) is not Screen.PANEL:
                raise StopBot("觀看任務領取後畫面已改變，結果待確認")
            after = read_panel(s)
            increase = (round(after.pending - before, 2)
                        if before is not None and after.pending is not None else None)
            if not notice and (increase is None or increase <= 0):
                self.capture("panel-claim-unknown", s)
                raise StopBot("觀看任務領取結果不明；未計入成功，請檢查帳戶")
            amount = increase if increase is not None and increase > 0 else money(notice.text)
            if amount is None and notice:
                amount = p.claim_amount
            day = self.day
            day.watch_total = round(day.watch_total + (amount or 0.0), 2)
            log.info("✔ 觀看任務 +%s（領進待提領）", f"{amount:.2f}" if amount is not None else "?")
            p = after
        # 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤；只剩一個 90 的話分不出是哪個，就不更新
        day = self.day
        day.last_panel = time.time()
        if len(p.minutes) >= 2 or (p.minutes and p.minutes[0] != 90):
            day.watch_minutes = p.minutes[0]
        if p.done or day.watch_minutes >= 90:
            day.watch_done = True
        if p.pending is not None:
            day.pending = p.pending
        self.store.save()
        log.info("待提領 %.2f｜觀看任務 %d/90 分鐘", day.pending, day.watch_minutes)
        if p.pending is not None and self.should_withdraw(p.pending, final):
            self.withdraw(p)
        self.back_to_room(*self.look())

    def should_withdraw(self, pending: float, final: bool) -> bool:
        cfg, day = self.cfg, self.day
        if (not cfg.withdraw or day.withdrawal_pending
                or day.withdrawals >= cfg.withdraw_daily_limit
                or not math.isfinite(pending) or pending < 1):
            return False
        late = reward_now().hour == 23 and reward_now().minute >= 40
        return pending >= cfg.withdraw_min or final or late

    def withdraw(self, p: Panel):
        """在面板按「提領」，選不超過待提領的最大面額，送出並確認待提領有變少。"""
        day = self.day
        if day.withdrawal_pending:
            raise StopBot("先前提領仍待確認，請核對帳戶後再啟用提領")
        if not self.cfg.withdraw or day.withdrawals >= self.cfg.withdraw_daily_limit:
            return
        if not p.withdraw:
            log.warning("面板上找不到「提領」")
            return
        for _ in range(2):  # 剛關掉的提示有時會吃掉第一下
            self.phone.tap(*p.withdraw)
            scr, s = self.wait_for({Screen.WITHDRAW}, 6)
            if scr is not Screen.PANEL:
                break
        if scr is not Screen.WITHDRAW:
            log.warning("提領頁沒打開（目前在%s）", scr.value)
            self.capture("withdraw-not-open", s)
            return
        self.pause(1)
        s = self.snapshot()
        if detect(s) is not Screen.WITHDRAW:
            raise StopBot("提領頁已改變，取消選擇金額")
        w = read_withdraw(s)
        pending = w.pending
        if pending is None or not math.isfinite(pending) or pending < 0:
            raise StopBot("無法讀取目前待提領餘額，停止提領")
        options = [(a, pos) for a, pos in w.options if 0 < a <= pending]
        if not options:
            log.info("待提領 %.2f，沒有能選的提領金額", pending)
            self.phone.tap(*BACK)
            self.pause(2)
            return
        amount, pos = max(options)
        self.phone.tap(*pos)
        self.pause(1)
        s = self.snapshot()
        w = read_withdraw(s)
        if (detect(s) is not Screen.WITHDRAW or w.pending is None or not math.isfinite(w.pending)
                or abs(w.pending - pending) > .009
                or not any(option == amount for option, _ in w.options)):
            raise StopBot("提領頁、餘額或面額已改變，取消送出，請重新確認畫面")
        if not w.submit:
            log.warning("找不到「立即提領」")
            self.phone.tap(*BACK)
            self.pause(2)
            return
        self.capture_once("withdraw-selected", s)
        day = self.day  # Navigation/selection can cross midnight.
        if (not self.cfg.withdraw or day.withdrawal_pending
                or day.withdrawals >= self.cfg.withdraw_daily_limit):
            raise StopBot("目前的提領設定或額度不允許送出")
        day.withdrawal_pending = True
        self.store.save()  # Journal before sending, including disconnect/interrupt outcomes.
        self.ensure_running()
        self.phone.tap(*w.submit)
        confirmed = False
        for i in range(10):
            self.pause(1)
            scr, s = self.look()
            if i == 1:
                self.capture_once("withdraw-submitted", s)
            # 每天只能提領 3 次；手動提領過的話程式的計數會少算，看到上限提示就當作今天用完
            # （只看提領頁和面板，直播間的宣傳圖上常有「上限」之類的字）
            limit = next((ln.text for ln in s.lines
                          if scr in (Screen.WITHDRAW, Screen.PANEL) and "提領" in ln.text
                          and any(marker in ln.text for marker in ("已達", "已超過", "已用完", "不能再"))), None)
            if limit:
                log.info("提領被拒：%s，今天不再提領", limit)
                day = self.day
                day.withdrawals = self.cfg.withdraw_daily_limit
                day.withdrawal_pending = False
                self.store.save()
                break
            confirm = next((ln for ln in s.lines if ln.text == "確認提領"), None)
            if confirm and not confirmed and scr is Screen.WITHDRAW:
                self.phone.tap(confirm.x, confirm.y)
                confirmed = True
                continue
            if scr is Screen.WITHDRAW:
                after = read_withdraw(s).pending
            elif scr is Screen.PANEL:
                after = read_panel(s).pending
            else:
                after = None
            if scr in (Screen.WITHDRAW, Screen.PANEL) and s.find("提領成功") and after is None:
                after = round(pending - amount, 2)
            if after is not None and abs(after - (pending - amount)) <= 0.01:
                # An acknowledgement may arrive after midnight. Counting it today
                # is conservative and clears the marker on the live Day object.
                day = self.day
                day.withdrawals += 1
                day.withdrawn = round(day.withdrawn + amount, 2)
                day.pending = after
                day.withdrawal_pending = False
                self.store.save()
                log.info("✔ 提領 %d 蝦幣到「我的蝦幣」（待提領剩 %.2f，今天第 %d 次）",
                         amount, after, day.withdrawals)
                break
        else:
            log.warning("提領結果不明，畫面已存檔")
            self.capture("withdraw-unknown", s)
            raise StopBot("提領結果不明，已保留待確認標記，停止以避免重複提領")
        if scr is Screen.WITHDRAW:
            self.phone.tap(*BACK)
            self.pause(2)

    # ---------------- 主迴圈 ----------------
    def run(self, hours: float | None = None):
        if hours is not None and (not math.isfinite(hours) or hours <= 0):
            raise ValueError("hours must be finite and positive")
        self.end = time.monotonic() + hours * 3600 if hours is not None else None
        self.phone.deadline = self.end
        log.info("開始：%s", self.store.summary())
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
            if not self.time_up():
                log.error("停止：%s", e)
                raise
        log.info("結束：%s", self.store.summary())

    def cycle(self):
        day = self.day
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
        completed_date = day.date
        if not day.final_panel:
            self.go_room()
            self.panel_tasks(final=True)
            day = self.day
            if day.date != completed_date:
                return
            day.final_panel = True
            self.store.save()
        log.info("今天能領的都領完了，休息到明天：%s", self.store.summary())
        wake = datetime.fromisoformat(f"{completed_date}T00:03:00+08:00") + timedelta(days=1)
        while reward_now() < wake and not self.time_up():
            self.pause(min(300, max(1, (wake - reward_now()).total_seconds())))

    def time_up(self) -> bool:
        return self.end is not None and time.monotonic() >= self.end


def setup_logging(log_dir: Path, verbose: bool):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    file = RotatingFileHandler(log_dir / "ios.log", maxBytes=5_000_000, backupCount=3,
                               encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("PIL", "asyncio"):  # OCR 每次都會用 asyncio.run，會一直印 Using proactor
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    p = argparse.ArgumentParser(prog="ios/bot.py", description="蝦皮直播自動領蝦幣（iOS，TouchSynthesis）")
    p.add_argument("command", nargs="?", default="run", choices=["run", "status", "panel", "look"],
                   help="run：開始自動領（預設）；status：今天的統計；"
                        "panel：開一次任務獎勵面板；look：印出目前畫面的判讀結果")
    p.add_argument("--host", default=os.environ.get("TS_HOST", "127.0.0.1"),
                   help="手機的 IP（預設讀環境變數 TS_HOST）")
    p.add_argument("--hours", type=float, help="跑幾個小時後停止（預設一直跑）")
    p.add_argument("--no-withdraw", action="store_true", help="不要自動提領")
    p.add_argument("-v", "--verbose", action="store_true", help="畫面上也顯示除錯訊息")
    args = p.parse_args()
    if args.hours is not None and (not math.isfinite(args.hours) or args.hours <= 0):
        p.error("--hours 必須是大於零的有限數值")

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    cfg = Config()
    if args.no_withdraw:
        cfg.withdraw = False
    cfg.state_path = str(ROOT / "state-ios.json")  # iOS 跟 Android 是不同的蝦皮帳號，分開記
    cfg.log_dir = str(ROOT / cfg.log_dir)
    setup_logging(Path(cfg.log_dir), args.verbose)

    try:
        store = Store(cfg.state_path)
    except StateError as e:
        p.exit(1, f"狀態檔錯誤：{e}\n")
    if args.command == "status":
        print(store.summary())
        return

    try:
        _get_engine()
    except RuntimeError as exc:
        p.exit(1, f"OCR 初始化失敗：{exc}\n")

    try:
        with store.exclusive():
            phone = Phone(args.host)
            bot = Bot(cfg, phone, store)
            try:
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
                    bot.go_room()
                    bot.panel_tasks()
                    print(store.summary())
                else:
                    bot.run(args.hours)
            except KeyboardInterrupt:
                logging.getLogger("autocoin").info("手動停止：%s", store.summary())
            except StopBot as e:
                logging.getLogger("autocoin").error("停止：%s", e)
                return 1
            finally:
                phone.close()
    except StateError as exc:
        p.exit(1, f"State error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
