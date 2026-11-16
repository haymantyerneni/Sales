"""Owner-uploaded prospect lists: column mapping, tidy company and first
names, website from the email domain or a name search (flag, never guess),
verification, skips, dry run -> review sheet -> commit, and the shared
pipeline rules (opt-out line, do-not-contact, priority, tracker)."""

import csv
import tempfile
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from harvey import importer
from harvey.importer import (PlanRow, domain_of, is_free_mail, map_headers, name_match,
                             read_rows, registrable, split_name, tidy_company)
from harvey.models.prospect import IMPORT_PREFIX, Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── Columns ──

def test_headers_match_ignoring_case_dots_and_spaces():
    cols = map_headers(["COMPANY NAME", "PERSON NAME", "EMAIL.ID", "Web Site", "Designation",
                        "city", "Mobile No", "Remarks"])
    assert list(cols.values()) == ["company", "name", "email", "website", "title", "city",
                                   "phone", "notes"]
    for header in ("EMAIL.ID", "Email ID", "email", "E-mail Id"):
        assert map_headers([header]) == {0: "email"}


def test_reads_xlsx_with_a_title_row_and_csv(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["My prospects, Nov"])
    ws.append(["COMPANY NAME", "PERSON NAME", "EMAIL.ID"])
    ws.append(["Saras India Pvt Ltd", "Haymant  Y", " ravi@saras.in "])
    ws.append([None, None, None])
    wb.save(tmp_path / "l.xlsx")
    rows = read_rows(tmp_path / "l.xlsx")
    assert rows == [{"company": "Saras India Pvt Ltd", "name": "Haymant Y",
                     "email": "ravi@saras.in", "website": "", "title": "", "city": "",
                     "phone": "", "notes": "", "row": 3}]
    with open(tmp_path / "l.csv", "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["Company", "Name", "Email ID", "Website"],
                                 ["Acme", "Ravi", "r@acme.in", "https://www.acme.in/x"]])
    assert read_rows(tmp_path / "l.csv")[0]["website"] == "https://www.acme.in/x"
    with pytest.raises(ValueError, match="No email column"):
        (tmp_path / "bad.csv").write_text("a,b\n1,2\n")
        read_rows(tmp_path / "bad.csv")


def test_template_has_every_column(tmp_path):
    from openpyxl import load_workbook
    path = importer.write_template(tmp_path / "t.xlsx")
    header = [c.value for c in load_workbook(path).active[1]]
    assert header == list(importer.TEMPLATE_HEADERS)
    assert list(map_headers(header).values()) == list(importer.FIELDS)


# ── Names ──

@pytest.mark.parametrize("raw,tidy", [
    ("Spotmies Private Limi", "Spotmies"),
    ("ADVANCED INFOTECH PVT. LTD. (AIPL-HQ)", "Advanced Infotech"),
    ("M/s. Saras India Pvt Ltd", "Saras India"),
    ("VCollab Software Private Limited", "VCollab Software"),
    ("Reaidy LLP", "Reaidy"),
    ("Sri Sai Enterprises Pvt.Ltd.", "Sri Sai Enterprises"),
    ("XYZ (OPC) Private Limited", "XYZ"),
    ("The Coffee Company", "The Coffee Company"),
])
def test_company_names_are_tidied_for_emails(raw, tidy):
    assert tidy_company(raw) == tidy


@pytest.mark.parametrize("raw,first,flagged", [
    ("V V K Mahalakshmi", "Mahalakshmi", False),
    ("Siva Prasad P.", "Siva", False),
    ("Dr. K. Ramesh", "Ramesh", False),
    ("RAMESH KUMAR", "Ramesh", False),
    ("Lakshmi.N", "Lakshmi", False),
    ("Ch. Srinivas", "Srinivas", False),
    ("Venkata Ramana Rao", "Ramana", True),
    ("Rao K V S", "Rao", True),
    ("Reddy Suresh", "Reddy", True),
    ("A. K.", "", True),
    ("Accounts Team", "", True),
    ("", "", True),
])
def test_first_names_skip_initials_and_flag_when_unsure(raw, first, flagged):
    r = split_name(raw)
    assert r.first == first and bool(r.flag) == flagged


# ── Domains ──

