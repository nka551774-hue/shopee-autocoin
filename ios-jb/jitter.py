"""讓節奏不要那麼像機器人：所有等待與點擊都加一點隨機抖動。

風控不只看你裝了什麼，也看**行為的規律性**：固定 10.5 分鐘換台、每次都點在同一個
像素、倒數剩 3 秒準時醒來、每 30 分鐘準時開面板 —— 這些完美週期性在伺服器端最好抓。

（跟 autocoin/bot.py 的 doze()/wobble()、android/Jitter.kt、ios-app/Jitter.swift 同一套。）
"""
from __future__ import annotations

import random
import time

JITTER = 0.18  # 等待時間 ±18%

_sleep = time.sleep  # doze 自己用；避免被下面的整批取代誤傷


def set_jitter(pct: float) -> None:
    """讓 Config 覆蓋預設的抖動比例（0 = 完全固定，不建議）。"""
    global JITTER
    JITTER = max(0.0, pct)


def doze(sec: float, pct: float | None = None) -> None:
    """帶抖動的 sleep。"""
    p = JITTER if pct is None else pct
    _sleep(max(0.05, sec * random.uniform(1 - p, 1 + p)))


def wobble(base: float, pct: float = 0.18) -> float:
    """回傳一個帶抖動的數值（給門檻比較用，不做 sleep）。"""
    return base * random.uniform(1 - pct, 1 + pct)


def up(base: float, pct: float = 0.22) -> float:
    """只往上抖動。用在「至少要等這麼久才做得完」的門檻上，
    例如觀看任務的 10 分鐘 —— 往下抖會讓任務做不完，往上抖只是多待一下。"""
    return base * random.uniform(1.0, 1.0 + pct) if base > 0 else base


def int_in(low: int, high: int) -> int:
    """[low, high] 之間的隨機整數（含頭尾）。"""
    return low if high <= low else random.randint(low, high)


def wobble_xy(x: float, y: float, radius: float) -> tuple[float, float]:
    """把點推到中心附近 ±radius 的隨機位置。"""
    r = int(max(0, radius))
    return x + random.randint(-r, r), y + random.randint(-r, r)