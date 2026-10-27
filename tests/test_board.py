from datetime import date, datetime, timedelta, timezone

import db
from board import (boss_streak, log_all_plan, attend_board_data, bar, dashboard_stats, best_streak, member_card, streak, top_streaks, attend_data_sections, attend_report, board_data, boss_lists, member_day,
                   data_sections, day_lists, join_capped, link_lines, log_all, member_ids, missed_in_row, next_reset_ts, next_week_ts, not_donated,
                   monthly_csv, monthly_report, pack_lines, paginate, short, weekly_leaderboard, xp_cap_counts, xp_day, xp_week_totals)

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
    assert data["yesterday_count"] == 2


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
    assert data_sections(conn, G, [], DAY) == {"donated": [], "not_donated": [], "on_loa": [], "not_linked": []}


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
    donated, waiting, on_loa = day_lists(conn, G, [(1, "A"), (2, "B"), (3, "C")], DAY)
    assert [(name, ign) for name, ign, _ in donated] == [("B", "Beta")]
    assert waiting == ["Alpha", "C (not linked)"] and on_loa == []


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
        None, {"attended": [], "not_attended": [], "attend_on_loa": [], "today_totals": [("ann", "0/0"), ("Bob", "0/0")]})
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
    attended, waiting, on_loa = boss_lists(conn, G, [(1, "a"), (2, "Bo")], first)
    assert [row[:2] for row in attended] == [("a", "Ann")] and waiting == ["Bo (not linked)"] and on_loa == []
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
    assert link_lines({2: "zed", 1: "Alpha_1", 3: "a`b"}) == ["`1. a'b    `  <@3>", "`2. Alpha_1`  <@1>", "`3. zed    `  <@2>"]


def test_pack_lines():
    assert pack_lines(["a" * 10] * 35, limit=2000) == ["\n".join(["a" * 10] * 35)]  # fits: one message
    assert pack_lines(["aaaa", "bbbb", "cc"], limit=9) == ["aaaa\nbbbb", "cc"]


def test_log_all_skips_member_donated_and_unlinked():
    conn = conn_with((1, "Aa"), (2, "Bb"), (3, "Cc"), (4, None))
    db.record(conn, G, 2, "two", "Bb", DAY)  # already donated
    tracked = [(1, "one"), (2, "two"), (3, "three"), (4, "four")]
    logged, unlinked = log_all(conn, G, tracked, DAY, 3, 99)
    assert ([ign for _, ign in logged], unlinked) == (["Aa"], ["four"])
    assert db.get_submission(conn, G, logged[0][0])["logged_by"] == 99
    assert log_all(conn, G, tracked, DAY, 3, 99)[0] == []  # a second run logs nobody twice
    assert not_donated(conn, G, tracked, DAY) == [(3, "Cc"), (4, "four (not linked)")]


def test_member_ids_reads_mentions_and_ids_once():
    text = "<@111111111111111111> <@!222222222222222222>, 333333333333333333 <@111111111111111111> 42"
    assert member_ids(text) == [111111111111111111, 222222222222222222, 333333333333333333]


