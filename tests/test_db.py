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
