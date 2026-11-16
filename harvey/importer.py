"""Import the owner's own prospect list (.xlsx / .csv).

Two steps, always:
  1. Dry run (`harvey import FILE`): reads every row, tidies the company
     name and first name, finds the website, verifies the email, skips
     duplicates / do-not-contact / non-Indian rows, and flags anything it
     isn't sure about. Writes nothing to the database. Saves a review sheet
     (data/imports/<name>-review.xlsx) the owner can correct.
  2. Import (`harvey import FILE --commit`): adds the rows marked "import"
     in the review sheet, with the owner's corrections applied. No search
     or verifier credit is spent twice: the dry run's results are reused.

Imported people are Source = "Uploaded list", Contact type = "Uploaded
contact". They go through the same writer, Outbox approval, daily cap,
send window and pre-send gate as everyone else.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harvey.models.prospect import IMPORT_PREFIX

logger = logging.getLogger("harvey.importer")

IMPORT_SOURCE = "uploaded_list"
from harvey.paths import PROJECT_ROOT  # noqa: E402

IMPORT_DIR = PROJECT_ROOT / "data" / "imports"
TEMPLATE_PATH = PROJECT_ROOT / "data" / "import_template.xlsx"

# ── Columns ──────────────────────────────────────────────────────────

FIELDS = ("company", "name", "email", "website", "title", "city", "phone", "notes")
TEMPLATE_HEADERS = ("COMPANY NAME", "PERSON NAME", "EMAIL.ID", "Website", "Title",
                    "City", "Phone", "Notes")
ALIASES = {
    "company": {"companyname", "company", "organisation", "organization", "firm",
                "companyorganisation", "business", "businessname", "account"},
    "name": {"personname", "name", "fullname", "contactname", "person", "contact",
             "contactperson"},
    "email": {"emailid", "email", "emailaddress", "mail", "mailid", "emailaddr"},
    "website": {"website", "url", "web", "site", "domain", "websiteurl", "companywebsite"},
    "title": {"title", "designation", "jobtitle", "role", "position"},
    "city": {"city", "location", "town", "place"},
    "phone": {"phone", "mobile", "phonenumber", "mobilenumber", "contactnumber",
              "phoneno", "mobileno", "tel", "telephone"},
    "notes": {"notes", "note", "remarks", "comments", "comment"},
}


def header_key(text) -> str:
    """'EMAIL.ID', 'Email ID' and 'email' all become 'emailid'/'email'."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def map_headers(headers: list) -> dict[int, str]:
    """Column index -> field, for the headers we recognise."""
    out: dict[int, str] = {}
    for i, h in enumerate(headers):
        key = header_key(h)
        for fld, names in ALIASES.items():
            if key in names and fld not in out.values():
                out[i] = fld
                break
    return out


