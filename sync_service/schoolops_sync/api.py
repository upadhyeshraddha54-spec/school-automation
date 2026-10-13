"""FastAPI app: Excel sync control + the data the Fees Calling Desk pages need."""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import messages as msg
from .config import Config, NotAllowed
from .db import connect, init_db
from .reader import STD_ORDER
from .review import ReviewError, approve, reject
from .sync import sync_file, sync_folder
from .watcher import FolderWatcher

log = logging.getLogger("schoolops.api")

KLASS_SQL = "s.medium || ' ' || s.std || CASE WHEN s.section <> '' THEN '-' || s.section ELSE '' END"
ORDER_SQL = ("array_position(%(std_order)s::text[], s.std), s.medium, s.section, "
             "NULLIF(regexp_replace(coalesce(s.sr_no,''), '\\D', '', 'g'), '')::int NULLS LAST, s.name")


class SyncRequest(BaseModel):
    file: str | None = Field(None, description="File name inside the watched folder. Empty = whole folder.")
    dry_run: bool | None = Field(None, description="Default comes from config (dry-run unless switched off).")
    force: bool = False


class CallIn(BaseModel):
    student_id: int
    outcome: str
    follow_up_date: date | None = None
    note: str | None = Field(None, max_length=500)
    caller: str | None = Field(None, max_length=80)


