import pytest

import db

G, A, B, DAY = 1, 100, 200, "2026-10-22"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def donate(conn, member=A, day=DAY, amount=50000, image_hash="h1"):
    sid = db.create_draft(conn, G, member, "name", "ign", day, amount)
    db.approve_draft(conn, sid, "http://img", image_hash)
    return sid


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


def test_second_donation_same_day_refused(conn):
    donate(conn)
    with pytest.raises(db.AlreadyDonated):
        db.create_draft(conn, G, A, "name", "ign", DAY, 1)
    with pytest.raises(db.AlreadyDonated):
        db.log_donation(conn, G, A, "name", "ign", DAY, 1, staff_id=9)


def test_new_draft_replaces_old_draft(conn):
    s1 = db.create_draft(conn, G, A, "n", "i", DAY, 10)
    s2 = db.create_draft(conn, G, A, "n", "i", DAY, 20)
    assert db.get_draft(conn, G, A, DAY)["id"] == s2
    assert db.approve_draft(conn, s1, "u", "h") is False  # old draft is gone


def test_index_blocks_overlapping_approvals(conn):
    # two drafts that slipped past create_draft (overlapping commands): index refuses the second approval
    for _ in range(2):
        conn.execute("INSERT INTO submissions (guild_id, discord_id, discord_name, ign, day_utc, amount,"
                     " status, created_at) VALUES (1, 100, 'n', 'i', ?, 10, 'awaiting_proof', 'now')", (DAY,))
    first, second = [r["id"] for r in conn.execute("SELECT id FROM submissions")]
    assert db.approve_draft(conn, first, "u", "h1")
    with pytest.raises(db.AlreadyDonated):
        db.approve_draft(conn, second, "u", "h2")


def test_duplicate_image_refused_until_voided(conn):
    first = donate(conn, member=A, image_hash="same")
    other = db.create_draft(conn, G, B, "n", "i", DAY, 5)
    with pytest.raises(db.ImageInUse):
        db.approve_draft(conn, other, "u", "same")
    assert db.void(conn, first, staff_id=9)
    assert db.approve_draft(conn, other, "u", "same")


def test_voided_does_not_block_new_donation(conn):
    first = donate(conn)
    assert db.void(conn, first, staff_id=9)
    donate(conn, image_hash="h2")
    assert [r["status"] for r in db.history(conn, G, A)] == ["approved", "voided"]


def test_status_transitions(conn):
    sid = db.create_draft(conn, G, A, "n", "i", DAY, 10)
    assert db.void(conn, sid, 9) is False  # draft cannot be voided
    assert db.approve_draft(conn, sid, "u", "h") is True
    assert db.approve_draft(conn, sid, "u", "h") is False  # already approved
    assert db.void(conn, sid, 9) is True
    assert db.void(conn, sid, 9) is False  # already voided


def test_log_donation_marks_staff(conn):
    sid = db.log_donation(conn, G, A, "n", "i", DAY, 500, staff_id=9)
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert (row["status"], row["logged_by"], row["image_hash"]) == ("approved", 9, None)
    # no image: two logs for different members both fine
    db.log_donation(conn, G, B, "n", "i", DAY, 500, staff_id=9)


def test_log_donation_duplicate_image(conn):
    db.log_donation(conn, G, A, "n", "i", DAY, 5, 9, image_hash="x")
    with pytest.raises(db.ImageInUse):
        db.log_donation(conn, G, B, "n", "i", DAY, 5, 9, image_hash="x")
