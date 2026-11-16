"""Lead tracker: rows from the database only, one per person, write-only
Google Sheets backend (never reads the sheet), Excel fallback."""

import json
from types import SimpleNamespace as NS

import httpx
import pytest
import pytest_asyncio

from harvey.models.company import Company
from harvey.models.prospect import TARGETING_PREFIX, Prospect
from harvey.state import StateManager
from harvey.tracker import (COLUMNS, STATUS_COL, ExcelTracker, GoogleSheetsTracker,
                            TrackerError, build_rows, person_status, source_label)


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "t.db"))
    await sm.init_db()
    return sm


def col(row, name):
    return row[COLUMNS.index(name)]


async def add_person(state, email, source="dataforseo", status="new", notes="", title="CTO",
                     phone="", company_phone=""):
    cid = await state.add_company(Company(name=f"Co {email}", domain=email.split("@")[1],
                                          location="Hyderabad", industry="software",
                                          phone=company_phone))
    return await state.add_prospect(Prospect(
        first_name="A", last_name="B", title=title, company=f"Co {email}", company_id=cid,
        email=email, email_status="verified", source=source, status=status,
        personalization_notes=notes, phone=phone))


def test_status_mapping():
    p = {"status": "contacted", "email_status": "verified", "source": "scout"}
    sent = lambda *steps: [{"step": s, "status": "sent", "kind": "sequence"} for s in steps]  # noqa: E731
    assert person_status(p, []) == "found"
    assert person_status(p, [{"step": 1, "status": "pending_review", "kind": "sequence"}]) == "queued"
    assert person_status(p, sent(1)) == "Email 1 sent"
    assert person_status(p, sent(1, 2)) == "Follow-up 1 sent"
    assert person_status(p, sent(1, 2, 3)) == "Follow-up 2 sent"
    assert person_status({**p, "status": "replied"}, sent(1)) == "replied"
    assert person_status({**p, "status": "meeting"}, sent(1)) == "meeting"
    assert person_status({**p, "status": "opted_out"}, sent(1)) == "unsubscribed"
    assert person_status({**p, "email_status": "invalid"}, sent(1)) == "bounced"
    assert person_status({**p, "source": "test"}, sent(1, 2)) == "cancelled"
    assert person_status(p, [{"step": 1, "status": "cancelled", "kind": "sequence"}]) == "cancelled"
    assert source_label("test") == "Test" and source_label("csv_upload") == "Uploaded list"
    assert source_label("dataforseo_listings") == "Harvey lead gen"


@pytest.mark.asyncio
async def test_rows_one_per_person_with_all_columns(state):
    pid = await add_person(state, "cto@acme.in", phone="+91 1", company_phone="+91 40 2")
    fb = await add_person(state, "ceo@beta.in", title="Founder",
                          notes=f"Runs three sites.\n{TARGETING_PREFIX} fallback contact.")
    t = await add_person(state, "me@gmail.com", source="test", status="cancelled")
    await state.add_outbox_item(prospect_id=pid, campaign_id="c", step=1, to_email="cto@acme.in",
                                subject="s", body="b", send_at="2026-11-16T04:00:00",
                                status="pending_review")
    await state.add_outbox_item(prospect_id=t, campaign_id="c", step=1, to_email="me@gmail.com",
                                subject="s", body="b", send_at="2026-11-16T04:00:00", status="sent")
    await state.update_outbox_item(
        (await state.get_sequence_rows("c", t))[0]["id"], sent_at="2026-11-16T06:30:00")

    rows = await build_rows(state.db_path)
    assert len(rows) == 3 and all(len(r) == len(COLUMNS) for r in rows)
    by = {col(r, "Email"): r for r in rows}
    cto, ceo, test = by["cto@acme.in"], by["ceo@beta.in"], by["me@gmail.com"]
    assert col(cto, "Contact type") == "CTO" and col(cto, "Status") == "queued"
    assert col(cto, "Next step date") == "2026-11-16"
    assert col(cto, "Phone") == "Company: +91 40 2 | Direct: +91 1"
    assert col(cto, "City") == "Hyderabad" and col(cto, "Segment").startswith("IT services")
    assert col(ceo, "Contact type") == "CEO fallback"
    assert TARGETING_PREFIX not in col(ceo, "Notes") and "Runs three sites." in col(ceo, "Notes")
    assert col(test, "Source") == "Test" and col(test, "Status") == "cancelled"
    assert col(test, "Last contacted") == "2026-11-16 12:00"          # IST
    assert col(test, "Next step date") == ""


@pytest.mark.asyncio
async def test_excel_backend_freezes_filters_and_colours(state, tmp_path):
    from openpyxl import load_workbook
    await add_person(state, "me@gmail.com", source="test", status="cancelled")
    await add_person(state, "x@acme.in", status="replied")
    cfg = NS(tracker=NS(excel_path=str(tmp_path / "lead_tracker.xlsx")))
    path = await ExcelTracker(cfg).write(await build_rows(state.db_path))
    ws = load_workbook(path).active
    assert [c.value for c in ws[1]] == COLUMNS
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref
    fills = {ws.cell(row=r, column=STATUS_COL + 1).value:
             ws.cell(row=r, column=STATUS_COL + 1).fill.fgColor.rgb for r in (2, 3)}
    assert fills["cancelled"].endswith("D8D8D8") and fills["replied"].endswith("CCEDCC")


