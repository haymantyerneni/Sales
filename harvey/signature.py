"""Sign-off and sender identity, applied in code to every outgoing email.

The model is told not to sign; this module owns the sign-off so every email
(sequence steps and handler replies alike) ends identically, whatever the
model produced. ``apply_signature`` is idempotent: running it on an already
signed body returns the same body.
"""

import html
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
    # The model's text: one line per paragraph. (The signature block below
    # keeps its own line breaks.)
    text = unwrap_paragraphs(text)
    block = signature_block(persona)
    return f"{text}\n\n{SIGN_OFF}\n{block}" if block else f"{text}\n\n{SIGN_OFF}"


def unwrap_paragraphs(text: str) -> str:
    """One line per paragraph: joins line breaks inside a paragraph so the
    plain-text part never breaks mid-sentence. Blank lines are kept."""
    paragraphs = re.split(r"\n\s*\n", (text or "").strip())
    return "\n\n".join(" ".join(ln.strip() for ln in p.splitlines() if ln.strip())
                         for p in paragraphs if p.strip())


def strip_signature(body: str) -> str:
    """The body without its sign-off — what word limits are measured on."""
    text = (body or "").rstrip()
    matches = list(_SIGN_OFF_RE.finditer(text))
    return text[: matches[-1].start()].rstrip() if matches else text


LOGO_MAX_WIDTH = 150

_HTML_OPEN = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
              'line-height:1.5;color:#222222">')
_P_OPEN = '<p style="margin:0 0 12px 0">'


def _logo_tag(persona) -> str:
    url = (getattr(persona, "logo_url", "") or "").strip()
    width = min(int(getattr(persona, "logo_width", LOGO_MAX_WIDTH) or LOGO_MAX_WIDTH),
                LOGO_MAX_WIDTH)
    alt = html.escape((getattr(persona, "company", "") or "Logo").strip(), quote=True)
    return (f'<img src="{html.escape(url, quote=True)}" width="{width}" alt="{alt}" '
            f'style="display:block;border:0;outline:none;width:{width}px;'
            f'max-width:{width}px;height:auto">')


def html_alternative(body: str, persona) -> str:
    """The HTML part: the plain-text body, HTML-escaped (so nothing the model
    wrote can become markup), in paragraphs, plus the logo under the
    signature. '' when no logo is configured: the email stays plain text.

    This template is the ONLY source of HTML in outgoing mail; the gate
    rebuilds it from the plain text and rejects anything that differs."""
    if not (getattr(persona, "logo_url", "") or "").strip():
        return ""
    paragraphs = [p.strip("\n") for p in re.split(r"\n\s*\n", (body or "").strip())]
    parts = [
        _P_OPEN + "<br>".join(html.escape(line, quote=False) for line in p.split("\n")) + "</p>"
        for p in paragraphs if p.strip()
    ]
    return _HTML_OPEN + "".join(parts) + _logo_tag(persona) + "</div>"


def placeholders(text: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(text or "")))


def from_header(persona, fallback_email: str = "") -> str:
    """RFC-safe From header: '"Haymant Yerneni | CloudBox99" <a@b.com>'."""
    name = (getattr(persona, "from_name", "") or getattr(persona, "name", "") or "").strip()
    email = (getattr(persona, "email", "") or fallback_email or "").strip()
    if not email:
        return name
    return formataddr((name, email))
