"""Tests for configuration loading."""

import tempfile
import os

import yaml

from harvey.config import HarveyConfig, EnvConfig, load_env


def test_config_from_dict():
    """HarveyConfig can be constructed from a dict (YAML-like)."""
    data = {
        "persona": {
            "name": "Harvey",
            "company": "Acme",
            "role": "BDR",
            "email": "harvey@acme.com",
            "linkedin": "",
            "tone": "professional",
        },
        "product": {
            "name": "Widget",
            "description": "A great widget",
            "pricing": "$99/mo",
            "key_benefits": ["Fast", "Reliable"],
            "objection_responses": {"too expensive": "Consider the ROI"},
        },
        "icp": {
            "industries": ["SaaS"],
            "company_size": "10-200",
            "titles": ["VP of Sales"],
            "geography": ["US"],
        },
    }
    config = HarveyConfig(**data)
    assert config.persona.name == "Harvey"
    assert config.product.name == "Widget"
    assert config.icp.industries == ["SaaS"]
    assert config.channels.email.max_daily_sends == 50  # default
    assert config.usage.heartbeat_interval_minutes == 15  # default


def test_env_config_defaults():
    """EnvConfig has sensible defaults when env vars are unset."""
    env = EnvConfig()
    assert env.instantly_api_key == ""
    assert env.serper_api_key == ""


def test_offer_config_defaults():
    """OfferConfig defaults to book_call via calendar_link."""
    data = {
        "persona": {"name": "H", "company": "A", "role": "B", "email": "e", "linkedin": "", "tone": "t"},
        "product": {"name": "P", "description": "D", "pricing": "$1", "key_benefits": [], "objection_responses": {}},
        "icp": {"industries": [], "company_size": "", "titles": [], "geography": []},
    }
    config = HarveyConfig(**data)
    assert config.product.offer.goal == "book_call"
    assert config.product.offer.booking_method == "calendar_link"
    assert config.product.offer.meeting_duration == "15 minutes"


def test_load_config_reads_utf8_regardless_of_locale(monkeypatch, tmp_path):
    """Windows defaults to cp1252; harvey.yaml (and skills) are UTF-8."""
    import builtins
    from harvey.config import load_config

    path = tmp_path / "harvey.yaml"
    path.write_bytes(open("harvey.yaml", "rb").read())
    real_open = builtins.open

    def cp1252_default_open(file, mode="r", *args, encoding=None, **kw):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(file, mode, *args, encoding=encoding, **kw)

    monkeypatch.setattr(builtins, "open", cp1252_default_open)
    cfg = load_config(str(path))
    assert "—" in cfg.persona.tone
