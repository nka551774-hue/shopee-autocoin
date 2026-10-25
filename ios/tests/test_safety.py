"""Offline regression tests; no real phone, OCR service, or account is contacted."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import ocr
import screens
import tsclient
from autocoin.config import Config
from autocoin import state as state_module
from autocoin.state import Store, TAIPEI
from PIL import Image


class FakeSocket:
    def __init__(self, response=None):
        self.response = response
        self.buffer = bytearray()
        self.sent = []
        self.closed = False

    def sendall(self, payload):
        command = json.loads(payload[4:])
        self.sent.append(command)
        response = self.response(command) if self.response else {
            "id": command["id"], "success": True}
        data = response if isinstance(response, bytes) else json.dumps(response).encode()
        self.buffer.extend(struct.pack(">I", len(data)) + data)

    def recv(self, n):
        out = bytes(self.buffer[:min(n, 7)])
        del self.buffer[:len(out)]
        return out

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def client(self, fake):
        with patch.object(tsclient.socket, "create_connection", return_value=fake):
            return tsclient.TouchSynthesis(timeout=1)

    def test_fragmented_reply_is_reassembled(self):
        sock = FakeSocket()
        client = self.client(sock)
        self.assertTrue(client.call("tap", x=12, y=30)["success"])
        self.assertEqual(len(sock.sent), 1)

    def test_untrusted_length_is_rejected_before_body_read(self):
        sock = FakeSocket()
        client = self.client(sock)
        sock.sendall = lambda _: sock.buffer.extend(struct.pack(">I", 0xffffffff))
        with self.assertRaises(tsclient.ProtocolError):
            client.call("screenshot")
        self.assertTrue(sock.closed)

    def test_malformed_json_object_closes_connection(self):
        sock = FakeSocket(lambda _: b"[]")
        with self.assertRaises(tsclient.ProtocolError):
            self.client(sock).call("tap")
        self.assertTrue(sock.closed)

    def test_slow_fragment_stream_cannot_extend_total_deadline(self):
        sock = FakeSocket()
        client = self.client(sock)
        with patch.object(tsclient.time, "monotonic", side_effect=[0, .1, .2, .8, 1.1]):
            with self.assertRaises(TimeoutError):
                client.call("screenshot")
        self.assertTrue(sock.closed)

    def test_string_success_flag_is_not_accepted(self):
        sock = FakeSocket(lambda cmd: {"id": cmd["id"], "success": "false"})
        with self.assertRaises(RuntimeError):
            self.client(sock).call("tap")

    def test_malformed_base64_is_rejected(self):
        sock = FakeSocket(lambda cmd: {"id": cmd["id"], "success": True, "data": "%%"})
        with self.assertRaises(tsclient.ProtocolError):
            self.client(sock).screenshot()
        self.assertTrue(sock.closed)

    def test_mismatched_reply_id_closes_connection_without_replay(self):
        sock = FakeSocket(lambda _: {"id": "another-command", "success": True})
        with self.assertRaises(tsclient.ProtocolError):
            self.client(sock).call("tap")
        self.assertTrue(sock.closed)
        self.assertEqual(len(sock.sent), 1)

    def test_malformed_tree_payload_closes_connection(self):
        for payload in (None, "not json", "[]", {}):
            with self.subTest(payload=payload):
                sock = FakeSocket(lambda cmd: {"id": cmd["id"], "success": True, "data": payload})
                with self.assertRaises(tsclient.ProtocolError):
                    self.client(sock).tree("test.app")
                self.assertTrue(sock.closed)

    def test_queue_timeout_does_not_close_another_commands_socket(self):
        sock = FakeSocket()
        client = self.client(sock)
        client.timeout = .02
        acquired, release = threading.Event(), threading.Event()

        def hold_lock():
            with client._lock:
                acquired.set()
                release.wait(5)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        try:
            self.assertTrue(acquired.wait(2))
            with self.assertRaises(TimeoutError):
                client.call("tap")
            self.assertFalse(sock.closed)
            self.assertEqual(sock.sent, [])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(client.call("tap")["success"])

    def test_queue_wait_counts_toward_command_deadline(self):
        sock = FakeSocket()
        client = self.client(sock)
        client._lock = Mock()
        with patch.object(tsclient.time, "monotonic", side_effect=[0, 1.1]):
            with self.assertRaises(TimeoutError):
                client.call("tap")
        self.assertEqual(sock.sent, [])
        self.assertTrue(sock.closed)
        client._lock.acquire.assert_called_once_with(timeout=1)
        client._lock.release.assert_called_once()

    def test_stream_grab_skips_old_frame_and_stops_once(self):
        sock = FakeSocket()
        send = sock.sendall

        def stream_send(payload):
            send(payload)
            if json.loads(payload[4:])["action"] == "startStream":
                for frame in (b"\xffold", b"\xffnew"):
                    sock.buffer.extend(struct.pack(">I", len(frame)) + frame)

        sock.sendall = stream_send
        self.assertEqual(self.client(sock).grab(skip=1), b"\xffnew")
        self.assertEqual([cmd["action"] for cmd in sock.sent], ["startStream", "stopStream"])
        self.assertFalse(sock.closed)

    def test_touch_connection_failure_does_not_retry_stale_coordinates(self):
        phone = bot.Phone("test.invalid")
        with patch.object(bot, "TouchSynthesis", side_effect=ConnectionRefusedError) as connect, \
                patch.object(bot.time, "sleep") as sleep:
            with self.assertRaises(bot.StopBot):
                phone.tap(1, 2)
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_touch_is_never_replayed_after_lost_acknowledgement(self):
        connection = Mock()
        connection.tap.side_effect = ConnectionResetError("reply lost")
        phone = bot.Phone("test.invalid")
        phone.ts = connection
        with patch.object(bot, "TouchSynthesis") as reconnect:
            with self.assertRaises(bot.StopBot):
                phone.tap(1, 2)
        connection.tap.assert_called_once_with(1, 2)
        reconnect.assert_not_called()


class FakeShot:
    def __init__(self, texts=()):
        self.lines = [ocr.Line(text, [ocr.Word(text, 100, 200, 10, 10)]) for text in texts]
        self.data = b"offline"
        self.box = []

    def texts(self):
        return [line.text for line in self.lines]

    def find(self, *keys, lines=None):
        return next((line for line in (self.lines if lines is None else lines)
                     if any(k in line.text for k in keys)), None)

    def frac(self, *_):
        return 0.0


def withdraw_shot(pending=10, extra=()):
    shot = FakeShot(extra)
    rows = [("提領數值", 100, 100), ("5.00", 5, 200), ("立即提領", 9, 600)]
    if pending is not None:
        rows.append((f"待提領蝦幣數: {pending:.2f}", 100, 80))
    shot.lines.extend(ocr.Line(text, [ocr.Word(text, x, y, 10, 10)]) for text, x, y in rows)
    return shot


class BotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.phone = Mock()
        self.store = Store(Path(self.tmp.name) / "state.json")
        self.bot = bot.Bot(Config(log_dir=self.tmp.name, withdraw=False), self.phone, self.store)
        self.bot.capture = Mock()
        self.bot.capture_once = Mock()
        self.bot.pause = Mock()
        self.empty = FakeShot()
        self.phone.shot.return_value = self.empty

    def prepare_withdrawal(self, shot=None):
        shot = shot or withdraw_shot()
        self.bot.cfg.withdraw = True
        self.phone.shot.return_value = shot
        self.bot.wait_for = Mock(return_value=(screens.Screen.WITHDRAW, shot))
        self.bot.look = Mock(return_value=(screens.Screen.WITHDRAW, shot))
        return shot

    def test_risk_blocks_popup_dismissal_and_all_navigation(self):
        self.phone.shot.return_value = FakeShot(["偵測到風險", "稍後再說"])
        with self.assertRaises(bot.StopBot):
            self.bot.look()
        self.phone.tap.assert_not_called()

    def test_unknown_screen_never_uses_blind_back_coordinates(self):
        self.bot.look = Mock(return_value=(screens.Screen.UNKNOWN, self.empty))
        self.assertFalse(self.bot.back_to_room(screens.Screen.UNKNOWN, self.empty))
        self.phone.tap.assert_not_called()

    def test_unknown_screen_does_not_dismiss_generic_rating_prompt(self):
        self.phone.shot.return_value = FakeShot(["稍後再說"])
        with patch.object(bot, "detect", return_value=screens.Screen.UNKNOWN):
            self.assertEqual(self.bot.look()[0], screens.Screen.UNKNOWN)
        self.phone.tap.assert_not_called()

    def test_unknown_recovery_respects_deadline_without_touch(self):
        clock = [0.0]
        self.bot.end = .25
        self.bot.pause = bot.Bot.pause.__get__(self.bot)
        self.bot.look = Mock(return_value=(screens.Screen.UNKNOWN, self.empty))
        with patch.object(bot.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(bot.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaises(bot.StopBot):
                self.bot.back_to_room(screens.Screen.UNKNOWN, self.empty)
        self.assertEqual(clock[0], .25)
        self.phone.tap.assert_not_called()

    def test_unknown_recovery_remains_interruptible_without_touch(self):
        self.bot.pause = bot.Bot.pause.__get__(self.bot)
        with patch.object(bot.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.bot.back_to_room(screens.Screen.UNKNOWN, self.empty)
        self.phone.tap.assert_not_called()

    def test_deadline_interrupts_inside_room_loop(self):
        self.bot.end = 2
        with patch.object(bot.time, "monotonic", return_value=3):
            with self.assertRaises(bot.StopBot):
                self.bot.stay("coins", "room")
        self.phone.shot.assert_not_called()

    def test_restarted_countdown_is_not_recorded_as_success(self):
        before = screens.Room(coin=screens.Task(present=True, claimable=True, button=(1, 2)))
        after = screens.Room(coin=screens.Task(present=True, state="05:00"))
        with patch.object(bot, "read_room", return_value=after), \
                patch.object(bot.time, "monotonic", side_effect=[0, 1, 7]):
            with self.assertRaises(bot.StopBot):
                self.bot.claim(before, "room")
        self.assertEqual(self.store.day.coin_claims, 0)

    def test_verified_claim_updates_counters_once(self):
        before = screens.Room(coin=screens.Task(present=True, claimable=True, button=(1, 2)))
        after = screens.Room(claimed="成功領取0.25蝦幣")
        with patch.object(bot, "read_room", return_value=after):
            self.bot.claim(before, "room")
        self.assertEqual(self.store.day.coin_claims, 1)
        self.assertEqual(self.store.day.coin_total, .25)
        self.phone.tap.assert_called_once()

    def test_daily_coin_limit_blocks_touch(self):
        self.store.day.coin_claims = 100
        before = screens.Room(coin=screens.Task(present=True, claimable=True, button=(1, 2)))
        self.bot.claim(before, "room")
        self.phone.tap.assert_not_called()

    def test_claim_label_without_balance_change_is_not_counted(self):
        self.phone.shot.return_value = FakeShot(["完成任務拿獎勵"])
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.empty))
        before = screens.Panel(pending=10, claim=(1, 2), claim_amount=.21)
        after = screens.Panel(pending=10)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            with self.assertRaises(bot.StopBot):
                self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0)

    def test_watch_claim_uses_observed_balance_difference(self):
        self.phone.shot.return_value = FakeShot(["完成任務拿獎勵"])
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.empty))
        self.bot.back_to_room = Mock()
        before = screens.Panel(pending=10, claim=(1, 2), claim_amount=99)
        after = screens.Panel(pending=10.21)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, .21)

    def test_unknown_withdrawal_is_persisted_and_blocks_restart(self):
        self.prepare_withdrawal()
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(pending=10, withdraw=(1, 1)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertEqual(self.store.day.withdrawals, 0)
        self.assertEqual(self.phone.tap.call_count, 3)
        self.assertFalse(self.bot.should_withdraw(10, True))

    def test_general_limit_explanation_does_not_clear_transaction_journal(self):
        self.prepare_withdrawal(withdraw_shot(extra=["每日提領上限為三次", "確認"]))
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertEqual(self.phone.tap.call_count, 3)

    def test_unreadable_balance_does_not_use_stale_saved_balance(self):
        self.store.day.pending = 100
        self.prepare_withdrawal(withdraw_shot(pending=None))
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.phone.tap.assert_called_once_with(1, 1)

    def test_disabled_withdrawal_never_touches_phone(self):
        self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.phone.tap.assert_not_called()

    def test_nonfinite_pending_balance_never_requests_withdrawal(self):
        self.bot.cfg.withdraw = True
        for pending in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(pending=pending):
                self.assertFalse(self.bot.should_withdraw(pending, True))

    def test_changed_withdrawal_page_or_balance_prevents_submit(self):
        for changed in (FakeShot(), withdraw_shot(5), withdraw_shot(None)):
            with self.subTest(changed=changed.texts()):
                self.phone.tap.reset_mock()
                initial = self.prepare_withdrawal()
                self.phone.shot.side_effect = [initial, changed]
                with self.assertRaises(bot.StopBot):
                    self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
                self.assertEqual(self.phone.tap.call_count, 2)
                self.assertFalse(self.store.day.withdrawal_pending)

    def test_screen_change_before_amount_selection_prevents_stale_touch(self):
        self.prepare_withdrawal()
        self.phone.shot.return_value = self.empty
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.phone.tap.assert_called_once_with(1, 1)

    def test_changed_daily_limit_prevents_submit(self):
        self.prepare_withdrawal()

        def hit_limit(x, y):
            if (x, y) == (5, 200):
                self.store.day.withdrawals = 3

        self.phone.tap.side_effect = hit_limit
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.assertEqual(self.phone.tap.call_count, 2)
        self.assertFalse(self.store.day.withdrawal_pending)

    def test_rejection_is_processed_before_confirmation_button(self):
        self.prepare_withdrawal()
        shot = withdraw_shot(extra=["確認提領", "已達每日提領上限"])
        self.bot.look.return_value = (screens.Screen.WITHDRAW, shot)
        self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.assertNotIn(unittest.mock.call(100, 200), self.phone.tap.call_args_list)
        self.assertEqual(self.store.day.withdrawals, 3)
        self.assertFalse(Store(self.store.path).day.withdrawal_pending)

    def test_unknown_screen_success_text_cannot_clear_journal(self):
        self.prepare_withdrawal()
        self.bot.look.return_value = (screens.Screen.UNKNOWN, FakeShot(["提領成功"]))
        with self.assertRaises(bot.StopBot):
            self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertEqual(self.store.day.withdrawals, 0)

    def test_withdrawal_crossing_midnight_uses_live_day(self):
        for rollover_on in ((5, 200), (9, 600)):
            with self.subTest(rollover_on=rollover_on):
                now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
                with patch.object(state_module, "reward_now", side_effect=lambda: now[0]):
                    self.store = Store(Path(self.tmp.name) / f"rollover-{rollover_on[0]}.json")
                    self.bot.store = self.store
                    self.prepare_withdrawal()
                    self.bot.look.return_value = (screens.Screen.WITHDRAW, withdraw_shot(5))

                    def tap(x, y):
                        if (x, y) == (9, 600):
                            self.assertTrue(Store(self.store.path).day.withdrawal_pending)
                        if (x, y) == rollover_on:
                            now[0] += timedelta(minutes=2)
                            _ = self.store.day

                    self.phone.tap.side_effect = tap
                    self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
                    day = Store(self.store.path).day
                    self.assertEqual(day.date, "2026-10-26")
                    self.assertEqual(day.withdrawals, 1)
                    self.assertEqual(day.withdrawn, 5)
                    self.assertFalse(day.withdrawal_pending)

    def test_panel_screen_change_prevents_claim_touch(self):
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.empty))
        with self.assertRaises(bot.StopBot):
            self.bot.panel_tasks()
        self.phone.tap.assert_not_called()

    def test_final_panel_crossing_midnight_resumes_new_day(self):
        now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
        with patch.object(state_module, "reward_now", side_effect=lambda: now[0]), \
                patch.object(bot, "reward_now", side_effect=lambda: now[0]):
            self.bot.store = Store(Path(self.tmp.name) / "final-panel.json")
            self.bot.go_room = Mock()
            self.bot.panel_tasks = Mock(side_effect=lambda **_: now.__setitem__(0, now[0] + timedelta(minutes=2)))
            self.bot.rest_until_tomorrow()
            self.bot.pause.assert_not_called()
            self.assertFalse(self.bot.day.final_panel)


class ScreenTests(unittest.TestCase):
    def test_native_bitmap_is_closed_even_when_ocr_fails(self):
        bitmap = Mock()
        with patch.object(ocr, "_get_engine"), patch.object(ocr, "_bitmap", return_value=bitmap), \
                patch.object(ocr, "_recognize", new=AsyncMock(side_effect=RuntimeError("OCR failed"))):
            with self.assertRaises(RuntimeError):
                ocr.read(Image.new("RGB", (10, 10)))
        bitmap.close.assert_called_once()

    def test_invalid_seconds_are_not_a_countdown(self):
        self.assertIsNone(screens.countdown("01:99"))
        self.assertEqual(screens.countdown("01:59"), 119)

    def test_missing_withdraw_page_boundaries_produces_no_options(self):
        self.assertEqual(screens.read_withdraw(FakeShot(["5.00"])).options, [])

    def test_unsupported_geometry_stops_before_coordinates_are_used(self):
        stream = io.BytesIO()
        Image.new("RGB", (30, 40)).save(stream, "PNG")
        with self.assertRaises(ValueError):
            screens.Shot(stream.getvalue())

    def test_pixels_are_lazy_uint8_and_chat_cannot_fake_success(self):
        stream = io.BytesIO()
        Image.new("RGB", (1125, 2436)).save(stream, "PNG")
        shot = screens.Shot(stream.getvalue())
        self.assertNotIn("px", shot.__dict__)
        self.assertEqual(shot.px.dtype.name, "uint8")
        shot.lines = [ocr.Line("成功領取9.99蝦幣", [ocr.Word("", 100, 600, 10, 10)])]
        shot.box = []
        shot.name = "test"
        self.assertEqual(screens.read_room(shot).claimed, "")


if __name__ == "__main__":
    unittest.main()
