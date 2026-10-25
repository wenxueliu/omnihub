from __future__ import annotations

import pytest

from agent_reach.adapters.base import Health, RawResult
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    TIMEOUT,
    ReachError,
)


class FakeAdapter:
    kind = "fake"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.params = []

    def probe(self):
        return Health(ok=True)

    def invoke(self, call, *, ctx):
        self.calls.append(call.call)
        self.params.append(dict(call.params))
        value = self.responses[call.call]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, RawResult):
            return value
        return RawResult(data=value)


class FakeCtx:
    timeout = 30

    def secret(self, name):
        return None


def _search_action(sources):
    return {
        "search": {
            "params": {"query": {"type": "str", "required": True}},
            "sources": sources,
        }
    }


def _read_action(sources, *, url_format=False):
    target = {"type": "str", "required": True}
    if url_format:
        target["format"] = "url"
    return {"read": {"params": {"target": target}, "sources": sources}}


def _source(call, priority=10, parse="json_items"):
    return {
        "adapter": "builtin",
        "call": call,
        "parse": parse,
        "priority": priority,
    }


def _routing(mode="first_success", top=None):
    routing = {"version": 1, "mode": mode}
    if top is not None:
        routing["aggregate"] = {"top": top}
    return routing


def _registry(make_platform, tmp_path, platforms):
    root = tmp_path / "plugins"
    for platform_id, data in platforms.items():
        make_platform(root, platform_id, **data)
    return discover(plugin_dirs=[root], include_builtin=False)


def test_first_success_falls_through_sources(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "priority": 10,
                "actions": _search_action(
                    [
                        _source("slow", priority=10),
                        _source("ok", priority=20),
                    ]
                ),
            },
            "b": {
                "priority": 20,
                "actions": _search_action([_source("b_ok")]),
            },
        },
    )
    adapter = FakeAdapter(
        {
            "slow": ReachError(TIMEOUT, "slow"),
            "ok": [{"title": "A", "url": "https://a.example"}],
            "b_ok": [{"title": "B"}],
        }
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert result["channel"] == "a"
    assert result["backend"] == "ok"
    assert [item["title"] for item in result["items"]] == ["A"]
    assert [attempt["result"] for attempt in result["meta"]["attempts"]] == ["ERROR", "OK"]
    assert adapter.calls == ["slow", "ok"]


def test_final_code_stops_platform_fallback(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("login")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {
            "login": ReachError(NEED_LOGIN, "sign in"),
            "b_ok": [{"title": "B"}],
        }
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == NEED_LOGIN
    assert adapter.calls == ["login"]


def test_empty_result_falls_through_platforms(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("empty")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {
            "empty": [],
            "b_ok": [{"title": "B"}],
        }
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert result["channel"] == "b"
    assert result["meta"]["attempts"][0]["result"] == "EMPTY"


def test_provenance_is_attached(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {"a": {"priority": 10, "actions": _search_action([_source("ok", priority=7)])}},
    )
    adapter = FakeAdapter({"ok": [{"title": "A", "url": "https://a.example"}]})
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    item = result["items"][0]
    assert item["platform"] == "a"
    assert item["source"] == "ok"
    assert item["priority"] == 7


def test_aggregate_merges_and_dedupes(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("a_ok")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {
            "a_ok": [{"title": "X", "url": "https://x", "text": "a"}],
            "b_ok": [
                {"title": "X again", "url": "https://x", "text": "b"},
                {"title": "Y", "url": "https://y", "text": "c"},
            ],
        }
    )
    result = run_action(
        registry,
        _routing(mode="aggregate", top=2),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert result["channel"] == "@aggregate"
    assert [item["title"] for item in result["items"]] == ["X", "Y"]
    assert result["meta"]["platforms"] == ["a", "b"]


def test_aggregate_top_limits_platforms(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("a_ok")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter({"a_ok": [{"title": "A"}], "b_ok": [{"title": "B"}]})
    result = run_action(
        registry,
        _routing(mode="aggregate", top=1),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert [item["title"] for item in result["items"]] == ["A"]
    assert adapter.calls == ["a_ok"]


def test_read_falls_back_to_web_platform(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "site": {
                "priority": 10,
                "domains": ["example.com"],
                "actions": _read_action([_source("site_read", parse="json")]),
            },
            "web": {
                "priority": 900,
                "actions": _read_action([_source("web_read", parse="json")]),
            },
        },
    )
    adapter = FakeAdapter(
        {
            "site_read": ReachError(TIMEOUT, "site down"),
            "web_read": [{"title": "W", "url": "https://example.com/a", "text": "body"}],
        }
    )
    result = run_action(
        registry,
        _routing(),
        action="read",
        params={"target": "https://example.com/a"},
        url="https://example.com/a",
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert result["channel"] == "web"
    assert adapter.calls == ["site_read", "web_read"]


def test_missing_required_parameter_is_bad_input(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {"a": {"actions": _search_action([_source("ok")])}},
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={},
        ctx=FakeCtx(),
        adapters={"builtin": FakeAdapter({"ok": []})},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == BAD_INPUT


def test_url_format_parameter_rejects_private_target(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": _read_action([_source("ok", parse="json")], url_format=True)
            }
        },
    )
    result = run_action(
        registry,
        _routing(),
        action="read",
        params={"target": "file:///etc/passwd"},
        ctx=FakeCtx(),
        adapters={"builtin": FakeAdapter({"ok": []})},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == BAD_INPUT
    assert result["meta"]["attempts"] == []


def test_unknown_platform_is_bad_input(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {"a": {"actions": _search_action([_source("ok")])}},
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        platform="nope",
        ctx=FakeCtx(),
        adapters={"builtin": FakeAdapter({"ok": []})},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == BAD_INPUT


def test_each_platform_applies_its_own_defaults(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "priority": 10,
                "actions": {
                    "search": {
                        "params": {
                            "query": {"type": "str", "required": True},
                            "limit": {"type": "int", "default": 1},
                        },
                        "sources": [_source("a_fail")],
                    }
                },
            },
            "b": {
                "priority": 20,
                "actions": {
                    "search": {
                        "params": {
                            "query": {"type": "str", "required": True},
                            "limit": {"type": "int", "default": 2},
                        },
                        "sources": [_source("b_ok")],
                    }
                },
            },
        },
    )
    adapter = FakeAdapter(
        {"a_fail": ReachError(TIMEOUT, "slow"), "b_ok": [{"title": "B"}]}
    )
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert adapter.params[1]["limit"] == 2


def test_aggregate_honours_per_platform_limit(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("a_ok")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {
            "a_ok": [{"title": "A1", "url": "https://a1"}, {"title": "A2", "url": "https://a2"}],
            "b_ok": [{"title": "B1", "url": "https://b1"}, {"title": "B2", "url": "https://b2"}],
        }
    )
    routing = _routing(mode="aggregate", top=2)
    routing["aggregate"]["per_platform_limit"] = 1
    result = run_action(
        registry,
        routing,
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert [item["title"] for item in result["items"]] == ["A1", "B1"]
    assert result["meta"]["per_platform_limit"] == 1


def test_aggregate_can_disable_dedupe(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("a_ok")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {
            "a_ok": [{"title": "A", "url": "https://same"}],
            "b_ok": [{"title": "B", "url": "https://same"}],
        }
    )
    routing = _routing(mode="aggregate", top=2)
    routing["aggregate"]["dedupe"] = False
    result = run_action(
        registry,
        routing,
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert [item["title"] for item in result["items"]] == ["A", "B"]
    assert result["meta"]["dedupe"] is False
