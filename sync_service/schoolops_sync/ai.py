"""AI-written reminder wording (English / Hindi / Marathi) with Claude.

Privacy and accuracy:
* The AI never sees any student, parent, phone or fee data. It only writes the wording, with the
  placeholders {children}, {total} and {school}. The app fills these with real values locally.
* Wording with numbers, amounts, dates, links or extra placeholders is rejected, so nothing can be
  invented. Staff edit and approve every template before it can be used.
* WhatsApp and Indian SMS (DLT) need the exact wording registered with Meta / your DLT portal.
  `provider_formats` shows the text to paste there ({{1}} style for WhatsApp, {#var#} for DLT).
"""
from __future__ import annotations

import os
import re

import httpx

PLACEHOLDERS = ("children", "total", "school")
LANG_NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi"}
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
LIMITS = {"whatsapp": 900, "sms": 300}

BUILTIN = {
    "en": "Dear Parent, this is a gentle reminder from {school}. Fees are pending for {children}. Total due: {total}. "
          "Please pay at the school office at your earliest convenience. If you have already paid, please ignore this message. Thank you.",
    "hi": "आदरणीय पालक, {school} की ओर से विनम्र सूचना। {children} की फीस बकाया है। कुल बकाया: {total}। "
          "कृपया सुविधानुसार जल्द ही स्कूल कार्यालय में भुगतान करें। यदि भुगतान हो चुका है, तो कृपया इस संदेश को अनदेखा करें। धन्यवाद।",
    "mr": "आदरणीय पालक, {school} कडून नम्र सूचना. {children} यांची फी बाकी आहे. एकूण बाकी: {total}. "
          "कृपया सोयीनुसार लवकरात लवकर शाळेच्या कार्यालयात फी भरावी. फी भरली असल्यास या संदेशाकडे दुर्लक्ष करावे. धन्यवाद.",
}


class AIError(Exception):
    pass


def placeholders_in(body: str) -> list[str]:
    return re.findall(r"\{([a-z_]+)\}", body)


def validate(body: str, channel: str) -> list[str]:
    probs = []
    found = placeholders_in(body)
    for p in PLACEHOLDERS:
        n = found.count(p)
        if n != 1:
            probs.append(f"{{{p}}} must appear exactly once (found {n}).")
    extra = sorted(set(found) - set(PLACEHOLDERS))
    if extra:
        probs.append(f"Unknown placeholders: {', '.join('{' + e + '}' for e in extra)}.")
    plain = re.sub(r"\{[a-z_]+\}", "", body)
    if re.search(r"[0-9०-९]", plain):
        probs.append("Must not contain numbers (amounts, dates and phone numbers come from real data only).")
    if re.search(r"₹|\bRs\.?\b|INR|https?://|www\.", plain, re.I):
        probs.append("Must not contain amounts or links.")
    if "{" in plain or "}" in plain:
        probs.append("Stray { or } characters.")
    if len(body) > LIMITS.get(channel, 900):
        probs.append(f"Too long for {channel} ({len(body)} characters, limit {LIMITS[channel]}).")
    if not body.strip():
        probs.append("Empty.")
    return probs


def render(body: str, *, children: str, total: str, school: str) -> str:
    return body.replace("{children}", children).replace("{total}", total).replace("{school}", school)


def variables(body: str, values: dict) -> list[str]:
    """Values in the order the placeholders appear - the order the Meta/DLT template must use."""
    return [values[p] for p in placeholders_in(body)]


def provider_formats(body: str) -> dict:
    order = placeholders_in(body)
    wa, dlt = body, body
    for i, p in enumerate(order, 1):
        wa = wa.replace("{" + p + "}", "{{" + str(i) + "}}")
        dlt = dlt.replace("{" + p + "}", "{#var#}")
    return {"whatsapp_template_text": wa, "dlt_template_text": dlt, "variable_order": order}


def generate(cfg, channel: str, lang: str, tone: str = "polite", *, transport: httpx.BaseTransport | None = None) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise AIError("AI is not set up: set the ANTHROPIC_API_KEY environment variable (or write the wording yourself).")
    if lang not in LANG_NAMES or channel not in LIMITS:
        raise AIError("Unknown language or channel.")
    conf = cfg.raw.get("ai", {})
    limit = 160 if channel == "sms" and lang == "en" else (140 if channel == "sms" else 600)
    prompt = (
        f"Write ONE {tone}, respectful school fee reminder message template in {LANG_NAMES[lang]} "
        f"for parents in India, to be sent by {'WhatsApp' if channel == 'whatsapp' else 'SMS'}.\n"
        "Rules:\n"
        "- Use these placeholders exactly once each, written exactly like this: {school} (school name), "
        "{children} (child name(s) with class and amount), {total} (total amount due).\n"
        "- Do not write any numbers, amounts, currency symbols, dates, deadlines, phone numbers or links.\n"
        "- No threats, penalties or pressure. Say to ignore the message if already paid.\n"
        f"- About {limit} characters or fewer. Plain text, no emojis, no markdown, no quotes.\n"
        "Reply with only the message template.")
    try:
        r = httpx.Client(transport=transport, timeout=60).post(
            API_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": conf.get("model", DEFAULT_MODEL), "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]})
    except httpx.HTTPError as exc:
        raise AIError(f"Could not reach the AI service: {exc.__class__.__name__}")
    if r.status_code >= 400:
        raise AIError(f"AI service error ({r.status_code}). Check ANTHROPIC_API_KEY and [ai] model.")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip().strip('"')
    probs = validate(text, channel)
    if probs:
        raise AIError("The AI wording did not pass the safety checks: " + " ".join(probs) + " Try again or edit it.")
    return text