def test_streaks_and_card():
    days = {"2026-10-20", "2026-10-21", "2026-10-22", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"}
    assert streak(days, "2026-10-22") == 3
    assert streak(days, "2026-10-23") == 3  # today not logged yet: the streak still stands
    assert streak(days, "2026-10-24") == 0
    assert best_streak(days) == 4
    conn = conn_with((1, "Aa"), (2, "Bb"))
    for d in ("2026-10-20", "2026-10-21", "2026-10-22"):
        db.record(conn, G, 1, "one", "Aa", d)
    db.record(conn, G, 2, "two", "Bb", "2026-10-22")
    tracked = [(1, "one"), (2, "two")]
    assert top_streaks(conn, G, tracked, DAY) == [("Aa", 3)]
    card = member_card(conn, G, tracked, 2, datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
    assert (card["today"], card["streak"], card["month_days"], card["month_rank"], card["members"]) == (True, 1, 1, 2, 2)
    assert (card["month_elapsed"], card["all_days"], card["next_badge"]) == (22, 1, (6, "7-day streak"))
    assert card["last_14"] == [False] * 13 + [True]
    assert bar(3, 6, width=4) == "██░░" and bar(0, 0) == "░" * 10


def test_weekly_leaderboard():
    conn = conn_with((1, "Aa"), (2, "Bb"), (3, "Cc"))
    start = date(2026, 10, 19)
    for k in range(7):
        db.record(conn, G, 1, "one", "Aa", (start + timedelta(days=k)).isoformat())
    for k in (0, 1):
        db.record(conn, G, 2, "two", "Bb", (start + timedelta(days=k)).isoformat())
    db.record(conn, G, 1, "one", "Aa", "2026-10-18")  # week before: Aa 1 day, Bb 0
    data = weekly_leaderboard(conn, G, [(1, "one"), (2, "two"), (3, "three")], start)
    assert data == {"top": [("Aa", 7), ("Bb", 2)], "perfect": ["Aa"], "improved": [("Aa", 6), ("Bb", 2)]}


def test_monthly_report_and_boss_streak():
    conn = conn_with((1, "Aa"), (2, "Bb"))
    for t in ("2026-10-21T12:00:00+00:00", "2026-10-22T12:00:00+00:00", "2026-10-22T18:00:00+00:00"):
        db.add_boss(conn, G, t)
    ids = [b["id"] for b in db.started_bosses(conn, G, "2026-10-31T00:00:00+00:00")]  # newest first
    for b in ids[1:]:
        db.attend(conn, b, 1, "one", "Aa")
    db.attend(conn, ids[0], 2, "two", "Bb")
    db.record(conn, G, 1, "one", "Aa", DAY)
    db.record(conn, G, 1, "one", "Aa", "2026-09-30")  # other month
    # 18:30: the 18:00 boss is still open, so Aa's streak of two holds
    open_now = datetime(2026, 10, 22, 18, 30, tzinfo=timezone.utc)
    later = datetime(2026, 10, 31, tzinfo=timezone.utc)
    assert dict(monthly_report(conn, G, [(1, "one"), (2, "two")], "2026-10", open_now)["streaks"])["Aa"] == 2
    report = monthly_report(conn, G, [(1, "one"), (2, "two")], "2026-10", later)
    assert report["rows"] == [("Aa", "one", 1, 2), ("Bb", "two", 0, 1)]
    assert (report["boss_total"], report["days_in_month"]) == (3, 31)
    assert report["streaks"] == [("Bb", 1), ("Aa", 0)]
    assert monthly_csv(report).splitlines()[1] == "Aa,one,1,2,3,0"


def test_dashboard_stats():
    conn = conn_with((1, "Aa"), (2, "Bb"), (3, "Cc"))  # 3 is linked but lost the role
    for d in ("2026-10-19", "2026-10-20", "2026-10-21"):
        db.record(conn, G, 1, "one", "Aa", d)
    db.record(conn, G, 2, "two", "Bb", DAY)
    tracked = [(1, "one"), (2, "two"), (4, "Dee")]  # 4 has the role but no link
    stats = dashboard_stats(conn, G, tracked, datetime(2026, 10, 22, 12, tzinfo=timezone.utc))  # a Thursday
    assert stats["week"][-4:] == [(date(2026, 10, 19), 1), (date(2026, 10, 20), 1), (date(2026, 10, 21), 1),
                                  (date(2026, 10, 22), 1)]
    assert stats["week_rate"] == (4, 8)  # Monday to Thursday, 2 linked members
    assert stats["month_rate"] == (4, 44)
    assert stats["at_risk"] == [("Aa", 3)]
    assert (stats["not_linked"], stats["stale_links"]) == (["Dee"], ["Cc"])


def test_xp_day_and_week_reset():
    conn = conn_with((1, "Ann"), (2, "Bob"), (3, "Cid"), (4, None))
    db.xp_set(conn, G, 2, DAY, 9, total=900)
    db.xp_set(conn, G, 3, DAY, 9, gain=1500)
    tracked = [(1, "ann"), (2, "bob"), (3, "cid"), (4, "dee")]  # dee is not linked: left out
    assert xp_day(conn, G, tracked, DAY) == [(3, "cid", "Cid", 1500), (2, "bob", "Bob", 900), (1, "ann", "Ann", None)]
    assert xp_week_totals(conn, G, tracked, DAY) == {1: 0, 2: 900, 3: 1500}
    assert xp_cap_counts(conn, G, tracked, DAY) == [(date(2026, 10, 19), 0), (date(2026, 10, 20), 0),
                                                    (date(2026, 10, 21), 0), (date(2026, 10, 22), 1)]
    sunday_night = datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc)
    assert next_week_ts(sunday_night) == int(datetime(2026, 10, 26, tzinfo=timezone.utc).timestamp())
    assert next_week_ts(sunday_night + timedelta(minutes=1)) == int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())


def on_leave(conn, member, start, days):
    db.approve_loa(conn, G, db.loa_request(conn, G, member, "trip", days, start, start), 9, start)


def test_streak_skips_loa_days():
    days = {"2026-10-18", "2026-10-19", "2026-10-22"}
    loa = {"2026-10-20", "2026-10-21"}
    assert streak(days, "2026-10-22") == 1  # without LOA the gap breaks it
    assert streak(days, "2026-10-22", loa) == 3  # LOA days neither count nor break
    assert streak({"2026-10-19"}, "2026-10-22", loa | {"2026-10-22"}) == 1  # today on LOA, nothing logged
    assert best_streak(days) == 2 and best_streak(days, loa) == 3
    assert best_streak({"2026-10-18", "2026-10-22"}, {"2026-10-20"}) == 1  # a gap not fully LOA still breaks


