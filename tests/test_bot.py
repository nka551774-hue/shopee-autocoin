from collections import deque
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from autocoin import bot as bot_module
from autocoin import state as state_module
from autocoin.bot import Bot, StopBot
from autocoin.config import Config
from autocoin.screens import read_room
from autocoin.state import StateError, Store, TAIPEI
from autocoin.ui import PKG, Snapshot


def node(rid="", text="", bounds="[10,10][100,50]", children=""):
    from xml.sax.saxutils import quoteattr
    return (f'<node package="{PKG}" resource-id={quoteattr(rid)} '
            f'text={quoteattr(text)} bounds={quoteattr(bounds)}>{children}</node>')


def snap(body):
    return Snapshot(f"<hierarchy>{body}</hierarchy>", PKG)


def room(state="領取", claimed=""):
    return snap(node("tv_anchor_name", "room")
                + node("box_view_coin_streamer", children=node("tv_state_btn", state)
                       + node("tv_coin_num", "0.50"))
                + node("tv_coin_claimed_content", claimed))


def panel(pending=5, claim=True):
    return snap(node(text="待提領蝦幣數", bounds="[100,100][250,120]")
                + node(text=str(pending), bounds="[100,125][250,145]")
                + node(text="提領") + (node(text="領取") if claim else ""))


def withdraw_page(pending=5, extra=""):
    return snap(node(text=f"待提領蝦幣數: {pending}") + node(text="5.00")
                + node(text="1.00") + node(text="立即提領") + extra)


class FakeDevice:
    def __init__(self, snapshots):
        self.snapshots = deque(snapshots)
        self.actions = []
        self.before_action = lambda: None
        self.on_tap = lambda _: None
        self.locked = lambda: False

    def home_package(self):
        return "launcher"

    def snapshot(self):
        return self.snapshots.popleft() if len(self.snapshots) > 1 else self.snapshots[0]

    def tap(self, *coords):
        self.before_action()
        self.actions.append(coords)

    def tap_node(self, n):
        self.before_action()
        self.actions.append(n.text)
        self.on_tap(n.text)

    def back(self):
        self.before_action()
        self.actions.append("back")

    def screenshot(self, _):
        pass

    def clear_toast(self):
        pass

    def last_toast(self):
        return ""

    def screen_on(self):
        return True

    def current(self):
        return {"package": PKG}


@pytest.fixture
def make_bot(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bot_module, "_sleep", lambda secs: clock.__setitem__(0, clock[0] + secs))

    def make(snapshots):
        cfg = Config(log_dir=str(tmp_path / "logs"), jitter=0)
        return Bot(cfg, FakeDevice(snapshots), Store(tmp_path / "state.json"))

    return make


def test_countdown_reset_is_not_claim_success(make_bot):
    b = make_bot([room("09:00")])
    with pytest.raises(StopBot):
        b.claim(read_room(room()))
    assert b.day.coin_claims == 0
    assert len(b.dev.actions) == 1


def test_explicit_claim_success_counts_once_and_enforces_limit(make_bot):
    b = make_bot([room("09:00", "成功領取 0.50 Coins"), room("08:59")])
    b.day.coin_claims = 99
    b.claim(read_room(room()))
    assert b.day.coin_claims == 100
    assert b.day.coin_total == 0.5
    assert b.day.coin_limit_hit


def test_risk_stops_before_success_is_counted(make_bot):
    s = room("09:00", "成功領取 0.50 Coins")
    s = snap(s.xml.removeprefix("<hierarchy>").removesuffix("</hierarchy>")
             + node(text="偵測到風險活動"))
    b = make_bot([s])
    with pytest.raises(StopBot):
        b.claim(read_room(room()))
    assert b.day.coin_total == 0


def test_risk_in_navigation_stops_without_touch(make_bot):
    b = make_bot([snap(node(text="不符合領取此獎勵資格"))])
    with pytest.raises(StopBot):
        b.go_live()
    assert not b.dev.actions


