from __future__ import annotations

import json

import yaml

from omnihub.cli import main


def _payload(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured


def test_explain_includes_platform_and_source_details(capsys):
    assert main(["explain", "--platform", "noop", "--action", "search"]) == 0
    payload, _ = _payload(capsys)
    assert payload["platforms"][0]["id"] == "noop"
    assert payload["platforms"][0]["origin"] == "builtin"
    actions = payload["platforms"][0]["actions"]
    assert list(actions) == ["search"]
    source = actions["search"]["sources"][0]
    assert source["adapter"] == "builtin"
    assert source["priority"] == 10
    assert source["when"] is None


def test_explain_text_lists_sources(capsys):
    assert main(["explain", "--platform", "noop", "--action", "search", "--text"]) == 0
    captured = capsys.readouterr()
    assert "platforms: noop" in captured.out
    assert "adapter builtin" in captured.out
    assert "noop" in captured.out


def test_skill_renders_markdown_without_secrets(make_platform, tmp_path, capsys):
    root = tmp_path / "plugins"
    directory = make_platform(root, "alpha", priority=25)
    manifest = yaml.safe_load((directory / "platform.yaml").read_text(encoding="utf-8"))
    manifest["actions"]["search"]["sources"][0]["args"] = {"token": "secret-token"}
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    assert main(["skill", "--plugin-dir", str(root), "--no-builtin"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# OmniHub")
    assert "alpha" in captured.out
    assert "`query`: str (required)" in captured.out
    assert "adapter `builtin`" in captured.out
    assert "secret-token" not in captured.out


def test_skill_can_write_to_a_file(tmp_path, capsys):
    output = tmp_path / "SKILL.md"
    assert main(["skill", "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    text = output.read_text(encoding="utf-8")
    assert "### noop" in text
    assert "search" in text and "read" in text
