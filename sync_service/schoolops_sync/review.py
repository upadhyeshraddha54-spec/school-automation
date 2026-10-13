"""Approve or reject changes the sync held back. A person always decides."""
from __future__ import annotations

from .sync import insert_snapshot, insert_student


class ReviewError(Exception):
    pass


def _load(conn, item_id: int) -> dict:
    item = conn.execute("SELECT * FROM review_items WHERE id=%s", (item_id,)).fetchone()
    if not item:
        raise ReviewError("Review item not found")
    if item["status"] != "pending":
        raise ReviewError(f"Already {item['status']}")
    return item


def _close(conn, item_id: int, status: str, by: str | None):
    conn.execute("UPDATE review_items SET status=%s, resolved_at=now(), resolved_by=%s WHERE id=%s",
                 (status, by, item_id))


def approve(conn, item_id: int, by: str | None = None) -> dict:
    """fee_change: accept the new Excel values.
    possible_same_student: yes, it is the same student (keeps their call history)."""
    with conn.transaction():
        item = _load(conn, item_id)
        d = item["details"]
        if item["kind"] == "fee_change":
            insert_snapshot(conn, item["student_id"], item["run_id"], d["to"], d.get("raw", {}), source="review_approved")
        elif item["kind"] == "possible_same_student":
            row = d["row"]
            target = item["student_id"]
            clash = conn.execute("SELECT id FROM students WHERE identity_key=%s AND id<>%s",
                                 (row["identity_key"], target)).fetchone()
            if clash:
                raise ReviewError("Another student already has these details; resolve that first.")
            conn.execute(
                """UPDATE students SET identity_key=%s, name=%s, sr_no=%s, contact=COALESCE(%s, contact),
                       student_ext_id=COALESCE(%s, student_ext_id), last_seen_at=now(), missing_since=NULL,
                       source_file=%s, source_sheet=%s WHERE id=%s""",
                (row["identity_key"], row["name"], row["sr_no"], row["contact"], row["student_ext_id"],
                 d["file"], d["sheet"], target),
            )
            insert_snapshot(conn, target, item["run_id"], row["fees"], row.get("raw", {}), source="review_approved")
        else:
            raise ReviewError(f"Unknown review kind {item['kind']}")
        _close(conn, item_id, "approved", by)
    return {"id": item_id, "status": "approved"}


def reject(conn, item_id: int, by: str | None = None) -> dict:
    """fee_change: keep the old values.
    possible_same_student: no, it is a different student -> add them as a new student."""
    with conn.transaction():
        item = _load(conn, item_id)
        d = item["details"]
        if item["kind"] == "possible_same_student":
            row = d["row"]
            if conn.execute("SELECT 1 FROM students WHERE identity_key=%s", (row["identity_key"],)).fetchone():
                raise ReviewError("This student already exists.")
            sid = insert_student(conn, {k: row.get(k) for k in
                                        ("identity_key", "student_ext_id", "medium", "std", "section",
                                         "sr_no", "name", "contact")}, d["file"], d["sheet"])
            insert_snapshot(conn, sid, item["run_id"], row["fees"], row.get("raw", {}), source="review_approved")
        _close(conn, item_id, "rejected", by)
    return {"id": item_id, "status": "rejected"}

