"""Plan and apply a sync of one workbook (or a whole folder) into PostgreSQL.

Rules enforced here:
* No duplicates: a student is matched only by an exact identity key (see reader.identity_key).
  Anything that *looks* like the same student but doesn't match exactly goes to review.
* Nothing is deleted. A student missing from the latest file is flagged (missing_since).
* Fee values are appended as snapshots; history is never overwritten.
* A balance going up or a paid amount going down is held for review (until the school
  confirms other payment rules).
* Nobody is ever marked "paid": the system only stores what Excel says.
* Dry-run reads, validates and plans, then writes only a run report - never student data.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from psycopg.types.json import Jsonb

from .config import Config
from .reader import ParsedFile, StudentRow, identity_key, norm_key, read_workbook

log = logging.getLogger("schoolops.sync")
FEE_FIELDS = ("total_fee", "discount", "paid", "balance")
PREVIEW_LIMIT = 50


def _s(v):
    return None if v is None else str(v)


def _fees(row: StudentRow) -> dict:
    return {f: _s(getattr(row, f)) for f in FEE_FIELDS}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def is_workbook_name(name: str, patterns) -> bool:
    if name.startswith(("~$", ".~lock", ".")) or name.endswith("#"):
        return False  # Excel / WPS / LibreOffice temp and lock files
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns)


@dataclass
class Plan:
    new: list[StudentRow] = field(default_factory=list)
    matched: list[tuple[dict, StudentRow, dict]] = field(default_factory=list)  # (existing, row, field changes)
    fee_updates: list[tuple[dict | None, StudentRow]] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    returned: list[dict] = field(default_factory=list)
    unchanged: int = 0
    rows_not_synced: int = 0

    def counts(self) -> dict:
        return {
            "new_students": len(self.new),
            "fee_updates": sum(1 for e, _ in self.fee_updates if e is not None),
            "needs_review": len(self.reviews),
            "missing_from_file": len(self.missing),
            "back_in_file": len(self.returned),
            "unchanged": self.unchanged,
            "rows_not_synced": self.rows_not_synced,
        }

    def preview(self) -> dict:
        return {
            "new_students": [f"{r.name} ({r.medium} {r.std}-{r.section})" for r in self.new[:PREVIEW_LIMIT]],
            "fee_updates": [
                {"name": r.name, "from": {f: _s(e.get(f)) for f in FEE_FIELDS}, "to": _fees(r)}
                for e, r in self.fee_updates[:PREVIEW_LIMIT] if e is not None
            ],
            "needs_review": self.reviews[:PREVIEW_LIMIT],
            "missing_from_file": [m["name"] for m in self.missing[:PREVIEW_LIMIT]],
        }


def build_plan(conn, cfg: Config, pf: ParsedFile) -> Plan:
    plan = Plan()
    rows = pf.rows
    plan.rows_not_synced = sum(1 for r in rows if not r.valid)
    groups = sorted({(r.medium, r.std, r.section or "") for r in rows if r.medium and r.std})
    keys_in_file = {identity_key(r) for r in rows}

    existing: dict[str, dict] = {}
    q = """SELECT s.*, f.total_fee, f.discount, f.paid, f.balance, f.id AS snapshot_id
           FROM students s LEFT JOIN current_fees f ON f.student_id = s.id """
    if groups:
        for g in groups:
            for rec in conn.execute(q + "WHERE s.medium=%s AND s.std=%s AND s.section=%s", g):
                existing[rec["identity_key"]] = rec
    if keys_in_file:
        for rec in conn.execute(q + "WHERE s.identity_key = ANY(%s)", (list(keys_in_file),)):
            existing[rec["identity_key"]] = rec

    review_candidates: set[int] = set()
    for r in rows:
        if not r.valid:
            continue
        key = identity_key(r)
        ex = existing.get(key)
        if ex:
            changes = {}
            for fld in ("contact", "sr_no", "medium", "std", "section", "student_ext_id"):
                new = getattr(r, fld) if fld != "section" else (r.section or "")
                if new is not None and new != ex.get(fld):
                    changes[fld] = new
            if ex["missing_since"] is not None:
                plan.returned.append({"id": ex["id"], "name": ex["name"]})
            plan.matched.append((ex, r, changes))
            if ex["snapshot_id"] is None:
                plan.fee_updates.append((None, r))
                continue
            if all(_s(ex[f]) == _s(getattr(r, f)) for f in FEE_FIELDS):
                if not changes:
                    plan.unchanged += 1
                continue
            reasons = []
            if r.balance is not None and ex["balance"] is not None and r.balance > ex["balance"] \
                    and cfg.rule("balance_increase") == "review":
                reasons.append("balance_increased")
            if r.paid is not None and ex["paid"] is not None and r.paid < ex["paid"] \
                    and cfg.rule("paid_decrease") == "review":
                reasons.append("paid_decreased")
            if reasons:
                plan.reviews.append({
                    "kind": "fee_change", "student_id": ex["id"],
                    "details": {"name": r.name, "class": f"{r.medium} {r.std}-{r.section}",
                                "reasons": reasons,
                                "from": {f: _s(ex[f]) for f in FEE_FIELDS}, "to": _fees(r),
                                "raw": r.raw, "file": pf.path.name, "sheet": r.sheet, "row": r.row_number},
                })
            else:
                plan.fee_updates.append((ex, r))
            continue

        # Not an exact match. Look for a *possible* match in the same class/section,
        # but never decide it automatically.
        cands = []
        if not r.student_ext_id:
            for e in existing.values():
                if (e["medium"], e["std"], e["section"]) != (r.medium, r.std, r.section or ""):
                    continue
                if e["identity_key"] in keys_in_file:
                    continue
                if (r.sr_no and e["sr_no"] == r.sr_no) or norm_key(e["name"]) == norm_key(r.name):
                    cands.append(e)
        if cands:
            review_candidates.update(c["id"] for c in cands)
            plan.reviews.append({
                "kind": "possible_same_student", "student_id": cands[0]["id"],
                "details": {
                    "row": {"name": r.name, "sr_no": r.sr_no, "medium": r.medium, "std": r.std,
                            "section": r.section, "contact": r.contact, "student_ext_id": r.student_ext_id,
                            "identity_key": identity_key(r), "fees": _fees(r), "raw": r.raw},
                    "candidates": [{"id": c["id"], "name": c["name"], "sr_no": c["sr_no"]} for c in cands],
                    "file": pf.path.name, "sheet": r.sheet, "row_number": r.row_number,
                },
            })
        else:
            plan.new.append(r)

    # Students of this file's classes that are no longer in it: flag, never delete.
    for e in existing.values():
        if (e["identity_key"] not in keys_in_file and e["missing_since"] is None
                and e["id"] not in review_candidates and e["source_file"] == pf.path.name
                and (e["medium"], e["std"], e["section"]) in set(groups)):
            plan.missing.append({"id": e["id"], "name": e["name"]})
    return plan


def _review_hash(item: dict) -> str:
    d = item["details"]
    if item["kind"] == "fee_change":
        core = {"student_id": item["student_id"], "to": d["to"]}
    else:
        core = {"row": {k: v for k, v in d["row"].items() if k != "raw"},
                "candidates": [c["id"] for c in d["candidates"]]}
    return _hash({"kind": item["kind"], **core})


def insert_snapshot(conn, student_id: int, run_id: int | None, row_fees: dict, raw: dict, source="excel"):
    conn.execute(
        """INSERT INTO fee_snapshots (student_id, run_id, source, total_fee, discount, paid, balance, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (student_id, run_id, source, row_fees.get("total_fee"), row_fees.get("discount"),
         row_fees.get("paid"), row_fees["balance"], Jsonb(raw)),
    )


