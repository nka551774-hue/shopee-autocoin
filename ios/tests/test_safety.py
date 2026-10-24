"""Offline regression tests; no real phone, OCR service, or account is contacted."""
from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot
import ocr
import screens
import tsclient
from autocoin.config import Config
from autocoin.state import Store
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

    def texts(self):
        return [line.text for line in self.lines]

    def find(self, *keys):
        return next((line for line in self.lines if any(k in line.text for k in keys)), None)


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
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.empty))
        before = screens.Panel(pending=10, claim=(1, 2), claim_amount=.21)
        after = screens.Panel(pending=10)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            with self.assertRaises(bot.StopBot):
                self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, 0)

    def test_watch_claim_uses_observed_balance_difference(self):
        self.bot.look = Mock(return_value=(screens.Screen.PANEL, self.empty))
        self.bot.back_to_room = Mock()
        before = screens.Panel(pending=10, claim=(1, 2), claim_amount=99)
        after = screens.Panel(pending=10.21)
        with patch.object(bot, "read_panel", side_effect=[before, after]):
            self.bot.panel_tasks()
        self.assertEqual(self.store.day.watch_total, .21)

    def test_unknown_withdrawal_is_persisted_and_blocks_restart(self):
        self.bot.wait_for = Mock(return_value=(screens.Screen.WITHDRAW, self.empty))
        self.bot.look = Mock(return_value=(screens.Screen.WITHDRAW, self.empty))
        page = screens.WithdrawPage(pending=10, options=[(5, (5, 5))], submit=(9, 9))
        with patch.object(bot, "read_withdraw", return_value=page):
            with self.assertRaises(bot.StopBot):
                self.bot.withdraw(screens.Panel(pending=10, withdraw=(1, 1)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertEqual(self.store.day.withdrawals, 0)
        self.assertEqual(self.phone.tap.call_count, 3)
        self.assertFalse(self.bot.should_withdraw(10, True))

    def test_general_limit_explanation_does_not_clear_transaction_journal(self):
        shot = FakeShot(["每日提領上限為三次", "確認"])
        self.bot.wait_for = Mock(return_value=(screens.Screen.WITHDRAW, shot))
        self.bot.look = Mock(return_value=(screens.Screen.WITHDRAW, shot))
        page = screens.WithdrawPage(pending=10, options=[(5, (5, 5))], submit=(9, 9))
        with patch.object(bot, "read_withdraw", return_value=page):
            with self.assertRaises(bot.StopBot):
                self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.assertTrue(Store(self.store.path).day.withdrawal_pending)
        self.assertEqual(self.phone.tap.call_count, 3)

    def test_unreadable_balance_does_not_use_stale_saved_balance(self):
        self.store.day.pending = 100
        self.bot.wait_for = Mock(return_value=(screens.Screen.WITHDRAW, self.empty))
        page = screens.WithdrawPage(options=[(5, (5, 5))], submit=(9, 9))
        with patch.object(bot, "read_withdraw", return_value=page):
            with self.assertRaises(bot.StopBot):
                self.bot.withdraw(screens.Panel(withdraw=(1, 1)))
        self.phone.tap.assert_called_once_with(1, 1)


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
