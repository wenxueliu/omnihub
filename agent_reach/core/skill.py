"""Render an agent-facing SKILL.md from the discovered platforms.

The registry is the single source of truth: add or delete a platform
directory, re-run ``reach-platforms skill``, and the document follows. The
rendered text never contains credentials, source ``args`` or auth values.
"""

from __future__ import annotations

from typing import Any

from agent_reach.core.registry import PlatformManifest, Registry

_CLI = "python -m agent_reach.cli"


def _param_line(name: str, spec: Any) -> str:
    if not isinstance(spec, dict):
        return f"    - `{name}`"
    kind = spec.get("type") or "str"
    notes = []
    if spec.get("required"):
        notes.append("required")
    if "default" in spec:
        notes.append(f"default: `{spec['default']}`")
    suffix = f" ({', '.join(notes)})" if notes else ""
    description = f" — {spec['description']}" if spec.get("description") else ""
    return f"    - `{name}`: {kind}{suffix}{description}"


def _platform_section(platform: PlatformManifest) -> list[str]:
    lines = [
        f"### {platform.id}",
        "",
        platform.description,
        "",
        f"- priority: {platform.priority}",
        f"- level: {platform.level}",
        f"- origin: {platform.origin}"
        + (" (trusted)" if platform.trusted and platform.origin != "builtin" else ""),
    ]
    if platform.domains:
        lines.append(f"- domains: {', '.join(platform.domains)}")
    if platform.capabilities:
        lines.append(f"- capabilities: {', '.join(platform.capabilities)}")
    lines.append("")

    for action_name in sorted(platform.actions):
        action = platform.actions[action_name]
        lines.append(f"**{action_name}**")
        lines.append("")
        if action.params:
            lines.append("Parameters:")
            lines.extend(_param_line(name, spec) for name, spec in action.params.items())
            lines.append("")
        if action.sources:
            lines.append("Sources (tried in priority order):")
            for source in action.enabled_sources():
                label = source.name or source.tool or source.call or source.adapter
                detail = [f"adapter `{source.adapter}`"]
                if source.when:
                    detail.append(f"when `{source.when}`")
                if source.parse:
                    detail.append(f"parse `{' -> '.join(source.parse)}`")
                lines.append(
                    f"    - `{label}` ({', '.join(detail)}), priority {source.priority}"
                )
            lines.append("")
    return lines


def render_skill(registry: Registry, *, cli: str = _CLI) -> str:
    """Markdown for agents: commands, platform table and per-platform detail."""
    platforms = sorted(
        registry.platforms.values(), key=lambda p: (p.priority, p.id)
    )
    lines = [
        "# reach-platforms",
        "",
        "Platform-centric internet access for agents. Every platform is a "
        "delete-able directory; the commands below are the whole interface.",
        "",
        "## Commands",
        "",
        "```bash",
        f"{cli} platforms --text",
        f"{cli} doctor --text",
        f"{cli} explain --platform web --action search",
        f'{cli} search "query" --limit 5',
        f'{cli} search "query" --enrich 2 --platform web',
        f"{cli} read https://example.com/page --text",
        "```",
        "",
        "JSON is the default output (unified envelope: `ok`, `channel`, "
        "`action`, `backend`, `items`, `meta`, `error`). Use `--text` for "
        "humans. `search --enrich N` reads the first N short results and "
        "reports failures in `meta.enrich_errors`.",
        "",
        "## Platforms",
        "",
        "Lower priority numbers are tried first; `--platform` narrows to one "
        "platform and URLs are routed by their declared domains.",
        "",
        "| platform | priority | actions | domains | description |",
        "|---|---|---|---|---|",
    ]
    for platform in platforms:
        actions = ", ".join(sorted(platform.actions))
        domains = ", ".join(platform.domains) or "-"
        description = platform.description.replace("|", "\\|")
        lines.append(
            f"| {platform.id} | {platform.priority} | {actions} | {domains} | {description} |"
        )
    lines.append("")

    if registry.errors:
        lines.append("## Invalid platforms")
        lines.append("")
        for error in registry.errors:
            lines.append(f"- `{error.path}`: {error.message}")
        lines.append("")

    lines.append("## Platform details")
    lines.append("")
    for platform in platforms:
        lines.extend(_platform_section(platform))

    lines.extend(
        [
            "## Plugins",
            "",
            "Extra platforms live in `$REACH_PLUGIN_PATH`, "
            "`~/.agent-reach/plugins/<id>/` or `./.agent-reach/plugins/<id>/`. "
            "Validate one with `reach-platforms plugin validate <dir>`.",
            "Declarative plugins load directly. A plugin that ships "
            "`search.py`, `read.py`, `parse.py` or `transcribe.py` must "
            "declare `capabilities` and is only executed after "
            "`--trust-plugin <id>` or "
            "`REACH_TRUSTED_PLUGINS=<id>`.",
            "",
        ]
    )
    return "\n".join(lines)
