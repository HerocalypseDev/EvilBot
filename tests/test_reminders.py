from datetime import datetime, timezone

import db
from reminders import chunks, reminder_action


def at(hour, minute=0):
    return datetime(2026, 10, 22, hour, minute, tzinfo=timezone.utc)


def test_action_windows_for_60_minute_reminder():  # due 23:00
    assert reminder_action(at(22, 59), 60) == "wait"
    assert reminder_action(at(23, 0), 60) == "send"
    assert reminder_action(at(23, 4), 60) == "send"
    assert reminder_action(at(23, 5), 60) == "late"
    assert reminder_action(at(23, 59), 60) == "late"


def test_action_other_offsets():
    assert reminder_action(at(21, 0), 180) == "send"
    assert reminder_action(at(22, 0), 120) == "send"
    assert reminder_action(at(10, 0), 180) == "wait"
    assert reminder_action(at(0, 0), 60) == "wait"  # new day starts clean


def test_reminder_fires_once():
    conn = db.connect(":memory:")
    assert db.mark_reminder(conn, 1, "2026-10-22", 60) is True
    assert db.mark_reminder(conn, 1, "2026-10-22", 60) is False  # restart inside the window
    assert db.mark_reminder(conn, 1, "2026-10-22", 120) is True
    assert db.mark_reminder(conn, 1, "2026-10-23", 60) is True  # next day


def test_chunks_of_50():
    assert chunks([], 50) == []
    assert [len(c) for c in chunks(list(range(120)), 50)] == [50, 50, 20]
    assert [len(c) for c in chunks(list(range(50)), 50)] == [50]
