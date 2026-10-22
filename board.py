from datetime import datetime, timedelta, timezone

import db
from config import RECENT_DONORS


def join_capped(names, limit=1000):
    """Join names with commas; if too long, cut and add '+N more'. limit = Discord field cap."""
    text = ", ".join(names)
    if len(text) <= limit:
        return text
    shown = []
    for name in names:
        suffix = f" +{len(names) - len(shown) - 1} more"
        if len(", ".join([*shown, name])) + len(suffix) > limit:
            break
        shown.append(name)
    return f"{', '.join(shown)} +{len(names) - len(shown)} more"


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


def board_data(conn, guild_id, tracked, today, yesterday, now=None):
    today_rows = db.approved_for_day(conn, guild_id, today)
    y_count, y_coins = db.day_totals(conn, guild_id, yesterday)
    return {
        "today_count": len(today_rows),
        "reset_ts": next_reset_ts(now),
        "recent": [(r["ign"], r["amount"]) for r in today_rows[:RECENT_DONORS]],
        "waiting": [name for _, name in not_donated(conn, guild_id, tracked, today)],
        "yesterday_count": y_count,
        "yesterday_coins": y_coins,
    }
