"""判斷目前在哪個畫面，以及解析直播間、「看更多」列表的內容。"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from enum import Enum

from .ui import PKG, Node, Snapshot

_TIME = re.compile(r"(\d{1,4}):([0-5]\d)")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def num(text: str) -> float | None:
    m = _NUM.search(text or "")
    value = float(m.group()) if m else None
    return value if value is not None and math.isfinite(value) else None


def inside(point: tuple[int, int], bounds: tuple[int, int, int, int]) -> bool:
    x, y = point
    x1, y1, x2, y2 = bounds
    return x1 <= x <= x2 and y1 <= y <= y2


def number_near(s: Snapshot, anchor: Node, below: bool) -> float | None:
    """找 anchor 正上方或正下方最近的純數字文字（面板上的金額都是這樣排的）。"""
    ax1, ay1, ax2, ay2 = anchor.bounds
    best = None
    for n in s.trusted_nodes:
        if not re.fullmatch(r"\d+(?:\.\d+)?", n.text):
            continue
        x1, y1, x2, y2 = n.bounds
        if x2 < ax1 - 200 or x1 > ax2 + 200:
            continue
        gap = (y1 - ay2) if below else (ay1 - y2)
        if -20 <= gap <= 150 and (best is None or gap < best[0]):
            value = num(n.text)
            if value is not None:
                best = (gap, value)
    return best[1] if best else None


class Screen(str, Enum):
    ROOM = "直播間"
    LIVE_PREVIEW = "直播預覽"        # 直播分頁的預覽模式，要往上滑才會進直播間
    EXPLORE = "看更多列表"
    PANEL = "任務獎勵面板"
    WITHDRAW = "提領頁"
    WHEEL = "幸運轉盤"
    WHEEL_RESULT = "轉盤結果"
    FOLLOW_DIALOG = "關注對話框"
    VIDEO_TAB = "直播短影音分頁"
    HOME = "首頁"
    WEB = "網頁"
    OTHER_APP = "其他 App"
    UNKNOWN = "未知"


def detect(s: Snapshot) -> Screen:
    if s.package != PKG:
        return Screen.OTHER_APP
    # 疊在直播間上面的東西先判斷
    if s.id("fl_lucky_result"):
        return Screen.WHEEL_RESULT
    if s.id("tv_confirm") and s.text("僅限粉絲！"):
        return Screen.FOLLOW_DIALOG
    if s.id("draw_title"):
        return Screen.WHEEL
    if s.text("立即提領"):
        return Screen.WITHDRAW
    if s.text("待提領蝦幣數"):
        return Screen.PANEL
    if s.by_prefix("live_card_cover_"):
        return Screen.EXPLORE
    if s.id("tv_anchor_name") or s.id("live_page_anchor_info_view"):
        pager = s.id("live_cubepage_0")
        if s.text("向上滑動以查看完整內容") or (pager and pager.bounds[1] > 100):
            return Screen.LIVE_PREVIEW
        return Screen.ROOM
    if "WebPageActivity" in s.activity:
        return Screen.WEB
    if s.desc("tab_bar_button_video_and_live"):
        return Screen.VIDEO_TAB if s.desc("click 1 tab") else Screen.HOME
    return Screen.UNKNOWN


@dataclass
class Task:
    """「觀看直播任務」或「直播間蝦幣」其中一格。"""
    present: bool = False
    coin: float | None = None       # 這一輪可以拿到的蝦幣
    state: str = ""                 # 倒數 mm:ss、「領取」或其他文字
    button: tuple[int, int] | None = None

    @property
    def seconds(self) -> int | None:
        m = _TIME.fullmatch(self.state.strip())
        return int(m[1]) * 60 + int(m[2]) if m else None

    @property
    def claimable(self) -> bool:
        return self.button is not None and self.state.strip() == "領取"


def _task(box: Node | None) -> Task:
    if box is None:
        return Task()
    state, coin = box.find("tv_state_btn"), box.find("tv_coin_num")
    return Task(
        present=True,
        coin=num(coin.text) if coin else None,
        state=state.text if state else "",
        button=state.center if state else None,
    )


@dataclass
class Room:
    anchor: str = ""
    title: str = ""
    watch: Task = field(default_factory=Task)
    coin: Task = field(default_factory=Task)
    wheel: str = ""                                  # 參加／已參加／已結束；空字串＝沒有轉盤
    wheel_button: tuple[int, int] | None = None
    rains: dict[str, str] = field(default_factory=dict)   # {"luck_rain": 倒數文字}
    claimed: str = ""                                # 「成功領取 0.35 Coins」
    banner: tuple[int, int] | None = None            # 任務獎勵面板入口
    explore: tuple[int, int] | None = None           # 「看更多」
    close: tuple[int, int] | None = None             # 獨立直播間右上角的 X

    @property
    def key(self) -> str:
        return self.anchor or self.title


def read_room(s: Snapshot) -> Room:
    def center(rid: str) -> tuple[int, int] | None:
        n = s.id(rid)
        return n.center if n else None

    def text(rid: str) -> str:
        n = s.id(rid)
        return n.text if n else ""

    wheel_box = s.id("box_view_lucky_draw")
    wheel_state = wheel_box.find("tv_state_btn") if wheel_box else None
    rains = {}
    for rid, name in (("box_view_luck_rain", "luck_rain"), ("box_view_super_rain", "super_rain")):
        box = s.id(rid)
        if box:
            cd = box.find("tv_count_down") or box.find("stv_count_down")
            rains[name] = cd.text if cd else ""
    return Room(
        anchor=text("tv_anchor_name"),
        title=text("tv_live_title"),
        watch=_task(s.id("box_view_watch_live_task")),
        coin=_task(s.id("box_view_coin_streamer") or s.id("box_view_coin_streamer_v2")),
        wheel=(wheel_state.text if wheel_state else "?") if wheel_box else "",
        wheel_button=wheel_box.center if wheel_box else None,
        rains=rains,
        claimed=text("tv_coin_claimed_content"),
        banner=center("iv_banner"),
        explore=center("explore_layout"),
        close=center("iv_page_close"),
    )


@dataclass
class Card:
    """「看更多」列表裡的一張直播卡片。"""
    rid: str
    center: tuple[int, int]
    height: int
    viewers: str
    max_coins: float | None     # 卡片上的「最多X」
    lottery: bool               # 卡片上的「抽獎」
    streamer: str
    title: str

    @property
    def key(self) -> str:
        return self.streamer or self.title


# 卡片上的標籤，不是標題
CARD_TAGS = {"直播限時價", "官方特選賣家", "關注中的賣場", "官方合作直播主", "優惠券", "抽獎"}


def read_cards(s: Snapshot) -> list[Card]:
    cards = []
    for n in s.by_prefix("live_card_cover_"):
        avatar = next((c for c in n.iter() if c.rid.startswith("streamer_avatar_")), None)
        in_avatar = set(avatar.el.iter("node")) if avatar else set()
        texts = [c for c in n.iter() if c.text and c.el not in in_avatar]
        labels = [c.text for c in texts]
        coins = next((num(t) for t in labels if t.startswith("最多")), None)
        # 標題是頭像那排上面、最下面的一行字；卡片只露出上半部時拿不到
        body = [c for c in texts if c.text not in CARD_TAGS and c.bounds[1] - n.bounds[1] > 200]
        title = max(body, key=lambda c: c.bounds[1]).text if body else ""
        cards.append(Card(
            rid=n.rid,
            center=n.center,
            height=n.height,
            viewers=labels[0] if labels else "",
            max_coins=coins,
            lottery="抽獎" in labels,
            streamer=" ".join(avatar.texts()) if avatar else "",
            title=title,
        ))
    return cards
