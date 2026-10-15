"""Microsoft Graph (OneDrive / SharePoint) -> local read-only mirror -> existing safe Excel sync.

* READ-ONLY: GraphClient has no method that can write. It only sends GET requests, and the
  permissions requested are read permissions (Files.Read.All). Excel files in OneDrive are
  never changed, moved or deleted by this app.
* Files are downloaded into `watch_folder` (the mirror). Each changed file is then processed by
  the normal sync (validation, dry run, review, duplicate protection).
* Change detection: each file's cTag (content tag). Renames/moves are detected by the stable
  OneDrive item id. Files removed in OneDrive are moved to `<mirror>/.removed/` locally
  (students are never deleted).
* Throttling (429/503/504) is retried with the server's Retry-After.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

import httpx
from psycopg.types.json import Jsonb

from .config import Config, NotAllowed
from .db import connect
from .sync import is_workbook_name, sync_file

log = logging.getLogger("schoolops.onedrive")
GRAPH = "https://graph.microsoft.com/v1.0"
DELEGATED_SCOPES = ["Files.Read.All", "User.Read"]  # read-only files + who signed in
APP_SCOPES = ["https://graph.microsoft.com/.default"]  # app-only: grant Files.Read.All / Sites.Read.All (read)
REMOVED_DIR = ".removed"


class OneDriveError(Exception):
    pass


class NotConnected(OneDriveError):
    pass


@dataclass
class OneDriveConfig:
    enabled: bool = False
    auth: str = "device_code"          # device_code (sign in once as a user) | client_credentials (app-only)
    tenant_id: str = "organizations"   # your tenant id/domain; "consumers" for personal Microsoft accounts
    client_id: str = ""
    drive_id: str = ""                 # empty = the signed-in user's own OneDrive
    folder_path: str = ""              # folder inside that drive, e.g. "School/Fees"
    folder_item_id: str = ""           # OR: id of a folder shared with you (then drive_id is required)
    recursive: bool = True
    poll_minutes: float = 10
    token_cache: str = "data/onedrive_token.json"

    @classmethod
    def from_raw(cls, raw: dict | None) -> "OneDriveConfig":
        raw = raw or {}
        c = cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})
        if c.auth not in ("device_code", "client_credentials"):
            raise ValueError("onedrive.auth must be device_code or client_credentials")
        if c.folder_item_id and not c.drive_id:
            raise ValueError("onedrive.folder_item_id needs onedrive.drive_id")
        return c


# ---------------------------------------------------------------------------------- auth
class MsalTokenProvider:
    """Gets Graph access tokens with MSAL. Tokens are cached in a local file (git-ignored, mode 600)."""

    def __init__(self, oc: OneDriveConfig):
        import msal

        if not oc.client_id:
            raise NotConnected("OneDrive is not set up: onedrive.client_id is empty.")
        self.oc = oc
        self.cache = msal.SerializableTokenCache()
        self.cache_path = Path(oc.token_cache)
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        authority = f"https://login.microsoftonline.com/{oc.tenant_id}"
        if oc.auth == "client_credentials":
            secret = os.environ.get("ONEDRIVE_CLIENT_SECRET")
            if not secret:
                raise NotConnected("Set the ONEDRIVE_CLIENT_SECRET environment variable.")
            self.app = msal.ConfidentialClientApplication(oc.client_id, authority=authority,
                                                          client_credential=secret, token_cache=self.cache)
        else:
            self.app = msal.PublicClientApplication(oc.client_id, authority=authority, token_cache=self.cache)

    def _save(self):
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())
            os.chmod(self.cache_path, 0o600)

    def __call__(self) -> str:
        if self.oc.auth == "client_credentials":
            res = self.app.acquire_token_for_client(scopes=APP_SCOPES)
        else:
            accounts = self.app.get_accounts()
            res = self.app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0]) if accounts else None
            if not res:
                raise NotConnected("Not signed in to OneDrive. Run: python -m schoolops_sync --config config.toml onedrive-login")
        self._save()
        if "access_token" not in res:
            raise NotConnected(f"OneDrive sign-in failed: {res.get('error_description') or res.get('error')}")
        return res["access_token"]

    def login_device_code(self, show: Callable[[str], None] = print) -> str:
        if self.oc.auth != "device_code":
            raise OneDriveError("Login is only needed for auth = device_code.")
        flow = self.app.initiate_device_flow(scopes=DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise OneDriveError(f"Could not start sign-in: {flow.get('error_description')}")
        show(flow["message"])
        res = self.app.acquire_token_by_device_flow(flow)
        self._save()
        if "access_token" not in res:
            raise NotConnected(f"Sign-in failed: {res.get('error_description') or res.get('error')}")
        return res.get("id_token_claims", {}).get("preferred_username", "signed in")


# ---------------------------------------------------------------------------------- client
class GraphClient:
    """GET-only Microsoft Graph client."""

    def __init__(self, token_provider: Callable[[], str], *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 4):
        self.token = token_provider
        self.http = httpx.Client(transport=transport, timeout=60, follow_redirects=True)
        self.sleep = sleep
        self.max_retries = max_retries
        self.requests = 0

    def get(self, url: str, *, auth: bool = True) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            headers = {"Authorization": f"Bearer {self.token()}"} if auth else {}
            self.requests += 1
            r = self.http.request("GET", url, headers=headers)  # the only HTTP verb this app uses
            if r.status_code in (429, 503, 504) and attempt < self.max_retries:
                wait = min(float(r.headers.get("Retry-After", 2 ** attempt)), 60)
                log.warning("Graph throttled (%s); waiting %ss", r.status_code, wait)
                self.sleep(wait)
                continue
            if r.status_code == 401:
                raise NotConnected("OneDrive sign-in expired or not allowed. Sign in again.")
            if r.status_code == 403:
                raise OneDriveError("No permission to read this OneDrive folder (403). Check sharing and app permissions.")
            if r.status_code == 404:
                raise OneDriveError("OneDrive folder not found (404). Check onedrive.folder_path / folder_item_id.")
            r.raise_for_status()
            return r
        raise OneDriveError("OneDrive kept refusing requests (throttled). Will try again later.")

    def children(self, url: str) -> Iterator[dict]:
        while url:
            data = self.get(url).json()
            yield from data.get("value", [])
            url = data.get("@odata.nextLink")

    def download(self, item: dict) -> bytes:
        url = item.get("@microsoft.graph.downloadUrl")
        if url:  # short-lived pre-authenticated link: no token needed
            return self.get(url, auth=False).content
        drive = item.get("parentReference", {}).get("driveId")
        return self.get(f"{GRAPH}/drives/{drive}/items/{item['id']}/content").content


def folder_children_url(oc: OneDriveConfig) -> str:
    if oc.folder_item_id:
        return f"{GRAPH}/drives/{oc.drive_id}/items/{oc.folder_item_id}/children"
    drive = f"drives/{oc.drive_id}" if oc.drive_id else "me/drive"
    path = oc.folder_path.strip("/")
    return f"{GRAPH}/{drive}/root:/{quote(path)}:/children" if path else f"{GRAPH}/{drive}/root/children"


_SAFE = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def _safe_part(name: str) -> str:
    name = _SAFE.sub("_", name).strip(" .")
    return name if name and name not in (".", "..") else "_"


def walk(client: GraphClient, url: str, oc: OneDriveConfig, patterns, rel: str = "") -> Iterator[tuple[dict, str]]:
    for item in client.children(url):
        name = _safe_part(item.get("name", ""))
        if "folder" in item and oc.recursive and not name.startswith("."):
            drive = item.get("parentReference", {}).get("driveId") or oc.drive_id
            sub = f"{GRAPH}/drives/{drive}/items/{item['id']}/children"
            yield from walk(client, sub, oc, patterns, f"{rel}{name}/")
        elif "file" in item and is_workbook_name(name, patterns):
            yield item, f"{rel}{name}"


# ---------------------------------------------------------------------------------- pull + sync
def _write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".download-{uuid.uuid4().hex}.tmp"
    tmp.write_bytes(data)
    os.chmod(tmp, 0o444)  # mirror copies are read-only: edit the file in OneDrive, not here
    os.replace(tmp, dest)


def _move_aside(mirror: Path, rel: str) -> None:
    src = mirror / rel
    if src.exists():
        dst = mirror / REMOVED_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(rel).name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)


def pull(conn, cfg: Config, oc: OneDriveConfig, client: GraphClient) -> dict:
    """Bring the local mirror up to date. Returns which files changed."""
    from .imports import is_synthetic_file

    mirror = Path(cfg.watch_folder)
    mirror.mkdir(parents=True, exist_ok=True)
    known = {r["item_id"]: r for r in conn.execute("SELECT * FROM onedrive_items").fetchall()}
    seen, changed, renamed, blocked, unchanged = set(), [], [], [], 0
    for item, rel in walk(client, folder_children_url(oc), oc, cfg.file_patterns):
        iid = item["id"]
        seen.add(iid)
        ctag = item.get("cTag") or item.get("eTag")
        dest = mirror / rel
        k = known.get(iid)
        was_renamed = bool(k and not k["deleted_at"] and k["rel_path"] != rel)
        if was_renamed:
            renamed.append({"from": k["rel_path"], "to": rel})
            old = mirror / k["rel_path"]
            if old.exists() and k["ctag"] == ctag and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, dest)  # same content, new name: no download needed
            else:
                _move_aside(mirror, k["rel_path"])
        if not k or k["ctag"] != ctag or k["deleted_at"] or not dest.exists():
            _write_atomic(dest, client.download(item))
            if cfg.test_mode and not is_synthetic_file(dest):
                os.chmod(dest, 0o644)
                dest.unlink()
                blocked.append(rel)
                continue
            changed.append(rel)
        elif was_renamed:
            changed.append(rel)  # process so students are linked to the new file name
        else:
            unchanged += 1
        conn.execute(
            """INSERT INTO onedrive_items (item_id, name, rel_path, ctag, size, modified_at, downloaded_at, deleted_at)
               VALUES (%s,%s,%s,%s,%s,%s,now(),NULL)
               ON CONFLICT (item_id) DO UPDATE SET name=EXCLUDED.name, rel_path=EXCLUDED.rel_path, ctag=EXCLUDED.ctag,
                 size=EXCLUDED.size, modified_at=EXCLUDED.modified_at, deleted_at=NULL,
                 downloaded_at=CASE WHEN onedrive_items.ctag IS DISTINCT FROM EXCLUDED.ctag THEN now()
                                    ELSE onedrive_items.downloaded_at END""",
            (iid, item.get("name"), rel, ctag, item.get("size"), item.get("lastModifiedDateTime")))
    removed = []
    for iid, k in known.items():
        if iid not in seen and not k["deleted_at"]:
            _move_aside(mirror, k["rel_path"])
            conn.execute("UPDATE onedrive_items SET deleted_at=now() WHERE item_id=%s", (iid,))
            removed.append(k["rel_path"])
    return {"downloaded_or_renamed": changed, "renamed": renamed, "removed": removed,
            "blocked_not_synthetic": blocked, "unchanged": unchanged, "graph_requests": client.requests}


def save_status(conn, status: dict) -> None:
    conn.execute("""INSERT INTO settings (key, value) VALUES ('onedrive_status', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (Jsonb(status),))


