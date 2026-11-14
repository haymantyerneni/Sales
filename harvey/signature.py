"""Sign-off and sender identity, applied in code to every outgoing email.

The model is told not to sign; this module owns the sign-off so every email
(sequence steps and handler replies alike) ends identically, whatever the
model produced. ``apply_signature`` is idempotent: running it on an already
signed body returns the same body.
"""

import re
from email.utils import formataddr

SIGN_OFF = "Warm regards,"

# A sign-off line the model may have written anyway. Everything from the
# LAST such line onwards is replaced by the canonical sign-off + signature.
_SIGN_OFF_RE = re.compile(
    r"^\s*(warm regards|kind regards|best regards|regards|best wishes|"
    r"best|thanks|thank you|cheers|sincerely)\s*,?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Template placeholders that must never reach a prospect: "[phone]",
# "[detail from their website]". Merge tags ({{x}}) are the gate's job.
PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9 /_.'-]{1,60}\]")


def signature_block(persona) -> str:
    sig = (getattr(persona, "signature", "") or "").strip()
    return sig or (getattr(persona, "name", "") or "").strip()


def apply_signature(body: str, persona) -> str:
    """Body + "Warm regards," + the persona's signature, exactly once."""
    text = (body or "").rstrip()
    matches = list(_SIGN_OFF_RE.finditer(text))
    if matches:
        text = text[: matches[-1].start()].rstrip()
    block = signature_block(persona)
    return f"{text}\n\n{SIGN_OFF}\n{block}" if block else f"{text}\n\n{SIGN_OFF}"


def strip_signature(body: str) -> str:
    """The body without its sign-off — what word limits are measured on."""
    text = (body or "").rstrip()
    matches = list(_SIGN_OFF_RE.finditer(text))
    return text[: matches[-1].start()].rstrip() if matches else text


def placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text or "")))


def from_header(persona, fallback_email: str = "") -> str:
    """RFC-safe From header: '"Haymant Yerneni | CloudBox99" <a@b.com>'."""
    name = (getattr(persona, "from_name", "") or getattr(persona, "name", "") or "").strip()
    email = (getattr(persona, "email", "") or fallback_email or "").strip()
    if not email:
        return name
    return formataddr((name, email))
