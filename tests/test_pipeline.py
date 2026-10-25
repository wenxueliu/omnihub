from __future__ import annotations

import pytest

from agent_reach.adapters.base import Health, RawResult
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.ratelimit import RateLimits
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
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

    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})

    def secret(self, name):
        return self.secrets.get(name)


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


def _source(call, priority=10, parse="json_items", when=None):
    source = {
        "adapter": "builtin",
        "call": call,
        "parse": parse,
        "priority": priority,
    }
    if when:
        source["when"] = when
    return source


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


def test_parameter_range_is_enforced(make_platform, tmp_path):
    routing = _routing()
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": {
                    "search": {
                        "params": {
                            "query": {"type": "str", "required": True},
                            "limit": {"type": "int", "default": 5, "minimum": 1, "maximum": 3},
                        },
                        "sources": [_source("ok")],
                    }
                }
            }
        },
    )
    adapter = FakeAdapter({"ok": [{"title": "A"}]})
    for value in (0, 4):
        result = run_action(
            registry,
            routing,
            action="search",
            params={"query": "x", "limit": value},
            ctx=FakeCtx(),
            adapters={"builtin": adapter},
        )
        assert result["ok"] is False
        assert result["error"]["code"] == BAD_INPUT
    result = run_action(
        registry,
        routing,
        action="search",
        params={"query": "x", "limit": 3},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True


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


def test_source_when_secret_skips_without_secret(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "priority": 10,
                "actions": _search_action(
                    [_source("token", when="secret:DEMO_TOKEN")]
                ),
            },
            "b": {
                "priority": 20,
                "actions": _search_action([_source("b_ok")]),
            },
        },
    )
    adapter = FakeAdapter({"token": [{"title": "T"}], "b_ok": [{"title": "B"}]})
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
    assert adapter.calls == ["b_ok"]
    assert result["meta"]["attempts"][0]["result"] == "SKIP"
    assert "DEMO_TOKEN" in result["meta"]["attempts"][0]["reason"]


def test_source_when_secret_runs_with_secret(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": _search_action(
                    [_source("token", when="secret:DEMO_TOKEN")]
                )
            }
        },
    )
    adapter = FakeAdapter({"token": [{"title": "T"}]})
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx({"DEMO_TOKEN": "value"}),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert adapter.calls == ["token"]


def test_source_when_tool_skips_missing_executable(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": _search_action(
                    [_source("cli", when="tool:definitely-not-a-real-tool-xyz")]
                )
            }
        },
    )
    adapter = FakeAdapter({"cli": [{"title": "T"}]})
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is False
    assert adapter.calls == []
    assert result["meta"]["attempts"][0]["result"] == "SKIP"


def test_source_when_adapter_skips_an_unhealthy_adapter(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": _search_action(
                    [
                        {"adapter": "daemon", "call": "read", "when": "adapter:daemon"},
                        _source("ok", priority=20),
                    ]
                )
            }
        },
    )

    class UnhealthyDaemon:
        kind = "daemon"

        def probe(self):
            return Health(ok=False, detail="kimi-webbridge is not running")

        def invoke(self, call, *, ctx):
            raise AssertionError("a skipped source must not be invoked")

    adapter = FakeAdapter({"ok": [{"title": "ok"}]})
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter, "daemon": UnhealthyDaemon()},
    )
    assert result["ok"] is True
    assert result["backend"] == "ok"
    attempts = result["meta"]["attempts"]
    assert attempts[0]["result"] == "SKIP"
    assert "not running" in attempts[0]["reason"]


def test_source_when_rejects_unknown_condition(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "actions": _search_action(
                    [_source("bad", when="moon:phase"), _source("ok", priority=20)]
                )
            }
        },
    )
    adapter = FakeAdapter({"bad": [{"title": "bad"}], "ok": [{"title": "ok"}]})
    result = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert result["ok"] is True
    assert adapter.calls == ["ok"]
    assert result["meta"]["attempts"][0]["code"] == "NEED_SETUP"


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


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_rate_limit_cools_the_platform_and_stops_fallback(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {"a": {"priority": 10, "actions": _search_action([_source("limited")])}},
    )
    adapter = FakeAdapter(
        {"limited": ReachError(RATE_LIMITED, "HTTP 429", retry_after_seconds=30)}
    )
    first = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert first["ok"] is False
    assert first["error"]["code"] == RATE_LIMITED
    assert first["error"]["retry_after_seconds"] == 30
    assert adapter.calls == ["limited"]

    second = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert second["error"]["code"] == RATE_LIMITED
    assert 29 < second["error"]["retry_after_seconds"] <= 30
    assert adapter.calls == ["limited"]
    attempt = second["meta"]["attempts"][0]
    assert attempt["result"] == "SKIP"
    assert "platform cooldown" in attempt["reason"]
    assert 29 < attempt["retry_after_seconds"] <= 30


def test_backend_pause_keeps_other_sources_available(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {
                "priority": 10,
                "actions": _search_action(
                    [_source("broken", priority=10), _source("ok", priority=20)]
                ),
            }
        },
    )
    adapter = FakeAdapter(
        {
            "broken": ReachError(UPSTREAM_BROKEN, "boom"),
            "ok": [{"title": "A"}],
        }
    )
    first = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert first["ok"] is True
    assert first["backend"] == "ok"

    second = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert second["ok"] is True
    assert adapter.calls == ["broken", "ok", "ok"]
    assert [attempt["result"] for attempt in second["meta"]["attempts"]] == [
        "SKIP",
        "OK",
    ]
    assert 119 < second["meta"]["attempts"][0]["retry_after_seconds"] <= 120


def test_backend_pause_does_not_stop_platform_fallback(make_platform, tmp_path):
    registry = _registry(
        make_platform,
        tmp_path,
        {
            "a": {"priority": 10, "actions": _search_action([_source("a_timeout")])},
            "b": {"priority": 20, "actions": _search_action([_source("b_ok")])},
        },
    )
    adapter = FakeAdapter(
        {"a_timeout": ReachError(TIMEOUT, "slow"), "b_ok": [{"title": "B"}]}
    )
    first = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert first["ok"] is True
    assert first["channel"] == "b"
    assert adapter.calls == ["a_timeout", "b_ok"]

    second = run_action(
        registry,
        _routing(),
        action="search",
        params={"query": "x"},
        ctx=FakeCtx(),
        adapters={"builtin": adapter},
    )
    assert second["ok"] is True
    assert second["channel"] == "b"
    assert adapter.calls == ["a_timeout", "b_ok", "b_ok"]
    assert second["meta"]["attempts"][0]["result"] == "SKIP"
    assert 59 < second["meta"]["attempts"][0]["retry_after_seconds"] <= 60


def test_min_interval_paces_repeat_calls(make_platform, tmp_path):
    source = _source("ok")
    source["min_interval"] = 2
    registry = _registry(
        make_platform,
        tmp_path,
        {"a": {"priority": 10, "actions": _search_action([source])}},
    )
    adapter = FakeAdapter({"ok": [{"title": "A"}]})
    clock = FakeClock()
    sleeps: list[float] = []
    limiter = RateLimits(
        tmp_path / "state" / "rate_limits.json", now=clock, sleep=sleeps.append
    )

    def run():
        return run_action(
            registry,
            _routing(),
            action="search",
            params={"query": "x"},
            ctx=FakeCtx(),
            adapters={"builtin": adapter},
            rate_limits=limiter,
        )

    assert run()["ok"] is True
    assert sleeps == []
    clock.advance(0.5)
    assert run()["ok"] is True
    assert sleeps == [1.5]
