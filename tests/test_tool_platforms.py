from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import agent_reach.core.pipeline as pipeline
from agent_reach.adapters.base import RawResult
from agent_reach.adapters.builtin import BuiltinAdapter
from agent_reach.core.config import DEFAULT_ROUTING
from agent_reach.core.doctor import build_doctor_report
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.router import build_plan

PLATFORMS = Path(__file__).resolve().parents[1] / "agent_reach" / "platforms"


def _fixture(platform: str, name: str):
    return json.loads((PLATFORMS / platform / "fixtures" / name).read_text(encoding="utf-8"))


def _registry():
    return discover(plugin_dirs=[], include_builtin=True)


class ProcessStub:
    def __init__(self, stdout: str = "", *, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.truncated = False


class FakeCtx:
    timeout = 30

    def __init__(self, *, http_responder=None, process_responder=None, secrets=None):
        self.http_responder = http_responder
        self.process_responder = process_responder
        self.secrets = dict(secrets or {})
        self.http_calls = []
        self.run_calls = []

    def secret(self, name):
        return self.secrets.get(name)

    def http(self, url, *, method="GET", headers=None, data=None, **kwargs):
        self.http_calls.append(
            {"url": url, "method": method, "headers": dict(headers or {}), "data": data}
        )
        if self.http_responder is None:  # pragma: no cover - test bug
            raise AssertionError("unexpected HTTP request")
        return self.http_responder(url, data)

    def run(self, argv, **kwargs):
        self.run_calls.append(list(argv))
        if self.process_responder is None:  # pragma: no cover - test bug
            raise AssertionError("unexpected subprocess")
        return self.process_responder(list(argv))


def _adapters():
    return {"builtin": BuiltinAdapter()}


@pytest.fixture
def notebooklm_installed(monkeypatch):
    """Pretend the optional notebooklm CLI exists so its guard passes."""
    real_which = shutil.which
    monkeypatch.setattr(
        pipeline.shutil,
        "which",
        lambda name, *args, **kwargs: "/usr/bin/notebooklm"
        if name == "notebooklm"
        else real_which(name, *args, **kwargs),
    )


def _run(action, params, *, ctx, platform, url=None):
    return run_action(
        _registry(),
        DEFAULT_ROUTING,
        action=action,
        params=params,
        ctx=ctx,
        platform=platform,
        url=url,
        adapters=_adapters(),
    )


def test_tool_platforms_enter_the_catalog():
    registry = _registry()
    assert not registry.errors
    assert {"deepwiki", "notebooklm"} <= set(registry.platforms)

    deepwiki = registry.platforms["deepwiki"]
    assert deepwiki.priority == 200
    assert deepwiki.level == "optional"
    assert deepwiki.supports("read")
    assert set(deepwiki.domains) == {"deepwiki.com", "www.deepwiki.com"}

    notebooklm = registry.platforms["notebooklm"]
    assert notebooklm.priority == 210
    assert notebooklm.level == "optional"
    assert notebooklm.supports("search") and notebooklm.supports("read")
    assert notebooklm.auth.get("session") == "notebooklm"


def test_deepwiki_url_routes_before_web():
    plan = build_plan(
        _registry(),
        DEFAULT_ROUTING,
        action="read",
        url="https://deepwiki.com/facebook/react",
    )
    assert plan.platform_ids[0] == "deepwiki"
    assert plan.platform_ids[-1] == "web"


@pytest.mark.parametrize(
    "target",
    [
        "facebook/react",
        "https://github.com/facebook/react",
        "https://github.com/facebook/react/tree/main/packages",
        "https://deepwiki.com/facebook/react/2-architecture",
        "github.com/facebook/react",
    ],
)
def test_deepwiki_read_normalizes_repository_targets(target):
    ctx = FakeCtx(http_responder=lambda url, data: RawResult(data=_fixture("deepwiki", "read.json")))
    result = _run("read", {"target": target}, ctx=ctx, platform="deepwiki")

    assert result["ok"] is True
    assert result["backend"] == "deepwiki-mcp"
    item = result["items"][0]
    assert item["title"] == "facebook/react — DeepWiki"
    assert item["url"] == "https://deepwiki.com/facebook/react"
    assert "virtual tree" in item["text"]

    body = json.loads(ctx.http_calls[0]["data"])
    assert body["params"]["arguments"]["repoName"] == "facebook/react"


def test_deepwiki_read_parses_an_sse_response():
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": _fixture("deepwiki", "read.json")["result"]}
    )
    ctx = FakeCtx(
        http_responder=lambda url, data: RawResult(
            text=f"event: message\ndata: {payload}\n\n", content_type="text/event-stream"
        )
    )
    result = _run("read", {"target": "facebook/react"}, ctx=ctx, platform="deepwiki")
    assert result["ok"] is True
    assert result["items"][0]["url"] == "https://deepwiki.com/facebook/react"


