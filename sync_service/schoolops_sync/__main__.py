"""Command line:

  python -m schoolops_sync --config config.toml init-db
  python -m schoolops_sync --config config.toml make-synthetic ./synthetic_school
  python -m schoolops_sync --config config.toml check            # dry run of the whole folder
  python -m schoolops_sync --config config.toml sync --apply     # really write to the database
  python -m schoolops_sync --config config.toml serve            # API + folder watcher
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from pathlib import Path

import psycopg

from .config import ConfigError, NotAllowed, load_config
from .db import connect, init_db
from .synthetic import generate
from .sync import sync_file, sync_folder


def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="schoolops_sync")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    g = sub.add_parser("make-synthetic")
    g.add_argument("folder")
    sub.add_parser("check", help="Dry run: validate and show what would change. Writes nothing.")
    s = sub.add_parser("sync")
    s.add_argument("--apply", action="store_true", help="Write to the database (otherwise dry run).")
    s.add_argument("--file", help="Only this workbook (inside the watched folder).")
    s.add_argument("--force", action="store_true", help="Sync even if the file has not changed.")
    v = sub.add_parser("serve")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--no-watch", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "make-synthetic":
        for p in generate(a.folder):
            print("created", p)
        return 0
    try:
        cfg = load_config(a.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Config problem: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_file)

    try:
        if a.cmd == "init-db":
            with connect(cfg.database_url) as c:
                init_db(c)
            print("Database ready.")
        elif a.cmd in ("check", "sync"):
            dry = a.cmd == "check" or not a.apply
            cfg.assert_sync_allowed()  # check safety before touching anything
            with connect(cfg.database_url) as c:
                init_db(c)
                if getattr(a, "file", None):
                    res = sync_file(c, cfg, Path(cfg.watch_folder) / a.file, dry_run=dry, trigger="manual",
                                    force=getattr(a, "force", False))
                else:
                    res = sync_folder(c, cfg, dry_run=dry, trigger="manual", force=getattr(a, "force", False))
            print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
        elif a.cmd == "serve":
            import uvicorn

            from .api import create_app
            uvicorn.run(create_app(cfg, start_watcher=not a.no_watch), host=a.host, port=a.port)
    except NotAllowed as exc:
        print(f"Blocked for safety: {exc}", file=sys.stderr)
        return 3
    except psycopg.OperationalError as exc:
        print(f"Cannot reach the database. Is PostgreSQL running and is database_url correct?\n{exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
