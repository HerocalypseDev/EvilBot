import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

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


def link_lines(igns):
    """igns = {discord_id: ign}. One line per link, sorted by in-game name. The number and name sit in
    inline code padded to one width, so the monospace font lines up every @member in one column."""
    rows = sorted(igns.items(), key=lambda x: x[1].lower())
    width = max((len(ign) for ign in igns.values()), default=0)
    num = len(str(len(rows)))
    # a backtick would end the inline code early
    return [f"`{k:>{num}}. {ign.replace('`', chr(39)):<{width}}`  <@{i}>" for k, (i, ign) in enumerate(rows, 1)]


def pack_lines(lines, limit=2000):
    """Join lines into as few messages as fit, each at most `limit` characters (Discord's message cap)."""
    messages = [""]
    for line in lines:
        if messages[-1] and len(messages[-1]) + 1 + len(line) > limit:
            messages.append("")
        messages[-1] += ("\n" if messages[-1] else "") + line
    return messages


def paginate(items, page, size=10):
    """Returns (items on the page, page clamped to the valid range, page count)."""
    pages = max(1, -(-len(items) // size))
    page = min(max(page, 0), pages - 1)
    return items[page * size:(page + 1) * size], page, pages


def next_reset_ts(now=None):
    now = now or datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def next_week_ts(now=None):
    """Next Monday 00:00 UTC, when weekly XP goes back to 0."""
    now = now or datetime.now(timezone.utc)
    monday = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp())


def streak(days, today):
    """Days in a row with a donation, up to today. A streak still counts while today is not logged yet."""
    d = date.fromisoformat(today)
    if today not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


def best_streak(days):
    best = run = 0
    prev = None
    for d in sorted(date.fromisoformat(x) for x in days):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        best, prev = max(best, run), d
    return best


def badge(n):
    """Badge for a streak of n days, or None."""
    return "30-day streak" if n >= 30 else "7-day streak" if n >= 7 else None


def top_streaks(conn, guild_id, tracked, today, count=5):
    """Linked members with the longest running streaks (2 days or more): [(in-game name, days)]."""
    igns, days = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id)
    rows = [(igns[i], streak(days.get(i, set()), today)) for i, _ in tracked if i in igns]
    return sorted((r for r in rows if r[1] >= 2), key=lambda r: (-r[1], r[0].lower()))[:count]


def bar(done, total, width=10):
    """Text progress bar, e.g. '███████░░░'."""
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def next_badge(n):
    """(days to go, badge name) for the next streak badge, or None after the last one."""
    for goal in (7, 30):
        if n < goal:
            return goal - n, badge(goal)
    return None


def member_card(conn, guild_id, tracked, discord_id, now):
    """Numbers for /me. Ranks are among tracked members (plus the member, if they lost the role)."""
    today = now.date().isoformat()
    days = db.donation_days(conn, guild_id)
    mine = days.get(discord_id, set())
    month = today[:7]
    ids = {i for i, _ in tracked} | {discord_id}
    month_counts = {i: sum(d.startswith(month) for d in days.get(i, set())) for i in ids}
    all_counts = {i: len(days.get(i, set())) for i in ids}
    last = next((r for r in db.history(conn, guild_id, discord_id) if r["status"] == "approved"), None)
    boss_total, boss_counts = db.attend_counts(conn, guild_id, f"{month}-01", today, now.isoformat(timespec="seconds"))
    d = date.fromisoformat(today)
    return {
        "today": today in mine,
        "streak": streak(mine, today),
        "best": best_streak(mine),
        "next_badge": next_badge(streak(mine, today)),
        "month_days": month_counts[discord_id],
        "month_elapsed": d.day,
        "month_rank": 1 + sum(c > month_counts[discord_id] for c in month_counts.values()),
        "all_days": all_counts[discord_id],
        "all_rank": 1 + sum(c > all_counts[discord_id] for c in all_counts.values()),
        "members": len(ids),
        "last_ts": int(datetime.fromisoformat(last["created_at"]).timestamp()) if last else None,
        "last_14": [(d - timedelta(days=k)).isoformat() in mine for k in range(13, -1, -1)],  # oldest first
        "boss_attended": boss_counts.get(discord_id, 0),
        "boss_total": boss_total,
        "boss_streak": boss_streak(db.started_bosses(conn, guild_id, now.isoformat(timespec="seconds")),
                                   db.attended_bosses(conn, guild_id).get(discord_id, set()), now),
    }