def insert_student(conn, row: dict, file_name: str, sheet: str) -> int:
    rec = conn.execute(
        """INSERT INTO students (identity_key, student_ext_id, medium, std, section, sr_no, name, contact,
                                 source_file, source_sheet)
           VALUES (%(identity_key)s, %(student_ext_id)s, %(medium)s, %(std)s, %(section)s, %(sr_no)s,
                   %(name)s, %(contact)s, %(file)s, %(sheet)s) RETURNING id""",
        {**row, "section": row.get("section") or "", "file": file_name, "sheet": sheet},
    ).fetchone()
    return rec["id"]


def apply_plan(conn, pf: ParsedFile, plan: Plan, run_id: int) -> None:
    fname = pf.path.name
    for r in plan.new:
        sid = insert_student(conn, {"identity_key": identity_key(r), "student_ext_id": r.student_ext_id,
                                    "medium": r.medium, "std": r.std, "section": r.section, "sr_no": r.sr_no,
                                    "name": r.name, "contact": r.contact}, fname, r.sheet)
        insert_snapshot(conn, sid, run_id, _fees(r), r.raw)
    for ex, r, changes in plan.matched:
        sets = ", ".join(f"{k} = %({k})s" for k in changes)
        conn.execute(
            f"""UPDATE students SET {sets + ',' if sets else ''} last_seen_at = now(), missing_since = NULL,
                    source_file = %(f)s, source_sheet = %(sh)s WHERE id = %(id)s""",
            {**changes, "f": fname, "sh": r.sheet, "id": ex["id"]},
        )
    for ex, r in plan.fee_updates:
        if ex is not None:
            insert_snapshot(conn, ex["id"], run_id, _fees(r), r.raw)
        else:  # matched student with no fee history yet
            sid = conn.execute("SELECT id FROM students WHERE identity_key=%s", (identity_key(r),)).fetchone()["id"]
            insert_snapshot(conn, sid, run_id, _fees(r), r.raw)
    for item in plan.reviews:
        conn.execute(
            """INSERT INTO review_items (run_id, student_id, kind, details, details_hash)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT (kind, details_hash) DO NOTHING""",
            (run_id, item["student_id"], item["kind"], Jsonb(item["details"]), _review_hash(item)),
        )
    for m in plan.missing:
        conn.execute("UPDATE students SET missing_since = now() WHERE id = %s AND missing_since IS NULL", (m["id"],))
    conn.execute(
        """INSERT INTO file_state (path, last_hash, last_synced_at, last_run_id) VALUES (%s,%s,now(),%s)
           ON CONFLICT (path) DO UPDATE SET last_hash = EXCLUDED.last_hash,
             last_synced_at = now(), last_run_id = EXCLUDED.last_run_id""",
        (str(pf.path.resolve()), pf.file_hash, run_id),
    )


