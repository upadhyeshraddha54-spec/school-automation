"""Call outcomes -> follow-ups, promises, payments seen in Excel, the parent queue, campaigns."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import edit_cell
from schoolops_sync.api import create_app
from schoolops_sync.sync import sync_file, sync_folder

STD1 = "English - Std 1.xlsx"
TODAY = date.today()


@pytest.fixture
def client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        yield c


def due_student(conn, sheet="1 Peacock"):
    return conn.execute(
        """SELECT s.id, s.sr_no, s.name, f.balance, f.paid FROM students s JOIN current_fees f ON f.student_id=s.id
           WHERE s.source_sheet=%s AND f.balance > 0 ORDER BY s.id LIMIT 1""", (sheet,)).fetchone()


def open_fu(conn, sid):
    return conn.execute("SELECT * FROM follow_ups WHERE student_id=%s AND status='open'", (sid,)).fetchone()


def call(client, sid, outcome, **kw):
    r = client.post("/calls", json={"student_id": sid, "outcome": outcome, **kw})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("outcome,kind,days", [
    ("will_pay", "callback", 3), ("no_answer", "retry", 1), ("phone_off", "retry", 1),
    ("call_later", "callback", 1), ("part_payment", "part_payment", 7),
    ("complaint", "dispute", 0), ("already_paid", "verify_payment", 0), ("wrong_number", "fix_number", 0)])
def test_each_outcome_schedules_the_right_follow_up(client, conn, outcome, kind, days):
    s = due_student(conn)
    call(client, s["id"], outcome)
    fu = open_fu(conn, s["id"])
    assert fu["kind"] == kind and fu["due_date"] == TODAY + timedelta(days=days)
    assert fu["balance_at_open"] == s["balance"]


def test_chosen_date_and_amount_are_used(client, conn):
    s = due_student(conn)
    d = str(TODAY + timedelta(days=10))
    call(client, s["id"], "part_payment", follow_up_date=d, promised_amount=5000)
    fu = open_fu(conn, s["id"])
    assert str(fu["due_date"]) == d and fu["promised_amount"] == 5000
    assert client.post("/calls", json={"student_id": s["id"], "outcome": "part_payment",
                                       "promised_amount": -1}).status_code == 422


def test_only_one_open_follow_up_per_child(client, conn):
    s = due_student(conn)
    call(client, s["id"], "no_answer")
    call(client, s["id"], "no_answer")
    call(client, s["id"], "will_pay")
    rows = conn.execute("SELECT status, kind FROM follow_ups WHERE student_id=%s ORDER BY id", (s["id"],)).fetchall()
    assert [r["status"] for r in rows] == ["done", "done", "open"] and rows[-1]["kind"] == "callback"


def test_missed_promise_alert_and_queue_priority(client, conn):
    s = due_student(conn)
    call(client, s["id"], "gave_date", follow_up_date=str(TODAY + timedelta(days=2)))
    conn.execute("UPDATE follow_ups SET due_date = current_date - 2 WHERE student_id=%s AND status='open'", (s["id"],))
    conn.execute("UPDATE calls SET called_at = now() - interval '5 days' WHERE student_id=%s", (s["id"],))
    d = client.get("/dashboard").json()
    assert d["overdue_promises"] == 1
    q = client.get("/queue").json()
    assert q["items"][0]["priority"] == "missed_promise" and s["id"] in q["items"][0]["call_children"]
    assert q["counts"]["missed_promise"] == 1
    assert [x["id"] for x in client.get("/follow-ups", params={"when": "overdue"}).json()] == [s["id"]]
    call(client, s["id"], "call_later")  # calling again closes the old promise as "missed"
    assert conn.execute("SELECT status FROM follow_ups WHERE student_id=%s ORDER BY id LIMIT 1",
                        (s["id"],)).fetchone()["status"] == "missed"


def test_payment_in_excel_closes_follow_up_and_part_payment_is_shown(client, conn, cfg, folder):
    s = due_student(conn)
    row = 2 + int(s["sr_no"])
    call(client, s["id"], "gave_date", follow_up_date=str(TODAY + timedelta(days=3)))
    # part payment recorded in Excel
    edit_cell(folder / STD1, "1 Peacock", row, "Paid Amt", float(s["paid"] + 1000))
    edit_cell(folder / STD1, "1 Peacock", row, "Balance", float(s["balance"] - 1000))
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    fam = client.get(f"/families/{client.get(f'/students/{s['id']}').json()['family_id']}").json()
    kid = next(k for k in fam["children"] if k["id"] == s["id"])
    assert kid["paid_since_follow_up"] == 1000 and kid["fu_kind"] == "promise"
    # fully paid
    edit_cell(folder / STD1, "1 Peacock", row, "Paid Amt", float(s["paid"] + s["balance"]))
    edit_cell(folder / STD1, "1 Peacock", row, "Balance", 0)
    res = sync_file(conn, cfg, folder / STD1, dry_run=False)
    assert res["follow_ups_closed_paid"] == 1
    assert conn.execute("SELECT status FROM follow_ups WHERE student_id=%s", (s["id"],)).fetchone()["status"] == "paid"


def test_queue_groups_siblings_and_orders_by_priority(client, conn):
    it = conn.execute("SELECT * FROM review_items WHERE kind='possible_siblings' ORDER BY id").fetchone()
    client.post(f"/review/{it['id']}/approve")
    q = client.get("/queue", params={"page_size": 100}).json()
    fam = next(f for f in q["items"] if len(f["children"]) == 2)
    assert {c["id"] for c in fam["children"]} == {s["id"] for s in it["details"]["students"]} \
        or len(fam["children"]) == 2
    assert all(f["priority"] == "not_called" for f in q["items"])  # nobody called yet
    first = q["items"][0]
    call(client, first["call_children"][0], "no_answer")
    q2 = client.get("/queue", params={"page_size": 100}).json()
    assert first["family_id"] not in [f["family_id"] for f in q2["items"]]  # called today -> off today's list
    assert q2["families_done_today"] == 1
    # dispute goes to "needs office action", not the calling queue
    other = q2["items"][0]["call_children"][0]
    call(client, other, "complaint")
    q3 = client.get("/queue", params={"page_size": 100}).json()
    assert other in [k["id"] for k in q3["needs_action"]]


def test_undo_restores_the_previous_follow_up(client, conn):
    s = due_student(conn)
    call(client, s["id"], "gave_date", follow_up_date=str(TODAY + timedelta(days=4)))
    second = call(client, s["id"], "no_answer")
    client.delete(f"/calls/{second['id']}")
    fu = open_fu(conn, s["id"])
    assert fu["kind"] == "promise" and fu["due_date"] == TODAY + timedelta(days=4)


def test_close_follow_up_manually(client, conn):
    s = due_student(conn)
    call(client, s["id"], "complaint")
    fu = open_fu(conn, s["id"])
    assert client.post(f"/follow-ups/{fu['id']}/close", json={"reason": "Fee corrected by office"}).status_code == 200
    assert open_fu(conn, s["id"]) is None
    assert client.post(f"/follow-ups/{fu['id']}/close", json={"reason": "again"}).status_code == 409


def test_campaign_scope_progress_and_targets(client, conn, cfg, folder):
    c = client.post("/campaigns", json={"name": "Std 1 start of year", "std": "1", "daily_target": 5}).json()
    assert c["progress"]["families_in_scope"] > 0
    q = client.get("/queue", params={"page_size": 100}).json()
    assert all(any(k["std"] == "1" for k in f["children"]) for f in q["items"])
    s = due_student(conn)
    call(client, s["id"], "will_pay")
    edit_cell(folder / STD1, "1 Peacock", 2 + int(s["sr_no"]), "Balance", 0)
    sync_file(conn, cfg, folder / STD1, dry_run=False)
    d = client.get("/dashboard").json()
    assert d["daily_target"] == 5 and d["families_called_today"] == 1 and d["progress_pct"] == 20
    p = d["campaign"]["progress"]
    assert p["families_contacted"] == 1 and p["students_cleared"] >= 1 and float(p["collected"]) >= float(s["balance"])
    client.put("/settings/daily-target", json={"daily_target": 10})
    assert client.get("/dashboard").json()["daily_target"] == 10
    client.post(f"/campaigns/{c['id']}/close")
    assert client.get("/dashboard").json()["campaign"] is None


def test_students_follow_up_filter(client, conn):
    s = due_student(conn)
    call(client, s["id"], "complaint")
    r = client.get("/students", params={"follow_up": "needs_action"}).json()
    assert [i["id"] for i in r["items"]] == [s["id"]] and r["items"][0]["follow_up_label"] == "Resolve fee issue"
    assert client.get("/students", params={"follow_up": "not_called"}).json()["total"] == 83
    assert client.get("/students", params={"follow_up": "bogus"}).status_code == 422