def test_deepwiki_unknown_repository_is_not_found():
    ctx = FakeCtx(
        http_responder=lambda url, data: RawResult(
            data={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "Repository not found. Visit https://deepwiki.com to index it.",
                        }
                    ],
                },
            }
        )
    )
    result = _run("read", {"target": "example/does-not-exist"}, ctx=ctx, platform="deepwiki")
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"
    assert "deepwiki.com" in (result["error"].get("hint") or "")


def test_deepwiki_rejects_a_foreign_host():
    ctx = FakeCtx(
        http_responder=lambda url, data: RawResult(data=_fixture("deepwiki", "read.json"))
    )
    result = _run(
        "read",
        {"target": "https://gitlab.com/facebook/react"},
        ctx=ctx,
        platform="deepwiki",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "BAD_INPUT"
    assert ctx.http_calls == []


def test_notebooklm_search_filters_titles_and_respects_limit(notebooklm_installed):
    payload = json.dumps(_fixture("notebooklm", "search.json"))
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub(payload))
    result = _run(
        "search", {"query": "python", "limit": 1}, ctx=ctx, platform="notebooklm"
    )

    assert result["ok"] is True
    assert result["backend"] == "notebooklm-cli"
    assert ctx.run_calls == [["notebooklm", "list", "--json"]]
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["title"] == "Python asyncio deep dive"
    assert item["url"] == "https://notebooklm.google.com/notebook/nbb0987654321"
    assert item["published_at"] == "2026-05-19T12:00:00Z"
    assert "nbb0987654321" in item["text"]


def test_notebooklm_search_without_query_returns_every_notebook(notebooklm_installed):
    payload = json.dumps(_fixture("notebooklm", "search.json"))
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub(payload))
    result = _run("search", {"query": ""}, ctx=ctx, platform="notebooklm")
    assert result["ok"] is True
    assert len(result["items"]) == 3


def test_notebooklm_read_summarizes_a_notebook_url(notebooklm_installed):
    payload = json.dumps(_fixture("notebooklm", "read.json"))
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub(payload))
    result = _run(
        "read",
        {"target": "https://notebooklm.google.com/notebook/nbb0987654321?authuser=1"},
        ctx=ctx,
        platform="notebooklm",
    )

    assert result["ok"] is True
    assert ctx.run_calls == [
        ["notebooklm", "summary", "--json", "--topics", "--notebook", "nbb0987654321"]
    ]
    item = result["items"][0]
    assert item["url"] == "https://notebooklm.google.com/notebook/nbb0987654321"
    assert "structured concurrency" in item["text"]
    assert "When should asyncio.TaskGroup replace gather?" in item["text"]


@pytest.mark.parametrize("target", ["", "   ", "-rf"])
def test_notebooklm_read_rejects_an_invalid_target(target, notebooklm_installed):
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub("{}"))
    result = _run("read", {"target": target}, ctx=ctx, platform="notebooklm")
    assert result["ok"] is False
    assert result["error"]["code"] == "BAD_INPUT"
    assert ctx.run_calls == []


def test_notebooklm_read_maps_invalid_json_to_upstream_broken(notebooklm_installed):
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub("<html>not json</html>"))
    result = _run("read", {"target": "nbb0987654321"}, ctx=ctx, platform="notebooklm")
    assert result["ok"] is False
    assert result["error"]["code"] == "UPSTREAM_BROKEN"


def test_notebooklm_search_skips_when_the_cli_is_missing(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    ctx = FakeCtx(process_responder=lambda argv: ProcessStub("{}"))
    result = _run("search", {"query": "python"}, ctx=ctx, platform="notebooklm")
    assert result["ok"] is False
    assert result["meta"]["attempts"][0]["result"] == "SKIP"
    assert ctx.run_calls == []


def test_doctor_degrades_notebooklm_when_the_cli_is_missing(monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: None
        if name == "notebooklm"
        else real_which(name, *args, **kwargs),
    )
    ctx = FakeCtx()
    report = build_doctor_report(_registry(), ctx=ctx, adapters=_adapters())
    rows = {row["id"]: row for row in report["platforms"]}

    row = rows["notebooklm"]
    assert row["status"] == "degraded"
    tools = {tool["name"]: tool for tool in row["tools"]}
    assert tools["notebooklm"]["required"] is False
    assert tools["notebooklm"]["ok"] is False
    assert rows["deepwiki"]["status"] == "ok"
