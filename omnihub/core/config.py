"""Layered routing configuration.

Precedence, lowest to highest:

1. built-in defaults
2. ``~/.omnihub/config/routing.yaml``
3. ``./.omnihub/config/routing.yaml``
4. ``$OMNIHUB_ROUTING``
5. an explicitly supplied path

Only the merged result is schema-validated, so each layer may be a partial
override. Security policy is deliberately not part of this file.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

DEFAULT_ROUTING: dict[str, Any] = {
    "version": 1,
    "mode": "first_success",
    "intents": {},
    "platforms": {},
    "aggregate": {
        "top": 3,
        "per_platform_limit": 5,
        "dedupe": True,
    },
}


class ConfigError(Exception):
    """An unreadable or schema-invalid routing layer."""

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        self.message = message
        super().__init__(f"{self.path}: {message}")


def _validator() -> Draft202012Validator:
    with (SCHEMA_DIR / "routing.schema.json").open(encoding="utf-8") as handle:
        return Draft202012Validator(json.load(handle))


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings; lists and scalars replace wholesale."""
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_layer(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(path, f"invalid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(path, "routing config must be a YAML mapping")
    return dict(data)


def config_dir(*, home: Optional[Path | str] = None) -> Path:
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".omnihub" / "config"


def state_dir(*, home: Optional[Path | str] = None) -> Path:
    """Writable runtime state (cooldowns, pauses); override with OMNIHUB_STATE_DIR."""
    override = os.environ.get("OMNIHUB_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".omnihub" / "state"


def project_config_dir(project_dir: Optional[Path | str] = None) -> Path:
    base = Path(project_dir).expanduser() if project_dir is not None else Path.cwd()
    return base / ".omnihub" / "config"


def routing_candidates(
    *, home: Optional[Path | str] = None, project_dir: Optional[Path | str] = None
) -> list[Path]:
    """Config files in increasing precedence, excluding CLI overrides."""
    candidates = [config_dir(home=home) / "routing.yaml"]
    candidates.append(project_config_dir(project_dir) / "routing.yaml")
    env_path = os.environ.get("OMNIHUB_ROUTING")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    return candidates


def validate_routing(data: Mapping[str, Any], *, path: Path | str = "<merged>") -> None:
    errors = sorted(
        _validator().iter_errors(data),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ConfigError(path, details)


def load_routing(
    *,
    home: Optional[Path | str] = None,
    project_dir: Optional[Path | str] = None,
    path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Load and merge every routing layer, then validate the result."""
    merged = copy.deepcopy(DEFAULT_ROUTING)
    for candidate in routing_candidates(home=home, project_dir=project_dir):
        if candidate.is_file():
            merged = deep_merge(merged, _read_layer(candidate))
    if path is not None:
        explicit = Path(path).expanduser()
        if not explicit.is_file():
            raise ConfigError(explicit, "routing config not found")
        merged = deep_merge(merged, _read_layer(explicit))
    validate_routing(merged, path=path or "<merged>")
    return merged
