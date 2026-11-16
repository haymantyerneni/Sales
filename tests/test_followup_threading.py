"""Follow-up threading, the signature, the send window, and the CloudBox99
writer style checks."""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio

from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.signature import apply_signature, from_header, placeholders, strip_signature
from harvey.state import StateManager

SIG = "Haymant Yerneni\nSales Team, CloudBox99, Hyderabad\n+91 90000 00000 | cloudbox99.com"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class FakeProvider(MailProvider):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbound = []

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", html=""):
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to, "html": html})
        n = len(self.sent)
        return SendResult(ok=True, message_id=f"<m{n}@mail.gmail.com>",
                          thread_ref=thread_ref or f"thread-{n}")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


def make_config(signature=SIG, window=("", ""), weekdays_only=False):
    return NS(
        persona=NS(name="Haymant Yerneni", from_name="Haymant Yerneni | CloudBox99",
                   email="haymant@cb99mail.com", company="CloudBox99", role="Sales Team",
                   tone="warm", signature=signature),
        product=NS(name="P", description="d", pricing="p", key_benefits=["b"],
                   objection_responses={}),
        channels=NS(email=NS(enabled=True, provider="smtp", max_daily_sends=50,
                             send_to_risky=False, require_approval=False,
                             max_bounce_rate=0.05, weekdays_only=weekdays_only,
                             send_window_start=window[0], send_window_end=window[1]),
                    linkedin=NS(enabled=False)),
        usage=NS(quiet_hours=NS(timezone="Asia/Kolkata")),
    )


def make_sender(state, provider, **cfg):
    from harvey.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=make_config(**cfg),
                    env=NS(instantly_api_key=""))
    sender.provider = provider
    sender.send_pacing = False
    return sender


async def seed(state, email="ravi@acmetech.in", estatus="verified"):
    pid = await state.add_prospect(Prospect(
        first_name="Ravi", last_name="K", title="CTO", company="Acme Tech",
        email=email, email_status=estatus, email_verified=True, status="new",
    ))
    campaign = Campaign(
        id="", name="it-outreach", channel="email",
        sequence=[
            EmailStep(step=1, subject="Cloud hosting & IT support for {{company}}",
                      body="Hi {{first_name}},\n\nIntro for {{company}}. Would this week work?",
                      delay_days=0),
            EmailStep(step=2, subject="Re: whatever the model wrote",
                      body="Hi {{first_name}},\n\nDid you see my note? Call?", delay_days=4),
            EmailStep(step=3, subject="Re: whatever", body="Hi {{first_name}},\n\nLast note. Ok?",
                      delay_days=6),
        ],
        prospect_ids=[pid], status="draft",
    )
    campaign.id = await state.add_campaign(campaign)
    return pid, campaign


async def rows(state, campaign, pid):
    return {r["step"]: r for r in await state.get_sequence_rows(campaign.id, pid)}


async def make_due(state, item_id):
    await state.update_outbox_item(item_id, send_at=(_now() - timedelta(minutes=1)).isoformat())


# ── signature ──


def test_signature_appended_once_and_replaces_model_signoff():
    persona = make_config().persona
    body = "Hi Ravi,\n\nWould Thursday work?\n\nBest regards,\nHaymant\nCloudBox99"
    signed = apply_signature(body, persona)
    assert signed == f"Hi Ravi,\n\nWould Thursday work?\n\nWarm regards,\n{SIG}"
    assert apply_signature(signed, persona) == signed       # idempotent
    assert strip_signature(signed) == "Hi Ravi,\n\nWould Thursday work?"


def test_from_header_and_placeholders():
    persona = make_config().persona
    # "|" is legal atext in a display name, so no quoting is needed.
    assert from_header(persona) == "Haymant Yerneni | CloudBox99 <haymant@cb99mail.com>"
    assert placeholders("call me on [phone] | cloudbox99.com") == ["[phone]"]
    assert placeholders("VPS (from ₹999/month)") == []


# ── threading ──


