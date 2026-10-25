"""Discover, validate and index platform manifests.

A platform is a directory containing ``platform.yaml``. The directory *is* the
deletion boundary: remove it and the platform disappears from routing with no
shared-code edit. Manifests are validated against a JSON Schema and then
checked for references that only make sense across files (unknown adapters,
missing platform dependencies).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml
from jsonschema import Draft202012Validator

from omnihub.adapters.base import ADAPTER_KINDS
from omnihub.core.scripts import (
    SCRIPT_DIR,
    SCRIPT_SUFFIXES,
    ScriptPathError,
    resolve_script_path,
)
from omnihub.utils.url import host_matches

PACKAGE_DIR = Path(__file__).resolve().parent.parent
BUILTIN_PLATFORM_DIR = PACKAGE_DIR / "platforms"
BUILTIN_TOOL_DIR = PACKAGE_DIR / "tools"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
MANIFEST_NAME = "platform.yaml"
CODE_HOOKS = ("search.py", "read.py", "parse.py", "transcribe.py")
TRUST_ENV = "OMNIHUB_TRUSTED_PLUGINS"

_SCHEMA_CACHE: dict[str, Draft202012Validator] = {}


class ManifestError(Exception):
    """One invalid platform manifest, with the file that caused it."""

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        self.message = message
        super().__init__(f"{self.path}: {message}")


class RegistryError(Exception):
    """Raised in strict mode; keeps every manifest error for the CLI."""

    def __init__(self, errors: Iterable[ManifestError]) -> None:
        self.errors = list(errors)
        super().__init__(f"{len(self.errors)} invalid manifest(s)")


@dataclass(frozen=True)
class Source:
    adapter: str
    name: Optional[str] = None
    tool: Optional[str] = None
    call: Optional[str] = None
    script: Optional[str] = None
    args: Mapping[str, Any] = field(default_factory=dict)
    url: Optional[str] = None
    site: Optional[str] = None
    session: Optional[str] = None
    parse: tuple[str, ...] = ()
    priority: int = 0
    when: Optional[str] = None
    disabled: bool = False
    timeout: Optional[float] = None
    max_bytes: Optional[int] = None
    min_interval: Optional[float] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[Source, ...] = ()
    timeout: Optional[float] = None
    public_web: Any = False

    def enabled_sources(self) -> tuple[Source, ...]:
        """Enabled sources, lowest priority number first; manifest order breaks ties."""
        indexed = [(index, source) for index, source in enumerate(self.sources) if not source.disabled]
        indexed.sort(key=lambda pair: (pair[1].priority, pair[0]))
        return tuple(source for _, source in indexed)


@dataclass(frozen=True)
class PlatformManifest:
    id: str
    description: str
    version: int = 1
    level: str = "default"
    priority: int = 1000
    domains: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    public_web: Any = False
    check: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    requires: Mapping[str, Any] = field(default_factory=dict)
    auth: Mapping[str, Any] = field(default_factory=dict)
    actions: Mapping[str, Action] = field(default_factory=dict)
    path: Path = Path()
    origin: str = "unknown"
    trusted: bool = False
    parse_script: Optional[str] = None
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def supports(self, action: Optional[str]) -> bool:
        return action is None or action in self.actions

    @property
    def code_hooks(self) -> tuple[str, ...]:
        return code_hooks_in(self.path.parent, self.manifest)


def _declared_scripts(manifest: Optional[Mapping[str, Any]]) -> list[str]:
    """Script paths declared by a manifest, in document order."""
    if not manifest:
        return []
    found: list[str] = []
    parse_script = manifest.get("parse_script")
    if isinstance(parse_script, str):
        found.append(parse_script)
    for action in (manifest.get("actions") or {}).values():
        for source in action.get("sources") or ():
            script = source.get("script")
            if isinstance(script, str):
                found.append(script)
    return found


def code_hooks_in(
    directory: Path | str, manifest: Optional[Mapping[str, Any]] = None
) -> tuple[str, ...]:
    """Executable files shipped by a platform directory.

    Legacy root-level hooks come first (kept for third-party compatibility),
    then declared scripts, then anything else under ``scripts/``. Relative
    POSIX paths make the result deterministic in catalogs and plugin reports.
    """
    base = Path(directory)
    found: list[str] = []

    def add(relative: str) -> None:
        if relative not in found:
            found.append(relative)

    for name in CODE_HOOKS:
        if (base / name).is_file():
            add(name)
    for declared in _declared_scripts(manifest):
        add(declared)
    scripts_dir = base / SCRIPT_DIR
    if scripts_dir.is_dir():
        for path in sorted(scripts_dir.rglob("*")):
            if (
                path.is_file()
                and path.suffix in SCRIPT_SUFFIXES
                and "__pycache__" not in path.parts
            ):
                add(path.relative_to(base).as_posix())
    return tuple(found)


@dataclass
class Registry:
    platforms: dict[str, PlatformManifest] = field(default_factory=dict)
    errors: list[ManifestError] = field(default_factory=list)
    tools: dict[str, dict] = field(default_factory=dict)

    def get(self, platform_id: str) -> Optional[PlatformManifest]:
        return self.platforms.get(platform_id)

    def catalog(self) -> list[dict]:
        """Agent-facing metadata; never includes source args or auth values."""
        ordered = sorted(self.platforms.values(), key=lambda p: (p.priority, p.id))
        return [_catalog_entry(platform) for platform in ordered]

    def for_url(self, url: str, *, action: Optional[str] = None) -> list[PlatformManifest]:
        """Platforms whose declared domains own *url*, highest priority first."""
        matches = []
        for platform in self.platforms.values():
            if not platform.domains or not platform.supports(action):
                continue
            if any(host_matches(url, domain) for domain in platform.domains):
                matches.append(platform)
        matches.sort(key=lambda p: (p.priority, p.id))
        return matches


def _validator(kind: str) -> Draft202012Validator:
    if kind not in _SCHEMA_CACHE:
        schema_path = SCHEMA_DIR / f"{kind}.schema.json"
        with schema_path.open(encoding="utf-8") as handle:
            _SCHEMA_CACHE[kind] = Draft202012Validator(json.load(handle))
    return _SCHEMA_CACHE[kind]


def _format_schema_error(error: Any) -> str:
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


def _as_parse_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(entry) for entry in value)


def _parse_source(data: Mapping[str, Any]) -> Source:
    return Source(
        adapter=str(data["adapter"]),
        name=data.get("name"),
        tool=data.get("tool"),
        call=data.get("call"),
        script=data.get("script"),
        args=dict(data.get("args") or {}),
        url=data.get("url"),
        site=data.get("site"),
        session=data.get("session"),
        parse=_as_parse_tuple(data.get("parse")),
        priority=int(data.get("priority") or 0),
        when=data.get("when"),
        disabled=bool(data.get("disabled", False)),
        timeout=float(data["timeout"]) if data.get("timeout") is not None else None,
        max_bytes=int(data["max_bytes"]) if data.get("max_bytes") is not None else None,
        min_interval=(
            float(data["min_interval"]) if data.get("min_interval") is not None else None
        ),
        raw=dict(data),
    )


def _parse_action(name: str, data: Mapping[str, Any], platform_public_web: Any) -> Action:
    return Action(
        name=name,
        params=dict(data.get("params") or {}),
        sources=tuple(_parse_source(source) for source in data.get("sources") or ()),
        timeout=float(data["timeout"]) if data.get("timeout") is not None else None,
        public_web=data.get("public_web", platform_public_web),
    )


def _parse_platform(data: Mapping[str, Any], manifest_path: Path, origin: str) -> PlatformManifest:
    platform_public_web = data.get("public_web", False)
    actions = {
        name: _parse_action(name, action_data, platform_public_web)
        for name, action_data in (data.get("actions") or {}).items()
    }
    return PlatformManifest(
        id=str(data["id"]),
        description=str(data["description"]),
        version=int(data.get("version", 1)),
        level=str(data.get("level", "default")),
        priority=int(data.get("priority", 1000)),
        domains=tuple(str(domain) for domain in data.get("domains") or ()),
        capabilities=tuple(str(capability) for capability in data.get("capabilities") or ()),
        public_web=platform_public_web,
        check={
            str(name): dict(value)
            for name, value in (data.get("check") or {}).items()
        },
        requires=dict(data.get("requires") or {}),
        auth=dict(data.get("auth") or {}),
        actions=actions,
        path=manifest_path,
        origin=origin,
        parse_script=data.get("parse_script"),
        manifest=dict(data),
    )


def _check_references(data: Mapping[str, Any], manifest_path: Path) -> None:
    for action_name, action_data in (data.get("actions") or {}).items():
        for index, source in enumerate(action_data.get("sources") or ()):
            adapter = source.get("adapter")
            if adapter not in ADAPTER_KINDS:
                raise ManifestError(
                    manifest_path,
                    f"actions.{action_name}.sources[{index}]: unknown adapter {adapter!r} "
                    f"(known: {', '.join(sorted(ADAPTER_KINDS))})",
                )
    for adapter in (data.get("requires") or {}).get("adapters") or ():
        if adapter not in ADAPTER_KINDS:
            raise ManifestError(
                manifest_path, f"requires.adapters: unknown adapter {adapter!r}"
            )


def _validate_script(
    value: Any,
    manifest_path: Path,
    location: str,
    suffixes: tuple[str, ...],
) -> None:
    """Validate one declared script path and require the file to exist."""
    try:
        path = resolve_script_path(manifest_path.parent, str(value), must_exist=False)
    except ScriptPathError as exc:
        raise ManifestError(manifest_path, f"{location}: {exc}") from None
    if path.suffix not in suffixes:
        raise ManifestError(
            manifest_path,
            f"{location}: script must end in {', '.join(suffixes)}",
        )
    if not path.is_file():
        raise ManifestError(manifest_path, f"{location}: script not found: {value}")


def _check_scripts(data: Mapping[str, Any], manifest_path: Path) -> None:
    """Resolve every declared script at load time.

    A missing or escaping path is a manifest error, so ``platforms``,
    ``explain``, ``plugin validate`` and ``doctor`` all surface it before an
    action runs. Only ``builtin`` Python modules and ``daemon`` browse
    JavaScript may be referenced.
    """
    parse_script = data.get("parse_script")
    if parse_script is not None:
        _validate_script(parse_script, manifest_path, "parse_script", (".py",))

    for action_name, action_data in (data.get("actions") or {}).items():
        for index, source in enumerate(action_data.get("sources") or ()):
            script = source.get("script")
            if script is None:
                continue
            location = f"actions.{action_name}.sources[{index}].script"
            adapter = source.get("adapter")
            call = source.get("call")
            if adapter == "builtin":
                suffixes = (".py",)
            elif adapter == "daemon" and call == "browse":
                suffixes = (".js",)
            else:
                raise ManifestError(
                    manifest_path,
                    f"{location}: script is only supported by builtin sources "
                    "or daemon browse sources",
                )
            if (source.get("args") or {}).get("code") is not None:
                raise ManifestError(
                    manifest_path,
                    f"{location}: a source cannot declare both script and args.code",
                )
            _validate_script(script, manifest_path, location, suffixes)


def _check_code_hooks(
    data: Mapping[str, Any], manifest_path: Path, origin: str
) -> None:
    """Third-party code hooks must declare what they are allowed to touch.

    Declarative plugins are safe to load. Python hooks run with the user's
    privileges, so a plugin that ships one has to say up front whether it
    needs ``http``, ``exec`` or ``browser`` -- and the user still has to trust
    the plugin id before anything is executed.
    """
    if origin != "plugin":
        return
    hooks = list(code_hooks_in(manifest_path.parent, data))
    if hooks and "capabilities" not in data:
        raise ManifestError(
            manifest_path,
            f"plugin ships Python hooks ({', '.join(hooks)}) but declares no "
            "capabilities; add `capabilities: []` or list http/exec/browser",
        )


def default_trusted_plugins(environ: Optional[Mapping[str, str]] = None) -> frozenset[str]:
    """Plugin ids explicitly trusted through ``OMNIHUB_TRUSTED_PLUGINS``."""
    source = os.environ if environ is None else environ
    raw = source.get(TRUST_ENV, "")
    return frozenset(
        entry.strip()
        for entry in raw.replace(os.pathsep, ",").split(",")
        if entry.strip()
    )


def load_platform(path: Path | str, *, origin: str = "unknown") -> PlatformManifest:
    """Load and validate one manifest from a directory or a ``platform.yaml``."""
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ManifestError(manifest_path, "platform.yaml not found")

    try:
        with manifest_path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ManifestError(manifest_path, f"invalid YAML: {exc}") from None

    if not isinstance(data, Mapping):
        raise ManifestError(manifest_path, "manifest must be a YAML mapping")

    schema_errors = sorted(
        _validator("platform").iter_errors(data),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if schema_errors:
        raise ManifestError(
            manifest_path, "; ".join(_format_schema_error(error) for error in schema_errors)
        )

    _check_references(data, manifest_path)
    _check_scripts(data, manifest_path)
    _check_code_hooks(data, manifest_path, origin)
    return _parse_platform(data, manifest_path, origin)


def default_plugin_dirs(
    *, home: Optional[Path | str] = None, project_dir: Optional[Path | str] = None
) -> list[Path]:
    """Plugin roots in increasing precedence: env, user, project."""
    dirs: list[Path] = []
    for entry in os.environ.get("OMNIHUB_PLUGIN_PATH", "").split(os.pathsep):
        if entry.strip():
            dirs.append(Path(entry).expanduser())
    base = Path(home).expanduser() if home is not None else Path.home()
    dirs.append(base / ".omnihub" / "plugins")
    project_base = Path(project_dir).expanduser() if project_dir is not None else Path.cwd()
    dirs.append(project_base / ".omnihub" / "plugins")
    return dirs


def _scan_root(root: Path, origin: str) -> tuple[dict[str, PlatformManifest], list[ManifestError]]:
    found: dict[str, PlatformManifest] = {}
    errors: list[ManifestError] = []
    if not root.is_dir():
        return found, errors

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            platform = load_platform(manifest_path, origin=origin)
        except ManifestError as exc:
            errors.append(exc)
            continue
        if child.name != platform.id:
            errors.append(
                ManifestError(
                    manifest_path,
                    f"directory name {child.name!r} must equal platform id {platform.id!r}",
                )
            )
            continue
        found[platform.id] = platform
    return found, errors


def discover(
    *,
    plugin_dirs: Optional[Iterable[Path | str]] = None,
    include_builtin: bool = True,
    trusted_plugins: Optional[Iterable[str]] = None,
    strict: bool = False,
) -> Registry:
    """Scan built-in and plugin directories.

    Duplicate ids inside one root are an error. A later root overrides an
    earlier one, so a project can deliberately shadow a built-in platform.
    """
    roots: list[tuple[str, Path]] = []
    if include_builtin:
        roots.append(("builtin", BUILTIN_PLATFORM_DIR))
    resolved_plugins = (
        list(plugin_dirs) if plugin_dirs is not None else default_plugin_dirs()
    )
    for plugin_dir in resolved_plugins:
        roots.append(("plugin", Path(plugin_dir).expanduser()))

    platforms: dict[str, PlatformManifest] = {}
    origins: dict[str, Path] = {}
    errors: list[ManifestError] = []

    for origin, root in roots:
        found, root_errors = _scan_root(root, origin)
        errors.extend(root_errors)
        for platform_id, platform in found.items():
            if platform_id in platforms:
                if origins[platform_id] == root:
                    errors.append(
                        ManifestError(
                            platform.path,
                            f"duplicate platform id {platform_id!r} in {root}",
                        )
                    )
                    continue
                # Later roots override earlier ones (project over user over builtin).
            platforms[platform_id] = platform
            origins[platform_id] = root

    missing: dict[str, list[str]] = {}
    for platform_id, platform in list(platforms.items()):
        absent = [
            dependency
            for dependency in platform.requires.get("platforms") or ()
            if dependency not in platforms
        ]
        if absent:
            missing[platform_id] = absent
            errors.append(
                ManifestError(
                    platform.path,
                    f"requires missing platform(s): {', '.join(absent)}",
                )
            )
    for platform_id in missing:
        platforms.pop(platform_id, None)

    trusted = (
        frozenset(trusted_plugins)
        if trusted_plugins is not None
        else default_trusted_plugins()
    )
    for platform_id, platform in list(platforms.items()):
        if platform.origin == "builtin" or platform_id in trusted:
            platforms[platform_id] = replace(platform, trusted=True)

    registry = Registry(platforms=platforms, errors=errors, tools=discover_tools())
    if strict and errors:
        raise RegistryError(errors)
    return registry


def discover_tools(tool_dirs: Optional[Iterable[Path | str]] = None) -> dict[str, dict]:
    """Load optional tool manifests from ``omnihub/tools/*.yaml``."""
    roots = (
        [Path(directory).expanduser() for directory in tool_dirs]
        if tool_dirs is not None
        else [BUILTIN_TOOL_DIR]
    )
    tools: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for manifest_path in sorted(root.glob("*.yaml")):
            try:
                with manifest_path.open(encoding="utf-8") as handle:
                    data = yaml.safe_load(handle)
            except yaml.YAMLError:
                continue
            if not isinstance(data, Mapping):
                continue
            errors = list(_validator("tool").iter_errors(data))
            if errors or data.get("id") != manifest_path.stem:
                continue
            tools[str(data["id"])] = dict(data)
    return tools


def _catalog_entry(platform: PlatformManifest) -> dict:
    return {
        "id": platform.id,
        "description": platform.description,
        "version": platform.version,
        "level": platform.level,
        "priority": platform.priority,
        "domains": list(platform.domains),
        "capabilities": list(platform.capabilities),
        "public_web": platform.public_web,
        "check": sorted(platform.check),
        "requires": dict(platform.requires),
        "origin": platform.origin,
        "trusted": platform.trusted,
        "parse_script": platform.parse_script,
        "code_hooks": list(platform.code_hooks),
        "actions": {
            name: {
                "params": dict(action.params),
                "timeout": action.timeout,
                "public_web": action.public_web,
                "sources": [
                    {
                        "adapter": source.adapter,
                        "name": source.name,
                        "tool": source.tool,
                        "call": source.call,
                        "script": source.script,
                        "priority": source.priority,
                        "parse": list(source.parse),
                        "when": source.when,
                        "disabled": source.disabled,
                    }
                    for source in action.sources
                ],
            }
            for name, action in sorted(platform.actions.items())
        },
        "path": str(platform.path),
    }
