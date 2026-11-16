"""Lead tracker: one row per person, kept in a Google Sheet (or an Excel
file as the fallback backend).

WRITE-ONLY by design. Every row is built from Harvey's own database and
the whole sheet is rewritten on each sync; Harvey never reads values,
notes or instructions back from the sheet, so nothing anyone types there
can change what Harvey does (and any manual edit is overwritten on the
next sync). The spreadsheet id lives in data/tracker_state.json, not in
the sheet.

Synced at the end of every heartbeat cycle and by `harvey tracker sync`.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx

from harvey.models.prospect import IMPORT_PREFIX, TARGETING_PREFIX
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.tracker")

COLUMNS = [
    "Date added", "Source", "Name", "Title", "Contact type", "Company", "Website",
    "City", "Segment", "Email", "Email status", "Phone", "Status", "Last contacted",
    "Next step date", "Notes",
]
STATUS_COL = COLUMNS.index("Status")             # 12 → column M
SHEET_TAB = "Leads"

# Status colours (Sheets and Excel): green / red / grey.
STATUS_COLOURS = {
    "replied": "green", "meeting": "green",
    "bounced": "red", "unsubscribed": "red",
    "cancelled": "grey",
}
RGB = {"green": (0.80, 0.93, 0.80), "red": (0.96, 0.80, 0.80), "grey": (0.85, 0.85, 0.85)}

DATA_DIR = PROJECT_ROOT / "data"
TOKEN_FILE = DATA_DIR / "tracker_token.json"
STATE_FILE = DATA_DIR / "tracker_state.json"
TRACKER_SCOPE = "openid email https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

OPEN_ITEM_STATUSES = ("pending_review", "approved")


class TrackerError(Exception):
    pass


# ── rows (from Harvey's database only) ─────────────────────────────────

def _local(ts: str | None, tz: str, with_time: bool) -> str:
    if not ts:
        return ""
    try:
        import pytz
        dt = datetime.fromisoformat(str(ts).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(pytz.timezone(tz))
        return dt.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")
    except (ValueError, TypeError, Exception):
        return str(ts)[:16]


def source_label(source: str) -> str:
    s = (source or "").strip().lower()
    if s == "test" or s.startswith("test"):
        return "Test"
    if any(k in s for k in ("upload", "import", "csv", "xlsx", "spreadsheet")):
        return "Uploaded list"
    return "Harvey lead gen"


def person_status(prospect: dict, items: list[dict]) -> str:
    """One status per person, most decisive first."""
    p_status = (prospect.get("status") or "").lower()
    if p_status == "opted_out":
        return "unsubscribed"
    if (prospect.get("email_status") or "").lower() == "invalid":
        return "bounced"
    if p_status in ("meeting", "closed"):
        return "meeting"
    if p_status == "replied":
        return "replied"
    if source_label(prospect.get("source", "")) == "Test" or p_status in ("cancelled", "lost"):
        return "cancelled"
    seq = [i for i in items if (i.get("kind") or "sequence") == "sequence"]
    sent = [int(i["step"]) for i in seq if i.get("status") == "sent"]
    if sent:
        return {1: "Email 1 sent", 2: "Follow-up 1 sent"}.get(max(sent), "Follow-up 2 sent")
    if any(i.get("status") in OPEN_ITEM_STATUSES for i in seq):
        return "queued"
    if seq and all(i.get("status") in ("cancelled", "rejected", "failed") for i in seq):
        return "cancelled"
    return "queued" if p_status == "queued" else "found"


def _sent_summary(items: list[dict]) -> str:
    names = {1: "Email 1", 2: "FU1", 3: "FU2"}
    sent = sorted({int(i["step"]) for i in items
                   if i.get("status") == "sent" and (i.get("kind") or "sequence") == "sequence"})
    return ", ".join(names.get(s, f"step {s}") for s in sent)


def build_row(prospect: dict, company: dict, items: list[dict], tz: str) -> list[str]:
    from harvey.agents.writer import segment_for

    notes_lines = [ln.strip() for ln in (prospect.get("personalization_notes") or "").splitlines()
                   if ln.strip()]
    fallback = any(ln.startswith(TARGETING_PREFIX) for ln in notes_lines)
    imported = [ln[len(IMPORT_PREFIX):].strip() for ln in notes_lines
                if ln.startswith(IMPORT_PREFIX)]
    facts = [ln for ln in notes_lines if not ln.startswith((TARGETING_PREFIX, IMPORT_PREFIX))]
    status = person_status(prospect, items)
    uploaded = source_label(prospect.get("source", "")) == "Uploaded list"

    phones = []
    if company.get("phone"):
        phones.append(f"Company: {company['phone']}")
    if prospect.get("phone"):
        phones.append(f"{'From list' if uploaded else 'Direct'}: {prospect['phone']}")
    website = company.get("website") or (f"https://{company['domain']}" if company.get("domain") else "")

    seq = [i for i in items if (i.get("kind") or "sequence") == "sequence"]
    sent_at = [i["sent_at"] for i in items if i.get("status") == "sent" and i.get("sent_at")]
    upcoming = [i["send_at"] for i in seq if i.get("status") in OPEN_ITEM_STATUSES and i.get("send_at")]

    notes = []
    if source_label(prospect.get("source", "")) == "Test":
        notes.append("Owner test recipient: never contacted again.")
    if fallback:
        notes.append("CEO/Founder fallback: no CTO-level contact found.")
    summary = _sent_summary(items)
    if summary:
        notes.append(f"Sent: {summary}.")
    if imported:
        notes.append(" ".join(imported))
    if facts:
        notes.append(" ".join(facts))
    segment = segment_for(prospect.get("industry", ""), company.get("industry", ""),
                          company.get("description", ""), company.get("name", ""))

    return [
        _local(prospect.get("created_at"), tz, with_time=False),
        source_label(prospect.get("source", "")),
        " ".join(filter(None, [prospect.get("first_name"), prospect.get("last_name")])),
        prospect.get("title") or "",
        "Uploaded contact" if uploaded else ("CEO fallback" if fallback else "CTO"),
        company.get("name") or prospect.get("company") or "",
        website,
        company.get("location") or "",
        segment["label"],
        prospect.get("email") or "",
        prospect.get("email_status") or "",
        " | ".join(phones),
        status,
        _local(max(sent_at), tz, with_time=True) if sent_at else "",
        _local(min(upcoming), tz, with_time=False)
        if upcoming and status not in ("cancelled", "bounced", "unsubscribed") else "",
        " ".join(notes)[:500],
    ]


async def build_rows(db_path: str, tz: str = "Asia/Kolkata") -> list[list[str]]:
    """Every person with an email, one row each (deduped by email)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        prospects = [dict(r) for r in await (await db.execute(
            "SELECT * FROM prospects WHERE email IS NOT NULL AND email != '' "
            "ORDER BY created_at, email")).fetchall()]
        companies = {r["id"]: dict(r) for r in await (await db.execute(
            "SELECT * FROM companies")).fetchall()}
        items: dict[str, list[dict]] = {}
        for r in await (await db.execute(
                "SELECT prospect_id, step, status, kind, sent_at, send_at FROM outbox")).fetchall():
            items.setdefault(r["prospect_id"], []).append(dict(r))

    by_email: dict[str, list[str]] = {}
    for p in prospects:
        email = (p.get("email") or "").strip().lower()
        if not email:
            continue
        row = build_row(p, companies.get(p.get("company_id") or "", {}), items.get(p["id"], []), tz)
        by_email[email] = row          # later (newer) records win: one row per person
    return list(by_email.values())


