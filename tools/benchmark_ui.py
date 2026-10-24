"""Offline benchmark: XML parsing plus repeated lookups; never contacts a phone."""
from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocoin.ui import PKG, Snapshot  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="f421c31")
    args = parser.parse_args()
    old_source = subprocess.run(["git", "show", f"{args.baseline}:autocoin/ui.py"], cwd=ROOT,
                                check=True, capture_output=True, encoding="utf-8").stdout
    baseline = types.ModuleType("baseline_ui")
    exec(compile(old_source, "baseline_ui.py", "exec"), baseline.__dict__)
    count = 1800
    xml = "<hierarchy>" + "".join(
        f'<node package="{PKG}" resource-id="item_{i}" text="text_{i}" '
        f'content-desc="desc_{i}" bounds="[0,0][100,100]"/>' for i in range(count)
    ) + "</hierarchy>"
    targets = list(range(1500, 1800, 10))

    def measure(snapshot_type):
        samples = []
        for _ in range(7):
            start = time.perf_counter()
            for _ in range(25):
                shot = snapshot_type(xml, PKG)
                for i in targets:
                    assert shot.id(f"item_{i}").text == f"text_{i}"
                    assert shot.text(f"text_{i}").desc == f"desc_{i}"
                    assert shot.desc(f"desc_{i}").rid == f"item_{i}"
            samples.append((time.perf_counter() - start) / 25)
        return statistics.median(samples)

    before, after = measure(baseline.Snapshot), measure(Snapshot)
    print(f"Python {sys.version.split()[0]}; {count} nodes; 90 lookups per snapshot")
    print(f"Baseline {args.baseline}: {before * 1000:.3f} ms/snapshot")
    print(f"Current: {after * 1000:.3f} ms/snapshot")
    print(f"Speedup: {before / after:.2f}x (includes XML parsing and index construction)")


if __name__ == "__main__":
    main()
