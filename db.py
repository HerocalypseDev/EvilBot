import sqlite3
from datetime import datetime, timezone

from config import IGN_MAX, IGN_MIN

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id          INTEGER PRIMARY KEY,
    panel_channel_id  INTEGER,
    panel_message_id  INTEGER,
    status_message_id INTEGER,
    review_channel_id INTEGER,
    officer_role_id   INTEGER,
    member_role_id    INTEGER,
    board_day         TEXT,
    backup_day        TEXT
);
CREATE TABLE IF NOT EXISTS links (
    guild_id   INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    ign        TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (guild_id, discord_id),
    UNIQUE (guild_id, ign)
);
CREATE TABLE IF NOT EXISTS submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    discord_id        INTEGER NOT NULL,
    discord_name      TEXT NOT NULL,
    ign               TEXT NOT NULL,
    day_utc           TEXT NOT NULL,
    amount            INTEGER NOT NULL CHECK (amount BETWEEN 1 AND 50000),
    status            TEXT NOT NULL CHECK (status IN ('awaiting_proof','approved','voided')),
    image_url         TEXT,
    image_hash        TEXT,
    review_message_id INTEGER,
    reviewed_by       INTEGER,
    logged_by         INTEGER,
    created_at        TEXT NOT NULL,
    reviewed_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_day
    ON submissions (guild_id, discord_id, day_utc) WHERE status = 'approved';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_image
    ON submissions (guild_id, image_hash) WHERE image_hash IS NOT NULL AND status = 'approved';
CREATE TABLE IF NOT EXISTS reminders (
    guild_id       INTEGER NOT NULL,
    day_utc        TEXT NOT NULL,
    minutes_before INTEGER NOT NULL,
    sent_at        TEXT NOT NULL,
    PRIMARY KEY (guild_id, day_utc, minutes_before)
);
"""


class AlreadyDonated(Exception):
    pass


class ImageInUse(Exception):
    pass


class AlreadyLinked(Exception):
    pass


class IgnTaken(Exception):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --- links ---

def link(conn, guild_id, discord_id, ign):
    ign = ign.strip()
    if not IGN_MIN <= len(ign) <= IGN_MAX:
        raise ValueError(f"In-game name must be {IGN_MIN} to {IGN_MAX} characters.")
    if get_ign(conn, guild_id, discord_id):
        raise AlreadyLinked
    try:
        with conn:
            conn.execute("INSERT INTO links VALUES (?, ?, ?)", (guild_id, discord_id, ign))
    except sqlite3.IntegrityError:
        raise IgnTaken from None
    return ign


def unlink(conn, guild_id, discord_id):
    with conn:
        return conn.execute(
            "DELETE FROM links WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
        ).rowcount == 1


def get_ign(conn, guild_id, discord_id):
    row = conn.execute(
        "SELECT ign FROM links WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
    ).fetchone()
    return row["ign"] if row else None


# --- submissions ---

def _insert_approved(conn, guild_id, discord_id, discord_name, ign, day, amount,
                     image_url, image_hash, review_message_id, logged_by):
    try:
        with conn:
            return conn.execute(
                """INSERT INTO submissions (guild_id, discord_id, discord_name, ign, day_utc,
                       amount, status, image_url, image_hash, review_message_id, logged_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                (guild_id, discord_id, discord_name, ign, day, amount,
                 image_url, image_hash, review_message_id, logged_by, now_iso()),
            ).lastrowid
    except sqlite3.IntegrityError as e:
        raise (ImageInUse if "image_hash" in str(e) else AlreadyDonated) from None


def create_draft(conn, guild_id, discord_id, discord_name, ign, day, amount):
    """New draft replaces any earlier draft for the same member and day."""
    if get_approved(conn, guild_id, discord_id, day):
        raise AlreadyDonated
    with conn:
        conn.execute(
            "DELETE FROM submissions WHERE guild_id = ? AND discord_id = ? AND day_utc = ? "
            "AND status = 'awaiting_proof'", (guild_id, discord_id, day))
        return conn.execute(
            """INSERT INTO submissions (guild_id, discord_id, discord_name, ign, day_utc,
                   amount, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'awaiting_proof', ?)""",
            (guild_id, discord_id, discord_name, ign, day, amount, now_iso()),
        ).lastrowid


def get_draft(conn, guild_id, discord_id, day):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? AND day_utc = ? "
        "AND status = 'awaiting_proof'", (guild_id, discord_id, day)).fetchone()


def get_approved(conn, guild_id, discord_id, day):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? AND day_utc = ? "
        "AND status = 'approved'", (guild_id, discord_id, day)).fetchone()


def approve_draft(conn, submission_id, image_url, image_hash, review_message_id=None):
    """awaiting_proof -> approved. Returns False if the row is not a draft."""
    try:
        with conn:
            return conn.execute(
                """UPDATE submissions SET status = 'approved', image_url = ?, image_hash = ?,
                       review_message_id = ? WHERE id = ? AND status = 'awaiting_proof'""",
                (image_url, image_hash, review_message_id, submission_id),
            ).rowcount == 1
    except sqlite3.IntegrityError as e:
        raise (ImageInUse if "image_hash" in str(e) else AlreadyDonated) from None


def log_donation(conn, guild_id, discord_id, discord_name, ign, day, amount, staff_id,
                 image_url=None, image_hash=None, review_message_id=None):
    return _insert_approved(conn, guild_id, discord_id, discord_name, ign, day, amount,
                            image_url, image_hash, review_message_id, staff_id)


def void(conn, submission_id, staff_id):
    """approved -> voided. Returns False if the row is not approved."""
    with conn:
        return conn.execute(
            "UPDATE submissions SET status = 'voided', reviewed_by = ?, reviewed_at = ? "
            "WHERE id = ? AND status = 'approved'", (staff_id, now_iso(), submission_id)
        ).rowcount == 1


def history(conn, guild_id, discord_id):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? AND status != 'awaiting_proof' "
        "ORDER BY id DESC", (guild_id, discord_id)).fetchall()
