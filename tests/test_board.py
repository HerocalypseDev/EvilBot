from datetime import datetime, timezone

import db
from board import (attend_board_data, attend_data_sections, attend_report, board_data, boss_lists, member_day,
                   data_sections, day_lists, join_capped, link_lines, missed_in_row, next_reset_ts, not_donated,
                   pack_lines, paginate, short)

G, DAY, YDAY = 1, "2026-10-22", "2026-10-21"


def conn_with(*members):
    """members = (discord_id, ign or None). Linked ones get a link."""
    conn = db.connect(":memory:")
    for i, ign in members:
        if ign:
            db.roster_add(conn, G, [ign])
            db.link(conn, G, i, ign)
    return conn


def test_join_capped_short_and_long():
    assert join_capped(["a", "b"]) == "a, b"
    out = join_capped([f"name{i}" for i in range(500)], limit=100)
    assert len(out) <= 100 and out.endswith(" more")
    shown = out.rsplit(" +", 1)[0].split(", ")
    assert out.endswith(f"+{500 - len(shown)} more")


def test_paginate():
    items = list(range(25))
    assert paginate(items, 0) == (list(range(10)), 0, 3)
    assert paginate(items, 2) == ([20, 21, 22, 23, 24], 2, 3)
    assert paginate(items, 9)[1] == 2  # clamped high
    assert paginate(items, -1)[1] == 0  # clamped low
    assert paginate([], 0) == ([], 0, 1)
    assert paginate(list(range(10)), 0)[2] == 1  # exactly one full page


def test_not_donated_excludes_donors_and_names_unlinked():
    conn = conn_with((1, "Alpha"), (2, "Beta"), (3, None))
    db.record(conn, G, 1, "A", "Alpha", DAY)
    tracked = [(1, "A"), (2, "B"), (3, "C")]
    assert not_donated(conn, G, tracked, DAY) == [(2, "Beta"), (3, "C (not linked)")]


def test_yesterday_donor_still_not_donated_today():
    conn = conn_with((1, "Alpha"))
    db.record(conn, G, 1, "A", "Alpha", YDAY)
    assert not_donated(conn, G, [(1, "A")], DAY) == [(1, "Alpha")]


def test_board_recent_order_and_totals():
    members = [(i, f"P{i}") for i in range(1, 12)]
    conn = conn_with(*members)
    for i, ign in members:
        db.record(conn, G, i, "n", ign, DAY)
    db.record(conn, G, 1, "n", "P1", YDAY)
    db.record(conn, G, 2, "n", "P2", YDAY)
    data = board_data(conn, G, [(i, "n") for i, _ in members], DAY, YDAY)
    assert data["today_count"] == 11
    assert [ign for _, ign, _ in data["recent"]] == ["P11", "P10", "P9", "P8", "P7", "P6", "P5", "P4", "P3", "P2"]
    assert data["waiting"] == []
    assert all(name == "n" and isinstance(ts, int) for name, _, ts in data["recent"])
    assert (data["yesterday_count"], data["yesterday_coins"]) == (2, 100_000)


def test_voided_donation_leaves_board():
    conn = conn_with((1, "Alpha"))
    sid = db.record(conn, G, 1, "A", "Alpha", DAY)
    db.void(conn, G, sid, 9)
    data = board_data(conn, G, [(1, "A")], DAY, YDAY)
    assert data["today_count"] == 0 and data["waiting"] == ["Alpha"]


def test_next_reset_is_next_midnight_utc():
    now = datetime(2026, 10, 22, 21, 30, tzinfo=timezone.utc)
    assert next_reset_ts(now) == int(datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp())


def test_data_sections_split_the_guild_three_ways():
    conn = conn_with((1, "Alpha"), (2, "Beta"), (3, "Gamma"), (4, None), (5, None))
    db.roster_add(conn, G, ["Zed", "Yan"])  # on the roster, nobody has claimed them
    db.record(conn, G, 3, "Gam", "Gamma", DAY)
    tracked = [(5, "Eve"), (4, "dan"), (3, "Gam"), (2, "Bob"), (1, "Al")]
    sections = data_sections(conn, G, tracked, DAY)
    assert [(n, ign) for n, ign, _ in sections["donated"]] == [("Gam", "Gamma")]
    assert isinstance(sections["donated"][0][2], int)
    assert sections["not_donated"] == [("Al", "Alpha"), ("Bob", "Beta")]  # Discord name + in-game name
    assert sections["not_linked"] == [("Yan",), ("Zed",)]  # unclaimed roster names only


def test_data_sections_empty_guild():
    conn = conn_with()
    assert data_sections(conn, G, [], DAY) == {"donated": [], "not_donated": [], "not_linked": []}


def test_short_cuts_then_escapes():
    assert short("Alpha") == "Alpha"
    assert short("a" * 16) == "a" * 16
    assert short("a" * 17) == "a" * 15 + "…"
    assert short("abcdefghijklmn_xyz") == r"abcdefghijklmn\_…"  # cut first: no stray backslash
    assert short("*bold*") == r"\*bold\*"


def test_join_capped_one_per_line():
    assert join_capped(["Ann", "Bob (not linked)"], sep="\n") == "Ann\nBob (not linked)"
    out = join_capped([f"name{i}" for i in range(500)], limit=100, sep="\n")
    assert len(out) <= 100 and out.splitlines()[-1].startswith("+") and out.endswith(" more")
    assert all(line.startswith("name") for line in out.splitlines()[:-1])


