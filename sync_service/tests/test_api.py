from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import edit_cell
from schoolops_sync.api import create_app
from schoolops_sync.sync import sync_folder


@pytest.fixture
def client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        yield c


def test_students_pagination_and_filters(client):
    r = client.get("/students", params={"page_size": 10}).json()
    assert r["total"] == 84 and r["pages"] == 9 and len(r["items"]) == 10
    assert r["items"][0]["std"] == "Nursery"  # Nursery comes first, then 1..10
    r2 = client.get("/students", params={"page": 9, "page_size": 10}).json()
    assert len(r2["items"]) == 4
    f = client.get("/students", params={"std": "1", "medium": "English", "section": "Peacock"}).json()
    assert f["total"] == 12 and {i["klass"] for i in f["items"]} == {"English 1-Peacock"}
    semi = client.get("/students", params={"medium": "Semi-English"}).json()
    assert semi["total"] == 24
    due = client.get("/students", params={"fee_status": "due", "page_size": 100}).json()
    assert all(float(i["balance"]) > 0 for i in due["items"])
    nb = client.get("/students", params={"fee_status": "no_balance_in_excel", "page_size": 100}).json()
    assert due["total"] + nb["total"] == 84


def test_search_by_name_and_phone(client):
    first = client.get("/students", params={"page_size": 1}).json()["items"][0]
    assert any(i["id"] == first["id"] for i in
               client.get("/students", params={"search": first["name"].split()[1].lower()}).json()["items"])
    assert client.get("/students", params={"search": first["contact"]}).json()["total"] == 1


def test_filters_endpoint(client):
    f = client.get("/filters").json()
    assert f["std"] == ["Nursery", "1", "10"]
    assert f["medium"] == ["English", "Semi-English"]
    assert "gave_date" in f["outcomes"]


def test_call_next_and_follow_ups(client):
    nxt = client.get("/call-next", params={"std": "1", "medium": "English"}).json()["student"]
    assert nxt is not None
    bad = client.post("/calls", json={"student_id": nxt["id"], "outcome": "gave_date"})
    assert bad.status_code == 422
    past = client.post("/calls", json={"student_id": nxt["id"], "outcome": "gave_date",
                                       "follow_up_date": str(date.today() - timedelta(days=1))})
    assert past.status_code == 422
    ok = client.post("/calls", json={"student_id": nxt["id"], "outcome": "gave_date", "caller": "Sneha",
                                     "follow_up_date": str(date.today() + timedelta(days=3)), "note": "after salary"})
    assert ok.status_code == 201
    nxt2 = client.get("/call-next", params={"std": "1", "medium": "English"}).json()["student"]
    assert nxt2["id"] != nxt["id"]  # already called today and has a future date
    up = client.get("/follow-ups", params={"when": "upcoming"}).json()
    assert [u["id"] for u in up] == [nxt["id"]]
    hist = client.get("/calls", params={"student_id": nxt["id"]}).json()
    assert hist["total"] == 1 and hist["items"][0]["outcome_label"] == "Gave a date"
    d = client.get("/dashboard").json()
    assert d["calls_today"] == 1 and d["promised_today"] == 1 and d["students"] == 84


def test_undo_recent_call(client):
    sid = client.get("/students").json()["items"][0]["id"]
    call = client.post("/calls", json={"student_id": sid, "outcome": "no_answer"}).json()
    assert client.delete(f"/calls/{call['id']}").status_code == 200
    assert client.get("/calls", params={"student_id": sid}).json()["total"] == 0


def test_unknown_outcome_rejected(client):
    sid = client.get("/students").json()["items"][0]["id"]
    assert client.post("/calls", json={"student_id": sid, "outcome": "paid"}).status_code == 422


def test_message_previews_three_languages_and_mock_only(client):
    s = client.get("/students", params={"fee_status": "due"}).json()["items"][0]
    p = client.get(f"/messages/preview/{s['id']}").json()
    assert set(p) == {"en", "hi", "mr"}
    assert s["name"] in p["en"] and "₹" in p["en"]
    assert "फीस" in p["hi"] and "फी" in p["mr"]
    m = client.post("/messages/mock", json={"student_id": s["id"], "lang": "mr"}).json()
    assert m["status"] == "mock_not_sent" and "No real message" in m["note"]
    assert client.get("/messages").json()["total"] == 1


def test_sync_endpoints_dry_run_and_path_safety(client, cfg, folder):
    r = client.post("/sync/run", json={"file": "English - Std 1.xlsx", "dry_run": True, "force": True}).json()
    assert r["status"] == "dry_run"
    assert client.post("/sync/run", json={"file": "../../etc/passwd"}).status_code == 400
    runs = client.get("/sync/runs").json()
    assert runs["total"] >= 6
    detail = client.get(f"/sync/runs/{r['run_id']}").json()
    assert "issues" in detail
    st = client.get("/sync/status").json()
    assert st["test_mode"] is True and st["mapping_confirmed"] is False


def test_review_via_api(client, folder):
    s = client.get("/students", params={"std": "10", "medium": "English", "section": "Swan"}).json()["items"][0]
    row = 2 + int(s["sr_no"])
    edit_cell(folder / "English - Std 10.xlsx", "10 Swan", row, "Balance", float(s["balance"]) + 999)
    client.post("/sync/run", json={"file": "English - Std 10.xlsx", "dry_run": False})
    items = client.get("/review").json()
    assert len(items) == 1
    assert client.post(f"/review/{items[0]['id']}/approve", params={"by": "office"}).json()["status"] == "approved"
    assert client.post(f"/review/{items[0]['id']}/approve").status_code == 409
    detail = client.get(f"/students/{s['id']}").json()
    assert float(detail["balance"]) == float(s["balance"]) + 999 and len(detail["fee_history"]) == 2


def test_classes_summary(client):
    rows = client.get("/classes").json()
    assert rows[0]["std"] == "Nursery" and sum(r["students"] for r in rows) == 84
    assert {r["klass"] for r in rows} >= {"English 1-Peacock", "Semi-English 10-Lotus"}


def test_app_cannot_apply_changes_to_real_data(client, cfg):
    cfg.test_mode = False
    cfg.mapping_confirmed = True
    r = client.post("/sync/run", json={"dry_run": False})
    assert r.status_code == 409 and "disabled" in r.json()["detail"]
