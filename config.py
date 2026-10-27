import os

DONATION_AMOUNT = 50_000
RECENT_DONORS = 10
DATA_PAGE = 15  # rows per page in the data channel
MEMBER_ROLE_NAME = "ingame guild member"
# Minutes before 00:00 UTC. Env override lets you test a reminder at any time of day, e.g. REMINDER_MINUTES=870
try:
    REMINDER_MINUTES = tuple(int(m) for m in os.getenv("REMINDER_MINUTES", "60").split(","))
except ValueError:
    raise SystemExit("REMINDER_MINUTES must be whole numbers separated by commas, e.g. 60 or 120,60.")
# under 5 the send window crosses midnight and is cut short; over 1435 it never opens
if not all(5 <= m <= 1435 for m in REMINDER_MINUTES):
    raise SystemExit("REMINDER_MINUTES values must be from 5 to 1435.")
BACKUP_HOUR_UTC = 4
IGN_MIN, IGN_MAX = 2, 32
XP_CAP = 1500  # most XP a member can gain in one UTC day
LOA_MAX_DAYS = 14  # longest leave a member can ask for
LOA_COOLDOWN_DAYS = 7  # wait after a leave ends before the next one
LOA_MAX_AHEAD_DAYS = 7  # furthest start date a member can pick
VERSION = "2.3.3"  # bump on each upload, so /help and the startup log show which code is live