def test_risk_is_checked_when_foreground_query_fails(make_bot):
    s = snap(node(text="偵測到風險活動"))
    s.package = ""
    b = make_bot([s])
    with pytest.raises(StopBot):
        b.look()
    assert not b.dev.actions


def test_chat_cannot_fake_risk_message(make_bot):
    s = snap(node("public_screen_view", children=node(text="偵測到風險活動")))
    b = make_bot([s])
    b.look()
    assert not b.dev.actions


def test_unconfirmed_watch_claim_is_not_booked(make_bot):
    b = make_bot([panel()])
    with pytest.raises(StopBot):
        b.panel_tasks()
    assert b.day.watch_total == 0
    assert b.dev.actions == ["領取"]


def test_watch_claim_uses_confirmed_balance_delta(make_bot):
    b = make_bot([panel(5), panel(5), panel(5.2, False)])
    b.cfg.withdraw = False
    b.back_to_room = Mock()
    b.panel_tasks()
    assert b.day.watch_total == 0.2
    assert b.day.pending == 5.2


def test_zero_balance_does_not_fall_back_to_old_balance(make_bot):
    b = make_bot([withdraw_page(0)])
    b.day.pending = 50
    b.withdraw(panel())
    assert b.dev.actions == ["提領"]
    assert not b.day.withdrawal_pending


def test_withdrawal_journal_is_durable_before_submission(make_bot):
    b = make_bot([withdraw_page(), withdraw_page(), withdraw_page(), withdraw_page(0)])

    def on_tap(text):
        if text == "立即提領":
            assert Store(b.store.path).day.withdrawal_pending

    b.dev.on_tap = on_tap
    b.withdraw(panel())
    assert b.day.withdrawals == 1
    assert b.day.withdrawn == 5
    assert not Store(b.store.path).day.withdrawal_pending


def test_unknown_withdrawal_stays_blocked_after_restart(make_bot):
    b = make_bot([withdraw_page(extra=node(text="確認"))])
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert b.dev.actions.count("立即提領") == 1
    assert "確認" not in b.dev.actions
    assert Store(b.store.path).day.withdrawal_pending
    assert not b.should_withdraw(50, True)


def test_chat_cannot_clear_pending_withdrawal(make_bot):
    s = withdraw_page(extra=node("public_screen_view", children=node(text="已達提領上限")))
    b = make_bot([s])
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert Store(b.store.path).day.withdrawal_pending


def test_verified_withdrawal_rejection_blocks_day(make_bot):
    reject = withdraw_page(extra=node(text="您已達每日提領次數上限"))
    b = make_bot([withdraw_page(), withdraw_page(), withdraw_page(), reject])
    b.withdraw(panel())
    assert b.day.withdrawals == b.cfg.withdraw_daily_limit
    assert not b.day.withdrawal_pending


def test_inexact_balance_drop_is_not_withdrawal_proof(make_bot):
    b = make_bot([withdraw_page(10), withdraw_page(10), withdraw_page(10), withdraw_page(0)])
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert b.day.withdrawals == 0
    assert b.day.withdrawal_pending


def test_failed_journal_write_prevents_submission(make_bot, monkeypatch):
    b = make_bot([withdraw_page()])
    monkeypatch.setattr(b.store, "save", Mock(side_effect=StateError("disk full")))
    with pytest.raises(StateError):
        b.withdraw(panel())
    assert "立即提領" not in b.dev.actions


def test_hours_stops_inside_locked_wait_without_final_withdrawal(make_bot):
    b = make_bot([room()])
    b.dev.locked = lambda: True
    b.panel_tasks = Mock()
    b.run(hours=0.001)
    assert b.time_up()
    assert not b.dev.actions
    b.panel_tasks.assert_not_called()


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_rejected_before_device_operations(make_bot, hours):
    b = make_bot([room()])
    with pytest.raises(ValueError):
        b.run(hours)
    assert not b.dev.actions


