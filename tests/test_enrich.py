from __future__ import annotations

import json

from agent_reach.core.enrich import enrich_items


def _payload(items, *, ok=True):
    return {
        "schema_version": 1,
        "ok": ok,
        "channel": "web",
        "action": "search",
        "backend": "exa",
        "items": items,
        "meta": {"attempts": []},
        "error": None,
    }


def _read_ok(text="Body " * 100, title="Read title"):
    return {
        "schema_version": 1,
        "ok": True,
        "channel": "web",
        "action": "read",
        "backend": "jina",
        "items": [{"title": title, "url": None, "text": text, "raw": {"truncated": False}}],
        "meta": {},
        "error": None,
    }


def test_enrich_reads_only_short_results_up_to_limit():
    items = [
        {"title": "one", "url": "https://one.example/a", "text": "short"},
        {"title": "two", "url": "https://two.example/b", "text": "long " * 100},
        {"title": "three", "url": "https://three.example/c", "text": None},
    ]
    calls = []

    def read_one(url):
        calls.append(url)
        return _read_ok(text=f"body for {url}", title="Read title")

    result = enrich_items(_payload(items), limit=3, read_one=read_one)

    assert calls == ["https://one.example/a", "https://three.example/c"]
    assert result["ok"] is True
    assert result["meta"]["enriched"] == 2
    assert result["meta"]["enrich_errors"] == []
    assert result["items"][0]["text"] == "body for https://one.example/a"
    assert result["items"][0]["raw"]["search_text"] == "short"
    assert result["items"][0]["raw"]["enriched"]["source"] == "jina"
    assert result["items"][1]["text"] == "long " * 100
    assert result["items"][2]["title"] == "three"
    assert result["items"][2]["text"] == "body for https://three.example/c"


def test_enrich_limit_caps_the_number_of_reads():
    items = [
        {"title": str(index), "url": f"https://example.com/{index}", "text": ""}
        for index in range(5)
    ]
    calls = []
    result = enrich_items(
        _payload(items),
        limit=2,
        read_one=lambda url: calls.append(url) or _read_ok(),
    )
    assert len(calls) == 2
    assert result["meta"]["enriched"] == 2


def test_enrich_isolates_failures_and_keeps_search_results():
    items = [
        {"title": "bad", "url": "https://bad.example/x", "text": ""},
        {"title": "good", "url": "https://good.example/y", "text": ""},
    ]

    def read_one(url):
        if "bad" in url:
            return {
                "ok": False,
                "error": {"code": "TIMEOUT", "message": "read timed out"},
            }
        return _read_ok(text="good body")

    result = enrich_items(_payload(items), limit=2, read_one=read_one)

    assert result["ok"] is True
    assert result["meta"]["enriched"] == 1
    assert result["meta"]["enrich_errors"] == [
        {"url": "https://bad.example/x", "code": "TIMEOUT", "message": "read timed out"}
    ]
    assert result["items"][0]["text"] == ""
    assert result["items"][1]["text"] == "good body"


def test_enrich_revalidates_urls_before_reading():
    items = [{"title": "internal", "url": "http://127.0.0.1/admin", "text": ""}]
    calls = []

    result = enrich_items(
        _payload(items),
        limit=1,
        read_one=lambda url: calls.append(url) or _read_ok(),
    )

    assert calls == []
    assert result["meta"]["enriched"] == 0
    assert result["meta"]["enrich_errors"][0]["url"] == "http://127.0.0.1/admin"
    assert result["meta"]["enrich_errors"][0]["code"] == "BAD_INPUT"


def test_enrich_skips_when_nothing_needs_a_body():
    items = [{"title": "done", "url": "https://example.com", "text": "x" * 500}]
    result = enrich_items(_payload(items), limit=3, read_one=lambda url: _read_ok())
    assert result["meta"]["enriched"] == 0
    assert result["meta"]["enrich_skipped"] == "no result needed a body"


def test_enrich_does_nothing_for_failed_payloads():
    payload = _payload([], ok=False)
    result = enrich_items(payload, limit=3, read_one=lambda url: _read_ok())
    assert result["meta"]["enriched"] == 0
    assert result["meta"]["enrich_errors"] == []


def test_search_enrich_flag_wires_reads(monkeypatch, capsys):
    from agent_reach import cli

    def fake_run_action(registry, routing, *, action, params, ctx, **kwargs):
        if action == "search":
            return _payload(
                [
                    {"title": "doc", "url": "https://example.com/doc", "text": ""},
                ]
            )
        assert action == "read"
        return _read_ok(text="full body", title="Doc")

    monkeypatch.setattr(cli, "run_action", fake_run_action)
    code = cli.main(["search", "doc", "--enrich", "3"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["items"][0]["text"] == "full body"
    assert payload["meta"]["enriched"] == 1
