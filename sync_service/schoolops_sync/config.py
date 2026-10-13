"""Configuration loading and safety checks.

The column mapping and payment rules are NOT guessed. They live in config.toml and
must be confirmed by the school (mapping_confirmed / payment_rules.confirmed) before
the service will touch a real school folder.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Internal field names the sync understands. Each maps to an exact Excel heading.
KNOWN_FIELDS = (
    "student_id",      # optional: GR no. / admission no. If mapped, used as identity.
    "sr_no",
    "student_name",
    "class_label",
    "contact",
    "total_fee",
    "discount",
    "paid",
    "balance",
)
REQUIRED_FIELDS = ("student_name", "balance")

# Marker file that the synthetic-data generator writes. Test mode only runs in
# folders containing it, so test mode can never be pointed at a real school folder.
SYNTHETIC_MARKER = ".synthetic-test-data"


class ConfigError(Exception):
    pass


class NotAllowed(Exception):
    """Raised when a safety rule blocks a sync."""


@dataclass
class Classifier:
    source: str  # "file" | "sheet" | "class_column"
    regex: str


@dataclass
class Config:
    watch_folder: Path
    database_url: str
    dry_run: bool = True
    debounce_seconds: float = 5.0
    reconcile_minutes: float = 30.0
    file_patterns: tuple[str, ...] = ("*.xlsx", "*.xlsm")
    log_file: str = "logs/sync.log"
    polling: bool = False  # true for network/shared drives where file events are unreliable
    school_name: str = "School"
    header_row: int = 2
    first_data_row: int = 3
    mapping_confirmed: bool = False
    test_mode: bool = False
    columns: dict[str, str] = field(default_factory=dict)
    std: Classifier | None = None
    section: Classifier | None = None
    medium: Classifier | None = None
    payment_rules_confirmed: bool = False
    balance_increase: str = "review"
    paid_decrease: str = "review"

    # ---- safety -------------------------------------------------------
    def rule(self, name: str) -> str:
        """Payment rules only take effect once confirmed; until then everything
        that could look like money moving backwards goes to review."""
        if not self.payment_rules_confirmed:
            return "review"
        return getattr(self, name)

    def assert_sync_allowed(self, folder: Path | None = None) -> None:
        folder = Path(folder or self.watch_folder)
        if self.test_mode:
            if not (folder / SYNTHETIC_MARKER).exists():
                raise NotAllowed(
                    f"test_mode is on, but {folder} is not a synthetic test folder "
                    f"(no {SYNTHETIC_MARKER} file). Test mode never runs on real data."
                )
            return
        if not self.mapping_confirmed:
            raise NotAllowed(
                "The Excel column mapping has not been confirmed yet. Fill in [columns] "
                "and [classification] in config.toml, check them with the school, then set "
                "mapping_confirmed = true."
            )


def _classifier(raw: dict | None, name: str) -> Classifier | None:
    if not raw:
        return None
    src = raw.get("from", "")
    if src not in ("file", "sheet", "class_column"):
        raise ConfigError(f"classification.{name}.from must be file, sheet or class_column")
    if not raw.get("regex"):
        raise ConfigError(f"classification.{name}.regex is required")
    return Classifier(source=src, regex=raw["regex"])


def load_config(path: str | os.PathLike) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    svc, layout = raw.get("service", {}), raw.get("layout", {})
    cols = {k: str(v).strip() for k, v in raw.get("columns", {}).items() if str(v).strip()}
    unknown = set(cols) - set(KNOWN_FIELDS)
    if unknown:
        raise ConfigError(f"Unknown column fields in [columns]: {sorted(unknown)}")
    missing = [f for f in REQUIRED_FIELDS if f not in cols]
    if missing:
        raise ConfigError(f"[columns] must map {missing}")
    cls = raw.get("classification", {})
    pay = raw.get("payment_rules", {})
    for k in ("balance_increase", "paid_decrease"):
        if pay.get(k, "review") not in ("review", "apply"):
            raise ConfigError(f"payment_rules.{k} must be 'review' or 'apply'")
    cfg = Config(
        watch_folder=Path(os.environ.get("SCHOOLOPS_WATCH_FOLDER", svc.get("watch_folder", ""))).expanduser(),
        database_url=os.environ.get("DATABASE_URL", svc.get("database_url", "")),
        dry_run=bool(svc.get("dry_run", True)),
        debounce_seconds=float(svc.get("debounce_seconds", 5)),
        reconcile_minutes=float(svc.get("reconcile_minutes", 30)),
        file_patterns=tuple(svc.get("file_patterns", ["*.xlsx", "*.xlsm"])),
        log_file=svc.get("log_file", "logs/sync.log"),
        polling=bool(svc.get("polling", False)),
        school_name=svc.get("school_name", "School"),
        header_row=int(layout.get("header_row", 2)),
        first_data_row=int(layout.get("first_data_row", 3)),
        mapping_confirmed=bool(layout.get("mapping_confirmed", False)),
        test_mode=bool(layout.get("test_mode", False)),
        columns=cols,
        std=_classifier(cls.get("std"), "std"),
        section=_classifier(cls.get("section"), "section"),
        medium=_classifier(cls.get("medium"), "medium"),
        payment_rules_confirmed=bool(pay.get("confirmed", False)),
        balance_increase=pay.get("balance_increase", "review"),
        paid_decrease=pay.get("paid_decrease", "review"),
    )
    if not cfg.database_url:
        raise ConfigError("service.database_url (or DATABASE_URL) is required")
    if cfg.first_data_row <= cfg.header_row:
        raise ConfigError("layout.first_data_row must come after layout.header_row")
    return cfg
