"""主流程：挑有蝦幣的直播間、待著領取、轉盤、定時開面板領觀看任務與提領。"""
from __future__ import annotations

import logging
import math
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .device import Device, UnsafeAction
from .screens import Card, Room, Screen, detect, inside, num, number_near, read_cards, read_room
from .state import StateError, Store, reward_now
from .ui import FREE_TEXT_IDS, PKG, Snapshot

log = logging.getLogger("autocoin")

# 等待時間的隨機抖動比例。固定節奏（每次都剛好等 20 秒、10.5 分鐘準時換台）
# 是風控最容易抓的特徵，所以所有 sleep 都過一層抖動。
JITTER = 0.18

_sleep = time.sleep  # doze 自己用；避免被下面的整批取代誤傷


def set_jitter(pct: float) -> None:
    """讓 Config 覆蓋預設的抖動比例（0 = 完全固定，不建議）。"""
    global JITTER
    JITTER = max(0.0, pct)


def doze(sec: float, pct: float | None = None) -> None:
    """帶抖動的 sleep。注意不要叫 nap：bot.py 裡面 nap 是區域變數。"""
    p = JITTER if pct is None else pct
    _sleep(max(0.05, sec * random.uniform(1 - p, 1 + p)))


def wobble(base: float, pct: float = 0.18) -> float:
    """回傳一個帶抖動的數值（給門檻比較用，不做 sleep）。"""
    return base * random.uniform(1 - pct, 1 + pct)

# App 內的提示文字（取自 APK 的 zh-rTW 字串）
MSG_RISK = ("不符合領取此獎勵資格", "偵測到風險活動", "您已達多裝置使用上限", "已達裝置數限制")
MSG_DAILY_LIMIT = ("已超過今天領取的次數限制", "您已達領取上限")
MSG_ROOM_DONE = ("直播蝦幣已全部發放",)
MSG_WHEEL_LIMIT = ("達到每日參與上限", "達到每日上限")

REASONS = {
    "no_coins": "沒有直播間蝦幣",
    "room_done": "這場的蝦幣發完了",
    "daily_limit": "今天的直播間蝦幣次數用完了",
    "timeout": "待太久了",
    "watched": "觀看任務在這間已累計 10 分鐘",
    "wheel_checked": "轉盤處理完了",
    "lost": "畫面跑掉了",
    "switched": "直播間被換掉了",
}
# 因為這些原因離開的直播間，今天就不再進去
DONE_REASONS = {"no_coins", "room_done", "timeout", "watched", "wheel_checked"}


class StopBot(Exception):
    """遇到不該繼續的狀況（例如蝦皮回報帳號有風險），整個程式停下來。"""


