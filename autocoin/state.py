"""每日統計與處理過的直播間，存成 JSON，重開程式也記得。"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")


def reward_now() -> datetime:
    """蝦皮台灣的獎勵日不隨電腦或手機時區改變。"""
    return datetime.now(TAIPEI)


class StateError(RuntimeError):
    """統計無法安全讀寫；停止操作，避免把每日額度當成零。"""


@dataclass
class Day:
    date: str
    coin_claims: int = 0            # 直播間蝦幣領取次數（官方上限每天 100）
    coin_total: float = 0.0
    coin_limit_hit: bool = False
    wheel_spins: int = 0            # 轉盤（每天 10 次）
    wheel_total: float = 0.0
    wheel_done: bool = False
    watch_total: float = 0.0        # 在面板領到的觀看任務蝦幣
    watch_minutes: int = 0          # 面板上的觀看進度（滿 90 分鐘）
    watch_done: bool = False
    withdrawals: int = 0            # 提領次數（每天 3 次）
    withdrawal_pending: bool = False  # 已送出但未確認；跨重啟、換日保留
    withdrawn: float = 0.0
    pending: float = 0.0            # 最後一次看到的待提領蝦幣數
    last_panel: float = 0.0         # 最後一次開面板的時間（epoch）
    final_panel: bool = False       # 今天收尾的面板操作做過了沒
    rooms: dict = field(default_factory=dict)

    def room(self, key: str) -> dict:
        return self.rooms.setdefault(key, {"claims": 0, "coins": 0.0, "done": False,
                                           "wheel": False, "watched": False})

    def room_done(self, key: str) -> bool:
        return bool(key) and self.rooms.get(key, {}).get("done", False)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._day = self._load()

    def _load(self) -> Day:
        today = reward_now().date().isoformat()
        try:
            if self.path.stat().st_size > 8_000_000:
                raise ValueError("狀態檔超過 8 MB")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            loaded = self._validate(data)
            if loaded.date > today:
                raise ValueError("狀態日期在未來，請檢查系統時鐘")
            if data.get("date") == today:
                return loaded
            return Day(date=today, withdrawal_pending=loaded.withdrawal_pending)
        except FileNotFoundError:
            return Day(date=today)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise StateError(f"無法讀取 {self.path}，請先檢查或還原統計檔；不會自動清零") from exc

    @staticmethod
    def _validate(data: dict) -> Day:
        if not isinstance(data, dict) or not isinstance(data.get("date"), str):
            raise ValueError("狀態必須包含日期")
        if date.fromisoformat(data["date"]).isoformat() != data["date"]:
            raise ValueError("日期必須使用 YYYY-MM-DD")
        defaults = Day(date=data["date"])
        values = {}
        for item in dataclasses.fields(Day):
            name = item.name
            value = data.get(name, getattr(defaults, name))
            default = getattr(defaults, name)
            if isinstance(default, bool):
                valid = type(value) is bool
            elif isinstance(default, int):
                valid = type(value) is int and value >= 0
            elif isinstance(default, float):
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            else:
                valid = isinstance(value, type(default))
            if not valid:
                raise ValueError(f"無效的狀態欄位：{name}")
            values[name] = value
        rooms = {}
        for key, room in values["rooms"].items():
            if not isinstance(key, str) or not isinstance(room, dict):
                raise ValueError("無效的直播間統計")
            normalized = {"claims": 0, "coins": 0.0, "done": False,
                          "wheel": False, "watched": False, **room}
            for name in ("done", "wheel", "watched", "wheel_got"):
                if name in normalized and type(normalized[name]) is not bool:
                    raise ValueError(f"無效的直播間旗標：{name}")
            if type(normalized["claims"]) is not int or normalized["claims"] < 0:
                raise ValueError("無效的直播間領取次數")
            coins = normalized["coins"]
            if type(coins) not in (int, float) or not math.isfinite(coins) or coins < 0:
                raise ValueError("無效的直播間金額")
            rooms[key] = normalized
        values["rooms"] = rooms
        return Day(**values)

    @contextmanager
    def exclusive(self):
        """所有會操作裝置的 CLI 持有此鎖；鎖檔不可刪除以免分裂鎖。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StateError(f"另一個程序正在使用 {self.path}，請先停止它") from exc
            try:
                self._day = self._load()
                yield self
            finally:
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @property
    def day(self) -> Day:
        today = reward_now().date().isoformat()
        if self._day.date > today:
            raise StateError("系統日期倒退，請先校正時鐘；每日額度不會自動歸零")
        if self._day.date != today:
            log.info("換日了：%s → %s", self._day.date, today)
            self._day = Day(date=today, withdrawal_pending=self._day.withdrawal_pending)
        if self._day.coin_claims >= 100:
            self._day.coin_limit_hit = True
        if self._day.wheel_spins >= 10:
            self._day.wheel_done = True
        return self._day

    def save(self):
        tmp = None
        try:
            data = dataclasses.asdict(self._day)
            self._validate(data)
            payload = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp",
                                             delete=False) as stream:
                tmp = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise StateError(f"無法保存 {self.path}；已停止以避免重複操作") from exc
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    # Cleanup failure must not mask StateError and turn a fatal
                    # persistence failure into a retryable generic exception.
                    log.warning("無法清理統計暫存檔 %s", tmp)

    def summary(self) -> str:
        d = self.day
        return (f"{d.date} 直播間蝦幣 {d.coin_claims} 次 +{d.coin_total:.2f}｜"
                f"轉盤 {d.wheel_spins} 次 +{d.wheel_total:.2f}｜"
                f"觀看任務 {d.watch_minutes} 分鐘 +{d.watch_total:.2f}｜"
                f"提領 {d.withdrawals} 次 {d.withdrawn:.0f}｜待提領 {d.pending:.2f}"
                + ("｜有未確認提領，已暫停自動提領" if d.withdrawal_pending else ""))
