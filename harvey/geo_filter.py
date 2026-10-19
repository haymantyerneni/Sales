"""Hard country filter: is a company actually based in the target country?

Search queries and job-board locations only *bias* discovery toward a
country — results still include foreign companies with a local office, or a
company name guessed onto the wrong domain. When `icp.required_country` is
set, a company is kept only if its own website shows concrete evidence of
being there. Evidence is matched in code, not judged by a model, so the
reason for every keep or skip is reproducible and shows up in the log.

India is the only country with dedicated rules. Any other value falls back
to the country's name appearing in the page text — weak, so it says so.
"""

import re

from bs4 import BeautifulSoup

# Pages most likely to carry an address, phone number or company ID, tried
# in order when the homepage alone shows no evidence.
EVIDENCE_PATHS = ("/contact", "/contact-us", "/about", "/about-us")

_INDIA_PLACES = (
    "bengaluru|bangalore|mumbai|bombay|delhi|new delhi|gurugram|gurgaon|noida|"
    "pune|hyderabad|chennai|madras|kolkata|calcutta|ahmedabad|jaipur|kochi|"
    "cochin|thiruvananthapuram|trivandrum|coimbatore|indore|chandigarh|"
    "lucknow|nagpur|bhubaneswar|visakhapatnam|vadodara|surat|mohali|"
    "karnataka|maharashtra|telangana|tamil nadu|haryana|uttar pradesh|"
    "west bengal|gujarat|kerala|rajasthan|andhra pradesh|odisha|punjab|"
    "madhya pradesh"
)

# (reason, pattern). Each one alone is enough: none of them shows up on a
# site that has no real presence in India.
_INDIA_TEXT_RULES = (
    # +91 / 0091 followed by a 10-digit number, any separators.
    ("+91 phone number", re.compile(r"(?:\+|\b00)\s*91[\s\-.()]*(?:\d[\s\-.()]*){10}\b")),
    ("GSTIN", re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")),
    ("company CIN", re.compile(r"\b[UL]\d{5}[A-Z]{2}\d{4}(?:PTC|PLC|OPC)\d{6}\b")),
    ("Private Limited company", re.compile(r"\b(?:private\s+limited|pvt\.?\s*ltd)\b", re.I)),
    ("rupee pricing", re.compile(r"₹\s*\d|\bINR\s*\d|\bRs\.?\s*\d", re.I)),
    (
        "Indian address with PIN code",
        re.compile(rf"\b(?:{_INDIA_PLACES})\b[^\n]{{0,80}}?\b[1-9]\d{{2}}\s?\d{{3}}\b", re.I),
    ),
)


def mentions_country(country: str, text: str) -> bool:
    """Loose check for short text with no website behind it (a search
    snippet for a LinkedIn profile): the country, or for India one of its
    major cities or states, is named.
    """
    country = (country or "").strip().lower()
    if not country:
        return True
    if re.search(rf"\b{re.escape(country)}\b", text or "", re.I):
        return True
    return country == "india" and bool(re.search(rf"\b(?:{_INDIA_PLACES})\b", text or "", re.I))


def _page_text(html: str) -> str:
    try:
        return BeautifulSoup(html, "html.parser").get_text(" ")
    except Exception:
        return html


def country_evidence(country: str, domain: str = "", html: str = "") -> str:
    """Why we believe the company is based in `country`, or "" if no evidence.

    `domain` and `html` are both optional so a caller can check the domain
    alone, or each extra page as it's fetched.
    """
    country = (country or "").strip().lower()
    if not country:
        return "no country filter"
    domain = (domain or "").strip().lower()

    if country == "india":
        if domain.endswith(".in"):
            return ".in domain"
        if not html:
            return ""
        # tel: links carry the number even when the visible text doesn't.
        for tel in re.findall(r'href=["\']tel:([^"\']+)', html, re.I):
            if re.sub(r"[^\d+]", "", tel).startswith(("+91", "0091")):
                return "+91 phone number"
        text = _page_text(html)
        for reason, pattern in _INDIA_TEXT_RULES:
            if pattern.search(text):
                return reason
        return ""

    if html and re.search(rf"\b{re.escape(country)}\b", _page_text(html), re.I):
        return f"mentions {country} (name match only)"
    return ""