class Bot:
    def __init__(self, cfg: Config, dev: Device, store: Store):
        self.cfg = cfg
        self.dev = dev
        self.store = store
        self.captures = Path(cfg.log_dir) / "captures"
        self.captures.mkdir(parents=True, exist_ok=True)
        self.home_pkg = dev.home_package()
        self.rain_seen: set[tuple[str, str, str]] = set()
        self.end: float | None = None  # run(hours=…) 的結束時間
        self._cache_day = self.store.day.date
        self._caps: dict[tuple[str, float], float] = {}  # 每個直播間抽一次的上限
        self._panel_key: float = -1.0  # 面板間隔的抖動只在每個週期抽一次
        self._panel_gap: float = 0.0
        set_jitter(cfg.jitter)
        dev.tap_jitter_px = cfg.tap_jitter_px
        dev.before_action = self.check_running

    def check_running(self):
        if self.time_up():
            raise StopBot("已達指定執行時間")

    def pause(self, seconds: float, pct: float | None = None):
        self.check_running()
        spread = self.cfg.jitter if pct is None else pct
        delay = max(0.05, seconds * random.uniform(1 - spread, 1 + spread))
        if self.end is not None:
            delay = min(delay, max(0, self.end - time.monotonic()))
        _sleep(delay)
        self.check_running()

    def _room_cap(self, key: str, base: float, pct: float = 0.25) -> float:
        """每個直播間用不同的上限，而且只往上抖（往下抖會讓觀看任務做不完）。"""
        k = (key, base)
        if k not in self._caps:
            self._caps[k] = base * random.uniform(1.0, 1.0 + pct)
        return self._caps[k]

    @property
    def day(self):
        day = self.store.day
        if day.date != self._cache_day:
            self._caps.clear()
            self.rain_seen.clear()
            self._panel_key = -1.0
            self._cache_day = day.date
        return day

    # ---------------- 基本工具 ----------------
    def snapshot(self) -> Snapshot:
        self.check_running()
        s = self.dev.snapshot()
        # 節點已依套件過濾；前景查詢暫時失敗時也不可略過風險提示。
        self.check_messages(s)
        self.check_running()
        return s

    def look(self) -> tuple[Screen, Snapshot]:
        s = self.snapshot()
        return detect(s), s

    def wait_for(self, wanted: set[Screen], timeout: float = 10) -> tuple[Screen, Snapshot]:
        deadline = time.monotonic() + timeout
        while True:
            self.check_running()
            scr, s = self.look()
            if scr in wanted or time.monotonic() >= deadline:
                return scr, s
            self.pause(0.7)

    def capture(self, tag: str, s: Snapshot | None = None):
        """存下目前畫面（截圖＋UI 樹），之後分析沒看過的狀態用。"""
        safe_tag = re.sub(r"[^\w.-]+", "_", tag)
        base = self.captures / f"{reward_now():%Y%m%d-%H%M%S-%f}-{safe_tag}"
        try:
            self.dev.screenshot(f"{base}.png")
            s = s or self.dev.snapshot()
            Path(f"{base}.xml").write_text(s.xml, encoding="utf-8")
        except Exception:
            log.debug("存畫面失敗", exc_info=True)
        stems = sorted({old.stem for pattern in ("*.png", "*.xml")
                        for old in self.captures.glob(pattern)})
        for stem in stems[:-300]:
            for suffix in (".png", ".xml"):
                try:
                    (self.captures / (stem + suffix)).unlink(missing_ok=True)
                except OSError:
                    log.debug("清理舊畫面失敗", exc_info=True)
        log.debug("已存畫面 %s", base.name)

    def check_messages(self, s: Snapshot, toast: str = "") -> str:
        """比對 App 的提示文字。遇到風控就停止；回傳 daily_limit / room_done / 空字串。"""
        texts = s.texts_excluding(FREE_TEXT_IDS) + ([toast] if toast else [])

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

    # ---------------- 讓蝦皮待在前景 ----------------
    def ensure_app(self):
        waiting = ""
        while True:
            if not self.dev.screen_on():
                log.info("螢幕關著，喚醒")
                self.dev.wake()
                self.pause(2)
            if self.dev.locked():
                if waiting != "locked":
                    log.warning("手機鎖定中，等解鎖…（建議開「充電時保持螢幕常亮」）")
                    waiting = "locked"
                self.pause(30)
                continue
            pkg = self.dev.current().get("package", "")
            if pkg == PKG:
                if waiting:
                    log.info("蝦皮回到前景，繼續")
                return
            if not pkg:
                log.warning("無法確認前景 App，等待裝置恢復")
                self.pause(5)
                continue
            if pkg == self.home_pkg:
                log.info("蝦皮不在前景，重新開啟")
                self.dev.launch()
                self.pause(8)
                continue
            if pkg in ("android", "com.android.intentresolver"):  # 分享選單之類的系統視窗
                self.pause(5)
                continue
            if waiting != pkg:
                log.info("前景是 %s，可能有人在用手機，等它結束…", pkg)
                waiting = pkg
            self.pause(30)

    # ---------------- 導航 ----------------
    def close_overlay(self, s: Snapshot):
        btn = s.id("ic_close")
        if btn:
            self.dev.tap_node(btn)
        else:
            self.dev.back()

    def close_explore(self, s: Snapshot):
        btn = s.id("collapse_icon")
        if btn:
            self.dev.tap_node(btn)
        else:
            self.dev.back()

    def go_live(self) -> Snapshot:
        """不管現在在哪，回到某個直播間。"""
        for _ in range(2):
            for _ in range(10):
                scr, s = self.look()
                if scr is Screen.ROOM:
                    return s
                log.debug("go_live：目前在%s", scr.value)
                if scr is Screen.LIVE_PREVIEW:
                    self.dev.swipe_reference(540, 1800, 540, 600, 0.3)
                elif scr is Screen.VIDEO_TAB:
                    self.dev.tap_node(s.desc("click 1 tab"))
                elif scr is Screen.HOME:
                    self.dev.tap_node(s.desc("tab_bar_button_video_and_live"))
                elif scr is Screen.EXPLORE:
                    self.close_explore(s)
                elif scr in (Screen.WHEEL, Screen.WHEEL_RESULT):
                    self.close_overlay(s)
                elif scr is Screen.FOLLOW_DIALOG:
                    self.dev.tap_node(s.id("tv_cancel"))
                elif scr is Screen.OTHER_APP:
                    self.ensure_app()
                else:
                    self.dev.back()
                self.pause(2.5)
            log.warning("一直回不到直播間，重新啟動蝦皮")
            self.capture("lost", s)
            self.dev.restart_app()
            self.pause(10)
            self.ensure_app()
        raise RuntimeError("回不到直播間")

    def back_to_room(self, scr: Screen, s: Snapshot) -> bool:
        """收掉蓋在直播間上面的東西（轉盤、面板、提領頁…）。回不到直播間就回傳 False。"""
        for _ in range(4):
            if scr is Screen.ROOM:
                return True
            if scr in (Screen.WHEEL, Screen.WHEEL_RESULT):
                self.close_overlay(s)
            elif scr is Screen.FOLLOW_DIALOG:
                self.dev.tap_node(s.id("tv_cancel"))
            elif scr in (Screen.PANEL, Screen.WITHDRAW, Screen.WEB, Screen.UNKNOWN):
                if scr is Screen.UNKNOWN:
                    self.capture("unknown", s)
                self.dev.back()
            elif scr is Screen.OTHER_APP:
                self.ensure_app()
            else:
                return False
            self.pause(2)
            scr, s = self.look()
        return scr is Screen.ROOM

    def open_explore(self) -> Snapshot | None:
        """打開直播間右上角的「看更多」列表。"""
        for _ in range(3):
            scr, s = self.look()
            if scr is not Screen.EXPLORE:
                if scr is not Screen.ROOM:
                    s = self.go_live()
                r = read_room(s)
                if r.explore:
                    self.dev.tap(*r.explore)
                elif r.close:  # 沒有「看更多」的獨立直播間，先關掉
                    self.dev.tap(*r.close)
                    self.pause(2.5)
                    continue
                else:
                    log.warning("直播間沒有「看更多」")
                    self.capture("no-explore", s)
                    return None
                scr, s = self.wait_for({Screen.EXPLORE}, 8)
                if scr is not Screen.EXPLORE:
                    continue
                self.pause(2)  # 廣告彈窗通常晚一點才出來
                s = self.snapshot()
            if self.dismiss_popup(s):
                self.pause(1)
                s = self.snapshot()
            return s
        return None

    def dismiss_popup(self, s: Snapshot) -> bool:
        """「看更多」列表上偶爾蓋著廣告：一張大圖＋下面一個小關閉鈕，兩者都沒有文字或 ID。"""
        bare = [n for n in s.nodes if n.clickable and not (n.rid or n.text or n.desc)]
        big = next((n for n in bare if n.width > 600 and n.height > 700), None)
        if not big:
            return False
        close = next((n for n in bare if 60 <= n.width <= 150 and 60 <= n.height <= 150
                      and n.bounds[1] >= big.bounds[3] - 30), None)
        if not close:
            return False
        log.info("關掉廣告彈窗")
        self.dev.tap_node(close)
        return True

    def scroll_top(self, s: Snapshot) -> Snapshot:
        for _ in range(self.cfg.explore_pages + 3):
            if any(c.rid == "live_card_cover_0" for c in read_cards(s)):
                break
            self.dev.swipe_reference(590, 700, 590, 2000, 0.3)
            self.pause(0.8)
            s = self.snapshot()
        return s

    def scan(self, s: Snapshot) -> dict[str, Card]:
        """往下捲「看更多」列表，收集卡片（rid → Card）。"""
        cards: dict[str, Card] = {}
        for page in range(self.cfg.explore_pages + 1):
            new = 0
            for c in read_cards(s):
                if not c.key:
                    continue
                if c.rid not in cards:
                    new += 1
                # 卡片露出越多資訊越完整（標題、直播主）
                if c.rid not in cards or c.height > cards[c.rid].height:
                    cards[c.rid] = c
            if page == self.cfg.explore_pages or (page and not new):
                break
            self.dev.swipe_reference(590, 1900, 590, 700, 0.5)
            self.pause(1.2)
            s = self.snapshot()
        return cards

    def choose(self, cards: dict[str, Card]) -> tuple[Card | None, str]:
        """挑下一間：還能轉盤就先去「抽獎」的；再來挑「最多X」最高的；都沒有就去累積觀看任務。"""
        day = self.day

        def flag(c: Card, name: str) -> bool:
            return day.rooms.get(c.key, {}).get(name, False)

        fresh = [c for c in cards.values() if not day.room_done(c.key)]
        if not day.wheel_done:
            lottery = [c for c in fresh if c.lottery and not flag(c, "wheel")]
            if lottery:
                return lottery[0], ("wheel" if day.coin_limit_hit else "coins")
        if not day.coin_limit_hit:
            rich = [c for c in fresh if (c.max_coins or 0) >= self.cfg.min_room_coins]
            if rich:
                return max(rich, key=lambda c: c.max_coins), "coins"
        if not day.watch_done:
            unwatched = [c for c in fresh if not flag(c, "watched")]
            if unwatched:
                return unwatched[0], "watch"
        return None, ""

    def enter(self, card: Card) -> Snapshot | None:
        """捲回那張卡片並點進去。"""
        for _ in range(self.cfg.explore_pages * 2 + 2):
            s = self.snapshot()
            hit = next((c for c in read_cards(s) if c.key == card.key and c.height >= 300), None)
            if hit:
                self.dev.tap(*hit.center)
                scr, s = self.wait_for({Screen.ROOM}, 15)
                return s if scr is Screen.ROOM else None
            self.dev.swipe_reference(590, 800, 590, 1700, 0.5)  # 往回捲
            self.pause(1)
        log.warning("列表裡找不到 %s", card.key)
        return None

    def pick(self) -> tuple[Card | None, str]:
        s = self.open_explore()
        if s is None:
            return None, ""
        cards = self.scan(self.scroll_top(s))
        card, purpose = self.choose(cards)
        if not card:
            self.close_explore(self.snapshot())
            return None, ""
        badge = f"最多 {card.max_coins:g}" if card.max_coins else ("抽獎" if card.lottery else "-")
        log.info("→ 進入 %s（%s，%s 人在看）目的：%s", card.key, badge, card.viewers, purpose)
        if self.enter(card) is None:
            return None, ""
        return card, purpose

    def leave(self):
        """離開目前的直播間。獨立直播間按 X；首頁直播分頁裡那間不用關，下次直接開「看更多」。"""
        scr, s = self.look()
        if scr is Screen.ROOM:
            r = read_room(s)
            if r.close:
                self.dev.tap(*r.close)
                self.pause(2.5)

    # ---------------- 待在直播間 ----------------
    def stay(self, purpose: str) -> tuple[str, str]:
        """待在目前的直播間直到該走了。purpose：coins / wheel / watch。回傳（原因, 直播主）。"""
        cfg = self.cfg
        start = time.monotonic()
        coin_missing = odd_state = watch_stuck = None
        wheel_tries = 0
        wheel_settled = False  # 這間的轉盤已經看到結果了
        last_wheel = ""
        key = ""
        while True:
            scr, s = self.look()
            if scr is not Screen.ROOM:
                if not self.back_to_room(scr, s):
                    return "lost", key
                continue
            r = read_room(s)
            if key and r.key and r.key != key:
                return "switched", key
            if not key:
                log.debug("進房：%s｜直播間蝦幣 %s %s｜轉盤 %r｜紅包雨 %s",
                          r.key, r.coin.coin, r.coin.state or "-", r.wheel, r.rains or "-")
            elif r.wheel != last_wheel:
                log.debug("轉盤狀態：%r → %r", last_wheel, r.wheel)
            last_wheel = r.wheel
            key = key or r.key
            day = self.day

            verdict = self.check_messages(s)
            if verdict == "room_done" or (verdict == "daily_limit" and purpose == "coins"):
                return verdict, key

            # 幸運轉盤：每間一次，每天 10 次。「已參加」只代表關注過，不代表轉過
            if (r.wheel in ("參加", "已參加") and not wheel_settled and not day.wheel_done
                    and wheel_tries < 2):
                wheel_tries += 1
                wheel_settled = self.play_wheel(r)
                continue
            if purpose == "wheel":
                return "wheel_checked", key

            if r.rains:
                self.observe_rain(r, s)

            # 觀看任務到點會自己領，卡在「領取」太久才幫忙按
            if r.watch.claimable:
                watch_stuck = watch_stuck or time.monotonic()
                if time.monotonic() - watch_stuck > 15:
                    self.dev.tap(*r.watch.button)
                    watch_stuck = None
            else:
                watch_stuck = None
            if r.watch.state == "明天再來" and not day.watch_done:
                log.info("觀看任務今天做完了")
                day.watch_done = True
                self.store.save()

            if self.panel_due():
                self.panel_tasks()
                continue

            elapsed = time.monotonic() - start
            if elapsed > self._room_cap(key, cfg.max_room_minutes) * 60:
                return "timeout", key
            if purpose == "watch":
                if elapsed > self._room_cap(key, cfg.watch_room_minutes) * 60:
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
                    self.claim(r)
                continue
            secs = r.coin.seconds
            if secs is None:
                odd_state = odd_state or time.monotonic()
                if time.monotonic() - odd_state > 30:
                    self.capture(f"coin-{r.coin.state}", s)
                    return f"coin_state:{r.coin.state}", key
                self.pause(3)
                continue
            odd_state = None
            nap = secs - random.randint(1, 6)  # 不要每次都剛好提前 2 秒醒
            for state in r.rains.values():  # 紅包雨快開始時要醒著
                m = re.fullmatch(r"(\d+):(\d\d)", state)
                if m:
                    nap = min(nap, int(m[1]) * 60 + int(m[2]) - 2)
            self.pause(min(max(nap, 1), 20))

    def claim(self, r: Room):
        """按「直播間蝦幣」的領取，確認有成功。"""
        if not r.coin.claimable or "成功" in r.claimed or self.day.coin_limit_hit:
            return
        key = r.key
        self.dev.clear_toast()
        self.dev.tap(*r.coin.button)
        amount = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            s = self.snapshot()
            got = read_room(s).claimed
            verdict = self.check_messages(s, self.dev.last_toast())
            if verdict:
                log.info("領取沒成功：%s", REASONS.get(verdict, verdict))
                return
            if "成功領取" in got and read_room(s).key == key:
                amount = num(got)
                if amount is not None:
                    break
            self.pause(0.3)
        if amount is None:
            s = self.snapshot()
            self.capture("claim-unknown", s)
            raise StopBot("直播間蝦幣未確認成功，已停止以避免重複領取")
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
                 amount, key, room["claims"], day.coin_claims, day.coin_total)
        # 成功提示出來時按鈕常常還停在「領取」；等它換掉再走，免得又按一次
        # （那個位置按下去會打開任務獎勵面板）
        for _ in range(10):
            self.pause(0.8)
            if not read_room(self.snapshot()).coin.claimable:
                break

    def play_wheel(self, r: Room) -> bool:
        """轉這間的幸運轉盤。看到結果（這次轉的或之前轉過的）就回傳 True。"""
        key = r.key
        self.day.room(key)["wheel"] = True
        self.store.save()
        self.dev.tap(*r.wheel_button)
        scr, s = self.wait_for({Screen.WHEEL, Screen.WHEEL_RESULT}, 6)
        spins = 0
        for _ in range(6):
            if scr is Screen.WHEEL_RESULT:
                self.wheel_result(s, key)
                return True
            if scr is Screen.FOLLOW_DIALOG:
                log.info("轉盤限粉絲，關注 %s", key)
                self.dev.tap_node(s.id("tv_confirm"))
                # 對話框關掉後它的空殼視窗會擋一陣子，期間讀不到底下的轉盤（轉盤其實還開著）
                scr, s = self.wait_for({Screen.WHEEL, Screen.WHEEL_RESULT}, 8)
                continue
            if scr is not Screen.WHEEL or spins >= 3:
                break
            if any(k in t for t in s.texts_excluding(()) for k in MSG_WHEEL_LIMIT):
                self.wheel_exhausted()
                break
            # 「開始」沒有獨立節點，按轉盤正中央
            light = s.id("iv_draw_light")
            if light is None:
                break
            self.dev.tap_node(light)
            spins += 1
            scr, s = self.wait_for({Screen.WHEEL_RESULT, Screen.FOLLOW_DIALOG}, 12)
        log.info("轉盤沒有結果（可能還沒開始或已結束）")
        self.capture("wheel-unknown", s)
        if scr is not Screen.ROOM:
            self.close_overlay(s)
        return False

    def wheel_result(self, s: Snapshot, key: str):
        result, more = s.id("tv_result"), s.id("tv_more")
        text = result.text if result else ""
        amount = num(text) if "獲得" in text else None
        left = num(more.text) if more else None
        day = self.day
        room = day.room(key)
        # 每間只能轉一次，第一次看到結果就記帳（按「關注」時可能就直接轉了，當下讀不到結果）
        if amount is not None and not room.get("wheel_got"):
            room["wheel_got"] = True
            day.wheel_spins += 1
            day.wheel_total = round(day.wheel_total + amount, 2)
            log.info("✔ 轉盤 +%.2f｜%s｜今天還能轉 %s 次", amount, key,
                     int(left) if left is not None else "?")
        elif text:
            log.info("轉盤：%s", text)
        if left == 0 or day.wheel_spins >= 10 or any(k in text for k in MSG_WHEEL_LIMIT):
            self.wheel_exhausted()
        self.store.save()
        self.close_overlay(s)

    def wheel_exhausted(self):
        if not self.day.wheel_done:
            log.info("轉盤今天的次數用完了")
            self.day.wheel_done = True
            self.store.save()

    def observe_rain(self, r: Room, s: Snapshot):
        """紅包雨還沒實際遇過，先把畫面存下來，之後再補玩法。"""
        for name, state in r.rains.items():
            started = bool(state) and not re.fullmatch(r"\d+:\d\d", state)
            tag = (r.key, name, "start" if started else "seen")
            if tag in self.rain_seen:
                continue
            self.rain_seen.add(tag)
            label = "寵粉紅包雨" if name == "luck_rain" else "蝦皮紅包雨"
            log.info("發現%s（%s）：%s", label, r.key, state or "沒有倒數文字")
            self.capture(f"rain-{name}", s)
            if started:  # 連拍幾張，記錄開始後的樣子
                for i in range(8):
                    self.pause(1)
                    self.capture(f"rain-{name}-{i}")

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
        minute = 35 + (int(self.day.date.replace("-", "")) % 16)
        return reward_now().replace(hour=23, minute=minute, second=0, microsecond=0)

    def pending_amount(self, s: Snapshot) -> float | None:
        label = s.text("待提領蝦幣數")
        if label:
            return number_near(s, label, below=True)
        for node in s.trusted_nodes:
            match = re.fullmatch(r"待提領蝦幣數[:：]\s*(\d+(?:\.\d+)?)\s*", node.text)
            if match:
                return num(match[1])
        return None

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
                log.info("這間沒有任務獎勵入口，晚點再開面板")
                return
            self.dev.tap(*r.banner)
            scr, s = self.wait_for({Screen.PANEL}, 10)
        if scr is not Screen.PANEL:
            log.warning("任務獎勵面板沒打開（目前在%s）", scr.value)
            self.back_to_room(scr, s)
            return
        self.pause(1.5)  # 等 RN 畫面載完
        s = self.snapshot()
        if detect(s) is not Screen.PANEL:
            raise StopBot("任務面板已改變，停止使用舊畫面")
        btn = s.text("領取")
        if btn:
            before_pending = self.pending_amount(s)
            self.dev.tap_node(btn)
            self.pause(3)
            s = self.snapshot()
            self.check_messages(s, self.dev.last_toast())
            after_pending = self.pending_amount(s)
            if (detect(s) is Screen.PANEL and s.text("領取") is None
                    and before_pending is not None and after_pending is not None
                    and after_pending > before_pending):
                amount = round(after_pending - before_pending, 2)
                day = self.day
                day.watch_total = round(day.watch_total + amount, 2)
                log.info("✔ 觀看任務 +%.2f（領進待提領）", amount)
            else:
                self.capture("watch-claim-unknown", s)
                raise StopBot("觀看任務領取結果不明，已停止並保留統計")
        # 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤；只剩一個 90 的話分不出是哪個，就不更新
        day = self.day
        minutes = sorted(int(n.text[:-2]) for n in s.trusted_nodes if re.fullmatch(r"\d+分鐘", n.text))
        if len(minutes) >= 2 or (minutes and minutes[0] != 90):
            day.watch_minutes = minutes[0]
        if s.text("明天再來") or day.watch_minutes >= 90:
            day.watch_done = True
        pending = self.pending_amount(s)
        if pending is not None:
            day.pending = pending
        self.store.save()
        log.info("待提領 %.2f｜觀看任務 %d/90 分鐘", day.pending, day.watch_minutes)
        if pending is not None and self.should_withdraw(pending, final):
            self.withdraw(s)
        self.back_to_room(*self.look())

    def should_withdraw(self, pending: float, final: bool) -> bool:
        cfg, day = self.cfg, self.day
        if (not cfg.withdraw or day.withdrawal_pending
                or day.withdrawals >= cfg.withdraw_daily_limit
                or not math.isfinite(pending) or pending < 1):
            return False
        late = reward_now().hour == 23 and reward_now().minute >= 40
        return pending >= cfg.withdraw_min or final or late

    def withdraw(self, s: Snapshot):
        """在面板按「提領」，選不超過待提領的最大面額，送出並確認待提領有變少。"""
        day = self.day
        if not self.cfg.withdraw or day.withdrawals >= self.cfg.withdraw_daily_limit:
            return
        if day.withdrawal_pending:
            raise StopBot("先前提領結果尚未確認，請核對紀錄後再恢復自動提領")
        btn = s.text("提領")
        if not btn:
            return
        self.dev.tap_node(btn)
        scr, s = self.wait_for({Screen.WITHDRAW}, 10)
        if scr is not Screen.WITHDRAW:
            log.warning("提領頁沒打開（目前在%s）", scr.value)
            return
        self.pause(1)
        s = self.snapshot()
        if detect(s) is not Screen.WITHDRAW:
            raise StopBot("提領頁已改變，取消選擇金額")
        pending = self.pending_amount(s)
        if pending is None or pending < 1:
            log.warning("無法確認目前可提領餘額，取消提領")
            return
        float_win = s.id("rl_float_window")  # 直播浮窗會蓋住部分選項
        options = []
        for n in s.trusted_nodes:
            m = re.fullmatch(r"(\d+)\.00", n.text)
            if (m and 1 <= int(m[1]) <= pending and n.el.get("enabled") != "false"
                    and not (float_win and inside(n.center, float_win.bounds))):
                options.append((int(m[1]), n))
        if not options:
            log.info("待提領 %.2f，沒有能選的提領金額", pending)
            self.dev.back()
            return
        amount, node = max(options, key=lambda t: t[0])
        self.dev.tap_node(node)
        self.pause(1)
        s = self.snapshot()
        if detect(s) is not Screen.WITHDRAW:
            raise StopBot("提領頁已改變，取消送出")
        latest_pending = self.pending_amount(s)
        if latest_pending is None or abs(latest_pending - pending) > 0.001:
            raise StopBot("提領餘額在送出前改變或無法辨識，請重新核對")
        selected = s.text(node.text)
        if selected is None or selected.el.get("enabled") == "false":
            raise StopBot("已選擇的提領金額不再可用，取消送出")
        submit = s.text("立即提領")
        if not submit:
            log.warning("找不到「立即提領」")
            self.dev.back()
            return
        self.dev.clear_toast()
        day = self.day
        if (not self.cfg.withdraw or day.withdrawal_pending
                or day.withdrawals >= self.cfg.withdraw_daily_limit):
            raise StopBot("提領條件在送出前已改變，取消送出")
        day.withdrawal_pending = True
        self.store.save()
        self.dev.tap_node(submit)
        confirmed = False
        for _ in range(10):
            self.pause(1)
            scr, s = self.look()
            self.check_messages(s, self.dev.last_toast())
            # 每天只能提領 3 次；如果手動提領過，程式的計數會少算，看到上限提示就當作今天用完
            limit = None
            if scr in (Screen.WITHDRAW, Screen.PANEL):
                limit = next((t for t in s.texts_excluding(FREE_TEXT_IDS) + [self.dev.last_toast()]
                              if "提領" in t and any(marker in t for marker in
                                                     ("已達", "已超過", "已用完", "不能再"))), None)
            if limit:
                log.info("提領被拒：%s，今天不再提領", limit)
                day = self.day
                day.withdrawals = self.cfg.withdraw_daily_limit
                day.withdrawal_pending = False
                self.store.save()
                break
            confirm = s.text("確認提領") if scr is Screen.WITHDRAW else None
            if confirm and not confirmed:
                self.dev.tap_node(confirm)
                confirmed = True
                continue
            after = self.pending_amount(s) if scr in (Screen.WITHDRAW, Screen.PANEL) else None
            if after is not None and abs(after - (pending - amount)) < 0.011:
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
            raise StopBot("提領結果不明，已停止；重啟後也不會自動重送")
        self.dev.back()

    # ---------------- 主迴圈 ----------------
    def run(self, hours: float | None = None):
        if hours is not None and (not math.isfinite(hours) or hours <= 0):
            raise ValueError("hours 必須為有限的正數")
        self.end = time.monotonic() + hours * 3600 if hours is not None else None
        log.info("開始：%s", self.store.summary())
        try:
            while not self.time_up():
                try:
                    self.ensure_app()
                    self.cycle()
                except (StopBot, UnsafeAction, StateError):
                    raise
                except Exception:
                    log.exception("出錯了，10 秒後繼續")
                    self.pause(10)
        except StopBot as e:
            # A deadline can expire inside a wait.  That is a successful timed
            # run; every other StopBot must reach the CLI so it returns nonzero.
            if not self.time_up():
                log.error("停止：%s", e)
                raise
        except (UnsafeAction, StateError) as e:
            log.error("停止：%s", e)
            raise
        log.info("結束：%s", self.store.summary())

    def cycle(self):
        day = self.day
        if day.coin_limit_hit and day.wheel_done and day.watch_done:
            self.rest_until_tomorrow()
            return
        # 剛好在一間還在倒數的直播間就先待著（離開會讓倒數重來）
        scr, s = self.look()
        if scr is Screen.ROOM:
            r = read_room(s)
            if r.coin.present and not day.coin_limit_hit and not day.room_done(r.key):
                reason, key = self.stay("coins")
                self.finish_room({key}, reason, "coins")
                return
        card, purpose = self.pick()
        if not card:
            log.info("沒有合適的直播間，2 分鐘後再找")
            self.pause(120)
            return
        reason, key = self.stay(purpose)
        self.finish_room({card.key, key}, reason, purpose)

    def finish_room(self, keys: set[str], reason: str, purpose: str):
        day = self.day
        keys = {k for k in keys if k}
        for k in keys:
            room = day.room(k)
            if purpose == "watch" and reason == "watched":
                room["watched"] = True
            if reason in DONE_REASONS or reason.startswith("coin_state"):
                room["done"] = True
        self.store.save()
        claims = max((day.rooms[k]["claims"] for k in keys), default=0)
        log.info("離開 %s：%s（這間領了 %d 次）｜%s", "／".join(sorted(keys)) or "?",
                 REASONS.get(reason, reason), claims, self.store.summary())
        self.leave()

    def rest_until_tomorrow(self):
        day = self.day
        completed_date = day.date
        if not day.final_panel:
            self.go_live()
            self.panel_tasks(final=True)
            day = self.day
            if day.date != completed_date:
                return  # 收尾跨午夜後，立即開始新獎勵日。
            day.final_panel = True
            self.store.save()
        log.info("今天能領的都領完了，休息到明天：%s", self.store.summary())
        wake = datetime.fromisoformat(f"{completed_date}T00:03:00+08:00") + timedelta(days=1)
        while reward_now() < wake and not self.time_up():
            self.pause(min(300, max(1, (wake - reward_now()).total_seconds())))

    def time_up(self) -> bool:
        return self.end is not None and time.monotonic() >= self.end
