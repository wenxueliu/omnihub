from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from omnihub.core.registry import (
    SCHEMA_DIR,
    ManifestError,
    discover_tools,
    load_platform,
)
from omnihub.core.result import TIMEOUT, OmniHubError, envelope, make_item

NOOP = (
    Path(__file__).resolve().parents[1]
    / "omnihub"
    / "platforms"
    / "noop"
    / "platform.yaml"
)


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _base(platform_id: str = "sample") -> dict:
    return {
        "id": platform_id,
        "description": "sample",
        "actions": {
            "search": {
                "sources": [{"adapter": "builtin", "call": "search", "parse": "json"}]
            }
        },
    }


def test_builtin_noop_is_valid():
    platform = load_platform(NOOP)
    assert platform.id == "noop"
    assert platform.level == "experimental"
    assert set(platform.actions) == {"search", "read"}
    source = platform.actions["search"].enabled_sources()[0]
    assert source.adapter == "builtin"
    assert source.parse == ("json_items",)


def test_missing_actions_is_rejected(tmp_path):
    path = _write(
        tmp_path / "sample" / "platform.yaml",
        {"id": "sample", "description": "sample"},
    )
    with pytest.raises(ManifestError, match="actions"):
        load_platform(path)


def test_unknown_adapter_is_rejected(tmp_path):
    data = _base()
    data["actions"]["search"]["sources"][0]["adapter"] = "telepathy"
    path = _write(tmp_path / "sample" / "platform.yaml", data)
    with pytest.raises(ManifestError, match="unknown adapter"):
        load_platform(path)


def test_unknown_level_is_rejected(tmp_path):
    data = _base()
    data["level"] = "core"
    path = _write(tmp_path / "sample" / "platform.yaml", data)
    with pytest.raises(ManifestError, match="level"):
        load_platform(path)


def test_public_web_object_is_accepted(tmp_path):
    data = _base()
    data["actions"]["search"]["public_web"] = {
        "scopes": ["example.com/path/"],
        "priority": 90,
    }
    path = _write(tmp_path / "sample" / "platform.yaml", data)
    platform = load_platform(path)
    assert platform.actions["search"].public_web == {
        "scopes": ["example.com/path/"],
        "priority": 90,
    }


def test_tool_manifest_validation(tmp_path):
    good = {
        "id": "opencli",
        "kind": "cli",
        "limits": {"timeout": 30, "max_output_bytes": 1024},
    }
    bad = {"id": "bad", "kind": "telepathy"}
    (tmp_path / "opencli.yaml").write_text(yaml.safe_dump(good), encoding="utf-8")
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    (tmp_path / "mismatch.yaml").write_text(
        yaml.safe_dump({"id": "other", "kind": "cli"}), encoding="utf-8"
    )

    tools = discover_tools([tmp_path])
    assert set(tools) == {"opencli"}


def test_envelope_matches_schema():
    schema = json.loads((SCHEMA_DIR / "envelope.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    item = make_item(
        title="T",
        url="https://example.com/a",
        text="S",
        platform="noop",
        source="builtin",
        priority=10,
    )
    validator.validate(
        envelope(
            channel="noop",
            action="search",
            backend="builtin",
            items=[item],
            elapsed_ms=3,
            attempts=[{"backend": "builtin", "result": "OK"}],
            meta={"platform": "noop"},
        )
    )

    validator.validate(
        envelope(channel="noop", action="search", error=OmniHubError(TIMEOUT, "slow"))
    )


def test_builtin_noop_sources_declare_their_scripts():
    platform = load_platform(NOOP)
    search = platform.actions["search"].enabled_sources()[0]
    read = platform.actions["read"].enabled_sources()[0]
    assert search.script == "scripts/search.py"
    assert read.script == "scripts/read.py"
    assert platform.parse_script is None


def test_builtin_platform_parses_a_custom_parser_script():
    path = NOOP.parents[1] / "github" / "platform.yaml"
    platform = load_platform(path)
    assert platform.parse_script == "scripts/parse.py"


def _platform_with_script(script: object) -> dict:
    data = _base()
    data["actions"]["search"]["sources"][0]["script"] = script
    return data


@pytest.mark.parametrize(
    "script",
    [
        "/tmp/search.py",
        "../search.py",
        "lib/search.py",
        "scripts/search.sh",
        "scripts/search",
        "scripts\\search.py",
        42,
    ],
)
def test_invalid_source_script_is_rejected(tmp_path, script):
    path = _write(tmp_path / "sample" / "platform.yaml", _platform_with_script(script))
    with pytest.raises(ManifestError, match="script"):
        load_platform(path)


@pytest.mark.parametrize(
    "parse_script",
    ["/tmp/parse.py", "../parse.py", "scripts/parse.sh", 42],
)
def test_invalid_parse_script_is_rejected(tmp_path, parse_script):
    data = _base()
    data["parse_script"] = parse_script
    path = _write(tmp_path / "sample" / "platform.yaml", data)
    with pytest.raises(ManifestError, match="parse_script"):
        load_platform(path)
