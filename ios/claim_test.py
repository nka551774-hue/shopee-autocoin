"""待在目前的直播間，等「直播間蝦幣」變成「領取」就點，然後連拍記錄結果。"""
from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autocoin.state import Store  # noqa: E402

from PIL import Image

import ocr
from tsclient import TouchSynthesis

TASK_BOX = (220, 120, 375, 360)
OUT = "D:/tmp/ios"


def coin_row(lines):
    """回傳 (金額文字, 狀態文字, 狀態座標)；沒有直播間蝦幣就回傳 None。"""
    title = next((ln for ln in lines if "直播間" in ln.text and "幣" in ln.text), None)
    if not title:
        return None
    below = [ln for ln in lines if 8 < ln.y - title.y < 40]
    state = next((ln for ln in below if ln.x > 300), None)
    amount = next((ln for ln in below if ln.x <= 300), None)
    return (amount.text if amount else "?", state.text if state else "?",
            (state.x, state.y) if state else None)


def shot(ts):
    return Image.open(io.BytesIO(ts.screenshot())).convert("RGB")


def main():
    with Store(ROOT / "state-ios.json").exclusive():
        return run_session()


def run_session():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    deadline = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 600)
    ts = TouchSynthesis(timeout=40)
    try:
        last = None
        while time.time() < deadline:
            im = shot(ts)
            row = coin_row(ocr.read(im, TASK_BOX, upscale=2))
            if row != last:
                print(time.strftime("%H:%M:%S"), row)
                last = row
            if row and "領" in row[1] and row[2]:
                im.save(f"{OUT}/claim-before.jpg", quality=92)
                ts.tap(*row[2])
                t = time.time()
                for i, delay in enumerate((0.4, 1.2, 2.5, 4.5)):
                    while time.time() - t < delay:
                        time.sleep(0.05)
                    after = shot(ts)
                    after.save(f"{OUT}/claim-after{i}.jpg", quality=92)
                    texts = [ln.text for ln in ocr.read(after)]
                    hits = [s for s in texts if any(k in s for k in ("成功", "Coins", "待提領", "失敗", "上限", "風險"))]
                    print(f"  +{delay}s coin={coin_row(ocr.read(after, TASK_BOX, upscale=2))} msg={hits}")
                return
            if row is None:
                print(time.strftime("%H:%M:%S"), "直播間蝦幣不見了")
            m = re.search(r"(\d+):(\d\d)", row[1]) if row else None
            secs = int(m[1]) * 60 + int(m[2]) if m else 5
            time.sleep(min(max(secs - 3, 1), 20))
    finally:
        ts.close()


if __name__ == "__main__":
    main()
