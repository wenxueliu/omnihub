from __future__ import annotations

import sys

from omnihub.adapters.base import Health, ProcessResult
from omnihub.core.doctor import build_doctor_report
from omnihub.core.registry import Registry, discover, load_platform


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


def _report(registry, ctx=None, adapters=None, rate_limits=None):
    return build_doctor_report(
        registry,
        ctx=ctx or FakeCtx(),
        adapters=adapters or {"builtin": FakeAdapter()},
        rate_limits=rate_limits,
    )


class StubLimits:
    def __init__(self, rows):
        self.rows = list(rows)

    def active(self):
        return list(self.rows)


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
        "required": True,
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
    from omnihub.cli import main

    root = tmp_path / "plugins"
    make_platform(root, "demo")
    code = main(["doctor", "--plugin-dir", str(root), "--no-builtin"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"platforms"' in out
    assert '"demo"' in out


def test_doctor_surfaces_cooldowns_without_changing_status(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "demo")
    row = {
        "scope": "source",
        "platform": "demo",
        "source": "exa",
        "code": "UPSTREAM_BROKEN",
        "retry_after_seconds": 42.0,
    }
    report = _report(
        discover(plugin_dirs=[root], include_builtin=False),
        rate_limits=StubLimits([row]),
    )
    platform = report["platforms"][0]
    assert platform["status"] == "ok"
    assert platform["cooldowns"] == [row]
    assert report["meta"]["cooling_count"] == 1
    assert report["ok"] is True


def test_doctor_without_a_ledger_reports_no_cooldowns(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "demo")
    report = _report(discover(plugin_dirs=[root], include_builtin=False))
    assert report["platforms"][0]["cooldowns"] == []
    assert report["meta"]["cooling_count"] == 0


def test_doctor_cli_text_prints_active_cooldowns(monkeypatch, tmp_path, capsys):
    from omnihub.cli import main
    from omnihub.core.ratelimit import RateLimits
    from omnihub.core.result import RATE_LIMITED

    monkeypatch.setenv("OMNIHUB_STATE_DIR", str(tmp_path / "state"))
    RateLimits().note_error("github", "github-api", RATE_LIMITED, retry_after=120)
    code = main(["doctor", "--text"])
    assert code == 0
    captured = capsys.readouterr()
    assert "cooling platform" in captured.out
    assert "120s left (RATE_LIMITED)" in captured.out
