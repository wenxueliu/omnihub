from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnihub.core.registry import ManifestError, discover, load_platform

PLATFORMS = Path(__file__).resolve().parents[1] / "omnihub" / "platforms"


def _write_platform(directory: Path, actions: dict, **extra) -> None:
    data = {
        "id": "alpha",
        "description": "alpha test platform",
        "level": "default",
        "priority": 100,
        "domains": [],
        "actions": actions,
    }
    data.update(extra)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )


def _builtin_source(**overrides) -> dict:
    source = {
        "adapter": "builtin",
        "name": "alpha",
        "call": "search",
        "script": "scripts/search.py",
        "parse": ["json"],
        "priority": 10,
    }
    source.update(overrides)
    return source


def _actions(source: dict) -> dict:
    return {
        "search": {
            "params": {"query": {"type": "str", "required": True}},
            "sources": [source],
        }
    }


def test_every_builtin_platform_loads_its_declared_scripts() -> None:
    registry = discover(plugin_dirs=[], include_builtin=True)
    assert registry.errors == []
    for platform in registry.platforms.values():
        directory = platform.path.parent
        parse_script = platform.parse_script
        if parse_script:
            assert parse_script.endswith(".py")
            assert (directory / parse_script).is_file()
        for action in platform.actions.values():
            for source in action.sources:
                if source.script is None:
                    continue
                path = directory / source.script
                assert path.is_file(), f"{platform.id}: {source.script}"
                expected = ".py" if source.adapter == "builtin" else ".js"
                assert path.suffix == expected, f"{platform.id}: {source.script}"


def test_every_builtin_platform_has_a_skill_and_no_inline_javascript() -> None:
    for directory in sorted(PLATFORMS.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "platform.yaml"
        assert manifest.is_file(), directory
        assert (directory / "SKILL.md").is_file(), directory
        text = manifest.read_text(encoding="utf-8")
        assert "code: |" not in text, directory
        assert "args.code" not in text, directory


def test_missing_script_is_a_manifest_error(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_platform(directory, _actions(_builtin_source()))

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "script not found" in excinfo.value.message


def test_declared_script_and_args_code_are_mutually_exclusive(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    source = _builtin_source(args={"code": "return []"})
    _write_platform(directory, _actions(source))
    (directory / "scripts").mkdir()
    (directory / "scripts" / "search.py").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "both script and args.code" in excinfo.value.message


def test_builtin_source_rejects_javascript(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_platform(directory, _actions(_builtin_source(script="scripts/search.js")))
    (directory / "scripts").mkdir()
    (directory / "scripts" / "search.js").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "script must end in .py" in excinfo.value.message


def test_daemon_script_is_only_allowed_for_browse(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    source = {
        "adapter": "daemon",
        "name": "kimi-browser",
        "call": "read",
        "script": "scripts/read.js",
        "priority": 10,
    }
    _write_platform(directory, _actions(source))
    (directory / "scripts").mkdir()
    (directory / "scripts" / "read.js").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "only supported by builtin sources or daemon browse" in excinfo.value.message


def test_script_path_must_not_escape_the_platform(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    source = _builtin_source(script="scripts/a/../../outside.py")
    _write_platform(directory, _actions(source))
    (tmp_path / "outside.py").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "must not contain '..'" in excinfo.value.message


def test_parse_script_must_be_python(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_platform(
        directory,
        _actions(_builtin_source()),
        parse_script="scripts/parse.js",
    )
    (directory / "scripts").mkdir()
    (directory / "scripts" / "search.py").write_text("", encoding="utf-8")
    (directory / "scripts" / "parse.js").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory)
    assert "parse_script" in excinfo.value.message
    assert "script must end in .py" in excinfo.value.message


def test_scripts_under_scripts_require_plugin_capabilities(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    directory = root / "alpha"
    _write_platform(directory, _actions(_builtin_source()))
    (directory / "scripts").mkdir()
    (directory / "scripts" / "search.py").write_text("", encoding="utf-8")

    with pytest.raises(ManifestError) as excinfo:
        load_platform(directory, origin="plugin")
    assert "declares no capabilities" in excinfo.value.message
