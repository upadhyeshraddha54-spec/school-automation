"""Automatic reminders: enable/disable, AI wording, confirmed Send All, duplicates, providers, webhooks.
All synthetic. Real providers are only exercised against mock transports - no network."""
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from schoolops_sync import ai, providers as pv
from schoolops_sync.api import create_app
from schoolops_sync.sync import sync_folder


@pytest.fixture
def app_client(conn, cfg):
    sync_folder(conn, cfg, dry_run=False)
    app = create_app(cfg, start_watcher=False)
    with TestClient(app) as c:
        yield c, app.state.schoolops


def approve_builtin(c, channel, lang="en", name=None):
    t = c.post("/templates/generate", json={"channel": channel, "lang": lang, "use_builtin": True}).json()
    assert t["problems"] == [] and t["status"] == "draft"
    return c.post(f"/templates/{t['id']}/approve", json={"provider_template": name, "by": "Office"}).json()


def due_families(conn, enabled_only=True):
    return conn.execute(f"""SELECT count(DISTINCT s.family_id) AS n FROM students s JOIN current_fees f ON f.student_id=s.id
                            WHERE f.balance > 0 {'AND s.reminders_enabled' if enabled_only else ''}""").fetchone()["n"]


# ------------------------------------------------------------------ enable / disable
def test_everyone_starts_disabled_and_toggles_work(app_client, conn):
    c, _ = app_client
    st = c.get("/reminders/status").json()
    assert st["students"]["enabled"] == 0 and st["test_mode"] is True and st["real_sending_switch"] is False
    sid = c.get("/reminders/students").json()["items"][0]["id"]
    assert c.post("/reminders/enable", json={"enabled": True, "student_ids": [sid], "by": "Office"}).json()["changed"] == 1
    row = conn.execute("SELECT reminders_enabled, reminders_changed_by FROM students WHERE id=%s", (sid,)).fetchone()
    assert row["reminders_enabled"] and row["reminders_changed_by"] == "Office"
    n_std1 = conn.execute("SELECT count(*) AS n FROM students WHERE std='1' AND missing_since IS NULL").fetchone()["n"]
    r = c.post("/reminders/enable", json={"enabled": True, "std": "1"}).json()  # Enable All (for the filter)
    assert r["changed"] == n_std1
    assert c.get("/reminders/students", params={"only": "enabled", "std": "10"}).json()["total"] == 0
    r = c.post("/reminders/enable", json={"enabled": False}).json()  # Disable All
    assert r["changed"] == conn.execute("SELECT count(*) AS n FROM students WHERE missing_since IS NULL").fetchone()["n"]
    assert c.get("/reminders/status").json()["students"]["enabled"] == 0


# ------------------------------------------------------------------ wording
def test_validation_rejects_numbers_links_and_missing_placeholders():
    assert ai.validate(ai.BUILTIN["en"], "whatsapp") == []
    assert ai.validate(ai.BUILTIN["mr"], "sms") == []
    assert any("numbers" in p for p in ai.validate(ai.BUILTIN["en"] + " Pay by 15 Oct.", "whatsapp"))
    assert any("links" in p for p in ai.validate(ai.BUILTIN["en"] + " See https://x.example", "whatsapp"))
    assert any("{total}" in p for p in ai.validate("Dear parent {children} {school}", "whatsapp"))
    assert any("Unknown" in p for p in ai.validate(ai.BUILTIN["en"] + " {due_date}", "whatsapp"))
    f = ai.provider_formats("Hi from {school}: {children} owe {total}.")
    assert f["whatsapp_template_text"] == "Hi from {{1}}: {{2}} owe {{3}}."
    assert f["dlt_template_text"] == "Hi from {#var#}: {#var#} owe {#var#}."


def test_ai_generation_sends_no_student_data(app_client, conn, monkeypatch):
    c, state = app_client
    seen = []
    names = [r["name"] for r in conn.execute("SELECT name FROM students")]

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"content": [{"type": "text", "text": ai.BUILTIN["hi"]}]})

    assert c.post("/templates/generate", json={"channel": "sms", "lang": "hi"}).status_code == 409  # no key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    state["ai_transport"] = httpx.MockTransport(handler)
    t = c.post("/templates/generate", json={"channel": "sms", "lang": "hi"}).json()
    assert t["source"] == "ai" and t["problems"] == [] and t["status"] == "draft"
    sent = seen[0].content.decode()
    assert seen[0].headers["x-api-key"] == "test-key" and "Hindi" in sent
    assert not any(n in sent for n in names)  # no student names ever leave the computer

    def bad(req):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Pay Rs 5000 by 10 Nov {children}"}]})
    state["ai_transport"] = httpx.MockTransport(bad)
    r = c.post("/templates/generate", json={"channel": "sms", "lang": "hi"})
    assert r.status_code == 409 and "safety checks" in r.json()["detail"]


