from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PLATFORMS = Path(__file__).resolve().parents[1] / "omnihub" / "platforms"
CLI_COMMANDS = {
    "platforms",
    "doctor",
    "check",
    "cooldowns",
    "explain",
    "plugin",
    "skill",
    "search",
    "read",
    "transcribe",
}


def _platform_dirs() -> list[Path]:
    return sorted(
        directory
        for directory in PLATFORMS.iterdir()
        if directory.is_dir() and (directory / "platform.yaml").is_file()
    )


def _frontmatter(text: str, path: Path) -> dict:
    if not text.startswith("---\n"):
        pytest.fail(f"{path} has no YAML frontmatter")
    _, _, remainder = text.partition("---\n")
    header, separator, _ = remainder.partition("\n---\n")
    assert separator, f"{path} has an unterminated frontmatter block"
    parsed = yaml.safe_load(header)
    assert isinstance(parsed, dict), path
    return parsed


def test_every_platform_skill_matches_its_manifest() -> None:
    for directory in _platform_dirs():
        skill_path = directory / "SKILL.md"
        assert skill_path.is_file(), skill_path
        text = skill_path.read_text(encoding="utf-8")
        manifest = yaml.safe_load(
            (directory / "platform.yaml").read_text(encoding="utf-8")
        )

        header = _frontmatter(text, skill_path)
        assert header["name"] == manifest["id"], skill_path
        description = header["description"]
        assert isinstance(description, str) and description.strip(), skill_path
        assert len(description) <= 500, skill_path

        for action_name, action in (manifest.get("actions") or {}).items():
            assert action_name in text, f"{skill_path}: missing action {action_name}"
            for source in action.get("sources") or ():
                script = source.get("script")
                if script:
                    assert script in text, f"{skill_path}: missing {script}"

        parse_script = manifest.get("parse_script")
        if parse_script:
            assert parse_script in text, f"{skill_path}: missing {parse_script}"


def test_platform_skills_do_not_leak_paths_or_secret_values() -> None:
    for skill_path in PLATFORMS.glob("*/SKILL.md"):
        text = skill_path.read_text(encoding="utf-8")
        assert "/home/" not in text, skill_path
        assert "${secret:" not in text, skill_path
        assert not re.search(r"Authorization:\s*Bearer", text), skill_path
        assert "args.code" not in text, skill_path


def test_platform_skills_are_hand_written_not_generated() -> None:
    for skill_path in PLATFORMS.glob("*/SKILL.md"):
        text = skill_path.read_text(encoding="utf-8")
        assert text.startswith("---\n"), skill_path
        assert "# " in text, skill_path
        assert "platform.yaml" in text, skill_path


def test_platform_skill_commands_use_real_cli_subcommands() -> None:
    for skill_path in PLATFORMS.glob("*/SKILL.md"):
        text = skill_path.read_text(encoding="utf-8")
        for match in re.finditer(r"\bomnihub(?:\.cli)?\s+([a-z][a-z0-9_-]*)", text):
            assert match.group(1) in CLI_COMMANDS, f"{skill_path}: {match.group(0)}"
        assert not re.search(r"doctor\b[^\n]*--platform", text), skill_path
