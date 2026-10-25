from __future__ import annotations

import pytest
import yaml

from omnihub.core.config import ConfigError, deep_merge, load_routing


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_deep_merge_is_recursive_and_replaces_lists():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    override = {"a": {"c": [3], "e": 2}, "d": 9}
    assert deep_merge(base, override) == {
        "a": {"b": 1, "c": [3], "e": 2},
        "d": 9,
    }
    assert base["a"]["c"] == [1, 2]


def test_layers_merge_user_project_explicit(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    explicit = tmp_path / "explicit.yaml"

    _write(
        home / ".omnihub" / "config" / "routing.yaml",
        {
            "version": 1,
            "mode": "aggregate",
            "platforms": {"noop": {"enabled": False}},
        },
    )
    _write(
        project / ".omnihub" / "config" / "routing.yaml",
        {"version": 1, "mode": "first_success", "aggregate": {"top": 7}},
    )
    _write(explicit, {"version": 1, "intents": {"code": ["noop"]}})

    routing = load_routing(home=home, project_dir=project, path=explicit)
    assert routing["mode"] == "first_success"
    assert routing["platforms"]["noop"]["enabled"] is False
    assert routing["aggregate"]["top"] == 7
    assert routing["aggregate"]["dedupe"] is True
    assert routing["intents"]["code"] == ["noop"]


def test_environment_layer(tmp_path, monkeypatch):
    path = _write(tmp_path / "env.yaml", {"version": 1, "mode": "aggregate"})
    monkeypatch.setenv("OMNIHUB_ROUTING", str(path))
    routing = load_routing(home=tmp_path / "empty", project_dir=tmp_path / "none")
    assert routing["mode"] == "aggregate"


def test_unknown_key_is_rejected(tmp_path):
    path = _write(
        tmp_path / "bad.yaml",
        {"version": 1, "security": {"allow_private": True}},
    )
    with pytest.raises(ConfigError, match="security"):
        load_routing(home=tmp_path / "empty", project_dir=tmp_path / "none", path=path)


def test_missing_explicit_layer_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_routing(
            home=tmp_path / "empty",
            project_dir=tmp_path / "none",
            path=tmp_path / "missing.yaml",
        )
