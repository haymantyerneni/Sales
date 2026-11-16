"""CTO-first targeting with CEO/Founder fallback, and the signature logo
(multipart/alternative with a gate-checked HTML part)."""

from email import message_from_bytes
from email.message import EmailMessage
from types import SimpleNamespace as NS

import pytest

from harvey.gate import html_part_problems, pre_send_check
from harvey.models.prospect import TARGETING_PREFIX, Prospect
from harvey.signature import apply_signature, html_alternative

LOGO = "https://cdn.example.com/cloudbox99-logo.png"
SIG = "Haymant Yerneni\nSales Team, CloudBox99, Hyderabad\n+91 87127 05166 | cloudbox99.com"


def persona(logo=LOGO, width=150):
    return NS(name="Haymant Yerneni", company="CloudBox99", signature=SIG,
              logo_url=logo, logo_width=width, from_name="", email="h@x.com")


BODY = apply_signature(
    "Hi Ravi,\n\nI'm Haymant from CloudBox99, a Hyderabad-based cloud provider.\n\n"
    "Open to a 15-minute call this week?", persona())


# ── HTML part ──

def test_html_part_is_escaped_text_plus_one_logo():
    html = html_alternative(BODY, persona())
    assert html.count("<img") == 1 and f'src="{LOGO}"' in html
    assert 'alt="CloudBox99"' in html and 'width="150"' in html
    assert html.index("+91 87127 05166") < html.index("<img")     # logo under the signature
    assert "<a " not in html and "http" not in html.replace(LOGO, "")
    assert "Hi Ravi,</p>" in html and "Warm regards,<br>Haymant Yerneni" in html
    assert html_part_problems(html, BODY, persona()) == []


def test_logo_width_capped_and_no_logo_means_plain_text_only():
    assert 'width="150"' in html_alternative(BODY, persona(width=400))
    assert html_alternative(BODY, persona(logo="")) == ""
    assert html_part_problems("", BODY, persona(logo="")) == []


def test_model_html_is_escaped_never_rendered():
    sneaky = BODY.replace("Hi Ravi,", 'Hi Ravi, <img src="https://track.example/p.gif">')
    html = html_alternative(sneaky, persona())
    assert html.count("<img") == 1 and "&lt;img" in html
    # ...and the plain text itself never passes the gate with HTML in it.
    assert not pre_send_check("ravi@acme.in", "Hosting for Acme", sneaky)


def test_gate_rejects_any_html_that_is_not_the_template():
    good = html_alternative(BODY, persona())
    pixel = good.replace("</div>", '<img src="https://track.example/p.gif" width="1"></div>')
    assert any("exactly one image" in p or "other than the logo" in p
               for p in html_part_problems(pixel, BODY, persona()))
    linked = good.replace("Hi Ravi,", '<a href="https://x.example">Hi</a>')
    assert html_part_problems(linked, BODY, persona())
    edited = good.replace("Hi Ravi,", "Hi Ravi, buy now")
    assert any("not the signature template" in p for p in html_part_problems(edited, BODY, persona()))
    assert html_part_problems(good, BODY, persona(logo=""))     # HTML without a configured logo


def test_logo_url_must_be_https():
    from harvey.config import PersonaConfig
    base = dict(name="a", company="b", role="c", email="d@e.com", linkedin="", tone="t")
    assert PersonaConfig(**base, logo_url=LOGO).logo_url == LOGO
    with pytest.raises(Exception):
        PersonaConfig(**base, logo_url="http://insecure.example/logo.png")
    with pytest.raises(Exception):
        PersonaConfig(**base, logo_url='https://x.example/a.png" onerror="x')


@pytest.mark.asyncio
async def test_gmail_builds_multipart_alternative(monkeypatch):
    from harvey.integrations.gmail import GmailProvider
    import base64

    sent = {}
    provider = GmailProvider.__new__(GmailProvider)
    provider.config = NS(persona=persona())

    async def fake_request(method, path, **kw):
        if method == "POST":
            sent["raw"] = kw["json"]["raw"]
            return {"id": "g1", "threadId": "t1"}
        return {}

    provider._request = fake_request
    html = html_alternative(BODY, persona())
    result = await provider.send_email("ravi@acme.in", "Hosting for Acme", BODY, html=html)
    assert result.ok
    msg = message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
    assert msg.get_content_type() == "multipart/alternative"
    parts = [p.get_content_type() for p in msg.get_payload()]
    assert parts == ["text/plain", "text/html"]                   # plain first, HTML preferred
    plain = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    assert plain.strip() == BODY.strip()                           # plain text unchanged

    sent.clear()
    await provider.send_email("ravi@acme.in", "Hosting for Acme", BODY)
    assert message_from_bytes(base64.urlsafe_b64decode(sent["raw"])).get_content_type() == "text/plain"


# ── CTO-first targeting ──

def scout(titles=("CTO", "Head of Technology", "VP Engineering", "IT Head"),
          fallback=("CEO", "Founder", "Managing Director", "Owner")):
    from harvey.agents.scout import Scout
    s = Scout.__new__(Scout)
    s.config = NS(icp=NS(titles=list(titles), fallback_titles=list(fallback)))
    return s


def test_primary_titles_win_over_fallback():
    s = scout()
    keep, fallback = s._pick_targets(["Founder & CEO", "CTO", "Sales Manager", "IT Head"])
    assert keep == [1, 3] and fallback is False


def test_fallback_to_ceo_founder_when_no_cto():
    s = scout()
    keep, fallback = s._pick_targets(["Founder", "Office Manager", "Managing Director"])
    assert keep == [0, 2] and fallback is True
    note = s._fallback_note("Acme Tech", "Founder")
    assert note.startswith(TARGETING_PREFIX) and "CTO" in note and "Founder" in note
    assert s._pick_targets(["Office Manager"]) == ([], True)


def test_titles_match_whole_words_only():
    s = scout()
    assert s._title_tier("Director of Sales") == ""            # "cto" inside "Director"
    assert s._title_tier("Co-Founder & CTO") == "primary"
    assert s._title_tier("Owner") == "fallback"


@pytest.mark.asyncio
async def test_targeting_note_is_never_shown_to_the_model():
    from harvey.agents.writer import Writer

    prompts = []

    class Brain:
        def load_prompt(self, *a, **k):
            return ""

        async def think_json(self, prompt, **kw):
            prompts.append(prompt)
            return None

    cfg = NS(persona=NS(name="Haymant Yerneni", company="CloudBox99", role="Sales Team",
                        tone="warm", signature=SIG),
             product=NS(name="CloudBox99", description="", key_benefits=[], pricing=""),
             channels=NS(email=NS(provider="gmail")))
    w = Writer(brain=Brain(), state=NS(), config=cfg)
    p = Prospect(first_name="Ravi", last_name="K", title="Founder", company="Acme Tech",
                 industry="software", email="ravi@acme.in",
                 personalization_notes="Builds payroll software.\n"
                                       f"{TARGETING_PREFIX} fallback contact. No CTO found.")
    await w._write_personal_email(p)
    assert prompts and "Builds payroll software." in prompts[0]
    assert TARGETING_PREFIX not in prompts[0]
