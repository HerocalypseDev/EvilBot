import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from config import DONATION_AMOUNT, IGN_MAX, IGN_MIN, XP_CAP

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id          INTEGER PRIMARY KEY,
    panel_channel_id  INTEGER,
    status_message_id INTEGER,
    officer_role_id   INTEGER,
    member_role_id    INTEGER,
    board_day         TEXT,
    boss_channel_id   INTEGER,
    data_channel_id   INTEGER,
    data_message_ids  TEXT
);
CREATE TABLE IF NOT EXISTS roster (
    guild_id INTEGER NOT NULL,
    ign      TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (guild_id, ign)
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
CREATE TABLE IF NOT EXISTS boss_polls (
    guild_id      INTEGER NOT NULL,
    cycle_start   TEXT NOT NULL,
    message_id    INTEGER NOT NULL,
    resolved_time TEXT,
    PRIMARY KEY (guild_id, cycle_start)
);
CREATE TABLE IF NOT EXISTS reminders (
    guild_id       INTEGER NOT NULL,
    day_utc        TEXT NOT NULL,
    minutes_before INTEGER NOT NULL,
    sent_at        TEXT NOT NULL,
    PRIMARY KEY (guild_id, day_utc, minutes_before)
);
CREATE TABLE IF NOT EXISTS posts (
    guild_id INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    period   TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (guild_id, kind, period)
);
CREATE TABLE IF NOT EXISTS bosses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    day_utc    TEXT NOT NULL,
    boss_time  TEXT NOT NULL,
    opened_at  TEXT,
    UNIQUE (guild_id, boss_time)
);
CREATE TABLE IF NOT EXISTS attendance (
    boss_id      INTEGER NOT NULL REFERENCES bosses (id),
    discord_id   INTEGER NOT NULL,
    discord_name TEXT NOT NULL,
    ign          TEXT NOT NULL,
    logged_by    INTEGER,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (boss_id, discord_id)
);
CREATE TABLE IF NOT EXISTS xp_logs (
    guild_id   INTEGER NOT NULL,
    discord_id INTEGER NOT NULL,
    day_utc    TEXT NOT NULL,
    total      INTEGER,
    gain       INTEGER,
    logged_by  INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, discord_id, day_utc),
    CHECK ((total IS NULL) != (gain IS NULL))
);
CREATE TABLE IF NOT EXISTS xp_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    discord_id  INTEGER NOT NULL,
    day_utc     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending','approved','denied')),
    reviewed_by INTEGER,
    created_at  TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (guild_id, discord_id, day_utc)
);
"""


class AlreadyDonated(Exception):
    pass


class AlreadyLinked(Exception):
    pass


class IgnTaken(Exception):
    pass


class NotInRoster(Exception):
    pass


class AlreadyAttended(Exception):
    pass


class AlreadyRequested(Exception):
    pass


class XpInvalid(Exception):
    """A log that would give some day of the week a gain below 0 or above the cap. args: (day, gain, cap)."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def yesterday_utc():
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(boss_polls)")}
    for column in ("ping_message_id", "cleaned_at"):  # columns added after the first release
        if column not in have:
            conn.execute(f"ALTER TABLE boss_polls ADD COLUMN {column} {'INTEGER' if column.endswith('id') else 'TEXT'}")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(guilds)")}
    for column, kind in (("boss_channel_id", "INTEGER"), ("data_channel_id", "INTEGER"),
                         ("data_message_ids", "TEXT"), ("attend_channel_id", "INTEGER"),
                         ("attend_message_id", "INTEGER"), ("attend_data_channel_id", "INTEGER"),
                         ("attend_data_message_ids", "TEXT"),
                         ("staff_channel_id", "INTEGER"),
                         ("welcome_channel_id", "INTEGER"), ("xp_channel_id", "INTEGER"),
                         ("xp_message_id", "INTEGER"), ("xp_review_channel_id", "INTEGER")):  # columns added after the first release
        if column not in have:
            conn.execute(f"ALTER TABLE guilds ADD COLUMN {column} {kind}")
    return conn


# --- guild config ---