@pytest.mark.asyncio
async def test_followups_thread_onto_email1_and_schedule_from_send(state):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)

    await sender._run_native()          # stage (approved) + send email 1
    assert len(provider.sent) == 1
    first = provider.sent[0]
    assert first["subject"] == "Cloud hosting & IT support for Acme Tech"
    assert first["body"].endswith(f"Warm regards,\n{SIG}")

    r = await rows(state, campaign, pid)
    assert r[1]["status"] == "sent"
    for step, days in ((2, 4), (3, 10)):
        assert r[step]["thread_ref"] == "thread-1"
        assert r[step]["in_reply_to"] == "<m1@mail.gmail.com>"
        assert r[step]["subject"] == "Re: Cloud hosting & IT support for Acme Tech"
        due = datetime.fromisoformat(r[step]["send_at"])
        assert abs(due - (_now() + timedelta(days=days))) < timedelta(minutes=5)

    await make_due(state, r[2]["id"])
    await sender._drain_due()
    assert len(provider.sent) == 2
    fu = provider.sent[1]
    assert fu["thread_ref"] == "thread-1"
    assert fu["in_reply_to"] == "<m1@mail.gmail.com>"
    assert fu["subject"] == "Re: Cloud hosting & IT support for Acme Tech"
    assert fu["body"].endswith(f"Warm regards,\n{SIG}")


@pytest.mark.asyncio
async def test_followup_waits_while_email1_awaits_approval(state):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.channels.email.require_approval = True
    await sender._run_native()                       # all three pending_review
    r = await rows(state, campaign, pid)
    await state.approve_outbox(r[2]["id"])           # follow-up approved early
    await make_due(state, r[2]["id"])

    await sender._drain_due()
    assert provider.sent == []
    assert (await rows(state, campaign, pid))[2]["status"] == "approved"


@pytest.mark.parametrize("dead", ["rejected", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_followups_cancelled_when_email1_never_sent(state, dead):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.channels.email.require_approval = True
    await sender._run_native()
    r = await rows(state, campaign, pid)
    await state.update_outbox_item(r[1]["id"], status=dead)
    for step in (2, 3):
        await state.approve_outbox(r[step]["id"])
        await make_due(state, r[step]["id"])

    await sender._drain_due()
    assert provider.sent == []
    r = await rows(state, campaign, pid)
    assert r[2]["status"] == r[3]["status"] == "cancelled"
    assert r[2]["error"] == "email 1 was never sent"


@pytest.mark.asyncio
async def test_followups_cancelled_when_email1_bounced(state):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._run_native()                         # email 1 sent
    await state.update_prospect_email(pid, "ravi@acmetech.in", "invalid")
    r = await rows(state, campaign, pid)
    await make_due(state, r[2]["id"])

    await sender._drain_due()
    assert len(provider.sent) == 1
    r = await rows(state, campaign, pid)
    assert r[2]["status"] == "cancelled" and r[2]["error"] == "email 1 bounced"


@pytest.mark.asyncio
async def test_bounce_matched_by_thread_cancels_followups(state):
    from harvey.agents.handler import Handler
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._run_native()                         # email 1 sent in thread-1

    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)", body="not delivered",
        in_reply_to="", thread_ref="thread-1", is_bounce=True,
    )]
    brain = NS(load_skills_for_agent=lambda *_: "", load_prompt=lambda *a, **k: "")
    handler = Handler(brain=brain, state=state, config=sender.config,
                      env=NS(instantly_api_key=""))
    handler.provider = provider
    await handler._handle_bounce(provider.inbound[0])

    assert (await state.get_prospect(pid)).email_status == "invalid"
    r = await rows(state, campaign, pid)
    assert r[2]["status"] == r[3]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unfilled_signature_placeholder_blocks_send(state):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider,
                         signature="Haymant Yerneni\n[phone] | cloudbox99.com")
    await sender._run_native()
    assert provider.sent == []
    r = await rows(state, campaign, pid)
    assert r[1]["status"] == "failed" and "[phone]" in r[1]["error"]


# ── send window ──


