"""FastAPI app: Excel sync + everything the Fees Calling Desk (single staff member) needs."""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import drafts as dr
from . import followups as fx
from . import messages as msg
from .config import Config, NotAllowed
from .db import connect, init_db
from .families import FamilyError, link_student, merge_families, unlink_student
from .imports import ImportError_, confirm_import, discard_upload, preview_upload
from .reader import STD_ORDER
from .review import ReviewError, approve, reject
from .sync import sync_file, sync_folder
from .watcher import FolderWatcher

log = logging.getLogger("schoolops.api")

KLASS_SQL = "s.medium || ' ' || s.std || CASE WHEN s.section <> '' THEN '-' || s.section ELSE '' END"
ORDER_SQL = ("array_position(%(std_order)s::text[], s.std), s.medium, s.section, "
             "NULLIF(regexp_replace(coalesce(s.sr_no,''), '\\D', '', 'g'), '')::int NULLS LAST, s.name")
ACTION_KINDS_SQL = "('fix_number','verify_payment','dispute')"
FOLLOW_UP_FILTERS = {
    "not_called": "NOT EXISTS (SELECT 1 FROM calls c WHERE c.student_id = s.id)",
    "none": "fu.id IS NULL",
    "overdue_promise": "fu.kind IN ('promise','part_payment') AND fu.due_date < current_date",
    "due_today": f"fu.kind NOT IN {ACTION_KINDS_SQL} AND (fu.due_date = current_date OR "
                 "(fu.kind IN ('callback','retry') AND fu.due_date < current_date))",
    "upcoming": "fu.due_date > current_date",
    "needs_action": f"fu.kind IN {ACTION_KINDS_SQL}",
}
DEFAULT_DAILY_TARGET = 40


class SyncRequest(BaseModel):
    file: str | None = Field(None, description="File name inside the watched folder. Empty = whole folder.")
    dry_run: bool | None = Field(None, description="Default comes from config (dry-run unless switched off).")
    force: bool = False


class CallIn(BaseModel):
    student_id: int | None = None
    student_ids: list[int] | None = None   # several siblings in one call
    outcome: str
    follow_up_date: date | None = None
    note: str | None = Field(None, max_length=500)
    caller: str | None = Field(None, max_length=80)
    promised_amount: Decimal | None = None


class ImportConfirm(BaseModel):
    token: str
    files: list[str]


class MockMessageIn(BaseModel):
    student_id: int
    lang: Literal["en", "hi", "mr"]


class DraftIn(BaseModel):
    student_ids: list[int]
    lang: Literal["en", "hi", "mr"]


class DraftEdit(BaseModel):
    body: str = Field(..., max_length=2000)


class FamilyMerge(BaseModel):
    family_ids: list[int]


class FamilyLink(BaseModel):
    student_id: int


class CloseFollowUp(BaseModel):
    reason: str = Field("Done", max_length=200)
    by: str | None = None


class CampaignIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    medium: str | None = None
    std: str | None = None
    section: str | None = None
    daily_target: int = Field(DEFAULT_DAILY_TARGET, ge=1, le=1000)


class TargetIn(BaseModel):
    daily_target: int = Field(..., ge=1, le=1000)


