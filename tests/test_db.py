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
    assert db.link(conn, G, A, " Dragon ") == "Dragon"
    with pytest.raises(db.AlreadyLinked):
        db.link(conn, G, A, "Other")
    with pytest.raises(db.IgnTaken):
        db.link(conn, G, B, "dragon")  # unique ignoring case
    with pytest.raises(ValueError):
        db.link(conn, G, B, "x")
    assert db.unlink(conn, G, A)
    db.link(conn, G, B, "Dragon")


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
    assert db.void(conn, first, staff_id=9)
    donate(conn)
    assert [r["status"] for r in db.history(conn, G, A)] == ["approved", "voided"]


def test_void_transitions(conn):
    sid = donate(conn)
    assert db.void(conn, sid, 9) is True
    assert db.void(conn, sid, 9) is False  # already voided
    assert db.void(conn, 999, 9) is False  # missing


def test_log_marks_staff(conn):
    sid = donate(conn, logged_by=9)
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert row["logged_by"] == 9
    assert db.get_approved(conn, G, A, DAY)["id"] == sid