def weekly_leaderboard(conn, guild_id, tracked, week_start):
    """Linked members' donation days in the 7 days from week_start (a date), against the week before.
    top: [(ign, days)] most first; perfect: igns with 7 of 7; improved: [(ign, days gained)] most first."""
    igns, days = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id)
    week = {(week_start + timedelta(days=k)).isoformat() for k in range(7)}
    before = {(week_start - timedelta(days=k)).isoformat() for k in range(1, 8)}
    rows = [(igns[i], len(days.get(i, set()) & week), len(days.get(i, set()) & before))
            for i, _ in tracked if i in igns]
    by_name = sorted(rows, key=lambda r: r[0].lower())
    return {
        "top": [(ign, n) for ign, n, _ in sorted(by_name, key=lambda r: -r[1]) if n][:10],
        "perfect": [ign for ign, n, _ in by_name if n == 7],
        "improved": [(ign, n - b) for ign, n, b in sorted(by_name, key=lambda r: r[2] - r[1]) if n > b][:3],
    }


def boss_streak(bosses, attended, now):
    """Bosses in a row attended, newest first. bosses = started boss rows, newest first. A boss whose
    logging is still open does not break the streak yet."""
    n = 0
    for k, b in enumerate(bosses):
        if b["id"] in attended:
            n += 1
        elif not (k == 0 and boss.attend_state(datetime.fromisoformat(b["boss_time"]), now) == "open"):
            break
    return n


def monthly_report(conn, guild_id, tracked, month, now):
    """month = 'YYYY-MM'. Linked members' month: [(ign, discord name, donation days, bosses attended)] by name,
    plus the boss count, the current boss streaks and the month's day count."""
    first = date.fromisoformat(f"{month}-01")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    igns, days = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id)
    boss_total, counts = db.attend_counts(conn, guild_id, first.isoformat(), last.isoformat(),
                                          now.isoformat(timespec="seconds"))
    bosses = db.started_bosses(conn, guild_id, now.isoformat(timespec="seconds"))
    attended = db.attended_bosses(conn, guild_id)
    linked = sorted(((i, name, igns[i]) for i, name in tracked if i in igns), key=lambda r: r[2].lower())
    return {
        "rows": [(ign, name, sum(d.startswith(month) for d in days.get(i, set())), counts.get(i, 0))
                 for i, name, ign in linked],
        "boss_total": boss_total,
        "streaks": sorted(((ign, boss_streak(bosses, attended.get(i, set()), now)) for i, _, ign in linked),
                          key=lambda r: (-r[1], r[0].lower())),
        "days_in_month": last.day,
    }


def monthly_csv(report):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("in_game_name", "discord_name", "donation_days", "bosses_attended", "bosses_total"))
    for ign, name, donated, attended in report["rows"]:
        writer.writerow([db.safe_cell(ign), db.safe_cell(name), donated, attended, report["boss_total"]])
    return out.getvalue()


def dashboard_stats(conn, guild_id, tracked, now):
    """Extra numbers for /dashboard. tracked = [(discord_id, display_name)]; rates count linked members.
    week: [(day, donated)] for the last 7 UTC days, oldest first.
    week_rate / month_rate: (member-days donated, member-days possible) since Monday / the 1st, today included.
    at_risk: [(ign, streak)] streaks of 3+ days not logged today, longest first.
    bosses_today: [(unix time, attended)] for bosses started today.
    not_linked: role members with no link. stale_links: igns linked to someone without the role."""
    today = now.date()
    igns, days = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id)
    linked = [i for i, _ in tracked if i in igns]
    tracked_ids = {i for i, _ in tracked}

    def rate(first):
        span = {(first + timedelta(days=k)).isoformat() for k in range((today - first).days + 1)}
        return sum(len(days.get(i, set()) & span) for i in linked), len(linked) * len(span)

    now_text = now.isoformat(timespec="seconds")
    return {
        "week": [(today - timedelta(days=k), sum((today - timedelta(days=k)).isoformat() in days.get(i, set())
                                                  for i in linked)) for k in range(6, -1, -1)],
        "week_rate": rate(today - timedelta(days=today.weekday())),
        "month_rate": rate(today.replace(day=1)),
        "at_risk": sorted(((igns[i], streak(days.get(i, set()), today.isoformat())) for i in linked
                           if today.isoformat() not in days.get(i, set())
                           and streak(days.get(i, set()), today.isoformat()) >= 3),
                          key=lambda r: (-r[1], r[0].lower())),
        "bosses_today": [(int(datetime.fromisoformat(b["boss_time"]).timestamp()), len(db.attendance_for_boss(conn, b["id"])))
                         for b in db.bosses_for_day(conn, guild_id, today.isoformat()) if b["boss_time"] <= now_text],
        "not_linked": sorted((name for i, name in tracked if i not in igns), key=str.lower),
        "stale_links": sorted((ign for i, ign in igns.items() if i not in tracked_ids), key=str.lower),
    }


def not_donated(conn, guild_id, tracked, day):
    """tracked = [(discord_id, display_name)]. Returns [(discord_id, board_name)] who have no
    approved donation for `day`. Board name is the in-game name, or 'Name (not linked)'."""
    donated = {r["discord_id"] for r in db.approved_for_day(conn, guild_id, day)}
    igns = db.get_igns(conn, guild_id)
    return [(i, igns.get(i) or f"{name} (not linked)") for i, name in tracked if i not in donated]


