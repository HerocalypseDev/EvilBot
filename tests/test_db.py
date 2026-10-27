import sqlite3

import pytest

import db
from config import DONATION_AMOUNT

G, A, B, DAY = 1, 100, 200, "2026-10-22"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def donate(conn, member=A, day=DAY, **kw):
    return db.record(conn, G, member, "name", "ign", day, **kw)


def test_link_rules(conn):
    db.roster_add(conn, G, ["Dragon", "Other"])
    assert db.link(conn, G, A, " dragon ") == "Dragon"  # roster spelling is stored
    with pytest.raises(db.AlreadyLinked):
        db.link(conn, G, A, "Other")
    with pytest.raises(db.IgnTaken):
        db.link(conn, G, B, "DRAGON")  # taken, ignoring case
    with pytest.raises(db.NotInRoster):
        db.link(conn, G, B, "Nobody")
    assert db.unlink(conn, G, A)
    db.link(conn, G, B, "Dragon")


def test_parse_names():
    assert db.parse_names("Ann, bob\nC,  ann ,, Dee ") == (["Ann", "bob", "Dee"], ["C"])
    assert db.roster_names(db.connect(":memory:"), 1, "a!b") == []  # ! in a search is plain text
    assert db.parse_names("") == ([], [])
    assert db.parse_names("x" * 33) == ([], ["x" * 33])


def test_roster_add_remove_and_search(conn):
    assert db.roster_add(conn, G, ["Ann", "Bob_1", "Cyd"]) == 3
    assert db.roster_add(conn, G, ["ann", "Dee"]) == 1  # "ann" already there
    assert db.roster_names(conn, G) == ["Ann", "Bob_1", "Cyd", "Dee"]
    assert db.roster_names(conn, G, "B") == ["Bob_1"]
    assert db.roster_names(conn, G, "_") == ["Bob_1"]  # _ is not a wildcard
    assert db.roster_names(conn, G, "%") == []
    db.link(conn, G, A, "Ann")
    assert db.roster_names(conn, G, only_free=True) == ["Bob_1", "Cyd", "Dee"]
    assert db.roster_names(conn, G, only_free=True, limit=2) == ["Bob_1", "Cyd"]
    assert db.roster_remove(conn, G, "cyd") and not db.roster_remove(conn, G, "cyd")
    assert db.roster_names(conn, 2) == []  # other guild


def test_unlink_returns_name_to_the_list(conn):
    db.roster_add(conn, G, ["Ann"])
    db.link(conn, G, A, "Ann")
    db.unlink(conn, G, A)
    assert db.roster_names(conn, G, only_free=True) == ["Ann"]


def test_save_guild_inserts_then_updates(conn):
    db.save_guild(conn, G, panel_channel_id=5, officer_role_id=6)
    db.save_guild(conn, G, panel_channel_id=7)
    row = db.get_guild(conn, G)
    assert (row["panel_channel_id"], row["officer_role_id"]) == (7, 6)
    with pytest.raises(ValueError):
        db.save_guild(conn, G, nope=1)


def test_record_is_approved_fixed_amount(conn):
    sid = donate(conn)
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert (row["status"], row["amount"], row["logged_by"]) == ("approved", DONATION_AMOUNT, None)


def test_second_donation_same_day_refused(conn):
    donate(conn)
    with pytest.raises(db.AlreadyDonated):
        donate(conn)
    with pytest.raises(db.AlreadyDonated):
        donate(conn, logged_by=9)
    donate(conn, day="2026-10-23")  # other day fine
    donate(conn, member=B)  # other member fine


def test_voided_does_not_block_new_donation(conn):
    first = donate(conn)
    assert db.void(conn, G, first, staff_id=9)
    donate(conn)
    assert [r["status"] for r in db.history(conn, G, A)] == ["approved", "voided"]


def test_void_transitions(conn):
    sid = donate(conn)
    assert db.void(conn, G, sid, 9) is True
    assert db.void(conn, G, sid, 9) is False  # already voided
    assert db.void(conn, G, 999, 9) is False  # missing


