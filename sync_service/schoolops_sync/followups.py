"""Call outcomes -> next follow-up, payment detection from Excel, and the daily parent queue.

* Each child has at most one OPEN follow-up (database-enforced). A new call closes the old one
  (as "done", or "missed" if it was a promise whose date had passed) and opens the next one.
* Payments are only ever learned from Excel. When a child's balance reaches 0, their open
  follow-up closes as "paid"; when it drops but is not 0, the queue shows "part paid".
* The queue is per parent/family: one call can cover several siblings.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class Outcome:
    label: str
    kind: str | None          # follow-up created
    days: int | None          # default due in N days (None = staff must choose a date)
    date_required: bool = False
    in_queue: bool = True     # does the follow-up come back in the calling queue?


# ids are stable (older records use them); labels are what staff see
OUTCOMES: dict[str, Outcome] = {
    "will_pay": Outcome("Will pay", "callback", 3),
    "gave_date": Outcome("Will pay on date", "promise", None, date_required=True),
    "part_payment": Outcome("Part payment", "part_payment", 7),
    "call_later": Outcome("Call again", "callback", 1),
    "no_answer": Outcome("No answer", "retry", 1),
    "phone_off": Outcome("Phone off", "retry", 1),
    "wrong_number": Outcome("Wrong number", "fix_number", 0, in_queue=False),
    "already_paid": Outcome("Says already paid", "verify_payment", 0, in_queue=False),
    "complaint": Outcome("Fee issue", "dispute", 0, in_queue=False),
}
FOLLOW_UP_LABELS = {
    "promise": "Promised to pay", "part_payment": "Part payment promised", "callback": "Call back",
    "retry": "Try again (no answer)", "fix_number": "Fix phone number in Excel",
    "verify_payment": "Check payment with office", "dispute": "Resolve fee issue",
}
ACTION_KINDS = ("fix_number", "verify_payment", "dispute")  # need office action, not another call
PRIORITY = {"missed_promise": 1, "due_today": 2, "not_called": 3, "recontact": 4}
PRIORITY_LABEL = {"missed_promise": "Missed promise", "due_today": "Due today",
                  "not_called": "Not called yet", "recontact": "Call again (no reply for a week)"}
RECONTACT_DAYS = 7


class CallError(Exception):
    pass


def _today(conn) -> date:
    return conn.execute("SELECT current_date AS d").fetchone()["d"]


def record_call(conn, student_ids: list[int], outcome: str, *, follow_up_date: date | None = None,
                note: str | None = None, caller: str | None = None, promised_amount: Decimal | None = None) -> dict:
    """Save one call (covering one or more siblings) and schedule each child's next follow-up."""
    if outcome not in OUTCOMES:
        raise CallError(f"Unknown result '{outcome}'.")
    o = OUTCOMES[outcome]
    ids = list(dict.fromkeys(int(i) for i in student_ids))
    if not ids:
        raise CallError("Choose at least one student.")
    today = _today(conn)
    if o.date_required and not follow_up_date:
        raise CallError("Please choose the date the parent will pay.")
    if follow_up_date and follow_up_date < today:
        raise CallError("The follow-up date cannot be in the past.")
    if promised_amount is not None and promised_amount <= 0:
        raise CallError("The amount must be more than zero.")
    studs = conn.execute(
        """SELECT s.id, s.family_id, f.balance FROM students s LEFT JOIN current_fees f ON f.student_id=s.id
           WHERE s.id = ANY(%s)""", (ids,)).fetchall()
    if len(studs) != len(ids):
        raise CallError("Student not found.")
    if len({s["family_id"] for s in studs}) > 1:
        raise CallError("One call can only cover children of the same parent. Link them as siblings first.")
    due = follow_up_date or (today + timedelta(days=o.days) if o.days is not None else None)
    group = uuid.uuid4().hex
    calls, fus = [], []
    with conn.transaction():
        for s in sorted(studs, key=lambda x: ids.index(x["id"])):
            call = conn.execute(
                """INSERT INTO calls (student_id, outcome, follow_up_date, note, caller, call_group, promised_amount)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (s["id"], outcome, follow_up_date, note, caller, group, promised_amount)).fetchone()
            calls.append(call)
            conn.execute(
                """UPDATE follow_ups SET status = CASE WHEN kind IN ('promise','part_payment') AND due_date < %s
                                                       THEN 'missed' ELSE 'done' END,
                          closed_at = now(), closed_by_call_id = %s, close_reason = %s
                   WHERE student_id = %s AND status = 'open'""",
                (today, call["id"], f"Called: {o.label}", s["id"]))
            if o.kind:
                fus.append(conn.execute(
                    """INSERT INTO follow_ups (student_id, kind, due_date, source_call_id, balance_at_open,
                                               promised_amount, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (s["id"], o.kind, due, call["id"], s["balance"], promised_amount, note)).fetchone())
    return {"group": group, "calls": calls, "follow_ups": fus, "id": calls[0]["id"]}


