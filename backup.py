import re
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

_NAME = re.compile(r"donations-(\d{4}-\d{2}-\d{2})\.db")


def run_backup(conn, day, local_dir, extra_dir=None, keep_days=30):
    """Write the backup for `day` once, copy it to extra_dir (OneDrive) if given, drop old ones.
    Returns True if anything was written."""
    name = f"donations-{day}.db"
    local = Path(local_dir)
    local.mkdir(parents=True, exist_ok=True)
    target = local / name
    wrote = False
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        dest = sqlite3.connect(tmp)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        tmp.replace(target)
        wrote = True
    if extra_dir:
        extra = Path(extra_dir)
        extra.mkdir(parents=True, exist_ok=True)
        if not (extra / name).exists():
            shutil.copy2(target, extra / name)
            wrote = True
    for folder in filter(None, (local, extra_dir)):
        prune(folder, day, keep_days)
    return wrote


def prune(folder, today, keep_days=30):
    """Delete donations-YYYY-MM-DD.db files older than keep_days. Other files are never touched."""
    cutoff = date.fromisoformat(today) - timedelta(days=keep_days)
    for path in Path(folder).iterdir():
        m = _NAME.fullmatch(path.name)
        if m and date.fromisoformat(m.group(1)) < cutoff:
            path.unlink()
