"""不用手機也能先驗一次：用假的 ZXTouch（真的 TCP socket ＋ 假的畫面）跑過解析與判讀。

    python selftest.py

它會檢查：
  1. 協議層（zxtouch.py）對 task 25 / 27 的送收與解析（像素、左上角原點）
  2. 畫面判讀（screens.py）：直播間、任務獎勵面板、提領頁、金額與倒數的解析
"""
from __future__ import annotations

import socket
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screens  # noqa: E402
import zxtouch  # noqa: E402

S = 3  # 模擬 iPhone 11 Pro：3x
FRAME: list[tuple[str, float, float, float, float]] = []  # (text, x, y, w, h) 像素、左上角


def add(text: str, cx: float, cy: float, h: float = 12.0):
    """用 point 的中心點加一行字（內部換算成 tweak 會回的像素左上角）。"""
    w = max(len(text), 1) * h * 0.95
    FRAME.append((text, cx * S - w * S / 2, cy * S - h * S / 2, w * S, h * S))


def as_dicts(region) -> list[dict]:
    x, y, w, h = region
    if not w and not h:          # tweak 的規矩：(0,0,0,0) = 整頁
        w, h = 10000, 10000
    out = []
    for text, lx, ly, lw, lh in FRAME:
        if x <= lx + lw / 2 <= x + w and y <= ly + lh / 2 <= y + h:
            out.append({"text": text, "x": lx, "y": ly, "width": lw, "height": lh})
    return out


def encode(lines: list[dict]) -> str:
    return "0;;" + ";;".join(
        "{},,{:.0f},,{:.0f},,{:.0f},,{:.0f}".format(l["text"], l["x"], l["y"], l["width"], l["height"])
        for l in lines) + "\r\n"


def serve(stop: threading.Event, ready: queue.Queue):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ready.put(srv.getsockname()[1])
    srv.settimeout(5)
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        handle(conn)
    srv.close()


def handle(conn):
    with conn:
        conn.settimeout(2)
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                text = line.decode("utf-8")
                task, parts = int(text[:2]), text[2:].split(";;")
                if task == zxtouch.TASK_DEVICE_INFO:
                    sub = int(parts[0])
                    reply = {1: "0;;1125.000000;;2436.000000",
                             2: "0;;1",
                             3: "0;;3.000000"}[sub] + "\r\n"
                elif task == zxtouch.TASK_OCR:
                    region = tuple(int(v) for v in parts[1].split(",,"))
                    reply = encode(as_dicts(region))
                else:
                    reply = "0\r\n"
                conn.sendall(reply.encode("utf-8"))


class FakeZX:
    """假連線：只回畫面內容，不碰 socket（給 screens.py 用）。"""

    def screen_scale(self):
        return float(S)

    def screen_size(self):
        return 1125.0, 2436.0

    def ocr(self, region, **kw):
        lines = as_dicts(region)
        return lines, [""]


def check(name, got, want):
    ok = got == want
    print(("  OK  " if ok else "  !!  ") + f"{name}: {got!r}" + ("" if ok else f"（應該是 {want!r}）"))
    if not ok:
        check.failed = True


check.failed = False


def protocol_test():
    print("---- 1) 協議層（真的走 socket）----")
    stop = threading.Event()
    ready = queue.Queue()
    t = threading.Thread(target=serve, args=(stop, ready), daemon=True)
    t.start()
    port = ready.get(timeout=2)
    zx = None
    for _ in range(50):
        try:
            zx = zxtouch.ZXTouch("127.0.0.1", port, timeout=5)
            break
        except OSError:
            time.sleep(0.1)
    assert zx, "連不上測試用的假伺服器"
    check("螢幕尺寸（像素）", zx.screen_size(), (1125.0, 2436.0))
    check("scale", zx.screen_scale(), 3.0)
    lines, _ = zx.ocr((0, 0, 0, 0))
    check("OCR 行數", len(lines), len(FRAME))
    check("OCR 第一行", lines[0]["text"], FRAME[0][0])
    check("OCR x 是像素左上角", lines[0]["x"], float(round(FRAME[0][1])))
    zx.toast("測試")
    zx.close()
    stop.set()
    # Wake accept without depending on its five-second timeout.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        pass
    t.join(2)


