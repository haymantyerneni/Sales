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


def make_config(signature=SIG, window=("", ""), weekdays_only=False, exempt=()):
    return NS(
        persona=NS(name="Haymant Yerneni", from_name="Haymant Yerneni | CloudBox99",
                   email="haymant@cb99mail.com", company="CloudBox99", role="Sales Team",
                   tone="warm", signature=signature),
        product=NS(name="P", description="d", pricing="p", key_benefits=["b"],
                   objection_responses={}),
        channels=NS(email=NS(enabled=True, provider="smtp", max_daily_sends=50,
                             send_to_risky=False, require_approval=False,
                             max_bounce_rate=0.05, weekdays_only=weekdays_only,
                             send_window_start=window[0], send_window_end=window[1],
                             window_exempt=list(exempt)),
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


def test_real_prospect_window_is_weekdays_0930_to_1800_ist():
    from datetime import datetime, timezone
    sender = make_sender(None, FakeProvider(), window=("09:30", "18:00"), weekdays_only=True)
    utc = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)  # noqa: E731
    assert sender._cold_hold_reason(utc(16, 4, 0)) == ""            # Mon 09:30 IST
    assert sender._cold_hold_reason(utc(16, 12, 29)) == ""          # Mon 17:59 IST
    assert "window" in sender._cold_hold_reason(utc(16, 12, 30))    # Mon 18:00 IST
    assert "window" in sender._cold_hold_reason(utc(16, 3, 59))     # Mon 09:29 IST
    assert sender._cold_hold_reason(utc(21, 6, 0)) == "weekend"     # Sat 11:30 IST


@pytest.mark.asyncio
async def test_owner_test_addresses_ignore_the_window(state, monkeypatch):
    pid, campaign = await seed(state)
    pid2, campaign2 = await seed(state, email="owner@gmail.com")
    provider = FakeProvider()
    sender = make_sender(state, provider, window=("09:30", "18:00"), weekdays_only=True,
                         exempt=["owner@gmail.com"])
    monkeypatch.setattr(sender, "_cold_hold_reason", lambda now=None: "weekend")
    for p, c, to in ((pid, campaign, "ravi@acmetech.in"), (pid2, campaign2, "owner@gmail.com")):
        await state.add_outbox_item(prospect_id=p, campaign_id=c.id, step=1, to_email=to,
                                    subject="Hosting for Acme", body="Hi,\n\nCall?",
                                    send_at=_now().isoformat(), status="approved")
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["owner@gmail.com"]   # real prospect waits


# ── writer style ──

REFERENCE_EMAIL_1 = """Hi Haymant,

I came across Saras India's SharePoint and Office 365 work, both online and on-premises. Firms delivering that for clients usually need reliable servers and a dependable source for Microsoft licences.

That's what we do at CloudBox99 in Hyderabad: Microsoft licensing, VPS and dedicated servers from ₹999/month, cloud migration, storage and backup, and 24x7 SOC and managed IT.

Would a 15-minute call this week make sense?

Warm regards,"""

# No detail found: a general line about the segment, claiming nothing about them.
REFERENCE_EMAIL_1_NO_DETAIL = """Hi Ravi,

Software teams usually need servers that cost less than the hyperscalers, with someone local to call.

That's what we do at CloudBox99 in Hyderabad: VPS and dedicated servers from ₹999/month, cloud migration, Microsoft licensing, storage and backup, and 24x7 SOC and managed IT.

Would a 15-minute call this week make sense?

Warm regards,"""

REFERENCE_FU1 = """Hi {{first_name}},

An easy first step for {{company}} is a cost comparison for one workload on our servers.

Open to a 15-minute call this week or next?

Warm regards,"""

# The brief's own FU1 for Saras India (continues email 1's Microsoft angle).
SARAS_FU1 = """Hi Haymant,

If it's useful, we could start with a quick review of Saras India's Microsoft licensing or the servers behind your on-premises SharePoint work.

Would a 15-minute call this week or next suit you?

Warm regards,"""

SARAS_FU2 = """Hi Haymant,

Last note. If Microsoft licensing comes up at Saras India, just reply here.

May I check back in a few months?

Warm regards,"""

REFERENCE_FU2 = """Hi {{first_name}},

Last note from me. If hosting comes up at {{company}} later, just reply here.

May I check back in a few months?

Warm regards,"""

SARAS = dict(industry="IT services / software",
             detail="Saras India delivers SharePoint and Office 365 solutions.")


def saras_services():
    from harvey.agents.writer import segment_for, services_for, works_with_microsoft
    return services_for(segment_for(SARAS["industry"]),
                        microsoft=works_with_microsoft(SARAS["industry"], SARAS["detail"]))


def test_segments_order_the_services():
    from harvey.agents.writer import (SERVICES, segment_for, services_for, services_sentence,
                                      works_with_microsoft)
    assert segment_for("SaaS startups")["key"] == "it"
    assert segment_for("diagnostic centres")["key"] == "health"
    assert segment_for("NBFCs")["key"] == "finance"
    assert segment_for("retail chains")["key"] == "mfg_retail"
    assert segment_for("D2C brands")["key"] == "ecommerce"
    assert len(SERVICES) == 5 and SERVICES["microsoft"] == "Microsoft licensing"
    it = services_for(segment_for("software companies"))
    assert services_sentence(it) == ("VPS and dedicated servers from ₹999/month, cloud migration, "
                                     "Microsoft licensing, storage and backup, and 24x7 SOC and managed IT")
    clinic = services_for(segment_for("clinics"))
    assert clinic[0] == SERVICES["storage"]
    assert sorted(clinic) == sorted(SERVICES.values())        # all five, once each
    assert services_for(segment_for("banks"))[0] == SERVICES["managed"]
    # Industry outranks the description: a software firm serving manufacturers.
    assert segment_for("software companies",
                       "builds billing software for small manufacturers")["key"] == "it"
    assert segment_for("", "a chain of diagnostic labs")["key"] == "health"
    assert segment_for("Coworking spaces")["key"] == "mfg_retail"   # multi-site premises


def test_microsoft_companies_hear_about_licensing_first():
    from harvey.agents.writer import SERVICES, segment_for, services_for, works_with_microsoft
    for text in ("SharePoint portals", "Office 365 migrations", "Dynamics 365 partner",
                 "Azure consulting", "a Microsoft Gold partner", "M365 rollouts"):
        assert works_with_microsoft(text), text
    for text in ("logistics software", "coworking spaces", "dental clinics", "Teamsters union"):
        assert not works_with_microsoft(text), text
    assert saras_services()[:3] == [SERVICES["microsoft"], SERVICES["vps"], SERVICES["migration"]]
    banks = services_for(segment_for("banks"), microsoft=True)
    assert banks[:2] == [SERVICES["microsoft"], SERVICES["managed"]]
    assert sorted(banks) == sorted(SERVICES.values())


def test_reference_drafts_pass_the_style_checks():
    from harvey.agents.writer import (
        FIRST_EMAIL_MAX_WORDS, FOLLOWUP_MAX_WORDS, first_email_problems,
        followup_problems, segment_for, services_for, _subject_problems)
    from harvey.signature import strip_signature
    assert first_email_problems(REFERENCE_EMAIL_1, "Haymant", saras_services()) == []
    assert first_email_problems(REFERENCE_EMAIL_1_NO_DETAIL, "Ravi",
                                services_for(segment_for("software companies"))) == []
    assert _subject_problems("Microsoft licensing & hosting for Saras India") == []
    assert followup_problems(REFERENCE_FU1, 2) == []
    assert followup_problems(REFERENCE_FU2, 3) == []
    assert (FIRST_EMAIL_MAX_WORDS, FOLLOWUP_MAX_WORDS) == (70, {2: 40, 3: 25})
    assert len(strip_signature(REFERENCE_EMAIL_1).split()) < 70


def test_style_checks_catch_violations():
    from harvey.agents.writer import (
        first_email_problems, followup_problems, segment_for, services_for)
    services = saras_services()
    check = lambda body, svc=services: first_email_problems(body, "Haymant", svc)  # noqa: E731
    ref = REFERENCE_EMAIL_1
    assert any("banned" in p for p in check(ref.replace("cloud migration", "free migration")))
    assert any("order" in p for p in check(ref, services_for(segment_for("banks"))))
    assert any("filler" in p for p in check(ref.replace("I came across", "Good morning. I came across")))
    assert any("filler" in p for p in check(ref.replace("I came across", "Hope you are well. I came across")))
    assert any("I'm Haymant" in p for p in check(ref.replace("I came across", "I'm Haymant from CloudBox99. I came across")))
    assert any("rupee" in p for p in check(ref.replace("managed IT.", "managed IT, billed in rupees.")))
    assert any("rupee" in p for p in check(ref.replace("managed IT.", "managed IT. INR invoices.")))
    assert any("That's what we do" in p for p in check(ref.replace("That's what we do at CloudBox99 in Hyderabad:", "We offer")))
    assert check(ref.replace("That's", "That’s")) == []                 # curly apostrophe is fine
    assert any("phrases exactly" in p for p in check(ref.replace("Microsoft licensing, VPS", "VPS")))
    assert any("links" in p for p in check(ref.replace("this week", "this week (https://cloudbox99.com)")))
    assert any("15-minute" in p for p in check(ref.replace("a 15-minute call", "a call")))
    long = ref.replace("on-premises.", "on-premises " + "across many states " * 3 + ".")
    assert any("under 70 words" in p for p in check(long))
    assert any("lists" in p for p in check(ref.replace("That's what", "- That's what")))
    assert any("filler" in p for p in followup_problems(REFERENCE_FU1.replace("An easy", "Hope your week is going well. An easy"), 2))
    assert any("repeat the services" in p for p in followup_problems(REFERENCE_FU1.replace("on our servers", "from ₹999/month"), 2))
    assert any("rupee" in p for p in followup_problems(REFERENCE_FU1.replace("on our servers", "billed in rupees"), 2))
    assert any("under 25" in p for p in followup_problems(REFERENCE_FU2.replace("later,", "later, or backups, or IT support, or migration,"), 3))
    two_q = REFERENCE_FU2.replace("Last note from me.", "Last note from me. Busy week?")
    assert any("one question" in p for p in followup_problems(two_q, 3))


def test_follow_ups_never_name_weekdays_or_assume_tools():
    from harvey.agents.writer import assumed_tools, first_email_problems, followup_problems
    facts = "- Research notes: " + SARAS["detail"]
    assert followup_problems(SARAS_FU1, 2, "Haymant", facts) == []
    assert followup_problems(SARAS_FU2, 3, "Haymant", facts) == []
    thu = SARAS_FU1.replace("this week or next", "on Thursday")
    assert any("weekday" in p for p in followup_problems(thu, 2, "Haymant", facts))
    aws = SARAS_FU1.replace("a quick review of Saras India's Microsoft licensing",
                            "moving a workload off AWS")
    assert any("assumes they use ['AWS']" in p for p in followup_problems(aws, 2, "Haymant", facts))
    azure = SARAS_FU1.replace("a quick review of", "moving off Azure and")
    assert any("Azure" in p for p in followup_problems(azure, 2, "Haymant", facts))
    # Named in the facts, so it may be mentioned.
    assert assumed_tools("moving off AWS", "- Research notes: runs its platform on AWS") == []
    assert assumed_tools("Google Cloud and Azure", "") == ["Azure", "Google Cloud"]
    assert any("weekday" in p for p in first_email_problems(
        REFERENCE_EMAIL_1.replace("this week", "on Monday"), "Haymant", saras_services()))
    # A personal follow-up must greet the person by name, not a merge tag.
    assert any("Hi Haymant," in p for p in followup_problems(REFERENCE_FU1, 2, "Haymant", facts))


def test_paragraphs_are_one_line_each():
    from harvey.signature import apply_signature, unwrap_paragraphs
    wrapped = "Hi Ravi,\n\nI came across Acme's work on\nlogistics software for\ndistributors.\n\nOpen to a call?"
    assert unwrap_paragraphs(wrapped) == ("Hi Ravi,\n\nI came across Acme's work on logistics "
                                          "software for distributors.\n\nOpen to a call?")
    persona = NS(signature=SIG, name="Haymant Yerneni")
    signed = apply_signature(wrapped, persona)
    assert "work on logistics software for distributors." in signed
    assert signed.endswith("Warm regards,\n" + SIG)                  # signature keeps its lines
    assert apply_signature(signed, persona) == signed                 # still idempotent


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
                return {"subject": "Microsoft licensing & hosting for Saras India",
                        "body": REFERENCE_EMAIL_1,
                        "followup_1": SARAS_FU1.replace("this week or next", "on Thursday or Friday"),
                        "followup_2": SARAS_FU2}
            return {"subject": "Microsoft licensing & hosting for Saras India",
                    "body": REFERENCE_EMAIL_1, "followup_1": SARAS_FU1, "followup_2": SARAS_FU2}

    cfg = make_config()
    brain = Brain()
    writer = Writer(brain=brain, state=state, config=cfg)
    prospect = Prospect(first_name="Haymant", last_name="Y", title="Founder",
                        company="Saras India", industry=SARAS["industry"],
                        personalization_notes=SARAS["detail"],
                        email="haymant@sarasindia.com")
    draft = await writer._write_personal_email(prospect)
    assert draft["body"] == REFERENCE_EMAIL_1
    assert draft["followup_1"] == SARAS_FU1.replace("\n\nWarm regards,", "\n\nWarm regards,")
    assert len(brain.calls) == 2
    assert "broke these rules" in brain.calls[1] and "weekday" in brain.calls[1]
    assert "Microsoft licensing" in brain.calls[0] and "this week or next" in brain.calls[0]

    # Staged: email 1 and both personal follow-ups, signed, follow-ups as Re:.
    from harvey.models.campaign import Campaign
    pid = await state.add_prospect(prospect)
    cid = await state.add_campaign(Campaign(id="", name="t", channel="email", sequence=[],
                                            prospect_ids=[pid], status="draft"))
    prospect.id = pid
    brain.calls.clear()
    await writer._personalize_first_emails(cid, [prospect])
    staged = await state.get_sequence_rows(cid, pid)
    assert [r["step"] for r in staged] == [1, 2, 3]
    assert staged[1]["subject"] == "Re: Microsoft licensing & hosting for Saras India"
    assert all(r["body"].endswith("Warm regards,\n" + SIG) for r in staged)
    assert "Microsoft licensing or the servers" in staged[1]["body"]


@pytest.mark.asyncio
async def test_followup_is_sent_signed_in_both_parts(state):
    """FU1 leaves with the signature in the plain part and, with a logo
    configured, in the HTML part too."""
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.persona.logo_url = "https://cdn.example.com/logo.png"
    await sender._run_native()
    rows_ = await rows(state, campaign, pid)
    await state.update_outbox_item(rows_[1]["id"], status="approved")
    await sender._run_native()
    rows_ = await rows(state, campaign, pid)
    await state.update_outbox_item(rows_[2]["id"], status="approved")
    await make_due(state, rows_[2]["id"])
    await sender._run_native()
    fu1 = provider.sent[-1]
    assert fu1["subject"].startswith("Re: ")
    assert fu1["body"].endswith("Warm regards,\n" + SIG)
    assert "+91 90000 00000 | cloudbox99.com" in fu1["html"]
    assert fu1["html"].index("Haymant Yerneni") < fu1["html"].index("<img")
