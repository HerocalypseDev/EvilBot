import sqlite3

import db
from backup import other_db_files, prune, run_backup


def make_db(tmp_path):
    conn = db.connect(tmp_path / "live.db")
    db.record(conn, 1, 100, "name", "Ign", "2026-10-22")
    return conn


def test_backup_copy_has_the_rows_and_runs_once(tmp_path):
    conn = make_db(tmp_path)
    assert run_backup(conn, "2026-10-22", tmp_path / "local", tmp_path / "cloud") is True
    for folder in ("local", "cloud"):
        copy = sqlite3.connect(tmp_path / folder / "donations-2026-10-22.db")
        assert copy.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
        copy.close()
    assert run_backup(conn, "2026-10-22", tmp_path / "local", tmp_path / "cloud") is False


def test_backup_fills_missing_cloud_copy(tmp_path):
    conn = make_db(tmp_path)
    run_backup(conn, "2026-10-22", tmp_path / "local")
    assert run_backup(conn, "2026-10-22", tmp_path / "local", tmp_path / "cloud") is True
    assert (tmp_path / "cloud" / "donations-2026-10-22.db").exists()


def test_prune_only_removes_old_backup_files(tmp_path):
    for name in ("donations-2026-09-01.db", "donations-2026-10-10.db", "notes.txt", "donations-old.db"):
        (tmp_path / name).write_text("x")
    prune(tmp_path, "2026-10-22", keep_days=30)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "donations-2026-10-10.db", "donations-old.db", "notes.txt"]


def test_other_db_files(tmp_path):
    for name in ("donations.db", "donations (1).db", "notes.txt"):
        (tmp_path / name).write_text("")
    assert other_db_files(tmp_path / "donations.db") == ["donations (1).db"]
