"""命令列入口：python -m autocoin [run|status|scan|panel]"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config
from .state import StateError, Store

ROOT = Path(__file__).resolve().parent.parent


def setup_logging(log_dir: Path, verbose: bool):
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(message)s", "%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    file = RotatingFileHandler(log_dir / "autocoin.log", maxBytes=5_000_000, backupCount=3,
                               encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("uiautomator2", "adbutils", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="autocoin", description="蝦皮直播自動領蝦幣")
    p.add_argument("command", nargs="?", default="run", choices=["run", "status", "scan", "panel"],
                   help="run：開始自動領（預設）；status：今天的統計；scan：列出「看更多」裡的直播間；"
                        "panel：開一次任務獎勵面板（領觀看任務、視情況提領）")
    p.add_argument("--serial", help="裝置序號，例如 100.112.173.34:38841")
    p.add_argument("--adb-port", type=int, help="adb server 的 port（預設 5037）")
    p.add_argument("--hours", type=float, help="跑幾個小時後停止（預設一直跑）")
    p.add_argument("--no-withdraw", action="store_true", help="不要自動提領")
    p.add_argument("-v", "--verbose", action="store_true", help="畫面上也顯示除錯訊息")
    args = p.parse_args()

    if args.hours is not None and (not math.isfinite(args.hours) or args.hours <= 0):
        p.error("--hours 必須為有限的正數")
    if args.adb_port is not None and not 1 <= args.adb_port <= 65535:
        p.error("--adb-port 必須介於 1 與 65535")

    cfg = Config()
    if args.serial:
        cfg.serial = args.serial
    if args.adb_port is not None:
        cfg.adb_port = args.adb_port
    if args.no_withdraw:
        cfg.withdraw = False
    cfg.state_path = str(ROOT / cfg.state_path)
    cfg.log_dir = str(ROOT / cfg.log_dir)
    setup_logging(Path(cfg.log_dir), args.verbose)
    log = logging.getLogger("autocoin")

    try:
        store = Store(cfg.state_path)
    except StateError as exc:
        p.exit(1, f"{exc}\n")
    if args.command == "status":
        print(store.summary())
        return

    # status / --help 不需要安裝或初始化裝置驅動。
    from .bot import Bot, StopBot
    from .device import Device, UnsafeAction

    try:
        with store.exclusive():
            bot = Bot(cfg, Device(cfg.serial, cfg.adb_host, cfg.adb_port), store)
            if args.command == "scan":
                s = bot.open_explore()
                if s is None:
                    print("打不開「看更多」")
                    return
                cards = bot.scan(bot.scroll_top(s))
                for c in sorted(cards.values(), key=lambda c: -(c.max_coins or 0)):
                    badge = f"最多 {c.max_coins:g}" if c.max_coins else ("抽獎" if c.lottery else "")
                    print(f"{badge:10s} {c.viewers:>6s}  {c.key}")
            elif args.command == "panel":
                bot.go_live()
                bot.panel_tasks()
                print(store.summary())
            else:
                bot.run(args.hours)
    except KeyboardInterrupt:
        log.info("手動停止：%s", store.summary())
    except (StopBot, UnsafeAction, StateError) as exc:
        p.exit(1, f"停止：{exc}\n")


if __name__ == "__main__":
    main()
