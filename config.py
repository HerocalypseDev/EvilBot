import re
from decimal import Decimal

MAX_AMOUNT = 50_000
DONATION_AMOUNT = MAX_AMOUNT
RECENT_DONORS = 8
MEMBER_ROLE_NAME = "ingame guild member"
STAFF_ROLE_NAME = "Officer"
REMINDER_MINUTES = (180, 120, 60)
IGN_MIN, IGN_MAX = 2, 32

_AMOUNT = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)|(\d+(?:\.\d+)?)k", re.IGNORECASE)


def parse_amount(text):
    """'50000', '50,000', '50k', '1.5k' -> int in 1..MAX_AMOUNT. Raises ValueError."""
    m = _AMOUNT.fullmatch((text or "").strip())
    if not m:
        raise ValueError("Enter a number like 50000, 50,000 or 50k.")
    if m.group(1):
        value = Decimal(m.group(1).replace(",", ""))
    else:
        value = Decimal(m.group(2)) * 1000
    if value != value.to_integral_value() or not 1 <= value <= MAX_AMOUNT:
        raise ValueError(f"Amount must be a whole number from 1 to {MAX_AMOUNT:,}.")
    return int(value)