def read_rows(path: str | Path) -> list[dict]:
    """Rows as dicts with the FIELDS keys (missing ones are ""), plus 'row'
    (the spreadsheet row number). The header is the first row, within the
    first 10, that has an email column."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        raw = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
    elif path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = [list(r) for r in csv.reader(f)]
    else:
        raise ValueError(f"{path.name}: use an .xlsx or .csv file")

    for h, header in enumerate(raw[:10]):
        cols = map_headers(header)
        if "email" in cols.values():
            break
    else:
        raise ValueError(
            "No email column found. Expected headers like COMPANY NAME, PERSON NAME, "
            "EMAIL.ID (see data/import_template.xlsx).")

    rows = []
    for n, values in enumerate(raw[h + 1:], start=h + 2):
        rec = {f: "" for f in FIELDS}
        for i, fld in cols.items():
            v = values[i] if i < len(values) else None
            rec[fld] = re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""
        if not any(rec.values()):
            continue
        rec["row"] = n
        rows.append(rec)
    return rows


def write_template(path: str | Path = TEMPLATE_PATH) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Prospects"
    ws.append(list(TEMPLATE_HEADERS))
    ws.append(["Acme Technologies Pvt Ltd", "Ravi Kumar", "ravi@acmetech.in",
               "acmetech.in", "CTO", "Hyderabad", "+91 40 1234 5678", "Met at the expo"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, width in zip("ABCDEFGH", (32, 24, 30, 24, 20, 16, 18, 30)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.save(path)
    return path


# ── Company names ────────────────────────────────────────────────────

LEGAL_TOKENS = {"pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "corp",
                "opc", "llc", "plc", "pte", "pty", "gmbh", "lp"}


def _is_legal_tail(tok: str, prev: str) -> bool:
    t = tok.lower().strip(".,")
    if t in LEGAL_TOKENS:
        return True
    # Truncated forms: "Private Limi", "Pvt Lt", "Privat"
    if len(t) >= 3 and ("limited".startswith(t) or "private".startswith(t)):
        return True
    if t in ("l", "lt", "li", "pv", "p") and prev.lower().strip(".,") in ("pvt", "private", "pvt.ltd"):
        return True
    return False


def tidy_company(name: str) -> str:
    """'M/s. SARAS INDIA PVT. LTD. (AIPL-HQ)' -> 'Saras India'."""
    s = (name or "").strip()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", s)          # bracketed parts
    s = re.sub(r"\([^)]*$", " ", s)                                   # unclosed "(..."
    s = re.sub(r"^\s*m\s*/\s*s\.?\s*", "", s, flags=re.IGNORECASE)   # "M/s."
    s = re.sub(r"\bpvt\.?\s*ltd\.?", "Pvt Ltd", s, flags=re.IGNORECASE)
    tokens = s.replace(",", " ").split()
    while len(tokens) > 1 and _is_legal_tail(tokens[-1], tokens[-2] if len(tokens) > 1 else ""):
        tokens.pop()
    s = " ".join(tokens).strip(" ,.-&/")
    letters = [c for c in s if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        # ALL CAPS -> Title Case, keeping short acronyms ("ABC", "IT").
        s = " ".join(w if len(w) <= 3 else w.capitalize() for w in s.split())
    return s or (name or "").strip()


# ── Person names ─────────────────────────────────────────────────────

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "smt", "er", "prof", "capt", "col",
              "adv", "ca", "cs", "mx", "sir", "madam"}
# Prefixes people often don't go by ("Venkata Ramana" is usually "Ramana").
CALL_NAME_PREFIXES = {"sri", "sree", "shri", "md", "mohd", "mohammed", "mohammad",
                      "muhammad", "syed", "sk", "venkata", "naga", "late"}
COMMON_SURNAMES = {"rao", "reddy", "naidu", "sharma", "varma", "verma", "gupta", "singh",
                   "patel", "shah", "iyer", "iyengar", "nair", "menon", "pillai", "chowdary",
                   "choudhary", "chaudhary", "goud", "yadav", "jain", "agarwal", "agrawal",
                   "mehta", "joshi", "kulkarni", "deshpande", "patil", "mishra", "pandey",
                   "tiwari", "banerjee", "chatterjee", "mukherjee", "das", "bose", "ghosh"}
ROLE_WORDS = {"accounts", "admin", "hr", "info", "sales", "support", "team", "office",
              "manager", "director", "purchase", "purchases", "contact", "enquiry",
              "enquiries", "marketing", "department", "dept", "reception", "helpdesk",
              "careers", "billing", "ceo", "cto", "owner", "proprietor", "partner"}


@dataclass
class NameResult:
    first: str
    last: str
    flag: str = ""


def _is_initial(tok: str) -> bool:
    bare = tok.replace(".", "")
    if not bare:
        return True
    if len(bare) == 1:
        return True
    if "." in tok and all(len(p) <= 1 for p in tok.split(".")):
        return True                                   # "V.V.K." / "K.S"
    if 2 <= len(bare) <= 3 and bare.isupper() and not re.search(r"[AEIOU]", bare):
        return True                                   # "VVK", "KS"
    return False


def _cap(tok: str) -> str:
    if tok.isupper() or tok.islower():
        return "-".join(p.capitalize() for p in tok.split("-"))
    return tok


def split_name(full: str) -> NameResult:
    """First name for the greeting. Skips honorifics and leading/trailing
    initials. Flags instead of guessing when it isn't sure."""
    raw = (full or "").strip()
    if not raw:
        return NameResult("", "", "no person name")
    if re.search(r"[@\d]", raw):
        return NameResult("", "", f"name '{raw}' has digits or @")
    # A short abbreviation with a dot ("Ch.", "Sk.") is an initial, unless
    # it's an honorific ("Dr.", "Mrs.").
    raw_marked = re.sub(
        r"\b([A-Za-z]{2,3})\.",
        lambda m: m.group(0) if m.group(1).lower() in HONORIFICS else m.group(1)[0] + ".",
        raw)
    # Dots separate initials ("V.V.K. Mahalakshmi", "Lakshmi.N").
    tokens = [t.strip(",;") for t in re.sub(r"[.,]", " ", raw_marked).split() if t.strip(",;")]
    while tokens and tokens[0].lower().strip(".") in HONORIFICS and len(tokens) > 1:
        tokens.pop(0)
    while tokens and _is_initial(tokens[0]):
        tokens.pop(0)
    trailing = 0
    while tokens and _is_initial(tokens[-1]):
        tokens.pop()
        trailing += 1
    core = [t.replace(".", "") for t in tokens if not _is_initial(t)]
    if not core:
        return NameResult("", "", f"name '{raw}' is only initials")
    if any(t.lower() in ROLE_WORDS for t in core):
        return NameResult("", "", f"'{raw}' looks like a role or team, not a person")
    first = core[0]
    rest = core[1:]
    flag = ""
    if first.lower() in CALL_NAME_PREFIXES and rest:
        flag = f"'{raw}': may go by '{_cap(rest[0])}' rather than '{_cap(first)}'"
        first, rest = rest[0], rest[1:]
    elif first.lower() in COMMON_SURNAMES:
        flag = f"'{raw}': '{_cap(first)}' is usually a surname"
    elif not rest and trailing >= 2:
        flag = f"'{raw}': '{_cap(first)}' may be a surname (written surname first)"
    elif len(first) <= 2:
        flag = f"'{raw}': first name '{first}' is very short"
    return NameResult(_cap(first), " ".join(_cap(t) for t in rest), flag)


