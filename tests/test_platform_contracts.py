from __future__ import annotations

import json
import shutil
from pathlib import Path

from omnihub.adapters.base import Health, RawResult
from omnihub.adapters.builtin import BuiltinAdapter
from omnihub.adapters.registry import build_adapters
from omnihub.core.config import DEFAULT_ROUTING
from omnihub.core.doctor import build_doctor_report
from omnihub.core.pipeline import run_action
from omnihub.core.registry import discover
from omnihub.core.result import NEED_SETUP, NOT_FOUND, OmniHubError
from omnihub.core.router import build_plan

PLATFORMS = Path(__file__).resolve().parents[1] / "omnihub" / "platforms"


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
        raise OmniHubError(NOT_FOUND, f"unexpected URL: {url}")


def _adapters(http_adapter):
    return {"http": http_adapter, "builtin": BuiltinAdapter()}


def test_builtin_catalog_includes_m2_platforms():
    registry = _registry()
    ids = set(registry.platforms)
    assert {"github", "v2ex", "bilibili", "reddit", "youtube", "web"} <= ids
    assert registry.platforms["github"].priority == 100
    assert registry.platforms["bilibili"].priority == 110
    assert registry.platforms["v2ex"].priority == 120
    assert registry.platforms["reddit"].priority == 130
    assert registry.platforms["youtube"].priority == 140
    assert registry.platforms["web"].priority == 10
    for platform_id in ("github", "v2ex", "bilibili", "reddit", "youtube"):
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


class DaemonAdapterStub:
    kind = "daemon"

    def __init__(self, data=None, *, ok=True, detail=None):
        self.data = data or {}
        self.ok = ok
        self.detail = detail
        self.calls = []

    def probe(self):
        return Health(ok=self.ok, detail=self.detail)

    def invoke(self, call, *, ctx):
        self.calls.append(call)
        return RawResult(data=self.data)


def _bilibili_adapters(daemon, http):
    return {"daemon": daemon, "http": http, "builtin": BuiltinAdapter()}


def test_bilibili_read_downloads_subtitles_through_the_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIHUB_RAW_DIR", str(tmp_path / "_raw"))
    registry = _registry()
    daemon = DaemonAdapterStub(
        {
            "title": "【纪录片】示例视频",
            "owner": "示例UP主",
            "pubdate": 1700000000,
            "duration": 61,
            "bvid": "BV1S1dPBYEG9",
            "cid": 38164562135,
            "track_count": 1,
            "subtitles": [
                {
                    "lan": "ai-zh",
                    "lan_doc": "中文",
                    "url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/demo.json",
                }
            ],
        }
    )
    ctx = FakeCtx(
        [
            (
                "aisubtitle.hdslb.com",
                RawResult(
                    data={"body": [{"from": 0.0, "to": 2.0, "content": "第一句"}]}
                ),
            )
        ]
    )

    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": "https://www.bilibili.com/video/BV1S1dPBYEG9"},
        ctx=ctx,
        platform="bilibili",
        adapters=_bilibili_adapters(daemon, HttpAdapterStub(lambda call: RawResult())),
    )

    assert result["ok"] is True
    assert result["backend"] == "kimi-browser"
    assert result["items"][0]["title"] == "【纪录片】示例视频"
    assert result["items"][0]["text"] == "第一句"
    assert result["items"][0]["raw"]["lan"] == "ai-zh"
    assert len(result["items"][0]["raw"]["saved_files"]) == 2
    assert (tmp_path / "_raw").is_dir()
    assert daemon.calls[0].call == "browse"
    assert daemon.calls[0].args["url"] == "https://www.bilibili.com/video/BV1S1dPBYEG9"


def test_bilibili_read_skips_the_browser_source_when_the_daemon_is_down():
    registry = _registry()
    daemon = DaemonAdapterStub(
        ok=False, detail="kimi-webbridge daemon is not reachable"
    )

    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": "https://www.bilibili.com/video/BV1S1dPBYEG9"},
        ctx=FakeCtx(),
        platform="bilibili",
        adapters=_bilibili_adapters(daemon, HttpAdapterStub(lambda call: RawResult())),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == NEED_SETUP
    assert result["meta"]["attempts"][0]["result"] == "SKIP"
    assert "kimi-webbridge" in result["meta"]["attempts"][0]["reason"]
    assert daemon.calls == []


