from __future__ import annotations

import json

import pytest

from omnihub.adapters.base import RawResult, ToolCall
from omnihub.adapters.daemon import (
    DaemonAdapter,
    discover_address,
    normalize_address,
)
from omnihub.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    PLATFORM_BLOCKED,
    TIMEOUT,
    OmniHubError,
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


def _closed(count=1):
    return _ok({"success": True, "closed": count})


def _actions(ctx):
    return [call["payload"]["action"] for call in ctx.calls]


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
        raise OmniHubError(NEED_SETUP, "cannot reach the local daemon at 127.0.0.1:1")

    health = _adapter(down, tmp_path).probe()
    assert health.ok is False
    assert "not reachable" in health.detail


def test_invoke_passes_through_a_snapshot_command(tmp_path):
    def handler(url, payload):
        assert payload["action"] == "list_tabs"
        assert payload["session"] == "omnihub-web"
        return _ok({"success": True, "tabs": []})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        ToolCall(adapter="daemon", call="list_tabs", args={}, raw={"platform_id": "web"}),
        ctx=adapter._context,
    )
    assert result.data == {"success": True, "tabs": []}
    assert result.meta["session"] == "omnihub-web"
    assert _actions(adapter._context) == ["list_tabs"]


def test_daemon_read_navigates_and_pages_until_budget(tmp_path):
    snapshots = []

    def handler(url, payload):
        if payload["action"] == "close_session":
            return _closed()
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
    assert _actions(adapter._context)[-1] == "close_session"
    sessions = {call["payload"]["session"] for call in adapter._context.calls}
    assert len(sessions) == 1
    assert next(iter(sessions)).startswith("omnihub-web-")


def test_daemon_read_stops_cleanly_on_expired_cursor(tmp_path):
    def handler(url, payload):
        if payload["action"] == "close_session":
            return _closed()
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
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_extension_missing_maps_to_setup_error(tmp_path):
    def handler(url, payload):
        if payload["action"] == "close_session":
            return _closed()
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/"})
        return _error("extension_not_connected", "open the extension")

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
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
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_read_rejects_private_targets(tmp_path):
    def handler(url, payload):
        return _closed() if payload["action"] == "close_session" else _ok({})

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
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
    assert _actions(adapter._context) == ["close_session"]


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
        if action == "close_session":
            return _closed()
        if action == "navigate":
            return _ok(
                {"success": True, "url": "https://example.com/search?q=x", "tabId": 9}
            )
        if action == "list_browser_tabs":
            return _ok({"tabs": []})
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
    assert _actions(adapter._context)[-1] == "close_session"
    sessions = {call["payload"]["session"] for call in adapter._context.calls}
    assert len(sessions) == 1
    assert next(iter(sessions)).startswith("omnihub-example-")


def test_daemon_browse_warms_up_a_tab_before_a_user_agent_override(tmp_path):
    calls = []
    user_agent = {
        "userAgent": "Mobile",
        "acceptLanguage": "zh-CN",
        "platform": "iPhone",
    }

    def handler(url, payload):
        action = payload["action"]
        calls.append((action, payload.get("args") or {}))
        if action == "close_session":
            return _closed()
        if action == "list_tabs":
            return _ok({"tabs": []})
        if action == "list_browser_tabs":
            return _ok({"tabs": []})
        if action == "navigate":
            args = payload.get("args") or {}
            return _ok({"success": True, "url": args["url"], "tabId": 12})
        if action == "cdp":
            return _ok({})
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "value": json.dumps(
                        {"url": "https://example.com/article", "title": "Article"}
                    )
                }
            )
        return _ok({"value": json.dumps([{"title": "One", "url": "https://example.com/1"}])})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(url="https://example.com/article", user_agent=user_agent),
        ctx=adapter._context,
    )
    assert result.data == [{"title": "One", "url": "https://example.com/1"}]
    assert result.meta["ua_override_failed"] is False
    navigations = [args for action, args in calls if action == "navigate"]
    assert [args["url"] for args in navigations] == [
        "https://example.com/",
        "https://example.com/article",
    ]
    assert [args["newTab"] for args in navigations] == [True, False]
    # The override is applied before the real target loads, so the site never
    # sees the desktop user agent.
    actions = [action for action, _ in calls]
    assert actions.index("cdp") < actions.index("navigate", actions.index("navigate") + 1)
    cdp_calls = [args for action, args in calls if action == "cdp"]
    assert cdp_calls[0]["method"] == "Emulation.setUserAgentOverride"
    assert cdp_calls[0]["params"] == user_agent