class FakeGoogle:
    """Records every request; serves token refresh, create, batchUpdate,
    clear and update. Any GET (a read) fails the test."""

    def __init__(self, missing_once=False):
        self.calls = []
        self.missing_once = missing_once
        self.created = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.method != "GET", "the tracker must never read from the sheet"
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at"})
        path = request.url.path
        if path == "/v4/spreadsheets":
            self.created += 1
            return httpx.Response(200, json={
                "spreadsheetId": f"sheet{self.created}",
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/sheet{self.created}/edit",
                "sheets": [{"properties": {"sheetId": 7}}]})
        if path.endswith(":batchUpdate"):
            return httpx.Response(200, json={})
        if self.missing_once and ":clear" in path:
            self.missing_once = False
            return httpx.Response(404, json={})
        if request.method == "PUT":
            self.last_values = json.loads(request.content)["values"]
        return httpx.Response(200, json={})


def sheets_tracker(tmp_path, fake, account="owner@gmail.com", token_account="owner@gmail.com"):
    token = tmp_path / "tok.json"
    token.write_text(json.dumps({"refresh_token": "r", "account": token_account}))
    cfg = NS(tracker=NS(account=account, sheet_name="CloudBox99 Lead Tracker"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return GoogleSheetsTracker(cfg, NS(gmail_client_id="i", gmail_client_secret="s"),
                               token_file=token, state_file=tmp_path / "state.json", client=client)


@pytest.mark.asyncio
async def test_sheets_backend_creates_once_then_only_writes(tmp_path):
    fake = FakeGoogle()
    rows = [["2026-11-16", "Test", "A B"] + [""] * (len(COLUMNS) - 3)]
    url = await sheets_tracker(tmp_path, fake).write(rows)
    assert url.endswith("/sheet1/edit") and fake.created == 1
    assert fake.last_values[0] == COLUMNS and fake.last_values[1] == rows[0]
    await sheets_tracker(tmp_path, fake).write(rows)                   # second sync: no new sheet
    assert fake.created == 1
    assert not [c for c in fake.calls if c[0] == "GET"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["spreadsheet_id"] == "sheet1" and state["account"] == "owner@gmail.com"


@pytest.mark.asyncio
async def test_sheets_backend_recreates_a_deleted_sheet_and_moves_with_the_account(tmp_path):
    fake = FakeGoogle()
    await sheets_tracker(tmp_path, fake).write([])
    fake.missing_once = True
    url = await sheets_tracker(tmp_path, fake).write([])
    assert fake.created == 2 and url.endswith("/sheet2/edit")
    # tracker.account changed (e.g. to the Workspace account) with a matching token:
    url = await sheets_tracker(tmp_path, fake, account="sales@cloudbox99.com",
                               token_account="sales@cloudbox99.com").write([])
    assert fake.created == 3 and url.endswith("/sheet3/edit")


@pytest.mark.asyncio
async def test_sheets_backend_refuses_a_token_for_another_account(tmp_path):
    with pytest.raises(TrackerError, match="harvey tracker auth"):
        await sheets_tracker(tmp_path, FakeGoogle(), account="sales@cloudbox99.com",
                             token_account="owner@gmail.com").write([])


def test_format_requests_freeze_filter_and_status_colours():
    reqs = GoogleSheetsTracker._format_requests(7)
    kinds = [next(iter(r)) for r in reqs]
    assert "setBasicFilter" in kinds and kinds.count("addConditionalFormatRule") == 3
    formulas = [r["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]
                ["userEnteredValue"] for r in reqs if "addConditionalFormatRule" in r]
    assert any('"replied"' in f and '"meeting"' in f for f in formulas)
    assert any('"bounced"' in f and '"unsubscribed"' in f for f in formulas)
    assert any('"cancelled"' in f for f in formulas)


def test_unticked_drive_permission_is_caught():
    from harvey.integrations.gmail import missing_scopes
    from harvey.tracker import TRACKER_SCOPE
    granted = "https://www.googleapis.com/auth/userinfo.email openid"
    assert missing_scopes(TRACKER_SCOPE, granted) == ["https://www.googleapis.com/auth/drive.file"]
    assert missing_scopes(TRACKER_SCOPE, granted + " https://www.googleapis.com/auth/drive.file") == []


def test_id_token_email_is_read_from_the_payload():
    import base64
    from harvey.integrations.gmail import _id_token_email
    payload = base64.urlsafe_b64encode(json.dumps({"email": "Owner@Gmail.com"}).encode()).decode().rstrip("=")
    assert _id_token_email(f"h.{payload}.sig") == "owner@gmail.com"
    assert _id_token_email("garbage") == ""
