"""往上滑換台，用 OCR 看右上角任務框，找到有「直播間蝦幣」的直播間就停。"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autocoin.state import Store  # noqa: E402

from PIL import Image

import ocr
from tsclient import TouchSynthesis

TASK_BOX = (220, 120, 375, 360)  # 右上角任務框（含下方的轉盤／福袋圖示），iOS point


def main():
    with Store(ROOT / "state-ios.json").exclusive():
        return run_session()


def run_session():
    sys.stdout.reconfigure(encoding="utf-8")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    ts = TouchSynthesis(timeout=40)
    try:
        for i in range(limit):
            data = ts.screenshot()
            im = Image.open(io.BytesIO(data)).convert("RGB")
            top = [ln.text for ln in ocr.read(im, (0, 90, 200, 135))]
            box = [f"{ln.text}@({ln.x:.0f},{ln.y:.0f})" for ln in ocr.read(im, TASK_BOX, upscale=2)]
            print(f"[{i}] {' '.join(top)[:30]} | {' / '.join(box)}")
            if any("直播間" in t and "幣" in t for t in box):
                im.save("D:/tmp/ios/coin-room.jpg", quality=92)
                print("找到直播間蝦幣，畫面存在 D:/tmp/ios/coin-room.jpg")
                return
            ts.swipe(187, 600, 187, 150, 0.25)
            time.sleep(3)
        print("沒找到")
    finally:
        ts.close()


if __name__ == "__main__":
    main()