def test_daemon_browse_reuses_an_existing_session_tab_for_the_override(tmp_path):
    calls = []

    def handler(url, payload):
        action = payload["action"]
        calls.append((action, payload.get("args") or {}))
        if action == "close_session":
            return _closed()
        if action in ("list_tabs", "list_browser_tabs"):
            tabs = [{"tabId": 12, "url": "https://example.com/article"}]
            return _ok({"tabs": tabs})
        if action == "navigate":
            return _ok(
                {"success": True, "url": "https://example.com/article", "tabId": 12}
            )
        if action == "cdp":
            return _ok({})
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "value": json.dumps(
                        {"url": "https://example.com/article", "title": "Article"}
                    )
                }
            )
        return _ok({"value": json.dumps([{"title": "One", "url": "https://example.com/1"}])})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(url="https://example.com/article", user_agent={"userAgent": "Mobile"}),
        ctx=adapter._context,
    )
    assert result.data == [{"title": "One", "url": "https://example.com/1"}]
    navigations = [args for action, args in calls if action == "navigate"]
    assert [args["url"] for args in navigations] == ["https://example.com/article"]
    assert navigations[0]["newTab"] is False


def test_daemon_browse_keeps_the_normal_path_when_cdp_is_unavailable(tmp_path):
    def handler(url, payload):
        action = payload["action"]
        if action == "close_session":
            return _closed()
        if action == "list_tabs":
            return _ok({"tabs": []})
        if action == "list_browser_tabs":
            return _ok({"tabs": []})
        if action == "navigate":
            return _ok(
                {"success": True, "url": "https://example.com/article", "tabId": 12}
            )
        if action == "cdp":
            return _error("tool_error", "cdp is not supported")
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "value": json.dumps(
                        {"url": "https://example.com/article", "title": "Article"}
                    )
                }
            )
        return _ok({"value": json.dumps([{"title": "One", "url": "https://example.com/1"}])})

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(url="https://example.com/article", user_agent={"userAgent": "Mobile"}),
        ctx=adapter._context,
    )
    assert result.data == [{"title": "One", "url": "https://example.com/1"}]
    assert result.meta["ua_override_failed"] is True


def test_daemon_browse_maps_login_redirect_without_evaluating(tmp_path):
    evaluations = []

    def handler(url, payload):
        if payload["action"] == "close_session":
            return _closed()
        if payload["action"] == "list_browser_tabs":
            return _ok({"tabs": []})
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
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            _browse_call(login_url="/login"),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_LOGIN
    assert len(evaluations) == 1
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_browse_maps_structured_login_and_blocked_markers(tmp_path):
    def reply(value):
        def handler(url, payload):
            if payload["action"] == "close_session":
                return _closed()
            if payload["action"] == "navigate":
                return _ok({"success": True, "url": "https://example.com/"})
            return _ok({"type": "object", "value": value})

        return handler

    for value, code in (
        ({"login": True, "message": "sign in"}, NEED_LOGIN),
        ({"blocked": True, "message": "captcha"}, PLATFORM_BLOCKED),
    ):
        adapter = _adapter(reply(value), tmp_path)
        with pytest.raises(OmniHubError) as info:
            adapter.invoke(_browse_call(), ctx=adapter._context)
        assert info.value.code == code
        assert value["message"] in info.value.message
        assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_browse_recovers_a_tab_after_navigation_timeout(tmp_path):
    calls = []
    tab_lists = 0

    def handler(url, payload):
        nonlocal tab_lists
        action = payload["action"]
        calls.append(action)
        if action == "navigate":
            return OmniHubError(TIMEOUT, "navigate: page load timeout (30s)")
        if action == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if action == "list_browser_tabs":
            # The first call is the pre-navigation ownership snapshot; the
            # second one runs during recovery and sees the opened tab.
            tab_lists += 1
            if tab_lists == 1:
                return _ok({"tabs": [{"tabId": 3, "url": "https://example.com/unrelated"}]})
            return _ok(
                {
                    "tabs": [
                        {"tabId": 3, "url": "https://example.com/unrelated"},
                        {
                            "tabId": 7,
                            "url": "https://example.com/search?q=x",
                            "groupTitle": "omnihub: test",
                        },
                    ]
                }
            )
        if action == "attach_tab":
            assert payload["args"]["tabId"] == 7
            return _ok({"success": True, "tabId": 7, "url": "https://example.com/search?q=x"})
        if action == "close_session":
            return _closed()
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
        _browse_call(group_title="omnihub: test", wait_for=None),
        ctx=adapter._context,
    )
    assert result.data == [{"title": "One", "url": "https://example.com/1"}]
    assert result.meta["nav_timed_out"] is True
    assert result.meta["nav_recovered"] is True
    assert result.meta["tab_id"] == 7
    assert calls[:5] == [
        "list_browser_tabs",
        "navigate",
        "list_tabs",
        "list_browser_tabs",
        "attach_tab",
    ]
    assert calls[-1] == "close_session"


