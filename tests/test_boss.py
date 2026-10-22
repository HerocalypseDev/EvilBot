from datetime import datetime, timedelta, timezone

import db
from boss import next_cycle_start, pick_ping, poll_text, slot_times, upcoming_cycle

C = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)  # the 12:00 cycle: options 12..17


def at(hour, minute=0, day=22):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc)


def test_options_stop_before_next_spawn():
    assert [t.hour for t in slot_times(C)] == [12, 13, 14, 15, 16, 17]


def test_next_cycle_start():
    assert next_cycle_start(at(10, 30)) == at(12)
    assert next_cycle_start(at(12, 0)) == at(18)
    assert next_cycle_start(at(23, 59)) == at(0, day=23)
    assert next_cycle_start(at(0, 0)) == at(6)


def test_upcoming_cycle_skips_missed_cycles():
    assert upcoming_cycle(at(18), at(14)) == at(18)
    assert upcoming_cycle(at(18), at(22, 30)) == at(18)  # 23:00 is still ahead
    assert upcoming_cycle(at(18), at(23, 30)) == at(0, day=23)
    assert upcoming_cycle(at(18), at(7, day=23)) == at(6, day=23)


def test_most_votes_wins_and_pings_ten_minutes_before():
    votes = [0, 1, 4, 2, 0, 0]  # 14:00 leads
    assert pick_ping(at(13, 49), C, votes) is None
    assert pick_ping(at(13, 50), C, votes) == 2
    assert pick_ping(at(13, 59), C, votes) == 2  # late ping, boss time not started
    assert pick_ping(at(14, 0), C, votes) != 2  # started: no ping for it


def test_tie_goes_to_the_earliest():
    assert pick_ping(at(12, 50), C, [0, 3, 3, 0, 0, 0]) == 1  # 13:00 and 14:00 tied
    assert pick_ping(at(13, 40), C, [0, 3, 3, 0, 0, 0]) is None  # 13:00 still leads: wait for 12:50


def test_no_votes_pings_the_first_time():
    assert pick_ping(at(11, 49), C, [0] * 6) is None
    assert pick_ping(at(11, 50), C, [0] * 6) == 0


def test_passed_times_cannot_win():
    # 12:00 has the votes but it is 12:10 now, so it is out. 13:00 takes over.
    assert pick_ping(at(12, 10), C, [9, 1, 0, 0, 0, 0]) is None
    assert pick_ping(at(12, 50), C, [9, 1, 0, 0, 0, 0]) == 1


def test_nothing_ahead_after_the_last_time():
    assert pick_ping(at(17, 0), C, [1, 1, 1, 1, 1, 1]) is None


def test_poll_text_has_six_options_and_local_time_tags():
    text = poll_text("<@&5>", C)
    assert text.count("<t:") == 6 and "UTC" not in text
    assert f"<t:{int(C.timestamp())}:t>" in text and f"<t:{int(at(18).timestamp())}:t>" not in text


def test_boss_poll_rows_resolve_once():
    conn = db.connect(":memory:")
    db.add_boss_poll(conn, 1, C.isoformat(), 99)
    assert db.latest_boss_poll(conn, 1)["resolved_time"] is None
    assert db.resolve_boss_poll(conn, 1, C.isoformat(), at(14).isoformat()) is True
    assert db.resolve_boss_poll(conn, 1, C.isoformat(), at(15).isoformat()) is False
    assert db.latest_boss_poll(conn, 1)["resolved_time"] == at(14).isoformat()
