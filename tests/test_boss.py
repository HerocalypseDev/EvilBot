from datetime import datetime, timedelta, timezone

import db
from boss import attend_state, next_cycle_start, pick_ping, poll_text, result_text, slot_times, upcoming_cycle

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
    assert text.count("<t:") == 12 and text.count(":R>") == 6 and "UTC" not in text
    assert f"<t:{int(C.timestamp())}:t>" in text and f"<t:{int(at(18).timestamp())}:t>" not in text


def test_boss_poll_rows_resolve_once():
    conn = db.connect(":memory:")
    db.add_boss_poll(conn, 1, C.isoformat(), 99)
    assert db.latest_boss_poll(conn, 1)["resolved_time"] is None
    assert db.resolve_boss_poll(conn, 1, C.isoformat(), at(14).isoformat()) is True
    assert db.resolve_boss_poll(conn, 1, C.isoformat(), at(15).isoformat()) is False
    assert db.latest_boss_poll(conn, 1)["resolved_time"] == at(14).isoformat()


def test_next_action_whole_cycle():
    from boss import next_action
    assert next_action(None, at(10, 30)) == ("post", at(12))  # first poll: next spawn
    assert next_action((C, None), at(11, 0)) == ("wait", None)  # more than 10 min before 12:00
    assert next_action((C, None), at(11, 50)) == ("count", None)
    assert next_action((C, None), at(12, 30)) == ("wait", None)  # 13:00 window not open yet
    assert next_action((C, None), at(12, 50)) == ("count", None)
    assert next_action((C, None), at(17, 0)) == ("end", at(17))  # all times passed, no ping
    assert next_action((C, at(14)), at(14, 4)) == ("wait", None)  # pinged 14:00, wait 5 min
    assert next_action((C, at(14)), at(14, 5)) == ("post", at(18))
    assert next_action((C, at(14)), at(23, 30)) == ("post", at(0, day=23))  # bot was off: skip 18:00


def test_attend_state():
    t = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
    assert attend_state(t, t - timedelta(minutes=10)) == "soon"
    assert attend_state(t, t) == "open"
    assert attend_state(t, t + timedelta(minutes=59)) == "open"
    assert attend_state(t, t + timedelta(hours=1)) == "closed"


def test_result_text():
    assert f"<t:{int(C.timestamp())}:t> won with 3 vote(s)" in result_text(C, 3)
    assert "no time was picked" in result_text(None, 0)


def test_old_polls_are_cleaned_once():
    conn = db.connect(":memory:")
    db.add_boss_poll(conn, 1, "2026-10-21T00:00:00+00:00", 10)
    db.add_boss_poll(conn, 1, "2026-10-22T00:00:00+00:00", 11)  # still open: kept
    db.resolve_boss_poll(conn, 1, "2026-10-21T00:00:00+00:00", "2026-10-21T02:00:00+00:00")
    db.save_ping_message(conn, 1, "2026-10-21T00:00:00+00:00", 20)
    old = db.polls_to_clean(conn, 1, "2026-10-23T00:00:00+00:00")
    assert [(p["message_id"], p["ping_message_id"]) for p in old] == [(10, 20)]
    db.mark_poll_cleaned(conn, 1, "2026-10-21T00:00:00+00:00")
    assert db.polls_to_clean(conn, 1, "2026-10-23T00:00:00+00:00") == []