GUILD_FIELDS = {"panel_channel_id", "status_message_id", "officer_role_id",
                "member_role_id", "board_day", "boss_channel_id",
                "data_channel_id", "data_message_ids", "attend_channel_id",
                "attend_message_id", "attend_data_channel_id", "attend_data_message_ids", "staff_channel_id",
                "welcome_channel_id", "xp_channel_id", "xp_message_id", "xp_review_channel_id"}


def save_guild(conn, guild_id, **fields):
    bad = fields.keys() - GUILD_FIELDS
    if bad:
        raise ValueError(f"Unknown guild fields: {sorted(bad)}")
    cols = ", ".join(["guild_id", *fields])
    marks = ", ".join("?" * (len(fields) + 1))
    updates = ", ".join(f"{c} = excluded.{c}" for c in fields)
    with conn:
        conn.execute(
            f"INSERT INTO guilds ({cols}) VALUES ({marks}) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {updates}", (guild_id, *fields.values()))


def get_guild(conn, guild_id):
    return conn.execute("SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)).fetchone()


# --- boss polls ---

def add_boss_poll(conn, guild_id, cycle_start, message_id):
    with conn:
        conn.execute("INSERT INTO boss_polls (guild_id, cycle_start, message_id) VALUES (?, ?, ?)",
                     (guild_id, cycle_start, message_id))


def latest_boss_poll(conn, guild_id):
    return conn.execute("SELECT * FROM boss_polls WHERE guild_id = ? ORDER BY cycle_start DESC LIMIT 1",
                        (guild_id,)).fetchone()


def resolve_boss_poll(conn, guild_id, cycle_start, resolved_time):
    """Claim the end of a poll. True only the first time, so a restart never double-pings."""
    with conn:
        return conn.execute(
            "UPDATE boss_polls SET resolved_time = ? WHERE guild_id = ? AND cycle_start = ? "
            "AND resolved_time IS NULL", (resolved_time, guild_id, cycle_start)).rowcount == 1


def poll_by_message(conn, message_id):
    return conn.execute("SELECT * FROM boss_polls WHERE message_id = ?", (message_id,)).fetchone()


def save_ping_message(conn, guild_id, cycle_start, message_id):
    with conn:
        conn.execute("UPDATE boss_polls SET ping_message_id = ? WHERE guild_id = ? AND cycle_start = ?",
                     (message_id, guild_id, cycle_start))


def polls_to_clean(conn, guild_id, before, limit=5):
    """Closed polls whose cycle started before `before` (ISO 8601 UTC) and whose messages are not deleted yet."""
    return conn.execute("SELECT * FROM boss_polls WHERE guild_id = ? AND cycle_start < ? AND resolved_time IS NOT NULL "
                        "AND cleaned_at IS NULL ORDER BY cycle_start LIMIT ?", (guild_id, before, limit)).fetchall()


def mark_poll_cleaned(conn, guild_id, cycle_start):
    with conn:
        conn.execute("UPDATE boss_polls SET cleaned_at = ? WHERE guild_id = ? AND cycle_start = ?",
                     (now_iso(), guild_id, cycle_start))


# --- boss attendance: one boss row per ping, at the winning time ---

def add_boss(conn, guild_id, boss_time):
    """boss_time = ISO 8601 UTC. Day = its UTC date. A second add for the same time is ignored."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO bosses (guild_id, day_utc, boss_time) VALUES (?, ?, ?)",
                     (guild_id, boss_time[:10], boss_time))


def started_boss(conn, guild_id, now):
    """Newest boss whose time is at or before `now` (ISO 8601 UTC). The next boss is added at its
    ping, 10 minutes early, so the newest row can still be ahead while the last one's window is open."""
    return conn.execute("SELECT * FROM bosses WHERE guild_id = ? AND boss_time <= ? "
                        "ORDER BY boss_time DESC LIMIT 1", (guild_id, now)).fetchone()


def next_boss(conn, guild_id, now):
    """The first boss after `now` (ISO 8601 UTC). It exists from its ping, 10 minutes before. None if none yet."""
    return conn.execute("SELECT * FROM bosses WHERE guild_id = ? AND boss_time > ? ORDER BY boss_time LIMIT 1",
                        (guild_id, now)).fetchone()


def bosses_for_day(conn, guild_id, day):
    return conn.execute("SELECT * FROM bosses WHERE guild_id = ? AND day_utc = ? ORDER BY boss_time",
                        (guild_id, day)).fetchall()


def get_boss(conn, guild_id, boss_time):
    return conn.execute("SELECT * FROM bosses WHERE guild_id = ? AND boss_time = ?",
                        (guild_id, boss_time)).fetchone()


def claim_boss_open(conn, boss_id):
    """Claim the 'logging open' ping. True only the first time, so a restart never double-pings."""
    with conn:
        return conn.execute("UPDATE bosses SET opened_at = ? WHERE id = ? AND opened_at IS NULL",
                            (now_iso(), boss_id)).rowcount == 1


def attend(conn, boss_id, discord_id, discord_name, ign, logged_by=None):
    """Raises AlreadyAttended if the member is already logged for that boss."""
    try:
        with conn:
            conn.execute("INSERT INTO attendance VALUES (?, ?, ?, ?, ?, ?)",
                         (boss_id, discord_id, discord_name, ign, logged_by, now_iso()))
    except sqlite3.IntegrityError:
        raise AlreadyAttended from None


def unattend(conn, boss_id, discord_id):
    with conn:
        return conn.execute("DELETE FROM attendance WHERE boss_id = ? AND discord_id = ?",
                            (boss_id, discord_id)).rowcount == 1


def attend_counts(conn, guild_id, first_day, last_day, now):
    """Bosses from first_day to last_day that started by `now` (ISO 8601 UTC).
    Returns (boss count, {discord_id: bosses attended})."""
    where = "guild_id = ? AND day_utc BETWEEN ? AND ? AND boss_time <= ?"
    args = (guild_id, first_day, last_day, now)
    total = conn.execute(f"SELECT COUNT(*) FROM bosses WHERE {where}", args).fetchone()[0]
    counts = {r[0]: r[1] for r in conn.execute(
        f"SELECT discord_id, COUNT(*) FROM attendance WHERE boss_id IN (SELECT id FROM bosses WHERE {where}) "
        "GROUP BY discord_id", args)}
    return total, counts


def recent_bosses(conn, guild_id, now, count):
    """Ids of the newest `count` bosses that started by `now` (ISO 8601 UTC)."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM bosses WHERE guild_id = ? AND boss_time <= ? ORDER BY boss_time DESC LIMIT ?",
        (guild_id, now, count))]


def started_bosses(conn, guild_id, now):
    """Bosses that started by `now` (ISO 8601 UTC), newest first."""
    return conn.execute("SELECT * FROM bosses WHERE guild_id = ? AND boss_time <= ? ORDER BY boss_time DESC",
                        (guild_id, now)).fetchall()


def attended_bosses(conn, guild_id):
    """{discord_id: set of boss ids attended}."""
    out = {}
    for r in conn.execute("SELECT a.discord_id, a.boss_id FROM attendance a JOIN bosses b ON b.id = a.boss_id "
                          "WHERE b.guild_id = ?", (guild_id,)):
        out.setdefault(r[0], set()).add(r[1])
    return out


def attendance_for_boss(conn, boss_id):
    """Newest first."""
    return conn.execute("SELECT * FROM attendance WHERE boss_id = ? ORDER BY created_at DESC, rowid DESC",
                        (boss_id,)).fetchall()


# --- reminders ---

def mark_reminder(conn, guild_id, day, minutes_before):
    """Claim a reminder slot. True only the first time, so a restart never double-sends."""
    with conn:
        return conn.execute(
            "INSERT OR IGNORE INTO reminders VALUES (?, ?, ?, ?)",
            (guild_id, day, minutes_before, now_iso())).rowcount == 1


def claim_post(conn, guild_id, kind, period):
    """Claim a scheduled post (e.g. kind 'weekly', period '2026-10-19'). True only the first time."""
    with conn:
        return conn.execute("INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?)",
                            (guild_id, kind, period, now_iso())).rowcount == 1


# --- roster: the in-game names staff can pick in /link_member ---

def parse_names(text):
    """Split on commas or new lines. Returns (valid names, names with a bad length), no duplicates."""
    seen, good, bad = set(), [], []
    for raw in re.split(r"[,\n]", text):
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            (good if IGN_MIN <= len(name) <= IGN_MAX else bad).append(name)
    return good, bad


def roster_add(conn, guild_id, names):
    """Returns how many names were new."""
    with conn:
        return conn.executemany("INSERT OR IGNORE INTO roster VALUES (?, ?)",
                                [(guild_id, n) for n in names]).rowcount


def roster_remove(conn, guild_id, ign):
    with conn:
        return conn.execute("DELETE FROM roster WHERE guild_id = ? AND ign = ?",
                            (guild_id, ign.strip())).rowcount == 1


def roster_names(conn, guild_id, text="", only_free=False, limit=-1):
    """Roster names containing `text` (any case), sorted. only_free hides names already linked."""
    like = "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"  # ! escapes wildcards
    sql = "SELECT ign FROM roster WHERE guild_id = ? AND ign LIKE ? ESCAPE '!'"
    if only_free:
        sql += " AND ign NOT IN (SELECT ign FROM links WHERE guild_id = ?)"
    sql += " ORDER BY ign LIMIT ?"
    args = (guild_id, like, *((guild_id,) if only_free else ()), limit)
    return [r["ign"] for r in conn.execute(sql, args)]


# --- links ---

def link(conn, guild_id, discord_id, ign):
    """Link a member to a name from the roster (the roster's spelling is stored)."""
    row = conn.execute("SELECT ign FROM roster WHERE guild_id = ? AND ign = ?",
                       (guild_id, ign.strip())).fetchone()
    if not row:
        raise NotInRoster
    ign = row["ign"]
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


def void(conn, guild_id, submission_id, staff_id):
    """approved -> voided. Returns False if the row is not an approved row of this guild."""
    with conn:
        return conn.execute(
            "UPDATE submissions SET status = 'voided', reviewed_by = ?, reviewed_at = ? "
            "WHERE id = ? AND guild_id = ? AND status = 'approved'",
            (staff_id, now_iso(), submission_id, guild_id)
        ).rowcount == 1


def get_submission(conn, guild_id, submission_id):
    return conn.execute("SELECT * FROM submissions WHERE id = ? AND guild_id = ?",
                        (submission_id, guild_id)).fetchone()


def approved_for_day(conn, guild_id, day, limit=-1):
    """Approved donations for a day, newest first."""
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND day_utc = ? AND status = 'approved' "
        "ORDER BY id DESC LIMIT ?", (guild_id, day, limit)).fetchall()


def day_totals(conn, guild_id, day):
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM submissions "
        "WHERE guild_id = ? AND day_utc = ? AND status = 'approved'", (guild_id, day)).fetchone()
    return row[0], row[1]