def create_app(cfg: Config, *, start_watcher: bool = True) -> FastAPI:
    state: dict = {"watcher": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with connect(cfg.database_url) as c:
            init_db(c)
        if start_watcher:
            try:
                w = FolderWatcher(cfg, polling=cfg.polling)
                w.start()
                state["watcher"] = w
            except NotAllowed as exc:
                log.error("Watcher not started: %s", exc)
                state["watcher_error"] = str(exc)
            except FileNotFoundError as exc:
                log.error("Watcher not started, folder missing: %s", exc)
                state["watcher_error"] = f"Folder not found: {cfg.watch_folder}"
        yield
        if state["watcher"]:
            state["watcher"].stop()

    app = FastAPI(title="SchoolOps Fees Calling Desk", version="0.2.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
                       allow_methods=["*"], allow_headers=["*"])

    def db():
        with connect(cfg.database_url) as conn:
            yield conn

    def bad(exc, code=422):
        raise HTTPException(code, str(exc))

    def student_or_404(conn, sid: int) -> dict:
        row = conn.execute(
            f"""SELECT s.*, {KLASS_SQL} AS klass, f.balance, f.total_fee, f.paid, f.discount, f.captured_at AS fee_as_of
                FROM students s LEFT JOIN current_fees f ON f.student_id=s.id WHERE s.id=%s""", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Student not found")
        return row

    def setting(conn, key, default):
        r = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return r["value"] if r else default

    def active_campaign(conn, campaign_id: int | None = None):
        if campaign_id:
            return conn.execute("SELECT * FROM campaigns WHERE id=%s", (campaign_id,)).fetchone()
        return conn.execute("SELECT * FROM campaigns WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()

    # ------------------------------------------------------------------ health / sync / review
    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/sync/status")
    def sync_status(conn=Depends(db)):
        runs = conn.execute("SELECT id, started_at, file_path, status, dry_run, trigger, summary FROM sync_runs "
                            "ORDER BY id DESC LIMIT 10").fetchall()
        pending = conn.execute("SELECT count(*) AS n FROM review_items WHERE status='pending'").fetchone()["n"]
        errors = conn.execute(
            """SELECT r.file_path, i.level, i.code, i.message, i.sheet, i.row_number, r.started_at
               FROM sync_issues i JOIN sync_runs r ON r.id = i.run_id
               WHERE i.level = 'error' AND r.id IN (SELECT max(id) FROM sync_runs GROUP BY file_path)
               ORDER BY r.started_at DESC LIMIT 50""").fetchall()
        return {
            "watch_folder": str(cfg.watch_folder), "folder_exists": Path(cfg.watch_folder).is_dir(),
            "dry_run_default": cfg.dry_run, "recursive": cfg.recursive,
            "mapping_confirmed": cfg.mapping_confirmed, "test_mode": cfg.test_mode,
            "payment_rules_confirmed": cfg.payment_rules_confirmed, "messaging_enabled": cfg.messaging_enabled,
            "watching": state["watcher"] is not None, "watcher_error": state.get("watcher_error"),
            "pending_reviews": pending, "recent_runs": runs, "current_errors": errors,
        }

    @app.post("/sync/run")
    def run_sync(req: SyncRequest, conn=Depends(db)):
        dry = cfg.dry_run if req.dry_run is None else req.dry_run
        if not dry and not cfg.test_mode:
            raise HTTPException(409, "Applying changes from the app is disabled for real school data. "
                                     "Use a dry run, or the command line after the mapping is confirmed.")
        try:
            if req.file:
                folder = Path(cfg.watch_folder).resolve()
                p = (folder / req.file).resolve()
                if folder not in p.parents or not p.is_file():
                    raise HTTPException(400, "File must be an existing workbook inside the watched folder.")
                return sync_file(conn, cfg, p, dry_run=req.dry_run, trigger="api", force=req.force)
            return sync_folder(conn, cfg, dry_run=req.dry_run, trigger="api", force=req.force)
        except NotAllowed as exc:
            raise HTTPException(409, str(exc))

    @app.get("/sync/runs")
    def runs(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), conn=Depends(db)):
        total = conn.execute("SELECT count(*) AS n FROM sync_runs").fetchone()["n"]
        items = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT %s OFFSET %s",
                             (page_size, (page - 1) * page_size)).fetchall()
        return _page(items, total, page, page_size)

    @app.get("/sync/runs/{run_id}")
    def run_detail(run_id: int, conn=Depends(db)):
        run = conn.execute("SELECT * FROM sync_runs WHERE id=%s", (run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "Run not found")
        run["issues"] = conn.execute("SELECT level, code, message, sheet, row_number FROM sync_issues "
                                     "WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
        return run

    @app.get("/review")
    def review_list(status: str = "pending", conn=Depends(db)):
        return conn.execute("SELECT * FROM review_items WHERE status=%s ORDER BY id", (status,)).fetchall()

    @app.post("/review/{item_id}/{action}")
    def review_action(item_id: int, action: Literal["approve", "reject"], by: str | None = None, conn=Depends(db)):
        try:
            return (approve if action == "approve" else reject)(conn, item_id, by)
        except ReviewError as exc:
            raise HTTPException(409, str(exc))

    # ------------------------------------------------------------------ Add Excel (upload -> preview -> confirm)
    @app.post("/imports/preview")
    async def import_preview(files: list[UploadFile] = File(...), conn=Depends(db)):
        data = [(f.filename, await f.read()) for f in files]
        try:
            return preview_upload(conn, cfg, data)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))

    @app.post("/imports/confirm")
    def import_confirm(req: ImportConfirm, conn=Depends(db)):
        try:
            return confirm_import(conn, cfg, req.token, req.files)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))
        except NotAllowed as exc:
            raise HTTPException(409, str(exc))

    @app.delete("/imports/{token}")
    def import_discard(token: str):
        try:
            discard_upload(cfg, token)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))
        return {"discarded": True}

    # ------------------------------------------------------------------ students & families
    @app.get("/filters")
    def filters(conn=Depends(db)):
        def distinct(col):
            return [r["v"] for r in conn.execute(f"SELECT DISTINCT {col} AS v FROM students WHERE {col} <> '' ORDER BY 1")]
        stds = set(distinct("std"))
        return {"std": [s for s in STD_ORDER if s in stds], "medium": distinct("medium"),
                "section": distinct("section"),
                "fee_status": ["due", "no_balance_in_excel", "unknown"], "outcomes": msg.OUTCOMES,
                "outcome_rules": {k: {"label": o.label, "date_required": o.date_required,
                                      "default_days": o.days, "follow_up": o.kind} for k, o in fx.OUTCOMES.items()},
                "follow_up_filters": {"not_called": "Not called yet", "overdue_promise": "Missed promise",
                                      "due_today": "Due today", "upcoming": "Waiting for a date",
                                      "needs_action": "Needs office action", "none": "No follow-up"},
                "follow_up_labels": fx.FOLLOW_UP_LABELS, "languages": msg.LANGS}

    @app.get("/students")
    def students(search: str | None = None, std: str | None = None, medium: str | None = None,
                 section: str | None = None,
                 fee_status: Literal["due", "no_balance_in_excel", "unknown"] | None = None,
                 follow_up: str | None = None,
                 include_missing: bool = False, sort: Literal["class", "balance"] = "class",
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), conn=Depends(db)):
        where, p = ["TRUE"], {"std_order": STD_ORDER}
        if search:
            where.append("(s.name ILIKE %(q)s OR s.contact ILIKE %(q)s OR s.sr_no = %(qs)s OR s.parent_name ILIKE %(q)s)")
            p.update(q=f"%{search.strip()}%", qs=search.strip())
        for col, val in (("std", std), ("medium", medium), ("section", section)):
            if val:
                where.append(f"s.{col} = %({col})s")
                p[col] = val
        if fee_status == "due":
            where.append("f.balance > 0")
        elif fee_status == "no_balance_in_excel":
            where.append("f.balance <= 0")
        elif fee_status == "unknown":
            where.append("f.balance IS NULL")
        if follow_up:
            if follow_up not in FOLLOW_UP_FILTERS:
                raise HTTPException(422, f"follow_up must be one of {list(FOLLOW_UP_FILTERS)}")
            where.append(FOLLOW_UP_FILTERS[follow_up])
        if not include_missing:
            where.append("s.missing_since IS NULL")
        w = " AND ".join(where)
        base = ("FROM students s LEFT JOIN current_fees f ON f.student_id = s.id "
                "LEFT JOIN follow_ups fu ON fu.student_id = s.id AND fu.status = 'open' WHERE " + w)
        total = conn.execute("SELECT count(*) AS n " + base, p).fetchone()["n"]
        items = conn.execute(
            f"""SELECT s.id, s.name, s.medium, s.std, s.section, s.sr_no, s.contact, s.missing_since, s.family_id,
                       s.parent_name, {KLASS_SQL} AS klass, f.total_fee, f.paid, f.balance, f.captured_at AS fee_as_of,
                       CASE WHEN f.balance IS NULL THEN 'unknown' WHEN f.balance > 0 THEN 'due'
                            ELSE 'no_balance_in_excel' END AS fee_status,
                       fu.kind AS fu_kind, fu.due_date AS fu_due,
                       (SELECT count(*) - 1 FROM students x WHERE x.family_id = s.family_id) AS siblings,
                       lc.called_at AS last_call_at, lc.outcome AS last_outcome, lc.follow_up_date
                {base.replace('WHERE', '''LEFT JOIN LATERAL (SELECT called_at, outcome, follow_up_date FROM calls c
                     WHERE c.student_id = s.id ORDER BY called_at DESC LIMIT 1) lc ON TRUE WHERE''', 1)}
                ORDER BY {"f.balance DESC NULLS LAST, " if sort == "balance" else ""}{ORDER_SQL}
                LIMIT %(lim)s OFFSET %(off)s""",
            {**p, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        for i in items:
            i["follow_up_label"] = fx.FOLLOW_UP_LABELS.get(i["fu_kind"])
        return _page(items, total, page, page_size)

    @app.get("/students/{sid}")
    def student_detail(sid: int, conn=Depends(db)):
        s = student_or_404(conn, sid)
        s["fee_history"] = conn.execute("SELECT total_fee, discount, paid, balance, source, captured_at FROM fee_snapshots "
                                        "WHERE student_id=%s ORDER BY id DESC", (sid,)).fetchall()
        s["calls"] = conn.execute("SELECT * FROM calls WHERE student_id=%s ORDER BY called_at DESC", (sid,)).fetchall()
        for c in s["calls"]:
            c["outcome_label"] = msg.OUTCOMES.get(c["outcome"], c["outcome"])
        s["follow_ups"] = conn.execute("SELECT * FROM follow_ups WHERE student_id=%s ORDER BY id DESC", (sid,)).fetchall()
        for f in s["follow_ups"]:
            f["label"] = fx.FOLLOW_UP_LABELS.get(f["kind"], f["kind"])
        s["siblings"] = conn.execute(
            f"""SELECT s.id, s.name, {KLASS_SQL} AS klass, f.balance FROM students s
                LEFT JOIN current_fees f ON f.student_id=s.id WHERE s.family_id=%s AND s.id<>%s ORDER BY s.id""",
            (s["family_id"], sid)).fetchall()
        return s

    @app.get("/families/{fid}")
    def family_detail(fid: int, conn=Depends(db)):
        fam = conn.execute("SELECT * FROM families WHERE id=%s", (fid,)).fetchone()
        if not fam:
            raise HTTPException(404, "Family not found")
        kids = conn.execute(
            f"""SELECT s.id, s.name, s.sr_no, s.contact, s.parent_name, s.missing_since, {KLASS_SQL} AS klass,
                       f.total_fee, f.discount, f.paid, f.balance, f.captured_at AS fee_as_of,
                       fu.id AS fu_id, fu.kind AS fu_kind, fu.due_date AS fu_due, fu.balance_at_open
                FROM students s LEFT JOIN current_fees f ON f.student_id=s.id
                LEFT JOIN follow_ups fu ON fu.student_id=s.id AND fu.status='open'
                WHERE s.family_id=%s ORDER BY array_position(%s::text[], s.std), s.id""", (fid, STD_ORDER)).fetchall()
        today = date.today()
        for k in kids:
            k["follow_up_label"] = fx.FOLLOW_UP_LABELS.get(k["fu_kind"])
            k["promise_missed"] = bool(k["fu_kind"] in ("promise", "part_payment") and k["fu_due"] and k["fu_due"] < today)
            k["paid_since_follow_up"] = (k["balance_at_open"] - k["balance"]) if (
                k["balance_at_open"] is not None and k["balance"] is not None and k["balance"] < k["balance_at_open"]) else None
        # each child counted once; only positive balances are "outstanding"
        fam["children"] = kids
        fam["total_due"] = sum((k["balance"] for k in kids if k["balance"] and k["balance"] > 0), Decimal(0))
        fam["phones"] = sorted({k["contact"] for k in kids if k["contact"]})
        fam["calls"] = conn.execute(
            f"""SELECT c.call_group, min(c.id) AS id, min(c.called_at) AS called_at, min(c.outcome) AS outcome,
                       min(c.note) AS note, min(c.caller) AS caller, min(c.follow_up_date) AS follow_up_date,
                       string_agg(s.name, ', ' ORDER BY s.id) AS children
                FROM calls c JOIN students s ON s.id=c.student_id WHERE s.family_id=%s
                GROUP BY c.call_group ORDER BY min(c.called_at) DESC LIMIT 50""", (fid,)).fetchall()
        for c in fam["calls"]:
            c["outcome_label"] = msg.OUTCOMES.get(c["outcome"], c["outcome"])
        return fam

    @app.post("/families/merge")
    def families_merge(req: FamilyMerge, conn=Depends(db)):
        try:
            with conn.transaction():
                return {"family_id": merge_families(conn, req.family_ids)}
        except FamilyError as exc:
            bad(exc, 409)

    @app.post("/families/{fid}/link")
    def family_link(fid: int, req: FamilyLink, conn=Depends(db)):
        try:
            return {"family_id": link_student(conn, fid, req.student_id)}
        except FamilyError as exc:
            bad(exc, 409)

    @app.post("/students/{sid}/unlink")
    def family_unlink(sid: int, conn=Depends(db)):
        try:
            return {"family_id": unlink_student(conn, sid)}
        except FamilyError as exc:
            bad(exc, 409)

    # ------------------------------------------------------------------ dashboard / queue / calling
    @app.get("/dashboard")
    def dashboard(conn=Depends(db)):
        fx.refresh_follow_ups(conn)
        t = conn.execute(
            """SELECT count(*) FILTER (WHERE s.missing_since IS NULL) AS students,
                      count(*) FILTER (WHERE f.balance > 0 AND s.missing_since IS NULL) AS students_due,
                      count(DISTINCT s.family_id) FILTER (WHERE f.balance > 0 AND s.missing_since IS NULL) AS families_due,
                      coalesce(sum(f.balance) FILTER (WHERE f.balance > 0 AND s.missing_since IS NULL), 0) AS pending_amount
               FROM students s LEFT JOIN current_fees f ON f.student_id=s.id""").fetchone()
        c = conn.execute(
            """SELECT count(DISTINCT c.call_group) FILTER (WHERE c.called_at::date = current_date) AS calls_today,
                      count(DISTINCT s.family_id) FILTER (WHERE c.called_at::date = current_date) AS families_called_today,
                      count(DISTINCT c.call_group) FILTER (WHERE c.called_at::date = current_date
                                AND c.outcome IN ('will_pay','gave_date','part_payment')) AS promised_today
               FROM calls c JOIN students s ON s.id=c.student_id""").fetchone()
        fu = conn.execute(
            f"""SELECT count(*) FILTER (WHERE fu.kind IN ('promise','part_payment') AND fu.due_date < current_date) AS overdue_promises,
                       count(*) FILTER (WHERE fu.kind NOT IN {ACTION_KINDS_SQL} AND fu.due_date = current_date) AS due_today,
                       count(*) FILTER (WHERE fu.kind IN {ACTION_KINDS_SQL}) AS needs_action
                FROM follow_ups fu JOIN current_fees f ON f.student_id=fu.student_id
                WHERE fu.status='open' AND f.balance > 0""").fetchone()
        last = conn.execute("SELECT finished_at, status, file_path FROM sync_runs WHERE dry_run = FALSE "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        rev = conn.execute("SELECT count(*) AS n FROM review_items WHERE status='pending'").fetchone()["n"]
        camp = active_campaign(conn)
        target = camp["daily_target"] if camp else int(setting(conn, "daily_target", DEFAULT_DAILY_TARGET))
        return {**t, **c, "follow_ups_today": fu["due_today"], "follow_ups_overdue": fu["overdue_promises"],
                "overdue_promises": fu["overdue_promises"], "needs_action": fu["needs_action"],
                "pending_reviews": rev, "last_sync": last, "daily_target": target,
                "progress_pct": min(100, round(100 * c["families_called_today"] / target)) if target else 0,
                "campaign": ({**camp, "progress": fx.campaign_progress(conn, camp)} if camp else None)}

    @app.get("/queue")
    def queue(std: str | None = None, medium: str | None = None, section: str | None = None,
              search: str | None = None, campaign_id: int | None = None, use_campaign: bool = True,
              page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), conn=Depends(db)):
        fx.refresh_follow_ups(conn)
        camp = active_campaign(conn, campaign_id) if use_campaign else None
        q = fx.build_queue(conn, medium=medium, std=std, section=section, search=search, campaign=camp)
        total = len(q["items"])
        q["items"] = q["items"][(page - 1) * page_size: page * page_size]
        q.update(total=total, page=page, page_size=page_size, pages=max(1, math.ceil(total / page_size)),
                 campaign=camp)
        return q

    @app.get("/classes")
    def classes(conn=Depends(db)):
        """One row per medium/class/section for the dashboard and class filters."""
        return conn.execute(
            f"""SELECT s.medium, s.std, s.section, {KLASS_SQL} AS klass,
                       count(*) AS students,
                       count(*) FILTER (WHERE f.balance > 0) AS students_due,
                       coalesce(sum(f.balance) FILTER (WHERE f.balance > 0), 0) AS pending_amount,
                       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM calls c WHERE c.student_id = s.id
                                                       AND c.called_at::date = current_date)) AS called_today
                FROM students s LEFT JOIN current_fees f ON f.student_id = s.id
                WHERE s.missing_since IS NULL
                GROUP BY s.medium, s.std, s.section
                ORDER BY array_position(%(o)s::text[], s.std), s.medium, s.section""", {"o": STD_ORDER}).fetchall()

    @app.get("/call-next")
    def call_next(std: str | None = None, medium: str | None = None, section: str | None = None,
                  skip: int = Query(0, ge=0), conn=Depends(db)):
        """Kept for older screens: the first child of the next family in the queue."""
        q = fx.build_queue(conn, medium=medium, std=std, section=section)
        if skip >= len(q["items"]):
            return {"student": None, "message": "Nobody left to call with these filters today."}
        fam = q["items"][skip]
        k = next(c for c in fam["children"] if c["id"] in fam["call_children"])
        return {"student": {"id": k["id"], "name": k["name"], "contact": k["contact"], "sr_no": k["sr_no"],
                            "klass": k["klass"], "balance": k["balance"], "last_call_at": k["last_call_at"],
                            "last_outcome": k["last_outcome"], "follow_up_date": k["fu_due"],
                            "promise_passed": fam["priority"] == "missed_promise"}, "family": fam, "message": None}

    @app.get("/follow-ups")
    def follow_ups(when: Literal["today", "overdue", "upcoming", "needs_action"] = "today", conn=Depends(db)):
        fx.refresh_follow_ups(conn)
        rows = fx.follow_up_list(conn, when)
        for r in rows:
            r["follow_up_date"] = r["fu_due"]
            r["note"] = r["fu_note"]
        return rows

    @app.post("/follow-ups/{fu_id}/close")
    def follow_up_close(fu_id: int, req: CloseFollowUp, conn=Depends(db)):
        try:
            fx.close_follow_up(conn, fu_id, req.reason, req.by)
        except fx.CallError as exc:
            bad(exc, 409)
        return {"closed": fu_id}

    @app.post("/calls", status_code=201)
    def add_call(c: CallIn, conn=Depends(db)):
        ids = c.student_ids or ([c.student_id] if c.student_id else [])
        try:
            return fx.record_call(conn, ids, c.outcome, follow_up_date=c.follow_up_date, note=c.note,
                                  caller=c.caller, promised_amount=c.promised_amount)
        except fx.CallError as exc:
            raise HTTPException(404 if "not found" in str(exc) else 422, str(exc))

    @app.delete("/calls/{call_id}")
    def undo_call(call_id: int, conn=Depends(db)):
        """Undo for a mis-tap (last 10 minutes). Removes the whole call, for every sibling it covered."""
        try:
            return {"deleted": call_id, "removed_calls": fx.undo_call(conn, call_id)}
        except fx.CallError as exc:
            bad(exc, 409)

    @app.get("/calls")
    def call_history(student_id: int | None = None, std: str | None = None, medium: str | None = None,
                     section: str | None = None, search: str | None = None, outcome: str | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), conn=Depends(db)):
        where, p = ["TRUE"], {}
        if student_id:
            where.append("c.student_id = %(student_id)s")
            p["student_id"] = student_id
        for col, val in (("std", std), ("medium", medium), ("section", section)):
            if val:
                where.append(f"s.{col} = %({col})s")
                p[col] = val
        if search:
            where.append("(s.name ILIKE %(q)s OR c.note ILIKE %(q)s)")
            p["q"] = f"%{search}%"
        if outcome:
            where.append("c.outcome = %(outcome)s")
            p["outcome"] = outcome
        w = " AND ".join(where)
        total = conn.execute(f"SELECT count(*) AS n FROM calls c JOIN students s ON s.id=c.student_id WHERE {w}", p).fetchone()["n"]
        items = conn.execute(
            f"""SELECT c.*, s.name, s.family_id, {KLASS_SQL} AS klass,
                       (SELECT string_agg(x.name, ', ') FROM calls c2 JOIN students x ON x.id=c2.student_id
                         WHERE c2.call_group = c.call_group AND c2.id <> c.id) AS with_siblings
                FROM calls c JOIN students s ON s.id=c.student_id
                WHERE {w} ORDER BY c.called_at DESC, c.id DESC LIMIT %(lim)s OFFSET %(off)s""",
            {**p, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        for i in items:
            i["outcome_label"] = msg.OUTCOMES.get(i["outcome"], i["outcome"])
        return _page(items, total, page, page_size)

    # ------------------------------------------------------------------ campaigns & targets
    @app.get("/campaigns")
    def campaigns(conn=Depends(db)):
        rows = conn.execute("SELECT * FROM campaigns ORDER BY status, id DESC").fetchall()
        for r in rows:
            r["progress"] = fx.campaign_progress(conn, r)
        return rows

    @app.post("/campaigns", status_code=201)
    def campaign_create(req: CampaignIn, conn=Depends(db)):
        filters_ = {k: v for k, v in (("medium", req.medium), ("std", req.std), ("section", req.section)) if v}
        with conn.transaction():
            conn.execute("UPDATE campaigns SET status='closed', closed_at=now() WHERE status='active'")
            row = conn.execute("INSERT INTO campaigns (name, filters, daily_target) VALUES (%s,%s,%s) RETURNING *",
                               (req.name.strip(), Jsonb(filters_), req.daily_target)).fetchone()
        row["progress"] = fx.campaign_progress(conn, row)
        return row

    @app.post("/campaigns/{cid}/close")
    def campaign_close(cid: int, conn=Depends(db)):
        r = conn.execute("UPDATE campaigns SET status='closed', closed_at=now() WHERE id=%s AND status='active' "
                         "RETURNING id", (cid,)).fetchone()
        if not r:
            raise HTTPException(409, "Campaign is not active.")
        return {"closed": cid}

    @app.put("/settings/daily-target")
    def set_target(req: TargetIn, conn=Depends(db)):
        conn.execute("INSERT INTO settings (key, value) VALUES ('daily_target', %s) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (Jsonb(req.daily_target),))
        camp = active_campaign(conn)
        if camp:
            conn.execute("UPDATE campaigns SET daily_target=%s WHERE id=%s", (req.daily_target, camp["id"]))
        return {"daily_target": req.daily_target}

    # ------------------------------------------------------------------ messages: drafts only, nothing is sent
    @app.get("/messages/preview/{sid}")
    def preview(sid: int, conn=Depends(db)):
        s = student_or_404(conn, sid)
        if s["balance"] is None:
            raise HTTPException(409, "No fee amount from Excel for this student yet, so no message can be prepared.")
        return {lang: msg.render(lang, school=cfg.school_name, name=s["name"], klass=s["klass"], amount=s["balance"])
                for lang in msg.TEMPLATES}

    @app.post("/messages/mock", status_code=201)
    def mock_send(m: MockMessageIn, conn=Depends(db)):
        s = student_or_404(conn, m.student_id)
        if s["balance"] is None:
            raise HTTPException(409, "No fee amount from Excel for this student yet.")
        body = msg.render(m.lang, school=cfg.school_name, name=s["name"], klass=s["klass"], amount=s["balance"])
        rec = conn.execute("INSERT INTO messages (student_id, lang, body) VALUES (%s,%s,%s) RETURNING *",
                           (m.student_id, m.lang, body)).fetchone()
        rec["note"] = "Mock only. No real message was sent."
        return rec

    @app.get("/messages")
    def message_log(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), conn=Depends(db)):
        total = conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
        items = conn.execute(
            f"""SELECT m.*, s.name, {KLASS_SQL} AS klass FROM messages m JOIN students s ON s.id=m.student_id
                ORDER BY m.id DESC LIMIT %s OFFSET %s""", (page_size, (page - 1) * page_size)).fetchall()
        return _page(items, total, page, page_size)

    @app.post("/drafts", status_code=201)
    def draft_create(req: DraftIn, conn=Depends(db)):
        try:
            return dr.create(conn, req.student_ids, req.lang, cfg.school_name)
        except dr.DraftError as exc:
            bad(exc)

    @app.get("/drafts")
    def draft_list(status: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                   conn=Depends(db)):
        w, p = ("WHERE d.status = %(st)s", {"st": status}) if status else ("", {})
        total = conn.execute(f"SELECT count(*) AS n FROM message_drafts d {w}", p).fetchone()["n"]
        items = conn.execute(
            f"""SELECT d.*, (SELECT string_agg(s.name, ', ' ORDER BY s.id) FROM students s
                              WHERE s.id = ANY(d.student_ids)) AS children
                FROM message_drafts d {w} ORDER BY d.id DESC LIMIT %(lim)s OFFSET %(off)s""",
            {**p, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        return _page(items, total, page, page_size)

    @app.put("/drafts/{did}")
    def draft_edit(did: int, req: DraftEdit, conn=Depends(db)):
        try:
            return dr.edit(conn, did, req.body)
        except dr.DraftError as exc:
            bad(exc, 409)

    @app.post("/drafts/{did}/approve")
    def draft_approve(did: int, by: str | None = None, conn=Depends(db)):
        try:
            return dr.approve(conn, did, by)
        except dr.DraftError as exc:
            bad(exc, 409)

    @app.post("/drafts/{did}/discard")
    def draft_discard(did: int, conn=Depends(db)):
        try:
            return dr.discard(conn, did)
        except dr.DraftError as exc:
            bad(exc, 409)

    @app.post("/drafts/{did}/send")
    def draft_send(did: int):
        raise HTTPException(409, "Sending is turned off. No SMS/WhatsApp/email provider is configured. "
                                 "Copy the approved text and send it yourself if needed.")

    web = Path(__file__).resolve().parent.parent / "web"
    if web.is_dir():
        app.mount("/app", StaticFiles(directory=web, html=True), name="app")

        @app.get("/", include_in_schema=False)
        def home():
            return RedirectResponse("/app/")

    return app


def _page(items, total, page, page_size):
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size))}