def undo_call(conn, call_id: int) -> list[int]:
    """Undo a mis-tap (within 10 minutes): removes the whole call (all siblings) and restores
    the follow-ups it replaced."""
    row = conn.execute("SELECT call_group FROM calls WHERE id=%s AND called_at > now() - interval '10 minutes'",
                       (call_id,)).fetchone()
    if not row:
        raise CallError("Only a call saved in the last 10 minutes can be undone.")
    with conn.transaction():
        ids = [r["id"] for r in conn.execute("SELECT id FROM calls WHERE call_group=%s", (row["call_group"],))]
        conn.execute("DELETE FROM follow_ups WHERE source_call_id = ANY(%s)", (ids,))
        conn.execute("""UPDATE follow_ups SET status='open', closed_at=NULL, closed_by_call_id=NULL, close_reason=NULL
                        WHERE closed_by_call_id = ANY(%s)""", (ids,))
        conn.execute("DELETE FROM calls WHERE id = ANY(%s)", (ids,))
    return ids


def close_follow_up(conn, fu_id: int, reason: str, by: str | None = None) -> None:
    r = conn.execute("""UPDATE follow_ups SET status='done', closed_at=now(), close_reason=%s
                        WHERE id=%s AND status='open' RETURNING id""",
                     (f"{reason}{' (' + by + ')' if by else ''}", fu_id)).fetchone()
    if not r:
        raise CallError("This follow-up is already closed.")


def refresh_follow_ups(conn) -> int:
    """Close follow-ups whose child now has no balance in Excel."""
    rows = conn.execute(
        """UPDATE follow_ups fu SET status='paid', closed_at=now(), close_reason='Balance is 0 in Excel'
           FROM current_fees f
           WHERE f.student_id = fu.student_id AND fu.status='open' AND f.balance <= 0
             AND fu.kind <> 'dispute'
           RETURNING fu.id""").fetchall()
    return len(rows)


STUDENT_STATE_SQL = """
SELECT s.id, s.name, s.contact, s.family_id, s.medium, s.std, s.section, s.sr_no, s.parent_name,
       s.medium || ' ' || s.std || CASE WHEN s.section <> '' THEN '-' || s.section ELSE '' END AS klass,
       f.balance, f.total_fee, f.paid, f.captured_at AS fee_as_of,
       fu.id AS fu_id, fu.kind AS fu_kind, fu.due_date AS fu_due, fu.balance_at_open, fu.promised_amount,
       fu.note AS fu_note, fu.created_at AS fu_created,
       lc.called_at AS last_call_at, lc.outcome AS last_outcome,
       (SELECT count(*) FROM calls c WHERE c.student_id = s.id) AS n_calls,
       EXISTS (SELECT 1 FROM calls c WHERE c.student_id = s.id AND c.called_at::date = current_date) AS called_today
FROM students s
JOIN current_fees f ON f.student_id = s.id
LEFT JOIN follow_ups fu ON fu.student_id = s.id AND fu.status = 'open'
LEFT JOIN LATERAL (SELECT called_at, outcome FROM calls c WHERE c.student_id = s.id
                   ORDER BY called_at DESC LIMIT 1) lc ON TRUE
WHERE s.missing_since IS NULL AND f.balance > 0
"""


def _child_state(s: dict, today: date) -> str:
    if s["fu_kind"] in ACTION_KINDS:  # office must act first (even if called today)
        return "needs_action"
    if s["called_today"]:
        return "done_today"
    if s["fu_kind"]:
        if s["fu_due"] is None or s["fu_due"] > today:
            return "waiting"
        if s["fu_kind"] in ("promise", "part_payment") and s["fu_due"] < today:
            return "missed_promise"
        return "due_today"
    if not s["n_calls"]:
        return "not_called"
    if s["last_call_at"] and (today - s["last_call_at"].date()).days >= RECONTACT_DAYS:
        return "recontact"
    return "waiting"


def _decorate(s: dict, state: str) -> dict:
    s = dict(s)
    s["state"] = state
    s["follow_up_label"] = FOLLOW_UP_LABELS.get(s["fu_kind"]) if s["fu_kind"] else None
    s["last_outcome_label"] = OUTCOMES[s["last_outcome"]].label if s["last_outcome"] in OUTCOMES else s["last_outcome"]
    paid_since = None
    if s["balance_at_open"] is not None and s["balance"] is not None and s["balance"] < s["balance_at_open"]:
        paid_since = s["balance_at_open"] - s["balance"]
    s["paid_since_follow_up"] = paid_since  # part payment seen in Excel since the follow-up started
    return s


def campaign_filter(campaign: dict | None) -> dict:
    return {k: v for k, v in ((campaign or {}).get("filters") or {}).items() if v}


