"""WhatsApp + SMS reminder drafts for follow-up lists (never sent)."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from schoolops_sync.api import create_app
from schoolops_sync.db import connect
from schoolops_sync.outreach import AutoDrafts, prepare_batch, sms_parts
from schoolops_sync.sync import sync_folder


@pytest.fixture
def client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    with TestClient(create_app(cfg, start_watcher=False)) as c:
        yield c


def families_due(conn):
    return conn.execute("""SELECT count(DISTINCT s.family_id) AS n FROM students s JOIN current_fees f
                           ON f.student_id=s.id WHERE f.balance > 0""").fetchone()["n"]


def test_batch_one_draft_per_parent_per_channel_per_day(client, conn, cfg):
    r = client.post("/drafts/batch", json={"list": "all_unpaid", "channels": ["whatsapp", "sms"], "lang": "mr"}).json()
    n = families_due(conn)
    assert r["created"] == 2 * n and r["sending"] == "disabled"
    rows = conn.execute("SELECT channel, phone, segments, status, reason FROM message_drafts").fetchall()
    assert {x["channel"] for x in rows} == {"whatsapp", "sms"} and all(x["status"] == "draft" for x in rows)
    assert all(x["phone"] and len(x["phone"]) == 10 for x in rows)
    assert all(x["segments"] >= 1 for x in rows if x["channel"] == "sms")
    again = client.post("/drafts/batch", json={"list": "all_unpaid", "lang": "mr"}).json()
    assert again["created"] == 0 and all("already prepared" in s["why"] for s in again["skipped"])


def test_siblings_get_one_combined_draft(client, conn):
    it = conn.execute("SELECT * FROM review_items WHERE kind='possible_siblings' ORDER BY id").fetchone()
    client.post(f"/review/{it['id']}/approve")
    ids = [x["id"] for x in it["details"]["students"]]
    fam = conn.execute("SELECT family_id FROM students WHERE id=%s", (ids[0],)).fetchone()["family_id"]
    client.post("/drafts/batch", json={"list": "all_unpaid", "channels": ["whatsapp"], "lang": "en"})
    d = conn.execute("SELECT * FROM message_drafts WHERE family_id=%s", (fam,)).fetchall()
    assert len(d) == 1
    due = conn.execute("SELECT s.name FROM students s JOIN current_fees f ON f.student_id=s.id "
                       "WHERE s.id = ANY(%s) AND f.balance > 0", (ids,)).fetchall()
    for k in due:
        assert k["name"] in d[0]["body"]


def test_parent_without_mobile_is_skipped_with_reason(client, conn):
    s = conn.execute("""SELECT s.id, s.name FROM students s JOIN current_fees f ON f.student_id=s.id
                        WHERE f.balance > 0 ORDER BY s.id LIMIT 1""").fetchone()
    conn.execute("UPDATE students SET contact = NULL WHERE id=%s", (s["id"],))
    r = client.post("/drafts/batch", json={"list": "all_unpaid", "channels": ["sms"], "lang": "en"}).json()
    assert any(x["why"] == "No valid mobile number in Excel" and s["name"] in x["name"] for x in r["skipped"])


def test_missed_promise_and_due_soon_lists(client, conn):
    a, b = [r["id"] for r in conn.execute("""SELECT s.id FROM students s JOIN current_fees f ON f.student_id=s.id
            WHERE f.balance > 0 AND s.source_sheet='1 Peacock' ORDER BY s.id LIMIT 2""")]
    client.post("/calls", json={"student_id": a, "outcome": "gave_date", "follow_up_date": str(date.today() + timedelta(days=2))})
    client.post("/calls", json={"student_id": b, "outcome": "gave_date", "follow_up_date": str(date.today() + timedelta(days=5))})
    conn.execute("UPDATE follow_ups SET due_date = current_date - 1 WHERE student_id=%s", (b,))
    conn.execute("UPDATE calls SET called_at = now() - interval '6 days'")
    lists = {x["id"]: x["families"] for x in client.get("/drafts/lists").json()}
    assert lists["missed_promise"] == 1 and lists["due_soon"] == 1
    r = client.post("/drafts/batch", json={"list": "missed_promise", "channels": ["sms"], "lang": "hi"}).json()
    assert r["created"] == 1
    d = conn.execute("SELECT * FROM message_drafts").fetchone()
    assert d["reason"] == "missed_promise" and d["student_ids"] == [b] and d["segments"] >= 1


def test_sms_parts():
    assert sms_parts("a" * 160) == 1 and sms_parts("a" * 161) == 2
    assert sms_parts("फी" * 35) == 1 and sms_parts("फी" * 36) == 2


def test_approve_many_edit_and_never_send(client, conn):
    client.post("/drafts/batch", json={"list": "all_unpaid", "channels": ["sms"], "lang": "en"})
    ids = [r["id"] for r in conn.execute("SELECT id FROM message_drafts ORDER BY id LIMIT 3")]
    r = client.post("/drafts/approve-many", json={"ids": ids, "by": "Office"}).json()
    assert r["approved"] == ids
    e = client.put(f"/drafts/{ids[0]}", json={"body": "x" * 200}).json()
    assert e["status"] == "draft" and e["segments"] == 2
    assert client.post(f"/drafts/{ids[1]}/send").status_code == 409
    client.post("/drafts/discard-many", json={"ids": [ids[2]]})
    assert conn.execute("SELECT status FROM message_drafts WHERE id=%s", (ids[2],)).fetchone()["status"] == "discarded"
    # after discarding, a new SMS draft for that parent can be prepared again today
    again = client.post("/drafts/batch", json={"list": "all_unpaid", "channels": ["sms"], "lang": "en"}).json()
    assert again["created"] == 1
    assert client.get("/drafts", params={"channel": "sms", "status": "approved"}).json()["total"] == 1


def test_template_names_from_config(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    cfg.messaging = {"whatsapp_templates": {"mr": "fee_reminder_mr"}, "sms_templates": {"mr": "1107161234567890"}}
    with conn.transaction():
        prepare_batch(conn, cfg, "all_unpaid", lang="mr")
    t = {r["channel"]: r["template_name"] for r in conn.execute("SELECT DISTINCT channel, template_name FROM message_drafts")}
    assert t == {"whatsapp": "fee_reminder_mr", "sms": "1107161234567890"}


def test_daily_auto_drafts_once_per_day(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    cfg.messaging = {"auto_drafts": True, "auto_drafts_time": "09:00", "auto_drafts_lists": ["all_unpaid"],
                     "auto_drafts_channels": ["whatsapp"], "auto_drafts_language": "en"}
    a = AutoDrafts(cfg, connect)
    today = datetime.now().replace(second=0, microsecond=0)
    assert a.run_if_due(today.replace(hour=8, minute=59)) is None
    res = a.run_if_due(today.replace(hour=9, minute=1))
    assert res["all_unpaid"]["created"] == families_due(conn)
    assert a.run_if_due(today.replace(hour=15)) is None  # already done today
    assert conn.execute("SELECT count(*) AS n FROM message_drafts WHERE status <> 'draft'").fetchone()["n"] == 0