def test_edit_and_approve_rules(app_client):
    c, _ = app_client
    t = c.post("/templates/generate", json={"channel": "whatsapp", "lang": "mr", "use_builtin": True}).json()
    e = c.put(f"/templates/{t['id']}", json={"body": "Hello {children} please pay"}).json()
    assert e["problems"]
    assert c.post(f"/templates/{t['id']}/approve", json={}).status_code == 422  # invalid wording can't be approved
    c.put(f"/templates/{t['id']}", json={"body": ai.BUILTIN["mr"]})
    a = c.post(f"/templates/{t['id']}/approve", json={"provider_template": "fee_reminder_mr", "by": "Office"}).json()
    assert a["status"] == "approved" and a["provider_template"] == "fee_reminder_mr"
    assert c.put(f"/templates/{t['id']}", json={"body": ai.BUILTIN["mr"]}).status_code == 409
    b = approve_builtin(c, "whatsapp", "mr")  # a newer approval retires the old one
    assert b["status"] == "approved"
    lst = {x["id"]: x["status"] for x in c.get("/templates").json()}
    assert t["id"] not in lst and lst[b["id"]] == "approved"


# ------------------------------------------------------------------ send all
def test_send_all_needs_confirmation_and_never_duplicates(app_client, conn):
    c, _ = app_client
    p = c.post("/send/preview", json={"channels": ["whatsapp"], "lang": "en"}).json()
    assert p["count"] == 0  # nobody enabled yet
    c.post("/reminders/enable", json={"enabled": True, "std": "1"})
    p = c.post("/send/preview", json={"channels": ["whatsapp", "sms"], "lang": "en"}).json()
    assert p["count"] == 0 and all("No approved" in s["why"] for s in p["skipped"])
    approve_builtin(c, "whatsapp")
    approve_builtin(c, "sms")
    p = c.post("/send/preview", json={"channels": ["whatsapp", "sms"], "lang": "en"}).json()
    fams = due_families(conn)
    with_phone = fams - sum(1 for s in p["skipped"] if s["channel"] == "whatsapp")
    assert p["simulated"] is True and p["by_channel"]["whatsapp"] == with_phone and p["count"] == 2 * with_phone
    assert "{" not in p["sample"] and "Synthetic Test School" in p["sample"]
    wrong = c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"] + 1})
    assert wrong.status_code == 409 and "type the number" in wrong.json()["detail"]
    # the wrong count used up the token: a fresh check is required
    assert c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"]}).status_code == 409
    p = c.post("/send/preview", json={"channels": ["whatsapp", "sms"], "lang": "en"}).json()
    r = c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"], "by": "Office"}).json()
    assert r["simulated"] and r["summary"] == {"simulated": p["count"]}
    out = conn.execute("SELECT * FROM outbound_messages").fetchall()
    assert len(out) == p["count"] and all(o["provider"] == "simulated" for o in out)
    assert conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 0
    # same day again: everything skipped, nothing new written
    again = c.post("/send/preview", json={"channels": ["whatsapp", "sms"], "lang": "en"}).json()
    assert again["count"] == 0 and all("Already sent" in s["why"] or "No valid" in s["why"] for s in again["skipped"])
    # a stale preview made before the send can't double-send either (database-enforced)
    assert conn.execute("SELECT count(*) AS n FROM outbound_messages").fetchone()["n"] == p["count"]
    lst = c.get("/outbound").json()
    assert lst["total"] == p["count"] and lst["today"] == {"simulated": p["count"]}


def test_stale_preview_cannot_double_send(app_client, conn):
    c, _ = app_client
    c.post("/reminders/enable", json={"enabled": True, "std": "10"})
    approve_builtin(c, "sms")
    p1 = c.post("/send/preview", json={"channels": ["sms"], "lang": "en"}).json()
    p2 = c.post("/send/preview", json={"channels": ["sms"], "lang": "en"}).json()
    c.post("/send/confirm", json={"token": p1["token"], "confirm_count": p1["count"]})
    r = c.post("/send/confirm", json={"token": p2["token"], "confirm_count": p2["count"]}).json()
    assert r["duplicates_prevented"] == p2["count"] and r["summary"] == {"skipped": p2["count"]}
    assert conn.execute("SELECT count(*) AS n FROM outbound_messages").fetchone()["n"] == p1["count"]