def test_void_other_guild_refused(conn):
    sid = donate(conn)
    assert db.void(conn, 2, sid, 9) is False
    assert db.get_approved(conn, G, A, DAY)["id"] == sid


def test_export_csv_escapes_formulas_and_keeps_unicode(conn):
    db.record(conn, G, A, "=cmd()", "Zoë", DAY)
    lines = db.export_csv(conn, G).splitlines()
    assert lines[0].startswith("id,day_utc,discord_id")
    assert "'=cmd()" in lines[1] and "Zoë" in lines[1]


def test_log_marks_staff(conn):
    sid = donate(conn, logged_by=9)
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert row["logged_by"] == 9
    assert db.get_approved(conn, G, A, DAY)["id"] == sid


def test_boss_attendance(conn):
    t = "2026-10-22T02:00:00+00:00"
    db.add_boss(conn, G, t)
    db.add_boss(conn, G, t)  # second ping for the same time is ignored
    assert db.started_boss(conn, G, "2026-10-22T01:59:59+00:00") is None
    boss = db.started_boss(conn, G, t)
    assert boss["day_utc"] == DAY and [b["id"] for b in db.bosses_for_day(conn, G, DAY)] == [boss["id"]]
    assert db.get_boss(conn, G, t)["id"] == boss["id"] and db.get_boss(conn, 2, t) is None
    assert db.claim_boss_open(conn, boss["id"]) and not db.claim_boss_open(conn, boss["id"])
    db.attend(conn, boss["id"], A, "a", "Ann")
    with pytest.raises(db.AlreadyAttended):
        db.attend(conn, boss["id"], A, "a", "Ann", logged_by=9)
    db.attend(conn, boss["id"], B, "b", "Bob", logged_by=9)
    assert [r["discord_id"] for r in db.attendance_for_boss(conn, boss["id"])] == [B, A]  # newest first
    assert db.unattend(conn, boss["id"], A) and not db.unattend(conn, boss["id"], A)
    db.attend(conn, boss["id"], A, "a", "Ann")  # can log again after a staff removal


