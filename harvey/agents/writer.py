"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Steps 2-3 are campaign-level follow-ups sent as replies in email 1's
thread (the sender threads them).

House style (CloudBox99): warm Indian business tone; email 1 introduces the
sender and carries the full services list, ordered so the service that fits
the prospect's segment comes first; follow-up 1 (day 4) suggests one easy
starting point and a 15-minute call; follow-up 2 (day 10) is a polite last
note. Segment detection and the services list are deterministic code, and
every draft is checked against the style rules before it is queued — a
draft that breaks them is regenerated with the problems spelled out.
Every queued email still passes the deterministic pre-send gate.
"""

import logging
import re
from datetime import datetime, timezone

from harvey.brain import Brain
from harvey.config import HarveyConfig
from harvey.gate import BANNED_PHRASES, URL_RE
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.signature import apply_signature, placeholders, strip_signature
from harvey.state import StateManager

logger = logging.getLogger("harvey.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50

# Attempts per draft: the first try plus retries that quote the problems.
MAX_ATTEMPTS = 3

# Follow-up cadence: day 4 and day 10 after email 1 actually goes out.
SEQUENCE_DELAYS = (0, 4, 6)

FIRST_EMAIL_WORDS = (120, 170)
FOLLOWUP_MAX_WORDS = {2: 80, 3: 50}     # "under N words"

# ── Services and segments ─────────────────────────────────────────────

SERVICES = {
    "vps": "VPS hosting (from ₹999/month) and dedicated servers",
    "migration": "Cloud migration from AWS, Azure or on-premise, handled end to end by our engineers",
    "storage": "Cloud storage and backup",
    "eyecloud": "EyeCloud: encrypted cloud storage for CCTV footage",
    "managed": "Managed IT, cybersecurity and 24x7 SOC monitoring",
    "microsoft": "Microsoft licensing",
}

# Checked in order: the first segment whose keyword appears wins. Specific
# segments come before broad ones ("retail chain" before "it").
SEGMENTS = (
    {
        "key": "finance", "label": "Banks / NBFCs / financial services",
        "keywords": ("bank", "nbfc", "financ", "fintech", "insurance", "lending",
                     "microfinance", "wealth", "broking", "capital"),
        "lead": ("managed", "storage"),
        "angle": "SOC monitoring and cybersecurity, plus storage that keeps data in India",
        "start": "a security review with SOC monitoring, or moving sensitive data to storage that stays in India",
    },
    {
        "key": "health", "label": "Clinics / hospitals / diagnostic centres",
        "keywords": ("clinic", "hospital", "diagnost", "health", "medical", "dental",
                     "pathology", "patholab", "imaging", "nursing", "pharma"),
        "lead": ("storage", "eyecloud"),
        "angle": "secure backup for patient data, plus EyeCloud for CCTV footage",
        "start": "setting up proper encrypted backups for patient data, or moving CCTV footage to the cloud",
    },
    {
        "key": "ecommerce", "label": "D2C brands / e-commerce",
        "keywords": ("d2c", "e-commerce", "ecommerce", "online store", "direct to consumer",
                     "direct-to-consumer", "shopify", "marketplace seller"),
        "lead": ("vps",),
        "angle": "VPS hosting for their store or website",
        "start": "moving the store or website onto a faster VPS",
    },
    {
        "key": "mfg_retail", "label": "Manufacturing / retail chains / multi-site premises",
        "keywords": ("manufactur", "factory", "industrial", "industries", "retail", "cowork",
                     "supermarket", "showroom", "warehouse", "textile", "pharma manufact"),
        "lead": ("eyecloud", "managed"),
        "angle": "EyeCloud for multi-site CCTV, plus backup and managed IT",
        "start": "moving CCTV footage from local recorders at each site to the cloud",
    },
    {
        "key": "it", "label": "IT services / software / SaaS",
        "keywords": ("software", "saas", "it services", "it solutions", "information technology",
                     "technolog", "tech ", "developer", "app development", "digital",
                     "startup", "cloud", "it company", "infotech", "systems"),
        "lead": ("vps", "migration"),
        "angle": "VPS and dedicated servers, often moving off AWS/Azure to cut cost",
        "start": "moving a few servers off AWS or Azure",
    },
)
DEFAULT_SEGMENT = {
    "key": "general", "label": "General business",
    "keywords": (), "lead": (),
    "angle": "reliable hosting, backup and IT support with local help",
    "start": "setting up proper backups",
}


def segment_for(*texts: str) -> dict:
    """The prospect's segment. Texts are in priority order (industry first,
    then description): a software company whose description mentions its
    manufacturing customers is still a software company."""
    for text in texts:
        blob = (text or "").lower() + " "
        for seg in SEGMENTS:
            if any(k in blob for k in seg["keywords"]):
                return seg
    return DEFAULT_SEGMENT


def services_for(segment: dict) -> list[str]:
    """All services, the segment's most relevant ones first."""
    lead = [k for k in segment.get("lead", ()) if k in SERVICES]
    rest = [k for k in SERVICES if k not in lead]
    return [SERVICES[k] for k in lead + rest]


