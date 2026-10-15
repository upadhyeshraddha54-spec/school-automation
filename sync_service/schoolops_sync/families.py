"""Parents/families: one family -> many students.

Rules:
* Every student belongs to exactly one family. A new student gets their own family.
* Students are put in the same family automatically ONLY by a verified ID column from
  Excel (config [columns] family_id). Never by phone number.
* Students in different families who share a phone number produce a "possible siblings"
  review item. Staff decide; a "no" is remembered so the same question is not asked again.
"""
from __future__ import annotations

import hashlib
import json
import re

from psycopg.types.json import Jsonb


class FamilyError(Exception):
    pass


def _phone(p: str | None) -> str | None:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    return d if len(d) == 10 else None


def ensure_families(conn) -> None:
    """Give every student a family. Students with the same verified family ID share one."""
    with conn.transaction():
        # verified IDs from Excel
        rows = conn.execute(
            "SELECT id, family_id, family_ext_id, parent_name FROM students WHERE family_ext_id IS NOT NULL").fetchall()
        for r in rows:
            key = "ext:" + r["family_ext_id"].strip().casefold()
            fam = conn.execute("SELECT id FROM families WHERE family_key=%s", (key,)).fetchone()
            if not fam:
                fam = conn.execute("INSERT INTO families (family_key, display_name, verified) VALUES (%s,%s,TRUE) "
                                   "RETURNING id", (key, r["parent_name"])).fetchone()
            if r["family_id"] != fam["id"]:
                old = r["family_id"]
                conn.execute("UPDATE students SET family_id=%s WHERE id=%s", (fam["id"], r["id"]))
                _drop_if_empty(conn, old)
        # everyone else: their own family
        for r in conn.execute("SELECT id, parent_name FROM students WHERE family_id IS NULL ORDER BY id").fetchall():
            fid = conn.execute("INSERT INTO families (display_name) VALUES (%s) RETURNING id",
                               (r["parent_name"],)).fetchone()["id"]
            conn.execute("UPDATE students SET family_id=%s WHERE id=%s", (fid, r["id"]))


def _drop_if_empty(conn, family_id: int | None) -> None:
    if family_id is None:
        return
    if not conn.execute("SELECT 1 FROM students WHERE family_id=%s", (family_id,)).fetchone():
        conn.execute("UPDATE message_drafts SET family_id=NULL WHERE family_id=%s", (family_id,))
        conn.execute("DELETE FROM families WHERE id=%s", (family_id,))


def suggest_siblings(conn, run_id: int | None = None) -> int:
    """Create review items for students in different families that share a phone number."""
    by_phone: dict[str, list[dict]] = {}
    for s in conn.execute(
            """SELECT s.id, s.name, s.contact, s.family_id, s.medium, s.std, s.section
               FROM students s WHERE s.missing_since IS NULL AND s.contact IS NOT NULL""").fetchall():
        p = _phone(s["contact"])
        if p:
            by_phone.setdefault(p, []).append(s)
    created = 0
    for phone, studs in by_phone.items():
        fams = sorted({s["family_id"] for s in studs})
        if len(fams) < 2:
            continue
        details = {"phone": phone, "family_ids": fams,
                   "students": [{"id": s["id"], "name": s["name"], "family_id": s["family_id"],
                                 "class": f"{s['medium']} {s['std']}{'-' + s['section'] if s['section'] else ''}"}
                                for s in sorted(studs, key=lambda x: x["id"])]}
        h = hashlib.sha256(json.dumps({"kind": "possible_siblings", "phone": phone, "families": fams}).encode()).hexdigest()
        r = conn.execute(
            """INSERT INTO review_items (run_id, student_id, kind, details, details_hash)
               VALUES (%s,%s,'possible_siblings',%s,%s) ON CONFLICT (kind, details_hash) DO NOTHING RETURNING id""",
            (run_id, studs[0]["id"], Jsonb(details), h)).fetchone()
        created += 1 if r else 0
    return created


def merge_families(conn, family_ids: list[int]) -> int:
    """Put all students of these families into one family (staff-confirmed)."""
    ids = sorted({int(i) for i in family_ids})
    if len(ids) < 2:
        raise FamilyError("Choose at least two families to join.")
    fams = conn.execute("SELECT * FROM families WHERE id = ANY(%s)", (ids,)).fetchall()
    if len(fams) != len(ids):
        raise FamilyError("One of these families no longer exists. Refresh and try again.")
    keys = {f["family_key"] for f in fams if f["family_key"]}
    if len(keys) > 1:
        raise FamilyError("These students have different family IDs in Excel. Fix the Excel file instead.")
    target = next((f for f in fams if f["family_key"]), None) or fams[0]
    name = target["display_name"] or next((f["display_name"] for f in fams if f["display_name"]), None)
    conn.execute("UPDATE families SET verified=TRUE, display_name=%s WHERE id=%s", (name, target["id"]))
    for f in fams:
        if f["id"] != target["id"]:
            conn.execute("UPDATE students SET family_id=%s WHERE family_id=%s", (target["id"], f["id"]))
            _drop_if_empty(conn, f["id"])
    return target["id"]


def link_student(conn, family_id: int, student_id: int) -> int:
    s = conn.execute("SELECT family_id, family_ext_id FROM students WHERE id=%s", (student_id,)).fetchone()
    if not s:
        raise FamilyError("Student not found")
    if s["family_ext_id"]:
        raise FamilyError("This student's family comes from a family ID in Excel. Change it there.")
    if not conn.execute("SELECT 1 FROM families WHERE id=%s", (family_id,)).fetchone():
        raise FamilyError("Family not found")
    with conn.transaction():
        conn.execute("UPDATE students SET family_id=%s WHERE id=%s", (family_id, student_id))
        conn.execute("UPDATE families SET verified=TRUE WHERE id=%s", (family_id,))
        _drop_if_empty(conn, s["family_id"])
    return family_id


def unlink_student(conn, student_id: int) -> int:
    """Move a student out of a family into their own (e.g. linked by mistake)."""
    s = conn.execute("SELECT family_id, family_ext_id, parent_name FROM students WHERE id=%s", (student_id,)).fetchone()
    if not s:
        raise FamilyError("Student not found")
    if s["family_ext_id"]:
        raise FamilyError("This student's family comes from a family ID in Excel. Change it there.")
    if conn.execute("SELECT count(*) AS n FROM students WHERE family_id=%s", (s["family_id"],)).fetchone()["n"] == 1:
        return s["family_id"]
    with conn.transaction():
        fid = conn.execute("INSERT INTO families (display_name, verified) VALUES (%s, TRUE) RETURNING id",
                           (s["parent_name"],)).fetchone()["id"]
        conn.execute("UPDATE students SET family_id=%s WHERE id=%s", (fid, student_id))
    return fid
