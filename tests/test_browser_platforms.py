from __future__ import annotations

from pathlib import Path

from omnihub.adapters.base import Health, RawResult
from omnihub.adapters.builtin import BuiltinAdapter
from omnihub.core.config import DEFAULT_ROUTING
from omnihub.core.doctor import build_doctor_report
from omnihub.core.pipeline import run_action
from omnihub.core.registry import discover
from omnihub.core.router import build_plan

PLATFORMS = Path(__file__).resolve().parents[1] / "omnihub" / "platforms"
BROWSER_PLATFORMS = ("twitter", "xueqiu", "xiaohongshu", "linkedin")


def _registry():
    return discover(plugin_dirs=[], include_builtin=True)


class DaemonStub:
    kind = "daemon"

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def probe(self):
        return Health(ok=True)

    def invoke(self, call, *, ctx):
        self.calls.append(call)
        return self.responder(call)


class DownDaemon:
    kind = "daemon"

    def probe(self):
        return Health(ok=False, detail="daemon is not running")

    def invoke(self, call, *, ctx):
        raise AssertionError("a down adapter must not be invoked")


class HttpStub:
    kind = "http"

    def probe(self):
        return Health(ok=True)

    def invoke(self, call, *, ctx):
        raise AssertionError("these tests exercise browser sources only")


class FakeCtx:
    timeout = 30

    def secret(self, name):
        return None

    def http(self, url, *, headers=None, **kwargs):
        raise AssertionError(f"unexpected HTTP call: {url}")


SEARCH_ITEMS = {
    "twitter": [
        {
            "title": "A tweet",
            "url": "https://x.com/user/status/1",
            "author": "user",
            "text": "A tweet",
        }
    ],
    "xueqiu": [
        {
            "title": "茅台讨论",
            "url": "https://xueqiu.com/1/2",
            "author": "investor",
            "text": "茅台讨论",
        }
    ],
    "xiaohongshu": [
        {
            "title": "咖啡",
            "url": "https://www.xiaohongshu.com/explore/abc",
            "author": "author",
            "text": "咖啡",
        }
    ],
    "linkedin": [
        {
            "title": "Python Engineer",
            "url": "https://www.linkedin.com/jobs/view/42/",
            "author": "Example Corp",
            "text": "Shanghai",
        }
    ],
}


def test_browser_platforms_are_in_the_catalog_with_their_priorities():
    registry = _registry()
    expected = {
        "twitter": (150, {"x.com", "twitter.com"}),
        "xueqiu": (160, {"xueqiu.com"}),
        "xiaohongshu": (170, {"xiaohongshu.com", "xhslink.com"}),
        "xiaoyuzhou": (180, {"xiaoyuzhoufm.com"}),
        "linkedin": (190, {"linkedin.com"}),
    }
    for platform_id, (priority, domains) in expected.items():
        platform = registry.platforms[platform_id]
        assert platform.priority == priority
        assert platform.level == "optional"
        assert platform.supports("search")
        assert platform.supports("read")
        assert domains <= set(platform.domains)


def test_browser_platform_domains_route_before_the_public_web_fallback():
    registry = _registry()
    for platform_id, url in (
        ("twitter", "https://x.com/user/status/1"),
        ("xueqiu", "https://xueqiu.com/1/2"),
        ("xiaohongshu", "https://www.xiaohongshu.com/explore/abc"),
        ("xiaoyuzhou", "https://www.xiaoyuzhoufm.com/episode/abc"),
        ("linkedin", "https://www.linkedin.com/jobs/view/42/"),
    ):
        plan = build_plan(registry, DEFAULT_ROUTING, action="read", url=url)
        assert plan.platform_ids[0] == platform_id
        assert plan.platform_ids[-1] == "web"


