from __future__ import annotations

import json

import pytest

from agent_reach.adapters.base import RawResult, ToolCall
from agent_reach.adapters.daemon import (
    DaemonAdapter,
    discover_address,
    normalize_address,
)
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    PLATFORM_BLOCKED,
    TIMEOUT,
    ReachError,
)


class FakeLoopback:
    """Records daemon calls and replies through a handler."""

    timeout = 30

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def loopback_http(
        self,
        url,
        *,
        method="GET",
        headers=None,
        data=None,
        timeout=None,
        max_bytes=None,
    ):
        payload = json.loads(data) if data else None
        self.calls.append({"url": url, "method": method, "payload": payload})
        result = self.handler(url, payload)
        if isinstance(result, BaseException):
            raise result
        return result


def _ok(data):
    return RawResult(data={"ok": True, "data": data})


def _error(code, message):
    return RawResult(data={"ok": False, "error": {"code": code, "message": message}})


def _adapter(handler, tmp_path):
    return DaemonAdapter(context=FakeLoopback(handler), home=tmp_path, environ={})


def test_normalize_address_accepts_only_loopback():
    assert normalize_address("127.0.0.1:10086") == "http://127.0.0.1:10086"
    assert normalize_address("http://localhost:9000") == "http://localhost:9000"
    assert normalize_address("[::1]:1234") == "http://[::1]:1234"
    assert normalize_address("127.0.0.1") == "http://127.0.0.1:10086"

    for bad in (
        "http://example.com:10086",
        "0.0.0.0:10086",
        "http://user:pass@127.0.0.1:10086",
        "http://127.0.0.1:10086/path",
        "ftp://127.0.0.1:21",
    ):
        with pytest.raises(ValueError):
            normalize_address(bad)


def test_discover_address_precedence(tmp_path):
    assert (
        discover_address(
            home=tmp_path,
            environ={"KIMI_WEBBRIDGE_ADDR": "http://127.0.0.1:9001"},
        )
        == "http://127.0.0.1:9001"
    )

    directory = tmp_path / ".kimi-webbridge"
    directory.mkdir()
    (directory / "daemon.addr").write_text("127.0.0.1:9999\n", encoding="utf-8")
    assert discover_address(home=tmp_path, environ={}) == "http://127.0.0.1:9999"

    (directory / "daemon.addr").write_text("10.0.0.1:9999\n", encoding="utf-8")
    (directory / "config.json").write_text('{"addr": "127.0.0.1:7777"}', encoding="utf-8")
    assert discover_address(home=tmp_path, environ={}) == "http://127.0.0.1:7777"

    (directory / "config.json").write_text('{"addr": "example.com:7777"}', encoding="utf-8")
    assert discover_address(home=tmp_path, environ={}) == "http://127.0.0.1:10086"


def test_probe_reports_running_daemon_and_extension(tmp_path):
    def healthy(url, payload):
        assert url.endswith("/status")
        return RawResult(
            data={"running": True, "version": "v2.0.23", "extension_connected": True}
        )

    health = _adapter(healthy, tmp_path).probe()
    assert health.ok is True
    assert health.version == "v2.0.23"
    assert "v2.0.23" in health.detail

    def no_extension(url, payload):
        return RawResult(data={"running": True, "extension_connected": False})

    health = _adapter(no_extension, tmp_path).probe()
    assert health.ok is False
    assert "extension" in health.detail


def test_probe_is_offline_when_daemon_is_down(tmp_path):
    def down(url, payload):
        raise ReachError(NEED_SETUP, "cannot reach the local daemon at 127.0.0.1:1")

    health = _adapter(down, tmp_path).probe()
    assert health.ok is False
    assert "not reachable" in health.detail


def test_invoke_passes_through_a_snapshot_command(tmp_path):
    def handler(url, payload):
        assert payload["action"] == "list_tabs"
        assert payload["session"] == "reach-web"
        return _ok({"success": True, "tabs": []})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        ToolCall(adapter="daemon", call="list_tabs", args={}, raw={"platform_id": "web"}),
        ctx=adapter._context,
    )
    assert result.data == {"success": True, "tabs": []}
    assert result.meta["session"] == "reach-web"


