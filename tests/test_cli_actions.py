from __future__ import annotations

import json

from agent_reach.cli import main


def _payload(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured


def test_search_noop_json(capsys):
    assert main(["search", "hello", "--platform", "noop", "--limit", "2"]) == 0
    payload, _ = _payload(capsys)
    assert payload["ok"] is True
    assert payload["channel"] == "noop"
    assert payload["action"] == "search"
    assert len(payload["items"]) == 2
    assert "hello" in payload["items"][0]["title"]


def test_search_noop_text(capsys):
    assert main(["search", "hello", "--platform", "noop", "--limit", "1", "--text"]) == 0
    captured = capsys.readouterr()
    assert "noop result 1 for hello" in captured.out
    assert "https://example.com/noop/1" in captured.out


def test_read_noop_json(capsys):
    assert (
        main(["read", "https://example.com/a", "--platform", "noop"]) == 0
    )
    payload, _ = _payload(capsys)
    assert payload["ok"] is True
    assert payload["action"] == "read"
    assert payload["items"][0]["url"] == "https://example.com/a"


def test_read_noop_text_prints_full_content(capsys):
    assert main(["read", "https://example.com/a", "--platform", "noop", "--text"]) == 0
    captured = capsys.readouterr()
    assert "Deterministic offline content" in captured.out


def test_read_rejects_non_public_target(capsys):
    assert main(["read", "file:///etc/passwd"]) == 2
    payload, _ = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BAD_INPUT"


def test_search_unknown_platform_is_bad_input(capsys):
    assert main(["search", "hello", "--platform", "nope"]) == 2
    payload, _ = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BAD_INPUT"


def test_search_rejects_bad_timeout(capsys):
    assert main(["search", "hello", "--platform", "noop", "--timeout", "0.2"]) == 2
    payload, _ = _payload(capsys)
    assert payload["error"]["code"] == "BAD_INPUT"
