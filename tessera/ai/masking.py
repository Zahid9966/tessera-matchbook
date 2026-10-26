"""Redact identifiers before text reaches any model. Amounts and dates are kept (the model needs them);
account/card/phone numbers, long reference numbers, emails and UPI handles are not.

`leaks()` is the independent check the gateway runs in strict mode: masking is applied by callers,
but nothing is sent unless this finds nothing left to mask."""
from __future__ import annotations
import re

CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UPI = re.compile(r"\b[\w.-]{2,}@[a-z]{2,}\b", re.I)
# 9+ digit run that is not the integer part of a decimal amount (comma-grouped amounts have groups of 3, so they never match)
LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d|\.\d)")      # also catches a run right after a comma or period; only a decimal amount's integer part is exempt

def _last4(m: re.Match) -> str: return "[ID …" + re.sub(r"\D", "", m.group())[-4:] + "]"

def mask_text(s: str) -> str:
    s = EMAIL.sub("[EMAIL]", s)
    s = UPI.sub("[UPI-ID]", s)
    s = CARD.sub(_last4, s)
    s = LONG_DIGITS.sub(_last4, s)
    return s

def mask_obj(o):
    if isinstance(o, str): return mask_text(o)
    if isinstance(o, dict): return {k: mask_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [mask_obj(v) for v in o]
    return o

def leaks(s: str) -> list[str]:
    """Anything an attacker or a bug could still exfiltrate: returns the offending fragments."""
    return [m.group() for pat in (EMAIL, UPI, CARD, LONG_DIGITS) for m in pat.finditer(s) if not m.group().startswith("[")]

class LeakError(RuntimeError): ...

def assert_clean(messages: list[dict]) -> None:
    for m in messages:
        found = leaks(str(m.get("content", "")))
        if found: raise LeakError(f"refusing to send: {len(found)} unmasked identifier(s) in a {m.get('role')} message")