def _start_run(conn, path: Path, dry_run: bool, trigger: str, file_hash: str | None) -> int:
    with conn.transaction():
        return conn.execute(
            "INSERT INTO sync_runs (trigger, dry_run, file_path, file_hash) VALUES (%s,%s,%s,%s) RETURNING id",
            (trigger, dry_run, str(path), file_hash),
        ).fetchone()["id"]


def _finish_run(conn, run_id: int, status: str, summary: dict) -> None:
    with conn.transaction():
        conn.execute("UPDATE sync_runs SET status=%s, summary=%s, finished_at=now() WHERE id=%s",
                     (status, Jsonb(summary), run_id))


def sync_file(conn, cfg: Config, path: str | Path, *, dry_run: bool | None = None,
              trigger: str = "manual", force: bool = False) -> dict:
    path = Path(path)
    dry = cfg.dry_run if dry_run is None else dry_run
    cfg.assert_sync_allowed(path.parent)

    pf = read_workbook(path, cfg)
    if not force and not dry and pf.file_hash:
        st = conn.execute("SELECT last_hash FROM file_state WHERE path=%s", (str(path.resolve()),)).fetchone()
        conn.commit()
        if st and st["last_hash"] == pf.file_hash:
            return {"file": path.name, "status": "unchanged"}

    run_id = _start_run(conn, path, dry, trigger, pf.file_hash)
    with conn.transaction():
        for i in pf.issues:
            conn.execute(
                "INSERT INTO sync_issues (run_id, level, code, message, sheet, row_number) VALUES (%s,%s,%s,%s,%s,%s)",
                (run_id, i.level, i.code, i.message, i.sheet, i.row),
            )
    base = {
        "run_id": run_id, "file": path.name, "dry_run": dry,
        "sheets_read": pf.sheets_read, "sheets_failed": pf.sheets_failed, "sheets_skipped": pf.sheets_skipped,
        "errors": sum(i.level == "error" for i in pf.issues),
        "warnings": sum(i.level == "warning" for i in pf.issues),
    }
    for i in pf.issues:
        (log.error if i.level == "error" else log.info)("%s [%s] %s row %s: %s", path.name, i.code, i.sheet, i.row, i.message)
    if pf.fatal:
        summary = {**base, "status": "failed"}
        _finish_run(conn, run_id, "failed", summary)
        return summary

    try:
        plan = build_plan(conn, cfg, pf)
        conn.commit()
        summary = {**base, **plan.counts()}
        if dry:
            summary.update(status="dry_run", preview=plan.preview())
            _finish_run(conn, run_id, "dry_run", summary)
            log.info("DRY RUN %s: %s", path.name, plan.counts())
            return summary
        with conn.transaction():
            apply_plan(conn, pf, plan, run_id)
        summary["status"] = "applied"
        _finish_run(conn, run_id, "applied", summary)
        log.info("SYNCED %s: %s", path.name, plan.counts())
        return summary
    except Exception as exc:  # keep the service alive; record what happened
        conn.rollback()
        log.exception("Sync failed for %s", path.name)
        summary = {**base, "status": "failed", "error": f"{exc.__class__.__name__}: {exc}"}
        _finish_run(conn, run_id, "failed", summary)
        return summary


def list_workbooks(cfg: Config, folder: Path | None = None) -> list[Path]:
    folder = Path(folder or cfg.watch_folder)
    return sorted(p for p in folder.iterdir() if p.is_file() and is_workbook_name(p.name, cfg.file_patterns))


def sync_folder(conn, cfg: Config, folder: Path | None = None, *, dry_run: bool | None = None,
                trigger: str = "manual", force: bool = False) -> dict:
    """Sync every workbook in the folder. Also used for periodic reconciliation:
    unchanged files are skipped by hash, and files that disappeared are reported."""
    folder = Path(folder or cfg.watch_folder)
    cfg.assert_sync_allowed(folder)
    results = [sync_file(conn, cfg, p, dry_run=dry_run, trigger=trigger, force=force)
               for p in list_workbooks(cfg, folder)]
    present = {str(p.resolve()) for p in list_workbooks(cfg, folder)}
    gone = [r["path"] for r in conn.execute("SELECT path FROM file_state")
            if r["path"].startswith(str(folder.resolve())) and r["path"] not in present]
    conn.commit()
    if gone:
        log.warning("Files synced before but now missing from the folder (data kept): %s", gone)
    return {"files": results, "files_missing_from_folder": gone}
