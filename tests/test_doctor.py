from __future__ import annotations

import sys

from agent_reach.adapters.base import Health, ProcessResult
from agent_reach.core.doctor import build_doctor_report
from agent_reach.core.registry import Registry, discover, load_platform


class FakeAdapter:
    def __init__(self, *, ok=True, detail=""):
        self.health = Health(ok=ok, detail=detail)

    def probe(self):
        return self.health

    def invoke(self, call, *, ctx):
        raise AssertionError("doctor must not invoke adapters")


class FakeCtx:
    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})
        self.commands = []

    def secret(self, name):
        return self.secrets.get(name)

    def run(self, argv, **kwargs):
        self.commands.append(tuple(argv))
        return ProcessResult(stdout="ok", stderr="", returncode=0)


def _report(registry, ctx=None, adapters=None):
    return build_doctor_report(
        registry,
        ctx=ctx or FakeCtx(),
        adapters=adapters or {"builtin": FakeAdapter()},
    )


def test_doctor_reports_missing_required_tool_and_secret(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(
        root,
        "demo",
        requires={
            "tools": ["definitely-not-a-real-tool-xyz"],
            "secrets": ["DEMO_TOKEN"],
        },
    )
    report = _report(discover(plugin_dirs=[root], include_builtin=False))
    row = report["platforms"][0]
    assert report["ok"] is False
    assert row["status"] == "error"
    assert row["tools"][0]["ok"] is False
    assert row["secrets"][0] == {
        "name": "DEMO_TOKEN",
        "ok": False,
        "detail": "missing",
    }


def test_doctor_reports_configured_secret_without_leaking(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "demo", requires={"secrets": ["DEMO_TOKEN"]})
    report = _report(
        discover(plugin_dirs=[root], include_builtin=False),
        ctx=FakeCtx({"DEMO_TOKEN": "super-secret"}),
    )
    assert report["platforms"][0]["secrets"][0]["detail"] == "configured"
    assert "super-secret" not in str(report)


def test_doctor_marks_missing_optional_source_tool_as_degraded(make_platform, tmp_path):
    root = tmp_path / "plugins"
    actions = {
        "search": {
            "params": {"query": {"type": "str", "required": True}},
            "sources": [
                {
                    "adapter": "cli",
                    "tool": "definitely-not-a-real-tool-xyz",
                    "call": "search",
                    "parse": "json",
                }
            ],
        }
    }
    make_platform(root, "demo", actions=actions)
    report = _report(
        discover(plugin_dirs=[root], include_builtin=False),
        adapters={"cli": FakeAdapter()},
    )
    assert report["ok"] is True
    assert report["platforms"][0]["status"] == "degraded"
    assert report["meta"]["degraded_count"] == 1


def test_doctor_reports_missing_adapter(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(
        root,
        "demo",
        actions={
            "search": {
                "sources": [
                    {
                        "adapter": "cli",
                        "tool": "anything",
                        "parse": "json",
                    }
                ]
            }
        },
    )
    report = _report(
        discover(plugin_dirs=[root], include_builtin=False), adapters={}
    )
    row = report["platforms"][0]
    assert row["status"] == "error"
    assert row["adapters"][0] == {
        "kind": "cli",
        "ok": False,
        "detail": "adapter not implemented",
    }


def test_doctor_does_not_run_plugin_auth_status(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(
        root,
        "demo",
        auth={"status": [sys.executable, "-c", "raise SystemExit(1)"]},
    )
    ctx = FakeCtx()
    report = _report(discover(plugin_dirs=[root], include_builtin=False), ctx=ctx)
    auth = report["platforms"][0]["auth"]
    assert auth["ok"] is None
    assert "built-in" in auth["detail"]
    assert ctx.commands == []


def test_doctor_runs_builtin_auth_status(make_platform, tmp_path):
    path = make_platform(
        tmp_path,
        "demo",
        auth={"status": [sys.executable, "-c", "print('logged in')"]},
    )
    platform = load_platform(path, origin="builtin")
    report = _report(
        Registry(platforms={platform.id: platform}),
        ctx=FakeCtx(),
        adapters={"builtin": FakeAdapter()},
    )
    assert report["platforms"][0]["auth"]["ok"] is True
    assert report["ok"] is True


def test_doctor_cli_json(make_platform, tmp_path, capsys):
    from agent_reach.cli import main

    root = tmp_path / "plugins"
    make_platform(root, "demo")
    code = main(["doctor", "--plugin-dir", str(root), "--no-builtin"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"platforms"' in out
    assert '"demo"' in out
