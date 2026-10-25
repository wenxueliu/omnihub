from __future__ import annotations

import json

import pytest

from agent_reach.adapters.base import RawResult
from agent_reach.adapters.registry import build_adapters
from agent_reach.core.pipeline import load_platform_module, run_action
from agent_reach.core.registry import BUILTIN_PLATFORM_DIR, discover, load_platform
from agent_reach.core.result import RATE_LIMITED, UPSTREAM_BROKEN, ReachError

WEB_DIR = BUILTIN_PLATFORM_DIR / "web"


def _sse(text: str) -> str:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}]},
    }
    return "event: message\ndata: " + json.dumps(message) + "\n"


EXA_TEXT = (
    "Title: Example\n"
    "URL: https://example.com/\n"
    "Published: 2024-01-01\n"
    "Author: Alice\n"
    "\n"
    "Some body\n"
)


class FakeHttpCtx:
    timeout = 30

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def secret(self, name):
        return None


def _web_parser():
    platform = load_platform(WEB_DIR, origin="builtin")
    return load_platform_module(platform, "parse.py")


def test_web_manifest_is_valid():
    platform = load_platform(WEB_DIR, origin="builtin")
    assert platform.id == "web"
    assert set(platform.actions) == {"search", "read"}
    assert platform.actions["read"].params["target"]["format"] == "url"


def test_exa_search_parses_sse():
    parser = _web_parser()
    raw = RawResult(
        text=_sse(EXA_TEXT),
        meta={"params": {"query": "x", "limit": 2}},
    )
    items = parser.exa_search(raw)
    assert items[0]["title"] == "Example"
    assert items[0]["url"] == "https://example.com/"


def test_exa_search_rate_limit_is_mapped():
    parser = _web_parser()
    message = {"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "rate limit"}}
    raw = RawResult(text="data: " + json.dumps(message))
    with pytest.raises(ReachError) as info:
        parser.exa_search(raw)
    assert info.value.code == RATE_LIMITED


def test_exa_search_unrecognized_response_is_upstream_broken():
    parser = _web_parser()
    with pytest.raises(ReachError) as info:
        parser.exa_search(RawResult(text="not json"))
    assert info.value.code == UPSTREAM_BROKEN


def test_jina_parser_extracts_content():
    parser = _web_parser()
    raw = RawResult(
        text=(
            "Title: T\n"
            "Published Time: 2024-02-02\n"
            "\n"
            "Markdown Content:\n"
            "Hello world\n"
        ),
        meta={"params": {"target": "https://example.com/"}},
    )
    item = parser.jina(raw)[0]
    assert item == {
        "title": "T",
        "url": "https://example.com/",
        "author": None,
        "published_at": "2024-02-02",
        "text": "Hello world",
        "raw": None,
    }


def test_jina_antibot_page_is_upstream_broken():
    parser = _web_parser()
    body = "Title: Just a moment...\nWarning: requiring captcha\n## Performing security verification\n"
    raw = RawResult(text=body, meta={"params": {"target": "https://example.com/"}})
    with pytest.raises(ReachError) as info:
        parser.jina(raw)
    assert info.value.code == UPSTREAM_BROKEN


def test_exa_read_falls_back_to_plain_text():
    parser = _web_parser()
    raw = RawResult(
        text=_sse("Just some content"),
        meta={"params": {"target": "https://example.com/"}},
    )
    item = parser.exa_read(raw)[0]
    assert item["url"] == "https://example.com/"
    assert item["text"] == "Just some content"


def test_web_search_end_to_end_with_fake_http():
    registry = discover()
    ctx = FakeHttpCtx([RawResult(text=_sse(EXA_TEXT), content_type="text/event-stream")])
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="search",
        params={"query": "x", "limit": 2},
        platform="web",
        ctx=ctx,
        adapters=build_adapters(),
    )
    assert result["ok"] is True
    assert result["channel"] == "web"
    assert result["backend"] == "exa"
    assert result["items"][0]["platform"] == "web"
    assert result["items"][0]["source"] == "exa"
    assert ctx.calls[0][0] == "https://mcp.exa.ai/mcp"


def test_web_read_uses_jina_first():
    registry = discover()
    ctx = FakeHttpCtx(
        [
            RawResult(
                text="Title: T\n\nMarkdown Content:\nHello\n",
                content_type="text/plain",
            )
        ]
    )
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="read",
        params={"target": "https://example.com/"},
        platform="web",
        ctx=ctx,
        adapters=build_adapters(),
    )
    assert result["ok"] is True
    assert result["backend"] == "jina"
    assert ctx.calls[0][0] == "https://r.jina.ai/https://example.com/"
    assert result["items"][0]["text"] == "Hello"


def test_web_read_falls_back_to_exa():
    registry = discover()
    ctx = FakeHttpCtx(
        [
            RawResult(text="", content_type="text/plain"),
            RawResult(text=_sse(EXA_TEXT), content_type="text/event-stream"),
        ]
    )
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="read",
        params={"target": "https://example.com/"},
        platform="web",
        ctx=ctx,
        adapters=build_adapters(),
    )
    assert result["ok"] is True
    assert result["backend"] == "exa"
    assert [attempt["result"] for attempt in result["meta"]["attempts"]] == ["ERROR", "OK"]
