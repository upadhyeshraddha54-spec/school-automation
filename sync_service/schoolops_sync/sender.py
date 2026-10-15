"""Teacher-controlled automatic reminders: enable/disable, preview, confirmed Send All, status tracking.

* Only students the teacher enabled AND who owe fees in Excel are included.
* One message per parent per channel per day (database-enforced); siblings are combined.
* Send All needs a fresh preview token AND the exact number of messages typed back (confirmation).
* Status only moves forward (accepted -> sent -> delivered -> read); every update is logged.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from . import ai
from .messages import rupees
from .outreach import _phone
from .providers import NotConfigured, ProviderError, get_provider

log = logging.getLogger("schoolops.sender")
RANK = {"queued": 0, "sending": 1, "accepted": 2, "simulated": 2, "sent": 3, "delivered": 4, "read": 5}
PREVIEW_MINUTES = 10


class SendError(Exception):
    pass


def _filters(filters: dict | None) -> tuple[list[str], dict]:
    where, p = ["s.missing_since IS NULL"], {}
    for col in ("medium", "std", "section"):
        if (filters or {}).get(col):
            where.append(f"s.{col} = %({col})s")
            p[col] = filters[col]
    if (filters or {}).get("search"):
        where.append("(s.name ILIKE %(q)s OR s.contact ILIKE %(q)s)")
        p["q"] = f"%{filters['search'].strip()}%"
    return where, p


def set_enabled(conn, enabled: bool, by: str | None, *, student_ids: list[int] | None = None,
                filters: dict | None = None) -> int:
    """Enable/disable one or many students. With no ids, applies to every student matching the filters."""
    if student_ids is not None:
        rows = conn.execute("""UPDATE students SET reminders_enabled=%s, reminders_changed_at=now(), reminders_changed_by=%s
                               WHERE id = ANY(%s) RETURNING id""", (enabled, by, student_ids)).fetchall()
        return len(rows)
    where, p = _filters(filters)
    rows = conn.execute(f"""UPDATE students s SET reminders_enabled=%(en)s, reminders_changed_at=now(), reminders_changed_by=%(by)s
                            WHERE {' AND '.join(where)} RETURNING s.id""", {**p, "en": enabled, "by": by}).fetchall()
    return len(rows)


def approved_template(conn, channel: str, lang: str) -> dict | None:
    return conn.execute("SELECT * FROM message_templates WHERE channel=%s AND lang=%s AND status='approved'",
                        (channel, lang)).fetchone()


def _children_text(kids) -> str:
    return "; ".join(f"{k['name']} ({k['klass']}): {rupees(k['balance'])}" for k in kids)


def plan(conn, cfg, channels: list[str], lang: str, filters: dict | None = None) -> dict:
    where, p = _filters(filters)
    rows = conn.execute(
        f"""SELECT s.id, s.name, s.contact, s.family_id,
                   s.medium || ' ' || s.std || CASE WHEN s.section <> '' THEN '-' || s.section ELSE '' END AS klass,
                   f.balance FROM students s JOIN current_fees f ON f.student_id = s.id
            WHERE s.reminders_enabled AND f.balance > 0 AND {' AND '.join(where)} ORDER BY s.family_id, s.id""", p).fetchall()
    fams: dict[int, list[dict]] = {}
    for r in rows:
        fams.setdefault(r["family_id"], []).append(r)
    ready, skipped = [], []
    provider_problem = {}
    for ch in channels:
        try:
            get_provider(cfg, ch)
        except NotConfigured as exc:
            provider_problem[ch] = str(exc)
    templates = {ch: approved_template(conn, ch, lang) for ch in channels}
    sent_today = {(r["family_id"], r["channel"]) for r in conn.execute(
        "SELECT family_id, channel FROM outbound_messages WHERE created_on = current_date AND status NOT IN ('failed','cancelled')")}
    for fid, kids in fams.items():
        family_phones = conn.execute("SELECT contact FROM students WHERE family_id=%s", (fid,)).fetchall()
        phone = _phone(kids) or _phone(family_phones)
        label = " & ".join(k["name"] for k in kids)
        for ch in channels:
            why = None
            if ch in provider_problem:
                why = provider_problem[ch]
            elif not templates[ch]:
                why = f"No approved {ch} wording for this language yet"
            elif not phone:
                why = "No valid mobile number in Excel"
            elif (fid, ch) in sent_today:
                why = f"Already sent by {ch} today"
            if why:
                skipped.append({"family_id": fid, "name": label, "channel": ch, "why": why})
                continue
            values = {"children": _children_text(kids), "total": rupees(sum(k["balance"] for k in kids)),
                      "school": cfg.school_name}
            t = templates[ch]
            ready.append({"family_id": fid, "student_ids": [k["id"] for k in kids], "name": label, "channel": ch,
                          "phone": phone, "template_id": t["id"], "variables": ai.variables(t["body"], values),
                          "preview": ai.render(t["body"], **values)})
    return {"ready": ready, "skipped": skipped, "provider_problems": provider_problem}


def preview(conn, cfg, channels: list[str], lang: str, filters: dict | None = None) -> dict:
    p = plan(conn, cfg, channels, lang, filters)
    token = uuid.uuid4().hex
    expires = datetime.now(timezone.utc) + timedelta(minutes=PREVIEW_MINUTES)
    conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s)",
                 (f"send_preview:{token}", Jsonb({"items": p["ready"], "lang": lang, "expires": expires.isoformat()})))
    by_ch = {ch: sum(1 for x in p["ready"] if x["channel"] == ch) for ch in channels}
    return {"token": token, "count": len(p["ready"]), "by_channel": by_ch, "skipped": p["skipped"],
            "sample": p["ready"][0]["preview"] if p["ready"] else None, "simulated": bool(cfg.test_mode),
            "expires": expires.isoformat(), "provider_problems": p["provider_problems"]}


def _event(conn, oid, status, detail=None, raw=None):
    conn.execute("INSERT INTO message_events (outbound_id, status, detail, raw) VALUES (%s,%s,%s,%s)",
                 (oid, status, detail, Jsonb(raw) if raw is not None else None))


def _send_row(conn, cfg, row: dict, providers: dict) -> dict:
    conn.execute("UPDATE outbound_messages SET status='sending', updated_at=now() WHERE id=%s", (row["id"],))
    tpl = conn.execute("SELECT * FROM message_templates WHERE id=%s", (row["template_id"],)).fetchone()
    try:
        prov = providers.get(row["channel"]) or providers.setdefault(row["channel"], get_provider(cfg, row["channel"]))
        res = prov.send(row["phone"], tpl, row["variables"])
        conn.execute("""UPDATE outbound_messages SET status=%s, provider=%s, provider_message_id=%s, updated_at=now()
                        WHERE id=%s""", (res.status, res.provider, res.message_id, row["id"]))
        _event(conn, row["id"], res.status, res.message_id)
        return {"id": row["id"], "status": res.status}
    except (ProviderError, Exception) as exc:  # one failure never stops the batch
        msg = str(exc)[:300]
        conn.execute("UPDATE outbound_messages SET status='failed', error=%s, updated_at=now() WHERE id=%s", (msg, row["id"]))
        _event(conn, row["id"], "failed", msg)
        log.warning("Send failed for outbound %s: %s", row["id"], msg)
        return {"id": row["id"], "status": "failed", "error": msg}


def confirm(conn, cfg, token: str, confirm_count: int, by: str | None, *, providers: dict | None = None,
            sleep=time.sleep) -> dict:
    """Send everything in a preview. The teacher must type the number of messages to confirm."""
    rec = conn.execute("DELETE FROM settings WHERE key=%s RETURNING value", (f"send_preview:{token}",)).fetchone()
    if not rec:
        raise SendError("This preview has expired or was already used. Check again before sending.")
    pv = rec["value"]
    if datetime.fromisoformat(pv["expires"]) < datetime.now(timezone.utc):
        raise SendError("This preview is older than 10 minutes. Check again before sending.")
    if int(confirm_count) != len(pv["items"]):
        raise SendError(f"Please type the number of messages ({len(pv['items'])}) to confirm.")
    providers = providers if providers is not None else {}
    delay = 0 if cfg.test_mode else float(cfg.messaging.get("send_delay_seconds", 1.0))
    batch = uuid.uuid4().hex[:12]
    results, duplicates = [], 0
    for it in pv["items"]:
        still_due = conn.execute("""SELECT count(*) AS n FROM students s JOIN current_fees f ON f.student_id=s.id
                                    WHERE s.id = ANY(%s) AND s.reminders_enabled AND f.balance > 0""",
                                 (it["student_ids"],)).fetchone()["n"]
        if not still_due:
            results.append({"family_id": it["family_id"], "status": "skipped", "why": "No longer due or disabled"})
            continue
        row = conn.execute(
            """INSERT INTO outbound_messages (batch_id, family_id, student_ids, channel, phone, lang, template_id,
                                              variables, preview, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (family_id, channel, created_on) WHERE status NOT IN ('failed', 'cancelled')
               DO NOTHING RETURNING *""",
            (batch, it["family_id"], it["student_ids"], it["channel"], it["phone"], pv["lang"], it["template_id"],
             Jsonb(it["variables"]), it["preview"], by)).fetchone()
        if not row:
            duplicates += 1
            results.append({"family_id": it["family_id"], "status": "skipped", "why": "Already sent today"})
            continue
        _event(conn, row["id"], "queued", by)
        results.append({"family_id": it["family_id"], **_send_row(conn, cfg, row, providers)})
        if delay:
            sleep(delay)
    count = {s: sum(1 for r in results if r["status"] == s) for s in {r["status"] for r in results}}
    return {"batch_id": batch, "results": results, "summary": count, "duplicates_prevented": duplicates,
            "simulated": bool(cfg.test_mode)}


def retry(conn, cfg, outbound_id: int, by: str | None, *, providers: dict | None = None) -> dict:
    old = conn.execute("SELECT * FROM outbound_messages WHERE id=%s", (outbound_id,)).fetchone()
    if not old or old["status"] != "failed":
        raise SendError("Only a failed message can be sent again.")
    row = conn.execute(
        """INSERT INTO outbound_messages (batch_id, family_id, student_ids, channel, phone, lang, template_id, variables,
                                          preview, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (family_id, channel, created_on) WHERE status NOT IN ('failed', 'cancelled')
           DO NOTHING RETURNING *""",
        (old["batch_id"], old["family_id"], old["student_ids"], old["channel"], old["phone"], old["lang"],
         old["template_id"], Jsonb(old["variables"]), old["preview"], by)).fetchone()
    if not row:
        raise SendError("A message to this parent on this channel was already sent today.")
    _event(conn, row["id"], "queued", f"retry of {outbound_id} by {by}")
    return _send_row(conn, cfg, row, providers if providers is not None else {})


def apply_status(conn, provider_message_id: str, status: str, error: str | None, raw) -> bool:
    """Delivery/read/failed updates from the provider webhooks. Never goes backwards."""
    row = conn.execute("SELECT id, status FROM outbound_messages WHERE provider_message_id=%s",
                       (provider_message_id,)).fetchone()
    if not row:
        return False
    _event(conn, row["id"], status, error, raw)
    cur = row["status"]
    if status == "failed":
        if RANK.get(cur, 0) < RANK["delivered"]:
            conn.execute("UPDATE outbound_messages SET status='failed', error=%s, updated_at=now() WHERE id=%s",
                         (error or "Failed (provider)", row["id"]))
    elif RANK.get(status, 0) > RANK.get(cur, 0) and cur != "failed":
        conn.execute("UPDATE outbound_messages SET status=%s, updated_at=now() WHERE id=%s", (status, row["id"]))
    return True