def test_daemon_read_navigates_and_pages_until_budget(tmp_path):
    snapshots = []

    def handler(url, payload):
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/", "tabId": 7})
        assert payload["action"] == "snapshot"
        snapshots.append(payload["args"])
        if "cursor" not in payload["args"]:
            return _ok(
                {
                    "url": "https://example.com/",
                    "title": "Example",
                    "tree_text": "A" * 300,
                    "truncated": True,
                    "next_cursor": "c1",
                }
            )
        return _ok(
            {
                "url": "https://example.com/",
                "title": "Example",
                "tree_text": "B" * 200,
                "truncated": False,
            }
        )

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        ToolCall(
            adapter="daemon",
            call="read",
            args={"url": "https://example.com/", "max_chars": 400},
            params={"target": "https://example.com/"},
            raw={"platform_id": "web"},
        ),
        ctx=adapter._context,
    )
    data = result.data
    assert data["title"] == "Example"
    assert data["url"] == "https://example.com/"
    assert data["pages"] == 2
    assert data["text"] == "A" * 300 + "B" * 100
    assert data["truncated"] is True
    assert data["truncated_reason"] == "max_chars"
    assert data["tab_id"] == 7
    assert snapshots[1]["cursor"] == "c1"
    assert snapshots[0]["format"] == "aria"


def test_daemon_read_stops_cleanly_on_expired_cursor(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/"})
        if "cursor" not in payload["args"]:
            return _ok(
                {
                    "url": "https://example.com/",
                    "tree_text": "A" * 300,
                    "truncated": True,
                    "next_cursor": "c1",
                }
            )
        return _error("extension_error", "snapshot: unknown or expired cursor")

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        ToolCall(
            adapter="daemon",
            call="read",
            args={"url": "https://example.com/"},
            raw={"platform_id": "web"},
        ),
        ctx=adapter._context,
    )
    assert result.data["text"] == "A" * 300
    assert result.data["truncated"] is True
    assert result.data["truncated_reason"] == "cursor_expired"


def test_daemon_extension_missing_maps_to_setup_error(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/"})
        return _error("extension_not_connected", "open the extension")

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="read",
                args={"url": "https://example.com/"},
                raw={"platform_id": "web"},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert "extension" in info.value.message


def test_daemon_read_rejects_private_targets(tmp_path):
    adapter = _adapter(lambda url, payload: _ok({}), tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="read",
                args={"url": "http://127.0.0.1/secret"},
                raw={"platform_id": "web"},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == BAD_INPUT


def _browse_call(**args):
    base = {"url": "https://example.com/search?q=x", "code": "return []"}
    base.update(args)
    return ToolCall(
        adapter="daemon",
        call="browse",
        args=base,
        raw={"platform_id": "example", "platform_trusted": True},
    )


def test_daemon_browse_navigates_waits_and_decodes_json(tmp_path):
    evaluations = []

    def handler(url, payload):
        action = payload["action"]
        if action == "navigate":
            return _ok(
                {"success": True, "url": "https://example.com/search?q=x", "tabId": 9}
            )
        assert action == "evaluate"
        code = payload["args"]["code"]
        evaluations.append(code)
        if "location.href" in code:
            return _ok(
                {
                    "type": "string",
                    "value": json.dumps(
                        {"url": "https://example.com/search?q=x", "title": "Example"}
                    ),
                }
            )
        if "querySelector" in code:
            return _ok({"type": "boolean", "value": len(evaluations) > 2})
        return _ok(
            {
                "type": "string",
                "value": json.dumps(
                    [
                        {"title": "One", "url": "https://example.com/1", "text": "a"},
                        {"title": "Two", "url": "https://example.com/2", "text": "b"},
                        {"title": "Three", "url": "https://example.com/3", "text": "c"},
                    ]
                ),
            }
        )

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(wait_for=".result", limit=2),
        ctx=adapter._context,
    )
    assert [item["title"] for item in result.data] == ["One", "Two"]
    assert result.meta["page_url"] == "https://example.com/search?q=x"
    assert result.meta["page_title"] == "Example"
    assert result.meta["tab_id"] == 9
    assert result.meta["wait_timed_out"] is False
    assert "location.href" in evaluations[0]
    assert evaluations[1] == '!!document.querySelector(".result")'
    assert "querySelector" not in evaluations[-1]
    assert '"limit"' not in evaluations[-1]


def test_daemon_browse_maps_login_redirect_without_evaluating(tmp_path):
    evaluations = []

    def handler(url, payload):
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/login?next=/"})
        evaluations.append(payload["args"]["code"])
        return _ok(
            {
                "type": "string",
                "value": json.dumps(
                    {"url": "https://example.com/login?next=/", "title": "Sign in"}
                ),
            }
        )

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            _browse_call(login_url="/login"),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_LOGIN
    assert len(evaluations) == 1


def test_daemon_browse_maps_structured_login_and_blocked_markers(tmp_path):
    def reply(value):
        def handler(url, payload):
            if payload["action"] == "navigate":
                return _ok({"success": True, "url": "https://example.com/"})
            return _ok({"type": "object", "value": value})

        return handler

    for value, code in (
        ({"login": True, "message": "sign in"}, NEED_LOGIN),
        ({"blocked": True, "message": "captcha"}, PLATFORM_BLOCKED),
    ):
        adapter = _adapter(reply(value), tmp_path)
        with pytest.raises(ReachError) as info:
            adapter.invoke(_browse_call(), ctx=adapter._context)
        assert info.value.code == code
        assert value["message"] in info.value.message


def test_daemon_browse_recovers_a_tab_after_navigation_timeout(tmp_path):
    calls = []

    def handler(url, payload):
        action = payload["action"]
        calls.append(action)
        if action == "navigate":
            return ReachError(TIMEOUT, "navigate: page load timeout (30s)")
        if action == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if action == "list_browser_tabs":
            return _ok(
                {
                    "tabs": [
                        {"tabId": 3, "url": "https://example.com/unrelated"},
                        {
                            "tabId": 7,
                            "url": "https://example.com/search?q=x",
                            "groupTitle": "reach: test",
                        },
                    ]
                }
            )
        if action == "attach_tab":
            assert payload["args"]["tabId"] == 7
            return _ok({"success": True, "tabId": 7, "url": "https://example.com/search?q=x"})
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "type": "string",
                    "value": json.dumps(
                        {"url": "https://example.com/search?q=x", "title": "Example"}
                    ),
                }
            )
        return _ok(
            {
                "type": "string",
                "value": json.dumps([{"title": "One", "url": "https://example.com/1"}]),
            }
        )

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(group_title="reach: test", wait_for=None),
        ctx=adapter._context,
    )
    assert result.data == [{"title": "One", "url": "https://example.com/1"}]
    assert result.meta["nav_timed_out"] is True
    assert result.meta["nav_recovered"] is True
    assert result.meta["tab_id"] == 7
    assert calls[:4] == ["navigate", "list_tabs", "list_browser_tabs", "attach_tab"]