# ── Style checks (deterministic; the model's output is never trusted) ──

STYLE_BANNED = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bfree\b", r"guarantee", r"discount", r"limited[- ]time", r"act now",
    r"%\s*off", r"risk[- ]free", r"no cost", r"special offer", r"exclusive deal",
    r"click here", r"buy now", r"\burgent\b", r"don'?t miss", r"last chance",
    r"\bwinner\b", r"congratulations", r"100%", r"\$\$\$",
))
LIST_LINE_RE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)


def _common_problems(core: str) -> list[str]:
    problems = []
    lowered = core.lower()
    hits = [p.pattern for p in STYLE_BANNED if p.search(core)]
    hits += [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        problems.append(f"uses banned spam/AI words: {hits[:4]}")
    if "—" in core or "–" in core:
        problems.append("contains an em/en dash; use commas or full stops")
    if "!" in core:
        problems.append("contains an exclamation mark")
    if "**" in core or "__" in core or re.search(r"<\s*\w+[^>]*>", core):
        problems.append("contains formatting (bold/HTML); plain text only")
    left = placeholders(core)
    if left:
        problems.append(f"contains unfilled placeholders {left[:3]}")
    if core.count("?") != 1:
        problems.append(f"must ask exactly one question (found {core.count('?')} '?')")
    return problems


def first_email_problems(body: str, first_name: str, services: list[str],
                         good_morning: bool) -> list[str]:
    """Everything wrong with an email 1 draft ([] = it may be queued)."""
    core = strip_signature(body)
    problems = _common_problems(core)
    if first_name and not core.startswith(f"Hi {first_name},"):
        problems.append(f'must open with "Hi {first_name}," on its own line')
    opening = core[:120]
    if good_morning and "Good morning." not in opening:
        problems.append('first paragraph must begin with "Good morning."')
    if not good_morning and "good morning" in core.lower():
        problems.append('do not write "Good morning" (sends are not limited to the morning)')
    if "Hyderabad" not in core:
        problems.append("must say CloudBox99 is a cloud infrastructure company based in Hyderabad")
    lines = [ln.strip() for ln in core.splitlines()]
    positions = []
    for svc in services:
        target = f"- {svc}"
        positions.append(lines.index(target) if target in lines else -1)
    missing = [svc for svc, pos in zip(services, positions) if pos < 0]
    if missing:
        problems.append(f"services list must contain these lines exactly: {missing}")
    elif positions != sorted(positions):
        problems.append("services list must be in exactly the given order")
    extra = [ln for ln in lines if LIST_LINE_RE.match(ln) and ln[2:] not in services]
    if extra:
        problems.append(f"only the given services may appear as list lines (extra: {extra[:2]})")
    if URL_RE.search(core) or re.search(r"\bwww\.", core):
        problems.append("no links in email 1")
    words = len(core.split())
    lo, hi = FIRST_EMAIL_WORDS
    if not lo <= words <= hi:
        problems.append(f"must be {lo}-{hi} words before the sign-off (it is {words})")
    return problems


def followup_problems(body: str, step: int) -> list[str]:
    """Everything wrong with a follow-up template ([] = acceptable)."""
    core = strip_signature(body)
    problems = _common_problems(core)
    if not core.startswith("Hi {{first_name}},"):
        problems.append('must open with "Hi {{first_name}}," on its own line')
    if LIST_LINE_RE.search(core):
        problems.append("follow-ups must not contain a list (never repeat the services list)")
    if len(URL_RE.findall(core)) > 1:
        problems.append("at most one link in a follow-up")
    limit = FOLLOWUP_MAX_WORDS.get(step)
    words = len(core.split())
    if limit and words >= limit:
        problems.append(f"must be under {limit} words before the sign-off (it is {words})")
    return problems


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.skills = ""

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    @property
    def good_morning(self) -> bool:
        """'Good morning.' only when sends are confined to the morning."""
        email_cfg = self.config.channels.email
        start = getattr(email_cfg, "send_window_start", "") or ""
        end = getattr(email_cfg, "send_window_end", "") or ""
        return bool(start and end and end <= "12:00")

    @property
    def sender_first_name(self) -> str:
        return (self.config.persona.name.split() or [""])[0]

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        deliverable = {"verified", "risky"}
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in deliverable
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue
            await self.write_campaign(batch_name, prospects)

    async def write_campaign(self, batch_name: str, prospects: list) -> str | None:
        """Write one campaign (template sequence + per-prospect email 1)."""
        logger.info(
            f"Writer: Creating campaign '{batch_name}' for {len(prospects)} prospects."
        )
        sequence = await self._write_sequence(prospects)
        if not sequence:
            logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
            return None

        campaign = Campaign(
            id="",
            name=batch_name,
            channel="email",
            sequence=sequence,
            prospect_ids=[p.id for p in prospects],
            status="draft",
        )
        campaign_id = await self.state.add_campaign(campaign)

        # Mark prospects so they aren't picked up again
        for p in prospects:
            await self.state.update_prospect_status(p.id, "queued")

        # Native providers: replace the shared template for email 1 with
        # a per-prospect draft grounded in that company's actual facts.
        if self.is_native:
            await self._personalize_first_emails(campaign_id, prospects)

        await self.state.log_action(
            action_type="write_campaign",
            agent="writer",
            details={
                "campaign_id": campaign_id,
                "campaign_name": batch_name,
                "prospect_count": len(prospects),
                "steps": len(sequence),
            },
        )
        logger.info(
            f"Writer: Campaign '{batch_name}' created with "
            f"{len(sequence)} emails for {len(prospects)} prospects."
        )
        return campaign_id

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        drafted = 0
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if not draft:
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=apply_signature(draft["body"], self.config.persona),
                send_at=now,
                status=status,
                provider=provider,
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    def _base_prompt(self) -> str:
        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or (
            f"You are {self.config.persona.name}, {self.config.persona.role} at "
            f"{self.config.persona.company}.\nYour tone is: {self.config.persona.tone}\n\n"
            f"Product: {self.config.product.name}\n"
            f"Description: {self.config.product.description}\n"
            f"Key benefits: {', '.join(self.config.product.key_benefits)}\n"
            f"Pricing: {self.config.product.pricing}"
        )
        if self.skills:
            prompt += "\n\n" + self.skills
        prompt += (
            "\n\n---\nPRECEDENCE: the HOUSE STYLE and SPAM FILTER RULES at the top, and "
            "the task below, override anything in the foundational knowledge (for "
            "example advice to keep cold emails under 75 words, to avoid lists, or "
            "to offer 'free' trials or audits). Do not name competitors."
        )
        return prompt

    def _intro_instructions(self) -> str:
        sender = self.sender_first_name
        company = self.config.persona.company
        opener = '"Good morning." followed by ' if self.good_morning else ""
        return (
            f"- First paragraph: {opener}introduce yourself as {sender} from the "
            f"{company} sales team, {company} being a cloud infrastructure company "
            f"based in Hyderabad."
        )

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        facts = [
            f"- Name: {prospect.full_name()}",
            f"- Title: {prospect.title}",
            f"- Company: {prospect.company}",
        ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        description = ""
        if company:
            if company.location:
                facts.append(f"- Location: {company.location}")
            if company.description:
                description = company.description
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")

        segment = segment_for(prospect.industry,
                              getattr(company, "industry", "") if company else "",
                              description, prospect.company)
        services = services_for(segment)
        first = prospect.first_name or ""

        task = f"""

TASK: write EMAIL 1 (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect must
come from these facts. If a fact isn't listed, you don't know it:
{chr(10).join(facts)}

SEGMENT: {segment['label']} (most relevant to them: {segment['angle']}).

Write it in this order:
- "Hi {first or '{{first_name}}'}," on its own line.
{self._intro_instructions()}
  Then ONE sentence about {prospect.company} taken from the FACTS above (what
  they do, their market or city). If the facts hold nothing specific, say
  plainly how you came across them and nothing more. Never invent details.
- A short paragraph: you help growing businesses across India keep their
  servers, data and IT running smoothly, with local support and billing in
  rupees, leading into the list.
- This list, one item per line, each line starting with "- ", copied EXACTLY
  and in EXACTLY this order:
{chr(10).join('- ' + s for s in services)}
- Offer a short 15-minute call to understand their current setup and suggest
  what fits.
- Exactly ONE question in the whole email (the only "?"), e.g. whether some
  time this week would work.
- The last line is "Warm regards," with nothing after it.

Length: {FIRST_EMAIL_WORDS[0]}-{FIRST_EMAIL_WORDS[1]} words from "Hi" to the question. Count them.
Subject: 4-9 words naming {prospect.company} and the area that fits them, in the
style of "Cloud hosting & IT support for Acme Tech". No "Re:", no "quick question".
Write the actual text (no merge variables). Do not copy example sentences verbatim.

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        prompt = self._base_prompt() + task
        problems: list[str] = []
        draft = None
        for attempt in range(MAX_ATTEMPTS):
            ask = prompt
            if problems and draft:
                ask += (
                    "\n\nYour previous draft broke these rules:\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + f"\n\nPrevious draft:\n{draft['body']}\n\nRewrite it so every rule holds."
                )
            result = await self.brain.think_json(
                ask, session_id="harvey-writer",
                agent="writer", task="personal_email",
            )
            if not isinstance(result, dict):
                continue
            subject = str(result.get("subject") or "").strip()
            body = str(result.get("body") or "").strip()
            if not subject or not body:
                continue
            draft = {"subject": subject[:120], "body": body[:3000]}
            problems = first_email_problems(body, first, services, self.good_morning)
            problems += [f"subject: {p}" for p in _subject_problems(subject)]
            if not problems:
                return draft
            logger.info(f"Writer: email 1 draft for {prospect.email} attempt "
                        f"{attempt + 1} needs fixing: {problems}")
        logger.warning(f"Writer: no acceptable email 1 for {prospect.email} after "
                       f"{MAX_ATTEMPTS} attempts: {problems}")
        return None

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """The campaign's 3-email template: email 1 (fallback) + 2 follow-ups."""
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Industry: {p.industry}" if p.industry else "")
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]
        )
        segment = segment_for(*(f"{p.industry} {p.company}" for p in prospects[:5]))
        services = services_for(segment)

        task = f"""

TASK: write the 3-email sequence template for this campaign. Prospects:
{prospect_summary}

SEGMENT: {segment['label']} (most relevant to them: {segment['angle']}).
Use the merge variables {{{{first_name}}}} and {{{{company}}}}.

EMAIL 1 (day 0), a fallback used only when a personal draft is unavailable:
- "Hi {{{{first_name}}}}," on its own line.
{self._intro_instructions()}
  Then one sentence saying you came across {{{{company}}}} and thought you could
  be useful to their team (no invented details).
- A short paragraph: you help growing businesses across India keep servers,
  data and IT running smoothly, with local support and billing in rupees.
- This list, copied EXACTLY, one per line, each starting with "- ", in EXACTLY this order:
{chr(10).join('- ' + s for s in services)}
- Offer a 15-minute call; exactly ONE question; last line "Warm regards,".
- {FIRST_EMAIL_WORDS[0]}-{FIRST_EMAIL_WORDS[1]} words before the sign-off.

FOLLOW-UP 1 (day 4, sent as a reply in the same thread):
- "Hi {{{{first_name}}}}," then a short warm line (e.g. hoping their week is going
  well) and ask whether they had a chance to see your earlier email.
- Suggest ONE easy starting point for this segment: {segment['start']},
  building from there, and offer to suggest a starting point for {{{{company}}}}.
- Offer a 15-minute call; exactly ONE question. Do NOT repeat the services list.
- Under {FOLLOWUP_MAX_WORDS[2]} words before the sign-off. Last line "Warm regards,".

FOLLOW-UP 2 (day 10, same thread):
- "Hi {{{{first_name}}}}," then say you don't want to crowd their inbox, so this
  is your last note for now; invite them to reply any time if this comes up at
  {{{{company}}}} later.
- Ask once whether you may check back in a few months (the only question).
- Under {FOLLOWUP_MAX_WORDS[3]} words before the sign-off. Last line "Warm regards,".

Subjects: email 1 in the style of "Cloud hosting & IT support for {{{{company}}}}".
Follow-up subjects are replaced by "Re: <email 1 subject>" when sent; write
"Re: " plus email 1's subject for them.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 4}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 6}}
]"""

        prompt = self._base_prompt() + task
        steps: list[EmailStep] = []
        problems: list[str] = []
        for attempt in range(MAX_ATTEMPTS):
            ask = prompt
            if problems and steps:
                ask += (
                    "\n\nYour previous sequence broke these rules:\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + "\n\nPrevious sequence:\n"
                    + "\n\n".join(f"[email {s.step}]\n{s.body}" for s in steps)
                    + "\n\nRewrite the whole sequence so every rule holds."
                )
            result = await self.brain.think_json(
                ask, session_id="harvey-writer",
                agent="writer", task="write_sequence",
            )
            steps = self._parse_sequence(result)
            if not steps:
                continue
            problems = self._sequence_problems(steps, services)
            if not problems:
                return steps
            logger.info(f"Writer: sequence attempt {attempt + 1} needs fixing: {problems}")
        if steps and problems:
            # The follow-ups are what every prospect receives; email 1 is only
            # a fallback behind the per-prospect drafts. Accept a sequence
            # whose follow-ups are clean, never one whose follow-ups aren't.
            if len(steps) == 3 and not any(p.startswith("follow-up") for p in problems):
                logger.warning(f"Writer: fallback email 1 template still has issues "
                               f"(personal drafts replace it): {problems}")
                return steps
            logger.warning(f"Writer: sequence still breaks rules after {MAX_ATTEMPTS} "
                           f"attempts: {problems}")
            return []
        return steps

    def _sequence_problems(self, steps: list[EmailStep], services: list[str]) -> list[str]:
        if len(steps) != 3:
            return [f"must be exactly 3 emails (got {len(steps)})"]
        problems = [f"email 1: {p}" for p in first_email_problems(
            steps[0].body, "{{first_name}}", services, self.good_morning)]
        for s in steps[1:]:
            problems += [f"follow-up {s.step - 1}: {p}" for p in followup_problems(s.body, s.step)]
        return problems

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence. The cadence is fixed in code
        (day 0 / day 4 / day 10), whatever delays the model wrote.
        """
        if isinstance(result, dict):
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            n = len(steps)
            delay = SEQUENCE_DELAYS[n] if n < len(SEQUENCE_DELAYS) else 4
            try:
                steps.append(EmailStep(step=n + 1, subject=subject, body=body, delay_days=delay))
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry."""
        batches: dict[str, list] = {}
        for prospect in prospects:
            industry = prospect.industry or "general"
            batches.setdefault(f"{industry}-outreach", []).append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}


def _subject_problems(subject: str) -> list[str]:
    problems = []
    if subject.lower().startswith(("re:", "fwd:", "fw:")):
        problems.append('must not start with "Re:" or "Fwd:"')
    if any(p.search(subject) for p in STYLE_BANNED):
        problems.append("uses a banned spam word")
    if "quick question" in subject.lower():
        problems.append('must not say "quick question"')
    if not 3 <= len(subject.split()) <= 10:
        problems.append("should be 4-9 words")
    return problems