def test_domains_free_mail_and_name_match():
    assert domain_of("https://www.Acme.co.in/about?x=1") == "acme.co.in"
    assert domain_of("x@Mail.Acme.in") == "mail.acme.in"
    assert registrable("mail.acme.co.in") == "acme.co.in" and registrable("a.b.com") == "b.com"
    assert is_free_mail("gmail.com") and is_free_mail("yahoo.co.in") and is_free_mail("rediffmail.com")
    assert not is_free_mail("acme.in")
    assert name_match("Saras India", "sarasindia.com") == "high"
    assert name_match("VCollab Software", "vcollab.com") == "high"
    assert name_match("Advanced Infotech", "aipl.in") == "medium"
    assert name_match("Spotmies", "reaidy.io") == "low"


def test_bing_redirect_links_are_unwrapped_and_gov_sites_ignored():
    import base64
    from harvey.agents.scout import unwrap_bing_redirect
    from harvey.importer import is_institutional
    real = "https://www.sarasindia.com/about"
    enc = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    assert unwrap_bing_redirect(f"https://www.bing.com/ck/a?!&&p=x&u=a1{enc}&ntb=1") == real
    assert unwrap_bing_redirect(real) == real
    assert unwrap_bing_redirect("https://www.bing.com/ck/a?u=zz") == "https://www.bing.com/ck/a?u=zz"
    assert is_institutional("saras.gov.in") and is_institutional("iith.ac.in")
    assert not is_institutional("sarasindia.com") and not is_institutional("acme.co.in")


