from __future__ import annotations

from pathlib import Path

import pytest

from omnihub.core.scripts import (
    ScriptPathError,
    first_existing,
    normalize_script_path,
    relative_script,
    resolve_script_path,
    safe_relative_path,
)


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("scripts/search.py", "scripts/search.py"),
        ("scripts/lib/parse.py", "scripts/lib/parse.py"),
        ("  scripts/read.js  ", "scripts/read.js"),
    ],
)
def test_normalize_script_path_accepts_scripts(declared: str, expected: str) -> None:
    assert normalize_script_path(declared) == expected


@pytest.mark.parametrize(
    "declared",
    [
        "",
        "   ",
        "search.py",
        "lib/scripts/search.py",
        "scripts\\search.py",
        "/scripts/search.py",
        "scripts/../search.py",
        "scripts/search.txt",
    ],
)
def test_normalize_script_path_rejects_bad_paths(declared: str) -> None:
    with pytest.raises(ScriptPathError):
        normalize_script_path(declared)


def test_safe_relative_path_stays_inside_the_platform(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    (platform / "scripts").mkdir(parents=True)
    script = platform / "scripts" / "search.py"
    script.write_text("", encoding="utf-8")

    assert safe_relative_path(platform, "scripts/search.py") == script.resolve()
    with pytest.raises(ScriptPathError):
        safe_relative_path(platform, "../alpha/scripts/search.py")


def test_safe_relative_path_rejects_a_symlink_escape(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    (platform / "scripts").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    (platform / "scripts" / "escape.py").symlink_to(outside)

    with pytest.raises(ScriptPathError):
        safe_relative_path(platform, "scripts/escape.py")


def test_resolve_requires_the_file_to_exist(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    (platform / "scripts").mkdir(parents=True)
    with pytest.raises(ScriptPathError):
        resolve_script_path(platform, "scripts/search.py")

    script = platform / "scripts" / "search.py"
    script.write_text("", encoding="utf-8")
    assert resolve_script_path(platform, "scripts/search.py") == script.resolve()
    assert (
        resolve_script_path(platform, "scripts/missing.py", must_exist=False)
        == (platform / "scripts" / "missing.py").resolve()
    )


def test_first_existing_prefers_the_declared_order(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    (platform / "scripts").mkdir(parents=True)
    (platform / "scripts" / "search.py").write_text("", encoding="utf-8")
    (platform / "search.py").write_text("", encoding="utf-8")

    found = first_existing(platform, ["scripts/search.py", "search.py"])
    assert found == (platform / "scripts" / "search.py").resolve()

    legacy = first_existing(platform, ["scripts/missing.py", "search.py"])
    assert legacy == (platform / "search.py").resolve()


def test_first_existing_ignores_paths_that_escape(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    platform.mkdir()
    assert first_existing(platform, ["../outside.py"]) is None


def test_first_existing_declared_only_requires_the_scripts_prefix(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    platform.mkdir()
    (platform / "search.py").write_text("", encoding="utf-8")

    assert first_existing(platform, ["search.py"], declared_only=True) is None


def test_relative_script_returns_a_posix_path(tmp_path: Path) -> None:
    platform = tmp_path / "alpha"
    (platform / "scripts").mkdir(parents=True)
    script = platform / "scripts" / "read.js"
    script.write_text("", encoding="utf-8")

    assert relative_script(platform, script) == "scripts/read.js"