# ── Domains and websites ─────────────────────────────────────────────

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com",
    "rocketmail.com", "hotmail.com", "hotmail.co.in", "outlook.com", "outlook.in",
    "live.com", "live.in", "msn.com", "rediffmail.com", "rediff.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "pm.me",
    "zoho.com", "zohomail.com", "zohomail.in", "yandex.com", "mail.com", "gmx.com",
    "gmx.net", "inbox.com", "sify.com", "vsnl.net", "vsnl.com", "vsnl.net.in",
    "bsnl.in", "airtelmail.in", "indiatimes.com", "tutanota.com", "fastmail.com",
}
# Hosts a name search returns that are never the company's own site.
DIRECTORY_HOSTS = (
    "justdial", "indiamart", "linkedin", "facebook", "instagram", "twitter", "x.com",
    "youtube", "zaubacorp", "tofler", "crunchbase", "glassdoor", "ambitionbox",
    "wikipedia", "naukri", "sulekha", "tradeindia", "google.", "bing.", "duckduckgo",
    "yelp", "zoominfo", "rocketreach", "apollo.io", "dnb.com", "thecompanycheck",
    "falconebiz", "instafinancials", "indeed", "monster", "shine.com", "quora",
    "medium.com", "github.com", "play.google", "apps.apple", "amazon.", "flipkart",
    "exportersindia", "yellowpages", "mapquest", "foundit", "owler", "pitchbook",
    "signalhire", "lusha", "contactout", "economictimes", "business-standard",
)
SECOND_LEVEL = {"co", "com", "net", "org", "ac", "gov", "edu", "res", "firm", "gen", "ind", "nic"}
GENERIC_WORDS = {"india", "indian", "technologies", "technology", "tech", "solutions",
                 "solution", "services", "service", "systems", "system", "software",
                 "softwares", "infotech", "consulting", "consultants", "consultancy",
                 "private", "the", "and", "of", "global", "enterprises", "enterprise",
                 "industries", "group", "labs", "lab", "company", "co", "international",
                 "infra", "ventures", "corporation", "it", "info", "digital", "networks"}


def domain_of(text: str) -> str:
    """'https://www.Acme.co.in/about' or 'x@acme.co.in' -> 'acme.co.in'."""
    t = (text or "").strip().lower()
    if "@" in t and "/" not in t:
        t = t.rsplit("@", 1)[1]
    t = re.sub(r"^[a-z]+://", "", t)
    t = t.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    t = t.strip(". ")
    if t.startswith("www."):
        t = t[4:]
    return t if "." in t else ""


def registrable(domain: str) -> str:
    labels = domain.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else domain


def is_free_mail(domain: str) -> bool:
    d = registrable(domain)
    return domain in FREE_MAIL or d in FREE_MAIL or d.startswith("yahoo.")


def is_institutional(domain: str) -> bool:
    """Government / education sites: never a prospect company's website."""
    d = domain.lower()
    return bool(re.search(r"(\.gov|\.nic|\.ac|\.edu|\.mil|\.res)(\.[a-z]{2})?$", d))


