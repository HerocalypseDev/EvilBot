from datetime import timedelta

WINDOW = timedelta(minutes=5)


def reminder_action(now, minutes_before):
    """'wait' before the reminder time, 'send' in the 5 minutes after it, 'late' after that.
    Reminder time = next 00:00 UTC minus minutes_before. Needs minutes_before >= 5."""
    reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    due = reset - timedelta(minutes=minutes_before)
    if now < due:
        return "wait"
    return "send" if now < due + WINDOW else "late"


def chunks(items, size=50):
    return [items[i:i + size] for i in range(0, len(items), size)]
