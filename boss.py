from datetime import timedelta

CYCLE = timedelta(hours=6)      # a guild boss spawns at 00:00, 06:00, 12:00, 18:00 UTC
SLOTS = 6                       # options per poll: spawn hour to spawn hour + 5
LEAD = timedelta(minutes=10)    # ping this long before the winning time
NEXT_POLL_DELAY = timedelta(minutes=5)
ATTEND_WINDOW = timedelta(hours=1)  # members log attendance from the boss time until this long after
EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]


def slot_times(cycle_start):
    return [cycle_start + timedelta(hours=k) for k in range(SLOTS)]


def next_cycle_start(now):
    """First spawn time after `now`."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + CYCLE * (now.hour // 6 + 1)


def upcoming_cycle(after, now):
    """First cycle start >= `after` that still has a time ahead of `now` (skips cycles missed while off)."""
    start = after
    while start + timedelta(hours=SLOTS - 1) <= now:
        start += CYCLE
    return start


def next_action(latest, now):
    """What the boss loop does now. latest = (cycle_start, resolved_time or None) of the newest poll,
    or None if there is none. Returns one of:
    ("post", cycle_start)  post a new poll for that cycle
    ("end", last_time)     every time passed with no ping: close the poll
    ("count", None)        a ping may be due: count the votes and call pick_ping
    ("wait", None)         nothing to do yet"""
    if latest is None:
        return "post", next_cycle_start(now)
    cycle, resolved = latest
    if resolved:
        if now >= resolved + NEXT_POLL_DELAY:
            return "post", upcoming_cycle(cycle + CYCLE, now)
        return "wait", None
    ahead = [t for t in slot_times(cycle) if t > now]
    if not ahead:
        return "end", slot_times(cycle)[-1]
    if now < ahead[0] - LEAD:  # no ping can be due yet, so skip counting votes
        return "wait", None
    return "count", None


def pick_ping(now, cycle_start, votes):
    """Index of the time to ping about right now, or None. Only times still ahead can win. The most
    votes wins, the earliest on a tie (so with no votes the first time ahead). The ping is due
    LEAD before it; a late ping is still sent while that time has not started."""
    ahead = [k for k, t in enumerate(slot_times(cycle_start)) if t > now]
    if not ahead:
        return None
    top = max(votes[k] for k in ahead)
    winner = next(k for k in ahead if votes[k] == top)
    return winner if now >= slot_times(cycle_start)[winner] - LEAD else None


def poll_text(role_mention, cycle_start):
    lines = [f"{role_mention} **Guild boss time vote.** React with every time that works for you. "
             "Times below are shown in your own time zone, with a countdown."]
    for emoji, t in zip(EMOJIS, slot_times(cycle_start)):
        ts = int(t.timestamp())
        lines.append(f"{emoji} <t:{ts}:t> · <t:{ts}:R>")
    return "\n".join(lines)


def result_text(t, votes):
    """Line added to the poll when it closes. t = winning time, or None when no time was picked."""
    if t is None:
        return "**Result:** no time was picked. The next poll follows soon."
    return f"**Result:** <t:{int(t.timestamp())}:t> won with {votes} vote(s)."


def ping_text(role_mention, t, votes):
    ts = int(t.timestamp())
    return (f"{role_mention} Guild boss at <t:{ts}:t> (<t:{ts}:R>), picked with {votes} vote(s). "
            "Go to the guild island now.")


def attend_state(boss_time, now):
    """'soon' before the boss time, 'open' for ATTEND_WINDOW after it, then 'closed'."""
    if now < boss_time:
        return "soon"
    return "open" if now < boss_time + ATTEND_WINDOW else "closed"


def attend_open_text(role_mention, boss_time):
    close = int((boss_time + ATTEND_WINDOW).timestamp())
    return (f"{role_mention} Guild boss started. Press **I attended** below if you joined. "
            f"Logging closes <t:{close}:t> (<t:{close}:R>).")