def donation_days(conn, guild_id):
    """{discord_id: set of UTC days with an approved donation}."""
    days = {}
    for r in conn.execute("SELECT discord_id, day_utc FROM submissions WHERE guild_id = ? AND status = 'approved'",
                          (guild_id,)):
        days.setdefault(r["discord_id"], set()).add(r["day_utc"])
    return days


def get_igns(conn, guild_id):
    return {r["discord_id"]: r["ign"] for r in
            conn.execute("SELECT discord_id, ign FROM links WHERE guild_id = ?", (guild_id,))}


def history(conn, guild_id, discord_id):
    return conn.execute(
        "SELECT * FROM submissions WHERE guild_id = ? AND discord_id = ? "
        "ORDER BY id DESC", (guild_id, discord_id)).fetchall()


_CSV_COLUMNS = ("id", "day_utc", "discord_id", "discord_name", "ign", "amount", "status",
                "logged_by", "reviewed_by", "created_at", "reviewed_at")


def safe_cell(value):
    # a leading = + - @ makes Excel and Sheets run the cell as a formula
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(conn, guild_id):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(_CSV_COLUMNS)
    for row in conn.execute("SELECT * FROM submissions WHERE guild_id = ? ORDER BY id", (guild_id,)):
        writer.writerow([safe_cell(row[c]) for c in _CSV_COLUMNS])
    return out.getvalue()


