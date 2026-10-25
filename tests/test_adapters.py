from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from omnihub.adapters.base import Health, RawResult, ToolCall
from omnihub.adapters.builtin import BuiltinAdapter
from omnihub.adapters.cli import CliAdapter, args_to_argv, build_argv
from omnihub.adapters.http import HttpAdapter
from omnihub.adapters.opencli import OpenCliAdapter
from omnihub.core.context import Context
from omnihub.core.result import BAD_INPUT, NEED_LOGIN, NEED_SETUP, RATE_LIMITED, OmniHubError


class FakeHttpContext:
    def __init__(self, result=None):
        self.result = result or RawResult(data={"ok": True}, text='{"ok": true}')
        self.calls = []
        self.timeout = 30

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.result

    def secret(self, name):
        return None


def test_http_adapter_builds_json_request():
    ctx = FakeHttpContext()
    call = ToolCall(
        adapter="http",
        url="https://example.com/api",
        method="POST",
        headers={"X-Test": "1"},
        body={"q": "x"},
        timeout=5,
    )
    assert HttpAdapter().invoke(call, ctx=ctx) is ctx.result
    url, kwargs = ctx.calls[0]
    assert url == "https://example.com/api"
    assert kwargs["method"] == "POST"
    assert kwargs["headers"]["X-Test"] == "1"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert json.loads(kwargs["data"]) == {"q": "x"}


def test_http_adapter_rejects_private_url():
    with pytest.raises(OmniHubError) as info:
        HttpAdapter().invoke(
            ToolCall(adapter="http", url="http://127.0.0.1/"), ctx=FakeHttpContext()
        )
    assert info.value.code == BAD_INPUT


def test_http_adapter_requires_url():
    with pytest.raises(OmniHubError) as info:
        HttpAdapter().invoke(ToolCall(adapter="http"), ctx=FakeHttpContext())
    assert info.value.code == BAD_INPUT


def test_args_to_argv_flags():
    assert args_to_argv({"query": "x", "verbose": True, "dry_run": False, "limit": 2}) == [
        "--query",
        "x",
        "--verbose",
        "--limit",
        "2",
    ]


def test_build_argv_prefers_explicit_argv():
    call = ToolCall(adapter="cli", argv=("tool", "run"), tool="other", call="search")
    assert build_argv(call) == ("tool", "run")


def test_cli_adapter_runs_process():
    ctx = Context(timeout=5)
    call = ToolCall(
        adapter="cli",
        argv=(sys.executable, "-c", "print('{\"items\": []}')"),
    )
    result = CliAdapter().invoke(call, ctx=ctx)
    assert json.loads(result.text) == {"items": []}
    assert result.meta["returncode"] == 0


def _write_fake_binary(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "bin" / "fake-tool"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_cli_adapter_runs_a_fake_binary(tmp_path):
    binary = _write_fake_binary(
        tmp_path,
        "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n",
    )
    call = ToolCall(adapter="cli", argv=(str(binary), "hello", "world"))
    result = CliAdapter().invoke(call, ctx=Context(timeout=5))
    assert json.loads(result.text) == {"argv": ["hello", "world"]}
    assert result.meta["returncode"] == 0


def test_cli_adapter_maps_login_wall_from_a_fake_binary(tmp_path):
    binary = _write_fake_binary(
        tmp_path,
        "import sys\nsys.stderr.write('please log in first')\nsys.exit(1)\n",
    )
    with pytest.raises(OmniHubError) as info:
        CliAdapter().invoke(
            ToolCall(adapter="cli", argv=(str(binary),)), ctx=Context(timeout=5)
        )
    assert info.value.code == NEED_LOGIN


def test_cli_adapter_maps_rate_limit():
    ctx = Context(timeout=5)
    call = ToolCall(
        adapter="cli",
        argv=(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('HTTP 429 too many requests'); sys.exit(1)",
        ),
    )
    with pytest.raises(OmniHubError) as info:
        CliAdapter().invoke(call, ctx=ctx)
    assert info.value.code == RATE_LIMITED


def _write_opencli(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "opencli"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--fail' in sys.argv:\n"
        "    print('ok: false')\n"
        "    print('error:')\n"
        "    print('  code: NEED_LOGIN')\n"
        "    print('  message: sign in first')\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'items': [{'title': 'T', 'url': 'https://example.com'}]}))\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_opencli_adapter_success(tmp_path, monkeypatch):
    _write_opencli(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    ctx = Context(timeout=5)
    call = ToolCall(adapter="opencli", site="demo", call="search", args={"query": "x"})
    result = OpenCliAdapter().invoke(call, ctx=ctx)
    assert result.data["items"][0]["title"] == "T"


def test_opencli_adapter_maps_yaml_error(tmp_path, monkeypatch):
    _write_opencli(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    ctx = Context(timeout=5)
    call = ToolCall(adapter="opencli", site="demo", call="search", args={"fail": True})
    with pytest.raises(OmniHubError) as info:
        OpenCliAdapter().invoke(call, ctx=ctx)
    assert info.value.code == NEED_LOGIN
    assert "sign in first" in info.value.message


def test_opencli_probe_reports_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert OpenCliAdapter().probe() == Health(ok=False, detail="opencli is not installed")


def test_opencli_requires_site():
    with pytest.raises(OmniHubError) as info:
        OpenCliAdapter().invoke(ToolCall(adapter="opencli"), ctx=Context(timeout=5))
    assert info.value.code == BAD_INPUT


def test_builtin_adapter_refuses_plugin_python(tmp_path):
    with pytest.raises(OmniHubError) as info:
        BuiltinAdapter().invoke(
            ToolCall(
                adapter="builtin",
                call="search",
                action="search",
                raw={
                    "platform_origin": "plugin",
                    "platform_path": str(tmp_path),
                    "platform_id": "demo",
                },
            ),
            ctx=Context(timeout=5),
        )
    assert info.value.code == NEED_SETUP
