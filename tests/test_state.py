import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from autocoin import state
from autocoin.state import Day, StateError, Store, TAIPEI, reward_now


def payload(**changes):
    return {**dataclasses.asdict(Day(date=reward_now().date().isoformat())), **changes}


def test_atomic_save_creates_parents_and_roundtrips(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = Store(path)
    store.day.coin_claims = 9
    store.day.room("room")["claims"] = 2
    store.save()
    assert Store(path).day.coin_claims == 9
    assert Store(path).day.room("room")["claims"] == 2
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("changes", [
    {"withdrawals": -1}, {"withdrawals": True}, {"withdrawals": "0"},
    {"withdrawal_pending": "false"}, {"pending": float("nan")},
    {"coin_total": float("inf")}, {"rooms": []},
    {"rooms": {"a": {"claims": -1}}}, {"rooms": {"a": {"coins": True}}},
    {"rooms": {"a": {"done": "false"}}}, {"date": "2026-99-99"},
])
def test_invalid_state_never_silently_resets(tmp_path, changes):
    path = tmp_path / "state.json"
    original = json.dumps(payload(**changes))
    path.write_text(original, encoding="utf-8")
    with pytest.raises(StateError):
        Store(path)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("contents", ["{bad json", "[]", "null", '"text"'])
def test_corrupt_document_fails_closed(tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(StateError):
        Store(path)
    assert path.read_text(encoding="utf-8") == contents


def test_older_schema_gets_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"date": reward_now().date().isoformat(),
                                "rooms": {"room": {"claims": 1}}, "future_field": 3}),
                    encoding="utf-8")
    day = Store(path).day
    assert day.withdrawal_pending is False
    assert day.room("room")["coins"] == 0.0


def test_failed_replace_keeps_original_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = Store(path)
    store.save()
    original = path.read_bytes()
    store.day.withdrawals = 2

    def fail(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(state.os, "replace", fail)
    with pytest.raises(StateError):
        store.save()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_cleanup_error_does_not_mask_fatal_persistence_error(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.json")

    def fail(*_, **__):
        raise PermissionError("locked file")

    monkeypatch.setattr(state.os, "replace", fail)
    monkeypatch.setattr(state.Path, "unlink", fail)
    with pytest.raises(StateError):
        store.save()


def test_exclusive_lock_reloads_and_releases(tmp_path):
    path = tmp_path / "state.json"
    first, stale = Store(path), Store(path)
    with first.exclusive():
        first.day.withdrawals = 2
        first.save()
        with pytest.raises(StateError):
            with stale.exclusive():
                pytest.fail("second writer acquired lock")
    with stale.exclusive():
        assert stale.day.withdrawals == 2


def test_lock_blocks_other_process(tmp_path):
    path = tmp_path / "state.json"
    script = """
import sys
from autocoin.state import Store, StateError
try:
    with Store(sys.argv[1]).exclusive():
        sys.exit(0)
except StateError:
    sys.exit(7)
"""
    with Store(path).exclusive():
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                capture_output=True, timeout=15)
    assert result.returncode == 7, result.stderr


def test_reward_day_and_pending_withdrawal_survive_midnight(tmp_path, monkeypatch):
    now = datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)
    monkeypatch.setattr(state, "reward_now", lambda: now)
    store = Store(tmp_path / "state.json")
    store.day.withdrawals = 2
    store.day.withdrawal_pending = True
    store.save()
    now += timedelta(minutes=2)
    assert store.day.date == "2026-10-26"
    assert store.day.withdrawals == 0
    assert store.day.withdrawal_pending
    assert Store(store.path).day.withdrawal_pending
    assert store.summary().startswith("2026-10-26")
    assert reward_now().utcoffset() == timedelta(hours=8)


def test_readonly_summary_at_midnight_cannot_overwrite_running_writer(tmp_path, monkeypatch):
    now = datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)
    monkeypatch.setattr(state, "reward_now", lambda: now)
    path = tmp_path / "state.json"
    writer, reader = Store(path), Store(path)
    with writer.exclusive():
        writer.day.withdrawal_pending = True
        writer.save()
        now += timedelta(minutes=2)
        reader.summary()
        assert Store(path).day.withdrawal_pending


def test_clock_rollback_does_not_reset_limits(tmp_path, monkeypatch):
    now = datetime(2026, 10, 25, 23, 59, tzinfo=TAIPEI)
    monkeypatch.setattr(state, "reward_now", lambda: now)
    store = Store(tmp_path / "state.json")
    store.day.withdrawals = 2
    store.save()
    now -= timedelta(days=1)
    with pytest.raises(StateError):
        _ = store.day
    with pytest.raises(StateError):
        Store(store.path)