def test_paid_or_disabled_after_preview_is_skipped(app_client, conn):
    c, _ = app_client
    c.post("/reminders/enable", json={"enabled": True, "std": "10"})
    approve_builtin(c, "sms")
    p = c.post("/send/preview", json={"channels": ["sms"], "lang": "en"}).json()
    c.post("/reminders/enable", json={"enabled": False})
    r = c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"]}).json()
    assert r["summary"] == {"skipped": p["count"]}
    assert conn.execute("SELECT count(*) AS n FROM outbound_messages").fetchone()["n"] == 0


# ------------------------------------------------------------------ provider safety gates
def test_real_providers_refused_unless_every_switch_is_on(cfg, monkeypatch):
    real = replace(cfg, test_mode=False)
    with pytest.raises(pv.NotConfigured, match="switched off"):
        pv.get_provider(real, "whatsapp")
    real = replace(real, messaging_enabled=True)
    with pytest.raises(pv.NotConfigured, match="mapping"):
        pv.get_provider(real, "whatsapp")
    real = replace(real, mapping_confirmed=True, raw={"whatsapp": {"enabled": True, "phone_number_id": "123"},
                                                     "sms": {"enabled": True, "sender_id": "SCHOOL"}})
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MSG91_AUTHKEY", raising=False)
    with pytest.raises(pv.NotConfigured, match="WHATSAPP_ACCESS_TOKEN"):
        pv.get_provider(real, "whatsapp")
    with pytest.raises(pv.NotConfigured, match="MSG91_AUTHKEY"):
        pv.get_provider(real, "sms")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    assert isinstance(pv.get_provider(real, "whatsapp"), pv.WhatsAppCloud)


def test_whatsapp_payload_and_errors(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    calls = []

    def handler(req):
        calls.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"display_phone_number": "+91 90000 00000", "verified_name": "Test"})
        body = json.loads(req.content)
        if body["to"] == "919999999999":
            return httpx.Response(400, json={"error": {"message": "Template not approved", "code": 132001}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    wa = pv.WhatsAppCloud({"phone_number_id": "555"}, transport=httpx.MockTransport(handler))
    assert wa.verify()["verified_name"] == "Test"
    tpl = {"provider_template": "fee_reminder", "lang": "mr", "provider_lang": "mr"}
    res = wa.send("98765 43210", tpl, ["School", "A (1)\nB (2)", "₹ 1,000"])
    assert res.message_id == "wamid.ABC" and res.status == "accepted"
    body = json.loads(calls[-1].content)
    assert calls[-1].url.path == "/v23.0/555/messages" and calls[-1].headers["authorization"] == "Bearer tok"
    assert body["to"] == "919876543210" and body["template"]["name"] == "fee_reminder"
    assert body["template"]["language"]["code"] == "mr"
    assert [p["text"] for p in body["template"]["components"][0]["parameters"]] == ["School", "A (1); B (2)", "₹ 1,000"]
    with pytest.raises(pv.ProviderError, match="132001"):
        wa.send("9999999999", tpl, ["a", "b", "c"])
    with pytest.raises(pv.ProviderError, match="valid Indian mobile"):
        wa.send("12345", tpl, [])


def test_msg91_payload(monkeypatch):
    monkeypatch.setenv("MSG91_AUTHKEY", "key")
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"type": "success", "message": "req-123"})

    sms = pv.Msg91Sms({"sender_id": "SCHOOL"}, transport=httpx.MockTransport(handler))
    res = sms.send("+91 98765 43210", {"provider_template": "tmpl-1", "lang": "hi"}, ["S", "A", "₹ 5"])
    body = json.loads(calls[0].content)
    assert res.message_id == "req-123" and calls[0].headers["authkey"] == "key"
    assert body["template_id"] == "tmpl-1" and body["sender"] == "SCHOOL" and body["unicode"] == "1"
    assert body["recipients"] == [{"mobiles": "919876543210", "VAR1": "S", "VAR2": "A", "VAR3": "₹ 5"}]

    def refuse(req):
        return httpx.Response(200, json={"type": "error", "message": "Invalid template"})
    with pytest.raises(pv.ProviderError, match="Invalid template"):
        pv.Msg91Sms({"sender_id": "SCHOOL"}, transport=httpx.MockTransport(refuse)).send(
            "9876543210", {"provider_template": "x", "lang": "en"}, [])


