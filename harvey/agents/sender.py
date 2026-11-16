"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone

from harvey.brain import Brain
from harvey.config import HarveyConfig, EnvConfig
from harvey.gate import html_part_problems, pre_send_check
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from harvey.signature import apply_signature, html_alternative, placeholders
from harvey.state import StateManager

logger = logging.getLogger("harvey.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
SEND_JITTER_SECONDS = (4, 15)

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}

# Email 1 outbox states after which it will never be sent.
DEAD_STATUSES = {"failed", "rejected", "cancelled"}


def re_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": step.body.strip(),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `harvey sending resume` once the cause is fixed."
            )
            return

        for campaign in await self.state.get_campaigns_by_status("draft"):
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        await self._drain_due()

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            "first_name": prospect.first_name,
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        skipped = 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seen_emails: set[str] = set()

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            seen_emails.add(email)

            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=apply_signature(
                        self._render(step.body, prospect), self.config.persona
                    ),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                )
                if item_id:
                    staged += 1
            await self.state.update_prospect_status(prospect.id, "queued")

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    def _cold_hold_reason(self, now: datetime | None = None) -> str:
        """Why cold sequence emails must wait right now ('' = they may go)."""
        if self._is_weekend_hold(now):
            return "weekend"
        email_cfg = self.config.channels.email
        start = getattr(email_cfg, "send_window_start", "") or ""
        end = getattr(email_cfg, "send_window_end", "") or ""
        if not (start and end):
            return ""
        import pytz
        from datetime import time as dtime
        tz = pytz.timezone(self.config.usage.quiet_hours.timezone)
        local = (now or datetime.now(timezone.utc)).astimezone(tz).time()
        if dtime.fromisoformat(start) <= local < dtime.fromisoformat(end):
            return ""
        return f"outside the {start}-{end} send window"

    def _window_exempt(self, item: dict) -> bool:
        """The owner's own test addresses ignore the weekday/window hold."""
        exempt = getattr(self.config.channels.email, "window_exempt", []) or []
        return (item.get("to_email") or "").strip().lower() in {e.lower() for e in exempt}

    def _is_weekend_hold(self, now: datetime | None = None) -> bool:
        """True on Sat/Sun (local to the quiet-hours timezone) when
        ``channels.email.weekdays_only`` is set."""
        if not getattr(self.config.channels.email, "weekdays_only", False):
            return False
        import pytz
        tz = pytz.timezone(self.config.usage.quiet_hours.timezone)
        local = (now or datetime.now(timezone.utc)).astimezone(tz)
        return local.weekday() >= 5

    async def _prepare_followup(self, item: dict, prospect) -> dict | None:
        """Ready a follow-up to send, or return None (wait / cancelled)."""
        rows = await self.state.get_sequence_rows(item["campaign_id"], item["prospect_id"])
        first = next((r for r in rows if int(r["step"]) == 1), None)
        reason = ""
        if (prospect.email_status or "") == "invalid":
            reason = "email 1 bounced"
        elif first is None or first["status"] in DEAD_STATUSES:
            reason = "email 1 was never sent"
        if reason:
            await self.state.update_outbox_item(item["id"], status="cancelled", error=reason)
            logger.info(f"Sender: cancelled step {item['step']} to {item['to_email']}: {reason}.")
            return None
        if first["status"] != "sent":
            return None  # Email 1 still awaiting approval/sending: wait.

        threaded = {
            "thread_ref": first.get("thread_ref") or "",
            "in_reply_to": self._rfc_id(first.get("message_id") or ""),
            "subject": re_subject(first["subject"]),
        }
        changed = {k: v for k, v in threaded.items() if (item.get(k) or "") != v}
        if changed:
            await self.state.update_outbox_item(item["id"], **changed)
            item = {**item, **changed}
        return item

    @staticmethod
    def _rfc_id(message_id: str) -> str:
        """Only a real RFC Message-ID ('<...>') may go in In-Reply-To."""
        message_id = (message_id or "").strip()
        return message_id if message_id.startswith("<") and message_id.endswith(">") else ""

    async def _thread_followups(self, first: dict, result):
        """Email 1 just went out: thread its follow-ups onto it and schedule
        them from the real send time (day 4, day 10), not from staging."""
        sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        campaign = await self.state.get_campaign(first["campaign_id"])
        delays = {s.step: max(0, s.delay_days) for s in (campaign.sequence if campaign else [])}
        staged_first = None
        try:
            staged_first = datetime.fromisoformat(first["send_at"])
        except (TypeError, ValueError):
            pass

        rows = await self.state.get_sequence_rows(first["campaign_id"], first["prospect_id"])
        for row in rows:
            step = int(row["step"])
            if step <= 1 or row["status"] not in ("pending_review", "approved"):
                continue
            if delays:
                offset = timedelta(days=sum(delays.get(n, 0) for n in range(2, step + 1)))
            elif staged_first is not None:
                try:
                    offset = datetime.fromisoformat(row["send_at"]) - staged_first
                except (TypeError, ValueError):
                    offset = timedelta(0)
            else:
                offset = timedelta(0)
            await self.state.update_outbox_item(
                row["id"],
                send_at=(sent_at + offset).isoformat(),
                thread_ref=result.thread_ref or "",
                in_reply_to=self._rfc_id(result.message_id),
                subject=re_subject(first["subject"]),
            )

    async def _drain_due(self):
        """Send due, approved outbox items through the provider."""
        if not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return

        max_daily = self.config.channels.email.max_daily_sends
        sent_today = await self.state.count_outbox_sent_today()
        budget = min(MAX_SENDS_PER_CYCLE, max_daily - sent_today)
        if budget <= 0:
            logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}).")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3
        )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        hold_cold = self._cold_hold_reason()
        sent = 0
        for item in due:
            if sent >= budget:
                break

            # Weekend / send-window hold: cold sequence emails wait (still
            # approved); replies to people who wrote back are not held.
            if hold_cold and item["kind"] == "sequence" and not self._window_exempt(item):
                continue

            prospect = await self.state.get_prospect(item["prospect_id"])
            if prospect is None:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error="prospect missing"
                )
                continue

            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if item["kind"] == "sequence" and prospect.status in STOP_STATUSES:
                await self.state.update_outbox_item(
                    item["id"], status="cancelled",
                    error=f"prospect status '{prospect.status}'",
                )
                continue

            # Follow-ups go only after this prospect's Email 1 went out, as a
            # reply in its thread; if Email 1 never will, they are cancelled.
            if item["kind"] == "sequence" and int(item["step"]) > 1:
                item = await self._prepare_followup(item, prospect)
                if item is None:
                    continue

            # Every email leaves signed; a body edited after staging is
            # re-signed (idempotent) so what's stored matches what's sent.
            signed = apply_signature(item["body"], self.config.persona)
            if signed != item["body"]:
                await self.state.update_outbox_item(item["id"], body=signed)
                item = {**item, "body": signed}
            left = placeholders(item["subject"] + " " + item["body"])
            if left:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"unfilled placeholder(s) {left[:3]}: fix persona.signature "
                          "or edit the email, then re-queue",
                )
                logger.warning(f"Sender: unfilled placeholder {left} in email to "
                               f"{item['to_email']}, not sent.")
                continue

            gate = pre_send_check(
                item["to_email"], item["subject"], item["body"],
                prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            )
            if not gate:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error="gate: " + "; ".join(gate.reasons)[:300],
                )
                logger.warning(
                    f"Sender: gate blocked email to {item['to_email']}: "
                    f"{gate.reasons}"
                )
                continue

            # The only HTML that ever leaves: the signature template (logo)
            # built in code from the gated plain text, then checked again.
            html_part = html_alternative(item["body"], self.config.persona)
            html_issues = html_part_problems(html_part, item["body"], self.config.persona)
            if html_issues:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error="gate: " + "; ".join(html_issues)[:300],
                )
                logger.warning(f"Sender: HTML part blocked for {item['to_email']}: {html_issues}")
                continue

            result = await self.provider.send_email(
                item["to_email"], item["subject"], item["body"],
                thread_ref=item.get("thread_ref", ""),
                in_reply_to=item.get("in_reply_to", ""),
                **({"html": html_part} if html_part else {}),
            )
            if not result.ok:
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=result.error[:300]
                )
                continue

            sent += 1
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso,
                message_id=result.message_id, thread_ref=result.thread_ref,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")
            if item["kind"] == "sequence" and int(item["step"]) == 1:
                await self._thread_followups(item, result)

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from harvey.models.conversation import Message
                        convo.thread.append(
                            Message(sender="harvey", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details={
                    "to": item["to_email"], "step": item["step"],
                    "kind": item["kind"], "provider": self.provider.name,
                },
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {self.provider.name}."
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{max_daily} today).")
