"""Message providers: WhatsApp Business Cloud API and MSG91 (India, DLT) - plus a simulated provider.

Safety rules enforced by `get_provider`:
* test_mode (synthetic data)  -> ALWAYS the simulated provider. No request ever leaves the computer.
* real data                   -> only if [messaging] enabled = true AND the channel is enabled AND
                                 its credentials are present. Otherwise sending is refused.
Credentials are read from environment variables, never from files in Git.

Business-initiated WhatsApp messages must use a template approved by Meta. SMS in India must use a
DLT-registered template (configured in MSG91). The app therefore sends the approved template name/id
plus variables filled from real data (children, amounts, school name) - not free text.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass

import httpx

META_GRAPH = "https://graph.facebook.com"
MSG91_URL = "https://control.msg91.com/api/v5/flow"


class ProviderError(Exception):
    pass


class NotConfigured(ProviderError):
    pass


@dataclass
class SendResult:
    provider: str
    message_id: str
    status: str  # accepted | simulated


def to_e164_india(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    if len(d) != 10 or d[0] not in "6789":
        raise ProviderError(f"Not a valid Indian mobile number: {phone!r}")
    return "91" + d


def clean_param(v: str) -> str:
    """WhatsApp template parameters may not contain new lines, tabs or more than 4 spaces in a row."""
    return re.sub(r" {4,}", "   ", re.sub(r"[\r\n\t]+", "; ", str(v))).strip()


class SimulatedProvider:
    name = "simulated"

    def __init__(self, channel: str):
        self.channel = channel

    def send(self, to: str, template: dict, variables: list[str]) -> SendResult:
        to_e164_india(to)
        return SendResult(self.name, f"sim-{self.channel}-{uuid.uuid4().hex[:16]}", "simulated")

    def verify(self) -> dict:
        return {"ok": True, "simulated": True}


class WhatsAppCloud:
    name = "whatsapp_cloud"

    def __init__(self, conf: dict, *, transport: httpx.BaseTransport | None = None):
        self.phone_number_id = conf.get("phone_number_id") or ""
        self.version = conf.get("api_version", "v23.0")
        self.token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        if not (self.phone_number_id and self.token):
            raise NotConfigured("WhatsApp: set [whatsapp] phone_number_id and the WHATSAPP_ACCESS_TOKEN environment variable.")
        self.http = httpx.Client(transport=transport, timeout=30)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def verify(self) -> dict:
        """Read-only check of the WhatsApp number (no message is sent)."""
        r = self.http.get(f"{META_GRAPH}/{self.version}/{self.phone_number_id}",
                          params={"fields": "display_phone_number,verified_name,quality_rating"}, headers=self._h())
        if r.status_code >= 400:
            raise ProviderError(f"WhatsApp check failed ({r.status_code}): {_meta_error(r)}")
        return {"ok": True, **r.json()}

    def send(self, to: str, template: dict, variables: list[str]) -> SendResult:
        if not template.get("provider_template"):
            raise NotConfigured("WhatsApp: no approved Meta template name set for this language.")
        body = {"messaging_product": "whatsapp", "to": to_e164_india(to), "type": "template",
                "template": {"name": template["provider_template"],
                             "language": {"code": template.get("provider_lang") or template["lang"]},
                             "components": [{"type": "body", "parameters": [
                                 {"type": "text", "text": clean_param(v)} for v in variables]}]}}
        r = self.http.post(f"{META_GRAPH}/{self.version}/{self.phone_number_id}/messages", json=body, headers=self._h())
        if r.status_code >= 400:
            raise ProviderError(f"WhatsApp refused ({r.status_code}): {_meta_error(r)}")
        mid = (r.json().get("messages") or [{}])[0].get("id")
        if not mid:
            raise ProviderError("WhatsApp gave no message id.")
        return SendResult(self.name, mid, "accepted")


def _meta_error(r: httpx.Response) -> str:
    try:
        e = r.json().get("error", {})
        return f"{e.get('message')} (code {e.get('code')})"
    except Exception:
        return r.text[:200]


class Msg91Sms:
    name = "msg91"

    def __init__(self, conf: dict, *, transport: httpx.BaseTransport | None = None):
        self.authkey = os.environ.get("MSG91_AUTHKEY", "")
        self.sender = conf.get("sender_id", "")
        self.url = conf.get("api_url", MSG91_URL)
        self.id_field = conf.get("template_field", "template_id")  # older accounts use "flow_id"
        self.route = str(conf.get("route", "4"))  # 4 = transactional
        if not (self.authkey and self.sender):
            raise NotConfigured("SMS: set [sms] sender_id (your DLT header) and the MSG91_AUTHKEY environment variable.")
        self.http = httpx.Client(transport=transport, timeout=30)

    def verify(self) -> dict:
        # MSG91 has no read-only "who am I" call we rely on; credentials are proven by the first test SMS.
        return {"ok": True, "checked": "configuration only - send a test SMS to your own number to prove it"}

    def send(self, to: str, template: dict, variables: list[str]) -> SendResult:
        if not template.get("provider_template"):
            raise NotConfigured("SMS: no MSG91 template id (DLT-approved) set for this language.")
        rec = {"mobiles": to_e164_india(to)}
        rec.update({f"VAR{i}": str(v) for i, v in enumerate(variables, 1)})
        body = {self.id_field: template["provider_template"], "sender": self.sender, "short_url": "0",
                "route": self.route, "unicode": "1" if template["lang"] in ("hi", "mr") else "0", "recipients": [rec]}
        r = self.http.post(self.url, json=body, headers={"authkey": self.authkey, "accept": "application/json"})
        try:
            d = r.json()
        except Exception:
            raise ProviderError(f"MSG91 returned an unexpected answer ({r.status_code}).")
        if r.status_code >= 400 or d.get("type") != "success":
            raise ProviderError(f"MSG91 refused: {d.get('message') or r.status_code}")
        return SendResult(self.name, str(d.get("message")), "accepted")


def get_provider(cfg, channel: str, *, transport: httpx.BaseTransport | None = None):
    """The ONLY way the app obtains a provider. See the safety rules at the top of this file."""
    if cfg.test_mode:
        return SimulatedProvider(channel)
    if not cfg.messaging_enabled:
        raise NotConfigured("Real sending is switched off ([messaging] enabled = false).")
    if not cfg.mapping_confirmed:
        raise NotConfigured("The Excel column mapping is not confirmed yet, so real data is not trusted for sending.")
    if channel == "whatsapp":
        conf = cfg.raw.get("whatsapp", {})
        if not conf.get("enabled"):
            raise NotConfigured("WhatsApp is switched off ([whatsapp] enabled = false).")
        return WhatsAppCloud(conf, transport=transport)
    if channel == "sms":
        conf = cfg.raw.get("sms", {})
        if not conf.get("enabled"):
            raise NotConfigured("SMS is switched off ([sms] enabled = false).")
        if conf.get("provider", "msg91") != "msg91":
            raise NotConfigured("Only MSG91 is supported for SMS.")
        return Msg91Sms(conf, transport=transport)
    raise NotConfigured(f"Unknown channel {channel}")


# ------------------------------------------------------------------ webhooks (delivery status)
WA_STATUS = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}
MSG91_STATUS = {0: "sent", 1: "delivered", 2: "failed", 9: "failed", 16: "failed", 17: "failed", 20: "failed", 25: "failed"}


def check_meta_signature(body: bytes, header: str | None) -> bool:
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret or not header or not header.startswith("sha256="):
        return False
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, header.split("=", 1)[1])


def parse_whatsapp_statuses(payload: dict) -> list[dict]:
    out = []
    for e in payload.get("entry", []):
        for ch in e.get("changes", []):
            for st in ch.get("value", {}).get("statuses", []) or []:
                if st.get("status") in WA_STATUS:
                    err = (st.get("errors") or [{}])[0]
                    out.append({"id": st.get("id"), "status": WA_STATUS[st["status"]],
                                "error": err.get("title") or err.get("message")})
    return out


def parse_msg91_report(payload) -> list[dict]:
    items = payload if isinstance(payload, list) else [payload]
    out = []
    for it in items:
        try:
            code = int(it.get("status"))
        except (TypeError, ValueError):
            continue
        if code in MSG91_STATUS and it.get("requestId"):
            out.append({"id": str(it["requestId"]), "status": MSG91_STATUS[code],
                        "phone": it.get("telNum"), "error": it.get("failureReason")})
    return out
