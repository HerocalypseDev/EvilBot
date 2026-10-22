from datetime import datetime, timedelta, timezone

from discord.utils import escape_markdown

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
