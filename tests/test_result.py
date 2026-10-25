from __future__ import annotations

import pytest

from omnihub.core.result import (
    DEFAULT_HINTS,
    FINAL_CODES,
    NEED_SETUP,
    TIMEOUT,
    UPSTREAM_BROKEN,
    OmniHubError,
    classify,
    envelope,
    make_item,
    unix_to_iso,
)
from omnihub.utils.text import scrub_url_credentials


def test_success_envelope_has_stable_shape():
    item = make_item(title="T", url="https://example.com", text="S")
    payload = envelope(
        channel="noop",
        action="search",
        backend="builtin",
        items=[item],
        elapsed_ms=12,
        attempts=[{"backend": "builtin", "result": "OK"}],
        meta={"platform": "noop"},
    )
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["channel"] == "noop"
    assert payload["action"] == "search"
    assert payload["backend"] == "builtin"
    assert payload["items"] == [item]
    assert payload["meta"]["platform"] == "noop"
    assert payload["error"] is None


def test_error_envelope_clears_items_and_uses_default_hint():
    payload = envelope(channel="noop", action="read", error=OmniHubError(TIMEOUT, "slow"))
    assert payload["ok"] is False
    assert payload["backend"] is None
    assert payload["items"] == []
    assert payload["error"]["code"] == TIMEOUT
    assert payload["error"]["hint"] == DEFAULT_HINTS[TIMEOUT]


def test_final_codes_stop_fallback():
    assert TIMEOUT not in FINAL_CODES
    assert "NEED_LOGIN" in FINAL_CODES
    assert "BAD_INPUT" in FINAL_CODES


def test_make_item_core_and_provenance_keys():
    core = make_item()
    assert set(core) == {"title", "url", "author", "published_at", "text", "raw"}

    enriched = make_item(platform="noop", source="builtin", priority=10)
    assert enriched["platform"] == "noop"
    assert enriched["source"] == "builtin"
    assert enriched["priority"] == 10


def test_classify_maps_common_failures():
    assert classify(FileNotFoundError("missing")).code == NEED_SETUP
    assert classify(TimeoutError()).code == TIMEOUT
    assert classify(RuntimeError("boom")).code == UPSTREAM_BROKEN


def test_unix_to_iso():
    assert unix_to_iso(0) is None
    assert unix_to_iso("bad") is None
    assert unix_to_iso(1700000000) == "2023-11-14T22:13:20Z"


def test_scrub_url_credentials():
    text = "https://user:pass@example.com/a?token=secret&ok=1"
    scrubbed = scrub_url_credentials(text)
    assert "pass" not in scrubbed
    assert "secret" not in scrubbed
    assert "***" in scrubbed
