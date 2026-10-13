"""Call outcomes and parent message templates (English / Hindi / Marathi).

Messages are previews only: nothing is sent. Templates should be checked by the
school before any real sending is switched on.
"""
from __future__ import annotations

from decimal import Decimal

# Same outcomes as the Fees Calling Desk buttons.
OUTCOMES = {
    "will_pay": "Will pay",
    "gave_date": "Gave a date",
    "already_paid": "Says already paid",   # NOT marked paid: office must verify in Excel
    "call_later": "Call later",
    "no_answer": "No answer",
    "phone_off": "Phone off",
    "wrong_number": "Wrong number",
    "complaint": "Has a complaint",
}
NEEDS_DATE = {"gave_date"}

LANGS = {"en": "English", "hi": "हिन्दी", "mr": "मराठी"}

TEMPLATES = {
    "en": ("Dear Parent, this is a reminder from {school}. Fees of {amount} are pending for "
           "{name} ({klass}). Please pay at the school office at the earliest. "
           "If already paid, please ignore this message. Thank you."),
    "hi": ("आदरणीय पालक, {school} की ओर से सूचना: {name} ({klass}) की {amount} फीस बकाया है। "
           "कृपया जल्द से जल्द स्कूल कार्यालय में भुगतान करें। "
           "यदि भुगतान हो चुका है, तो कृपया इस संदेश को अनदेखा करें। धन्यवाद।"),
    "mr": ("आदरणीय पालक, {school} कडून सूचना: {name} ({klass}) यांची {amount} फी बाकी आहे. "
           "कृपया लवकरात लवकर शाळेच्या कार्यालयात फी भरावी. "
           "फी भरली असल्यास या संदेशाकडे दुर्लक्ष करावे. धन्यवाद."),
}


def rupees(v) -> str:
    n = int(Decimal(str(v or 0)).quantize(Decimal("1")))
    s = str(n)
    if len(s) > 3:  # Indian grouping: 1,23,456
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return "₹" + s


def render(lang: str, *, school: str, name: str, klass: str, amount) -> str:
    if lang not in TEMPLATES:
        raise ValueError(f"lang must be one of {list(TEMPLATES)}")
    return TEMPLATES[lang].format(school=school, name=name, klass=klass, amount=rupees(amount))