def test_dashboard_import_endpoints(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from harvey import dashboard
    monkeypatch.setattr(importer, "IMPORT_DIR", tmp_path)
    seen = {}

    async def fake_dry_run(path, state, config, env, progress=None):
        seen["path"] = path
        return {"plan": [PlanRow(row=2, company_original="Acme Pvt Ltd", company="Acme",
                                 name_original="Ravi", first_name="Ravi", last_name="",
                                 email="r@acme.in", action="import")],
                "summary": {"rows": 1}, "review_path": "x.xlsx"}

    async def fake_commit(path, state, config, env):
        return {"added": [1], "held": [], "skipped": []}
    monkeypatch.setattr(importer, "dry_run", fake_dry_run)
    monkeypatch.setattr(importer, "commit", fake_commit)
    client = TestClient(dashboard.app)
    r = client.post("/api/import/preview?filename=../../My List.xlsx", content=b"PK fake").json()
    assert r["rows"][0]["company"] == "Acme" and seen["path"] == tmp_path / "uploads" / "My_List.xlsx"
    assert client.post("/api/import/commit", json={"path": r["path"]}).json()["added"] == 1
    assert "error" in client.post("/api/import/preview?filename=x.exe", content=b"x").json()


# ── Per-row plan (network faked) ──

class FakeTools:
    def __init__(self, found=None, sites=None):
        self.found = found or {}
        self.sites = sites or {}
        self.searched = []

    async def search_domain(self, company):
        self.searched.append(company)
        return self.found.get(company, ("", ""))

    async def read_site(self, domain):
        return (domain in self.sites), self.sites.get(domain, "")


@pytest.fixture
def verdicts(monkeypatch):
    table = {}

    async def fake_verify(email, env, smtp_ok):
        return table.get(email, ("verified", "fake verifier"))
    monkeypatch.setattr(importer, "verify_given_email", fake_verify)
    return table


async def plan(rec, tools=None, known=(), dnc=(), seen=None):
    base = {f: "" for f in importer.FIELDS}
    base.update(rec)
    base.setdefault("row", 2)
    return await importer.plan_row(base, tools or FakeTools(), None, True,
                                   set(known), set(dnc), seen if seen is not None else set())


@pytest.mark.asyncio
async def test_company_email_domain_is_the_website(verdicts):
    tools = FakeTools(sites={"sarasindia.com": "SharePoint and Office 365 software consultancy."})
    r = await plan({"company": "Saras India Pvt Ltd", "name": "V V K Mahalakshmi",
                    "email": "maha@sarasindia.com"}, tools)
    assert (r.action, r.website, r.website_source, r.website_confidence) == \
        ("import", "sarasindia.com", "email domain", "high")
    assert r.description.startswith("SharePoint") and r.first_name == "Mahalakshmi"
    assert r.segment.startswith("IT services") and tools.searched == []


@pytest.mark.asyncio
async def test_free_mail_finds_the_website_by_name(verdicts):
    tools = FakeTools(found={"Acme Steel": ("acmesteel.in", "high")},
                      sites={"acmesteel.in": "Steel manufacturer in Hyderabad."})
    r = await plan({"company": "Acme Steel Pvt Ltd", "name": "Ravi", "email": "ravi@gmail.com"}, tools)
    assert (r.website, r.website_source, r.website_confidence, r.action) == \
        ("acmesteel.in", "name search", "high", "import")
    assert tools.searched == ["Acme Steel"]
    low = FakeTools(found={"Acme Steel": ("steelnews.in", "low")})
    r = await plan({"company": "Acme Steel", "name": "Ravi", "email": "ravi@gmail.com"}, low)
    assert r.action == "review" and "low-confidence website" in r.flags[0] and r.description == ""
    none = await plan({"company": "Acme Steel", "name": "Ravi", "email": "ravi@gmail.com"})
    assert none.website_source == "not found" and none.action == "import"


@pytest.mark.asyncio
async def test_email_domain_that_disagrees_with_the_company_is_flagged(verdicts):
    tools = FakeTools(found={"Spotmies": ("spotmies.com", "high")},
                      sites={"reaidy.io": "AI interviews."})
    r = await plan({"company": "Spotmies Private Limi", "name": "Kiran", "email": "kiran@reaidy.io"},
                   tools)
    assert r.action == "review" and r.website_confidence == "low"
    assert "company 'Spotmies' but email @reaidy.io (name search found spotmies.com)" in r.flags[0]
    assert r.description == ""                     # never personalise from a doubtful site


@pytest.mark.asyncio
async def test_given_website_wins_and_mismatch_flags(verdicts):
    r = await plan({"company": "Acme", "name": "Ravi", "email": "ravi@acme.in",
                    "website": "https://www.acme.in"})
    assert (r.website, r.website_source, r.action) == ("acme.in", "given", "import")
    r = await plan({"company": "Acme", "name": "Ravi", "email": "ravi@other.in",
                    "website": "acme.in"})
    assert r.action == "review" and "domain mismatch" in r.flags[0]


@pytest.mark.asyncio
async def test_skips_invalid_duplicates_dnc_and_non_indian(verdicts):
    verdicts["bad@acme.in"] = ("invalid", "mail server rejected the address")
    assert (await plan({"company": "Acme", "name": "A B", "email": "bad@acme.in"})).skip \
        .startswith("invalid email")
    assert (await plan({"company": "Acme", "name": "Ravi", "email": "r@acme.in"},
                       known={"r@acme.in"})).skip == "already in the database / tracker"
    assert (await plan({"company": "Acme", "name": "Ravi", "email": "r@acme.in"},
                       dnc={"r@acme.in"})).skip == "on the do-not-contact list"
    assert "non-Indian" in (await plan({"company": "Acme", "name": "Ravi",
                                        "email": "r@acme.co.uk"})).skip
    assert "non-Indian phone" in (await plan({"company": "Acme", "name": "Ravi",
                                              "email": "r@acme.in", "phone": "+44 20 1234"})).skip
    seen = set()
    await plan({"company": "Acme", "name": "Ravi", "email": "r@acme.in"}, seen=seen)
    assert (await plan({"company": "Acme", "name": "Ravi", "email": "r@acme.in"},
                       seen=seen)).skip == "duplicate within this file"
    assert (await plan({"company": "Acme", "name": "Ravi", "email": ""})).skip == "no email"


@pytest.mark.asyncio
async def test_unclear_name_goes_to_review(verdicts):
    r = await plan({"company": "Acme", "name": "Accounts Team", "email": "accounts@acme.in"})
    assert r.action == "review" and "name unclear" in r.flags[0]


# ── Dry run -> review sheet -> commit ──

def write_list(path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["COMPANY NAME", "PERSON NAME", "EMAIL.ID", "Phone", "Notes"])
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def isolated(monkeypatch, tmp_path, verdicts):
    monkeypatch.setattr(importer, "IMPORT_DIR", tmp_path / "imports")
    tools = FakeTools(found={"Spotmies": ("spotmies.com", "high")},
                      sites={"sarasindia.com": "SharePoint consultancy.",
                             "acme.in": "Steel maker."})
    monkeypatch.setattr(importer, "SiteTools", lambda *a, **k: tools)

    async def port_open():
        return True
    monkeypatch.setattr(importer, "smtp_port_open", port_open)
    return tmp_path


@pytest.mark.asyncio
async def test_dry_run_adds_nothing_then_commit_applies_review_edits(state, isolated):
    from openpyxl import load_workbook
    src = write_list(isolated / "master.xlsx", [
        ["Saras India Pvt Ltd", "V V K Mahalakshmi", "maha@sarasindia.com", "+91 40 1", "met at expo"],
        ["Spotmies Private Limi", "Kiran", "kiran@reaidy.io", "", ""],
        ["Acme Steel Pvt Ltd", "Venkata Ramana", "ramana@acme.in", "", ""],
        ["Saras India Pvt Ltd", "Dup", "maha@sarasindia.com", "", ""],
    ])
    result = await importer.dry_run(src, state, None, None)
    s = result["summary"]
    assert (s["rows"], s["import"], s["review"], s["skip"]) == (4, 1, 2, 1)
    assert s["website"]["email domain"] == 3 and s["email"]["verified"] == 3
    assert await state.get_prospects_by_status("new") == []          # dry run adds nothing
    text = importer.format_summary(result)
    assert "'Saras India Pvt Ltd' -> 'Saras India'" in text and "'Mahalakshmi'" in text
    assert "row 3: domain mismatch" in text

    # The owner corrects row 4's first name and approves it; row 3 stays in review.
    wb = load_workbook(result["review_path"])
    ws = wb["Review"]
    header = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == 4:
            row[header.index("First name")].value = "Venkat"
            row[header.index("Action")].value = "import"
    wb.save(result["review_path"])

    res = await importer.commit(src, state, None, None)
    assert [r.row for r in res["added"]] == [2, 4] and [r.row for r in res["held"]] == [3]
    added = {p.email: p for p in await state.get_prospects_by_status("new")}
    maha, venkat = added["maha@sarasindia.com"], added["ramana@acme.in"]
    assert (maha.first_name, maha.company, maha.source) == ("Mahalakshmi", "Saras India",
                                                             "uploaded_list")
    assert venkat.first_name == "Venkat"
    assert all(ln.startswith(IMPORT_PREFIX) for ln in maha.personalization_notes.splitlines())
    assert "your note: met at expo" in maha.personalization_notes
    company = await state.get_company(maha.company_id)
    assert company.name == "Saras India Pvt Ltd" and company.description == "SharePoint consultancy."

    again = await importer.commit(src, state, None, None)              # no duplicates
    assert again["added"] == [] and len(again["skipped"]) == 2

    write_list(src, [["Changed", "X", "x@y.in", "", ""]])
    with pytest.raises(ValueError, match="changed since the dry run"):
        await importer.commit(src, state, None, None)


# ── Shared pipeline rules ──

def test_opt_out_line_goes_before_the_sign_off_once():
    from harvey.signature import add_opt_out, apply_signature
    from types import SimpleNamespace as NS
    line = "If this isn't relevant, just let me know and I won't follow up."
    body = "Hi Ravi,\n\nDetail.\n\nWould a 15-minute call this week make sense?\n\nWarm regards,"
    out = apply_signature(add_opt_out(body, line), NS(signature="Haymant", name="H"))
    assert out.endswith(f"make sense?\n\n{line}\n\nWarm regards,\nHaymant")
    assert add_opt_out(add_opt_out(body, line), line).count(line) == 1
    assert add_opt_out(body, "") == body


@pytest.mark.asyncio
async def test_writer_adds_opt_out_to_email_1_only_and_hides_import_notes(state):
    from harvey.agents.writer import Writer
    from harvey.models.campaign import Campaign
    from tests.test_followup_threading import (REFERENCE_EMAIL_1, SARAS_FU1, SARAS_FU2,
                                               make_config)

    class Brain:
        prompts = []

        def load_prompt(self, *a, **k):
            return ""

        async def think_json(self, prompt, **kw):
            self.prompts.append(prompt)
            return {"subject": "Microsoft licensing & hosting for Saras India",
                    "body": REFERENCE_EMAIL_1, "followup_1": SARAS_FU1, "followup_2": SARAS_FU2}

    cfg = make_config()
    cfg.channels.email.opt_out_line = "If this isn't relevant, just let me know and I won't follow up."
    writer = Writer(brain=Brain(), state=state, config=cfg)
    p = Prospect(first_name="Haymant", last_name="Y", title="", company="Saras India",
                 industry="SharePoint and Office 365 services", email="h@sarasindia.com",
                 source="uploaded_list",
                 personalization_notes=f"{IMPORT_PREFIX} your note: owes us money")
    p.id = await state.add_prospect(p)
    cid = await state.add_campaign(Campaign(id="", name="t", channel="email", sequence=[],
                                            prospect_ids=[p.id], status="draft"))
    await writer._personalize_first_emails(cid, [p])
    rows = await state.get_sequence_rows(cid, p.id)
    assert cfg.channels.email.opt_out_line in rows[0]["body"]
    assert all(cfg.channels.email.opt_out_line not in r["body"] for r in rows[1:])
    prompt = Brain.prompts[0]
    assert "owes us money" not in prompt and IMPORT_PREFIX not in prompt
    assert "business decision-maker" in prompt


@pytest.mark.asyncio
async def test_stop_reply_unsubscribes_and_adds_to_do_not_contact(state):
    from harvey.integrations.mail_provider import InboundMessage
    from tests.test_outbox_native import FakeProvider, make_handler, seed_prospect
    pid = await seed_prospect(state, status="contacted")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="s2", body="b",
                                send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                status="pending_review", campaign_id="c1", step=2)
    provider = FakeProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email="jane@acme.com",
                                       subject="Re: hi", body="Thanks, but this is not relevant for us.")]
    await make_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert await state.is_suppressed("Jane@Acme.com")
    assert [r["status"] for r in await state.get_outbox()] == ["cancelled"]


