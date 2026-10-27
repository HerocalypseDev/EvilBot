import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

from discord.utils import escape_markdown

import boss
import db
from config import RECENT_DONORS, XP_CAP


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


def without_loa(conn, guild_id, tracked, day):
    """tracked minus the members on LOA on `day`, and {discord_id: last LOA day} of everyone on LOA that day."""
    loa = db.loa_active_on(conn, guild_id, day)
    return [m for m in tracked if m[0] not in loa], loa


def loa_rows(conn, guild_id, loa):
    """loa = {discord_id: last LOA day}. [(in-game name, last LOA day)] of the linked ones, by name."""
    igns = db.get_igns(conn, guild_id)
    return sorted(((igns[i], last) for i, last in loa.items() if i in igns), key=lambda r: r[0].lower())


def loa_on(conn, guild_id, day):
    return loa_rows(conn, guild_id, db.loa_active_on(conn, guild_id, day))


def streak(days, today, loa=frozenset()):
    """Days in a row with a donation, up to today. A streak still counts while today is not logged yet.
    LOA days (a set of ISO days) neither count nor break it."""
    d = date.fromisoformat(today)
    if today not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days or d.isoformat() in loa:
        n += d.isoformat() in days
        d -= timedelta(days=1)
    return n


def best_streak(days, loa=frozenset()):
    best = run = 0
    prev = None
    for d in sorted(date.fromisoformat(x) for x in days):
        bridged = prev and all((prev + timedelta(days=k)).isoformat() in loa for k in range(1, (d - prev).days))
        run = run + 1 if bridged else 1
        best, prev = max(best, run), d
    return best


def badge(n):
    """Badge for a streak of n days, or None."""
    return "30-day streak" if n >= 30 else "7-day streak" if n >= 7 else None


def top_streaks(conn, guild_id, tracked, today, count=5):
    """Linked members with the longest running streaks (2 days or more): [(in-game name, days)]."""
    igns, days, loa = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id), db.loa_days(conn, guild_id)
    rows = [(igns[i], streak(days.get(i, set()), today, loa.get(i, set()))) for i, _ in tracked if i in igns]
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
    """Numbers for /me. Ranks are among tracked members (plus the member, if they lost the role).
    LOA days are left out of the month's days and bosses, and do not break streaks. loa = last LOA day if the
    member is on LOA today, else None. last_14_loa marks the LOA days in last_14."""
    today = now.date().isoformat()
    days = db.donation_days(conn, guild_id)
    mine = days.get(discord_id, set())
    loa = db.loa_days(conn, guild_id).get(discord_id, set())
    month = today[:7]
    now_text = now.isoformat(timespec="seconds")
    started = db.started_bosses(conn, guild_id, now_text)
    month_loa = sum(x.startswith(month) and x <= today for x in loa)
    ids = {i for i, _ in tracked} | {discord_id}
    month_counts = {i: sum(d.startswith(month) for d in days.get(i, set())) for i in ids}
    all_counts = {i: len(days.get(i, set())) for i in ids}
    last = next((r for r in db.history(conn, guild_id, discord_id) if r["status"] == "approved"), None)
    boss_total, boss_counts = db.attend_counts(conn, guild_id, f"{month}-01", today, now_text)
    d = date.fromisoformat(today)
    return {
        "loa": db.loa_active_on(conn, guild_id, today).get(discord_id),
        "today": today in mine,
        "streak": streak(mine, today, loa),
        "best": best_streak(mine, loa),
        "next_badge": next_badge(streak(mine, today, loa)),
        "month_days": month_counts[discord_id],
        "month_elapsed": d.day - month_loa,
        "month_loa": month_loa,
        "month_rank": 1 + sum(c > month_counts[discord_id] for c in month_counts.values()),
        "all_days": all_counts[discord_id],
        "all_rank": 1 + sum(c > all_counts[discord_id] for c in all_counts.values()),
        "members": len(ids),
        "last_ts": int(datetime.fromisoformat(last["created_at"]).timestamp()) if last else None,
        "last_14": [(d - timedelta(days=k)).isoformat() in mine for k in range(13, -1, -1)],  # oldest first
        "last_14_loa": [(d - timedelta(days=k)).isoformat() in loa for k in range(13, -1, -1)],
        "boss_attended": boss_counts.get(discord_id, 0),
        "boss_total": boss_total - sum(b["day_utc"].startswith(month) and b["day_utc"] in loa for b in started),
        "boss_streak": boss_streak(started, db.attended_bosses(conn, guild_id).get(discord_id, set()), now, loa),
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


def boss_streak(bosses, attended, now, loa=frozenset()):
    """Bosses in a row attended, newest first. bosses = started boss rows, newest first. A boss whose
    logging is still open does not break the streak yet. Bosses on LOA days (a set of ISO days) are skipped."""
    n = 0
    for k, b in enumerate(bosses):
        if b["id"] in attended:
            n += 1
        elif b["day_utc"] in loa:
            continue
        elif not (k == 0 and boss.attend_state(datetime.fromisoformat(b["boss_time"]), now) == "open"):
            break
    return n


def monthly_report(conn, guild_id, tracked, month, now):
    """month = 'YYYY-MM'. Linked members' month: [(ign, discord name, donation days, bosses attended)] by name,
    plus the boss count, the current boss streaks, the month's day count and loa_days: {ign: days on LOA in the month}."""
    first = date.fromisoformat(f"{month}-01")
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    igns, days, loa = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id), db.loa_days(conn, guild_id)
    boss_total, counts = db.attend_counts(conn, guild_id, first.isoformat(), last.isoformat(),
                                          now.isoformat(timespec="seconds"))
    bosses = db.started_bosses(conn, guild_id, now.isoformat(timespec="seconds"))
    attended = db.attended_bosses(conn, guild_id)
    linked = sorted(((i, name, igns[i]) for i, name in tracked if i in igns), key=lambda r: r[2].lower())
    return {
        "rows": [(ign, name, sum(d.startswith(month) for d in days.get(i, set())), counts.get(i, 0))
                 for i, name, ign in linked],
        "boss_total": boss_total,
        "streaks": sorted(((ign, boss_streak(bosses, attended.get(i, set()), now, loa.get(i, set())))
                           for i, _, ign in linked), key=lambda r: (-r[1], r[0].lower())),
        "days_in_month": last.day,
        "loa_days": {ign: sum(d.startswith(month) for d in loa.get(i, set())) for i, _, ign in linked},
    }


