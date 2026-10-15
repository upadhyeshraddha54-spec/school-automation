"""Watch the shared school folder and sync workbooks after they are saved.

* Debounce: Excel/WPS write a file in several steps (temp file, rename, re-save). We wait
  until no event has arrived for `debounce_seconds` AND the file size/mtime are stable.
* Reconciliation: every `reconcile_minutes` the whole folder is re-checked, in case an
  event was missed (network drives, laptop asleep, service restarted).
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .config import Config, NotAllowed
from .db import connect
from .sync import is_workbook_name, sync_file, sync_folder

log = logging.getLogger("schoolops.watcher")


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FolderWatcher"):
        self.w = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        for p in (getattr(event, "dest_path", None), event.src_path):
            if p and is_workbook_name(Path(p).name, self.w.cfg.file_patterns):
                self.w.touch(Path(p))


class FolderWatcher:
    def __init__(self, cfg: Config, *, polling: bool = False):
        self.cfg = cfg
        self.folder = Path(cfg.watch_folder)
        self._pending: dict[Path, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # PollingObserver works on network shares (SMB) where native events are unreliable.
        self._observer = PollingObserver(timeout=1) if polling else Observer()
        self._threads: list[threading.Thread] = []
        self.last_results: list[dict] = []

    def touch(self, path: Path) -> None:
        with self._lock:
            self._pending[path] = time.monotonic()

    def start(self) -> None:
        self.cfg.assert_sync_allowed(self.folder)  # refuse to watch a real folder before mapping is confirmed
        self._observer.schedule(_Handler(self), str(self.folder), recursive=self.cfg.recursive)
        self._observer.start()
        for target in (self._debounce_loop, self._reconcile_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Watching %s (dry_run=%s)", self.folder, self.cfg.dry_run)

    def stop(self) -> None:
        """Stop watching. Waits for a sync that is already running to finish cleanly."""
        self._stop.set()
        self._observer.stop()
        self._observer.join(timeout=5)
        for t in self._threads:
            t.join(timeout=30)

    @staticmethod
    def _fingerprint(p: Path):
        try:
            st = p.stat()
            return st.st_size, st.st_mtime_ns
        except FileNotFoundError:
            return None

    def _debounce_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.25)
            now = time.monotonic()
            with self._lock:
                ready = [p for p, t in self._pending.items() if now - t >= self.cfg.debounce_seconds]
            for p in ready:
                fp1 = self._fingerprint(p)
                time.sleep(0.5)
                fp2 = self._fingerprint(p)
                with self._lock:
                    if fp1 is None or fp1 != fp2:  # deleted, or still being written: wait for more events
                        if fp1 is None:
                            self._pending.pop(p, None)
                        else:
                            self._pending[p] = time.monotonic()
                        continue
                    self._pending.pop(p, None)
                self._run(lambda conn: sync_file(conn, self.cfg, p, trigger="watcher"))

    def _reconcile_loop(self) -> None:
        interval = max(60.0, self.cfg.reconcile_minutes * 60)
        while not self._stop.wait(interval):
            self.reconcile()

    def reconcile(self) -> dict | None:
        return self._run(lambda conn: sync_folder(conn, self.cfg, trigger="reconcile"))

    def _run(self, fn):
        try:
            with connect(self.cfg.database_url) as conn:
                res = fn(conn)
                self.last_results = (self.last_results + [res])[-20:]
                return res
        except NotAllowed as exc:
            log.error("Sync blocked: %s", exc)
        except Exception:
            log.exception("Sync error (the service keeps running)")
        return None