def test_leaving_watch_room_early_does_not_mark_completed(make_bot):
    b = make_bot([room()])
    b.leave = Mock()
    b.finish_room({"room"}, "lost", "watch")
    assert not b.day.room("room")["watched"]


@pytest.mark.parametrize("rollover_on", ["5.00", "立即提領"])
def test_withdrawal_crossing_midnight_keeps_journal_and_books_current_day(make_bot, monkeypatch, rollover_on):
    now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
    monkeypatch.setattr(state_module, "reward_now", lambda: now[0])
    b = make_bot([withdraw_page(), withdraw_page(), withdraw_page(), withdraw_page(0)])

    def on_tap(text):
        if text == "立即提領":
            assert Store(b.store.path).day.withdrawal_pending
        if text == rollover_on:
            now[0] += timedelta(days=1)
            _ = b.store.day  # A message/status read can cause rollover mid-operation.

    b.dev.on_tap = on_tap
    b.withdraw(panel())
    day = Store(b.store.path).day
    assert day.date == "2026-10-26"
    assert day.withdrawals == 1
    assert day.withdrawn == 5
    assert not day.withdrawal_pending


def test_watch_reward_after_midnight_updates_live_day(make_bot, monkeypatch):
    now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
    monkeypatch.setattr(state_module, "reward_now", lambda: now[0])
    b = make_bot([panel(5), panel(5), panel(5.2, False)])
    b.cfg.withdraw = False
    b.back_to_room = Mock()

    def on_tap(_):
        now[0] += timedelta(days=1)
        _ = b.store.day

    b.dev.on_tap = on_tap
    b.panel_tasks()
    assert Store(b.store.path).day.watch_total == 0.2


def test_withdrawal_rechecks_balance_before_submit(make_bot):
    b = make_bot([withdraw_page(5), withdraw_page(5), withdraw_page(10)])
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert "立即提領" not in b.dev.actions
    assert not b.day.withdrawal_pending


def test_withdrawal_rechecks_limit_before_submit(make_bot):
    b = make_bot([withdraw_page()])
    b.dev.on_tap = lambda text: setattr(b.day, "withdrawals", 3) if text == "5.00" else None
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert "立即提領" not in b.dev.actions


def test_rejection_is_handled_before_confirmation_button(make_bot):
    rejected = withdraw_page(extra=node(text="確認提領") + node(text="已達每日提領上限"))
    b = make_bot([withdraw_page(), withdraw_page(), withdraw_page(), rejected])
    b.withdraw(panel())
    assert "確認提領" not in b.dev.actions
    assert b.day.withdrawals == 3


def test_unknown_screen_balance_cannot_confirm_withdrawal(make_bot):
    unknown = snap(node(text="待提領蝦幣數: 0"))
    b = make_bot([withdraw_page(), withdraw_page(), withdraw_page(), unknown])
    with pytest.raises(StopBot):
        b.withdraw(panel())
    assert b.day.withdrawal_pending
    assert b.day.withdrawals == 0


def test_chat_and_negative_numbers_cannot_supply_pending_balance(make_bot):
    b = make_bot([room()])
    fake_balance = snap(node("public_screen_view", children=node(text="待提領蝦幣數: 50")))
    assert b.pending_amount(fake_balance) is None
    assert b.pending_amount(snap(node(text="待提領蝦幣數: -5"))) is None


def test_final_panel_crossing_midnight_does_not_sleep_a_whole_extra_day(make_bot, monkeypatch):
    now = [datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)]
    monkeypatch.setattr(state_module, "reward_now", lambda: now[0])
    monkeypatch.setattr(bot_module, "reward_now", lambda: now[0])
    b = make_bot([room()])
    b.go_live = Mock()
    b.pause = Mock(side_effect=AssertionError("must resume new day immediately"))
    b.panel_tasks = lambda **_: now.__setitem__(0, now[0] + timedelta(minutes=2))
    b.rest_until_tomorrow()
    b.pause.assert_not_called()
    assert not b.day.final_panel