def monthly_csv(report):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(("in_game_name", "discord_name", "donation_days", "bosses_attended", "bosses_total", "loa_days"))
    for ign, name, donated, attended in report["rows"]:
        writer.writerow([db.safe_cell(ign), db.safe_cell(name), donated, attended, report["boss_total"],
                         report["loa_days"].get(ign, 0)])
    return out.getvalue()


def dashboard_stats(conn, guild_id, tracked, now):
    """Extra numbers for /dashboard. tracked = [(discord_id, display_name)]; rates count linked members.
    week: [(day, donated)] for the last 7 UTC days, oldest first.
    week_rate / month_rate: (member-days donated, member-days possible) since Monday / the 1st, today included.
    at_risk: [(ign, streak)] streaks of 3+ days not logged today, longest first.
    bosses_today: [(unix time, attended)] for bosses started today.
    not_linked: role members with no link. stale_links: igns linked to someone without the role.
    on_loa: [(ign, last LOA day)] linked members on LOA today. LOA days are left out of the member-days possible."""
    today = now.date()
    igns, days, loa = db.get_igns(conn, guild_id), db.donation_days(conn, guild_id), db.loa_days(conn, guild_id)
    loa_now = db.loa_active_on(conn, guild_id, today.isoformat())
    linked = [i for i, _ in tracked if i in igns and i not in loa_now]
    tracked_ids = {i for i, _ in tracked}

    def rate(first):
        span = {(first + timedelta(days=k)).isoformat() for k in range((today - first).days + 1)}
        return (sum(len(days.get(i, set()) & span) for i in linked),
                sum(len(span - loa.get(i, set())) for i in linked))

    now_text = now.isoformat(timespec="seconds")
    return {
        "week": [(today - timedelta(days=k), sum((today - timedelta(days=k)).isoformat() in days.get(i, set())
                                                  for i in linked)) for k in range(6, -1, -1)],
        "week_rate": rate(today - timedelta(days=today.weekday())),
        "month_rate": rate(today.replace(day=1)),
        "at_risk": sorted(((igns[i], streak(days.get(i, set()), today.isoformat(), loa.get(i, set()))) for i in linked
                           if today.isoformat() not in days.get(i, set())
                           and streak(days.get(i, set()), today.isoformat(), loa.get(i, set())) >= 3),
                          key=lambda r: (-r[1], r[0].lower())),
        "bosses_today": [(int(datetime.fromisoformat(b["boss_time"]).timestamp()), len(db.attendance_for_boss(conn, b["id"])))
                         for b in db.bosses_for_day(conn, guild_id, today.isoformat()) if b["boss_time"] <= now_text],
        "not_linked": sorted((name for i, name in tracked if i not in igns), key=str.lower),
        "stale_links": sorted((ign for i, ign in igns.items() if i not in tracked_ids and i not in loa_now),
                              key=str.lower),
        "on_loa": loa_rows(conn, guild_id, loa_now),
    }