def test_daemon_browse_reuses_a_session_tab_after_navigation_timeout(tmp_path):
    calls = []

    def handler(url, payload):
        action = payload["action"]
        calls.append(action)
        if action == "navigate":
            return ReachError(TIMEOUT, "navigate: page load timeout (30s)")
        if action == "list_tabs":
            return _ok(
                {
                    "success": True,
                    "tabs": [
                        {"tabId": 4, "url": "https://example.com/"},
                        {
                            "tabId": 9,
                            "url": "https://example.com/",
                            "groupTitle": "reach: test",
                        },
                    ],
                }
            )
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "value": json.dumps(
                        {"url": "https://example.com/", "title": "Example"}
                    )
                }
            )
        return _ok({"value": json.dumps({"title": "One", "url": "https://example.com/1"})})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(group_title="reach: test"), ctx=adapter._context
    )
    assert result.data == {"title": "One", "url": "https://example.com/1"}
    assert result.meta["tab_id"] == 9
    assert "list_browser_tabs" not in calls


def test_daemon_browse_fails_when_a_timed_out_tab_cannot_be_found(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return ReachError(TIMEOUT, "navigate: page load timeout (30s)")
        if payload["action"] == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if payload["action"] == "list_browser_tabs":
            return _ok({"tabs": [{"tabId": 3, "url": "https://example.com/unrelated"}]})
        raise AssertionError("browse should stop before evaluating")

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            _browse_call(group_title="reach: test", wait_for=None),
            ctx=adapter._context,
        )
    assert info.value.code == TIMEOUT


def test_daemon_browse_requires_a_trusted_platform(tmp_path):
    calls = []

    def handler(url, payload):
        calls.append(payload)
        return _ok({})

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                args={"url": "https://example.com/", "code": "return []"},
                raw={"platform_id": "example", "platform_trusted": False},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert calls == []


def test_daemon_browse_rejects_private_targets(tmp_path):
    adapter = _adapter(lambda url, payload: _ok({}), tmp_path)
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            _browse_call(url="http://127.0.0.1/secret"),
            ctx=adapter._context,
        )
    assert info.value.code == BAD_INPUT


def test_daemon_address_override_cannot_leave_loopback(tmp_path):
    adapter = DaemonAdapter(
        context=FakeLoopback(lambda url, payload: _ok({})),
        home=tmp_path,
        environ={"KIMI_WEBBRIDGE_ADDR": "http://10.0.0.5:10086"},
    )
    health = adapter.probe()
    assert health.ok is False
    assert "non-loopback" in health.detail
    with pytest.raises(ReachError) as info:
        adapter.invoke(
            ToolCall(adapter="daemon", call="list_tabs", args={}, raw={}),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