def test_daemon_browse_reuses_a_session_tab_after_navigation_timeout(tmp_path):
    calls = []

    def handler(url, payload):
        action = payload["action"]
        calls.append(action)
        if action == "navigate":
            return OmniHubError(TIMEOUT, "navigate: page load timeout (30s)")
        if action == "list_tabs":
            return _ok(
                {
                    "success": True,
                    "tabs": [
                        {"tabId": 4, "url": "https://example.com/"},
                        {
                            "tabId": 9,
                            "url": "https://example.com/",
                            "groupTitle": "omnihub: test",
                        },
                    ],
                }
            )
        if action == "list_browser_tabs":
            return _ok({"tabs": []})
        if action == "close_session":
            return _closed()
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
        _browse_call(group_title="omnihub: test"), ctx=adapter._context
    )
    assert result.data == {"title": "One", "url": "https://example.com/1"}
    assert result.meta["tab_id"] == 9
    # One listing is the pre-navigation snapshot; recovery itself uses
    # list_tabs because the session already owns a tab.
    assert calls.count("list_browser_tabs") == 1
    assert calls[-1] == "close_session"


def test_daemon_browse_recovers_a_new_tab_when_group_titles_are_absent(tmp_path):
    """Daemon builds that report groupTitle=null still expose ownership by id."""
    calls = []
    tab_lists = 0
    user_tab = {"tabId": 5, "url": "https://mp.weixin.qq.com/s/already-open"}

    def handler(url, payload):
        nonlocal tab_lists
        action = payload["action"]
        calls.append(action)
        if action == "list_browser_tabs":
            tab_lists += 1
            if tab_lists == 1:
                return _ok({"tabs": [user_tab]})
            return _ok(
                {
                    "tabs": [
                        user_tab,
                        {
                            "tabId": 11,
                            "url": "https://mp.weixin.qq.com/s/article",
                            "groupTitle": None,
                        },
                    ]
                }
            )
        if action == "navigate":
            return OmniHubError(TIMEOUT, "navigate: page load timeout (30s)")
        if action == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if action == "attach_tab":
            assert payload["args"]["tabId"] == 11
            return _ok(
                {
                    "success": True,
                    "tabId": 11,
                    "url": "https://mp.weixin.qq.com/s/article",
                }
            )
        if action == "close_session":
            return _closed()
        assert action == "evaluate"
        if "location.href" in payload["args"]["code"]:
            return _ok(
                {
                    "value": json.dumps(
                        {
                            "url": "https://mp.weixin.qq.com/s/article",
                            "title": "文章",
                        }
                    )
                }
            )
        return _ok(
            {
                "value": json.dumps(
                    [
                        {
                            "title": "文章",
                            "url": "https://mp.weixin.qq.com/s/article",
                            "text": "正文",
                        }
                    ]
                )
            }
        )

    adapter = _adapter(handler, tmp_path)
    result = adapter.invoke(
        _browse_call(
            url="https://mp.weixin.qq.com/s/article",
            group_title="omnihub: wechat read",
            wait_for=None,
        ),
        ctx=adapter._context,
    )
    assert result.data == [
        {"title": "文章", "url": "https://mp.weixin.qq.com/s/article", "text": "正文"}
    ]
    assert result.meta["nav_recovered"] is True
    assert result.meta["tab_id"] == 11
    assert calls[:5] == [
        "list_browser_tabs",
        "navigate",
        "list_tabs",
        "list_browser_tabs",
        "attach_tab",
    ]