def test_browser_search_sources_go_through_the_daemon_and_parse_items():
    registry = _registry()
    for platform_id in BROWSER_PLATFORMS:
        daemon = DaemonStub(lambda call: RawResult(data=SEARCH_ITEMS[platform_id]))
        result = run_action(
            registry,
            DEFAULT_ROUTING,
            action="search",
            params={"query": "example", "limit": 2},
            ctx=FakeCtx(),
            platform=platform_id,
            adapters={"daemon": daemon, "http": HttpStub()},
        )
        assert result["ok"] is True, platform_id
        assert result["backend"] == "kimi-browser", platform_id
        assert result["items"][0]["platform"] == platform_id
        call = daemon.calls[0]
        assert call.call == "browse"
        assert call.raw["platform_trusted"] is True
        args = call.args
        assert args["code"]
        # The search text comes from the page URL, never from interpolated
        # JavaScript source.
        assert "${" not in args["code"]
        assert "location" in args["code"]
        assert "https://" in args["url"]


def test_browser_read_sources_return_a_daemon_page():
    registry = _registry()
    targets = {
        "twitter": "https://x.com/user/status/1",
        "xueqiu": "https://xueqiu.com/1/2",
        "xiaohongshu": "https://www.xiaohongshu.com/explore/abc",
        "linkedin": "https://www.linkedin.com/jobs/view/42/",
    }
    for platform_id, target in targets.items():
        daemon = DaemonStub(
            lambda call: RawResult(
                data={"url": target, "title": "Example", "author": "a", "text": "body"}
            )
        )
        result = run_action(
            registry,
            DEFAULT_ROUTING,
            action="read",
            params={"target": target},
            ctx=FakeCtx(),
            platform=platform_id,
            adapters={"daemon": daemon, "http": HttpStub()},
        )
        assert result["ok"] is True, platform_id
        assert result["backend"] == "kimi-browser"
        assert result["items"][0]["text"] == "body"
        assert daemon.calls[0].args["url"] == target


def test_missing_daemon_degrades_browser_platforms_instead_of_failing_doctor():
    registry = _registry()
    report = build_doctor_report(
        registry,
        ctx=FakeCtx(),
        adapters={
            "daemon": DownDaemon(),
            "http": HttpStub(),
            "builtin": BuiltinAdapter(),
        },
    )
    rows = {row["id"]: row for row in report["platforms"]}
    for platform_id in ("twitter", "xueqiu", "xiaohongshu", "xiaoyuzhou", "linkedin"):
        assert rows[platform_id]["status"] == "degraded", platform_id
    assert report["errors"] == []
    assert report["meta"]["error_count"] == 0


def test_xiaoyuzhou_uses_http_sources_and_has_no_browser_dependency():
    registry = _registry()
    platform = registry.platforms["xiaoyuzhou"]
    adapters = {
        source.adapter
        for action in platform.actions.values()
        for source in action.sources
    }
    assert {"http", "daemon"} <= adapters
    assert platform.requires.get("adapters") == ["http"]
    assert all(
        source.when for action in platform.actions.values()
        for source in action.sources
        if source.adapter == "daemon"
    )


def test_platform_check_declarations_are_runnable():
    registry = _registry()
    for platform_id in ("twitter", "xueqiu", "xiaohongshu", "xiaoyuzhou", "linkedin"):
        check = registry.platforms[platform_id].check
        assert "search" in check, platform_id
        assert "query" in check["search"]
    # Xiaohongshu reads need a short-lived xsec_token, so only search is checked.
    assert "read" in registry.platforms["linkedin"].check
    assert "read" not in registry.platforms["xiaohongshu"].check


def test_browser_manifests_do_not_interpolate_user_text_into_javascript():
    for platform_id in BROWSER_PLATFORMS:
        text = (PLATFORMS / platform_id / "platform.yaml").read_text(encoding="utf-8")
        blocks = text.split("code: |")[1:]
        assert blocks
        for block in blocks:
            code = block.split("parse:")[0]
            assert "${" not in code, platform_id
