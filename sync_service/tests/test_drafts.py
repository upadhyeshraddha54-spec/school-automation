"""Reminder drafts: real values only, sibling summaries, edit/approve states, sending disabled."""
import pytest
from fastapi.testclient import TestClient

from schoolops_sync.api import create_app
from schoolops_sync.messages import rupees
from schoolops_sync.sync import sync_folder


@pytest.fixture
def client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        yield c


def due(conn, n=1):
    return conn.execute("""SELECT s.id, s.name, f.balance FROM students s JOIN current_fees f ON f.student_id=s.id
                           WHERE f.balance > 0 ORDER BY s.id LIMIT %s""", (n,)).fetchall()


@pytest.mark.parametrize("lang,word", [("en", "pending"), ("hi", "बकाया"), ("mr", "बाकी")])
def test_single_child_draft_uses_real_values(client, conn, lang, word):
    s = due(conn)[0]
    d = client.post("/drafts", json={"student_ids": [s["id"]], "lang": lang}).json()
    assert d["status"] == "draft" and s["name"] in d["body"] and rupees(s["balance"]) in d["body"] and word in d["body"]
    assert "Synthetic Test School" in d["body"]


def test_sibling_draft_lists_each_child_and_total(client, conn):
    it = conn.execute("SELECT * FROM review_items WHERE kind='possible_siblings' ORDER BY id").fetchone()
    client.post(f"/review/{it['id']}/approve")
    ids = [x["id"] for x in it["details"]["students"]]
    kids = conn.execute("SELECT s.id, s.name, f.balance FROM students s JOIN current_fees f ON f.student_id=s.id "
                        "WHERE s.id = ANY(%s)", (ids,)).fetchall()
    d = client.post("/drafts", json={"student_ids": ids, "lang": "mr"}).json()
    pending = [k for k in kids if k["balance"] > 0]
    for k in pending:
        assert k["name"] in d["body"] and rupees(k["balance"]) in d["body"]
    if len(pending) == 2:
        assert rupees(sum(k["balance"] for k in pending)) in d["body"] and "एकूण" in d["body"]
    for k in kids:
        if k["balance"] <= 0:
            assert k["name"] not in d["body"] and any(x["id"] == k["id"] for x in d["left_out"])


def test_never_invents_amounts(client, conn):
    zero = conn.execute("SELECT s.id FROM students s JOIN current_fees f ON f.student_id=s.id WHERE f.balance <= 0 "
                        "LIMIT 1").fetchone()
    r = client.post("/drafts", json={"student_ids": [zero["id"]], "lang": "en"})
    assert r.status_code == 422 and "nothing to remind" in r.json()["detail"]
    # a student with no fee row from Excel at all
    fam = conn.execute("INSERT INTO families DEFAULT VALUES RETURNING id").fetchone()["id"]
    sid = conn.execute("""INSERT INTO students (identity_key, medium, std, section, name, family_id)
                          VALUES ('test|x', 'English', '1', 'Peacock', 'No Amount Child', %s) RETURNING id""",
                       (fam,)).fetchone()["id"]
    assert client.post("/drafts", json={"student_ids": [sid], "lang": "en"}).status_code == 422
    assert client.get(f"/messages/preview/{sid}").status_code == 409


def test_combined_draft_only_for_one_parent(client, conn):
    a, b = due(conn, 2)
    r = client.post("/drafts", json={"student_ids": [a["id"], b["id"]], "lang": "en"})
    assert r.status_code == 422 and "same parent" in r.json()["detail"]


def test_edit_approve_discard_and_no_sending(client, conn):
    s = due(conn)[0]
    d = client.post("/drafts", json={"student_ids": [s["id"]], "lang": "en"}).json()
    e = client.put(f"/drafts/{d['id']}", json={"body": d["body"] + " Office hours 9-1."}).json()
    assert e["edited"] is True and e["status"] == "draft"
    a = client.post(f"/drafts/{d['id']}/approve", params={"by": "Office"}).json()
    assert a["status"] == "approved" and a["approved_by"] == "Office"
    assert client.post(f"/drafts/{d['id']}/approve").status_code == 409  # already approved
    back = client.put(f"/drafts/{d['id']}", json={"body": "Changed again"}).json()
    assert back["status"] == "draft" and back["approved_at"] is None  # edits need a new approval
    assert client.put(f"/drafts/{d['id']}", json={"body": "   "}).status_code == 409
    r = client.post(f"/drafts/{d['id']}/send")
    assert r.status_code == 409 and "turned off" in r.json()["detail"]
    client.post(f"/drafts/{d['id']}/discard")
    assert client.post(f"/drafts/{d['id']}/approve").status_code == 409
    lst = client.get("/drafts").json()
    assert lst["total"] == 1 and lst["items"][0]["status"] == "discarded" and s["name"] in lst["items"][0]["children"]
    assert conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 0  # nothing went out
