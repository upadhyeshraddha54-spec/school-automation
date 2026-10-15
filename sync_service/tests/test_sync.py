from decimal import Decimal

import pytest
from openpyxl import load_workbook

from conftest import cell, delete_row, edit_cell
from schoolops_sync.config import NotAllowed, SYNTHETIC_MARKER
from schoolops_sync.reader import sha256_file
from schoolops_sync.review import approve, reject
from schoolops_sync.sync import sync_file, sync_folder

STD1 = "English - Std 1.xlsx"


def count(conn, table, where="TRUE", params=()):
    return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE {where}", params).fetchone()["n"]


def student_at(conn, folder, sheet, row):
    name = cell(folder / STD1, sheet, row, "Student Name")
    return conn.execute("SELECT s.*, f.balance, f.paid FROM students s JOIN current_fees f ON f.student_id=s.id "
                        "WHERE s.name=%s AND s.source_sheet=%s", (name, sheet)).fetchone()


def add_call(conn, sid, outcome="will_pay"):
    conn.execute("INSERT INTO calls (student_id, outcome, caller) VALUES (%s,%s,'Test')", (sid, outcome))


def test_dry_run_writes_no_student_data(conn, cfg):
    res = sync_folder(conn, cfg, dry_run=True)
    assert all(f["status"] == "dry_run" for f in res["files"])
    assert sum(f["new_students"] for f in res["files"]) == 84
    assert count(conn, "students") == 0 and count(conn, "fee_snapshots") == 0
    assert count(conn, "file_state") == 0 and count(conn, "review_items") == 0  # dry run: no suggestions either
    assert count(conn, "sync_runs", "status='dry_run'") == 5  # only the report is stored