def shot_test():
    print("---- 2) 畫面判讀 ----")
    s = screens.Shot(FakeZX())
    check("point 尺寸", tuple(round(v) for v in s.size), (375, 812))
    check("point 框 → 像素 region", s.region((215, 120, 375, 420)), (645, 360, 480, 900))

    check("money 0·25", screens.money("0·25"), 0.25)
    check("money 30·02（small）", screens.money("30·02", small=True), 0.02)
    check("countdown 04:36", screens.countdown("04:36"), 276)
    check("countdown 0115", screens.countdown("0115"), 75)

    room = screens.read_room(s)
    check("畫面", screens.detect(s).value, "直播間")
    check("直播主", room.anchor, "大大")
    check("觀看任務存在", room.watch.present, True)
    check("觀看任務可以領", room.watch.claimable, True)
    check("直播間蝦幣存在", room.coin.present, True)
    check("直播間蝦幣倒數", room.coin.seconds, 276)
    check("任務框標題位置", room.banner is not None, True)


def jitter_test():
    print("---- 3) 抖動、上限與 dry-run ----")
    import datetime
    import tempfile

    import bot as botmod
    import jitter
    from autocoin.config import Config
    from autocoin.state import Store

    vals = [jitter.wobble(10, 0.18) for _ in range(500)]
    check("wobble 在 ±18% 內", all(8.2 <= v <= 11.8 for v in vals), True)
    ups = [jitter.up(10, 0.22) for _ in range(500)]
    check("up 只往上（10~12.2）", all(10 <= v <= 12.2 for v in ups), True)

    tmp = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.log_dir = str(tmp / "logs")
    cfg.state_path = str(tmp / "state.json")
    b = botmod.Bot(cfg, botmod.ZXPhone("127.0.0.1", 6000), Store(cfg.state_path))
    late = b._late_panel_at()
    check("收尾時間落在 23:35~23:50", 23 * 60 + 35 <= late.hour * 60 + late.minute <= 23 * 60 + 50, True)
    check("同一天固定不變", b._late_panel_at() == late, True)
    caps = [b._room_cap("某人", 10.5) for _ in range(50)]
    check("同一直播間的上限只抽一次", len(set(caps)), 1)
    check("上限只往上", caps[0] >= 10.5, True)

    box = (310, 154, 350, 166)   # 任務框裡「領取」的橘色範圍（point，見 screens._task）
    pts = [jitter.wobble_xy(330, 160, b.phone.tap_jitter) for _ in range(2000)]
    check("點擊抖動仍落在按鈕內", all(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in pts), True)

    phone = botmod.ZXPhone("127.0.0.1", 6000)
    phone.dry = True
    phone.tap(100, 100)      # 不該丟例外、也不該連線
    phone.swipe(10, 10, 20, 20)
    check("dry-run 不會真的連線", phone.zx, None)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # 直播間：左上角名字、任務框兩格（一格可以領、一格倒數）、分頁列、留言區
    add("推薦", 60, 60)
    add("大大", 60, 105)
    add("N 觀看者", 60, 122)
    add("蝦皮直播", 245, 130)
    add("觀看直播", 245, 149)
    add("領取", 320, 169)
    add("直播間蝦幣", 245, 210)
    add("04:36", 320, 230)
    add("說點什麼…", 120, 700)
    protocol_test()
    shot_test()

    # 任務獎勵面板
    FRAME.clear()
    add("完成任務拿獎勵", 187, 300)
    add("待提領蝦幣數", 120, 400)
    add("3·02", 120, 430)
    add("提領", 300, 425)
    add("觀看直播", 120, 500)
    add("0·21", 310, 480)
    add("領取", 310, 500)
    add("30分鐘", 150, 560)
    add("90分鐘", 300, 560)
    s = screens.Shot(FakeZX())
    p = screens.read_panel(s)
    check("面板畫面", screens.detect(s).value, "任務獎勵面板")
    check("待提領", p.pending, 3.02)
    check("提領鍵", p.withdraw, (300.0, 425.0))
    check("觀看任務可以領", p.claim, (300.0, 506.0))
    check("任務金額", p.claim_amount, 0.21)
    check("分鐘數", p.minutes, [30, 90])
    check("今天做完了", p.done, False)

    # 提領頁
    FRAME.clear()
    add("提領數值", 100, 300)
    add("5.00", 150, 400)
    add("3.00", 150, 450)
    add("立即提領", 187, 600)
    s = screens.Shot(FakeZX())
    w = screens.read_withdraw(s)
    check("提領頁畫面", screens.detect(s).value, "提領頁")
    check("提領選項", [a for a, _ in w.options], [5, 3])
    check("立即提領", w.submit, (187.0, 600.0))

    jitter_test()

    print()
    print("失敗" if check.failed else "全部通過")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
