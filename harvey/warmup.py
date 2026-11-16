"""Per-mailbox daily send cap with a warm-up ramp.

A new mailbox starts at the first ramp step (10/day) and moves up one step
every `step_days` (7) counted from its first send: 10 -> 20 -> 30 -> 50.
It only steps up while the mailbox's bounce rate is under
`max_bounce_rate` (3%); otherwise it holds at the current step, and the
hold is shown in `harvey status` and on the dashboard.

The cap counts every email the mailbox sends: Email 1, follow-ups and
replies. `mailbox_caps` pins a hard cap for specific addresses (the test
Gmail stays at 5/day), and `channels.email.max_daily_sends` is the ceiling
over everything.

State lives in the settings table under `warmup:<mailbox>`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import aiosqlite

KEY_PREFIX = "warmup:"


@dataclass
class WarmupStatus:
    mailbox: str
    cap: int                 # the cap enforced today
    sent_today: int
    stage: int               # 0-based ramp step
    stages: int
    ramp_cap: int            # what the ramp alone allows today
    first_send: str          # YYYY-MM-DD, "" before the first send
    next_step_on: str        # YYYY-MM-DD, "" when fully ramped
    next_cap: int
    bounce_rate: float
    sent_total: int
    bounced_total: int
    held: str                # why the ramp is holding, "" when it isn't
    pinned: int              # mailbox_caps entry, 0 when none
    ceiling: int             # max_daily_sends

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.sent_today)

    def describe(self) -> str:
        """One line for `harvey status`."""
        if not self.first_send:
            stage = f"warm-up not started (starts at {self.ramp_cap}/day on the first send)"
        elif self.stage >= self.stages - 1:
            stage = f"fully warmed up ({self.ramp_cap}/day)"
        else:
            stage = (f"warm-up week {self.stage + 1} of {self.stages}: {self.ramp_cap}/day, "
                     f"{self.next_cap}/day from {self.next_step_on}")
        parts = [f"{self.cap}/day ({self.sent_today} sent today)", stage]
        if self.pinned and self.pinned < self.ramp_cap:
            parts.append(f"pinned at {self.pinned}/day for this mailbox")
        return " | ".join(parts)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["remaining"] = self.remaining
        d["summary"] = self.describe()
        return d


def _warmup_cfg(config):
    from harvey.config import WarmupConfig
    return getattr(config.channels.email, "warmup", None) or WarmupConfig()


def ramp_for(config) -> list[int]:
    ceiling = config.channels.email.max_daily_sends
    warmup = _warmup_cfg(config)
    if not warmup.enabled or not warmup.ramp:
        return [ceiling]
    return [min(step, ceiling) for step in warmup.ramp]


def advance(saved: dict, today: date, ramp: list[int], step_days: int,
            bounce_rate: float, max_bounce_rate: float) -> dict:
    """Pure ramp logic: move up one step per `step_days` since the current
    step began, unless the bounce rate is at or above the limit, in which
    case hold (and say why). Returns the new saved state."""
    s = dict(saved)
    last = len(ramp) - 1
    s["stage"] = min(int(s.get("stage", 0)), last)
    since = date.fromisoformat(s["stage_since"])
    s["held"] = ""
    while s["stage"] < last and today >= since + timedelta(days=step_days):
        if bounce_rate >= max_bounce_rate:
            s["held"] = (f"held at {ramp[s['stage']]}/day: bounce rate "
                         f"{bounce_rate:.1%} is not under {max_bounce_rate:.0%}")
            break
        s["stage"] += 1
        since = since + timedelta(days=step_days)
        # After a hold, the new step gets its full week from today.
        if s.get("was_held"):
            since = today
        s["was_held"] = False
    if s["held"]:
        s["was_held"] = True
    s["stage_since"] = since.isoformat()
    return s


async def _mailbox_counts(db_path: str, mailbox: str) -> tuple[str, int, int, int]:
    """(first send date, sends today, total sent, total bounced) for one
    mailbox, from the outbox. Rows sent before the column existed have no
    from_email; they count toward today's cap (conservative) but not toward
    the ramp history."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT MIN(sent_at), COUNT(*), "
            "SUM(CASE WHEN error = 'bounced' THEN 1 ELSE 0 END) "
            "FROM outbox WHERE status = 'sent' AND lower(from_email) = ?", (mailbox,)
        ) as cur:
            first, total, bounced = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'sent' "
            "AND date(sent_at) = date('now') AND (lower(from_email) = ? OR from_email = '')",
            (mailbox,),
        ) as cur:
            (today,) = await cur.fetchone()
    return (first or "")[:10], today, total or 0, bounced or 0


async def warmup_status(state, config, today: date | None = None) -> WarmupStatus:
    """Today's cap for the sending mailbox (persona.email). Saves ramp
    progress, so calling it from status/dashboard is safe and idempotent."""
    email = config.channels.email
    warmup = _warmup_cfg(config)
    mailbox = (config.persona.email or "").strip().lower()
    today = today or datetime.now(timezone.utc).date()
    ramp = ramp_for(config)

    first, sent_today, total, bounced = await _mailbox_counts(state.db_path, mailbox)
    rate = bounced / total if total else 0.0

    key = KEY_PREFIX + mailbox
    raw = await state.get_setting(key)
    saved = json.loads(raw) if raw else {}
    if first and not saved.get("first_send"):
        saved = {"first_send": first, "stage": 0, "stage_since": first}
    if saved.get("first_send"):
        new = advance(saved, today, ramp, warmup.step_days, rate, warmup.max_bounce_rate)
        if new != saved:
            await state.set_setting(key, json.dumps(new))
        saved = new

    stage = int(saved.get("stage", 0))
    ramp_cap = ramp[stage]
    pinned = int(warmup.mailbox_caps.get(mailbox, 0))
    cap = min(ramp_cap, pinned) if pinned else ramp_cap
    fully = stage >= len(ramp) - 1
    next_on = ""
    if saved.get("first_send") and not fully:
        next_on = (date.fromisoformat(saved["stage_since"])
                   + timedelta(days=warmup.step_days)).isoformat()
    return WarmupStatus(
        mailbox=mailbox, cap=cap, sent_today=sent_today, stage=stage, stages=len(ramp),
        ramp_cap=ramp_cap, first_send=saved.get("first_send", ""), next_step_on=next_on,
        next_cap=ramp[min(stage + 1, len(ramp) - 1)], bounce_rate=rate, sent_total=total,
        bounced_total=bounced, held=saved.get("held", ""), pinned=pinned,
        ceiling=email.max_daily_sends,
    )
