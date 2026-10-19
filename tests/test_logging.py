"""Secrets must not reach the console or the dashboard log."""

import logging


def test_request_urls_with_api_keys_are_not_logged():
    """Verifier/Hunter keys travel in the query string; httpx logs URLs at INFO."""
    import harvey.main  # noqa: F401 — configures logging on import

    for name in ("httpx", "httpcore"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
