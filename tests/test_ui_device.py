from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autocoin.device import Device, UnsafeAction
from autocoin.screens import Task, num
from autocoin.ui import PKG, Snapshot


def snapshot(body):
    return Snapshot(f'<hierarchy>{body}</hierarchy>', PKG)


def node(rid="", text="", bounds="[1,2][2,3]", **attrs):
    from xml.sax.saxutils import quoteattr
    attrs = {"resource-id": rid, "text": text, "bounds": bounds, "package": PKG, **attrs}
    return "<node " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + "/>"


def test_index_preserves_first_match_and_all_duplicates():
    s = snapshot(node("p:id/item", "first") + node("item", "second"))
    assert s.id("item").text == "first"
    assert [n.text for n in s.by_id("item")] == ["first", "second"]
    assert s.text("second").rid == "item"
    s.by_id("item").clear()
    assert len(s.by_id("item")) == 2


def test_jitter_never_leaves_small_node():
    n = snapshot(node()).nodes[0]
    for _ in range(50):
        assert n.spot == (1, 2)
    n = snapshot(node(bounds="[0,0][0,0]")).nodes[0]
    with pytest.raises(ValueError):
        _ = n.spot


def test_external_entities_are_rejected():
    with pytest.raises(ValueError):
        Snapshot('<!DOCTYPE node [<!ENTITY ex SYSTEM "file:///secret">]><node text="&ex;"/>')


def test_chat_cannot_masquerade_as_navigation_button():
    s = snapshot(f'<node package="{PKG}" resource-id="public_screen_view">'
                 + node(text="立即提領") + '</node>')
    assert s.text("立即提領") is None


def fake_device():
    d = object.__new__(Device)
    d.serial = "offline"
    d.tap_jitter_px = 0
    d.before_action = Mock()
    d.current = Mock(return_value={"package": PKG})
    d.locked = Mock(return_value=False)
    shell = Mock(return_value=SimpleNamespace(returncode=0))
    d.adb = SimpleNamespace(device=Mock(return_value=SimpleNamespace(shell2=shell)))
    return d


def test_tap_transport_error_is_never_retried():
    d = fake_device()
    command = d.adb.device.return_value.shell2
    command.side_effect = TimeoutError("acknowledgement lost")
    with pytest.raises(UnsafeAction):
        d.tap(10, 20)
    assert command.call_count == 1


@pytest.mark.parametrize("locked,package", [(True, PKG), (False, "bank.app"), (False, "")])
def test_gesture_rechecks_foreground_and_lock(locked, package):
    d = fake_device()
    d.locked.return_value = locked
    d.current.return_value = {"package": package}
    with pytest.raises(UnsafeAction):
        d.back()
    d.adb.device.assert_not_called()


def test_node_tap_does_not_apply_jitter_twice():
    d = fake_device()
    d.tap_jitter_px = 50
    d.tap_node(snapshot(node()).nodes[0])
    d.adb.device.return_value.shell2.assert_called_once_with(
        ["input", "tap", "1", "2"], timeout=10)


@pytest.mark.parametrize("method", ["wake", "launch", "restart_app"])
def test_app_control_commands_do_not_replay_after_connection_failure(method):
    d = fake_device()
    d.adb.device.return_value.shell2.side_effect = TimeoutError("reply lost")
    with pytest.raises(UnsafeAction):
        getattr(d, method)()
    assert d.adb.device.return_value.shell2.call_count == 1


def test_failed_input_command_stops_instead_of_claiming_it_was_sent():
    d = fake_device()
    d.adb.device.return_value.shell2.return_value.returncode = 1
    with pytest.raises(UnsafeAction):
        d.tap(1, 2)


def test_claim_requires_an_actual_button():
    assert not Task(state="領取").claimable
    assert Task(state="領取", button=(1, 2)).claimable


def test_malformed_countdown_and_nonfinite_amount_are_ignored():
    assert Task(state="01:99").seconds is None
    assert Task(state="01:59").seconds == 119
    assert num("9" * 400) is None
