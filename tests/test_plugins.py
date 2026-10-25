from __future__ import annotations

import json

import yaml

from agent_reach.adapters.registry import build_adapters
from agent_reach.cli import main
from agent_reach.core.context import Context
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.router import build_plan

SEARCH_HOOK = """
def search(query, limit=5, params=None, ctx=None):
    return [
        {"title": f"plugin result for {query}", "url": "https://plugin.example/a"}
    ]
"""


def _plugin(root, platform_id, *, capabilities=None, hook=True):
    directory = root / platform_id
    directory.mkdir(parents=True)
    manifest = {
        "id": platform_id,
        "description": f"{platform_id} plugin",
        "priority": 50,
        "actions": {
            "search": {
                "params": {"query": {"type": "str", "required": True}},
                "sources": [
                    {"adapter": "builtin", "call": "search", "parse": "json", "priority": 10}
                ],
            }
        },
    }
    if capabilities is not None:
        manifest["capabilities"] = capabilities
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    if hook:
        (directory / "search.py").write_text(SEARCH_HOOK, encoding="utf-8")
    return directory


def test_plugin_hooks_need_declared_capabilities(tmp_path):
    root = tmp_path / "plugins"
    _plugin(root, "alpha")

    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert registry.platforms == {}
    assert any("declares no capabilities" in error.message for error in registry.errors)


def test_plugin_loads_by_default_but_is_not_trusted(tmp_path):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])

    registry = discover(plugin_dirs=[root], include_builtin=False)
    platform = registry.platforms["alpha"]
    assert platform.trusted is False
    assert platform.code_hooks == ("search.py",)
    assert platform.capabilities == ("http",)


def test_trusted_plugins_env_and_argument(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])
    _plugin(root, "beta", capabilities=[])

    registry = discover(plugin_dirs=[root], include_builtin=False, trusted_plugins=["alpha"])
    assert registry.platforms["alpha"].trusted is True
    assert registry.platforms["beta"].trusted is False

    monkeypatch.setenv("REACH_TRUSTED_PLUGINS", "beta")
    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert registry.platforms["alpha"].trusted is False
    assert registry.platforms["beta"].trusted is True


def test_trusted_plugin_hooks_run_end_to_end(tmp_path):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])
    registry = discover(plugin_dirs=[root], include_builtin=False, trusted_plugins=["alpha"])
    plan = build_plan(registry, {}, platform="alpha", action="search")
    ctx = Context(timeout=5)
    payload = run_action(
        registry,
        {},
        action="search",
        params={"query": "hi"},
        ctx=ctx,
        platform="alpha",
        adapters=build_adapters(context=ctx),
    )
    assert plan.platform_ids == ("alpha",)
    assert payload["ok"] is True
    assert payload["items"][0]["title"] == "plugin result for hi"


def test_untrusted_plugin_hooks_are_refused(tmp_path, capsys):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])

    assert (
        main(
            [
                "search",
                "hi",
                "--plugin-dir",
                str(root),
                "--no-builtin",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NEED_SETUP"
    assert "trust" in payload["error"]["message"]


def test_trust_plugin_cli_flag_enables_hooks(tmp_path, capsys):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])

    assert (
        main(
            [
                "search",
                "hi",
                "--plugin-dir",
                str(root),
                "--no-builtin",
                "--trust-plugin",
                "alpha",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["items"][0]["title"] == "plugin result for hi"


def test_plugin_validate_reports_warnings_and_errors(tmp_path, capsys):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])
    broken = root / "broken"
    broken.mkdir()
    (broken / "platform.yaml").write_text("id: [unclosed", encoding="utf-8")

    assert main(["plugin", "validate", str(root)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "plugin validate"
    assert payload["ok"] is False
    assert payload["meta"]["count"] == 2
    alpha = next(item for item in payload["plugins"] if item["id"] == "alpha")
    assert alpha["ok"] is True
    assert alpha["trusted"] is False
    assert any("not run until" in warning for warning in alpha["warnings"])
    assert payload["meta"]["error_count"] == 1


def test_plugin_validate_accepts_a_trusted_plugin(tmp_path, capsys):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=["http"])

    assert main(["plugin", "validate", str(root), "--trust-plugin", "alpha"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plugins"][0]["trusted"] is True
    assert payload["plugins"][0]["warnings"] == []


def test_plugin_list_shows_only_plugins(tmp_path, capsys):
    root = tmp_path / "plugins"
    _plugin(root, "alpha", capabilities=[])

    assert main(["plugin", "list", "--plugin-dir", str(root), "--no-builtin"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [plugin["id"] for plugin in payload["plugins"]] == ["alpha"]
    assert all(plugin["origin"] == "plugin" for plugin in payload["plugins"])


def test_plugin_validate_with_no_roots_is_ok():
    assert main(["plugin", "validate", "--no-builtin"]) == 0


def test_plugin_validate_missing_path_is_an_error(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main(["plugin", "validate", str(missing)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == [{"path": str(missing), "message": "path does not exist"}]
