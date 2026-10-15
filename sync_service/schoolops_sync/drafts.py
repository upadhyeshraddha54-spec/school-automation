"""Reminder drafts in English / Hindi / Marathi.

Drafts use only values that exist in the database (name, class, balance from Excel).
A child without a verified amount, or with nothing due, is left out and listed in
`left_out`. Staff can edit, approve or discard a draft. Nothing is ever sent from here:
sending needs a provider and `[messaging] enabled = true`, and no provider is built in.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from .messages import LANGS, render, rupees

MULTI = {
    "en": ("Dear Parent, this is a reminder from {school}. Fees are pending for:\n{lines}\n"
           "Total pending: {total}. Please pay at the school office at the earliest. "
           "If already paid, please ignore this message. Thank you."),
    "hi": ("आदरणीय पालक, {school} की ओर से सूचना। इन विद्यार्थियों की फीस बकाया है:\n{lines}\n"
           "कुल बकाया: {total}। कृपया जल्द से जल्द स्कूल कार्यालय में भुगतान करें। "
           "यदि भुगतान हो चुका है, तो कृपया इस संदेश को अनदेखा करें। धन्यवाद।"),
    "mr": ("आदरणीय पालक, {school} कडून सूचना. खालील विद्यार्थ्यांची फी बाकी आहे:\n{lines}\n"
           "एकूण बाकी: {total}. कृपया लवकरात लवकर शाळेच्या कार्यालयात फी भरावी. "
           "फी भरली असल्यास या संदेशाकडे दुर्लक्ष करावे. धन्यवाद."),
}


class DraftError(Exception):
    pass


def _students(conn, ids: list[int]) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.name, s.family_id, s.contact,
                  s.medium || ' ' || s.std || CASE WHEN s.section <> '' THEN '-' || s.section ELSE '' END AS klass,
                  f.balance
           FROM students s LEFT JOIN current_fees f ON f.student_id = s.id WHERE s.id = ANY(%s) ORDER BY s.std, s.id""",
        (ids,)).fetchall()


def compose(conn, student_ids: list[int], lang: str, school: str) -> dict:
    if lang not in LANGS:
        raise DraftError(f"Language must be one of {list(LANGS)}.")
    ids = list(dict.fromkeys(int(i) for i in student_ids))
    studs = _students(conn, ids)
    if len(studs) != len(ids) or not ids:
        raise DraftError("Student not found.")
    if len({s["family_id"] for s in studs}) > 1:
        raise DraftError("A combined message can only include children of the same parent.")
    include, left_out = [], []
    for s in studs:
        if s["balance"] is None:
            left_out.append({"id": s["id"], "name": s["name"], "why": "No fee amount from Excel yet"})
        elif s["balance"] <= 0:
            left_out.append({"id": s["id"], "name": s["name"], "why": "Nothing due"})
        else:
            include.append(s)
    if not include:
        raise DraftError("None of these children has a pending amount in Excel, so there is nothing to remind about.")
    if len(include) == 1:
        s = include[0]
        body = render(lang, school=school, name=s["name"], klass=s["klass"], amount=s["balance"])
    else:
        lines = "\n".join(f"- {s['name']} ({s['klass']}): {rupees(s['balance'])}" for s in include)
        body = MULTI[lang].format(school=school, lines=lines, total=rupees(sum(s["balance"] for s in include)))
    return {"body": body, "student_ids": [s["id"] for s in include], "family_id": include[0]["family_id"],
            "left_out": left_out, "no_phone": not any(s["contact"] for s in include)}


def create(conn, student_ids: list[int], lang: str, school: str) -> dict:
    d = compose(conn, student_ids, lang, school)
    row = conn.execute(
        """INSERT INTO message_drafts (family_id, student_ids, lang, body, left_out)
           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
        (d["family_id"], d["student_ids"], lang, d["body"], Jsonb(d["left_out"]))).fetchone()
    row["no_phone"] = d["no_phone"]
    return row


def _get(conn, draft_id: int) -> dict:
    r = conn.execute("SELECT * FROM message_drafts WHERE id=%s", (draft_id,)).fetchone()
    if not r:
        raise DraftError("Draft not found.")
    return r


def edit(conn, draft_id: int, body: str) -> dict:
    d = _get(conn, draft_id)
    if d["status"] == "discarded":
        raise DraftError("This draft was discarded.")
    body = (body or "").strip()
    if not body:
        raise DraftError("The message cannot be empty.")
    # editing an approved draft takes it back to "draft": it must be approved again
    return conn.execute(
        """UPDATE message_drafts SET body=%s, edited=TRUE, status='draft', approved_at=NULL, approved_by=NULL,
                  updated_at=now() WHERE id=%s RETURNING *""", (body, draft_id)).fetchone()


def approve(conn, draft_id: int, by: str | None) -> dict:
    d = _get(conn, draft_id)
    if d["status"] != "draft":
        raise DraftError(f"Only a draft can be approved (this one is {d['status']}).")
    return conn.execute(
        """UPDATE message_drafts SET status='approved', approved_at=now(), approved_by=%s, updated_at=now()
           WHERE id=%s RETURNING *""", (by, draft_id)).fetchone()


def discard(conn, draft_id: int) -> dict:
    _get(conn, draft_id)
    return conn.execute("UPDATE message_drafts SET status='discarded', updated_at=now() WHERE id=%s RETURNING *",
                        (draft_id,)).fetchone()