def test_connect_adds_new_columns_to_an_old_database(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE guilds (guild_id INTEGER PRIMARY KEY, panel_channel_id INTEGER)")
    old.execute("INSERT INTO guilds VALUES (1, 5)")
    old.commit()
    old.close()
    conn = db.connect(tmp_path / "old.db")
    db.save_guild(conn, 1, staff_channel_id=9, welcome_channel_id=8)
    row = db.get_guild(conn, 1)
    assert (row["panel_channel_id"], row["staff_channel_id"]) == (5, 9)


def test_next_boss(conn):
    assert db.next_boss(conn, G, "2026-10-22T13:00:00+00:00") is None
    db.add_boss(conn, G, "2026-10-22T12:00:00+00:00")
    db.add_boss(conn, G, "2026-10-22T14:00:00+00:00")
    assert db.next_boss(conn, G, "2026-10-22T13:00:00+00:00")["boss_time"] == "2026-10-22T14:00:00+00:00"
    assert db.next_boss(conn, G, "2026-10-22T14:00:00+00:00") is None  # started: not next any more


def test_xp_week_gains_gaps_and_caps(conn):
    mon, tue, wed, thu = "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"
    assert db.week_start("2026-11-01") == mon and db.week_start(mon) == mon
    db.xp_set(conn, G, A, mon, 9, total=1500)
    db.xp_set(conn, G, A, wed, 9, total=4000)  # Tuesday not logged: Wednesday gets the rise since Monday
    week = db.xp_week(conn, G, A, thu)
    assert [(d["gain"], d["total"], d["cap"]) for d in week[:3]] == [(1500, 1500, 1500), (None, None, None),
                                                                      (2500, 4000, 3000)]
    with pytest.raises(db.XpInvalid) as bad:
        db.xp_set(conn, G, A, thu, 9, total=3999)  # below Wednesday
    assert bad.value.args == (thu, -1, 1500)
    with pytest.raises(db.XpInvalid):
        db.xp_set(conn, G, A, thu, 9, total=5501)  # over the cap
    with pytest.raises(db.XpInvalid):
        db.xp_set(conn, G, A, tue, 9, total=4100)  # makes Wednesday negative
    assert db.xp_week(conn, G, A, mon)[1]["gain"] is None  # failed logs saved nothing
    db.xp_set(conn, G, A, mon, 9, gain=1500)  # approved request: a gain, not a total
    db.xp_set(conn, G, A, tue, 9, total=2600)  # re-logging a day replaces it
    assert [d["gain"] for d in db.xp_week(conn, G, A, mon)[:3]] == [1500, 1100, 1400]
    assert db.xp_gains(conn, G, tue) == {A: 1100}
    assert db.xp_delete(conn, G, A, tue) and not db.xp_delete(conn, G, A, tue)
    assert db.xp_week(conn, G, A, "2026-11-02")[0]["gain"] is None  # new week starts empty


def test_xp_requests(conn):
    rid = db.xp_request(conn, G, A, DAY)
    with pytest.raises(db.AlreadyRequested):
        db.xp_request(conn, G, A, DAY)
    assert db.xp_pending(conn, G) == {A} and db.xp_pending(conn, G, "2026-10-23") == set()
    assert db.close_xp_request(conn, G, rid, "approved", 9)
    assert not db.close_xp_request(conn, G, rid, "denied", 9)  # already closed
    assert db.get_xp_request(conn, G, rid)["status"] == "approved" and db.xp_pending(conn, G) == set()


def test_xp_cap_days_across_weeks(conn):
    db.xp_set(conn, G, A, "2026-10-25", 9, total=3000)  # Sunday: 1500 over two days, not at cap
    db.xp_set(conn, G, A, "2026-10-24", 9, total=1500)  # Saturday at cap, so Sunday is too
    db.xp_set(conn, G, A, "2026-10-26", 9, gain=1500)  # Monday, new week
    db.xp_set(conn, G, A, "2026-10-27", 9, total=2000)
    assert db.xp_cap_days(conn, G, A) == {"2026-10-24", "2026-10-25", "2026-10-26"}


def test_parse_loa():
    today = "2026-10-27"
    assert db.parse_loa(" 5 ", "", today) == (5, today)
    assert db.parse_loa("14", "2026-11-03", today) == (14, "2026-11-03")  # 7 days ahead is the limit
    assert db.parse_loa("1", "2026-10-27", today) == (1, today)
    for days, start in (("0", ""), ("15", ""), ("x", ""), ("2.5", ""), ("3", "2026-10-26"),
                        ("3", "2026-11-04"), ("3", "next week"), ("3", "27-10-2026")):
        with pytest.raises(db.LoaInvalid):
            db.parse_loa(days, start, today)


def test_loa_request_rules(conn):
    today = "2026-10-27"
    first = db.loa_request(conn, G, A, "trip", 3, today, today)
    with pytest.raises(db.LoaBusy) as e:
        db.loa_request(conn, G, A, "again", 1, today, today)  # still pending
    assert e.value.args == ("pending", None)
    db.loa_request(conn, G, B, "other member", 1, today, today)  # other member is fine
    row = db.approve_loa(conn, G, first, 9, today)
    assert (row["start_day"], row["end_day"]) == ("2026-10-27", "2026-10-29")
    with pytest.raises(db.LoaBusy) as e:
        db.loa_request(conn, G, A, "again", 1, "2026-10-29", "2026-10-29")  # last day still counts
    assert e.value.args == ("active", "2026-10-29")
    # leave ended 10-29: 10-30 + 7 days wait -> first free day is 11-06
    with pytest.raises(db.LoaCooldown) as e:
        db.loa_request(conn, G, A, "again", 1, "2026-11-05", "2026-11-05")
    assert e.value.args == ("2026-11-06",)
    db.loa_request(conn, G, A, "again", 1, "2026-11-06", "2026-11-06")


def test_loa_denied_has_no_cooldown_and_approve_once(conn):
    today = "2026-10-27"
    first = db.loa_request(conn, G, A, "trip", 3, today, today)
    assert db.deny_loa(conn, G, first, 9, "need you this week")
    assert not db.deny_loa(conn, G, first, 9, "x")
    assert db.approve_loa(conn, G, first, 9, today) is None  # already closed
    second = db.loa_request(conn, G, A, "trip", 3, today, today)
    assert db.get_loa(conn, G, second)["status"] == "pending"
    assert db.get_loa(conn, 2, second) is None  # other guild


def test_loa_start_moves_to_approval_day_when_late(conn):
    loa = db.loa_request(conn, G, A, "trip", 2, "2026-10-25", "2026-10-25")
    row = db.approve_loa(conn, G, loa, 9, "2026-10-27")  # staff was slow
    assert (row["start_day"], row["end_day"]) == ("2026-10-27", "2026-10-28")
    future = db.loa_request(conn, G, B, "trip", 2, "2026-10-30", "2026-10-27")
    assert db.approve_loa(conn, G, future, 9, "2026-10-27")["start_day"] == "2026-10-30"


def test_loa_active_on(conn):
    loa = db.loa_request(conn, G, A, "trip", 3, "2026-10-27", "2026-10-27")
    assert db.loa_active_on(conn, G, "2026-10-27") == {}  # pending does not count
    db.approve_loa(conn, G, loa, 9, "2026-10-27")
    assert db.loa_active_on(conn, G, "2026-10-26") == {}
    assert db.loa_active_on(conn, G, "2026-10-27") == {A: "2026-10-29"}
    assert db.loa_active_on(conn, G, "2026-10-29") == {A: "2026-10-29"}
    assert db.loa_active_on(conn, G, "2026-10-30") == {}
    assert db.loa_active_on(conn, 2, "2026-10-28") == {}


def test_loa_start_and_end_are_claimed_once(conn):
    loa = db.loa_request(conn, G, A, "trip", 2, "2026-10-27", "2026-10-27")
    db.approve_loa(conn, G, loa, 9, "2026-10-27")
    assert [r["id"] for r in db.loa_due_start(conn, G, "2026-10-27")] == [loa]
    assert db.loa_due_start(conn, G, "2026-10-26") == []  # not started yet
    assert db.claim_loa_start(conn, loa) and not db.claim_loa_start(conn, loa)
    assert db.loa_due_start(conn, G, "2026-10-27") == []
    assert db.loa_due_end(conn, G, "2026-10-28") == []  # last day is still LOA
    assert [r["id"] for r in db.loa_due_end(conn, G, "2026-10-29")] == [loa]
    assert db.claim_loa_end(conn, loa) and not db.claim_loa_end(conn, loa)
    assert db.loa_due_end(conn, G, "2026-10-29") == []
    assert db.loa_active_on(conn, G, "2026-10-28") == {A: "2026-10-28"}  # past days stay marked LOA


def test_loa_guild_fields(conn):
    db.save_guild(conn, G, loa_channel_id=1, loa_review_channel_id=2, loa_role_id=3, loa_message_id=4)
    row = db.get_guild(conn, G)
    assert (row["loa_channel_id"], row["loa_review_channel_id"], row["loa_role_id"], row["loa_message_id"]) == (1, 2, 3, 4)


def test_loa_check_and_delete_request(conn):
    today = "2026-10-27"
    db.loa_check(conn, G, A, today)  # nothing yet: fine
    loa = db.loa_request(conn, G, A, "trip", 2, today, today)
    with pytest.raises(db.LoaBusy):
        db.loa_check(conn, G, A, today)
    db.delete_loa_request(conn, G, loa)
    db.loa_check(conn, G, A, today)  # the card never posted: the member may try again
    approved = db.loa_request(conn, G, A, "trip", 2, today, today)
    db.approve_loa(conn, G, approved, 9, today)
    db.delete_loa_request(conn, G, approved)  # only pending rows can be deleted
    assert db.get_loa(conn, G, approved)["status"] == "approved"