def member_ids(text):
    """Discord ids from @mentions or pasted ids, in order, no repeats."""
    ids = []
    for mention, raw in re.findall(r"<@!?(\d{15,20})>|\b(\d{15,20})\b", text):
        i = int(mention or raw)
        if i not in ids:
            ids.append(i)
    return ids


def log_all_plan(conn, guild_id, tracked, day, skip_id):
    """Who /log_all would log: tracked members without a donation on `day`, except skip_id.
    Returns ([(discord_id, display name, ign)], names left out for having no link)."""
    igns, names = db.get_igns(conn, guild_id), dict(tracked)
    to_log, unlinked = [], []
    for i, _ in not_donated(conn, guild_id, tracked, day):
        if i == skip_id:
            continue
        if i not in igns:
            unlinked.append(names[i])
            continue
        to_log.append((i, names[i], igns[i]))
    return to_log, unlinked


def log_all(conn, guild_id, tracked, day, skip_id, staff_id):
    """Run log_all_plan. Returns ([(submission id, ign)] logged, names left out for having no link)."""
    to_log, unlinked = log_all_plan(conn, guild_id, tracked, day, skip_id)
    logged = []
    for i, name, ign in to_log:
        try:
            logged.append((db.record(conn, guild_id, i, name, ign, day, logged_by=staff_id), ign))
        except db.AlreadyDonated:  # donated between the preview and the confirm
            pass
    return logged, unlinked


def data_sections(conn, guild_id, tracked, today):
    """Lists for the data channel. tracked = [(discord_id, display_name)].
    donated: (discord name, in-game name, unix time), newest first.
    not_donated: linked members with no donation today.
    not_linked: roster names nobody is linked to yet (in-game name only)."""
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
    y_count = db.day_totals(conn, guild_id, yesterday)[0]
    return {
        "today_count": len(today_rows),
        "reset_ts": next_reset_ts(now),
        "recent": [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
                   for r in today_rows[:RECENT_DONORS]],
        "waiting": [name for _, name in not_donated(conn, guild_id, tracked, today)],
        "yesterday_count": y_count,
        "streaks": top_streaks(conn, guild_id, tracked, today),
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


def boss_lists(conn, guild_id, tracked, boss_id):
    """day_lists for one boss: (attended as (discord name, in-game name, unix time), not attended as board names)."""
    attended = [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
                for r in db.attendance_for_boss(conn, boss_id)]
    return attended, [name for _, name in not_attended(conn, guild_id, tracked, boss_id)]


def member_day(conn, guild_id, discord_id, day, now):
    """Bosses started on `day` by `now`, as [(unix time, attended)]."""
    now_text = now.isoformat(timespec="seconds")
    return [(int(datetime.fromisoformat(b["boss_time"]).timestamp()),
             any(r["discord_id"] == discord_id for r in db.attendance_for_boss(conn, b["id"])))
            for b in db.bosses_for_day(conn, guild_id, day) if b["boss_time"] <= now_text]


def attend_report(conn, guild_id, tracked, first_day, last_day, now):
    """Linked members' attendance from first_day to last_day. Returns (boss count,
    [(in-game name, bosses attended)]), fewest first, then by name."""
    igns = db.get_igns(conn, guild_id)
    total, counts = db.attend_counts(conn, guild_id, first_day, last_day, now.isoformat(timespec="seconds"))
    rows = [(igns[i], counts.get(i, 0)) for i, _ in tracked if i in igns]
    return total, sorted(rows, key=lambda r: (r[1], r[0].lower()))


def missed_in_row(conn, guild_id, tracked, now, count):
    """Linked members who attended none of the newest `count` started bosses.
    Returns (bosses checked, in-game names sorted). Fewer bosses than `count` may exist."""
    ids = db.recent_bosses(conn, guild_id, now.isoformat(timespec="seconds"), count)
    seen = {r["discord_id"] for b in ids for r in db.attendance_for_boss(conn, b)}
    igns = db.get_igns(conn, guild_id)
    names = [igns[i] for i, _ in tracked if i in igns and i not in seen] if ids else []
    return len(ids), sorted(names, key=str.lower)


def xp_day(conn, guild_id, tracked, day):
    """Linked tracked members' XP on `day`: [(discord_id, discord name, ign, gain or None)], most XP first,
    then the members with no log."""
    igns, gains = db.get_igns(conn, guild_id), db.xp_gains(conn, guild_id, day)
    rows = [(i, name, igns[i], gains.get(i)) for i, name in tracked if i in igns]
    return sorted(rows, key=lambda r: (r[3] is None, -(r[3] or 0), r[2].lower()))
