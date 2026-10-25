from __future__ import annotations

import copy

import pytest

from omnihub.core.config import DEFAULT_ROUTING
from omnihub.core.registry import discover
from omnihub.core.router import RoutingError, build_plan, resolve_sources


@pytest.fixture
def registry(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "a", priority=20, domains=["a.example"])
    make_platform(root, "b", priority=10, domains=["b.example"])
    make_platform(root, "c", priority=5, domains=["c.example"])
    return discover(plugin_dirs=[root], include_builtin=False)


@pytest.fixture
def routing():
    config = copy.deepcopy(DEFAULT_ROUTING)
    config["platforms"] = {"c": {"enabled": False}}
    return config


def test_default_order_uses_priority_and_skips_disabled(registry, routing):
    plan = build_plan(registry, routing, action="search")
    assert plan.platform_ids == ("b", "a")
    assert plan.mode == "first_success"
    assert plan.top is None


def test_routing_priority_overrides_manifest(registry, routing):
    routing["platforms"]["a"] = {"priority": 1}
    plan = build_plan(registry, routing, action="search")
    assert plan.platform_ids == ("a", "b")


def test_intent_keeps_configured_order_and_reports_skips(registry, routing):
    routing["intents"] = {"code": ["a", "missing", "c", "b"]}
    plan = build_plan(registry, routing, intent="code", action="search")
    assert plan.platform_ids == ("a", "b")
    assert plan.intent == "code"
    assert len(plan.warnings) == 2


def test_unknown_intent_is_rejected(registry, routing):
    with pytest.raises(RoutingError, match="unknown intent"):
        build_plan(registry, routing, intent="nope")


def test_explicit_platform_wins(registry, routing):
    plan = build_plan(registry, routing, platform="a", action="search")
    assert plan.platform_ids == ("a",)
    assert plan.reason == "explicit platform"


def test_explicit_disabled_platform_is_rejected(registry, routing):
    with pytest.raises(RoutingError, match="disabled"):
        build_plan(registry, routing, platform="c")


def test_explicit_unknown_platform_is_rejected(registry, routing):
    with pytest.raises(RoutingError, match="unknown platform"):
        build_plan(registry, routing, platform="nope")


def test_action_support_is_enforced(registry, routing):
    with pytest.raises(RoutingError, match="does not support"):
        build_plan(registry, routing, platform="a", action="read")


def test_url_domain_match_precedes_intent(registry, routing):
    routing["intents"] = {"code": ["a"]}
    plan = build_plan(registry, routing, url="https://b.example/x", action="search")
    assert plan.platform_ids == ("b",)
    assert "url domain" in plan.reason


def test_unknown_url_domain_falls_back_with_warning(registry, routing):
    plan = build_plan(registry, routing, url="https://nope.test/x", action="search")
    assert plan.platform_ids == ("b", "a")
    assert plan.warnings


def test_aggregate_mode_reads_top(registry, routing):
    routing["mode"] = "aggregate"
    routing["aggregate"]["top"] = 2
    plan = build_plan(registry, routing, action="search")
    assert plan.mode == "aggregate"
    assert plan.top == 2


def test_cli_mode_override_wins(registry, routing):
    routing["mode"] = "aggregate"
    plan = build_plan(registry, routing, action="search", mode="first_success")
    assert plan.mode == "first_success"
    assert plan.top is None


def test_no_enabled_platform_is_an_error(registry, routing):
    routing["platforms"] = {platform_id: {"enabled": False} for platform_id in registry.platforms}
    with pytest.raises(RoutingError, match="no enabled platform"):
        build_plan(registry, routing, action="search")


def test_source_order_and_disable(make_platform, tmp_path):
    root = tmp_path / "plugins"
    actions = {
        "search": {
            "sources": [
                {"adapter": "builtin", "call": "late", "parse": "json", "priority": 30},
                {"adapter": "builtin", "call": "off", "parse": "json", "priority": 5, "disabled": True},
                {"adapter": "builtin", "call": "early", "parse": "json", "priority": 10},
                {"adapter": "builtin", "call": "middle", "parse": "json", "priority": 20},
            ]
        }
    }
    make_platform(root, "sources", actions=actions)
    registry = discover(plugin_dirs=[root], include_builtin=False)
    sources = resolve_sources(registry.platforms["sources"], "search")
    assert [source.call for source in sources] == ["early", "middle", "late"]
