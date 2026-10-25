from __future__ import annotations

import json

import pytest

from agent_reach.adapters.base import RawResult, ToolCall
from agent_reach.adapters.daemon import (
    DaemonAdapter,
    discover_address,
    normalize_address,
)
from agent_reach.core.result import BAD_INPUT, NEED_SETUP, ReachError


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
