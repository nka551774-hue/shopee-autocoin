"""用 OCR 和顏色判斷 iOS 蝦皮的畫面：直播間右上角的任務框、任務獎勵面板、提領頁。

iOS 讀不到元件樹（XCUIApplication 會讓 TouchSynthesis 卡死），只能看截圖。
座標都是 iOS point（iPhone 11 Pro 375x812，截圖 3 倍）。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property

import numpy as np
from PIL import Image

import ocr
from ocr import Line

TASK_BOX = (215, 120, 375, 420)  # 右上角任務框（含下方的福袋／轉盤圖示）
NAME_BOX = (40, 95, 160, 125)    # 左上角的直播主名稱
CHAT_TOP = 560                   # 這條線以下是留言區，觀眾可以亂打字，比對提示文字時略過
BACK = (19, 70)                  # 面板、提領頁左上角的返回鍵（直播間的這個位置是個人頁，別亂按）


def _orange(r, g, b):  # 直播間的「領取」、面板的「提領」
    return (r > 200) & (g > 50) & (g < 140) & (b < 90)


def _yellow(r, g, b):  # 面板上觀看任務的「領取」
    return (r > 225) & (g > 140) & (g < 220) & (b < 120)


def clean(text: str) -> str:
    """OCR 常見的誤讀：小數點讀成「·」或「,」、0 讀成 O。只拿來解析數字，不要拿來比對名字。"""
    return (text.replace("·", ".").replace(",", ".").replace("O", "0").replace("o", "0")
            .replace("：", ":").replace(" ", ""))


def money(text: str, small: bool = False) -> float | None:
    """「0·25」→ 0.25。small：金額只有個位數，前面多出來的數字是蝦幣圖示（「30·02」→ 0.02）。"""
    m = re.search(r"(\d+)\.(\d{1,2})", clean(text))
    if not m:
        return None
    return float(f"{m[1][-1:] if small else m[1]}.{m[2]}")


def countdown(text: str) -> int | None:
    """「04:36」→ 276 秒。冒號偶爾會漏掉（「0115」）。"""
    t = clean(text)
    m = re.search(r"(\d{1,2}):(\d{2})", t) or re.fullmatch(r"(\d{2})(\d{2})", t)
    return int(m[1]) * 60 + int(m[2]) if m and int(m[2]) < 60 else None


class Shot:
    """一張截圖，OCR 結果用到才算、算過就留著。"""

    def __init__(self, data: bytes):
        self.data = data
        with Image.open(io.BytesIO(data)) as image:
            if image.size != (1125, 2436):
                raise ValueError(f"Unsupported screenshot size {image.size}; layout requires 1125x2436")
            self.im = image.convert("RGB")

    @cached_property
    def px(self):
        # Color predicates only compare channels; uint8 halves the old int16 buffer.
        return np.asarray(self.im)

    @cached_property
    def lines(self) -> list[Line]:
        return ocr.read(self.im)

    @cached_property
    def box(self) -> list[Line]:
        """任務框，只留白字再放大兩倍辨識。"""
        return ocr.read(self.im, TASK_BOX, upscale=2, prep=ocr.white_text)

    @cached_property
    def name(self) -> str:
        lines = ocr.read(self.im, NAME_BOX, upscale=2)
        return "".join(ln.text for ln in lines if ln.y < 116)  # 下面那行是「N 觀看者」

    def find(self, *keys: str, lines: list[Line] | None = None) -> Line | None:
        return next((ln for ln in (self.lines if lines is None else lines)
                     if any(k in ln.text for k in keys)), None)

    def texts(self) -> list[str]:
        """留言區以外的文字（比對 App 的提示用）。"""
        return [ln.text for ln in self.lines if ln.y < CHAT_TOP]

    def frac(self, box: tuple[float, float, float, float], color) -> float:
        """box 裡有多少比例的像素是這個顏色。"""
        x1, y1, x2, y2 = (int(v * ocr.SCALE) for v in box)
        a = self.px[y1:y2, x1:x2]
        if not a.size:
            return 0.0
        return float(color(a[..., 0], a[..., 1], a[..., 2]).mean())


class Screen(str, Enum):
    ROOM = "直播間"
    PANEL = "任務獎勵面板"
    WITHDRAW = "提領頁"
    UNKNOWN = "未知"


def detect(s: Shot) -> Screen:
    if s.find("立即提領", "提領數值"):
        return Screen.WITHDRAW
    # 直播間領取成功的提示也有「待提領蝦幣數」（「已發放至「待提領蝦幣數」中」），只認面板上那個標籤
    if s.find("完成任務拿獎勵") or any(ln.text.endswith("待提領蝦幣數") for ln in s.lines):
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
    state: str = ""                  # OCR 讀到的倒數 mm:ss；按鈕是橘色時固定為「領取」
    claimable: bool = False
    button: tuple[float, float] | None = None

    @property
    def seconds(self) -> int | None:
        return None if self.claimable else countdown(self.state)


def _task(s: Shot, title: Line | None) -> Task:
    if title is None:
        return Task()
    ty = title.y
    # 「領取」是橘底白字，白字遮罩會把它吃掉，改看顏色；倒數時那格是深色底
    claimable = s.frac((310, ty + 14, 350, ty + 26), _orange) > 0.4
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
    claim: tuple[float, float] | None = None     # 觀看任務的「領取」（黃色才算）
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
        elif s.frac((285, label.y + 19, 320, label.y + 31), _orange) > 0.3:
            p.withdraw = (301, label.y + 25)
    # 「觀看直播任務」是區塊標題，「觀看直播」才是任務本身，按鈕在它右邊
    task = next((ln for ln in s.lines if ln.text == "觀看直播"), None)
    if task:
        if s.frac((280, task.y + 1, 320, task.y + 13), _yellow) > 0.3:
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
        m = re.search(r"(\d+)\.00$", clean(ln.text))
        if m and int(m[1]) > 0:
            w.options.append((int(m[1]), (ln.x, ln.y)))
    return w
