"""Per-mailbox send cap with a warm-up ramp: 10/20/30/50 per day by week from
the mailbox's first send, stepping up only while bounces stay under 3%,
a hard cap for the test mailbox, and every send (follow-ups, replies) counted."""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

import aiosqlite
import pytest

from harvey.config import WarmupConfig
from harvey.warmup import KEY_PREFIX, advance, warmup_status

from tests.test_followup_threading import FakeProvider, make_sender, seed, state  # noqa: F401

RAMP = [10, 20, 30, 50]
MAILBOX = "haymant@cb99mail.com"
TODAY = date(2026, 12, 1)


def cfg(ceiling=50, caps=None, mailbox=MAILBOX):
    return NS(persona=NS(email=mailbox),
              channels=NS(email=NS(max_daily_sends=ceiling,
                                   warmup=WarmupConfig(mailbox_caps=caps or {}))))


async def add_sent(state, n, days_ago=0, mailbox=MAILBOX, bounced=0, kind="sequence"):
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None)
    async with aiosqlite.connect(state.db_path) as db:
        for i in range(n):
            await db.execute(
                "INSERT INTO outbox (id, prospect_id, kind, to_email, status, sent_at, "
                "from_email, error) VALUES (?, 'p', ?, 'x@y.in', 'sent', ?, ?, ?)",
                (f"{days_ago}-{mailbox}-{kind}-{i}", kind, when.isoformat(), mailbox,
                 "bounced" if i < bounced else ""))
        await db.commit()


def test_ramp_steps_weekly_from_first_send():
    start = {"first_send": "2026-11-01", "stage": 0, "stage_since": "2026-11-01"}
    at = lambda d: advance(start, date.fromisoformat(d), RAMP, 7, 0.0, 0.03)["stage"]  # noqa: E731
    assert [at("2026-11-07"), at("2026-11-08"), at("2026-11-15"),
            at("2026-11-22"), at("2027-03-01")] == [0, 1, 2, 3, 3]


def test_high_bounce_rate_holds_then_recovers_with_a_full_week():
    s = {"first_send": "2026-11-01", "stage": 1, "stage_since": "2026-11-08"}
    held = advance(s, date(2026, 11, 16), RAMP, 7, 0.04, 0.03)
    assert held["stage"] == 1 and "held at 20/day" in held["held"] and "4.0%" in held["held"]
    assert advance(held, date(2026, 11, 20), RAMP, 7, 0.03, 0.03)["stage"] == 1   # 3% is not under
    ok = advance(held, date(2026, 11, 20), RAMP, 7, 0.01, 0.03)
    assert ok["stage"] == 2 and ok["held"] == "" and ok["stage_since"] == "2026-11-20"
    assert advance(ok, date(2026, 11, 26), RAMP, 7, 0.0, 0.03)["stage"] == 2      # full week first
    assert advance(ok, date(2026, 11, 27), RAMP, 7, 0.0, 0.03)["stage"] == 3


@pytest.mark.asyncio
async def test_new_mailbox_starts_at_10_and_test_mailbox_is_pinned_at_5(state):  # noqa: F811
    w = await warmup_status(state, cfg())
    assert (w.cap, w.stage, w.first_send) == (10, 0, "")
    assert "not started" in w.describe()
    w = await warmup_status(state, cfg(caps={MAILBOX: 5}))
    assert w.cap == 5 and "pinned at 5/day" in w.describe()
    # The pin belongs to the address: a different mailbox ramps normally.
    assert (await warmup_status(state, cfg(caps={"other@gmail.com": 5}))).cap == 10


@pytest.mark.asyncio
async def test_status_follows_first_send_date_and_counts_today(state):  # noqa: F811
    await add_sent(state, 3, days_ago=15)
    await add_sent(state, 2, days_ago=0, kind="reply")
    w = await warmup_status(state, cfg())
    assert (w.stage, w.cap, w.sent_today, w.remaining) == (2, 30, 2, 28)
    assert "week 3 of 4: 30/day, 50/day from" in w.describe()
    saved = json.loads(await state.get_setting(KEY_PREFIX + MAILBOX))
    assert saved["stage"] == 2
    # Another mailbox's history doesn't move this one's ramp.
    other = await warmup_status(state, cfg(mailbox="new@cloudbox99.com"))
    assert (other.stage, other.first_send) == (0, "")


@pytest.mark.asyncio
async def test_bounces_hold_the_ramp_and_show_why(state):  # noqa: F811
    await add_sent(state, 20, days_ago=8, bounced=1)          # 5% bounced
    w = await warmup_status(state, cfg())
    assert w.stage == 0 and w.cap == 10 and "held at 10/day" in w.held
    assert w.bounce_rate == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_ceiling_caps_the_ramp(state):  # noqa: F811
    await add_sent(state, 1, days_ago=40)
    assert (await warmup_status(state, cfg(ceiling=25))).cap == 25


@pytest.mark.asyncio
async def test_sender_stops_at_the_cap_counting_replies_and_records_the_mailbox(state):  # noqa: F811
    await add_sent(state, 9, days_ago=0, kind="reply")          # 9 of 10 used today
    pid, campaign = await seed(state)
    provider = FakeProvider()
    sender = make_sender(state, provider)
    await sender._run_native()                                  # stages + sends Email 1
    assert len(provider.sent) == 1
    await state.add_outbox_item(prospect_id=pid, to_email="ravi@acmetech.in", subject="Re: hi",
                                kind="reply", body="Hi Ravi,\n\nThanks.",
                                send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                status="approved")
    await sender._run_native()
    assert len(provider.sent) == 1                              # cap of 10 reached
    sent = [r for r in await state.get_outbox(status="sent") if r["prospect_id"] == pid]
    assert sent and all(r["from_email"] == MAILBOX for r in sent)
