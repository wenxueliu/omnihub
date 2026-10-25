"""Inspect third-party plugin directories before they are trusted.

Plugins are ordinary platform directories outside the package. Declarative
ones only need a valid ``platform.yaml``. A plugin that also ships
``search.py`` / ``read.py`` / ``parse.py`` runs arbitrary Python with the
user's privileges, so it must declare capabilities and be trusted explicitly
with ``--trust-plugin <id>`` or ``REACH_TRUSTED_PLUGINS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from agent_reach.core.registry import (
    CODE_HOOKS,
    MANIFEST_NAME,
    ManifestError,
    default_trusted_plugins,
    load_platform,
)


@dataclass
class PluginReport:
    path: Path
    id: Optional[str] = None
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hooks: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    trusted: bool = False
    domains: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "id": self.id,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "hooks": list(self.hooks),
            "capabilities": list(self.capabilities),
            "trusted": self.trusted,
            "domains": list(self.domains),
            "actions": list(self.actions),
        }


def _hooks_in(directory: Path) -> tuple[str, ...]:
    return tuple(name for name in CODE_HOOKS if (directory / name).is_file())


def inspect_plugin(
    path: Path | str, *, trusted: Optional[bool] = None
) -> PluginReport:
    """Validate one plugin directory (or a direct path to its manifest)."""
    directory = Path(path)
    if directory.is_file():
        directory = directory.parent
    report = PluginReport(path=directory, hooks=_hooks_in(directory))

    try:
        platform = load_platform(directory / MANIFEST_NAME, origin="plugin")
    except ManifestError as exc:
        report.errors.append(exc.message)
        return report

    report.id = platform.id
    report.capabilities = platform.capabilities
    report.domains = platform.domains
    report.actions = tuple(sorted(platform.actions))
    report.hooks = platform.code_hooks
    report.trusted = (
        bool(trusted) if trusted is not None else platform.id in default_trusted_plugins()
    )

    if directory.name != platform.id:
        report.errors.append(
            f"directory name {directory.name!r} must equal platform id {platform.id!r}"
        )
    if report.hooks and not report.trusted:
        report.warnings.append(
            f"Python hooks ({', '.join(report.hooks)}) will not run until "
            f"{platform.id!r} is trusted with `--trust-plugin {platform.id}` "
            "or REACH_TRUSTED_PLUGINS"
        )

    report.ok = not report.errors
    return report


def iter_plugin_dirs(root: Path | str) -> list[Path]:
    """Plugin directories under *root*; the root itself may be one plugin."""
    base = Path(root).expanduser()
    if (base / MANIFEST_NAME).is_file():
        return [base]
    if not base.is_dir():
        return []
    return [
        child
        for child in sorted(base.iterdir())
        if child.is_dir() and (child / MANIFEST_NAME).is_file()
    ]


def inspect_roots(
    roots: Iterable[Path | str], *, trusted_plugins: Optional[Iterable[str]] = None
) -> list[PluginReport]:
    trusted = (
        frozenset(trusted_plugins)
        if trusted_plugins is not None
        else default_trusted_plugins()
    )
    reports: list[PluginReport] = []
    for root in roots:
        for directory in iter_plugin_dirs(root):
            reports.append(
                inspect_plugin(directory, trusted=str(directory.name) in trusted)
            )
    return reports
