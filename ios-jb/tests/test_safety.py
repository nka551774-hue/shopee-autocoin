"""Offline regression tests; never connects to a phone or starts the bot process.

Run: python -m unittest discover -s ios-jb/tests -v
"""
from __future__ import annotations

import http.client
import io
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))

import bot
import probe
import screens
import serve
import zxtouch
from autocoin.config import Config
from autocoin import state as state_module
from autocoin.state import StateError, Store, TAIPEI


class ProbeSafetyTests(unittest.TestCase):
    def test_probe_rejects_existing_session_before_connecting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = Store(root / "state-ios.json")
            state.save()
            original = state.path.read_bytes()
            with state.exclusive(), patch.object(probe, "ROOT", root, create=True), \
                    patch.object(sys, "argv", ["probe.py", "--tap", "1", "2"]), \
                    patch.object(probe, "ZXTouch", side_effect=AssertionError("device connected")) as connect:
                with self.assertRaises(StateError):
                    probe.main()
            connect.assert_not_called()
            self.assertEqual(state.path.read_bytes(), original)

    def test_malformed_amounts_and_options_are_rejected(self):
        for text in ("-3.00", "3.001", "3.00.5", "9" * 400 + ".00"):
            with self.subTest(text=text):
                self.assertIsNone(screens.money(text))
        shot = SimpleNamespace(lines=[screens.Line("提領數值", 100, 100),
                                     screens.Line("立即提領", 100, 600)]
                               + [screens.Line(text, 100, 200)
                                  for text in ("-5.00", "1.5.00", "說5.00", "0.00")])
        shot.find = lambda *keys: next((ln for ln in shot.lines if any(k in ln.text for k in keys)), None)
        self.assertEqual(screens.read_withdraw(shot).options, [])


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        log = Path(self.tmp.name) / "console.log"
        log.write_text("<script>alert(1)</script>\n", encoding="utf-8")
        self.patchers = [patch.object(serve, "CONSOLE_LOG", log),
                         patch.object(serve, "start_bot", return_value="started"),
                         patch.object(serve, "stop_bot", return_value="stopped")]
        self.mocks = [p.start() for p in self.patchers]
        self.addCleanup(lambda: [p.stop() for p in self.patchers])
        self.server = serve.LocalServer(("127.0.0.1", 0), serve.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read().decode("utf-8")
        finally:
            c.close()

    def test_get_start_and_stop_are_read_only(self):
        self.assertEqual(self.request("GET", "/start")[0], 200)
        self.assertEqual(self.request("GET", "/stop")[0], 200)
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_post_requires_token_and_same_origin(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("POST", "/start", "token=wrong", headers)[0], 403)
        self.assertEqual(self.request("POST", "/start", "token=%E4%B8%AD", headers)[0], 403)
        body = urlencode({"token": serve._csrf_token})
        evil = {**headers, "Origin": "https://attacker.example"}
        self.assertEqual(self.request("POST", "/start", body, evil)[0], 403)
        self.assertEqual(self.request("POST", "/start", body, {**headers, "Origin": "null"})[0], 403)
        self.assertEqual(self.request("POST", "/start", body + "&" + body, headers)[0], 403)
        cross = {**headers, "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("POST", "/start", body, cross)[0], 403)
        self.mocks[1].assert_not_called()
        self.assertEqual(self.request("POST", "/start", body, headers)[0], 200)
        self.mocks[1].assert_called_once()

    def test_dns_rebinding_host_is_rejected(self):
        self.assertEqual(self.request("GET", "/", headers={"Host": "attacker.example"})[0], 403)

    def raw_request(self, request):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as conn:
            conn.sendall(request)
            conn.shutdown(socket.SHUT_WR)
            return conn.recv(4096)

    def test_duplicate_host_and_truncated_body_cannot_trigger_actions(self):
        port = self.server.server_port
        request = f"GET / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nHost: attacker.example\r\n\r\n"
        self.assertIn(b"403", self.raw_request(request.encode("ascii")).split(b"\r\n", 1)[0])
        body = urlencode({"token": serve._csrf_token})
        request = (f"POST /start HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body) + 3}\r\n\r\n{body}")
        self.assertIn(b"400", self.raw_request(request.encode("ascii")).split(b"\r\n", 1)[0])
        self.mocks[1].assert_not_called()

    def test_logs_cannot_execute_as_html(self):
        _, headers, body = self.request("GET", "/status")
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("<script>", body)
        _, _, page = self.request("GET", "/")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_body_and_field_counts_are_bounded(self):
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        self.assertEqual(self.request("POST", "/start", "x" * 2049, h)[0], 413)
        self.assertEqual(self.request("POST", "/start", "&".join(f"a{i}=b" for i in range(9)), h)[0], 400)

    def test_shell_stop_uses_authenticated_stop_and_start_is_idempotent(self):
        with patch.object(serve, "PORT", self.server.server_port), patch.object(serve.subprocess, "Popen") as spawn:
            serve.request_stop()
            serve.start_server()
        self.mocks[2].assert_called_once()
        spawn.assert_not_called()


class ProcessTests(unittest.TestCase):
    def test_foreground_launch_respects_existing_device_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with Store(root / "state-ios.json").exclusive(), patch.object(serve, "ROOT", root), \
                    patch.object(zxtouch, "ZXTouch") as connect:
                serve.bring_shopee_front()
            connect.assert_not_called()

    def test_idle_controller_clears_old_stop_and_releases_state_lock_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stop = root / "bot.stop"
            stop.touch()

            def spawn(*args, **kwargs):
                self.assertFalse(stop.exists())
                with Store(root / "state-ios.json").exclusive():
                    return SimpleNamespace(poll=lambda: None)

            with (patch.multiple(serve, ROOT=root, LOGS=root, STOP_FILE=stop,
                                 _proc=None, _starting=False, _stop_wanted=False),
                  patch.object(serve.subprocess, "Popen", side_effect=spawn) as child,
                  patch.object(serve.threading, "Thread")):
                self.assertEqual(serve.start_bot_now(), "開始了")
                child.assert_called_once()

    def test_restarted_controller_cannot_clear_an_active_owners_stop_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stop = root / "bot.stop"
            stop.touch()
            script = """
import sys
from autocoin.state import Store
with Store(sys.argv[1]).exclusive():
    print('locked', flush=True)
    sys.stdin.read()
"""
            owner = subprocess.Popen(
                [sys.executable, "-c", script, str(root / "state-ios.json")],
                cwd=HERE.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline().strip(), "locked")
                with (patch.multiple(serve, ROOT=root, LOGS=root, STOP_FILE=stop,
                                     _proc=None, _starting=False, _stop_wanted=False),
                      patch.object(serve.subprocess, "Popen") as spawn,
                      patch.object(serve.threading, "Thread")):
                    serve.start_bot_now()
                    self.assertTrue(stop.exists(), "an orphan still owns the state; its stop request must survive")
                    spawn.assert_not_called()
            finally:
                owner.communicate(timeout=5)

    def test_parallel_starts_and_stop_during_startup_create_no_child(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()

        def foreground():
            entered.set()
            release.wait(2)

        with tempfile.TemporaryDirectory() as tmp:
            with (patch.multiple(serve, _proc=None, _starting=False, _stopping=None,
                                 _stop_wanted=False, _cancel_start=cancelled,
                                 LOGS=Path(tmp), STOP_FILE=Path(tmp) / "stop"),
                  patch.object(serve, "bring_shopee_front", side_effect=foreground) as front,
                  patch.object(serve, "start_bot_now") as start):
                try:
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        results = list(pool.map(lambda _: serve.start_bot(), range(24)))
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(sum("開始中" in r for r in results), 1)
                    serve.stop_bot()
                    serve.start_bot()  # A new request cannot revive the cancelled in-flight startup.
                finally:
                    release.set()
                    deadline = time.monotonic() + 1
                    while serve._starting and time.monotonic() < deadline:
                        time.sleep(0.01)
                self.assertFalse(serve._starting)
                front.assert_called_once()
                start.assert_not_called()

    def test_cancelled_start_does_not_foreground_or_spawn(self):
        cancelled = threading.Event()
        cancelled.set()
        with (patch.object(serve, "_cancel_start", cancelled),
              patch.object(serve, "bring_shopee_front") as front,
              patch.object(serve, "start_bot_now") as start):
            serve._start_flow()
        front.assert_not_called()
        start.assert_not_called()

    def test_repeated_stop_uses_one_worker_for_captured_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Mock()
            proc.poll.return_value = None
            with patch.multiple(serve, LOGS=Path(tmp), STOP_FILE=Path(tmp) / "stop",
                                _proc=proc, _stopping=None, _stop_wanted=False), patch.object(serve.threading, "Thread") as thread:
                serve.stop_bot()
                serve.stop_bot()
                self.assertEqual(thread.call_count, 1)
                self.assertEqual(thread.call_args.kwargs["args"], (proc,))
                self.assertTrue((Path(tmp) / "stop").exists())

    def test_stale_stop_worker_never_kills_new_process(self):
        old, new = Mock(), Mock()
        old.wait.side_effect = [subprocess.TimeoutExpired("old", 90), None]
        with patch.multiple(serve, _proc=new, _stopping=old):
            serve._wait_kill(old)
        old.terminate.assert_called_once()
        new.terminate.assert_not_called()
        new.kill.assert_not_called()

    def test_console_output_and_tail_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "console.log"
            proc = SimpleNamespace(stdout=io.BytesIO((b"x" * 8100 + b"\n") * 400))
            with patch.object(serve, "CONSOLE_LOG", path):
                serve._copy_output(proc)
            logs = list(Path(tmp).glob("console.log*"))
            self.assertLessEqual(len(logs), 3)
            self.assertTrue(all(p.stat().st_size <= 1_008_192 for p in logs))
            self.assertLessEqual(len(serve.tail(path)), 65536)


class ProtocolTests(unittest.TestCase):
    def connect(self, timeout=0.5):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        with patch.object(zxtouch.socket, "create_connection", return_value=client):
            zx = zxtouch.ZXTouch(timeout=timeout)
        return zx, peer

    def test_coalesced_frames_are_preserved(self):
        zx, peer = self.connect()
        peer.sendall(b"0;;3\r\n0;;1\r\n")
        self.assertEqual(zx.screen_scale(), 3)
        self.assertEqual(zx.screen_orientation(), 1)

    def test_fragmented_frame_can_reach_exact_size_limit(self):
        zx, peer = self.connect()
        zx.MAX_RESPONSE = 16
        peer.sendall(b"x" * 16 + b"\r")
        timer = threading.Timer(0.02, lambda: peer.sendall(b"\n"))
        timer.start()
        try:
            self.assertEqual(zx._recv_raw(), "x" * 16)
        finally:
            timer.join()

    def test_touch_is_released_when_sleep_is_interrupted(self):
        zx, _ = self.connect()
        with patch.object(zx, "touch") as touch, patch.object(zxtouch.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                zx.tap(10, 20)
        self.assertEqual([c.args[0] for c in touch.call_args_list], [zxtouch.TOUCH_DOWN, zxtouch.TOUCH_UP])

    def test_response_limit_and_eof_fail_closed(self):
        zx, peer = self.connect()
        zx.MAX_RESPONSE = 16
        peer.sendall(b"0" * 18)
        with self.assertRaises(zxtouch.ZXConnectionError):
            zx._recv_raw()
        self.assertEqual(zx.s.fileno(), -1)
        zx, peer = self.connect()
        peer.close()
        with self.assertRaises(zxtouch.ZXConnectionError):
            zx._recv_raw()

    def test_slow_drip_cannot_extend_absolute_timeout(self):
        zx, peer = self.connect(timeout=0.08)
        stop = threading.Event()

        def drip():
            while not stop.wait(0.015):
                try:
                    peer.sendall(b"x")
                except OSError:
                    return

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        start = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                zx._recv_raw()
        finally:
            stop.set()
            thread.join(1)
        self.assertLess(time.monotonic() - start, 0.3)

    def test_protocol_injection_is_rejected_before_send(self):
        zx, peer = self.connect()
        for content in ("text\r\n11bad.app", "text;;extra", "text\n11bad.app"):
            with self.assertRaises(ValueError):
                zx.toast(content)
        peer.settimeout(0.02)
        with self.assertRaises(TimeoutError):
            peer.recv(1)

    def test_invalid_status_and_touch_coordinates_fail_closed(self):
        zx, peer = self.connect()
        peer.sendall(b"0123;;3\r\n")
        with self.assertRaises(zxtouch.ZXConnectionError):
            zx.screen_scale()
        zx, peer = self.connect()
        for value in (-1, float("nan"), float("inf"), 10000):
            with self.assertRaises(ValueError):
                zx.touch(zxtouch.TOUCH_DOWN, 1, value, 1)
        with self.assertRaises(ValueError):
            zx.swipe(1, 1, 2, 2, steps=0)

    def test_invalid_utf8_cannot_become_trusted_ocr_text(self):
        zx, peer = self.connect()
        peer.sendall(b"0;;risk\xffwarning,,10,,20,,30,,40\r\n")
        with self.assertRaises(zxtouch.ZXConnectionError):
            zx.ocr((0, 0, 0, 0))
        self.assertEqual(zx.s.fileno(), -1)

    def test_malformed_ocr_record_cannot_return_a_partial_screen(self):
        for broken in ("warning", "warning,,NaN,,20,,30,,40", "warning,,10,,20,,-1,,40"):
            with self.subTest(broken=broken):
                zx, peer = self.connect()
                peer.sendall(f"0;;Claim,,10,,20,,30,,40;;{broken}\r\n".encode())
                with self.assertRaises(zxtouch.ZXConnectionError):
                    zx.ocr((0, 0, 0, 0))
                self.assertEqual(zx.s.fileno(), -1)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(log_dir=str(Path(self.tmp.name) / "logs"), withdraw=False)
        self.store = Store(Path(self.tmp.name) / "state.json")
        self.phone = Mock()
        self.bot = bot.Bot(self.cfg, self.phone, self.store)
        self.shot = Mock()
        self.shot.texts.return_value = []
        self.shot.find.return_value = None
        self.shot.lines = []
        self.phone.shot.return_value = self.shot

    def test_unknown_screen_cannot_dismiss_an_unrelated_rating_popup(self):
        self.shot.lines = [screens.Line("稍後再說", 100, 300)]
        self.bot.pause = Mock()
        with patch.object(bot, "detect", return_value=screens.Screen.UNKNOWN):
            self.assertEqual(self.bot.look()[0], screens.Screen.UNKNOWN)
        self.phone.tap.assert_not_called()

    def test_old_reward_notice_cannot_confirm_a_new_watch_claim(self):
        self.shot.lines = [screens.Line("待提領蝦幣數", 100, 300)]
        self.shot.find.return_value = screens.Line("0.21已發放至待提領蝦幣數", 100, 300)
        self.bot.pause = Mock()
        self.bot.capture = Mock()
        self.bot.capture_once = Mock()
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.shot))
        self.bot.back_to_room = Mock()
        before = screens.Panel(pending=3, claim=(300, 500), claim_amount=.21)
        with patch.object(bot, "read_panel", side_effect=[before, screens.Panel(pending=3)]):
            with self.assertRaises(bot.StopBot):
                self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0)
        self.phone.tap.assert_called_once_with(300, 500)

    def test_restarted_countdown_is_not_recorded_as_success(self):
        before = screens.Room(coin=screens.Task(present=True, claimable=True, button=(1, 2)))
        after = screens.Room(coin=screens.Task(present=True, state="05:00"))
        self.bot.pause = Mock()
        self.bot.capture = Mock()
        with (patch.object(bot, "read_room", return_value=after),
              patch.object(bot.time, "monotonic", side_effect=[0, 1, 7])):
            with self.assertRaises(bot.StopBot):
                self.bot.claim(before, "room")
        self.assertEqual(self.store.day.coin_claims, 0)
        self.assertFalse(self.store.path.exists())

    def test_daily_coin_limit_prevents_another_touch(self):
        self.store.day.coin_claims = 100
        before = screens.Room(coin=screens.Task(present=True, claimable=True, button=(1, 2)))
        self.bot.pause = Mock()
        with patch.object(bot, "read_room", return_value=screens.Room(claimed="成功領取0.25蝦幣")):
            self.bot.claim(before, "room")
        self.phone.tap.assert_not_called()
        self.assertEqual(self.store.day.coin_claims, 100)

    def test_final_panel_crossing_midnight_resumes_new_day(self):
        now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
        with (patch.object(state_module, "reward_now", side_effect=lambda: now[0]),
              patch.object(bot, "reward_now", side_effect=lambda: now[0])):
            self.bot.store = Store(Path(self.tmp.name) / "midnight.json")
            self.bot.go_room = Mock()
            self.bot.panel_tasks = Mock(side_effect=lambda **_: now.__setitem__(0, now[0] + timedelta(minutes=2)))
            self.bot.pause = Mock(side_effect=AssertionError("must not sleep through the new reward day"))
            self.bot.rest_until_tomorrow()
            self.bot.pause.assert_not_called()
            self.assertFalse(self.bot.day.final_panel)

    def test_panel_screen_change_prevents_claim_touch(self):
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.shot))
        self.bot.pause = Mock()
        self.bot.capture = Mock()
        self.bot.capture_once = Mock()
        self.bot.back_to_room = Mock()
        with (patch.object(bot, "detect", return_value=screens.Screen.UNKNOWN),
              patch.object(bot, "read_panel", return_value=screens.Panel(claim=(1, 2)))):
            with self.assertRaises(bot.StopBot):
                self.bot.panel_tasks()
        self.phone.tap.assert_not_called()

    def test_cancel_interrupts_long_sleep_and_prevents_cleanup_actions(self):
        stop = Path(self.tmp.name) / "stop"
        self.bot.stop_file = stop
        timer = threading.Timer(0.03, stop.touch)
        timer.start()
        start = time.monotonic()
        with self.assertRaises(bot.StopBot):
            self.bot.pause(300)
        timer.join()
        self.assertLess(time.monotonic() - start, 0.5)
        self.bot.panel_tasks = Mock()
        self.bot.run()
        self.bot.panel_tasks.assert_not_called()
        self.phone.tap.assert_not_called()

    def test_ambiguous_touch_is_never_replayed(self):
        phone = bot.ZXPhone()
        phone.scale = 3
        phone.zx = Mock()
        old = phone.zx
        old.tap.side_effect = OSError("disconnected after send")
        with patch.object(bot, "ZXTouch") as reconnect:
            with self.assertRaises(bot.StopBot):
                phone.tap(100, 100)
        old.tap.assert_called_once()
        reconnect.assert_not_called()

    def test_screen_metadata_is_cached_between_snapshots(self):
        phone = bot.ZXPhone()
        phone.zx = Mock()
        phone.zx.screen_size.return_value = (1125, 2436)
        phone.zx.screen_scale.return_value = 3
        self.assertEqual(phone.shot().size, (375, 812))
        self.assertEqual(phone.shot().size, (375, 812))
        phone.zx.screen_size.assert_called_once()
        phone.zx.screen_scale.assert_called_once()

    def test_probe_point_coordinates_are_scaled_to_pixels(self):
        import probe
        zx = Mock()
        zx.screen_size.return_value = (1125, 2436)
        zx.screen_scale.return_value = 3
        zx.ocr.return_value = ([], [])
        with (patch.object(probe, "ZXTouch", return_value=zx), patch.object(probe.time, "sleep"),
              patch.object(probe, "ROOT", Path(self.tmp.name)),
              patch("builtins.print"),
              patch.object(sys, "argv", ["probe.py", "--tap", "330", "500", "--swipe", "1", "2", "3", "4"])):
            self.assertEqual(probe.main(), 0)
        zx.tap.assert_called_once_with(990, 1500)
        zx.swipe.assert_called_once_with(3, 6, 9, 12)

    def test_dry_panel_does_not_mutate_state(self):
        self.bot.dry = True
        self.bot.report = Mock()
        self.bot.panel_tasks()
        self.assertFalse(self.store.path.exists())
        self.phone.tap.assert_not_called()
        self.bot.report.assert_called_once()

    def test_unknown_screen_never_receives_a_blind_back_tap(self):
        self.bot.look = Mock(return_value=(screens.Screen.UNKNOWN, self.shot))
        self.bot.pause = Mock()
        self.bot.capture = Mock()
        self.assertFalse(self.bot.back_to_room(screens.Screen.UNKNOWN, self.shot))
        self.phone.tap.assert_not_called()

    def test_failed_watch_claim_is_not_counted(self):
        self.shot.lines = [screens.Line("待提領蝦幣數", 100, 300)]
        self.bot.pause = Mock()
        self.bot.capture_once = Mock()
        self.bot.capture = Mock()
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.shot))
        self.bot.back_to_room = Mock()
        before = screens.Panel(pending=3, claim=(300, 500), claim_amount=0.21)
        after = screens.Panel(pending=3)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            with self.assertRaises(bot.StopBot):
                self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0)

    def test_confirmed_watch_claim_counts_actual_balance_change(self):
        self.shot.lines = [screens.Line("待提領蝦幣數", 100, 300)]
        self.bot.pause = Mock()
        self.bot.capture_once = Mock()
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.shot))
        self.bot.back_to_room = Mock()
        before = screens.Panel(pending=3, claim=(300, 500), claim_amount=0.99)
        after = screens.Panel(pending=3.21)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0.21)

    def test_pending_withdrawal_still_allows_confirmed_watch_rewards(self):
        self.shot.lines = [screens.Line("待提領蝦幣數", 100, 300)]
        self.cfg.withdraw = True
        self.store.day.withdrawal_pending = True
        self.bot.pause = Mock()
        self.bot.capture_once = Mock()
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.shot))
        self.bot.back_to_room = Mock()
        self.bot.withdraw = Mock()
        before = screens.Panel(pending=10, claim=(300, 500), claim_amount=0.21)
        after = screens.Panel(pending=10.21, withdraw=(300, 400))
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0.21)
        self.assertTrue(self.store.day.withdrawal_pending)
        self.bot.withdraw.assert_not_called()

    def prepare_withdraw(self):
        self.cfg.withdraw = True
        self.bot.pause = Mock()
        self.bot.capture_once = Mock()
        self.bot.capture = Mock()
        withdrawal = Mock()
        withdrawal.lines = []
        withdrawal.texts.return_value = []
        withdrawal.find.return_value = None
        self.bot.wait_for = Mock(return_value=(screens.Screen.WITHDRAW, withdrawal))
        self.bot.look = Mock(side_effect=[(screens.Screen.PANEL, self.shot)]
                             + [(screens.Screen.WITHDRAW, withdrawal)] * 12)
        self.store.day.pending = 10
        page = screens.WithdrawPage(pending=10, options=[(5, (1, 2))], submit=(3, 4))
        return page, withdrawal

    def test_unconfirmed_withdrawal_is_persisted_before_submit(self):
        page, _ = self.prepare_withdraw()

        def tap(x, y):
            if (x, y) == (3, 4):
                self.assertTrue(Store(self.store.path).day.withdrawal_pending)

        self.phone.tap.side_effect = tap
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertFalse(self.bot.should_withdraw(10, final=True))
        self.phone.tap.reset_mock()
        self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.phone.tap.assert_not_called()

    def test_missing_current_balance_never_uses_saved_balance(self):
        page, _ = self.prepare_withdraw()
        page.pending = None  # Saved state still says 10, but this screen does not.
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual([c.args for c in self.phone.tap.call_args_list], [(5, 6)])
        self.assertFalse(self.store.day.withdrawal_pending)

    def test_balance_must_be_read_again_before_submit(self):
        page, _ = self.prepare_withdraw()
        unknown = screens.WithdrawPage(pending=None, options=page.options, submit=page.submit)
        with (patch.object(bot, "read_withdraw", side_effect=[page, unknown]),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual([c.args for c in self.phone.tap.call_args_list], [(5, 6), (1, 2)])
        self.assertFalse(self.store.day.withdrawal_pending)

    def test_changed_eligibility_after_amount_selection_prevents_submit(self):
        for change in ("disabled", "limit", "option"):
            with self.subTest(change=change):
                self.phone.tap.reset_mock()
                self.store.day.withdrawals = 0
                self.store.day.withdrawal_pending = False
                page, _ = self.prepare_withdraw()
                selected = screens.WithdrawPage(pending=10, options=page.options, submit=page.submit)

                def tap(x, y):
                    if (x, y) == (1, 2):
                        if change == "disabled":
                            self.cfg.withdraw = False
                        elif change == "limit":
                            self.store.day.withdrawals = 3
                        else:
                            selected.options = []

                self.phone.tap.side_effect = tap
                with (patch.object(bot, "read_withdraw", side_effect=[page] + [selected] * 11),
                      patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
                    self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
                self.assertEqual([c.args for c in self.phone.tap.call_args_list], [(5, 6), (1, 2)])
                self.assertFalse(self.store.day.withdrawal_pending)

    def test_generic_confirmation_is_never_tapped(self):
        page, shot = self.prepare_withdraw()
        shot.lines = [screens.Line("確認", 90, 90), screens.Line("確定", 95, 95)]
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        taps = [c.args for c in self.phone.tap.call_args_list]
        self.assertNotIn((90, 90), taps)
        self.assertNotIn((95, 95), taps)
        self.assertTrue(self.store.day.withdrawal_pending)

    def test_explicit_withdrawal_confirmation_is_tapped_only_once(self):
        page, shot = self.prepare_withdraw()
        shot.lines = [screens.Line("確認提領", 90, 90)]
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual([c.args for c in self.phone.tap.call_args_list].count((90, 90)), 1)
        self.assertTrue(self.store.day.withdrawal_pending)

    def test_instructional_limit_text_does_not_clear_pending_withdrawal(self):
        page, shot = self.prepare_withdraw()
        shot.lines = [screens.Line("每日提領上限三次", 100, 300)]
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual(self.store.day.withdrawals, 0)
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)

    def test_explicit_refusal_clears_pending_without_pressing_confirmation(self):
        page, safe = self.prepare_withdraw()
        rejected = Mock()
        rejected.lines = [screens.Line("您已達提領次數上限", 100, 300), screens.Line("確認提領", 90, 90)]
        rejected.texts.return_value = ["您已達提領次數上限"]
        self.bot.look.side_effect = [(screens.Screen.PANEL, self.shot)] + [
            (screens.Screen.WITHDRAW, s) for s in (safe, safe, rejected)]
        with (patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual(self.store.day.withdrawals, self.cfg.withdraw_daily_limit)
        self.assertFalse(Store(self.store.path).day.withdrawal_pending)
        self.assertNotIn((90, 90), [c.args for c in self.phone.tap.call_args_list])

    def check_withdrawal_risk(self, safe_frames, expected_taps):
        page, safe = self.prepare_withdraw()
        del self.bot.look  # Exercise the real screenshot + risk-check path.
        risky = Mock()
        risky.lines = []
        risky.texts.return_value = ["偵測到風險活動"]
        self.phone.shot.side_effect = [self.shot, *([safe] * (safe_frames - 1)), risky] if safe_frames else [risky]
        with (patch.object(bot, "detect", side_effect=[screens.Screen.PANEL] + [screens.Screen.WITHDRAW] * safe_frames),
              patch.object(bot, "read_withdraw", return_value=page),
              patch.object(bot, "read_panel", return_value=screens.Panel(withdraw=(5, 6)))):
            with self.assertRaises(bot.StopBot):
                self.bot.withdraw(screens.Panel(withdraw=(5, 6)))
        self.assertEqual([c.args for c in self.phone.tap.call_args_list], expected_taps)
        self.assertFalse(self.store.day.withdrawal_pending)

    def test_risk_on_panel_stops_before_opening_withdrawal(self):
        self.check_withdrawal_risk(0, [])

    def test_risk_on_first_withdrawal_snapshot_stops_before_selection(self):
        self.check_withdrawal_risk(1, [(5, 6)])

    def test_risk_after_selection_stops_before_submit(self):
        self.check_withdrawal_risk(2, [(5, 6), (1, 2)])

    def test_state_failure_is_not_retried(self):
        self.bot.cycle = Mock(side_effect=StateError("disk full"))
        with self.assertRaises(StateError):
            self.bot.run()
        self.bot.cycle.assert_called_once()

    def test_interrupted_watch_is_not_marked_completed(self):
        self.bot.finish_room("room", "lost", "watch")
        self.assertFalse(self.store.day.room("room")["watched"])

    def test_parsers_reject_malformed_values_and_blind_withdraw(self):
        self.assertIsNone(screens.countdown("12:99"))
        self.assertIsNone(screens.money("-3.00"))
        self.assertIsNone(screens.money("3.001"))
        shot = SimpleNamespace(lines=[screens.Line("待提領蝦幣數", 100, 300)],
                               find=lambda *a, **kw: screens.Line("待提領蝦幣數", 100, 300),
                               has_text=lambda *a: False)
        self.assertIsNone(screens.read_panel(shot).withdraw)


if __name__ == "__main__":
    unittest.main()
