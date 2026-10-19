"""icp.required_country: the hard India filter."""

import pytest

from harvey.agents.scout import Scout
from harvey.geo_filter import country_evidence, mentions_country


@pytest.mark.parametrize(
    "domain, html, reason",
    [
        ("acme.in", "", ".in domain"),
        ("acme.co.in", "", ".in domain"),
        ("acme.com", '<a href="tel:+91-80-4123-4567">Call</a>', "+91 phone number"),
        ("acme.com", "<p>Call us: +91 98450 12345</p>", "+91 phone number"),
        ("acme.com", "<footer>GSTIN: 29ABCDE1234F1Z5</footer>", "GSTIN"),
        ("acme.com", "<p>CIN U72900KA2019PTC123456</p>", "company CIN"),
        ("acme.com", "<p>© 2026 Acme Technologies Pvt. Ltd.</p>", "Private Limited company"),
        ("acme.com", "<p>Plans from ₹ 499/month</p>", "rupee pricing"),
        ("acme.com", "<p>Plans from Rs.899/mo</p>", "rupee pricing"),
        (
            "acme.com",
            "<address>4th Floor, HSR Layout, Bengaluru, Karnataka 560102</address>",
            "Indian address with PIN code",
        ),
    ],
)
def test_india_evidence_found(domain, html, reason):
    assert country_evidence("India", domain, html) == reason


@pytest.mark.parametrize(
    "domain, html",
    [
        ("acme.com", ""),
        # Mentioning India isn't evidence — multinationals say it all the time.
        ("acme.com", "<p>Offices in San Francisco, London and India.</p>"),
        ("acme.com", "<p>Call +1 (415) 555-0100, 100 Market St, San Francisco, CA 94105</p>"),
        ("acme.co.uk", "<p>Plans from £49/month. Call +44 20 7946 0958</p>"),
        # A US ZIP after an unrelated word is not an Indian PIN.
        ("acme.com", "<p>Austin, TX 787011</p>"),
    ],
)
def test_india_no_evidence(domain, html):
    assert country_evidence("India", domain, html) == ""


def test_no_country_means_no_filter():
    assert country_evidence("", "acme.com", "") == "no country filter"
    assert mentions_country("", "anything")


def test_other_country_falls_back_to_name_match():
    assert country_evidence("Kenya", "acme.com", "<p>Nairobi, Kenya</p>")
    assert not country_evidence("Kenya", "acme.com", "<p>Lagos</p>")


@pytest.mark.parametrize(
    "snippet, ok",
    [
        ("CTO at Acme · Bengaluru, Karnataka, India · 500+ connections", True),
        ("Founder - Acme | Pune", True),
        ("VP Engineering at Acme · San Francisco Bay Area", False),
    ],
)
def test_linkedin_snippet_check(snippet, ok):
    assert mentions_country("India", snippet) is ok


class _Icp:
    required_country = "India"


class _Cfg:
    icp = _Icp()


def _scout(pages: dict[str, str]):
    """A Scout whose network is a dict of url → html."""
    scout = Scout.__new__(Scout)
    scout.config = _Cfg()
    scout._country_rejected = set()
    fetched = []

    class _Resp:
        def __init__(self, text):
            self.text = text

    async def resolve(domain, retries=1):
        url = f"https://{domain}"
        return (url, _Resp(pages[url])) if url in pages else ("", None)

    async def fetch(url, **kw):
        fetched.append(url)
        return _Resp(pages[url]) if url in pages else None

    scout._resolve_base_url = resolve
    scout._fetch = fetch
    return scout, fetched


@pytest.mark.asyncio
async def test_scout_finds_evidence_on_contact_page():
    scout, fetched = _scout({
        "https://acme.com": "<h1>Cloud tools for teams</h1>",
        "https://acme.com/contact": "<p>Mail us: Koramangala, Bengaluru 560034</p>",
    })
    assert await scout._in_required_country("acme.com", "<h1>Cloud tools</h1>")
    assert fetched == ["https://acme.com/contact"]


@pytest.mark.asyncio
async def test_scout_rejects_and_remembers_foreign_company():
    scout, fetched = _scout({"https://bigco.com": "<p>San Francisco, CA</p>"})
    assert not await scout._in_required_country("bigco.com")
    assert "bigco.com" in scout._country_rejected
    fetched.clear()
    assert not await scout._in_required_country("bigco.com")
    assert fetched == []  # not re-fetched on the next cycle


@pytest.mark.asyncio
async def test_scout_filter_off_when_unset():
    scout, fetched = _scout({})
    scout.config.icp.required_country = ""
    try:
        assert await scout._in_required_country("anything.com")
    finally:
        scout.config.icp.required_country = "India"
