"""WhatsApp + SMS reminder DRAFTS for whole follow-up lists. Nothing is ever sent.

* One draft per parent (family) per channel per day; siblings are combined in one message.
* Only real values from the database are used. Parents without a valid phone are skipped
  and reported. Children with nothing due are left out.
* SMS drafts are short and show how many SMS parts they need (Hindi/Marathi use Unicode: 70
  characters per part). In India, real SMS needs a DLT-registered template; WhatsApp business
  messages need an approved template. Template names can be set in [messaging] so drafts are
  ready for that, but sending stays disabled.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from .drafts import DraftError, compose
from .followups import STUDENT_STATE_SQL, _child_state, build_queue
from .messages import rupees

log = logging.getLogger("schoolops.outreach")
CHANNELS = ("whatsapp", "sms")
LISTS = {
    "missed_promise": "Missed payment promises",
    "due_today": "Follow-ups due today",
    "due_soon": "Promised to pay in the next 3 days",
    "not_called": "Not contacted yet",
    "all_unpaid": "All parents with fees due",
}
SMS = {
    "en": ("{school}: Fees pending for {kids}. Total {total}. Please pay at the school office. "
           "Ignore if already paid."),
    "hi": "{school}: {kids} की फीस बकाया। कुल {total}। कृपया कार्यालय में जमा करें। भरी हो तो अनदेखा करें।",
    "mr": "{school}: {kids} यांची फी बाकी. एकूण {total}. कृपया कार्यालयात भरावी. भरली असल्यास दुर्लक्ष करा.",
}
_GSM = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
           "¿abcdefghijklmnopqrstuvwxyzäöñüà")


class OutreachError(Exception):
    pass


def sms_parts(text: str) -> int:
    if all(c in _GSM for c in text):
        return 1 if len(text) <= 160 else math.ceil(len(text) / 153)
    n = len(text.encode("utf-16-le")) // 2
    return 1 if n <= 70 else math.ceil(n / 67)


def _phone(kids: list[dict]) -> str | None:
    nums = []
    for k in kids:
        d = re.sub(r"\D", "", k.get("contact") or "")
        if len(d) == 12 and d.startswith("91"):
            d = d[2:]
        if len(d) == 10 and d[0] in "6789":
            nums.append(d)
    return Counter(nums).most_common(1)[0][0] if nums else None


def select_families(conn, list_name: str, *, medium=None, std=None, section=None, campaign=None) -> list[dict]:
    """Families (with their children who owe fees) for one follow-up list."""
    if list_name not in LISTS:
        raise OutreachError(f"Unknown list. Choose one of: {', '.join(LISTS)}")
    if list_name in ("missed_promise", "due_today", "not_called"):
        q = build_queue(conn, medium=medium, std=std, section=section, campaign=campaign)
        return [{"family_id": f["family_id"], "kids": f["children"]} for f in q["items"] if f["priority"] == list_name]
    today = conn.execute("SELECT current_date AS d").fetchone()["d"]
    fams: dict[int, list[dict]] = {}
    flt = {k: v for k, v in (("medium", medium), ("std", std), ("section", section)) if v}
    flt.update({k: v for k, v in ((campaign or {}).get("filters") or {}).items() if v})
    for r in conn.execute(STUDENT_STATE_SQL + " ORDER BY s.id").fetchall():
        fams.setdefault(r["family_id"], []).append(dict(r))
    out = []
    for fid, kids in fams.items():
        if flt and not any(all(str(k[c]) == str(v) for c, v in flt.items()) for k in kids):
            continue
        if list_name == "due_soon":
            soon = [k for k in kids if k["fu_kind"] in ("promise", "part_payment") and k["fu_due"]
                    and today < k["fu_due"] <= today + timedelta(days=3)]
            if soon:
                out.append({"family_id": fid, "kids": kids})
        else:  # all_unpaid
            if any(_child_state(k, today) != "needs_action" for k in kids):
                out.append({"family_id": fid, "kids": kids})
    return out


def _sms_text(lang: str, school: str, kids: list[dict]) -> str:
    names = ", ".join(f"{k['name']} ({rupees(k['balance'])})" for k in kids) if len(kids) > 1 else kids[0]["name"]
    return SMS[lang].format(school=school, kids=names, total=rupees(sum(k["balance"] for k in kids)))


def prepare_batch(conn, cfg, list_name: str, *, channels=CHANNELS, lang: str = "mr", medium=None, std=None,
                  section=None, campaign=None) -> dict:
    channels = [c for c in channels if c in CHANNELS]
    if not channels:
        raise OutreachError("Choose WhatsApp, SMS or both.")
    templates = cfg.messaging or {}
    batch = uuid.uuid4().hex[:12]
    created, skipped = [], []
    for fam in select_families(conn, list_name, medium=medium, std=std, section=section, campaign=campaign):
        due = [k for k in fam["kids"] if k["balance"] is not None and k["balance"] > 0]
        label = " & ".join(k["name"] for k in fam["kids"])
        phone = _phone(fam["kids"])
        if not due:
            continue
        if not phone:
            skipped.append({"family_id": fam["family_id"], "name": label, "why": "No valid mobile number in Excel"})
            continue
        try:
            d = compose(conn, [k["id"] for k in due], lang, cfg.school_name)
        except DraftError as exc:
            skipped.append({"family_id": fam["family_id"], "name": label, "why": str(exc)})
            continue
        for ch in channels:
            body = d["body"] if ch == "whatsapp" else _sms_text(lang, cfg.school_name, due)
            tpl = (templates.get(f"{ch}_templates") or {}).get(lang)
            row = conn.execute(
                """INSERT INTO message_drafts (family_id, student_ids, lang, body, left_out, channel, phone, reason,
                                               batch_id, template_name, segments)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (family_id, channel, created_on)
                     WHERE status <> 'discarded' AND channel IN ('whatsapp','sms') AND family_id IS NOT NULL
                   DO NOTHING RETURNING id""",
                (d["family_id"], d["student_ids"], lang, body, Jsonb(d["left_out"]), ch, phone, list_name, batch,
                 tpl, sms_parts(body) if ch == "sms" else None)).fetchone()
            if row:
                created.append(row["id"])
            else:
                skipped.append({"family_id": fam["family_id"], "name": label,
                                "why": f"A {ch} draft for this parent was already prepared today"})
    return {"batch_id": batch, "list": LISTS[list_name], "created": len(created), "draft_ids": created,
            "skipped": skipped, "sending": "disabled"}


class AutoDrafts:
    """Optional: prepare drafts once a day at a set time ([messaging] auto_drafts = true).
    It only PREPARES drafts. Staff still review and approve; nothing is sent."""

    def __init__(self, cfg, connect_fn):
        m = cfg.messaging or {}
        self.cfg, self.connect = cfg, connect_fn
        self.enabled = bool(m.get("auto_drafts", False))
        self.at = datetime.strptime(m.get("auto_drafts_time", "09:00"), "%H:%M").time()
        self.lists = [x for x in m.get("auto_drafts_lists", ["missed_promise", "due_soon"]) if x in LISTS]
        self.channels = [c for c in m.get("auto_drafts_channels", list(CHANNELS)) if c in CHANNELS]
        self.lang = m.get("auto_drafts_language", "mr")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def run_if_due(self, now: datetime) -> dict | None:
        if not self.enabled or now.time() < self.at:
            return None
        with self.connect(self.cfg.database_url) as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(7310003)")
                last = conn.execute("SELECT value FROM settings WHERE key='auto_drafts_last'").fetchone()
                if last and last["value"] == now.date().isoformat():
                    return None
                res = {ln: prepare_batch(conn, self.cfg, ln, channels=self.channels, lang=self.lang)
                       for ln in self.lists}
                conn.execute("""INSERT INTO settings (key, value) VALUES ('auto_drafts_last', %s)
                                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                             (Jsonb(now.date().isoformat()),))
        log.info("Prepared daily reminder drafts: %s", {k: v["created"] for k, v in res.items()})
        return res

    def start(self):
        if self.enabled:
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(60):
            try:
                self.run_if_due(datetime.now())
            except Exception:
                log.exception("Daily drafts failed (will retry)")


__all__ = ["prepare_batch", "select_families", "sms_parts", "AutoDrafts", "LISTS", "CHANNELS", "OutreachError"]
