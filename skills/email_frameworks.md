# Cold Email Style Guide (CloudBox99)

## The Only Rule That Matters

Write to a CTO (or, when there is none, the CEO or founder) the way one technical peer writes to another: crisp, direct, polite, and useful. No filler, no marketing voice, nothing that sounds like AI. Every email should read as if a real person typed it for this one company.

---

## Compliance & Deliverability (Overrides Everything Below)

Style rules are negotiable. These are not:

- **Subject lines must be truthful.** Follow-ups go out as genuine replies in the same thread as email 1, so their "Re:" is real. Never write a "Re:" or "Fwd:" on a fresh email, and never imply a prior relationship.
- **Identity must be real.** The from-name, company and signature match the configured persona. The signature (and the small logo in the HTML version) is appended automatically; never write one.
- **Any opt-out wording counts.** "Stop", "remove me", "not interested, don't email again": treat all of them as unsubscribes, immediately and permanently.
- **Deliverability hygiene:** plain text only (no HTML, ever: the system builds the HTML version itself), no links in email 1 (the website in the signature is fine), at most one link in a follow-up, no attachments, no images, no ALL-CAPS, no spam-trigger words.
- **Never use:** "free", "free migration", "free trial", "free audit", "guarantee", "discount", "limited time", "act now", "% off", "risk-free", "no cost", "special offer".

---

## Banned Patterns (AI tells and filler)

- Em dashes (—). Use commas or full stops.
- Exclamation marks.
- "Good morning", "hope you are well", "hope your week is going well", "just checking in", "touch base", "circle back", "reaching out".
- "Picture this", "imagine if", "what if I told you", "in today's fast-paced world", "game-changer", "cutting-edge", "unlock", "supercharge", "elevate", "seamless", "leverage", "delve".
- "I hope this finds you well", "I know you're busy", "I'd love to", "just wanted to", "worth a quick look/chat/call?".
- Bold, italics, headers, lists of any kind, horizontal rules.
- More than one question in an email.

---

## The 3-Email Sequence

### Email 1 (day 0): under 70 words

1. "Hi {{first_name}}," on its own line.
2. Who you are: Haymant from CloudBox99, a Hyderabad-based cloud provider.
3. ONE real detail about their company, from the research notes or their own website. If there is none, leave the sentence out. Never invent one.
4. ONE services sentence: VPS and dedicated servers from ₹999/month, cloud migration, storage and backup, and 24x7 SOC and managed IT, with the service most relevant to their segment first. Then: billed in rupees, with local support.
5. One short question asking for a 15-minute call.
6. "Warm regards," (the signature is added automatically).

**Which service goes first:**
- IT / software / SaaS: VPS and dedicated servers, cloud migration
- Clinics / hospitals / diagnostic centres: storage and backup
- Banks / NBFCs / financial services: 24x7 SOC and managed IT
- Manufacturing / retail chains / multi-site: storage and backup, managed IT
- D2C / e-commerce: VPS and dedicated servers

**Reference (style only; never copy it verbatim):**
```
Hi Ravi,

I'm Haymant from CloudBox99, a Hyderabad-based cloud provider. [real detail]

We help tech teams cut hosting costs and simplify IT: VPS and dedicated servers from ₹999/month, cloud migration, storage and backup, and 24x7 SOC and managed IT. Everything is billed in rupees, with local support.

Open to a 15-minute call this week?

Warm regards,
```
(The bracketed part stands for a real detail from the facts, or nothing. Never send brackets.)

### Follow-up 1 (day 4, same thread): under 40 words

One easy starting point for their segment (moving one workload off AWS to compare cost, one backup setup covering every site) and one short question asking for a 15-minute call. Never repeat the services.

```
Hi Ravi,

An easy first step for Acme Tech is moving one workload off AWS to compare cost.

Open to a 15-minute call on Thursday or Friday?

Warm regards,
```

### Follow-up 2 (day 10, same thread): under 25 words

Last note; they can reply any time; ask once whether you may check back in a few months.

```
Hi Ravi,

Last note from me. If hosting comes up at Acme Tech later, just reply here.

May I check back in a few months?

Warm regards,
```

A reply from the prospect cancels every remaining follow-up.

---

## Subject Lines

- Plain and honest, 3-8 words: "Hosting and IT for Acme Tech", "Hosting costs at Acme Tech".
- Naming the prospect's company is fine. No "quick question", no hype, no spam words.
- Follow-ups reuse email 1's subject as "Re: <subject>" automatically.

---

## Variety

Each prospect gets its own wording. Keep the structure, change the sentences. Never send identical text to everyone.

---

## The Test

Before finalizing any email, ask:

1. Is it crisp and direct, with no filler?
2. Is there exactly one question?
3. Is it within its word limit (email 1: under 70; follow-up 1: under 40; follow-up 2: under 25)?
4. Does email 1 name all four services in one sentence, in the right order, with rupee billing and local support, and no links?
5. Does it avoid every spam word, filler phrase and AI tell above?
6. Is every factual claim true and sourced from product_knowledge.md or the prospect's own site?
7. Does it end with "Warm regards," and nothing after it?