class FakeProvider:
    name = "fake"

    def __init__(self, fail_phones=()):
        self.n, self.fail = 0, set(fail_phones)

    def send(self, to, template, variables):
        if to in self.fail:
            raise pv.ProviderError("Number not on WhatsApp")
        self.n += 1
        return pv.SendResult("fake", f"wamid.{self.n}", "accepted")


def test_failures_retry_and_webhook_status(app_client, conn, monkeypatch):
    c, state = app_client
    c.post("/reminders/enable", json={"enabled": True, "std": "1", "medium": "English"})
    approve_builtin(c, "whatsapp")
    p = c.post("/send/preview", json={"channels": ["whatsapp"], "lang": "en"}).json()
    fake = FakeProvider()
    state["providers"] = {"whatsapp": fake}
    items = conn.execute("SELECT value FROM settings WHERE key LIKE 'send_preview:%%'").fetchone()["value"]["items"]
    fake.fail = {items[0]["phone"]}
    r = c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"]}).json()
    assert r["summary"]["failed"] == 1 and r["summary"]["accepted"] == p["count"] - 1
    failed = conn.execute("SELECT * FROM outbound_messages WHERE status='failed'").fetchone()
    assert "not on WhatsApp" in failed["error"]
    fake.fail = set()
    rr = c.post(f"/outbound/{failed['id']}/retry").json()
    assert rr["status"] == "accepted"
    assert c.post(f"/outbound/{failed['id']}/retry").status_code == 409  # already sent again today
    ok = conn.execute("SELECT * FROM outbound_messages WHERE status='accepted' ORDER BY id LIMIT 1").fetchone()

    # WhatsApp webhook: signature required, status only moves forward
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")

    def hook(status):
        body = json.dumps({"entry": [{"changes": [{"value": {"statuses": [
            {"id": ok["provider_message_id"], "status": status}]}}]}]}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        return c.post("/webhooks/whatsapp", content=body, headers={"x-hub-signature-256": sig,
                                                                     "content-type": "application/json"})

    assert c.post("/webhooks/whatsapp", content=b"{}", headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
    assert hook("read").json()["updated"] == 1
    hook("delivered")  # arrives late: ignored
    assert conn.execute("SELECT status FROM outbound_messages WHERE id=%s", (ok["id"],)).fetchone()["status"] == "read"
    ev = [e["status"] for e in conn.execute("SELECT status FROM message_events WHERE outbound_id=%s ORDER BY id", (ok["id"],))]
    assert ev == ["queued", "accepted", "read", "delivered"]

    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    v = c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert v.text == "42"
    assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403


def test_msg91_webhook(app_client, conn, monkeypatch):
    c, state = app_client
    c.post("/reminders/enable", json={"enabled": True, "std": "10", "medium": "English"})
    approve_builtin(c, "sms")
    fake = FakeProvider()
    state["providers"] = {"sms": fake}
    p = c.post("/send/preview", json={"channels": ["sms"], "lang": "en"}).json()
    c.post("/send/confirm", json={"token": p["token"], "confirm_count": p["count"]})
    a, b = conn.execute("SELECT * FROM outbound_messages ORDER BY id LIMIT 2").fetchall()
    assert c.post("/webhooks/msg91", json={}).status_code == 401
    monkeypatch.setenv("MSG91_WEBHOOK_TOKEN", "tk")
    r = c.post("/webhooks/msg91", params={"token": "tk"}, json=[
        {"requestId": a["provider_message_id"], "telNum": "91x", "status": "1"},
        {"requestId": b["provider_message_id"], "telNum": "91y", "status": "9", "failureReason": "NDNC"}]).json()
    assert r["updated"] == 2
    st = {o["id"]: (o["status"], o["error"]) for o in conn.execute("SELECT id, status, error FROM outbound_messages")}
    assert st[a["id"]] == ("delivered", None) and st[b["id"]] == ("failed", "NDNC")


def test_verify_in_test_mode_is_not_live(app_client, conn):
    c, _ = app_client
    r = c.post("/reminders/verify/whatsapp").json()
    assert r["simulated"] and "Test mode" in r["note"]
    assert c.get("/reminders/status").json()["whatsapp"]["verified"] is None
