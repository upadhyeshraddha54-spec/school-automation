"""Parents with several children: shared phones, different classes, separate balances."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from schoolops_sync.api import create_app
from schoolops_sync.config import SYNTHETIC_MARKER
from schoolops_sync.sync import sync_folder


@pytest.fixture
def client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        yield c


def sibling_reviews(conn, status="pending"):
    return conn.execute("SELECT * FROM review_items WHERE kind='possible_siblings' AND status=%s ORDER BY id",
                        (status,)).fetchall()


def test_shared_phone_is_suggested_never_merged(client, conn):
    assert conn.execute("SELECT count(*) AS n FROM students WHERE family_id IS NULL").fetchone()["n"] == 0
    items = sibling_reviews(conn)
    assert len(items) == 4  # the 4 synthetic sibling pairs
    for it in items:
        fams = {s["family_id"] for s in it["details"]["students"]}
        assert len(fams) == 2  # still separate until staff decide
    # phone is not an identity: both children are found by it, both remain separate students
    phone = items[0]["details"]["phone"]
    assert client.get("/students", params={"search": phone}).json()["total"] == 2


def test_confirmed_siblings_keep_separate_balances_and_combined_total(client, conn):
    it = sibling_reviews(conn)[0]
    assert client.post(f"/review/{it['id']}/approve").json()["status"] == "approved"
    a, b = [s["id"] for s in it["details"]["students"]]
    fa = conn.execute("SELECT family_id FROM students WHERE id=%s", (a,)).fetchone()["family_id"]
    assert conn.execute("SELECT family_id FROM students WHERE id=%s", (b,)).fetchone()["family_id"] == fa
    fam = client.get(f"/families/{fa}").json()
    assert len(fam["children"]) == 2 and fam["verified"] is True
    assert len({c["klass"] for c in fam["children"]}) == 2  # different classes
    bal = {c["id"]: Decimal(str(c["balance"])) for c in fam["children"]}
    assert Decimal(str(fam["total_due"])) == sum(v for v in bal.values() if v > 0)
    sib = client.get(f"/students/{a}").json()["siblings"]
    assert [s["id"] for s in sib] == [b]


def test_resync_after_decisions_creates_no_duplicates_or_repeat_questions(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    items = sibling_reviews(conn)
    from schoolops_sync.review import approve, reject
    approve(conn, items[0]["id"])
    reject(conn, items[1]["id"])
    n_students = conn.execute("SELECT count(*) AS n FROM students").fetchone()["n"]
    n_fam = conn.execute("SELECT count(*) AS n FROM families").fetchone()["n"]
    sync_folder(conn, cfg, dry_run=False, force=True)
    sync_folder(conn, cfg, dry_run=False, force=True)
    assert conn.execute("SELECT count(*) AS n FROM students").fetchone()["n"] == n_students == 84
    assert conn.execute("SELECT count(*) AS n FROM families").fetchone()["n"] == n_fam == 83
    assert len(sibling_reviews(conn)) == 2  # the 2 undecided pairs only; the rejected one is not asked again
    assert len(sibling_reviews(conn, "rejected")) == 1


def test_one_call_for_several_siblings(client, conn):
    it = sibling_reviews(conn)[0]
    client.post(f"/review/{it['id']}/approve")
    a, b = [s["id"] for s in it["details"]["students"]]
    due = str(date.today() + timedelta(days=5))
    r = client.post("/calls", json={"student_ids": [a, b], "outcome": "gave_date", "follow_up_date": due,
                                    "note": "Pays for both", "caller": "Office"})
    assert r.status_code == 201
    body = r.json()
    assert len(body["calls"]) == 2 and len({c["call_group"] for c in body["calls"]}) == 1
    assert len(body["follow_ups"]) == 2
    for sid in (a, b):  # each child keeps their own history and follow-up
        d = client.get(f"/students/{sid}").json()
        assert len(d["calls"]) == 1 and d["follow_ups"][0]["kind"] == "promise" and d["follow_ups"][0]["status"] == "open"
    fam = client.get(f"/families/{client.get(f'/students/{a}').json()['family_id']}").json()
    assert len(fam["calls"]) == 1 and "," in fam["calls"][0]["children"]  # one call, both children
    assert client.get("/dashboard").json()["calls_today"] == 1
    # undo removes the call for both children
    client.delete(f"/calls/{body['id']}")
    assert client.get("/calls", params={"student_id": a}).json()["total"] == 0
    assert client.get("/calls", params={"student_id": b}).json()["total"] == 0


def test_call_cannot_cover_children_of_different_parents(client, conn):
    ids = [r["id"] for r in conn.execute("SELECT id FROM students ORDER BY id LIMIT 2")]
    r = client.post("/calls", json={"student_ids": ids, "outcome": "will_pay"})
    assert r.status_code == 422 and "same parent" in r.json()["detail"]


def test_manual_link_and_unlink(client, conn):
    a, b = [r["id"] for r in conn.execute("SELECT id FROM students ORDER BY id LIMIT 2")]
    fa = client.get(f"/students/{a}").json()["family_id"]
    client.post(f"/families/{fa}/link", json={"student_id": b})
    assert client.get(f"/students/{b}").json()["family_id"] == fa
    client.post(f"/students/{b}/unlink")
    assert client.get(f"/students/{b}").json()["family_id"] != fa
    assert len(client.get(f"/families/{fa}").json()["children"]) == 1


def test_verified_family_id_column_links_automatically(conn, cfg, tmp_path):
    folder = tmp_path / "fam"
    folder.mkdir()
    (folder / SYNTHETIC_MARKER).write_text("x")
    cfg.watch_folder = folder
    cfg.columns = {**cfg.columns, "family_id": "Family ID", "parent_name": "Parent Name"}
    for label, rows in (("1", [(1, "Kale Om", "F-100"), (2, "Joshi Riya", "F-200")]),
                        ("10", [(1, "Kale Isha", "F-100")])):
        wb = Workbook()
        ws = wb.active
        ws.title = f"{label} Peacock"
        ws.append(["SYNTHETIC"])
        ws.append(["Sr. No.", "Student Name", "Class", "Contact", "Total Amt", "Disc. Amt", "Paid Amt", "Balance",
                   "Family ID", "Parent Name"])
        for sr, name, fam in rows:
            ws.append([sr, name, f"{label}-Peacock", "9000011111" if fam == "F-100" else "9000022222",
                       20000, 0, 5000, 15000, fam, "Mr. Kale" if fam == "F-100" else "Mrs. Joshi"])
        wb.save(folder / f"English - Std {label}.xlsx")
    sync_folder(conn, cfg, dry_run=False)
    rows = conn.execute("SELECT s.name, s.family_id, f.verified, f.display_name FROM students s "
                        "JOIN families f ON f.id=s.family_id ORDER BY s.name").fetchall()
    kale = [r for r in rows if r["name"].startswith("Kale")]
    assert len({r["family_id"] for r in kale}) == 1 and kale[0]["verified"] and kale[0]["display_name"] == "Mr. Kale"
    assert len(sibling_reviews(conn)) == 0  # verified IDs: nothing to ask
    sync_folder(conn, cfg, dry_run=False, force=True)
    assert conn.execute("SELECT count(*) AS n FROM families").fetchone()["n"] == 2