def test_apply_then_resync_creates_no_duplicates(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    assert count(conn, "students") == 84
    again = sync_folder(conn, cfg, dry_run=False)
    assert all(f["status"] == "unchanged" for f in again["files"])
    forced = sync_folder(conn, cfg, dry_run=False, force=True)
    assert all(f["new_students"] == 0 and f["fee_updates"] == 0 for f in forced["files"])
    assert count(conn, "students") == 84 and count(conn, "fee_snapshots") == 84


def test_payment_recorded_in_excel_is_synced_and_history_kept(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    before = student_at(conn, folder, "1 Peacock", 3)
    if before["balance"] == 0:
        pytest.skip("synthetic student already has zero balance")
    edit_cell(folder / STD1, "1 Peacock", 3, "Paid Amt", float(before["paid"] + before["balance"]))
    edit_cell(folder / STD1, "1 Peacock", 3, "Balance", 0)
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["fee_updates"] == 1 and res["needs_review"] == 0
    after = student_at(conn, folder, "1 Peacock", 3)
    assert after["id"] == before["id"] and after["balance"] == 0
    assert count(conn, "fee_snapshots", "student_id=%s", (before["id"],)) == 2  # old value kept


def test_balance_going_up_waits_for_review(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    s = student_at(conn, folder, "1 Swan", 4)
    edit_cell(folder / STD1, "1 Swan", 4, "Balance", float(s["balance"] + 5000))
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["needs_review"] == 1 and res["fee_updates"] == 0
    assert student_at(conn, folder, "1 Swan", 4)["balance"] == s["balance"]  # not applied
    item = conn.execute("SELECT * FROM review_items WHERE kind <> 'possible_siblings'").fetchone()
    assert item["kind"] == "fee_change" and "balance_increased" in item["details"]["reasons"]
    # same change seen again -> no second review item
    sync_file(conn, cfg, folder / STD1, dry_run=False, force=True)
    assert count(conn, "review_items", "kind <> 'possible_siblings'") == 1
    approve(conn, item["id"], by="office")
    assert student_at(conn, folder, "1 Swan", 4)["balance"] == s["balance"] + 5000


def test_paid_going_down_waits_for_review(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    s = student_at(conn, folder, "1 Swan", 5)
    edit_cell(folder / STD1, "1 Swan", 5, "Paid Amt", float(s["paid"] - 1)
              if s["paid"] > 0 else 0)
    if s["paid"] == 0:
        pytest.skip("synthetic student has paid nothing")
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["needs_review"] == 1
    item = conn.execute("SELECT * FROM review_items WHERE kind <> 'possible_siblings'").fetchone()
    assert "paid_decreased" in item["details"]["reasons"]
    reject(conn, item["id"], by="office")
    assert student_at(conn, folder, "1 Swan", 5)["paid"] == s["paid"]


def test_name_correction_is_reviewed_not_duplicated_and_keeps_calls(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    s = student_at(conn, folder, "1 Peacock", 6)
    add_call(conn, s["id"])
    edit_cell(folder / STD1, "1 Peacock", 6, "Student Name", s["name"] + " Kumar")
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["needs_review"] == 1 and res["new_students"] == 0 and res["missing_from_file"] == 0
    assert count(conn, "students") == 84
    item = conn.execute("SELECT * FROM review_items WHERE kind <> 'possible_siblings'").fetchone()
    assert item["kind"] == "possible_same_student" and item["student_id"] == s["id"]
    approve(conn, item["id"])  # same child, spelling fixed
    row = conn.execute("SELECT * FROM students WHERE id=%s", (s["id"],)).fetchone()
    assert row["name"] == s["name"] + " Kumar"
    assert count(conn, "calls", "student_id=%s", (s["id"],)) == 1
    assert count(conn, "students") == 84
    assert sync_file(conn, cfg, folder / STD1, dry_run=False, force=True)["needs_review"] == 0


def test_rejecting_a_possible_match_adds_a_new_student(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    s = student_at(conn, folder, "1 Peacock", 7)
    edit_cell(folder / STD1, "1 Peacock", 7, "Student Name", "Different Child")
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    item = conn.execute("SELECT * FROM review_items WHERE kind <> 'possible_siblings'").fetchone()
    reject(conn, item["id"])
    assert count(conn, "students") == 85
    assert conn.execute("SELECT name FROM students WHERE id=%s", (s["id"],)).fetchone()["name"] == s["name"]


def test_student_removed_from_excel_is_flagged_not_deleted(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    last_row = 3 + 12 - 1
    s = student_at(conn, folder, "1 Swan", last_row)
    add_call(conn, s["id"], "no_answer")
    delete_row(folder / STD1, "1 Swan", last_row)
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["missing_from_file"] == 1
    row = conn.execute("SELECT * FROM students WHERE id=%s", (s["id"],)).fetchone()
    assert row is not None and row["missing_since"] is not None
    assert count(conn, "calls", "student_id=%s", (s["id"],)) == 1


def test_failed_sheet_does_not_mark_its_students_missing(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    wb = load_workbook(folder / STD1)
    wb["1 Swan"]["H2"] = "Remaining"
    wb.save(folder / STD1)
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["sheets_failed"] == ["1 Swan"] and res["missing_from_file"] == 0
    assert count(conn, "students", "missing_since IS NOT NULL") == 0


def test_calls_survive_every_resync(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    ids = [r["id"] for r in conn.execute("SELECT id FROM students LIMIT 10")]
    for i in ids:
        add_call(conn, i)
    edit_cell(folder / STD1, "1 Peacock", 3, "Contact", "9000099999")
    sync_folder(conn, cfg, dry_run=False)
    sync_folder(conn, cfg, dry_run=False, force=True)
    assert count(conn, "calls") == 10


def test_sync_never_modifies_excel(conn, cfg, folder):
    before = {p.name: (sha256_file(p), p.stat().st_mtime_ns) for p in folder.glob("*.xlsx")}
    sync_folder(conn, cfg, dry_run=False)
    sync_folder(conn, cfg, dry_run=True, force=True)
    assert {p.name: (sha256_file(p), p.stat().st_mtime_ns) for p in folder.glob("*.xlsx")} == before


def test_test_mode_refuses_a_real_folder(conn, cfg, folder):
    (folder / SYNTHETIC_MARKER).unlink()
    with pytest.raises(NotAllowed):
        sync_folder(conn, cfg, dry_run=True)


def test_unconfirmed_mapping_refuses_real_use(conn, cfg):
    cfg.test_mode = False
    cfg.mapping_confirmed = False
    with pytest.raises(NotAllowed):
        sync_folder(conn, cfg, dry_run=True)


def test_confirmed_payment_rule_can_apply_increases(conn, cfg, folder):
    cfg.payment_rules_confirmed = True
    cfg.balance_increase = "apply"
    sync_folder(conn, cfg, dry_run=False)
    s = student_at(conn, folder, "1 Swan", 6)
    edit_cell(folder / STD1, "1 Swan", 6, "Balance", float(s["balance"] + 100))
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["fee_updates"] == 1 and res["needs_review"] == 0
    assert student_at(conn, folder, "1 Swan", 6)["balance"] == s["balance"] + Decimal(100)


def test_corrupt_file_is_logged_and_others_still_sync(conn, cfg, folder):
    (folder / "Broken.xlsx").write_bytes(b"half saved")
    res = sync_folder(conn, cfg, dry_run=False)
    statuses = {f["file"]: f["status"] for f in res["files"]}
    assert statuses["Broken.xlsx"] == "failed"
    assert list(statuses.values()).count("applied") == 5
    assert count(conn, "sync_issues", "code='unreadable_file'") == 1


def test_lock_and_temp_files_are_ignored(conn, cfg, folder):
    (folder / "~$English - Std 1.xlsx").write_bytes(b"lock")
    (folder / ".~lock.English - Std 1.xlsx#").write_bytes(b"lock")
    res = sync_folder(conn, cfg, dry_run=True)
    assert len(res["files"]) == 5


def test_renamed_file_is_recognised_without_duplicates(conn, cfg, folder):
    sync_folder(conn, cfg, dry_run=False)
    (folder / STD1).rename(folder / "English - Std 1 (2026-27).xlsx")
    res = sync_folder(conn, cfg, dry_run=False)
    r = next(f for f in res["files"] if f["file"].startswith("English - Std 1 ("))
    assert r["status"] == "applied" and r["new_students"] == 0 and r["missing_from_file"] == 0
    assert count(conn, "students") == 84
    assert count(conn, "sync_issues", "code='file_renamed'") == 1
    assert res["files_missing_from_folder"] == []  # state moved with the file


def test_files_in_subfolders_when_recursive(conn, cfg, folder):
    sub = folder / "Semi-English"
    sub.mkdir()
    (folder / "Semi-English - Std 1.xlsx").rename(sub / "Semi-English - Std 1.xlsx")
    cfg.recursive = False
    assert count_files(sync_folder(conn, cfg, dry_run=True)) == 4
    cfg.recursive = True
    res = sync_folder(conn, cfg, dry_run=False)
    assert count_files(res) == 5 and count(conn, "students") == 84


def count_files(res):
    return len(res["files"])


def test_unreadable_file_is_reported_not_skipped(conn, cfg, folder):
    (folder / "Old format.xls").write_bytes(b"garbage")
    res = sync_folder(conn, cfg, dry_run=False)
    bad = next(f for f in res["files"] if f["file"] == "Old format.xls")
    assert bad["status"] == "failed" and bad["errors"] == 1
