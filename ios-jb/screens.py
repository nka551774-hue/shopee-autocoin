"""用 OCR 判斷 iOS 蝦皮的畫面（ZXTouch 版）：直播間右上角的任務框、任務獎勵面板、提領頁。

跟 ios/screens.py 同一套判讀，差別只有兩點：
  1. 截圖與 OCR 都由手機上的 ZXTouch（Vision.framework）做。這一版 tweak 沒有取色／找色，
     所以「領取」按鈕改成直接 OCR 按鈕上的字，而不是看橘色／黃色。
  2. 座標：這個檔裡全部是 iOS point（375x812），送進 tweak 前才乘 scale 變像素。
"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property

TASK_BOX = (215, 120, 375, 420)  # 右上角任務框（含下方的福袋／轉盤圖示）
NAME_BOX = (40, 95, 160, 125)    # 左上角的直播主名稱
CHAT_TOP = 560                   # 這條線以下是留言區，觀眾可以亂打字，比對提示文字時略過
BACK = (19, 70)                  # 面板、提領頁左上角的返回鍵（直播間的這個位置是個人頁，別亂按）


def clean(text: str) -> str:
    """OCR 常見的誤讀：小數點讀成「·」或「,」、0 讀成 O。只拿來解析數字，不要拿來比對名字。"""
    return (text.replace("·", ".").replace(",", ".").replace("O", "0").replace("o", "0")
            .replace("：", ":").replace(" ", ""))


def money(text: str, small: bool = False) -> float | None:
    """「0·25」→ 0.25。small：金額只有個位數，前面多出來的數字是蝦幣圖示（「30·02」→ 0.02）。"""
    m = re.search(r"(?<![\d.-])(\d+)\.(\d{1,2})(?![\d.])", clean(text))
    if not m:
        return None
    amount = float(f"{m[1][-1:] if small else m[1]}.{m[2]}")
    return amount if math.isfinite(amount) else None


def countdown(text: str) -> int | None:
    """「04:36」→ 276 秒。冒號偶爾會漏掉（「0115」）。"""
    t = clean(text)
    m = re.search(r"(\d{1,2}):(\d{2})", t) or re.fullmatch(r"(\d{2})(\d{2})", t)
    return int(m[1]) * 60 + int(m[2]) if m and int(m[2]) < 60 else None


@dataclass
class Line:
    text: str
    x: float  # 中心點，point
    y: float
    w: float = 0.0
    h: float = 0.0


class Shot:
    """一張「現在的畫面」。ZXTouch 的 OCR 每次都自己重拍截圖，所以這裡不存圖，只存判讀結果。"""

    def __init__(self, zx):
        self.zx = zx
        self.scale = zx.screen_scale()
        w, h = zx.screen_size()          # 像素
        if not all(math.isfinite(v) and v > 0 for v in (self.scale, w, h)):
            raise ValueError("無效的螢幕尺寸或比例")
        self.size = (w / self.scale, h / self.scale)
        self._cache: dict[tuple, list[Line]] = {}

    # ---------------- 基本 ----------------
    def region(self, box) -> tuple:
        """point 的 (x1, y1, x2, y2) → 像素的 (x, y, width, height)。"""
        x1, y1, x2, y2 = box
        s = self.scale
        return (round(x1 * s), round(y1 * s), round((x2 - x1) * s), round((y2 - y1) * s))

    def ocr(self, box=None) -> list[Line]:
        """OCR 一塊 point 框（None = 整頁），回傳 point 座標（中心點）的 Line。"""
        key = box or (0, 0, 0, 0)
        if key in self._cache:
            return self._cache[key]
        lines, _ = self.zx.ocr(self.region(box) if box else (0, 0, 0, 0))
        s = self.scale
        out = [Line(text=ln["text"].replace(" ", ""),
                    x=(ln["x"] + ln["width"] / 2) / s,
                    y=(ln["y"] + ln["height"] / 2) / s,
                    w=ln["width"] / s, h=ln["height"] / s)
               for ln in lines]
        self._cache[key] = out
        return out

    @cached_property
    def lines(self) -> list[Line]:
        return self.ocr()

    @cached_property
    def box(self) -> list[Line]:
        """任務框。先用整頁 OCR 的結果；整頁讀不到框裡的標題才對框重讀一次（Vision 對小字比較吃力）。"""
        inside = self.in_box(self.lines, TASK_BOX)
        if any(("觀看" in ln.text) or ("直播間" in ln.text) or ("蝦皮" in ln.text) for ln in inside):
            return inside
        return self.ocr(TASK_BOX)

    @cached_property
    def name(self) -> str:
        lines = self.in_box(self.lines, NAME_BOX) or self.ocr(NAME_BOX)
        return "".join(ln.text for ln in lines if ln.y < 116)  # 下面那行是「N 觀看者」

    # ---------------- 查詢 ----------------
    @staticmethod
    def in_box(lines: list[Line], box) -> list[Line]:
        x1, y1, x2, y2 = box
        return [ln for ln in lines if x1 <= ln.x <= x2 and y1 <= ln.y <= y2]

    def find(self, *keys: str, lines: list[Line] | None = None) -> Line | None:
        return next((ln for ln in (self.lines if lines is None else lines)
                     if any(k in ln.text for k in keys)), None)

    def texts(self) -> list[str]:
        """留言區以外的文字（比對 App 的提示用）。"""
        return [ln.text for ln in self.lines if ln.y < CHAT_TOP]

    def has_text(self, box, *keys: str) -> bool:
        """box（point）裡有沒有這些字。先看整頁 OCR 的結果，沒有才對這塊重讀。"""
        if any(any(k in ln.text for k in keys) for ln in self.in_box(self.lines, box)):
            return True
        return any(any(k in ln.text for k in keys) for ln in self.ocr(box))


class Screen(str, Enum):
    ROOM = "直播間"
    PANEL = "任務獎勵面板"
    WITHDRAW = "提領頁"
    UNKNOWN = "未知"


def detect(s: Shot) -> Screen:
    if any(ln.text == "提領數值" and ln.y < CHAT_TOP for ln in s.lines):
        return Screen.WITHDRAW
    # 直播間領取成功的提示也有「待提領蝦幣數」（「已發放至「待提領蝦幣數」中」），只認面板上那個標籤
    if any(ln.y < CHAT_TOP and ln.text in ("完成任務拿獎勵", "待提領蝦幣數") for ln in s.lines):
        return Screen.PANEL
    # OCR 偶爾會漏掉某一行，多看幾個地方：上方分頁列、直播主名稱下的人數、留言區、任務框
    tabs = [ln for ln in s.lines if ln.y < 90]
    if (s.find("短影音", "推薦", lines=tabs) or s.find("觀看者", "說點", "歡迎來到蝦皮直播")
            or s.find("觀看直播", "直播間", lines=s.box)):
        return Screen.ROOM
    return Screen.UNKNOWN


@dataclass
class Task:
    """任務框裡「觀看直播任務」或「直播間蝦幣」其中一格。"""
    present: bool = False
    state: str = ""                  # OCR 讀到的倒數 mm:ss；按鈕是「領取」時固定為「領取」
    claimable: bool = False
    button: tuple[float, float] | None = None

    @property
    def seconds(self) -> int | None:
        return None if self.claimable else countdown(self.state)


def button_box(title_y: float):
    """任務框裡按鈕的位置（相對標題 y）。"""
    return (298, title_y + 4, 375, title_y + 34)


def _task(s: Shot, title: Line | None) -> Task:
    if title is None:
        return Task()
    ty = title.y
    # 這一版 tweak 不能取色，改成讀按鈕上的字：倒數是「04:36」，可以領是「領取」
    claimable = s.has_text(button_box(ty), "領取")
    below = [ln for ln in s.box if 8 < ln.y - ty < 40 and ln.x > 300]
    return Task(
        present=True,
        state="領取" if claimable else (below[0].text if below else ""),
        claimable=claimable,
        button=(330, ty + 20),
    )


@dataclass
class Room:
    anchor: str = ""
    watch: Task = field(default_factory=Task)
    coin: Task = field(default_factory=Task)
    claimed: str = ""                            # 「成功領取0·25蝦幣」
    banner: tuple[float, float] | None = None    # 任務框標題，點了打開任務獎勵面板

    @property
    def key(self) -> str:
        return self.anchor


def read_room(s: Shot) -> Room:
    watch = s.find("觀看", lines=s.box)
    coin = s.find("直播間", "間蝦", lines=s.box)
    head = s.find("蝦皮直播", lines=s.box)
    top = watch or coin  # 標題在第一格正上方；有些直播間只有直播間蝦幣那一格
    banner = (head.x, head.y) if head else ((306, top.y - 24) if top else None)
    done = s.find("成功領取", lines=[ln for ln in s.lines if ln.y < CHAT_TOP])
    return Room(
        anchor=s.name,
        watch=_task(s, watch),
        coin=_task(s, coin),
        claimed=done.text if done else "",
        banner=banner,
    )


@dataclass
class Panel:
    pending: float | None = None                 # 待提領蝦幣數
    withdraw: tuple[float, float] | None = None  # 「提領」
    claim: tuple[float, float] | None = None     # 觀看任務的「領取」（要 OCR 到「領取」才算）
    claim_amount: float | None = None            # 「領取」上面那個小標籤的金額
    minutes: list[int] = field(default_factory=list)
    done: bool = False                           # 「明天再來」


def read_panel(s: Shot) -> Panel:
    p = Panel()
    label = s.find("待提領蝦幣數")
    if label:
        amount = next((ln for ln in s.lines if 15 < ln.y - label.y < 45 and ln.x < 200
                       and money(ln.text) is not None), None)
        p.pending = money(amount.text) if amount else None
        btn = next((ln for ln in s.lines if ln.text == "提領" and ln.x > 250
                    and 0 < ln.y - label.y < 60), None)
        if btn:
            p.withdraw = (btn.x, btn.y)
    # 「觀看直播任務」是區塊標題，「觀看直播」才是任務本身，按鈕在它右邊
    task = next((ln for ln in s.lines if ln.text == "觀看直播"), None)
    if task:
        if s.has_text((265, task.y - 10, 355, task.y + 14), "領取"):
            p.claim = (300, task.y + 6)
        chip = next((ln for ln in s.lines if ln.x > 270 and -22 < ln.y - task.y < -4), None)
        p.claim_amount = money(chip.text, small=True) if chip else None
    # 進度條下有「N分鐘」和終點的「90分鐘」兩個標籤
    p.minutes = sorted(int(m[1]) for ln in s.lines if (m := re.fullmatch(r"(\d+)分鐘", ln.text)))
    p.done = s.find("明天再來") is not None
    return p


@dataclass
class WithdrawPage:
    pending: float | None = None
    options: list[tuple[int, tuple[float, float]]] = field(default_factory=list)  # (面額, 位置)
    submit: tuple[float, float] | None = None    # 「立即提領」


def read_withdraw(s: Shot) -> WithdrawPage:
    w = WithdrawPage()
    top, bottom = s.find("提領數值"), s.find("立即提領")
    if top is None or bottom is None or top.y >= bottom.y:
        return w
    if bottom:
        w.submit = (bottom.x, bottom.y)
    label = s.find("待提領蝦幣數")
    if label:
        w.pending = money(label.text)
    for ln in s.lines:
        if top and ln.y <= top.y or bottom and ln.y >= bottom.y or ln is label:
            continue
        m = re.fullmatch(r"(\d+)\.00", clean(ln.text))
        if m and int(m[1]) > 0:
            w.options.append((int(m[1]), (ln.x, ln.y)))
    return w
