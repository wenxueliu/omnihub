from __future__ import annotations

import json

import pytest

from omnihub.adapters.base import Health, RawResult
from omnihub.adapters.registry import build_adapters
from omnihub.core.pipeline import run_action
from omnihub.core.registry import BUILTIN_PLATFORM_DIR, discover, load_platform
from omnihub.core.result import RATE_LIMITED, UPSTREAM_BROKEN, OmniHubError
from omnihub.parse import web as web_parser

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

BING_TEXT = (
    "Title: python asyncio - Bing\n"
    "\n"
    "URL Source: https://www.bing.com/search?q=python+asyncio\n"
    "\n"
    "Markdown Content:\n"
    "45,700 results\n"
    "\n"
    "1.   ## [**asyncio** — Asynchronous I/O — **Python** documentation]"
    "(https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9kb2NzLnB5dGhvbi5vcmcvMy9saWJyYXJ5L2FzeW5jaW8uaHRtbA&ntb=1)\n"
    "\n"
    "asyncio is a library to write concurrent code.\n"
    "\n"
    "2.   ## [**Python**'s **asyncio**: A Hands-On Walkthrough]"
    "(https://www.bing.com/ck/a?!&&p=def&u=a1aHR0cHM6Ly9yZWFscHl0aG9uLmNvbS9hc3luYy1pby1weXRob24v&ntb=1)\n"
    "\n"
    "A walkthrough of async I/O.\n"
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
    return web_parser


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
    with pytest.raises(OmniHubError) as info:
        parser.exa_search(raw)
    assert info.value.code == RATE_LIMITED


def test_exa_search_unrecognized_response_is_upstream_broken():
    parser = _web_parser()
    with pytest.raises(OmniHubError) as info:
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
    with pytest.raises(OmniHubError) as info:
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
    assert [attempt["result"] for attempt in result["meta"]["attempts"]] == [
        "ERROR",
        "SKIP",
        "OK",
    ]


class HealthyDaemon:
    kind = "daemon"

    def __init__(self, data):
        self.data = data
        self.calls = []

    def probe(self):
        return Health(ok=True, detail="kimi-webbridge is running")

    def invoke(self, call, *, ctx):
        self.calls.append(call)
        return RawResult(data=self.data, meta={})


def test_web_read_uses_browser_when_jina_fails():
    registry = discover()
    ctx = FakeHttpCtx([RawResult(text="", content_type="text/plain")])
    daemon = HealthyDaemon(
        {
            "url": "https://example.com/",
            "title": "Browser title",
            "text": "Rendered by the user's browser",
            "truncated": False,
        }
    )
    adapters = build_adapters()
    adapters["daemon"] = daemon
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="read",
        params={"target": "https://example.com/"},
        platform="web",
        ctx=ctx,
        adapters=adapters,
    )
    assert result["ok"] is True
    assert result["backend"] == "kimi-browser"
    assert result["items"][0]["title"] == "Browser title"
    assert result["items"][0]["text"] == "Rendered by the user's browser"
    assert daemon.calls[0].call == "read"
    assert daemon.calls[0].args["url"] == "https://example.com/"


def test_bing_search_parses_markdown_and_decodes_redirects():
    parser = _web_parser()
    raw = RawResult(text=BING_TEXT, meta={"params": {"limit": 2}})
    items = parser.bing_search(raw)
    assert [item["url"] for item in items] == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://realpython.com/async-io-python/",
    ]
    assert items[0]["title"] == "asyncio — Asynchronous I/O — Python documentation"
    assert items[0]["text"] == "asyncio is a library to write concurrent code."
    assert items[1]["title"] == "Python's asyncio: A Hands-On Walkthrough"


def test_bing_search_rejects_unrecognized_output():
    parser = _web_parser()
    with pytest.raises(OmniHubError) as info:
        parser.bing_search(RawResult(text="Sorry, something went wrong."))
    assert info.value.code == UPSTREAM_BROKEN


def test_web_search_engine_bing_skips_exa():
    registry = discover()
    ctx = FakeHttpCtx([RawResult(text=BING_TEXT, content_type="text/plain")])
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="search",
        params={"query": "python asyncio", "limit": 2, "engine": "bing"},
        platform="web",
        ctx=ctx,
        adapters=build_adapters(),
    )
    assert result["ok"] is True
    assert result["backend"] == "bing"
    assert result["items"][0]["source"] == "bing"
    assert len(ctx.calls) == 1
    assert ctx.calls[0][0].startswith("https://r.jina.ai/https://www.bing.com/search")


def test_web_search_engine_google_uses_the_browser():
    registry = discover()
    ctx = FakeHttpCtx([])
    daemon = HealthyDaemon(
        [
            {"title": "Example", "url": "https://example.com/", "text": "Snippet"},
        ]
    )
    adapters = build_adapters()
    adapters["daemon"] = daemon
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="search",
        params={"query": "example", "limit": 2, "engine": "google"},
        platform="web",
        ctx=ctx,
        adapters=adapters,
    )
    assert result["ok"] is True
    assert result["backend"] == "google"
    assert ctx.calls == []
    assert daemon.calls[0].call == "browse"
    assert daemon.calls[0].script.endswith("search_google.js")
    assert daemon.calls[0].args["url"].startswith("https://www.google.com/search")


def test_web_search_auto_falls_back_to_bing_when_exa_breaks():
    registry = discover()
    ctx = FakeHttpCtx(
        [
            RawResult(text="not json", content_type="text/plain"),
            RawResult(text=BING_TEXT, content_type="text/plain"),
        ]
    )
    result = run_action(
        registry,
        {"version": 1, "mode": "first_success"},
        action="search",
        params={"query": "python asyncio", "limit": 2},
        platform="web",
        ctx=ctx,
        adapters=build_adapters(),
    )
    assert result["ok"] is True
    assert result["backend"] == "bing"
    assert [attempt["result"] for attempt in result["meta"]["attempts"]] == [
        "ERROR",
        "OK",
    ]
