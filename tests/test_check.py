from __future__ import annotations

import json

import pytest

import omnihub.cli as cli
from omnihub.cli import main
from omnihub.core.registry import discover


def _payload(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured


def test_builtin_manifests_declare_live_checks():
    registry = discover(plugin_dirs=[], include_builtin=True)
    assert not registry.errors
    for platform_id in ("web", "github", "v2ex", "bilibili", "reddit", "youtube"):
        platform = registry.platforms[platform_id]
        assert "search" in platform.check, platform_id
        assert "read" in platform.check, platform_id
        assert platform.check["search"].get("query"), platform_id
        assert platform.check["read"].get("target"), platform_id
    assert registry.platforms["noop"].check == {}


def test_catalog_exposes_declared_checks():
    registry = discover(plugin_dirs=[], include_builtin=True)
    entry = next(item for item in registry.catalog() if item["id"] == "github")
    assert entry["check"] == ["read", "search"]


def test_check_dry_run_does_not_touch_the_network(monkeypatch, capsys):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("dry-run executed a live check")

    monkeypatch.setattr(cli, "run_action", explode)
    assert main(["check", "--dry-run", "--platform", "github"]) == 0
    payload, _ = _payload(capsys)
    assert payload["ok"] is True
    assert payload["meta"] == {
        "count": 2,
        "failed": 0,
        "cooling": 0,
        "skipped": 0,
        "dry_run": True,
    }
    assert [item["action"] for item in payload["checks"]] == ["search", "read"]
    assert all(item["status"] == "planned" for item in payload["checks"])
    assert payload["checks"][0]["params"]["query"] == "flask"


def test_check_platform_filter_and_skipped_count(capsys):
    assert main(["check", "--dry-run", "--platform", "github", "--action", "read"]) == 0
    payload, _ = _payload(capsys)
    assert payload["meta"]["count"] == 1
    assert payload["checks"][0]["action"] == "read"

    assert main(["check", "--dry-run", "--platform", "noop"]) == 0
    payload, _ = _payload(capsys)
    assert payload["meta"] == {
        "count": 0,
        "failed": 0,
        "cooling": 0,
        "skipped": 1,
        "dry_run": True,
    }


def test_check_unknown_platform_is_bad_input(capsys):
    assert main(["check", "--dry-run", "--platform", "nope"]) == 2
    payload, _ = _payload(capsys)
    assert payload["error"]["code"] == "BAD_INPUT"
    assert "nope" in payload["error"]["message"]


def test_check_aggregates_live_results(monkeypatch, capsys):
    calls = []

    def fake_run_action(registry, routing, *, action, params, ctx, platform):
        calls.append((platform, action, params))
        if platform == "v2ex":
            return {
                "ok": False,
                "error": {"code": "UPSTREAM_BROKEN", "message": "boom"},
            }
        return {"ok": True, "backend": "exa", "items": [{"title": "t"}]}

    monkeypatch.setattr(cli, "run_action", fake_run_action)
    code = main(
        [
            "check",
            "--platform",
            "github",
            "--platform",
            "v2ex",
            "--action",
            "search",
        ]
    )
    assert code == 1
    payload, _ = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UPSTREAM_BROKEN"
    assert payload["meta"]["count"] == 2
    assert payload["meta"]["failed"] == 1
    by_platform = {item["platform"]: item for item in payload["checks"]}
    assert by_platform["github"]["status"] == "ok"
    assert by_platform["github"]["backend"] == "exa"
    assert by_platform["github"]["items"] == 1
    assert by_platform["v2ex"]["status"] == "failed"
    assert by_platform["v2ex"]["error"]["code"] == "UPSTREAM_BROKEN"
    assert all(item["elapsed_ms"] >= 0 for item in payload["checks"])
    assert calls == [
        ("github", "search", {"query": "flask", "limit": 1}),
        ("v2ex", "search", {"query": "python", "limit": 1}),
    ]


def _cooldown_payload(*, attempts=None):
    return {
        "ok": False,
        "error": {
            "code": "RATE_LIMITED",
            "message": "platform is cooling down",
            "retry_after_seconds": 42.0,
        },
        "meta": {
            "attempts": (
                [
                    {
                        "platform": "github",
                        "backend": None,
                        "result": "SKIP",
                        "reason": "platform cooldown (RATE_LIMITED)",
                        "retry_after_seconds": 42.0,
                    }
                ]
                if attempts is None
                else attempts
            )
        },
    }


def test_check_marks_ledger_skips_as_cooling(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_action", lambda *args, **kwargs: _cooldown_payload())
    assert main(["check", "--platform", "github", "--action", "search"]) == 0
    payload, _ = _payload(capsys)
    check = payload["checks"][0]
    assert check["status"] == "cooling"
    assert check["ok"] is None
    assert check["retry_after_seconds"] == 42.0
    assert payload["meta"]["cooling"] == 1
    assert payload["meta"]["failed"] == 0
    assert payload["ok"] is True
    assert payload["error"] is None


def test_check_keeps_a_network_rate_limit_as_failure(monkeypatch, capsys):
    attempts = [
        {
            "platform": "github",
            "backend": "github-api",
            "result": "RUN",
            "retry_after_seconds": 42.0,
        }
    ]
    monkeypatch.setattr(
        cli, "run_action", lambda *args, **kwargs: _cooldown_payload(attempts=attempts)
    )
    assert main(["check", "--platform", "github", "--action", "search"]) == 1
    payload, _ = _payload(capsys)
    check = payload["checks"][0]
    assert check["status"] == "failed"
    assert check["ok"] is False
    assert "retry_after_seconds" not in check
    assert payload["meta"]["cooling"] == 0
    assert payload["meta"]["failed"] == 1


def test_check_treats_all_skipped_sources_as_skipped(monkeypatch, capsys):
    payload = {
        "ok": False,
        "error": {"code": "NEED_SETUP", "message": "tool 'notebooklm' is not installed"},
        "meta": {
            "attempts": [
                {
                    "platform": "notebooklm",
                    "backend": "notebooklm-cli",
                    "result": "SKIP",
                    "reason": "tool 'notebooklm' is not installed",
                }
            ]
        },
    }
    monkeypatch.setattr(cli, "run_action", lambda *args, **kwargs: payload)
    assert main(["check", "--platform", "notebooklm"]) == 0
    payload, _ = _payload(capsys)
    check = payload["checks"][0]
    assert check["status"] == "skipped"
    assert check["ok"] is None
    assert payload["meta"]["failed"] == 0
    assert payload["meta"]["skipped"] == 1
    assert payload["ok"] is True


def test_check_text_marks_cooling_with_retry_hint(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_action", lambda *args, **kwargs: _cooldown_payload())
    assert main(["check", "--platform", "github", "--action", "search", "--text"]) == 0
    captured = capsys.readouterr()
    assert "cooling" in captured.out
    assert "retry in 42s" in captured.out
    assert "0 failed, 1 cooling" in captured.err


def test_check_filters_actions_that_are_not_declared(
    make_platform, tmp_path, monkeypatch, capsys
):
    import yaml

    root = tmp_path / "plugins"
    directory = make_platform(root, "alpha")
    manifest = yaml.safe_load((directory / "platform.yaml").read_text(encoding="utf-8"))
    manifest["check"] = {"search": {"query": "x"}}
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    def fake_run_action(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("no check declared for this action")

    monkeypatch.setenv("OMNIHUB_PLUGIN_PATH", str(root))
    monkeypatch.setattr(cli, "run_action", fake_run_action)
    assert main(["check", "--platform", "alpha", "--action", "read"]) == 0
    payload, _ = _payload(capsys)
    assert payload["meta"]["count"] == 0
    assert payload["meta"]["skipped"] == 1


def test_check_text_renders_status(capsys):
    assert main(["check", "--dry-run", "--platform", "github", "--text"]) == 0
    captured = capsys.readouterr()
    assert "github" in captured.out
    assert "planned" in captured.out
    assert "2 check(s)" in captured.err


def test_declared_check_params_are_valid_for_the_action():
    registry = discover(plugin_dirs=[], include_builtin=True)
    for platform in registry.platforms.values():
        for action_name, params in platform.check.items():
            assert action_name in platform.actions, (platform.id, action_name)
            declared = platform.actions[action_name].params
            for name in params:
                assert name in declared, (platform.id, action_name, name)
