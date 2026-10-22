import sqlite3
from datetime import datetime, timezone

from config import DONATION_AMOUNT, IGN_MAX, IGN_MIN

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id          INTEGER PRIMARY KEY,
    panel_channel_id  INTEGER,
    panel_message_id  INTEGER,
    status_message_id INTEGER,
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
    amount            INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('approved','voided')),
    reviewed_by       INTEGER,
    logged_by         INTEGER,
    created_at        TEXT NOT NULL,
    reviewed_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_day
    ON submissions (guild_id, discord_id, day_utc) WHERE status = 'approved';
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

def record(conn, guild_id, discord_id, discord_name, ign, day, logged_by=None):
    """Insert an approved donation. Raises AlreadyDonated if one exists for that day."""
    try:
        with conn:
            return conn.execute(
                """INSERT INTO submissions (guild_id, discord_id, discord_name, ign, day_utc,
                       amount, status, logged_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?)""",
                (guild_id, discord_id, discord_name, ign, day, DONATION_AMOUNT, logged_by, now_iso()),
            ).lastrowid
    except sqlite3.IntegrityError:
        raise AlreadyDonated from None


def get_approved(conn, guild_id, discord_id, day):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? AND day_utc = ? "
        "AND status = 'approved'", (guild_id, discord_id, day)).fetchone()


def void(conn, submission_id, staff_id):
    """approved -> voided. Returns False if the row is not approved."""
    with conn:
        return conn.execute(
            "UPDATE submissions SET status = 'voided', reviewed_by = ?, reviewed_at = ? "
            "WHERE id = ? AND status = 'approved'", (staff_id, now_iso(), submission_id)
        ).rowcount == 1


def history(conn, guild_id, discord_id):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? "
        "ORDER BY id DESC", (guild_id, discord_id)).fetchall()
