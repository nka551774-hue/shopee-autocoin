"""在手機上驗一次：ZXTouch（Dopamine 16.1.1 / rootless）到底能不能用、Vision OCR 讀到什麼。

手機上跑（NewTerm）：
    python3 probe.py                     # 只讀畫面，不點任何東西
    python3 probe.py --tap 330 500       # 順便點一下（point）
    python3 probe.py --swipe 187 600 187 150
    python3 probe.py --toast             # 試 toast
    python3 probe.py --unsupported       # 驗「這一版沒有取色／找色／shell」（會等逾時）
    python3 probe.py --raw               # 連原始回應一起印（要調格式時看這個）

它會印：
  1. 螢幕尺寸（像素）／scale／方向
  2. 整頁 OCR：前幾行（含座標，確認是像素、左上角）
  3. 任務框 OCR（point → 像素換算後）＋原始回應
  4. 「領取」按鈕那一格單獨 OCR：這是能不能自動領的關鍵
把整段輸出貼回來，就知道 screens.py 的框要不要微調。
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from screens import NAME_BOX, TASK_BOX, Shot, button_box, read_panel, read_room  # noqa: E402
from zxtouch import ZXTouch, ZXTouchError  # noqa: E402


def show(title, value=""):
    print(f"---- {title} ----")
    if value != "":
        print(value)
    print()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=6000)
    p.add_argument("--tap", nargs=2, type=float, metavar=("X", "Y"), help="point")
    p.add_argument("--swipe", nargs=4, type=float, metavar=("X1", "Y1", "X2", "Y2"), help="point")
    p.add_argument("--toast", action="store_true")
    p.add_argument("--unsupported", action="store_true", help="驗沒有實作的 task（會等到逾時）")
    p.add_argument("--raw", action="store_true", help="印出原始回應")
    p.add_argument("--watch", type=float, nargs="?", const=60, metavar="秒",
                   help="連續 OCR 任務框（預設 60 秒），文字有變就印一行：用來抓倒數→「領取」的瞬間")
    args = p.parse_args()
    if not 1 <= args.port <= 65535:
        p.error("--port 必須在 1 到 65535 之間")
    if args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
        p.error("--watch 必須是有限的正數")
    if any(not math.isfinite(value) or value < 0 for value in (args.tap or []) + (args.swipe or [])):
        p.error("座標必須是有限且非負的數值")

    print(f"連線 {args.host}:{args.port} ...")
    try:
        zx = ZXTouch(args.host, args.port)
    except OSError as e:
        print(f"連不上：{e}")
        print("→ ZXTouch tweak 裝好了嗎？裝完要 respring；socket 在 127.0.0.1:6000")
        return 1
    print("連上了\n")

    try:
        w, h = zx.screen_size()
        scale = zx.screen_scale()
        show("螢幕尺寸（像素）", f"{w:g} x {h:g}")
        show("scale", f"{scale:g}")
        show("換算成 point", f"{w / scale:g} x {h / scale:g}")
        show("方向（1=直）", zx.screen_orientation())

        s = Shot(zx)

        lines = s.lines
        show(f"整頁 OCR：{len(lines)} 行（座標是 point、中心點）",
             "\n".join(f"  ({ln.x:6.1f},{ln.y:6.1f}) {ln.text}" for ln in lines[:25]) or "(空)")

        box = s.box
        show(f"任務框 OCR：{len(box)} 行",
             "\n".join(f"  ({ln.x:6.1f},{ln.y:6.1f}) {ln.text}" for ln in box) or "(空)")

        name = s.name
        show("直播主名稱", name or "(空)")

        room = read_room(s)
        show("判讀：直播間", room)
        show("判讀：面板", read_panel(s))

        # 按鈕那一格：有「領取」就代表可以自動領
        title = next((ln for ln in box if "觀看" in ln.text), None) or next(
            (ln for ln in box if "直播間" in ln.text), None)
        if title:
            btn = s.ocr(button_box(title.y))
            show(f"按鈕那一格（標題 y={title.y:.0f}）",
                 "\n".join(f"  ({ln.x:6.1f},{ln.y:6.1f}) {ln.text}" for ln in btn) or "(空)")
            show("→ 可以領嗎", "可以（讀到「領取」）" if s.has_text(button_box(title.y), "領取")
                 else "還沒（應該是倒數）")
        else:
            print("任務框裡找不到標題，先確定手機停在有任務框的直播間\n")

        if args.raw:
            lines_raw, raw = zx.ocr(s.region(TASK_BOX))
            show("任務框原始回應", raw)

        if args.toast:
            print("送 toast ...")
            zx.toast("ZXTouch 測試：畫面讀得到我嗎？")
            time.sleep(2)

        if args.unsupported:
            # 直接送原始 payload（繞過 zxtouch.py 的 ZXUnsupported 保護），看 tweak 到底回不回
            print("驗沒有實作的 task（3 秒逾時）...")
            probes = (("取色 task 23", (23, 100, 100)),
                      ("找色 task 28", (28, 1, "0,0,100,100", 0, 255, 0, 255, 0, 255, 0)),
                      ("shell task 13", (13, "id")))
            for name, call in probes:
                probe = None
                try:
                    probe = ZXTouch(args.host, args.port, timeout=3)
                    probe._send(*call)
                    probe._recv_raw()
                    print(f"  {name}：有回應（意外，這一版應該沒有）")
                except OSError:
                    print(f"  {name}：逾時或連線失敗（不能只憑逾時判定功能不存在）")
                finally:
                    if probe:
                        probe.close()

        if args.watch:
            print(f"盯著任務框 {args.watch:g} 秒（文字有變才印）...")
            deadline = time.monotonic() + args.watch
            last = None
            while time.monotonic() < deadline:
                w = Shot(zx)
                lines = w.box
                title = next((ln for ln in lines if "觀看" in ln.text), None) or next(
                    (ln for ln in lines if "直播間" in ln.text), None)
                btn = w.ocr(button_box(title.y)) if title else []
                sig = (tuple(ln.text for ln in lines), tuple(ln.text for ln in btn))
                if sig != last:
                    last = sig
                    print(f"  [{time.strftime('%H:%M:%S')}] "
                          + " / ".join(ln.text for ln in lines)
                          + "   ｜按鈕："
                          + ("、".join(ln.text for ln in btn) or "(讀不到)")
                          + ("  ← 可以領！" if title and w.has_text(button_box(title.y), "領取") else ""))
                time.sleep(1)
            print("盯完了\n")

        if args.swipe:
            print(f"滑動 {args.swipe[:2]} → {args.swipe[2:]} ...")
            zx.swipe(*(value * scale for value in args.swipe))
            time.sleep(1)
        if args.tap:
            print(f"點 {args.tap} ...")
            zx.tap(*(value * scale for value in args.tap))
            time.sleep(1)
    except ZXTouchError as e:
        print(f"tweak 回報錯誤：{e}")
        return 1
    finally:
        zx.close()
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
