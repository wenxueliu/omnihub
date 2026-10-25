"""Resolve the scripts a platform bundles under its ``scripts/`` directory.

A manifest may reference a bundled script by relative path:

* ``source.script`` -- a ``builtin`` Python module or a ``daemon`` browse
  JavaScript file;
* top-level ``parse_script`` -- the platform's custom parser module.

The path is configuration, never content: adapters read the file themselves so
script text is neither templated nor embedded in the manifest. This module is a
leaf (stdlib only) so registry, pipeline and adapters can share one validator
without import cycles.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

SCRIPT_DIR = "scripts"
SCRIPT_SUFFIXES = (".py", ".js")


class ScriptPathError(ValueError):
    """A declared script path is malformed or escapes the platform directory."""


def normalize_script_path(declared: str) -> str:
    """Validate a manifest-declared script path and return its POSIX form.

    Declared scripts must live below ``scripts/``; legacy root-level modules
    are found through :func:`first_existing`, not here.
    """
    if not isinstance(declared, str) or not declared.strip():
        raise ScriptPathError("script path must be a non-empty string")
    text = declared.strip()
    if "\\" in text:
        raise ScriptPathError(f"script path must use forward slashes: {declared!r}")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ScriptPathError(f"script path must be relative: {declared!r}")
    if ".." in path.parts:
        raise ScriptPathError(f"script path must not contain '..': {declared!r}")
    if len(path.parts) < 2 or path.parts[0] != SCRIPT_DIR:
        raise ScriptPathError(
            f"script path must start with {SCRIPT_DIR}/: {declared!r}"
        )
    if path.suffix not in SCRIPT_SUFFIXES:
        raise ScriptPathError(
            f"script path must end in one of {', '.join(SCRIPT_SUFFIXES)}: {declared!r}"
        )
    return path.as_posix()


def safe_relative_path(platform_dir: Path | str, relative: str) -> Path:
    """Resolve *relative* inside *platform_dir*, rejecting escapes and symlinks."""
    if not isinstance(relative, str) or not relative.strip():
        raise ScriptPathError("script path must be a non-empty string")
    text = relative.strip()
    if "\\" in text:
        raise ScriptPathError(f"script path must use forward slashes: {relative!r}")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise ScriptPathError(f"script path must be relative: {relative!r}")
    if ".." in path.parts:
        raise ScriptPathError(f"script path must not contain '..': {relative!r}")
    root = Path(platform_dir).resolve()
    candidate = (root / path.as_posix()).resolve()
    if not candidate.is_relative_to(root):
        raise ScriptPathError(
            f"script path escapes the platform directory: {relative!r}"
        )
    return candidate


def resolve_script_path(
    platform_dir: Path | str, declared: str, *, must_exist: bool = True
) -> Path:
    """Resolve a declared ``scripts/...`` path, optionally requiring the file."""
    candidate = safe_relative_path(platform_dir, normalize_script_path(declared))
    if must_exist and not candidate.is_file():
        raise ScriptPathError(f"script not found: {declared!r}")
    return candidate


def first_existing(
    platform_dir: Path | str,
    candidates: Iterable[str],
    *,
    declared_only: bool = False,
) -> Optional[Path]:
    """Return the first candidate that exists, in the given order.

    ``declared_only`` applies :func:`normalize_script_path`; otherwise bare
    legacy names such as ``search.py`` are accepted too.
    """
    for candidate in candidates:
        try:
            if declared_only:
                path = safe_relative_path(
                    platform_dir, normalize_script_path(candidate)
                )
            else:
                path = safe_relative_path(platform_dir, candidate)
        except ScriptPathError:
            continue
        if path.is_file():
            return path
    return None


def relative_script(platform_dir: Path | str, path: Path) -> str:
    """The POSIX path of *path* relative to the platform directory."""
    return path.resolve().relative_to(Path(platform_dir).resolve()).as_posix()
