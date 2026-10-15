"""Add Excel from the app: upload several files -> preview & validate -> import only after confirmation.

Uploaded files are copied into a private staging folder (one folder per upload). The
preview is a dry run. Nothing is written to students/fees until the teacher confirms.
In test mode only synthetic workbooks (made by `make-synthetic`) are accepted.
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from openpyxl import load_workbook

from .config import SYNTHETIC_MARKER, Config, NotAllowed
from .reader import _xls_to_xlsx, norm_key, read_workbook
from .sync import sync_file

MAX_FILES = 20
MAX_BYTES = 10 * 1024 * 1024
PREVIEW_ROWS = 8
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class ImportError_(Exception):
    pass


def _safe_name(name: str) -> str:
    base = Path(name or "file.xlsx").name
    base = re.sub(r"[^\w .()\-]", "_", base).strip(" .") or "file.xlsx"
    return base[:120]


def _is_synthetic(path: Path) -> bool:
    """Synthetic workbooks carry 'SYNTHETIC' in cell A1 of every class sheet."""
    try:
        wb = load_workbook(path, read_only=True)
        try:
            firsts = [next(ws.iter_rows(min_row=1, max_row=1, values_only=True), (None,))[0] for ws in wb.worksheets]
        finally:
            wb.close()
    except Exception:
        return False
    return any(isinstance(v, str) and "SYNTHETIC" in v.upper() for v in firsts)


def is_synthetic_file(path: Path) -> bool:
    """True only for workbooks made by the synthetic generator (.xlsx or .xls)."""
    path = Path(path)
    if path.suffix.lower() != ".xls":
        return _is_synthetic(path)
    conv = path.with_name(".syn-" + uuid.uuid4().hex + ".xlsx")
    try:
        _xls_to_xlsx(path, conv)
        return _is_synthetic(conv)
    except Exception:
        return False
    finally:
        conv.unlink(missing_ok=True)


def _staging(cfg: Config, token: str) -> Path:
    if not _TOKEN.match(token or ""):
        raise ImportError_("Unknown upload. Please add the files again.")
    return Path(cfg.upload_folder).resolve() / token


def _columns(path: Path, cfg: Config) -> list[dict]:
    """For each expected heading: is it present in the heading row of every class sheet?"""
    if path.suffix.lower() == ".xls":
        conv = path.with_name(".cols-" + uuid.uuid4().hex + ".xlsx")
        _xls_to_xlsx(path, conv)
        try:
            return _columns(conv, cfg)
        finally:
            conv.unlink(missing_ok=True)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = {}
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(min_row=cfg.header_row, max_row=cfg.header_row, values_only=True))
            sheets[ws.title] = {norm_key(h) for h in (rows[0] if rows else ()) if h is not None}
    finally:
        wb.close()
    expected = {norm_key(h) for f in cfg.columns for h in cfg.headings(f)}
    class_sheets = [s for s, hs in sheets.items() if hs & expected]
    out = []
    for field, heading in cfg.columns.items():
        missing = [s for s in class_sheets if not ({norm_key(h) for h in cfg.headings(field)} & sheets[s])]
        out.append({"field": field, "heading": heading, "ok": bool(class_sheets) and not missing, "missing_in": missing})
    return out


def _cleanup_stale(cfg: Config, max_age_hours: float = 24) -> None:
    """Remove staged copies of uploads that were never imported or cancelled."""
    import time
    root = Path(cfg.upload_folder).resolve()
    if not root.is_dir():
        return
    for d in root.iterdir():
        if d.is_dir() and _TOKEN.match(d.name) and time.time() - d.stat().st_mtime > max_age_hours * 3600:
            shutil.rmtree(d, ignore_errors=True)


def preview_upload(conn, cfg: Config, files: list[tuple[str, bytes]]) -> dict:
    if not files:
        raise ImportError_("Choose at least one Excel file.")
    if len(files) > MAX_FILES:
        raise ImportError_(f"Please add at most {MAX_FILES} files at a time.")
    _cleanup_stale(cfg)
    token = uuid.uuid4().hex
    folder = _staging(cfg, token)
    folder.mkdir(parents=True, exist_ok=True)
    results, staged = [], []
    used = set()
    for name, data in files:
        safe = _safe_name(name)
        while safe.lower() in used:
            safe = "copy of " + safe
        used.add(safe.lower())
        r = {"file": safe, "importable": False}
        results.append(r)
        if len(data) > MAX_BYTES:
            r["problem"] = "File is larger than 10 MB."
            continue
        if Path(safe).suffix.lower() not in (".xlsx", ".xlsm", ".xls"):
            r["problem"] = "This is not an Excel file. Please choose .xlsx (or old .xls) files."
            continue
        p = folder / safe
        p.write_bytes(data)
        if p.suffix.lower() == ".xls":
            try:
                conv = folder / (".check-" + uuid.uuid4().hex + ".xlsx")
                _xls_to_xlsx(p, conv)
                synthetic = _is_synthetic(conv)
                conv.unlink(missing_ok=True)
            except Exception:
                p.unlink()
                r["problem"] = "This .xls file could not be read. Open it in Excel or WPS and use Save As → .xlsx."
                continue
        else:
            synthetic = _is_synthetic(p)
        if cfg.test_mode and not synthetic:
            p.unlink()
            r["problem"] = "Test mode only accepts synthetic test files. Real school files are blocked until the column mapping is confirmed."
            continue
        staged.append((r, p))

    if cfg.test_mode and staged:
        (folder / SYNTHETIC_MARKER).write_text("Verified synthetic uploads.\n")
    try:
        cfg.assert_sync_allowed(folder)
    except NotAllowed as exc:
        for r, p in staged:
            r["problem"] = str(exc)
            p.unlink(missing_ok=True)
        staged = []

    for r, p in staged:
        pf = read_workbook(p, cfg)
        r["columns"] = _columns(p, cfg) if not pf.fatal else []
        r["sheets"] = [{"sheet": s, "students": sum(1 for x in pf.rows if x.sheet == s)} for s in pf.sheets_read]
        r["sheets_failed"] = pf.sheets_failed
        r["issues"] = [{"level": i.level, "message": i.message, "sheet": i.sheet, "row": i.row}
                       for i in pf.issues if i.level != "info"][:30]
        r["rows"] = [{"sheet": x.sheet, "row": x.row_number, "sr_no": x.sr_no, "name": x.name,
                      "class": f"{x.medium or '?'} {x.std or '?'}{'-' + x.section if x.section else ''}",
                      "contact": x.contact, "total": x.total_fee, "paid": x.paid, "balance": x.balance, "ok": x.valid}
                     for x in pf.rows[:PREVIEW_ROWS]]
        r["students_in_file"] = len(pf.rows)
        r["rows_with_errors"] = sum(1 for x in pf.rows if not x.valid)
        if pf.fatal:
            r["problem"] = pf.issues[0].message if pf.issues else "The file could not be read."
            continue
        summary = sync_file(conn, cfg, p, dry_run=True, trigger="upload", force=True, record_state=False)
        r["plan"] = {k: summary.get(k, 0) for k in
                     ("new_students", "fee_updates", "needs_review", "unchanged", "missing_from_file", "rows_not_synced")}
        r["importable"] = bool(pf.sheets_read)
        if not r["importable"]:
            r["problem"] = "No class sheet with the expected headings was found."
    if not any(r["importable"] for r in results):
        shutil.rmtree(folder, ignore_errors=True)
        token = None
    return {"token": token, "files": results, "expected_columns": list(cfg.columns.values())}


def confirm_import(conn, cfg: Config, token: str, file_names: list[str]) -> dict:
    folder = _staging(cfg, token)
    if not folder.is_dir():
        raise ImportError_("This upload has expired. Please add the files again.")
    if not file_names:
        raise ImportError_("Choose at least one file to import.")
    cfg.assert_sync_allowed(folder)
    out = []
    for name in file_names:
        p = folder / _safe_name(name)
        if p.parent != folder or not p.is_file():
            raise ImportError_(f"{name} is not part of this upload.")
        out.append(sync_file(conn, cfg, p, dry_run=False, trigger="upload", force=True, record_state=False))
    shutil.rmtree(folder, ignore_errors=True)  # our staged copies only
    return {"files": out}


def discard_upload(cfg: Config, token: str) -> None:
    shutil.rmtree(_staging(cfg, token), ignore_errors=True)

