"""Trusted emails: addresses from the owner's uploaded file are sendable
without a verifier, in small daily batches during warm-up, with their own
bounce-rate pause. Harvey's own unverified leads still are not emailed."""

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio

from harvey.gate import pre_send_check
from harvey.integrations.mail_provider import InboundMessage
from harvey.models.prospect import TRUSTED, UPLOAD_SOURCE, Prospect, email_sendable, is_trusted
from harvey.state import StateManager
from tests.test_outbox_native import FakeProvider, make_handler, make_sender


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def P(status, source=UPLOAD_SOURCE, email="r@acme.in"):
    return Prospect(first_name="Ravi", last_name="K", title="", company="Acme", email=email,
                    email_status=status, source=source)


def test_trusted_only_counts_for_the_uploaded_list():
    assert email_sendable(P(TRUSTED)) and is_trusted(P(TRUSTED))
    assert not email_sendable(P(TRUSTED, source="dataforseo"))     # Harvey's own leads: no
    assert not email_sendable(P("guess")) and not email_sendable(P(""))
    assert not email_sendable(P("guess", source="dataforseo"))
    assert email_sendable(P("verified", source="dataforseo"))
    assert not email_sendable(P("risky")) and email_sendable(P("risky"), allow_risky=True)


def test_gate_lets_trusted_through_but_not_unverified():
    body = "Hi Ravi,\n\nShort note.\n\nWarm regards,\nHaymant"
    assert pre_send_check("r@acme.in", "Hosting for Acme", body, prospect=P(TRUSTED)).ok
    blocked = pre_send_check("r@acme.in", "Hosting for Acme", body,
                             prospect=P(TRUSTED, source="dataforseo"))
    assert not blocked.ok and "not deliverable" in blocked.reasons[0]
    assert not pre_send_check("r@acme.in", "Hosting for Acme", body, prospect=P("guess")).ok
    assert not pre_send_check("r@acme", "Hosting for Acme", body, prospect=P(TRUSTED, email="r@acme")).ok


@pytest.mark.asyncio
async def test_writer_drafts_trusted_uploads_but_not_unverified_leads(state):
    from harvey.agents.writer import Writer
    picked = []
    w = Writer.__new__(Writer)
    w.state = state
    w.brain = NS(load_skills_for_agent=lambda *_: "")

    async def capture(name, prospects):
        picked.extend(p.email for p in prospects)
    w.write_campaign = capture
    w._group_prospects = lambda ps: {"all": ps}
    for p in (P(TRUSTED, email="t@list.in"), P("guess", source="dataforseo", email="g@lead.in"),
              P(TRUSTED, source="dataforseo", email="x@lead.in"),
              P("verified", source="dataforseo", email="v@lead.in")):
        await state.add_prospect(p)
    await w.run()
    assert sorted(picked) == ["t@list.in", "v@lead.in"]


async def approved_trusted(state, n):
    for i in range(n):
        pid = await state.add_prospect(P(TRUSTED, email=f"p{i}@list{i}.in"))
        await state.update_prospect_status(pid, "contacted")
        await state.add_outbox_item(prospect_id=pid, to_email=f"p{i}@list{i}.in",
                                    subject="Hosting for Acme",
                                    body="Hi Ravi,\n\nShort note.\n\nWarm regards,\nHarvey",
                                    send_at=_now(), status="approved", campaign_id=f"c{i}", step=1)


@pytest.mark.asyncio
async def test_trusted_emails_go_in_small_daily_batches_during_warm_up(state):
    await approved_trusted(state, 5)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.imports = NS(priority=True, trusted_daily_max=3, trusted_max_bounce_rate=0.05)
    await sender._drain_due()
    assert len(provider.sent) == 3
    await sender._drain_due()                                   # same day: no more
    assert len(provider.sent) == 3
    assert len(await state.get_outbox(status="approved")) == 2  # waiting, not dropped
    from harvey.agents.sender import trusted_stats
    assert await trusted_stats(state) == (3, 0)


@pytest.mark.asyncio
async def test_no_trusted_batch_limit_once_warmed_up(state):
    from harvey.agents.sender import trusted_remaining_today
    cfg = NS(imports=NS(trusted_daily_max=3))
    assert await trusted_remaining_today(state, cfg, NS(stage=3, stages=4)) == float("inf")
    assert await trusted_remaining_today(state, cfg, NS(stage=1, stages=4)) == 3


async def bounce(state, provider, email, message_id):
    provider.inbound = [InboundMessage(
        provider_id=f"b-{message_id}", from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)", body="couldn't be delivered",
        in_reply_to=message_id, is_bounce=True)]


@pytest.mark.asyncio
async def test_trusted_bounce_marks_invalid_cancels_follow_ups_and_pauses_past_5pct(state):
    await approved_trusted(state, 3)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._drain_due()
    sent = await state.get_outbox(status="sent")
    first = sent[0]
    await state.add_outbox_item(prospect_id=first["prospect_id"], to_email=first["to_email"],
                                subject="Re: Hosting for Acme", body="Hi Ravi,\n\nFollow-up.",
                                send_at=_now(), status="approved", campaign_id=first["campaign_id"],
                                step=2)
    await bounce(state, provider, first["to_email"], first["message_id"])
    await make_handler(state, provider)._run_native()

    p = await state.get_prospect(first["prospect_id"])
    assert p.email_status == "invalid"
    assert [r["status"] for r in await state.get_sequence_rows(first["campaign_id"], p.id)
            if r["step"] == 2] == ["cancelled"]
    paused = await state.get_setting("sending_paused")
    assert "uploaded-list bounce rate 1/3" in paused        # 33% > 5%: paused


@pytest.mark.asyncio
async def test_a_verified_bounce_does_not_count_as_a_trusted_bounce(state):
    from harvey.agents.sender import trusted_stats
    pid = await state.add_prospect(P("verified", source="dataforseo", email="v@lead.in"))
    oid = await state.add_outbox_item(prospect_id=pid, to_email="v@lead.in", subject="s",
                                      body="b", send_at=_now(), status="approved",
                                      campaign_id="c", step=1)
    await state.update_outbox_item(oid, status="sent", message_id="<v@x>", sent_at=_now())
    provider = FakeProvider()
    await bounce(state, provider, "v@lead.in", "<v@x>")
    await make_handler(state, provider)._run_native()
    assert await trusted_stats(state) == (0, 0)
    assert await state.get_setting("sending_paused") == ""   # 1/1 but under the 10-send minimum


def test_tracker_email_status_labels():
    from harvey.tracker import email_status_label
    assert [email_status_label(s) for s in ("trusted", "verified", "invalid", "guess", "",
                                            "risky", None)] == \
        ["trusted", "verified", "invalid", "unverified", "unverified", "unverified", "unverified"]


@pytest.mark.asyncio
async def test_import_marks_unconfirmed_addresses_trusted(monkeypatch):
    from harvey import importer

    async def unconfirmed(email, env, smtp_ok):
        return "unverified", "couldn't confirm (Google mail)"
    monkeypatch.setattr(importer, "verify_given_email", unconfirmed)
    base = {f: "" for f in importer.FIELDS}
    base.update(company="Acme", name="Ravi", email="ravi@gmail.com", row=2)

    class Tools:
        async def search_domain(self, company):
            return "", ""
    r = await importer.plan_row(base, Tools(), None, True, set(), set(), set())
    assert r.email_status == TRUSTED and r.action == "import"
    assert importer.summarize([r])["email"] == {"verified": 0, "trusted": 1, "invalid": 0}