# --- XP: officers log each member's weekly running total (weeks start Monday 00:00 UTC at 0).
# An approved "I hit cap" request stores a gain of XP_CAP instead of a total. ---

def week_start(day):
    """Monday (ISO date) of the week holding `day` (ISO date)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return (d - timedelta(days=d.weekday())).isoformat()


def xp_week_days(rows, monday):
    """rows: the member's xp_logs rows in the week. Returns 7 dicts Monday first: day, gain, total, cap,
    logged_by. gain/total are None on a day with no log. A total log's gain is the rise since the last logged
    day, so a missed day's XP lands on the next log. cap = XP_CAP times the days since the last log."""
    by_day = {r["day_utc"]: r for r in rows}
    start = datetime.strptime(monday, "%Y-%m-%d").date()
    out, last, last_k = [], 0, -1
    for k in range(7):
        day = (start + timedelta(days=k)).isoformat()
        r = by_day.get(day)
        if not r:
            out.append({"day": day, "gain": None, "total": None, "cap": None, "logged_by": None})
            continue
        gain = r["gain"] if r["total"] is None else r["total"] - last
        last += gain
        out.append({"day": day, "gain": gain, "total": last, "cap": XP_CAP * (k - last_k), "logged_by": r["logged_by"]})
        last_k = k
    return out