def name_match(company: str, domain: str) -> str:
    """How well a company name matches a domain: high / medium / low."""
    label = re.sub(r"[^a-z0-9]", "", registrable(domain).split(".")[0])
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower())]
    core = [w for w in words if w not in GENERIC_WORDS] or words
    if not label or not core:
        return "low"
    joined = "".join(core)
    if len(joined) >= 3 and (joined in label or (len(label) >= 4 and label in "".join(words))):
        return "high"
    if any(len(w) >= 4 and w in label for w in core):
        return "medium"
    initials = "".join(w[0] for w in words)
    if len(initials) >= 2 and label.startswith(initials):
        return "medium"
    return "low"


# ── Email verification ───────────────────────────────────────────────

async def _domain_mail_state(domain: str) -> str:
    """'ok' / 'none' (the domain can't receive mail) / 'unknown'."""
    import dns.resolver

    def _resolve(kind):
        return dns.resolver.resolve(domain, kind, lifetime=8)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: _resolve("MX"))
        return "ok"
    except dns.resolver.NXDOMAIN:
        return "none"
    except dns.resolver.NoAnswer:
        try:
            await loop.run_in_executor(None, lambda: _resolve("A"))   # RFC 5321 fallback
            return "ok"
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return "none"
        except Exception:
            return "unknown"
    except Exception:
        return "unknown"


async def verify_given_email(email: str, env, smtp_ok: bool) -> tuple[str, str]:
    """(status, note). status: verified / risky / invalid / unverified.
    API verifiers first (Reoon, ZeroBounce, Hunter: whichever keys exist),
    then an SMTP check for small self-hosted mail servers. Gmail, Google
    Workspace and Microsoft 365 accept every address over SMTP, so for those
    only an API verifier gives an answer."""
    from harvey.integrations import email_finder as ef

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email or ""):
        return "invalid", "not a valid email address"
    domain = email.rsplit("@", 1)[1]
    state = await _domain_mail_state(domain)
    if state == "none":
        return "invalid", f"{domain} can't receive email"

    channels = []
    if getattr(env, "zerobounce_api_key", ""):
        channels.append(("ZeroBounce", lambda: ef.verify_zerobounce(email, env.zerobounce_api_key)))
    if getattr(env, "reoon_api_key", ""):
        channels.append(("Reoon", lambda: ef.verify_reoon(email, env.reoon_api_key)))
    hunter = ef._hunter_api_key()
    if hunter:
        channels.append(("Hunter", lambda: ef.verify_hunter(email, hunter)))
    for name, call in channels:
        try:
            verdict = await call()
        except Exception:
            verdict = None
        if not verdict:
            continue
        status = ef._status_from_verdict(verdict)
        if status:
            return status, f"checked by {name}"

    mx = await ef.get_mx_host(domain)
    kind = ef.classify_mx(mx or "")
    if smtp_ok and mx and kind == "other" and not is_free_mail(domain):
        ok = await ef.verify_email_smtp(email)
        if ok is False:
            return "invalid", "mail server rejected the address"
        if ok is True:
            if await ef.detect_catch_all(domain) is True:
                return "risky", "mail server accepts any address (catch-all)"
            return "verified", "mail server accepted the address"
    why = {"google": "Google mail", "microsoft": "Microsoft 365", "gateway": "a mail filter"}
    host = "a free mail service" if is_free_mail(domain) else why.get(kind, "its mail server")
    return "unverified", f"couldn't confirm ({host}; needs a verifier key)"


async def smtp_port_open() -> bool:
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection("gmail-smtp-in.l.google.com", 25), timeout=6)
        w.close()
        return True
    except Exception:
        return False


# ── The plan (dry run) ───────────────────────────────────────────────

@dataclass
class PlanRow:
    row: int
    company_original: str
    company: str
    name_original: str
    first_name: str
    last_name: str
    email: str
    title: str = ""
    city: str = ""
    phone: str = ""
    notes: str = ""
    email_status: str = ""
    email_note: str = ""
    website: str = ""            # domain
    website_source: str = ""     # given / email domain / name search / not found
    website_confidence: str = ""  # high / medium / low
    description: str = ""
    segment: str = ""
    flags: list[str] = field(default_factory=list)
    skip: str = ""
    action: str = ""             # import / review / skip

    def decide(self):
        self.action = "skip" if self.skip else ("review" if self.flags else "import")


