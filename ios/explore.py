"""研究 iOS 版蝦皮用的小工具：做一連串操作，每步之後截圖存縮圖。

縮圖固定 562x1218，跟 iOS point（375x812）剛好差 1.5 倍：縮圖座標 / 1.5 = point。

用法（座標都是 point）：
    python explore.py NAME tap:327,231 wait:2 tap:187,761 swipe:187,650,187,250 shot
每個 shot 會存成 D:/tmp/ios/NAME-<n>.jpg（縮圖）與 NAME-<n>-full.jpg（原圖）。
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autocoin.state import Store  # noqa: E402

from PIL import Image

from tsclient import TouchSynthesis

OUT = "D:/tmp/ios"


def main():
    with Store(ROOT / "state-ios.json").exclusive():
        return run_session()


def run_session():
    sys.stdout.reconfigure(encoding="utf-8")
    name, *steps = sys.argv[1:]
    ts = TouchSynthesis(timeout=40)
    n = 0
    try:
        for step in steps:
            kind, _, arg = step.partition(":")
            nums = [float(v) for v in arg.split(",")] if arg else []
            if kind == "tap":
                ts.tap(*nums)
                time.sleep(1.2)
            elif kind == "swipe":
                ts.swipe(*nums[:4], duration=nums[4] if len(nums) > 4 else 0.35)
                time.sleep(1.5)
            elif kind == "wait":
                time.sleep(nums[0])
            elif kind == "shot":
                data = ts.screenshot()
                im = Image.open(io.BytesIO(data)).convert("RGB")
                im.save(f"{OUT}/{name}-{n}-full.jpg", quality=90)
                im.thumbnail((562, 1218))
                im.save(f"{OUT}/{name}-{n}.jpg", quality=85)
                print(f"{OUT}/{name}-{n}.jpg")
                n += 1
            else:
                raise ValueError(f"看不懂的步驟：{step}")
    finally:
        ts.close()


if __name__ == "__main__":
    main()