def test_daemon_browse_fails_when_a_timed_out_tab_cannot_be_found(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return OmniHubError(TIMEOUT, "navigate: page load timeout (30s)")
        if payload["action"] == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if payload["action"] == "list_browser_tabs":
            return _ok({"tabs": [{"tabId": 3, "url": "https://example.com/unrelated"}]})
        if payload["action"] == "close_session":
            return _closed()
        raise AssertionError("browse should stop before evaluating")

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            _browse_call(group_title="omnihub: test", wait_for=None),
            ctx=adapter._context,
        )
    assert info.value.code == TIMEOUT
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_browse_does_not_borrow_a_user_tab_after_timeout(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return OmniHubError(TIMEOUT, "navigate: page load timeout (30s)")
        if payload["action"] == "list_tabs":
            return _ok({"success": True, "tabs": []})
        if payload["action"] == "list_browser_tabs":
            return _ok(
                {
                    "tabs": [
                        {
                            "tabId": 5,
                            "url": "https://example.com/search?q=x",
                            "groupTitle": "user research",
                        }
                    ]
                }
            )
        if payload["action"] == "close_session":
            return _closed()
        raise AssertionError("a user tab must not be attached or evaluated")

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            _browse_call(group_title="omnihub: example search", wait_for=None),
            ctx=adapter._context,
        )
    assert info.value.code == TIMEOUT
    assert "attach_tab" not in _actions(adapter._context)
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_browse_requires_a_trusted_platform(tmp_path):
    calls = []

    def handler(url, payload):
        calls.append(payload)
        return _ok({})

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
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
    def handler(url, payload):
        return _closed() if payload["action"] == "close_session" else _ok({})

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            _browse_call(url="http://127.0.0.1/secret"),
            ctx=adapter._context,
        )
    assert info.value.code == BAD_INPUT
    assert _actions(adapter._context) == ["close_session"]


def test_daemon_tab_cleanup_uses_a_fresh_session_per_invocation(tmp_path):
    def handler(url, payload):
        if payload["action"] == "close_session":
            return _closed()
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/"})
        return _ok({"url": "https://example.com/", "title": "Example", "tree_text": "A"})

    adapter = _adapter(handler, tmp_path)
    for _ in range(2):
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="read",
                args={"url": "https://example.com/"},
                raw={"platform_id": "web"},
            ),
            ctx=adapter._context,
        )

    navigations = [
        call["payload"]["session"]
        for call in adapter._context.calls
        if call["payload"]["action"] == "navigate"
    ]
    closes = [
        call["payload"]["session"]
        for call in adapter._context.calls
        if call["payload"]["action"] == "close_session"
    ]
    assert len(set(navigations)) == 2
    assert len(closes) == 2
    assert set(navigations) == set(closes)
    assert all(session.startswith("omnihub-web-") for session in navigations)


def test_daemon_tab_cleanup_failure_does_not_mask_the_action_error(tmp_path):
    def handler(url, payload):
        if payload["action"] == "navigate":
            return _ok({"success": True, "url": "https://example.com/"})
        if payload["action"] == "close_session":
            return OmniHubError(UPSTREAM_BROKEN, "daemon went away during cleanup")
        return _ok({"type": "object", "value": {"blocked": True, "message": "captcha"}})

    adapter = _adapter(handler, tmp_path)
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(_browse_call(), ctx=adapter._context)
    assert info.value.code == PLATFORM_BLOCKED
    assert _actions(adapter._context)[-1] == "close_session"


