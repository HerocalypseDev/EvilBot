from datetime import datetime, timedelta, timezone

from discord.utils import escape_markdown

import boss
import db
from config import RECENT_DONORS


def short(text, width=16):
    """Cut to `width` characters, then escape markdown. Cutting after escaping could leave a stray backslash."""
    return escape_markdown(text if len(text) <= width else text[:width - 1] + "…")


def join_capped(names, limit=1000, sep=", "):
    """Join names with `sep`; if too long, cut and add '+N more'. limit = Discord field cap."""
    text = sep.join(names)
    if len(text) <= limit:
        return text
    shown = []
    for name in names:
        suffix = f"{sep}+{len(names) - len(shown) - 1} more"
        if len(sep.join([*shown, name])) + len(suffix) > limit:
            break
        shown.append(name)
    return f"{sep.join(shown)}{sep}+{len(names) - len(shown)} more" if shown else f"+{len(names)} more"


def paginate(items, page, size=10):
    """Returns (items on the page, page clamped to the valid range, page count)."""
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages


def next_reset_ts(now=None):
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def not_donated(conn, guild_id, tracked, day):
    """tracked = [(discord_id, display_name)]. Returns [(discord_id, board_name)] who have no
    approved donation for `day`. Board name is the in-game name, or 'Name (not linked)'."""
    donated = {r["discord_id"] for r in db.approved_for_day(conn, guild_id, day)}
    igns = db.get_igns(conn, guild_id)
    return [(i, igns.get(i) or f"{name} (not linked)") for i, name in tracked if i not in donated]


def data_sections(conn, guild_id, tracked, today):
    """Lists for the data channel. tracked = [(discord_id, display_name)].
    donated: (discord name, in-game name, unix time), newest first.
    not_donated: linked members with no donation today.
    not_linked: roster names nobody has claimed with /link yet (in-game name only)."""
    donated_ids = set()
    donated = []
    for r in db.approved_for_day(conn, guild_id, today):
        donated_ids.add(r["discord_id"])
        donated.append((r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp())))
    igns = db.get_igns(conn, guild_id)
    return {
        "donated": donated,
        "not_donated": sorted(((name, igns[i]) for i, name in tracked if i in igns and i not in donated_ids),
                              key=lambda row: row[1].lower()),
        "not_linked": [(ign,) for ign in db.roster_names(conn, guild_id, only_free=True)],
    }


def day_lists(conn, guild_id, tracked, day):
    """For /history. Returns (donated as (discord name, in-game name, unix time), not donated as board names)."""
    donated = [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
               for r in db.approved_for_day(conn, guild_id, day)]
    return donated, [name for _, name in not_donated(conn, guild_id, tracked, day)]


def board_data(conn, guild_id, tracked, today, yesterday, now=None):
    today_rows = db.approved_for_day(conn, guild_id, today)
    y_count, y_coins = db.day_totals(conn, guild_id, yesterday)
    return {
        "today_count": len(today_rows),
        "reset_ts": next_reset_ts(now),
        "recent": [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
                   for r in today_rows[:RECENT_DONORS]],
        "waiting": [name for _, name in not_donated(conn, guild_id, tracked, today)],
        "yesterday_count": y_count,
        "yesterday_coins": y_coins,
    }


def not_attended(conn, guild_id, tracked, boss_id):
    """Like not_donated, for one boss. Returns [(discord_id, board_name)]."""
    attended = {r["discord_id"] for r in db.attendance_for_boss(conn, boss_id)}
    igns = db.get_igns(conn, guild_id)
    return [(i, igns.get(i) or f"{name} (not linked)") for i, name in tracked if i not in attended]


def attend_board_data(conn, guild_id, tracked, now):
    """Attendance board for the newest boss that has started, or None if there is none yet."""
    now_text = now.isoformat(timespec="seconds")
    row = db.started_boss(conn, guild_id, now_text)
    if not row:
        return None
    t = datetime.fromisoformat(row["boss_time"])
    return {
        "boss_id": row["id"],
        "boss_ts": int(t.timestamp()),
        "close_ts": int((t + boss.ATTEND_WINDOW).timestamp()),
        "state": boss.attend_state(t, now),
        "attended": [r["ign"] for r in db.attendance_for_boss(conn, row["id"])],
        "waiting": [name for _, name in not_attended(conn, guild_id, tracked, row["id"])],
        "today": [(int(datetime.fromisoformat(b["boss_time"]).timestamp()),
                   len(db.attendance_for_boss(conn, b["id"])))
                  for b in db.bosses_for_day(conn, guild_id, now.date().isoformat()) if b["boss_time"] <= now_text],
    }


def attend_data_sections(conn, guild_id, tracked, now):
    """Lists for the attendance data channel. Returns (boss info or None, sections).
    info: (boss unix time, close unix time, state) of the newest boss that has started.
    attended: (discord name, in-game name, unix time logged), newest first.
    not_attended: linked members who did not log that boss, by in-game name.
    today_totals: (in-game name, 'attended/bosses') for linked members, bosses started today."""
    now_text = now.isoformat(timespec="seconds")
    row = db.started_boss(conn, guild_id, now_text)
    igns = db.get_igns(conn, guild_id)
    linked = sorted(((i, name, igns[i]) for i, name in tracked if i in igns), key=lambda r: r[2].lower())
    info, attended, attended_ids = None, [], set()
    if row:
        t = datetime.fromisoformat(row["boss_time"])
        info = (int(t.timestamp()), int((t + boss.ATTEND_WINDOW).timestamp()), boss.attend_state(t, now))
        for r in db.attendance_for_boss(conn, row["id"]):
            attended_ids.add(r["discord_id"])
            attended.append((r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp())))
    today = now.date().isoformat()
    total, counts = db.attend_counts(conn, guild_id, today, today, now_text)
    return info, {
        "attended": attended,
        "not_attended": [(name, ign) for i, name, ign in linked if i not in attended_ids] if row else [],
        "today_totals": [(ign, f"{counts.get(i, 0)}/{total}") for i, _, ign in linked],
    }