def sync_from_onedrive(conn, cfg: Config, oc: OneDriveConfig, client: GraphClient, *,
                       dry_run: bool | None = None) -> dict:
    cfg.assert_sync_allowed(Path(cfg.watch_folder))  # nothing is downloaded before the safety checks pass
    started = datetime.now(timezone.utc).isoformat()
    try:
        res = pull(conn, cfg, oc, client)
        mirror = Path(cfg.watch_folder)
        res["sync"] = [sync_file(conn, cfg, mirror / rel, dry_run=dry_run, trigger="onedrive")
                       for rel in res["downloaded_or_renamed"]]
        save_status(conn, {"last_run": started, "ok": True, "error": None,
                           "files_changed": len(res["downloaded_or_renamed"]), "removed": len(res["removed"]),
                           "blocked": len(res["blocked_not_synthetic"])})
        return res
    except (OneDriveError, httpx.HTTPError) as exc:
        save_status(conn, {"last_run": started, "ok": False, "error": str(exc)})
        raise


class OneDrivePoller:
    """Runs sync_from_onedrive every poll_minutes in the background (used by `serve`)."""

    def __init__(self, cfg: Config, oc: OneDriveConfig, client_factory: Callable[[], GraphClient]):
        self.cfg, self.oc, self.make_client = cfg, oc, client_factory
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=30)

    def run_once(self) -> dict | None:
        try:
            with connect(self.cfg.database_url) as conn:
                return sync_from_onedrive(conn, self.cfg, self.oc, self.make_client())
        except (NotAllowed, OneDriveError, httpx.HTTPError) as exc:
            log.error("OneDrive sync skipped: %s", exc)
        except Exception:
            log.exception("OneDrive sync error (will retry)")
        return None

    def _loop(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(max(60.0, self.oc.poll_minutes * 60))


def verify(conn, cfg: Config, oc: OneDriveConfig, client: GraphClient, sample: int = 5) -> dict:
    """Prove the live connection: who is signed in, the folder can be listed, and a few files can be
    downloaded and read. Only headings/counts are kept (no student data). Nothing is synced."""
    import tempfile

    from .inspect import inspect_folder

    account = "app-only access"
    if oc.auth == "device_code":
        me = client.get(f"{GRAPH}/me?$select=displayName,userPrincipalName").json()
        account = me.get("userPrincipalName") or me.get("displayName") or "signed in"
    items = list(walk(client, folder_children_url(oc), oc, cfg.file_patterns))
    with tempfile.TemporaryDirectory(prefix="schoolops-verify-") as tmp:
        for item, rel in items[:sample]:
            dest = Path(tmp) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(client.download(item))
        rep = inspect_folder(tmp)
    result = {"verified_at": datetime.now(timezone.utc).isoformat(), "account": account,
              "folder": oc.folder_path or oc.folder_item_id or "(root)", "excel_files": len(items),
              "file_names": [rel for _, rel in items[:50]], "sampled": min(sample, len(items)), "inspection": rep}
    conn.execute("""INSERT INTO settings (key, value) VALUES ('onedrive_verified', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (Jsonb(result),))
    return result


def status(conn, cfg: Config, oc: OneDriveConfig) -> dict:
    r = conn.execute("SELECT value FROM settings WHERE key='onedrive_status'").fetchone()
    n = conn.execute("SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS files FROM onedrive_items").fetchone()
    v = conn.execute("SELECT value FROM settings WHERE key='onedrive_verified'").fetchone()
    return {"enabled": oc.enabled, "auth": oc.auth, "folder": oc.folder_path or oc.folder_item_id or "(root)",
            "drive": oc.drive_id or "my OneDrive", "configured": bool(oc.client_id),
            "token_cached": Path(oc.token_cache).exists(), "files_tracked": n["files"],
            "last": r["value"] if r else None, "mirror_folder": str(cfg.watch_folder),
            # "live" is only ever true after a real verification against Microsoft succeeded
            "verified": ({k: v["value"][k] for k in ("verified_at", "account", "excel_files")} if v else None)}


__all__ = ["verify", "GraphClient", "MsalTokenProvider", "OneDriveConfig", "OneDrivePoller", "pull", "sync_from_onedrive",
           "status", "NotConnected", "OneDriveError"]