class MockMessageIn(BaseModel):
    student_id: int
    lang: Literal["en", "hi", "mr"]


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
        yield
        if state["watcher"]:
            state["watcher"].stop()

    app = FastAPI(title="SchoolOps Excel Sync", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def db():
        with connect(cfg.database_url) as conn:
            yield conn

    def student_or_404(conn, sid: int) -> dict:
        row = conn.execute(
            f"""SELECT s.*, {KLASS_SQL} AS klass, f.balance, f.total_fee, f.paid, f.discount, f.captured_at AS fee_as_of
                FROM students s LEFT JOIN current_fees f ON f.student_id=s.id WHERE s.id=%s""", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "Student not found")
        return row

    # ------------------------------------------------------------------ health / sync
    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/sync/status")
    def sync_status(conn=Depends(db)):
        runs = conn.execute("SELECT id, started_at, file_path, status, dry_run, summary FROM sync_runs "
                            "ORDER BY id DESC LIMIT 10").fetchall()
        pending = conn.execute("SELECT count(*) AS n FROM review_items WHERE status='pending'").fetchone()["n"]
        return {
            "watch_folder": str(cfg.watch_folder), "dry_run_default": cfg.dry_run,
            "mapping_confirmed": cfg.mapping_confirmed, "test_mode": cfg.test_mode,
            "payment_rules_confirmed": cfg.payment_rules_confirmed,
            "watching": state["watcher"] is not None, "watcher_error": state.get("watcher_error"),
            "pending_reviews": pending, "recent_runs": runs,
        }

    @app.post("/sync/run")
    def run_sync(req: SyncRequest, conn=Depends(db)):
        try:
            if req.file:
                folder = Path(cfg.watch_folder).resolve()
                p = (folder / req.file).resolve()
                if p.parent != folder or not p.is_file():
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

    # ------------------------------------------------------------------ students
    @app.get("/filters")
    def filters(conn=Depends(db)):
        def distinct(col):
            return [r["v"] for r in conn.execute(f"SELECT DISTINCT {col} AS v FROM students WHERE {col} <> '' ORDER BY 1")]
        stds = set(distinct("std"))
        return {"std": [s for s in STD_ORDER if s in stds], "medium": distinct("medium"),
                "section": distinct("section"),
                "fee_status": ["due", "no_balance_in_excel", "unknown"], "outcomes": msg.OUTCOMES,
                "languages": msg.LANGS}

    @app.get("/students")
    def students(search: str | None = None, std: str | None = None, medium: str | None = None,
                 section: str | None = None,
                 fee_status: Literal["due", "no_balance_in_excel", "unknown"] | None = None,
                 include_missing: bool = False,
                 page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), conn=Depends(db)):
        where, p = ["TRUE"], {"std_order": STD_ORDER}
        if search:
            where.append("(s.name ILIKE %(q)s OR s.contact ILIKE %(q)s OR s.sr_no = %(qs)s)")
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
        if not include_missing:
            where.append("s.missing_since IS NULL")
        w = " AND ".join(where)
        base = "FROM students s LEFT JOIN current_fees f ON f.student_id = s.id WHERE " + w
        total = conn.execute("SELECT count(*) AS n " + base, p).fetchone()["n"]
        items = conn.execute(
            f"""SELECT s.id, s.name, s.medium, s.std, s.section, s.sr_no, s.contact, s.missing_since,
                       {KLASS_SQL} AS klass, f.total_fee, f.paid, f.balance, f.captured_at AS fee_as_of,
                       CASE WHEN f.balance IS NULL THEN 'unknown' WHEN f.balance > 0 THEN 'due'
                            ELSE 'no_balance_in_excel' END AS fee_status,
                       lc.called_at AS last_call_at, lc.outcome AS last_outcome, lc.follow_up_date
                {base.replace('WHERE', '''LEFT JOIN LATERAL (SELECT called_at, outcome, follow_up_date FROM calls c
                     WHERE c.student_id = s.id ORDER BY called_at DESC LIMIT 1) lc ON TRUE WHERE''', 1)}
                ORDER BY {ORDER_SQL} LIMIT %(lim)s OFFSET %(off)s""",
            {**p, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        return _page(items, total, page, page_size)

    @app.get("/students/{sid}")
    def student_detail(sid: int, conn=Depends(db)):
        s = student_or_404(conn, sid)
        s["fee_history"] = conn.execute("SELECT total_fee, discount, paid, balance, source, captured_at FROM fee_snapshots "
                                        "WHERE student_id=%s ORDER BY id DESC", (sid,)).fetchall()
        s["calls"] = conn.execute("SELECT * FROM calls WHERE student_id=%s ORDER BY called_at DESC", (sid,)).fetchall()
        return s

    # ------------------------------------------------------------------ dashboard / calling
    @app.get("/dashboard")
    def dashboard(conn=Depends(db)):
        t = conn.execute(
            """SELECT count(*) FILTER (WHERE s.missing_since IS NULL) AS students,
                      count(*) FILTER (WHERE f.balance > 0 AND s.missing_since IS NULL) AS students_due,
                      coalesce(sum(f.balance) FILTER (WHERE f.balance > 0 AND s.missing_since IS NULL), 0) AS pending_amount
               FROM students s LEFT JOIN current_fees f ON f.student_id=s.id""").fetchone()
        c = conn.execute(
            """SELECT count(*) FILTER (WHERE called_at::date = current_date) AS calls_today,
                      count(*) FILTER (WHERE called_at::date = current_date AND outcome IN ('will_pay','gave_date')) AS promised_today
               FROM calls""").fetchone()
        fu = conn.execute(
            """WITH last AS (SELECT DISTINCT ON (student_id) student_id, follow_up_date FROM calls
                             ORDER BY student_id, called_at DESC)
               SELECT count(*) FILTER (WHERE l.follow_up_date = current_date) AS due_today,
                      count(*) FILTER (WHERE l.follow_up_date < current_date) AS overdue
               FROM last l JOIN current_fees f ON f.student_id=l.student_id WHERE f.balance > 0""").fetchone()
        last = conn.execute("SELECT finished_at, status, file_path FROM sync_runs WHERE dry_run = FALSE "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        rev = conn.execute("SELECT count(*) AS n FROM review_items WHERE status='pending'").fetchone()["n"]
        return {**t, **c, "follow_ups_today": fu["due_today"], "follow_ups_overdue": fu["overdue"],
                "pending_reviews": rev, "last_sync": last}

    @app.get("/call-next")
    def call_next(std: str | None = None, medium: str | None = None, section: str | None = None,
                  skip: int = Query(0, ge=0), conn=Depends(db)):
        """The next parent to call: promise date passed first, then highest balance.
        Skips anyone already called today or with a future follow-up date."""
        where, p = ["f.balance > 0", "s.missing_since IS NULL",
                    "(lc.called_at IS NULL OR lc.called_at::date < current_date)",
                    "(lc.follow_up_date IS NULL OR lc.follow_up_date <= current_date)"], {}
        for col, val in (("std", std), ("medium", medium), ("section", section)):
            if val:
                where.append(f"s.{col} = %({col})s")
                p[col] = val
        row = conn.execute(
            f"""SELECT s.id, s.name, s.contact, s.sr_no, {KLASS_SQL} AS klass, f.balance,
                       lc.called_at AS last_call_at, lc.outcome AS last_outcome, lc.follow_up_date,
                       (lc.follow_up_date IS NOT NULL AND lc.follow_up_date < current_date) AS promise_passed
                FROM students s JOIN current_fees f ON f.student_id=s.id
                LEFT JOIN LATERAL (SELECT called_at, outcome, follow_up_date FROM calls c WHERE c.student_id=s.id
                                   ORDER BY called_at DESC LIMIT 1) lc ON TRUE
                WHERE {' AND '.join(where)}
                ORDER BY promise_passed DESC, f.balance DESC, s.id LIMIT 1 OFFSET %(skip)s""",
            {**p, "skip": skip}).fetchone()
        return {"student": row, "message": None if row else "Nobody left to call with these filters today."}

    @app.get("/follow-ups")
    def follow_ups(when: Literal["today", "overdue", "upcoming"] = "today", conn=Depends(db)):
        cond = {"today": "= current_date", "overdue": "< current_date", "upcoming": "> current_date"}[when]
        return conn.execute(
            f"""WITH last AS (SELECT DISTINCT ON (student_id) * FROM calls ORDER BY student_id, called_at DESC)
                SELECT s.id, s.name, s.contact, {KLASS_SQL} AS klass, f.balance, l.follow_up_date, l.note, l.caller
                FROM last l JOIN students s ON s.id=l.student_id JOIN current_fees f ON f.student_id=s.id
                WHERE f.balance > 0 AND l.follow_up_date {cond} ORDER BY l.follow_up_date, f.balance DESC""").fetchall()

    @app.post("/calls", status_code=201)
    def add_call(c: CallIn, conn=Depends(db)):
        if c.outcome not in msg.OUTCOMES:
            raise HTTPException(422, f"outcome must be one of {list(msg.OUTCOMES)}")
        if c.outcome in msg.NEEDS_DATE and not c.follow_up_date:
            raise HTTPException(422, "Please choose the date the parent will pay.")
        if c.follow_up_date and c.follow_up_date < date.today():
            raise HTTPException(422, "The follow-up date cannot be in the past.")
        student_or_404(conn, c.student_id)
        return conn.execute(
            """INSERT INTO calls (student_id, outcome, follow_up_date, note, caller) VALUES (%s,%s,%s,%s,%s)
               RETURNING *""", (c.student_id, c.outcome, c.follow_up_date, c.note, c.caller)).fetchone()

    @app.delete("/calls/{call_id}")
    def undo_call(call_id: int, conn=Depends(db)):
        """Undo for a mis-tap: only calls saved in the last 10 minutes can be removed."""
        r = conn.execute("DELETE FROM calls WHERE id=%s AND called_at > now() - interval '10 minutes' RETURNING id",
                         (call_id,)).fetchone()
        if not r:
            raise HTTPException(409, "Only a call saved in the last 10 minutes can be undone.")
        return {"deleted": call_id}

    @app.get("/calls")
    def call_history(student_id: int | None = None, std: str | None = None, medium: str | None = None,
                     section: str | None = None, search: str | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), conn=Depends(db)):
        where, p = ["TRUE"], {}
        for col, val in (("student_id", student_id),):
            if val:
                where.append("c.student_id = %(student_id)s")
                p["student_id"] = val
        for col, val in (("std", std), ("medium", medium), ("section", section)):
            if val:
                where.append(f"s.{col} = %({col})s")
                p[col] = val
        if search:
            where.append("s.name ILIKE %(q)s")
            p["q"] = f"%{search}%"
        w = " AND ".join(where)
        total = conn.execute(f"SELECT count(*) AS n FROM calls c JOIN students s ON s.id=c.student_id WHERE {w}", p).fetchone()["n"]
        items = conn.execute(
            f"""SELECT c.*, s.name, {KLASS_SQL} AS klass FROM calls c JOIN students s ON s.id=c.student_id
                WHERE {w} ORDER BY c.called_at DESC LIMIT %(lim)s OFFSET %(off)s""",
            {**p, "lim": page_size, "off": (page - 1) * page_size}).fetchall()
        for i in items:
            i["outcome_label"] = msg.OUTCOMES.get(i["outcome"], i["outcome"])
        return _page(items, total, page, page_size)

    # ------------------------------------------------------------------ messages (mock only)
    @app.get("/messages/preview/{sid}")
    def preview(sid: int, conn=Depends(db)):
        s = student_or_404(conn, sid)
        return {lang: msg.render(lang, school=cfg.school_name, name=s["name"], klass=s["klass"], amount=s["balance"])
                for lang in msg.TEMPLATES}

    @app.post("/messages/mock", status_code=201)
    def mock_send(m: MockMessageIn, conn=Depends(db)):
        s = student_or_404(conn, m.student_id)
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

    return app


def _page(items, total, page, page_size):
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size))}