def xp_week(conn, guild_id, discord_id, day):
    """The 7 days (see xp_week_days) of the week holding `day`."""
    monday = week_start(day)
    sunday = (datetime.strptime(monday, "%Y-%m-%d").date() + timedelta(days=6)).isoformat()
    rows = conn.execute("SELECT * FROM xp_logs WHERE guild_id = ? AND discord_id = ? AND day_utc BETWEEN ? AND ?",
                        (guild_id, discord_id, monday, sunday)).fetchall()
    return xp_week_days(rows, monday)


def xp_set(conn, guild_id, discord_id, day, logged_by, total=None, gain=None):
    """Save one day's log (replaces that day's log). Pass total (weekly running total) or gain.
    Raises XpInvalid, and saves nothing, if any day of the week would end up below 0 or above its cap."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO xp_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (guild_id, discord_id, day, total, gain, logged_by, now_iso()))
        for d in xp_week(conn, guild_id, discord_id, day):
            if d["gain"] is not None and not 0 <= d["gain"] <= d["cap"]:
                raise XpInvalid(d["day"], d["gain"], d["cap"])  # rolls the insert back


def xp_delete(conn, guild_id, discord_id, day):
    with conn:
        return conn.execute("DELETE FROM xp_logs WHERE guild_id = ? AND discord_id = ? AND day_utc = ?",
                            (guild_id, discord_id, day)).rowcount == 1


def xp_gains(conn, guild_id, day):
    """{discord_id: that day's gain} for every member logged on `day`."""
    ids = [r[0] for r in conn.execute("SELECT discord_id FROM xp_logs WHERE guild_id = ? AND day_utc = ?",
                                      (guild_id, day))]
    return {i: next(d["gain"] for d in xp_week(conn, guild_id, i, day) if d["day"] == day) for i in ids}


def xp_request(conn, guild_id, discord_id, day):
    """Open an "I hit cap" request. Raises AlreadyRequested if the member has one for that day."""
    try:
        with conn:
            return conn.execute("INSERT INTO xp_requests (guild_id, discord_id, day_utc, status, created_at) "
                                "VALUES (?, ?, ?, 'pending', ?)", (guild_id, discord_id, day, now_iso())).lastrowid
    except sqlite3.IntegrityError:
        raise AlreadyRequested from None


def delete_xp_request(conn, guild_id, request_id):
    """For a request whose card could not be posted, so the member can try again."""
    with conn:
        conn.execute("DELETE FROM xp_requests WHERE id = ? AND guild_id = ?", (request_id, guild_id))


def get_xp_request(conn, guild_id, request_id):
    return conn.execute("SELECT * FROM xp_requests WHERE id = ? AND guild_id = ?",
                        (request_id, guild_id)).fetchone()


def close_xp_request(conn, guild_id, request_id, status, staff_id):
    """pending -> approved/denied. False if already closed."""
    with conn:
        return conn.execute("UPDATE xp_requests SET status = ?, reviewed_by = ?, reviewed_at = ? "
                            "WHERE id = ? AND guild_id = ? AND status = 'pending'",
                            (status, staff_id, now_iso(), request_id, guild_id)).rowcount == 1


def xp_pending(conn, guild_id, day=None):
    """Discord ids with a pending request, on `day` or on any day."""
    sql = "SELECT discord_id FROM xp_requests WHERE guild_id = ? AND status = 'pending'"
    return {r[0] for r in conn.execute(sql + (" AND day_utc = ?" if day else ""), (guild_id, *([day] if day else [])))}