def test_doctor_reports_optional_auth_secret_without_leaking_values():
    registry = _registry()
    ctx = FakeCtx()
    report = build_doctor_report(
        registry, ctx=ctx, adapters=build_adapters(context=ctx)
    )
    rows = {row["id"]: row for row in report["platforms"]}
    auth = rows["github"]["auth"]
    assert auth["configured"] is False
    assert "GITHUB_TOKEN" in auth["detail"]
    assert rows["github"]["status"] == "ok"
    assert rows["bilibili"]["auth"]["detail"] == "no login required"
    assert "SESSDATA" not in json.dumps(report, ensure_ascii=False)


def test_reddit_search_uses_exa_and_keeps_provenance():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("reddit", "search.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "asyncio", "limit": 2},
        ctx=FakeCtx(),
        platform="reddit",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["channel"] == "reddit"
    assert result["backend"] == "exa"
    assert len(result["items"]) == 2
    assert result["items"][0]["platform"] == "reddit"
    assert result["items"][0]["url"].startswith("https://www.reddit.com/")


def test_reddit_prefers_the_browser_daemon_for_both_actions():
    registry = _registry()
    for action_name in ("search", "read"):
        sources = registry.platforms["reddit"].actions[action_name].enabled_sources()
        assert [source.name for source in sources] == ["kimi-browser", "exa"]
        assert sources[0].adapter == "daemon"
        assert sources[0].when == "adapter:daemon"
        assert sources[1].adapter == "http"


def test_reddit_read_uses_exa_fetch():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("reddit", "read.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={
            "target": "https://www.reddit.com/r/learnpython/comments/wpjc6d/best_resources_for_learning_asyncio/"
        },
        ctx=FakeCtx(),
        platform="reddit",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "exa"
    assert result["items"][0]["title"].startswith("Best resources")
    assert "official Python documentation" in result["items"][0]["text"]


def test_youtube_read_returns_transcript_text():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("youtube", "read.json")))
    target = "https://www.youtube.com/watch?v=example1234"
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="read",
        params={"target": target},
        ctx=FakeCtx(),
        platform="youtube",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["items"][0]["title"] == "asyncio in Python - Full Tutorial"
    assert result["items"][0]["url"] == target
    assert "event loop" in result["items"][0]["text"]


def test_youtube_search_uses_exa_and_keeps_provenance():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("youtube", "search.json")))
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "asyncio tutorial", "limit": 2},
        ctx=FakeCtx(),
        platform="youtube",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "exa"
    assert len(result["items"]) == 2
    assert result["items"][0]["platform"] == "youtube"
    assert result["items"][0]["url"].startswith("https://www.youtube.com/watch?")


def test_bilibili_search_uses_the_wbi_api_and_keeps_provenance():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("bilibili", "search.json")))
    ctx = FakeCtx(
        [
            (
                "/x/web-interface/nav",
                RawResult(data=_fixture("bilibili", "api_nav.json")),
            ),
            (
                "/wbi/search/type",
                RawResult(data=_fixture("bilibili", "api_search.json")),
            ),
        ]
    )
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "python 异步", "limit": 2},
        ctx=ctx,
        platform="bilibili",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "bilibili-api"
    assert len(result["items"]) == 2
    assert result["items"][0]["platform"] == "bilibili"
    assert result["items"][0]["url"].startswith("https://www.bilibili.com/video/")
    assert http.calls == []


def test_bilibili_search_falls_back_to_exa_when_the_api_is_intercepted():
    registry = _registry()
    http = HttpAdapterStub(lambda call: RawResult(data=_fixture("bilibili", "search.json")))
    ctx = FakeCtx(
        [
            (
                "/x/web-interface/nav",
                RawResult(data=_fixture("bilibili", "api_nav.json")),
            ),
            (
                "/wbi/search/type",
                RawResult(data={"code": -412, "message": "请求被拦截"}),
            ),
        ]
    )
    result = run_action(
        registry,
        DEFAULT_ROUTING,
        action="search",
        params={"query": "python 异步", "limit": 2},
        ctx=ctx,
        platform="bilibili",
        adapters=_adapters(http),
    )
    assert result["ok"] is True
    assert result["backend"] == "exa"
    assert len(result["items"]) == 2


def test_read_url_routes_reddit_and_youtube_before_web():
    registry = _registry()
    for url, expected in (
        ("https://www.reddit.com/r/python/comments/abc/x/", "reddit"),
        ("https://youtu.be/example1234", "youtube"),
    ):
        plan = build_plan(registry, DEFAULT_ROUTING, action="read", url=url)
        assert plan.platform_ids[0] == expected
        assert plan.platform_ids[-1] == "web"


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