def test_daemon_address_override_cannot_leave_loopback(tmp_path):
    adapter = DaemonAdapter(
        context=FakeLoopback(lambda url, payload: _ok({})),
        home=tmp_path,
        environ={"KIMI_WEBBRIDGE_ADDR": "http://10.0.0.5:10086"},
    )
    health = adapter.probe()
    assert health.ok is False
    assert "non-loopback" in health.detail
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(adapter="daemon", call="list_tabs", args={}, raw={}),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP


def _script_browse_handler(target, evaluations):
    def handler(url, payload):
        action = payload["action"]
        if action == "close_session":
            return _closed()
        if action == "navigate":
            return _ok({"success": True, "url": target, "tabId": 3})
        if action == "list_browser_tabs":
            return _ok({"tabs": []})
        assert action == "evaluate"
        code = payload["args"]["code"]
        evaluations.append(code)
        if code.startswith("JSON.stringify({url:"):
            return _ok(
                {
                    "type": "string",
                    "value": json.dumps({"url": target, "title": "Example"}),
                }
            )
        return _ok(
            {
                "type": "string",
                "value": json.dumps([{"title": "One", "url": target, "text": "a"}]),
            }
        )

    return handler


def test_daemon_browse_reads_a_bundled_script_without_templating(tmp_path):
    script = tmp_path / "search.js"
    script.write_text(
        "return JSON.stringify([{title: location.href, q: '${query}'}]);\n",
        encoding="utf-8",
    )
    evaluations = []
    adapter = _adapter(
        _script_browse_handler("https://example.com/search?q=x", evaluations), tmp_path
    )

    result = adapter.invoke(
        ToolCall(
            adapter="daemon",
            call="browse",
            script=str(script),
            args={"url": "https://example.com/search?q=x"},
            raw={"platform_id": "example", "platform_trusted": True},
        ),
        ctx=adapter._context,
    )

    assert result.data == [{"title": "One", "url": "https://example.com/search?q=x", "text": "a"}]
    assert evaluations[-1] == script.read_text(encoding="utf-8")
    assert "${query}" in evaluations[-1]


def test_daemon_browse_rejects_script_and_inline_code(tmp_path):
    script = tmp_path / "search.js"
    script.write_text("return [];\n", encoding="utf-8")
    adapter = _adapter(lambda url, payload: None, tmp_path)

    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                script=str(script),
                args={"url": "https://example.com/", "code": "return [];"},
                raw={"platform_id": "example", "platform_trusted": True},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == BAD_INPUT
    assert "both script and args.code" in info.value.message
    assert [call["payload"]["action"] for call in adapter._context.calls] == [
        "close_session"
    ]


def test_daemon_browse_requires_a_javascript_script(tmp_path):
    script = tmp_path / "search.py"
    script.write_text("return []\n", encoding="utf-8")
    adapter = _adapter(lambda url, payload: None, tmp_path)

    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                script=str(script),
                args={"url": "https://example.com/"},
                raw={"platform_id": "example", "platform_trusted": True},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert "must be a .js file" in info.value.message


def test_daemon_browse_reports_a_missing_or_empty_script(tmp_path):
    adapter = _adapter(lambda url, payload: None, tmp_path)
    missing = tmp_path / "missing.js"
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                script=str(missing),
                args={"url": "https://example.com/"},
                raw={"platform_id": "example", "platform_trusted": True},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert "cannot read bundled script" in info.value.message

    empty = tmp_path / "empty.js"
    empty.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                script=str(empty),
                args={"url": "https://example.com/"},
                raw={"platform_id": "example", "platform_trusted": True},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert "bundled script is empty" in info.value.message


def test_daemon_browse_checks_trust_before_reading_the_script(tmp_path):
    missing = tmp_path / "missing.js"
    adapter = _adapter(lambda url, payload: None, tmp_path)

    with pytest.raises(OmniHubError) as info:
        adapter.invoke(
            ToolCall(
                adapter="daemon",
                call="browse",
                script=str(missing),
                args={"url": "https://example.com/"},
                raw={"platform_id": "example", "platform_trusted": False},
            ),
            ctx=adapter._context,
        )
    assert info.value.code == NEED_SETUP
    assert "requires a built-in or explicitly trusted platform" in info.value.message