def build_queue(conn, *, medium=None, std=None, section=None, search=None, campaign: dict | None = None) -> dict:
    """Families to call today, most urgent first. One entry per parent; siblings together."""
    today = _today(conn)
    flt = {**campaign_filter(campaign), **{k: v for k, v in (("medium", medium), ("std", std), ("section", section)) if v}}
    rows = conn.execute(STUDENT_STATE_SQL + " ORDER BY s.id").fetchall()
    fams: dict[int, list[dict]] = {}
    for r in rows:
        fams.setdefault(r["family_id"], []).append(_decorate(r, _child_state(r, today)))
    names = {f["id"]: f for f in conn.execute("SELECT id, display_name, verified FROM families WHERE id = ANY(%s)",
                                               (list(fams),)).fetchall()}
    queue, needs_action, done_families = [], [], 0
    counts = {k: 0 for k in PRIORITY}
    q = (search or "").strip().casefold()
    for fid, kids in fams.items():
        match = [k for k in kids if all(str(k[c]) == str(v) for c, v in flt.items())]
        if not match:
            continue
        if q and not any(q in (k["name"] or "").casefold() or q in (k["contact"] or "") for k in kids):
            continue
        for k in kids:
            if k["state"] == "needs_action":
                needs_action.append(k)
        if any(k["called_today"] for k in kids):
            done_families += 1  # the parent was spoken to (or tried) today
            continue
        states = [k["state"] for k in kids if k["state"] in PRIORITY]
        if not states:
            continue
        best = min(states, key=PRIORITY.get)
        counts[best] += 1
        callable_kids = [k for k in kids if k["state"] in PRIORITY]
        queue.append({
            "family_id": fid, "parent_name": (names.get(fid) or {}).get("display_name"),
            "verified_family": (names.get(fid) or {}).get("verified", False),
            "phones": sorted({k["contact"] for k in kids if k["contact"]}),
            "priority": best, "reason": PRIORITY_LABEL[best],
            "children": kids, "call_children": [k["id"] for k in callable_kids],
            "total_due": sum((k["balance"] for k in kids), Decimal(0)),
            "oldest_due": min((k["fu_due"] for k in callable_kids if k["fu_due"]), default=None),
        })
    queue.sort(key=lambda f: (PRIORITY[f["priority"]], f["oldest_due"] or date.max, -f["total_due"], f["family_id"]))
    return {"items": queue, "counts": counts, "needs_action": needs_action, "families_done_today": done_families}


def follow_up_list(conn, when: str) -> list[dict]:
    today = _today(conn)
    out = []
    for r in conn.execute(STUDENT_STATE_SQL + " AND fu.id IS NOT NULL ORDER BY fu.due_date NULLS FIRST, f.balance DESC").fetchall():
        st = _child_state(r, today)
        d = _decorate(r, st)
        due = r["fu_due"]
        if when == "needs_action" and st == "needs_action":
            out.append(d)
        elif r["fu_kind"] in ACTION_KINDS:
            continue
        elif when == "overdue" and due and due < today:
            out.append(d)
        elif when == "today" and due == today:
            out.append(d)
        elif when == "upcoming" and due and due > today:
            out.append(d)
    return out


def campaign_progress(conn, c: dict) -> dict:
    """How the campaign is going, from calls made and from what Excel says was paid since it began."""
    flt = campaign_filter(c)
    where, p = ["s.missing_since IS NULL"], {"start": c["started_at"]}
    for k, v in flt.items():
        where.append(f"s.{k} = %({k})s")
        p[k] = v
    r = conn.execute(
        f"""WITH scope AS (
              SELECT s.id, s.family_id, f.balance AS now_bal,
                     coalesce((SELECT balance FROM fee_snapshots x WHERE x.student_id=s.id AND x.captured_at <= %(start)s
                               ORDER BY x.id DESC LIMIT 1),
                              (SELECT balance FROM fee_snapshots x WHERE x.student_id=s.id ORDER BY x.id LIMIT 1)) AS start_bal
              FROM students s JOIN current_fees f ON f.student_id=s.id WHERE {' AND '.join(where)})
            SELECT count(DISTINCT family_id) FILTER (WHERE start_bal > 0) AS families_in_scope,
                   count(*) FILTER (WHERE start_bal > 0) AS students_in_scope,
                   coalesce(sum(start_bal) FILTER (WHERE start_bal > 0), 0) AS due_at_start,
                   coalesce(sum(greatest(start_bal - now_bal, 0)) FILTER (WHERE start_bal > 0), 0) AS collected,
                   count(*) FILTER (WHERE start_bal > 0 AND now_bal <= 0) AS students_cleared,
                   count(DISTINCT family_id) FILTER (WHERE start_bal > 0 AND EXISTS (
                       SELECT 1 FROM calls c WHERE c.student_id = scope.id AND c.called_at >= %(start)s)) AS families_contacted
            FROM scope""", p).fetchone()
    return dict(r)