class SiteTools:
    """Free web search + homepage reading, borrowed from Scout."""

    def __init__(self, state, config, env):
        from harvey.agents.scout import Scout
        self.scout = Scout(None, state, config, env)
        self.searches = 0
        self._lock = asyncio.Lock()       # one search at a time, spaced out

    async def search_domain(self, company: str) -> tuple[str, str]:
        """(domain, confidence) of the company's own site, or ("", "")."""
        async with self._lock:
            return await self._search_domain(company)

    async def _search_domain(self, company: str) -> tuple[str, str]:
        if self.searches:
            await asyncio.sleep(random.uniform(2, 4))
        self.searches += 1
        self.scout._queries_this_cycle = 0
        try:
            results = await self.scout._web_search(f"{company} India official website")
        except Exception as e:
            logger.debug(f"search failed for {company}: {e}")
            return "", ""
        candidates = []
        for url, _ in results[:8]:
            d = domain_of(url)
            if not d or any(h in d for h in DIRECTORY_HOSTS) or is_free_mail(d) \
                    or is_institutional(d):
                continue
            d = registrable(d)
            if d not in candidates:
                candidates.append(d)
        for d in candidates[:5]:
            m = name_match(company, d)
            if m in ("high", "medium"):
                return d, m
        return (candidates[0], "low") if candidates else ("", "")

    async def read_site(self, domain: str) -> tuple[bool, str]:
        """(reachable, description)."""
        try:
            info = await self.scout._scrape_company_info(domain)
        except Exception:
            return False, ""
        reachable = "_homepage_html" in info
        return reachable, (info.get("description") or "").strip()


async def _known_emails(state) -> tuple[set[str], set[str]]:
    """(already in the database, do-not-contact)."""
    import aiosqlite
    from harvey.agents.sender import DO_NOT_CONTACT_SOURCES, DO_NOT_CONTACT_STATUSES
    known, dnc = set(), set()
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT lower(email), lower(source), status FROM prospects "
                              "WHERE email != ''") as cur:
            for email, source, status in await cur.fetchall():
                known.add(email)
                if source in DO_NOT_CONTACT_SOURCES or status in DO_NOT_CONTACT_STATUSES:
                    dnc.add(email)
    dnc |= await state.suppressed_emails()
    return known, dnc


def _outside_india(domain: str, phone: str) -> str:
    from harvey.collectors.discover import outside_country
    if domain and outside_country("IN", domain):
        return f"non-Indian website/email domain ({domain})"
    p = re.sub(r"[^\d+]", "", phone or "")
    if p.startswith("+") and not p.startswith("+91"):
        return f"non-Indian phone number ({phone})"
    return ""


async def plan_row(rec: dict, tools: SiteTools, env, smtp_ok: bool,
                   known: set[str], dnc: set[str], seen: set[str]) -> PlanRow:
    from harvey.agents.writer import segment_for

    email = (rec.get("email") or "").strip().lower().strip("<>;,")
    name = split_name(rec.get("name", ""))
    company = tidy_company(rec.get("company", ""))
    r = PlanRow(row=rec["row"], company_original=rec.get("company", ""), company=company,
                name_original=rec.get("name", ""), first_name=name.first,
                last_name=name.last, email=email, title=rec.get("title", ""),
                city=rec.get("city", ""), phone=rec.get("phone", ""),
                notes=rec.get("notes", ""))
    if name.flag:
        r.flags.append(f"name unclear: {name.flag}")
    if not company:
        r.flags.append("no company name")

    # Skips first: no point spending searches or verifier credits on them.
    if not email:
        r.skip = "no email"
    elif email in seen:
        r.skip = "duplicate within this file"
    elif email in dnc:
        r.skip = "on the do-not-contact list"
    elif email in known:
        r.skip = "already in the database / tracker"
    seen.add(email)
    email_domain = domain_of(email) if "@" in email else ""
    given = registrable(domain_of(rec.get("website", ""))) if rec.get("website") else ""
    if not r.skip:
        r.skip = _outside_india(given or ("" if is_free_mail(email_domain) else email_domain),
                                r.phone)
    if r.skip:
        r.decide()
        return r

    r.email_status, r.email_note = await verify_given_email(email, env, smtp_ok)
    if r.email_status == "invalid":
        r.skip = f"invalid email: {r.email_note}"
        r.decide()
        return r

    company_domain = "" if is_free_mail(email_domain) else registrable(email_domain)
    if given:
        r.website, r.website_source, r.website_confidence = given, "given", "high"
        if company_domain and company_domain != given:
            r.flags.append(f"domain mismatch: website {given} but email @{email_domain}")
            r.website_confidence = "low"
    elif company_domain:
        match = name_match(company, company_domain) if company else "medium"
        if match in ("high", "medium"):
            r.website, r.website_source, r.website_confidence = company_domain, "email domain", "high"
        else:
            found, conf = await tools.search_domain(company)
            if found == company_domain:
                r.website, r.website_source, r.website_confidence = found, "email domain", "high"
            else:
                r.website, r.website_source, r.website_confidence = company_domain, "email domain", "low"
                seen_as = f" (name search found {found})" if found else ""
                r.flags.append(f"domain mismatch: company '{company}' but email "
                               f"@{email_domain}{seen_as}")
    else:
        found, conf = await tools.search_domain(company) if company else ("", "")
        if found:
            r.website, r.website_source, r.website_confidence = found, "name search", conf
            if conf == "low":
                r.flags.append(f"low-confidence website: {found} doesn't clearly match "
                               f"'{company}'")
        else:
            r.website_source = "not found"

    if r.website and not r.skip:
        r.skip = _outside_india(r.website if r.website_confidence != "low" else "", "")
        if r.skip:
            r.decide()
            return r

    if r.website and r.website_confidence in ("high", "medium"):
        reachable, desc = await tools.read_site(r.website)
        if reachable and desc:
            r.description = desc[:500]
        elif not reachable:
            r.email_note += "; website unreachable, general email"
    r.segment = segment_for(r.description, r.company)["label"] if r.description else \
        segment_for(r.company)["label"]
    r.decide()
    return r