@pytest.mark.asyncio
async def test_do_not_contact_list_blocks_sending(state):
    from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect
    pid = await seed_prospect(state, status="contacted")
    await state.add_suppression("jane@acme.com", "asked to stop")
    await state.add_outbox_item(prospect_id=pid, to_email="jane@acme.com", subject="Hi", body="b",
                                send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                status="approved", campaign_id="c1", step=1)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox())[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_imported_prospects_send_first_when_cap_is_tight(state):
    harvey_pid = await state.add_prospect(Prospect(first_name="H", last_name="L", title="CTO",
                                                   email="h@lead.in", source="dataforseo"))
    upload_pid = await state.add_prospect(Prospect(first_name="U", last_name="L", title="",
                                                   email="u@list.in", source="uploaded_list"))
    for pid, email, when in ((harvey_pid, "h@lead.in", "2026-11-16T04:00:00"),
                             (upload_pid, "u@list.in", "2026-11-16T05:00:00")):
        await state.add_outbox_item(prospect_id=pid, to_email=email, subject="s", body="b",
                                    send_at=when, status="approved", campaign_id=pid, step=1)
    plain = await state.get_outbox(status="approved")
    first = await state.get_outbox(status="approved", prefer_source="uploaded_list")
    assert [r["to_email"] for r in plain] == ["h@lead.in", "u@list.in"]
    assert [r["to_email"] for r in first] == ["u@list.in", "h@lead.in"]


@pytest.mark.asyncio
async def test_tracker_row_for_an_uploaded_contact(state):
    from harvey.models.company import Company
    from harvey.tracker import COLUMNS, build_rows
    cid = await state.add_company(Company(name="Spotmies Private Limi", domain="",
                                          website="https://reaidy.io", source="uploaded_list"))
    await state.add_prospect(Prospect(
        first_name="Kiran", last_name="", title="", company="Spotmies", company_id=cid,
        email="kiran@reaidy.io", email_status="verified", source="uploaded_list", phone="98480",
        personalization_notes=f"{IMPORT_PREFIX} website reaidy.io (email domain, low confidence).\n"
                              f"{IMPORT_PREFIX} review: domain mismatch."))
    row = dict(zip(COLUMNS, (await build_rows(state.db_path))[0]))
    assert row["Source"] == "Uploaded list" and row["Contact type"] == "Uploaded contact"
    assert row["Company"] == "Spotmies Private Limi" and row["Website"] == "https://reaidy.io"
    assert "low confidence" in row["Notes"] and "domain mismatch" in row["Notes"]
    assert row["Phone"] == "From list: 98480"
