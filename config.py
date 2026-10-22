import os

DONATION_AMOUNT = 50_000
RECENT_DONORS = 10
MEMBER_ROLE_NAME = "ingame guild member"
STAFF_ROLE_NAME = "Officer"
# Minutes before 00:00 UTC. Env override lets you test a reminder at any time of day, e.g. REMINDER_MINUTES=870
REMINDER_MINUTES = tuple(int(m) for m in os.getenv("REMINDER_MINUTES", "180,120,60").split(","))
BACKUP_HOUR_UTC = 4
IGN_MIN, IGN_MAX = 2, 32