async def dry_run(path: str | Path, state, config, env, progress=None) -> dict:
    path = Path(path)
    rows = read_rows(path)
    known, dnc = await _known_emails(state)
    tools = SiteTools(state, config, env)
    smtp_ok = await smtp_port_open()
    seen: set[str] = set()
    plan: list[PlanRow] = []
    # Verification and site reads in parallel, searches one at a time
    # (SiteTools spaces them out) — keeps free search engines happy.
    sem = asyncio.Semaphore(4)

    async def one(rec):
        async with sem:
            return await plan_row(rec, tools, env, smtp_ok, known, dnc, seen)
    # 'seen' must be filled in file order so the first copy wins.
    order = []
    for rec in rows:
        e = (rec.get("email") or "").strip().lower()
        order.append(e)
    tasks = []
    first_seen: set[str] = set()
    for rec, e in zip(rows, order):
        if e and e in first_seen:
            rec = dict(rec, _dup=True)
        first_seen.add(e)
        tasks.append(rec)
    results = []
    for i in range(0, len(tasks), 8):
        batch = tasks[i:i + 8]
        done = await asyncio.gather(*[
            _dup_row(rec) if rec.get("_dup") else one(rec) for rec in batch])
        results.extend(done)
        if progress:
            progress(len(results), len(tasks))
    plan = results
    out = save_plan(path, plan, smtp_ok)
    return {"plan": plan, "summary": summarize(plan), "review_path": str(out["review"]),
            "plan_path": str(out["plan"]), "smtp_ok": smtp_ok}


async def _dup_row(rec: dict) -> PlanRow:
    name = split_name(rec.get("name", ""))
    r = PlanRow(row=rec["row"], company_original=rec.get("company", ""),
                company=tidy_company(rec.get("company", "")),
                name_original=rec.get("name", ""), first_name=name.first, last_name=name.last,
                email=(rec.get("email") or "").strip().lower(), skip="duplicate within this file")
    r.decide()
    return r


def summarize(plan: list[PlanRow]) -> dict:
    s = {"rows": len(plan), "import": 0, "review": 0, "skip": 0,
         "email": {"verified": 0, "risky": 0, "invalid": 0, "unverified": 0},
         "website": {"given": 0, "email domain": 0, "name search": 0, "not found": 0},
         "skipped": {}}
    for r in plan:
        s[r.action] += 1
        if r.email_status in s["email"]:
            s["email"][r.email_status] += 1
        elif r.skip.startswith("invalid email"):
            s["email"]["invalid"] += 1
        if not r.skip and r.website_source in s["website"]:
            s["website"][r.website_source] += 1
        if r.skip:
            key = r.skip.split(":")[0].split(" (")[0]
            s["skipped"][key] = s["skipped"].get(key, 0) + 1
    return s


# ── Review sheet ─────────────────────────────────────────────────────

REVIEW_COLUMNS = ("Row", "Action", "Company (original)", "Company (for emails)",
                  "Person name", "First name", "Last name", "Email", "Email status",
                  "Email check", "Website", "Website found by", "Confidence", "Segment",
                  "Review flags", "Skip reason", "Title", "City", "Phone", "Notes")
