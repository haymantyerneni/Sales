"""Deterministic pre-send gate — the last line before an email leaves.

LLM instructions are a floor, not a guarantee. Every outgoing email passes
these non-LLM checks at send time; a failure blocks the send and records
why, so a bad generation can never reach a prospect. This is the pattern
the surviving commercial AI-SDR vendors converged on after the 2025
autonomous-agent flameouts: constraints in the prompt, verification in
code.
"""

import re
from dataclasses import dataclass, field

# Spam-filter and AI-tell phrases that must never appear in outgoing mail.
# Mirrors prompts/writer.md; enforced here because prompts can be ignored.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "winner", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your", "as an ai", "i'm an ai", "i am an ai",
    "as a language model",
)

MAX_SUBJECT_LEN = 90
MAX_BODY_WORDS = 220        # hard ceiling; sequences aim far lower
MAX_LINKS = 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MERGE_TAG_RE = re.compile(r"\{\{\s*\w+\s*\}\}|\{\s*\w+\s*\}(?!\})")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

DELIVERABLE_STATUSES = {"verified", "risky"}


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def pre_send_check(
    to_email: str,
    subject: str,
    body: str,
    prospect=None,
    allow_risky: bool = False,
    kind: str = "sequence",
) -> GateResult:
    """Run every deterministic check. Returns ok=False with reasons on any hit."""
    reasons: list[str] = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    to_email = (to_email or "").strip().lower()

    # Recipient sanity
    if not EMAIL_RE.match(to_email):
        reasons.append(f"invalid recipient address '{to_email}'")
    if prospect is not None:
        if (prospect.email or "").strip().lower() != to_email:
            reasons.append("recipient does not match the prospect record")
        from harvey.models.prospect import email_sendable
        # Replies go back to someone who emailed US — deliverability proven.
        # Otherwise: verified (risky when allowed), or trusted from the
        # owner's own uploaded list.
        if kind == "sequence" and not email_sendable(prospect, allow_risky):
            reasons.append(
                f"email_status '{prospect.email_status or 'guess'}' is not deliverable"
            )

    # Content sanity
    if not subject:
        reasons.append("empty subject")
    if not body:
        reasons.append("empty body")
    if len(subject) > MAX_SUBJECT_LEN:
        reasons.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN})")
    word_count = len(body.split())
    if word_count > MAX_BODY_WORDS:
        reasons.append(f"body too long ({word_count} words > {MAX_BODY_WORDS})")

    # Unrendered merge tags — the classic mass-mail embarrassment
    leftover = MERGE_TAG_RE.findall(subject + " " + body)
    if leftover:
        reasons.append(f"unrendered merge tags: {sorted(set(leftover))[:3]}")

    # Banned phrases (spam triggers + AI tells)
    lowered = (subject + " " + body).lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        reasons.append(f"banned phrases: {hits[:3]}")

    # Link budget: cold email with links goes to spam
    links = URL_RE.findall(body)
    if len(links) > MAX_LINKS:
        reasons.append(f"too many links ({len(links)} > {MAX_LINKS})")

    # No HTML in the text the model wrote. The only HTML that leaves is the
    # signature template, built in code from this plain text
    # (html_part_problems below).
    if re.search(r"<\s*/?\s*(html|head|body|div|span|p|br|table|tr|td|img|a|b|i|u|"
                 r"strong|em|font|style|script|iframe|form|center|h[1-6])\b",
                 subject + " " + body, re.IGNORECASE):
        reasons.append("HTML markup in subject/body (plain text only)")

    return GateResult(ok=not reasons, reasons=reasons)


# ── The HTML part (signature logo) ─────────────────────────────────────

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
_ALLOWED_HTML_TAGS = {"div", "p", "br", "img"}


def html_part_problems(html_part: str, plain_body: str, persona) -> list[str]:
    """Why an HTML part may not be sent ([] = it may).

    HTML is allowed ONLY as the generated alternative of the plain-text
    body: our own template (escaped text + the configured logo). So the
    part must equal what the template builds from the plain text right
    now, byte for byte, and must hold no tag, image, link or URL other
    than the configured logo. Model-written HTML can never get through:
    the plain text is HTML-free (pre_send_check) and is escaped here."""
    from harvey.signature import html_alternative

    if not html_part:
        return []
    problems: list[str] = []
    logo = (getattr(persona, "logo_url", "") or "").strip()
    if not logo:
        problems.append("HTML part present but no persona.logo_url is configured")
    if html_part != html_alternative(plain_body, persona):
        problems.append("HTML part is not the signature template built from the plain text")
    tags = _TAG_RE.findall(html_part)
    bad = sorted({name.lower() for _, name, _ in tags} - _ALLOWED_HTML_TAGS)
    if bad:
        problems.append(f"HTML part uses tags outside the template: {bad[:4]}")
    imgs = [attrs for _, name, attrs in tags if name.lower() == "img"]
    if len(imgs) != 1:
        problems.append(f"HTML part must hold exactly one image, the logo (found {len(imgs)})")
    srcs = re.findall(r"""\b(?:src|href|background|action)\s*=\s*["']?([^"'\s>]+)""",
                      html_part, re.IGNORECASE)
    if any(s != logo for s in srcs):
        problems.append("HTML part references a URL other than the logo")
    attrs = " ".join(a for _, _, a in tags)
    if (re.search(r"<\s*(script|style|a|iframe|form)\b", html_part, re.IGNORECASE)
            or re.search(r"url\s*\(|@import|\bon\w+\s*=", attrs, re.IGNORECASE)):
        problems.append("HTML part contains scripts, styles, links or CSS URLs")
    return problems
