"""归一化要保住注入时钟的时刻，并且认 macOS 的 /private/tmp。"""
from datetime import datetime, timedelta, timezone

from baseline_plugin import _clock_instants, _note_instant, canonicalize
from workshop.core.models import due, stamp


def test_injected_clock_keeps_the_instant():
    early_clock = {
        "2026-11-13T08:00:00+00:00",
        "2026-11-13T08:30:00+00:00",
        "2026-11-13T09:00:00+00:00",
    }
    late_clock = {
        "2026-11-13T08:00:00+00:00",
        "2026-11-13T10:00:00+00:00",
        "2026-11-13T08:05:00+00:00",
    }
    early = canonicalize(
        {"deadline": "2026-11-13T08:30:00+00:00", "due": "2026-11-13T09:00:00+00:00"}, {}, early_clock)
    late = canonicalize(
        {"deadline": "2026-11-13T10:00:00+00:00", "due": "2026-11-13T08:05:00+00:00"}, {}, late_clock)
    assert early == {"deadline": "+30m", "due": "+1h"}
    assert late == {"deadline": "+2h", "due": "+5m"}
    assert early != late


def test_wall_clock_stays_numbered():
    stamped = canonicalize({
        "a": "2026-11-15T04:22:11.473532+00:00",
        "b": "2026-11-15T04:22:12.100000+00:00",
    }, {})
    assert stamped == {"a": "$time:1", "b": "$time:2"}
    # 整秒上的墙上时钟也不写成偏移，除非 Clock 真的给出过这一刻。
    whole = canonicalize({"at": "2026-11-15T04:22:11+00:00"}, {}, ["2026-11-13T08:00:00+00:00"])
    assert whole == {"at": "$time:1"}


def test_due_from_clock_is_remembered_and_wall_time_is_not():
    _clock_instants.clear()
    origin = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)
    noted = _note_instant(origin)
    stamped = due("+30m", noted)
    earlier = stamp(noted - timedelta(minutes=10))
    assert stamped in _clock_instants
    assert earlier in _clock_instants
    rendered = canonicalize({"deadline": stamped, "since": earlier}, {}, _clock_instants)
    assert rendered == {"deadline": "+30m", "since": "-10m"}
    wall = stamp(datetime(2026, 11, 15, 4, 22, 11, tzinfo=timezone.utc))
    assert wall not in _clock_instants
    assert canonicalize({"at": wall}, {}, _clock_instants) == {"at": "$time:1"}
    _clock_instants.clear()


def test_uuid4_hex_is_numbered_without_splitting_longer_hashes():
    first = canonicalize("9df4b59d34674308824c2a79ba492938", {})
    second = canonicalize("4a5ea330e68e4c9b84baeb81fe01997b", {})
    assert first == second == "$uuid:1"
    bare = "a" * 12 + "4bcd8" + "e" * 15
    assert canonicalize(bare, {}) == "$uuid:1"
    assert canonicalize(bare + "f" * 32, {}) == "$hash:1"
    assert canonicalize("0123456789abcdef0123456789abcdef", {}) == "0123456789abcdef0123456789abcdef"


def test_numbered_pytest_directory_collapses():
    first = canonicalize(
        "/tmp/pytest-of-x/test_tick_reads_do_not_grow_wi0/0100/workshop.db",
        {"/tmp/pytest-of-x": "$pytest", "/tmp/pytest-of-x/test_tick_reads_do_not_grow_wi0": "$testtmp"},
    )
    second = canonicalize(
        "/tmp/pytest-of-x/test_tick_reads_do_not_grow_wi4/0100/workshop.db",
        {"/tmp/pytest-of-x": "$pytest", "/tmp/pytest-of-x/test_tick_reads_do_not_grow_wi4": "$testtmp"},
    )
    assert first == second == "$testtmp/0100/workshop.db"


def test_wake_directory_matches_private_tmp():
    assert canonicalize("/tmp/ws-wake-abc/workshop.db", {}) == "$tmpdir/workshop.db"
    assert canonicalize("/private/tmp/ws-wake-abc/workshop.db", {}) == "$tmpdir/workshop.db"