def test_send_window_in_kolkata_time():
    from harvey.agents.sender import Sender
    s = Sender.__new__(Sender)
    s.config = make_config(window=("09:30", "12:00"))
    utc = timezone.utc
    # Wednesday 2026-11-18: 04:30 UTC = 10:00 IST (open), 07:00 UTC = 12:30 IST (closed)
    assert s._cold_hold_reason(datetime(2026, 11, 18, 4, 30, tzinfo=utc)) == ""
    assert "window" in s._cold_hold_reason(datetime(2026, 11, 18, 7, 0, tzinfo=utc))
    assert "window" in s._cold_hold_reason(datetime(2026, 11, 18, 3, 50, tzinfo=utc))


@pytest.mark.asyncio
async def test_window_holds_cold_email_but_not_replies(state, monkeypatch):
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider, window=("09:30", "12:00"))
    monkeypatch.setattr(sender, "_cold_hold_reason", lambda now=None: "outside the window")
    await state.add_outbox_item(
        prospect_id=pid, to_email="ravi@acmetech.in", subject="Re: hi", kind="reply",
        body="Hi Ravi,\n\nThursday works?", send_at=_now().isoformat(), status="approved",
    )
    await sender._run_native()
    assert [m["subject"] for m in provider.sent] == ["Re: hi"]


# ── writer style ──

REFERENCE_EMAIL_1 = """Hi Ravi,

I'm Haymant from CloudBox99, a Hyderabad-based cloud provider. Acme Tech builds logistics software for regional distributors.

We help tech teams cut hosting costs and simplify IT: VPS and dedicated servers from ₹999/month, cloud migration, storage and backup, and 24x7 SOC and managed IT. Everything is billed in rupees, with local support.

Open to a 15-minute call this week?

Warm regards,"""

# The brief's reference with its "[real detail]" left out (no detail found).
REFERENCE_EMAIL_1_NO_DETAIL = """Hi Ravi,

I'm Haymant from CloudBox99, a Hyderabad-based cloud provider.

We help tech teams cut hosting costs and simplify IT: VPS and dedicated servers from ₹999/month, cloud migration, storage and backup, and 24x7 SOC and managed IT. Everything is billed in rupees, with local support.

Open to a 15-minute call this week?

Warm regards,"""

REFERENCE_FU1 = """Hi {{first_name}},

An easy first step for {{company}} is moving one workload off AWS to compare cost.

Open to a 15-minute call on Thursday or Friday?

Warm regards,"""

REFERENCE_FU2 = """Hi {{first_name}},

Last note from me. If hosting comes up at {{company}} later, just reply here.

May I check back in a few months?

Warm regards,"""


def test_segments_order_the_services():
    from harvey.agents.writer import SERVICES, segment_for, services_for, services_sentence
    assert segment_for("SaaS startups")["key"] == "it"
    assert segment_for("diagnostic centres")["key"] == "health"
    assert segment_for("NBFCs")["key"] == "finance"
    assert segment_for("retail chains")["key"] == "mfg_retail"
    assert segment_for("D2C brands")["key"] == "ecommerce"
    it = services_for(segment_for("software companies"))
    assert services_sentence(it) == ("VPS and dedicated servers from ₹999/month, cloud migration, "
                                     "storage and backup, and 24x7 SOC and managed IT")
    clinic = services_for(segment_for("clinics"))
    assert clinic[0] == SERVICES["storage"]
    assert sorted(clinic) == sorted(SERVICES.values())        # all four, once each
    assert services_for(segment_for("banks"))[0] == SERVICES["managed"]
    # Industry outranks the description: a software firm serving manufacturers.
    assert segment_for("software companies",
                       "builds billing software for small manufacturers")["key"] == "it"
    assert segment_for("", "a chain of diagnostic labs")["key"] == "health"
    assert segment_for("Coworking spaces")["key"] == "mfg_retail"   # multi-site premises


def test_reference_drafts_pass_the_style_checks():
    from harvey.agents.writer import (
        FIRST_EMAIL_MAX_WORDS, FOLLOWUP_MAX_WORDS, first_email_problems,
        followup_problems, segment_for, services_for)
    from harvey.signature import strip_signature
    services = services_for(segment_for("software companies"))
    assert first_email_problems(REFERENCE_EMAIL_1, "Ravi", services) == []
    assert first_email_problems(REFERENCE_EMAIL_1_NO_DETAIL, "Ravi", services) == []
    assert followup_problems(REFERENCE_FU1, 2) == []
    assert followup_problems(REFERENCE_FU2, 3) == []
    assert (FIRST_EMAIL_MAX_WORDS, FOLLOWUP_MAX_WORDS) == (70, {2: 40, 3: 25})
    assert len(strip_signature(REFERENCE_EMAIL_1).split()) < 70