# ── Google Sheets backend ──────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _hex_rgb(colour: str) -> dict:
    r, g, b = RGB[colour]
    return {"red": r, "green": g, "blue": b}


class GoogleSheetsTracker:
    def __init__(self, config, env, token_file: Path = TOKEN_FILE, state_file: Path = STATE_FILE,
                 client: httpx.AsyncClient | None = None):
        self.cfg = config.tracker
        self.client_id = getattr(env, "gmail_client_id", "")
        self.client_secret = getattr(env, "gmail_client_secret", "")
        self.token_file = token_file
        self.state_file = state_file
        self._client = client
        self._access = ""

    async def _token(self, client) -> str:
        if self._access:
            return self._access
        tok = _read_json(self.token_file)
        if not tok.get("refresh_token"):
            raise TrackerError("not authorised yet: run `harvey tracker auth`")
        account = (self.cfg.account or "").lower()
        if account and tok.get("account") and tok["account"] != account:
            raise TrackerError(f"the tracker token is for {tok['account']}, but tracker.account is "
                               f"{account}: run `harvey tracker auth`")
        resp = await client.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if resp.status_code != 200:
            raise TrackerError(f"token refresh failed ({resp.status_code}): run `harvey tracker auth`")
        self._access = resp.json().get("access_token", "")
        return self._access

    async def _call(self, client, method: str, url: str, **kw) -> dict:
        headers = {"Authorization": f"Bearer {await self._token(client)}"}
        resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code == 404:
            raise FileNotFoundError(url)
        if resp.status_code >= 400:
            raise TrackerError(f"Sheets API {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    async def _create(self, client) -> dict:
        created = await self._call(client, "POST", SHEETS_API, json={
            "properties": {"title": self.cfg.sheet_name},
            "sheets": [{"properties": {"title": SHEET_TAB,
                                       "gridProperties": {"frozenRowCount": 1}}}],
        })
        sheet_id = created["sheets"][0]["properties"]["sheetId"]
        await self._call(client, "POST", f"{SHEETS_API}/{created['spreadsheetId']}:batchUpdate",
                         json={"requests": self._format_requests(sheet_id)})
        state = {
            "account": (self.cfg.account or "").lower(),
            "spreadsheet_id": created["spreadsheetId"],
            "url": created.get("spreadsheetUrl", ""),
            "sheet_id": sheet_id,
            "created_at": int(time.time()),
        }
        _write_json(self.state_file, state)
        logger.info(f"Tracker: created '{self.cfg.sheet_name}' in {state['account'] or 'Drive'}: "
                    f"{state['url']}")
        return state

    @staticmethod
    def _format_requests(sheet_id: int) -> list[dict]:
        n = len(COLUMNS)
        whole = {"sheetId": sheet_id, "startColumnIndex": 0, "endColumnIndex": n}
        status_range = {"sheetId": sheet_id, "startRowIndex": 1,
                        "startColumnIndex": STATUS_COL, "endColumnIndex": STATUS_COL + 1}
        col = chr(ord("A") + STATUS_COL)
        rules = []
        for colour in ("green", "red", "grey"):
            values = [v for v, c in STATUS_COLOURS.items() if c == colour]
            formula = "=OR(" + ",".join(f'${col}2="{v}"' for v in values) + ")"
            rules.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [status_range],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": _hex_rgb(colour)},
                }}}})
        return [
            {"repeatCell": {
                "range": {**whole, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                               "backgroundColor": {"red": 0.91, "green": 0.93, "blue": 0.96}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
            {"setBasicFilter": {"filter": {"range": whole}}},
            {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                     "startIndex": 0, "endIndex": n}}},
            *rules,
        ]

    async def write(self, rows: list[list[str]]) -> str:
        own = self._client is None
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            state = _read_json(self.state_file)
            account = (self.cfg.account or "").lower()
            if not state.get("spreadsheet_id") or state.get("account", "") != account:
                state = await self._create(client)
            for attempt in (1, 2):
                sid = state["spreadsheet_id"]
                try:
                    await self._call(client, "POST", f"{SHEETS_API}/{sid}/values/{SHEET_TAB}!A2:Z:clear",
                                     json={})
                    await self._call(client, "PUT", f"{SHEETS_API}/{sid}/values/{SHEET_TAB}!A1",
                                     params={"valueInputOption": "RAW"},
                                     json={"values": [COLUMNS] + rows})
                    return state.get("url") or f"https://docs.google.com/spreadsheets/d/{sid}/edit"
                except FileNotFoundError:
                    if attempt == 2:
                        raise TrackerError("the tracker sheet could not be found or recreated")
                    logger.warning("Tracker: sheet not found (deleted?); creating a new one.")
                    state = await self._create(client)
        finally:
            if own:
                await client.aclose()


# ── Excel backend ──────────────────────────────────────────────────────

class ExcelTracker:
    def __init__(self, config):
        path = Path(config.tracker.excel_path)
        self.path = path if path.is_absolute() else PROJECT_ROOT / path

    def _write_sync(self, rows: list[list[str]]) -> str:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill

        wb = Workbook()
        ws = wb.active
        ws.title = SHEET_TAB
        ws.append(COLUMNS)
        for row in rows:
            ws.append(row)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        fills = {c: PatternFill("solid", fgColor="%02X%02X%02X" % tuple(int(v * 255) for v in RGB[c]))
                 for c in RGB}
        for r in range(2, ws.max_row + 1):
            cell = ws.cell(row=r, column=STATUS_COL + 1)
            colour = STATUS_COLOURS.get(str(cell.value or ""))
            if colour:
                cell.fill = fills[colour]
        for i, name in enumerate(COLUMNS, start=1):
            longest = max([len(name)] + [len(str(r[i - 1])) for r in rows]) if rows else len(name)
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(longest + 2, 10), 60)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.xlsx")
        wb.save(tmp)
        os.replace(tmp, self.path)
        return str(self.path)

    async def write(self, rows: list[list[str]]) -> str:
        return await asyncio.get_running_loop().run_in_executor(None, self._write_sync, rows)


# ── entry points ───────────────────────────────────────────────────────

def make_tracker(config, env):
    if config.tracker.backend == "excel":
        return ExcelTracker(config)
    return GoogleSheetsTracker(config, env)


async def sync_tracker(config, env, db_path: str) -> tuple[int, str]:
    """Rewrite the tracker from the database. Returns (rows, link or path)."""
    tz = getattr(getattr(config.usage, "quiet_hours", None), "timezone", "") or "Asia/Kolkata"
    rows = await build_rows(db_path, tz)
    where = await make_tracker(config, env).write(rows)
    return len(rows), where


async def sync_tracker_safely(config, env, db_path: str, timeout: float = 120) -> None:
    """End-of-cycle sync: never raises, never blocks the heartbeat for long."""
    if not getattr(config, "tracker", None) or not config.tracker.enabled:
        return
    try:
        n, where = await asyncio.wait_for(sync_tracker(config, env, db_path), timeout)
        logger.info(f"Tracker: synced {n} row(s) to {where}")
    except Exception as e:
        logger.warning(f"Tracker: sync skipped: {e}")


def tracker_link() -> str:
    state = _read_json(STATE_FILE)
    return state.get("url", "")
