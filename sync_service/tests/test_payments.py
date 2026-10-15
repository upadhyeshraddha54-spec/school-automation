"""Payments are learned from Excel exactly once; safer imports."""
import os
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from conftest import edit_cell
from schoolops_sync.api import create_app
from schoolops_sync.config import load_config
from schoolops_sync.db import connect
from schoolops_sync.imports import _cleanup_stale
from schoolops_sync.reader import read_workbook
from schoolops_sync.review import approve
from schoolops_sync.sync import sync_file, sync_folder

STD1 = "English - Std 1.xlsx"


def stu(conn, sr="1", sheet="1 Peacock"):
    return conn.execute("""SELECT s.id, f.total_fee, f.discount, f.paid, f.balance FROM students s
                           JOIN current_fees f ON f.student_id=s.id WHERE s.source_sheet=%s AND s.sr_no=%s""",
                        (sheet, sr)).fetchone()


def pays(conn):
    return conn.execute("SELECT * FROM payments ORDER BY id").fetchall()


def due_sr(conn, sheet="1 Peacock"):
    return conn.execute("""SELECT s.sr_no FROM students s JOIN current_fees f ON f.student_id=s.id
                           WHERE s.source_sheet=%s AND f.balance > 1000 AND f.paid > 0 ORDER BY s.id LIMIT 1""",
                        (sheet,)).fetchone()["sr_no"]


def test_paid_increase_is_one_payment_even_after_resyncs(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    sr = due_sr(conn)
    s = stu(conn, sr)
    row = 2 + int(sr)
    edit_cell(folder / STD1, "1 Peacock", row, "Paid Amt", float(s["paid"] + 1000))
    edit_cell(folder / STD1, "1 Peacock", row, "Balance", float(s["balance"] - 1000))
    for _ in range(3):
        sync_folder(conn, cfg, dry_run=False, force=True)
    p = pays(conn)
    assert len(p) == 1 and p[0]["amount"] == 1000 and p[0]["basis"] == "paid_increased"
    assert conn.execute("SELECT count(*) AS n FROM fee_snapshots WHERE student_id=%s", (s["id"],)).fetchone()["n"] == 2


def test_balance_only_and_discount_changes(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    sr = due_sr(conn, "1 Swan")
    s = stu(conn, sr, "1 Swan")
    edit_cell(folder / STD1, "1 Swan", 2 + int(sr), "Balance", float(s["balance"] - 500))
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert [(p["amount"], p["basis"]) for p in pays(conn)] == [(500, "balance_decreased")]
    # a bigger discount lowers the balance but is NOT a payment
    edit_cell(folder / STD1, "1 Swan", 2 + int(sr), "Disc. Amt", float(s["discount"] + 300))
    edit_cell(folder / STD1, "1 Swan", 2 + int(sr), "Balance", float(s["balance"] - 800))
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert len(pays(conn)) == 1


def test_simultaneous_syncs_never_double_count(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    sr = due_sr(conn)
    s = stu(conn, sr)
    edit_cell(folder / STD1, "1 Peacock", 2 + int(sr), "Paid Amt", float(s["paid"] + 700))
    edit_cell(folder / STD1, "1 Peacock", 2 + int(sr), "Balance", float(s["balance"] - 700))
    results, errors = [], []

    def worker():
        try:
            with connect(cfg.database_url) as c:
                results.append(sync_file(c, cfg, folder / STD1, dry_run=False, force=True, trigger="watcher"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and all(r["status"] == "applied" for r in results)
    assert sum(r["fee_updates"] for r in results) == 1
    assert len(pays(conn)) == 1
    assert conn.execute("SELECT count(*) AS n FROM students").fetchone()["n"] == 84


def test_approving_a_balance_increase_is_not_a_payment(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    s = stu(conn, "2")
    edit_cell(folder / STD1, "1 Peacock", 4, "Balance", float(s["balance"] + 999))
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    item = conn.execute("SELECT id FROM review_items WHERE kind='fee_change'").fetchone()
    approve(conn, item["id"])
    assert pays(conn) == []


def test_headings_moved_down_are_still_found(cfg, folder):
    p = folder / STD1
    wb = load_workbook(p)
    wb["1 Peacock"].insert_rows(1)  # someone added a row above the headings
    wb.save(p)
    pf = read_workbook(p, cfg)
    assert {r.sheet for r in pf.rows} == {"1 Peacock", "1 Swan"} and len(pf.rows) == 24
    assert any(i.code == "headings_moved" and i.sheet == "1 Peacock" for i in pf.issues)


def test_uploads_do_not_touch_folder_tracking_and_stale_uploads_are_cleaned(conn, cfg, folder, tmp_path):
    cfg.upload_folder = str(tmp_path / "uploads")
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        r = c.post("/imports/preview", files=[("files", (STD1, (folder / STD1).read_bytes(), "x"))]).json()
        c.post("/imports/confirm", json={"token": r["token"], "files": [STD1]})
        r2 = c.post("/imports/preview", files=[("files", (STD1, (folder / STD1).read_bytes(), "x"))]).json()
        assert r2["files"][0]["plan"]["new_students"] == 0  # same file again: nothing new
        c.post("/imports/confirm", json={"token": r2["token"], "files": [STD1]})
    assert conn.execute("SELECT count(*) AS n FROM file_state").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM students").fetchone()["n"] == 24
    old = Path(cfg.upload_folder) / ("a" * 32)
    old.mkdir(parents=True)
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    _cleanup_stale(cfg)
    assert not old.exists()


def test_follow_up_list_csv(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        r = c.get("/queue.csv", params={"std": "1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("#,Why,Parent phone,Student") and len(lines) > 5
    assert "attachment" in r.headers["content-disposition"]