def test_style_checks_catch_violations():
    from harvey.agents.writer import (
        first_email_problems, followup_problems, segment_for, services_for)
    services = services_for(segment_for("software companies"))
    check = lambda body, svc=services: first_email_problems(body, "Ravi", svc)  # noqa: E731
    assert any("banned" in p for p in check(REFERENCE_EMAIL_1.replace("cloud migration", "free migration")))
    assert any("order" in p for p in check(REFERENCE_EMAIL_1, services_for(segment_for("banks"))))
    assert any("filler" in p for p in check(REFERENCE_EMAIL_1.replace("I'm Haymant", "Good morning. I'm Haymant")))
    assert any("filler" in p for p in check(REFERENCE_EMAIL_1.replace("I'm Haymant", "Hope you are well. I'm Haymant")))
    assert any("links" in p for p in check(REFERENCE_EMAIL_1.replace("local support.", "local support: https://cloudbox99.com")))
    assert any("rupees" in p for p in check(REFERENCE_EMAIL_1.replace("billed in rupees", "billed monthly")))
    assert any("15-minute" in p for p in check(REFERENCE_EMAIL_1.replace("a 15-minute call", "a call")))
    long = REFERENCE_EMAIL_1.replace("regional distributors.", "regional distributors " + "across many states " * 3 + ".")
    assert any("under 70 words" in p for p in check(long))
    listed = REFERENCE_EMAIL_1.replace("We help", "- We help")
    assert any("lists" in p for p in check(listed))
    assert any("filler" in p for p in followup_problems(REFERENCE_FU1.replace("An easy", "Hope your week is going well. An easy"), 2))
    assert any("repeat the services" in p for p in followup_problems(REFERENCE_FU1.replace("to compare cost", "from ₹999/month"), 2))
    assert any("under 25" in p for p in followup_problems(REFERENCE_FU2.replace("later,", "later, or backups, or IT support, or migration,"), 3))
    two_q = REFERENCE_FU2.replace("Last note from me.", "Last note from me. Busy week?")
    assert any("one question" in p for p in followup_problems(two_q, 3))


def test_parse_sequence_fixes_the_cadence():
    from harvey.agents.writer import Writer
    w = Writer.__new__(Writer)
    steps = w._parse_sequence([
        {"subject": "a", "body": "x", "delay_days": 3},
        {"subject": "b", "body": "y", "delay_days": 3},
        {"subject": "c", "body": "z", "delay_days": 4},
    ])
    assert [s.delay_days for s in steps] == [0, 4, 6]       # day 0, 4, 10


@pytest.mark.asyncio
async def test_personal_email_is_regenerated_until_it_follows_the_rules(state):
    from harvey.agents.writer import Writer

    class Brain:
        def __init__(self):
            self.calls = []

        def load_prompt(self, *a, **k):
            return ""

        async def think_json(self, prompt, **kw):
            self.calls.append(prompt)
            if len(self.calls) == 1:
                return {"subject": "Cloud hosting & IT support for Acme Tech",
                        "body": "Hi Ravi,\n\nGet a free migration now!"}
            return {"subject": "Cloud hosting & IT support for Acme Tech",
                    "body": REFERENCE_EMAIL_1}

    cfg = make_config(window=("09:30", "12:00"))
    brain = Brain()
    writer = Writer(brain=brain, state=state, config=cfg)
    prospect = Prospect(first_name="Ravi", last_name="K", title="CTO",
                        company="Acme Tech", industry="software companies",
                        email="ravi@acmetech.in")
    draft = await writer._write_personal_email(prospect)
    assert draft["body"] == REFERENCE_EMAIL_1
    assert len(brain.calls) == 2
    assert "broke these rules" in brain.calls[1]