def test_day_lists_splits_one_past_day():
    conn = conn_with((1, "Alpha"), (2, "Beta"), (3, None))
    db.record(conn, G, 1, "A", "Alpha", YDAY)
    db.record(conn, G, 2, "B", "Beta", DAY)
    donated, waiting = day_lists(conn, G, [(1, "A"), (2, "B"), (3, "C")], DAY)
    assert [(name, ign) for name, ign, _ in donated] == [("B", "Beta")]
    assert waiting == ["Alpha", "C (not linked)"]


def test_attend_board_data():
    conn = conn_with((1, "Ann"), (2, "Bob"))
    tracked = [(1, "a"), (2, "b"), (3, "Cy")]
    t = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
    assert attend_board_data(conn, G, tracked, t) is None
    db.add_boss(conn, G, t.isoformat())
    db.add_boss(conn, G, "2026-10-22T08:00:00+00:00")  # next boss, pinged early: not shown yet
    boss_id = db.started_boss(conn, G, t.isoformat())["id"]
    db.attend(conn, boss_id, 1, "a", "Ann")
    data = attend_board_data(conn, G, tracked, datetime(2026, 10, 22, 7, 55, tzinfo=timezone.utc))
    assert data["boss_ts"] == int(t.timestamp()) and data["state"] == "closed"
    assert data["attended"] == ["Ann"] and data["waiting"] == ["Bob", "Cy (not linked)"]
    assert data["today"] == [(int(t.timestamp()), 1)]


def test_attend_data_sections():
    conn = conn_with((1, "Bob"), (2, "ann"))
    tracked = [(1, "b"), (2, "a"), (3, "Cy")]  # Cy is not linked: not in these lists
    assert attend_data_sections(conn, G, tracked, datetime(2026, 10, 22, 1, tzinfo=timezone.utc)) == (
        None, {"attended": [], "not_attended": [], "today_totals": [("ann", "0/0"), ("Bob", "0/0")]})
    for hour in (2, 8):
        db.add_boss(conn, G, f"2026-10-22T0{hour}:00:00+00:00")
    first = db.get_boss(conn, G, "2026-10-22T02:00:00+00:00")["id"]
    db.attend(conn, first, 1, "b", "Bob")
    db.add_boss(conn, G, "2026-10-21T20:00:00+00:00")  # yesterday: not in today's totals
    db.attend(conn, db.get_boss(conn, G, "2026-10-21T20:00:00+00:00")["id"], 2, "a", "ann")
    now = datetime(2026, 10, 22, 8, 30, tzinfo=timezone.utc)
    info, sections = attend_data_sections(conn, G, tracked, now)
    eight = int(datetime(2026, 10, 22, 8, tzinfo=timezone.utc).timestamp())
    assert info == (eight, eight + 3600, "open")
    assert sections["attended"] == [] and sections["not_attended"] == [("a", "ann"), ("b", "Bob")]
    assert sections["today_totals"] == [("ann", "0/2"), ("Bob", "1/2")]


def test_boss_lists_and_member_day():
    conn = conn_with((1, "Ann"))
    for hour in (2, 8, 14):
        db.add_boss(conn, G, f"2026-10-22T{hour:02}:00:00+00:00")
    first = db.get_boss(conn, G, "2026-10-22T02:00:00+00:00")["id"]
    db.attend(conn, first, 1, "a", "Ann")
    attended, waiting = boss_lists(conn, G, [(1, "a"), (2, "Bo")], first)
    assert [row[:2] for row in attended] == [("a", "Ann")] and waiting == ["Bo (not linked)"]
    two = int(datetime(2026, 10, 22, 2, tzinfo=timezone.utc).timestamp())
    now = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)  # 14:00 not started yet
    assert member_day(conn, G, 1, DAY, now) == [(two, True), (two + 6 * 3600, False)]
    assert member_day(conn, G, 1, YDAY, now) == []


def test_attend_report_and_missed_in_row():
    conn = conn_with((1, "Ann"), (2, "bob"), (3, "Cy"))
    tracked = [(1, "a"), (2, "b"), (3, "c"), (4, "d")]  # 4 not linked: left out
    now = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
    assert missed_in_row(conn, G, tracked, now, 4) == (0, [])  # no bosses yet: nobody flagged
    ids = []
    for t in ("2026-10-21T20:00:00+00:00", "2026-10-22T02:00:00+00:00", "2026-10-22T08:00:00+00:00",
              "2026-10-22T14:00:00+00:00"):  # the last one has not started
        db.add_boss(conn, G, t)
        ids.append(db.get_boss(conn, G, t)["id"])
    db.attend(conn, ids[0], 1, "a", "Ann")
    db.attend(conn, ids[1], 1, "a", "Ann")
    db.attend(conn, ids[0], 2, "b", "bob")
    assert attend_report(conn, G, tracked, YDAY, DAY, now) == (3, [("Cy", 0), ("bob", 1), ("Ann", 2)])
    assert attend_report(conn, G, tracked, DAY, DAY, now) == (2, [("bob", 0), ("Cy", 0), ("Ann", 1)])
    assert missed_in_row(conn, G, tracked, now, 2) == (2, ["bob", "Cy"])
    assert missed_in_row(conn, G, tracked, now, 10) == (3, ["Cy"])


def test_link_lines():
    assert link_lines({2: "zed", 1: "Alpha_1"}) == [r"**Alpha\_1** · <@1>", "**zed** · <@2>"]


def test_pack_lines():
    assert pack_lines(["a" * 10] * 35, limit=2000) == ["\n".join(["a" * 10] * 35)]  # fits: one message
    assert pack_lines(["aaaa", "bbbb", "cc"], limit=9) == ["aaaa\nbbbb", "cc"]