EDITABLE = ("Action", "Company (for emails)", "First name", "Last name", "Website")


def _paths(source: Path) -> dict[str, Path]:
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", source.stem)[:60]
    return {"review": IMPORT_DIR / f"{stem}-review.xlsx",
            "plan": IMPORT_DIR / f"{stem}-plan.json"}


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_plan(source: Path, plan: list[PlanRow], smtp_ok: bool) -> dict[str, Path]:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    paths = _paths(source)
    paths["review"].parent.mkdir(parents=True, exist_ok=True)
    paths["plan"].write_text(json.dumps({
        "source": str(source.resolve()), "sha256": file_hash(source),
        "created": datetime.now(timezone.utc).isoformat(), "smtp_ok": smtp_ok,
        "rows": [asdict(r) for r in plan]}, indent=1), encoding="utf-8")

    wb = Workbook()
    ws = wb.active
    ws.title = "Review"
    ws.append(list(REVIEW_COLUMNS))
    fills = {"import": "CCEDCC", "review": "FFF2CC", "skip": "D8D8D8"}
    for r in plan:
        ws.append([r.row, r.action, r.company_original, r.company, r.name_original,
                   r.first_name, r.last_name, r.email, r.email_status, r.email_note,
                   r.website, r.website_source, r.website_confidence, r.segment,
                   "; ".join(r.flags), r.skip, r.title, r.city, r.phone, r.notes])
        ws.cell(row=ws.max_row, column=2).fill = PatternFill("solid", fgColor=fills[r.action])
    for c in ws[1]:
        c.font = Font(bold=True)
        if c.value in EDITABLE:
            c.fill = PatternFill("solid", fgColor="DDEBF7")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    for i, w in enumerate((6, 9, 30, 24, 24, 14, 16, 30, 12, 34, 22, 14, 11, 26, 50,
                           30, 18, 14, 16, 30), start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    help_ws = wb.create_sheet("How to use")
    for line in (
        "Edit the blue columns, save, then run: harvey import <your file> --commit",
        "Action: import / review / skip. Only rows set to 'import' are added.",
        "Rows marked 'review' need your decision: fix the name or website, then set 'import'.",
        "Company (for emails) and First name are what the emails will use.",
        "Website: correct it if wrong; a corrected website is read again for the personal line.",
    ):
        help_ws.append([line])
    help_ws.column_dimensions["A"].width = 100
    wb.save(paths["review"])
    return paths


def load_reviewed(source: Path) -> tuple[list[PlanRow], dict]:
    """The dry-run plan with the owner's edits from the review sheet."""
    from openpyxl import load_workbook
    paths = _paths(source)
    if not paths["plan"].exists():
        raise ValueError(f"No dry run found for {source.name}. Run: harvey import {source}")
    data = json.loads(paths["plan"].read_text(encoding="utf-8"))
    if data["sha256"] != file_hash(source):
        raise ValueError(f"{source.name} changed since the dry run. Run the dry run again: "
                         f"harvey import {source}")
    plan = {r["row"]: PlanRow(**r) for r in data["rows"]}
    edits = {}
    if paths["review"].exists():
        ws = load_workbook(paths["review"], data_only=True)["Review"]
        header = [c.value for c in ws[1]]
        for values in ws.iter_rows(min_row=2, values_only=True):
            rec = dict(zip(header, values))
            if rec.get("Row") in plan:
                edits[rec["Row"]] = rec
    for row, rec in edits.items():
        r = plan[row]
        action = str(rec.get("Action") or "").strip().lower()
        if action in ("import", "review", "skip"):
            r.action = action
        for col, attr in (("Company (for emails)", "company"), ("First name", "first_name"),
                          ("Last name", "last_name")):
            v = rec.get(col)
            if v is not None and str(v).strip() != getattr(r, attr):
                setattr(r, attr, str(v).strip())
        site = registrable(domain_of(str(rec.get("Website") or "")))
        if (rec.get("Website") or "") != (r.website or "") and site != r.website:
            r.website = site
            r.website_source, r.website_confidence = ("given", "high") if site else ("not found", "")
            r.description = "__reread__" if site else ""
    return list(plan.values()), data


def _bookkeeping(r: PlanRow, source_name: str) -> str:
    lines = [f"{IMPORT_PREFIX} from {source_name}, row {r.row}."]
    if r.website:
        lines.append(f"{IMPORT_PREFIX} website {r.website} ({r.website_source}, "
                     f"{r.website_confidence} confidence).")
    else:
        lines.append(f"{IMPORT_PREFIX} website not found.")
    if r.flags:
        lines.append(f"{IMPORT_PREFIX} review: " + "; ".join(r.flags) + ".")
    if r.email_note:
        lines.append(f"{IMPORT_PREFIX} email {r.email_status}: {r.email_note}.")
    if r.notes:
        lines.append(f"{IMPORT_PREFIX} your note: {r.notes}")
    return "\n".join(lines)


async def commit(path: str | Path, state, config, env) -> dict:
    """Add the rows marked 'import'. Re-checks duplicates and do-not-contact
    at this moment; never re-verifies or re-searches (dry run results)."""
    from harvey.models.company import Company
    from harvey.models.prospect import Prospect

    source = Path(path)
    plan, _ = load_reviewed(source)
    known, dnc = await _known_emails(state)
    tools = None
    added, skipped, held = [], [], []
    for r in plan:
        if r.action == "review":
            held.append(r)
            continue
        if r.action != "import":
            continue
        if r.email in dnc or r.email in known:
            skipped.append((r, "already in the database or do-not-contact"))
            continue
        if r.email_status == "invalid" or not r.email:
            skipped.append((r, "invalid email"))
            continue
        if r.description == "__reread__":
            tools = tools or SiteTools(state, config, env)
            reachable, desc = await tools.read_site(r.website)
            r.description = desc[:500] if reachable else ""
        good_site = r.website and r.website_confidence in ("high", "medium")
        company = Company(
            name=r.company_original or r.company,
            domain=r.website if good_site else "",
            website=f"https://{r.website}" if r.website else "",
            description=r.description if good_site else "",
            location=r.city, source=IMPORT_SOURCE,
            external_id="" if good_site else f"import:{r.company.lower()}",
        )
        company_id = await state.add_company(company)
        prospect = Prospect(
            company_id=company_id, first_name=r.first_name, last_name=r.last_name,
            email=r.email, email_status="guess" if r.email_status == "unverified" else r.email_status,
            email_verified=r.email_status == "verified", phone=r.phone, title=r.title,
            source=IMPORT_SOURCE, source_url=f"{source.name}#row{r.row}", status="new",
            personalization_notes=_bookkeeping(r, source.name), company=r.company,
        )
        await state.add_prospect(prospect)
        known.add(r.email)
        added.append(r)
    await state.log_action(action_type="import", agent="importer",
                           details={"file": source.name, "added": len(added),
                                    "held_for_review": len(held), "skipped": len(skipped)})
    return {"added": added, "held": held, "skipped": skipped}


# ── Text output ──────────────────────────────────────────────────────

def format_summary(result: dict) -> str:
    s, plan = result["summary"], result["plan"]
    e, w = s["email"], s["website"]
    out = [
        f"  Rows read:            {s['rows']}",
        f"  Ready to import:      {s['import']}",
        f"  Need your review:     {s['review']}",
        f"  Skipped:              {s['skip']}" + (
            "  (" + ", ".join(f"{k}: {v}" for k, v in s["skipped"].items()) + ")"
            if s["skipped"] else ""),
        f"  Emails:               verified {e['verified']}, risky {e['risky']}, "
        f"invalid {e['invalid']}, unverified {e['unverified']}",
        f"  Websites:             from email domain {w['email domain']}, name search "
        f"{w['name search']}, given {w['given']}, not found {w['not found']}",
    ]
    if not result.get("smtp_ok"):
        out.append("  (Port 25 is blocked here, so only verifier keys can confirm emails.)")
    flagged = [r for r in plan if r.action == "review"]
    if flagged:
        out.append("\n  Flagged for review:")
        for r in flagged:
            out.append(f"    row {r.row}: {'; '.join(r.flags)}")
    skipped = [r for r in plan if r.action == "skip"]
    if skipped:
        out.append("\n  Skipped:")
        for r in skipped:
            out.append(f"    row {r.row}: {r.email or '(no email)'}: {r.skip}")
    out.append("\n  Every row (company for emails / first name):")
    for r in plan:
        site = f"{r.website} ({r.website_source}, {r.website_confidence})" if r.website \
            else r.website_source or "-"
        out.append(f"    {r.row:>4} [{r.action:<6}] {r.company_original!r} -> {r.company!r} | "
                   f"{r.name_original!r} -> {r.first_name or '?'!r} | {r.email} "
                   f"[{r.email_status or '-'}] | {site}")
    return "\n".join(out)
