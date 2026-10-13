import time

import pytest

from conftest import edit_cell
from schoolops_sync.sync import sync_folder
from schoolops_sync.watcher import FolderWatcher


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.3)
    return False


@pytest.mark.parametrize("polling", [True, False], ids=["polling", "native-events"])
def test_saved_change_is_synced_automatically(conn, cfg, folder, polling):
    sync_folder(conn, cfg, dry_run=False)
    before = conn.execute("SELECT count(*) AS n FROM fee_snapshots").fetchone()["n"]
    w = FolderWatcher(cfg, polling=polling)
    w.start()
    try:
        time.sleep(1.5)
        edit_cell(folder / "English - Std 1.xlsx", "1 Peacock", 3, "Contact", "9000088888")
        edit_cell(folder / "English - Std 1.xlsx", "1 Peacock", 4, "Balance", 0)  # payment recorded
        assert wait_for(lambda: conn.execute(
            "SELECT count(*) AS n FROM sync_runs WHERE trigger='watcher' AND status='applied'").fetchone()["n"] >= 1)
        assert conn.execute("SELECT count(*) AS n FROM students WHERE contact='9000088888'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM fee_snapshots").fetchone()["n"] >= before
    finally:
        w.stop()


def test_reconcile_catches_missed_changes(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    w = FolderWatcher(cfg)  # not started: simulate an event that was missed
    edit_cell(folder / "English - Std 10.xlsx", "10 Peacock", 3, "Contact", "9000077777")
    res = w.reconcile()
    assert any(f["status"] == "applied" for f in res["files"])
    assert conn.execute("SELECT count(*) AS n FROM students WHERE contact='9000077777'").fetchone()["n"] == 1


def test_watcher_ignores_temp_files(conn, cfg, folder):
    w = FolderWatcher(cfg, polling=True)
    w.start()
    try:
        (folder / "~$English - Std 1.xlsx").write_bytes(b"lock")
        time.sleep(2.5)
        assert conn.execute("SELECT count(*) AS n FROM sync_runs").fetchone()["n"] == 0
    finally:
        w.stop()