def not_donated(conn, guild_id, tracked, day):
    """tracked = [(discord_id, display_name)]. Returns [(discord_id, board_name)] who have no
    approved donation for `day`, leaving out members on LOA that day. Board name is the in-game name, or
    'Name (not linked)'."""
    tracked = without_loa(conn, guild_id, tracked, day)[0]
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
    on_loa: (in-game name, last LOA day) of linked members on LOA today.
    not_linked: roster names nobody is linked to yet (in-game name only)."""
    tracked, loa = without_loa(conn, guild_id, tracked, today)
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
        "on_loa": loa_rows(conn, guild_id, loa),
        "not_linked": [(ign,) for ign in db.roster_names(conn, guild_id, only_free=True)],
    }


def day_lists(conn, guild_id, tracked, day):
    """For /history. Returns (donated as (discord name, in-game name, unix time), not donated as board names,
    on LOA as (in-game name, last LOA day))."""
    donated = [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
               for r in db.approved_for_day(conn, guild_id, day)]
    return donated, [name for _, name in not_donated(conn, guild_id, tracked, day)], loa_on(conn, guild_id, day)


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
    tracked = without_loa(conn, guild_id, tracked, db.boss_day(conn, boss_id))[0]
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
    not_attended: linked members who did not log that boss, by in-game name. Members on LOA that day are left out.
    attend_on_loa: (in-game name, last LOA day) of linked members on LOA that day.
    today_totals: (in-game name, 'attended/bosses') for linked members, bosses started today."""
    now_text = now.isoformat(timespec="seconds")
    row = db.started_boss(conn, guild_id, now_text)
    igns = db.get_igns(conn, guild_id)
    tracked, loa = without_loa(conn, guild_id, tracked, row["day_utc"] if row else now.date().isoformat())
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
        "attend_on_loa": loa_rows(conn, guild_id, loa),
        "today_totals": [(ign, f"{counts.get(i, 0)}/{total}") for i, _, ign in linked],
    }


def boss_lists(conn, guild_id, tracked, boss_id):
    """day_lists for one boss: (attended as (discord name, in-game name, unix time), not attended as board names,
    on LOA as (in-game name, last LOA day))."""
    attended = [(r["discord_name"], r["ign"], int(datetime.fromisoformat(r["created_at"]).timestamp()))
                for r in db.attendance_for_boss(conn, boss_id)]
    return (attended, [name for _, name in not_attended(conn, guild_id, tracked, boss_id)],
            loa_on(conn, guild_id, db.boss_day(conn, boss_id)))


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
    """Linked members who attended none of the newest `count` started bosses. Bosses on a member's LOA days do
    not count against them. Returns (bosses checked, in-game names sorted). Fewer bosses than `count` may exist."""
    bosses = db.started_bosses(conn, guild_id, now.isoformat(timespec="seconds"))[:count]
    attended, loa, igns = db.attended_bosses(conn, guild_id), db.loa_days(conn, guild_id), db.get_igns(conn, guild_id)

    def missed(i):
        due = {b["id"] for b in bosses if b["day_utc"] not in loa.get(i, set())}
        return bool(due) and not attended.get(i, set()) & due

    return len(bosses), sorted((igns[i] for i, _ in tracked if i in igns and missed(i)), key=str.lower)


def xp_day(conn, guild_id, tracked, day):
    """Linked tracked members' XP on `day`: [(discord_id, discord name, ign, gain or None)], most XP first,
    then the members with no log. Members on LOA that day are left out."""
    tracked = without_loa(conn, guild_id, tracked, day)[0]
    igns, gains = db.get_igns(conn, guild_id), db.xp_gains(conn, guild_id, day)
    rows = [(i, name, igns[i], gains.get(i)) for i, name in tracked if i in igns]
    return sorted(rows, key=lambda r: (r[3] is None, -(r[3] or 0), r[2].lower()))


def xp_week_totals(conn, guild_id, tracked, day):
    """{discord_id: weekly total so far} for linked tracked members, in the week holding `day`. Members on LOA
    that day are left out."""
    tracked = without_loa(conn, guild_id, tracked, day)[0]
    igns = db.get_igns(conn, guild_id)
    totals = {}
    for i, _ in tracked:
        if i in igns:
            logged = [d["total"] for d in db.xp_week(conn, guild_id, i, day) if d["total"] is not None]
            totals[i] = logged[-1] if logged else 0
    return totals


def xp_cap_counts(conn, guild_id, tracked, today):
    """[(date, members at the cap)] from Monday to today."""
    start = date.fromisoformat(db.week_start(today))
    days = [start + timedelta(days=k) for k in range((date.fromisoformat(today) - start).days + 1)]
    return [(d, sum(g is not None and g >= XP_CAP for *_, g in xp_day(conn, guild_id, tracked, d.isoformat())))
            for d in days]