def test_loa_member_is_not_waiting_and_shows_as_loa():
    conn = conn_with((1, "Alpha"), (2, "Beta"))
    on_leave(conn, 1, "2026-10-21", 3)  # 21st to 23rd
    tracked = [(1, "A"), (2, "B")]
    assert not_donated(conn, G, tracked, DAY) == [(2, "Beta")]
    assert [r[0] for r in not_donated(conn, G, tracked, "2026-10-24")] == [1, 2]  # leave is over
    assert data_sections(conn, G, tracked, DAY)["on_loa"] == [("Alpha", "2026-10-23")]
    assert data_sections(conn, G, tracked, DAY)["not_donated"] == [("B", "Beta")]
    donated, waiting, on_loa = day_lists(conn, G, tracked, YDAY)
    assert (waiting, on_loa) == (["Beta"], [("Alpha", "2026-10-23")])
    assert day_lists(conn, G, [(2, "B")], DAY)[2] == [("Alpha", "2026-10-23")]  # lost the role: still listed
    to_log, _ = log_all_plan(conn, G, tracked, DAY, 0)
    assert [i for i, *_ in to_log] == [2]  # /log_all skips the member on LOA


def test_member_card_with_loa():
    conn = conn_with((1, "Alpha"))
    for day in ("2026-10-18", "2026-10-19", "2026-10-22"):
        db.record(conn, G, 1, "A", "Alpha", day)
    on_leave(conn, 1, "2026-10-20", 2)  # 20th and 21st
    card = member_card(conn, G, [], 1, datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
    assert card["loa"] is None and card["streak"] == 3 and card["best"] == 3
    assert card["month_loa"] == 2 and card["month_elapsed"] == 20  # 22 days minus 2 LOA
    assert card["last_14_loa"][-3:] == [True, True, False]
    on_card = member_card(conn, G, [], 1, datetime(2026, 10, 21, 9, tzinfo=timezone.utc))
    assert on_card["loa"] == "2026-10-21" and on_card["last_14_loa"][-1]


def test_boss_streak_and_missed_skip_loa_days():
    conn = conn_with((1, "Ann"), (2, "Bob"))
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        db.add_boss(conn, G, f"{day}T08:00:00+00:00")
    ids = {d: db.get_boss(conn, G, f"{d}T08:00:00+00:00")["id"] for d in ("2026-10-20", "2026-10-21", "2026-10-22")}
    db.attend(conn, ids["2026-10-22"], 1, "a", "Ann")
    db.attend(conn, ids["2026-10-20"], 1, "a", "Ann")
    on_leave(conn, 1, "2026-10-21", 1)
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    bosses = db.started_bosses(conn, G, now.isoformat(timespec="seconds"))
    attended = db.attended_bosses(conn, G)[1]
    assert boss_streak(bosses, attended, now) == 1
    assert boss_streak(bosses, attended, now, {"2026-10-21"}) == 2
    assert missed_in_row(conn, G, [(1, "a"), (2, "b")], now, 3) == (3, ["Bob"])
    on_leave(conn, 2, "2026-10-20", 3)  # Bob on LOA for all three
    assert missed_in_row(conn, G, [(1, "a"), (2, "b")], now, 3) == (3, [])


def test_dashboard_stats_and_attendance_lists_with_loa():
    conn = conn_with((1, "Aa"), (2, "Bb"))
    on_leave(conn, 2, "2026-10-21", 2)  # 21st and 22nd
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)  # a Thursday: week since Mon 19th = 4 days
    stats = dashboard_stats(conn, G, [(1, "a")], now)  # Bb lost the member role for the leave
    assert stats["on_loa"] == [("Bb", "2026-10-22")]
    assert stats["stale_links"] == []  # no role is expected while on LOA
    assert stats["week_rate"] == (0, 4)
    both = dashboard_stats(conn, G, [(1, "a"), (2, "b")], now)  # role not swapped yet
    assert both["week_rate"] == (0, 4)  # Bb is on LOA today, so Bb is left out even with the role
    back = dashboard_stats(conn, G, [(1, "a"), (2, "b")], datetime(2026, 10, 24, 12, tzinfo=timezone.utc))
    assert back["week_rate"] == (0, 6 + 6 - 2)  # Mon 19th to Sat 24th: Aa 6 days, Bb 6 minus 2 LOA days
    db.add_boss(conn, G, "2026-10-22T08:00:00+00:00")
    info, sections = attend_data_sections(conn, G, [(1, "a")], now)
    assert sections["attend_on_loa"] == [("Bb", "2026-10-22")] and sections["not_attended"] == [("a", "Aa")]
    boss_id = db.get_boss(conn, G, "2026-10-22T08:00:00+00:00")["id"]
    assert boss_lists(conn, G, [(1, "a"), (2, "b")], boss_id)[1:] == (["Aa"], [("Bb", "2026-10-22")])


def test_xp_and_monthly_with_loa():
    conn = conn_with((1, "Aa"), (2, "Bb"))
    on_leave(conn, 2, "2026-10-22", 1)
    tracked = [(1, "a"), (2, "b")]
    assert [r[2] for r in xp_day(conn, G, tracked, DAY)] == ["Aa"]
    assert [r[2] for r in xp_day(conn, G, tracked, YDAY)] == ["Aa", "Bb"]
    assert xp_week_totals(conn, G, tracked, DAY) == {1: 0}
    report = monthly_report(conn, G, tracked, "2026-10", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert report["loa_days"] == {"Aa": 0, "Bb": 1}
    assert monthly_csv(report).splitlines()[2].endswith(",1")
