from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent_reach.adapters.base import Health, RawResult
from agent_reach.adapters.builtin import BuiltinAdapter
from agent_reach.adapters.registry import build_adapters
from agent_reach.core.config import DEFAULT_ROUTING
from agent_reach.core.doctor import build_doctor_report
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.result import NOT_FOUND, ReachError
from agent_reach.core.router import build_plan

PLATFORMS = Path(__file__).resolve().parents[1] / "agent_reach" / "platforms"


def _fixture(platform: str, name: str):
    return json.loads((PLATFORMS / platform / "fixtures" / name).read_text(encoding="utf-8"))


def _registry():
    return discover(plugin_dirs=[], include_builtin=True)


class HttpAdapterStub:
    kind = "http"

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def probe(self):
        return Health(ok=True)

    def invoke(self, call, *, ctx):
        self.calls.append(call)
        return self.responder(call)


class FakeCtx:
    timeout = 30

    def __init__(self, routes=None, *, secrets=None):
        self.routes = list(routes or [])
        self.secrets = dict(secrets or {})
        self.calls = []

    def secret(self, name):
        return self.secrets.get(name)

    def http(self, url, *, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        for needle, result in self.routes:
            if needle in url:
                return result
        raise ReachError(NOT_FOUND, f"unexpected URL: {url}")


def _adapters(http_adapter):
    return {"http": http_adapter, "builtin": BuiltinAdapter()}


def test_builtin_catalog_includes_m2_platforms():
    registry = _registry()
    ids = set(registry.platforms)
    assert {"github", "v2ex", "bilibili", "web"} <= ids
    assert registry.platforms["github"].priority == 100
    assert registry.platforms["bilibili"].priority == 110
    assert registry.platforms["v2ex"].priority == 120
    assert registry.platforms["web"].priority == 10
    for platform_id in ("github", "v2ex", "bilibili"):
        platform = registry.platforms[platform_id]
        assert platform.supports("search")
        assert platform.supports("read")
        assert platform.domains


def test_generic_search_defaults_to_web_but_keeps_specialists_available():
    registry = _registry()
    plan = build_plan(registry, DEFAULT_ROUTING, action="search")
    assert plan.platform_ids[0] == "web"
    assert {"github", "v2ex", "bilibili"} <= set(plan.platform_ids)


def test_read_url_prefers_the_domain_platform_then_web():
    registry = _registry()
    plan = build_plan(
        registry,
        DEFAULT_ROUTING,
        action="read",
        url="https://github.com/pallets/flask",
    )
    assert plan.platform_ids[0] == "github"
    assert plan.platform_ids[-1] == "web"


def test_github_search_skips_token_source_without_secret():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("github", "search.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "flask", "limit": 1},
        ctx=FakeCtx(),
        platform="github",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["channel"] == "github"
    assert result["backend"] == "github-api"
    assert len(result["items"]) == 1
    assert result["items"][0]["platform"] == "github"
    assert result["items"][0]["source"] == "github-api"
    assert result["meta"]["attempts"][0]["result"] == "SKIP"
    assert len(http.calls) == 1
    assert "Authorization" not in http.calls[0].headers


def test_github_search_uses_token_source_when_configured():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("github", "search.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "flask", "limit": 1},
        ctx=FakeCtx(secrets={"GITHUB_TOKEN": "token-value"}),
        platform="github",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "github-token"
    assert http.calls[0].headers["Authorization"] == "Bearer token-value"
    assert "token-value" not in json.dumps(result, ensure_ascii=False)


def test_v2ex_search_uses_sov2ex_and_normalizes_items():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("v2ex", "search.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "python", "limit": 2},
        ctx=FakeCtx(),
        platform="v2ex",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["channel"] == "v2ex"
    assert result["backend"] == "sov2ex"
    assert result["items"][0]["url"] == "https://www.v2ex.com/t/1251611"
    assert result["items"][0]["published_at"] == "2026-10-25T03:04:50"


def test_v2ex_read_uses_api_with_replies():
    registry = _registry()
    ctx = FakeCtx(
        [
            ("/replies/show.json", RawResult(data=_fixture("v2ex", "replies.json"))),
            ("/topics/show.json", RawResult(data=_fixture("v2ex", "topic.json"))),
        ]
    )
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": "https://www.v2ex.com/t/1"},
        ctx=ctx,
        platform="v2ex",
        adapters=_adapters(HttpAdapterStub(lambda call: RawResult())),
    )
    assert result["ok"] is True
    assert result["backend"] == "v2ex-api"
    assert "@Jay" in result["items"][0]["text"]


def test_bilibili_read_skips_cookie_source_and_uses_jina():
    registry = _registry()
    http = HttpAdapterStub(
        lambda call: RawResult(
            text="Title: 示例视频\n\nMarkdown Content:\n# 视频正文\n内容",
            content_type="text/plain",
        )
    )
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": "https://www.bilibili.com/video/BV1xx411c7mD"},
        ctx=FakeCtx(),
        platform="bilibili",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "jina"
    assert result["items"][0]["title"] == "示例视频"
    assert "视频正文" in result["items"][0]["text"]
    assert result["meta"]["attempts"][0]["result"] == "SKIP"
    assert "BILIBILI_COOKIE" in result["meta"]["attempts"][0]["reason"]


def test_bilibili_read_uses_cookie_source_when_configured():
    registry = _registry()
    ctx = FakeCtx(
        [("/x/web-interface/view", RawResult(data=_fixture("bilibili", "view.json")))],
        secrets={"BILIBILI_COOKIE": "SESSDATA=cookie-secret"},
    )
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": "https://www.bilibili.com/video/BV1xx411c7mD"},
        ctx=ctx,
        platform="bilibili",
        adapters=_adapters(HttpAdapterStub(lambda call: RawResult())),
    )
    assert result["ok"] is True
    assert result["backend"] == "bilibili-api"
    assert result["items"][0]["title"] == "【纪录片】示例视频"
    assert "SESSDATA" not in json.dumps(result, ensure_ascii=False)


def test_doctor_reports_optional_auth_secret_without_leaking_values():
    registry = _registry()
    ctx = FakeCtx()
    report = build_doctor_report(
        registry, ctx=ctx, adapters=build_adapters(context=ctx)
    )
    rows = {row["id"]: row for row in report["platforms"]}
    for platform_id, secret in (
        ("github", "GITHUB_TOKEN"),
        ("bilibili", "BILIBILI_COOKIE"),
    ):
        auth = rows[platform_id]["auth"]
        assert auth["configured"] is False
        assert secret in auth["detail"]
        assert rows[platform_id]["status"] == "ok"
    assert "SESSDATA" not in json.dumps(report, ensure_ascii=False)


def test_removing_one_platform_directory_leaves_the_others(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    for platform_id in ("github", "v2ex", "bilibili", "web"):
        shutil.copytree(PLATFORMS / platform_id, root / platform_id)

    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert not registry.errors
    assert {"github", "v2ex", "bilibili", "web"} <= set(registry.platforms)

    shutil.rmtree(root / "v2ex")
    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert not registry.errors
    assert {"github", "bilibili", "web"} <= set(registry.platforms)
    assert "v2ex" not in registry.platforms
