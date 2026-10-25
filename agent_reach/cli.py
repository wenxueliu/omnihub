"""Agent-facing CLI. JSON is the default output; ``--text`` is for humans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_reach import __version__
from agent_reach.adapters.registry import build_adapters
from agent_reach.core.config import ConfigError, load_routing
from agent_reach.core.context import Context
from agent_reach.core.doctor import build_doctor_report
from agent_reach.core.enrich import enrich_items
from agent_reach.core.pipeline import run_action
from agent_reach.core.plugins import inspect_roots
from agent_reach.core.registry import Registry, default_trusted_plugins, discover
from agent_reach.core.result import BAD_INPUT, ReachError, envelope
from agent_reach.core.router import RoutingError, build_plan
from agent_reach.core.skill import render_skill


def _trusted_plugins(args: argparse.Namespace) -> set[str]:
    trusted = set(default_trusted_plugins())
    trusted.update(getattr(args, "trust_plugin", None) or ())
    return trusted


def _discover(args: argparse.Namespace) -> Registry:
    plugin_dirs = [Path(entry) for entry in args.plugin_dir] if args.plugin_dir else None
    return discover(
        plugin_dirs=plugin_dirs,
        include_builtin=not args.no_builtin,
        trusted_plugins=_trusted_plugins(args),
    )


def _error_payload(
    code: str, message: str, *, hint: str = "", extra: Optional[dict] = None
) -> dict:
    payload = {
        "schema_version": 1,
        "ok": False,
        "error": {"code": code, "message": message, "hint": hint},
    }
    if extra:
        payload.update(extra)
    return payload


def _emit(payload: dict, *, text: bool, render_text) -> None:
    if text:
        render_text(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _platforms_text(payload: dict) -> None:
    for platform in payload.get("platforms", []):
        actions = ",".join(platform["actions"])
        print(
            f"{platform['id']:<20} {platform['level']:<12} "
            f"{platform['priority']:>5}  {actions:<16} {platform['description']}"
        )
    for error in payload.get("errors", []):
        print(f"invalid: {error['path']}: {error['message']}", file=sys.stderr)


def _cmd_platforms(args: argparse.Namespace) -> int:
    registry = _discover(args)
    catalog = registry.catalog()
    errors = [
        {"path": str(error.path), "message": error.message}
        for error in registry.errors
    ]
    payload = {
        "schema_version": 1,
        "ok": not errors,
        "platforms": catalog,
        "errors": errors,
        "meta": {"count": len(catalog), "error_count": len(errors)},
        "error": None,
    }
    if errors:
        payload["error"] = {
            "code": BAD_INPUT,
            "message": f"{len(errors)} invalid platform manifest(s)",
            "hint": "Fix or remove the listed platform directories, then run the command again.",
        }
    _emit(payload, text=args.text, render_text=_platforms_text)
    return 0 if not errors else 2


def _doctor_text(payload: dict) -> None:
    for platform in payload.get("platforms", []):
        actions = ",".join(platform.get("actions") or [])
        print(f"{platform['id']:<20} {platform['status']:<9} {actions}")
        for tool in platform.get("tools") or []:
            label = "required" if tool.get("required") else "optional"
            print(f"  tool {tool['name']:<14} {label:<8} {tool.get('detail') or ''}")
        for adapter in platform.get("adapters") or []:
            if adapter.get("ok"):
                state = "ok"
            else:
                state = "missing" if adapter.get("required", True) else "optional"
            detail = adapter.get("detail") or ""
            print(f"  adapter {adapter['kind']:<11} {state:<8} {detail}")
        for secret in platform.get("secrets") or []:
            print(
                f"  secret {secret['name']:<12} "
                f"{'set' if secret.get('ok') else 'missing'}"
            )
        auth = platform.get("auth") or {}
        if auth.get("detail") and auth.get("detail") != "no login required":
            state = {True: "ok", False: "failed", None: "unknown"}[auth.get("ok")]
            print(f"  auth {'':<17} {state:<8} {auth.get('detail') or ''}")
    for error in payload.get("errors", []):
        print(f"invalid: {error['path']}: {error['message']}", file=sys.stderr)
    meta = payload.get("meta") or {}
    print(
        f"{meta.get('count', 0)} platform(s), {meta.get('error_count', 0)} error(s), "
        f"{meta.get('degraded_count', 0)} degraded",
        file=sys.stderr,
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    registry = _discover(args)
    try:
        context = Context()
    except ReachError as exc:
        _emit(
            _error_payload(exc.code, exc.message, hint=exc.hint or ""),
            text=args.text,
            render_text=lambda payload: print(payload["error"]["message"], file=sys.stderr),
        )
        return 1
    report = build_doctor_report(
        registry,
        ctx=context,
        adapters=build_adapters(context=context),
    )
    _emit(report, text=args.text, render_text=_doctor_text)
    return 0 if report["ok"] else 1


def _plan_text(payload: dict) -> None:
    plan = payload.get("plan") or {}
    print(f"mode:      {plan.get('mode')}")
    print(f"platforms: {' -> '.join(plan.get('platforms') or [])}")
    print(f"reason:    {plan.get('reason')}")
    if plan.get("top") is not None:
        print(f"top:       {plan['top']}")
    for warning in plan.get("warnings") or []:
        print(f"warning:   {warning}", file=sys.stderr)
    for platform in payload.get("platforms") or []:
        print()
        print(
            f"{platform['id']} (priority {platform['priority']}, "
            f"{platform.get('origin') or 'unknown'})"
        )
        for action_name, action in (platform.get("actions") or {}).items():
            print(f"  {action_name}")
            for source in action.get("sources") or []:
                label = source.get("name") or source.get("tool") or source.get("call")
                bits = [f"adapter {source.get('adapter')}"]
                if source.get("when"):
                    bits.append(f"when {source['when']}")
                if source.get("disabled"):
                    bits.append("disabled")
                if source.get("parse"):
                    bits.append(f"parse {' -> '.join(source['parse'])}")
                print(f"    {source.get('priority'):>4}  {label:<20} {', '.join(bits)}")


def _cmd_explain(args: argparse.Namespace) -> int:
    try:
        routing = load_routing(path=args.routing)
    except ConfigError as exc:
        _emit(
            _error_payload(BAD_INPUT, str(exc)),
            text=args.text,
            render_text=lambda payload: print(payload["error"]["message"], file=sys.stderr),
        )
        return 2

    registry = _discover(args)
    if registry.errors:
        errors = [
            {"path": str(error.path), "message": error.message}
            for error in registry.errors
        ]
        payload = _error_payload(
            BAD_INPUT,
            f"{len(errors)} invalid platform manifest(s)",
            hint="Run `reach-platforms platforms` for details.",
            extra={"errors": errors},
        )
        _emit(
            payload,
            text=args.text,
            render_text=lambda value: _platforms_text(
                {"platforms": [], "errors": value.get("errors", [])}
            ),
        )
        return 2

    try:
        plan = build_plan(
            registry,
            routing,
            platform=args.platform,
            intent=args.intent,
            url=args.url,
            action=args.action,
            mode=args.mode,
        )
    except RoutingError as exc:
        _emit(
            _error_payload(BAD_INPUT, str(exc)),
            text=args.text,
            render_text=lambda payload: print(payload["error"]["message"], file=sys.stderr),
        )
        return 2

    catalog = {entry["id"]: entry for entry in registry.catalog()}
    selected = []
    for platform_id in plan.platform_ids:
        entry = catalog.get(platform_id)
        if entry is None:
            continue
        if args.action:
            entry = {
                **entry,
                "actions": {
                    name: value
                    for name, value in entry["actions"].items()
                    if name == args.action
                },
            }
        selected.append(entry)

    payload = {
        "schema_version": 1,
        "ok": True,
        "plan": plan.as_dict(),
        "platforms": selected,
        "error": None,
    }
    _emit(payload, text=args.text, render_text=_plan_text)
    return 0


def _plugins_text(payload: dict) -> None:
    for plugin in payload.get("plugins") or []:
        state = "ok" if plugin.get("ok") else "invalid"
        trust = "trusted" if plugin.get("trusted") else "untrusted"
        hook_names = plugin.get("hooks") or plugin.get("code_hooks") or []
        hooks = f" hooks={','.join(hook_names)}" if hook_names else ""
        print(f"{plugin.get('id') or '(unknown)':<20} {state:<8} {trust:<10}{hooks}")
        if plugin.get("path"):
            print(f"  {plugin['path']}")
        for error in plugin.get("errors") or []:
            print(f"  error: {error}", file=sys.stderr)
        for warning in plugin.get("warnings") or []:
            print(f"  warning: {warning}", file=sys.stderr)
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            print(f"invalid: {error.get('path')}: {error.get('message')}", file=sys.stderr)
    meta = payload.get("meta") or {}
    print(
        f"{meta.get('count', 0)} plugin(s), {meta.get('error_count', 0)} error(s), "
        f"{meta.get('warning_count', 0)} warning(s)",
        file=sys.stderr,
    )


def _cmd_plugin(args: argparse.Namespace) -> int:
    if args.plugin_command == "list":
        registry = _discover(args)
        plugins = [
            entry for entry in registry.catalog() if entry.get("origin") == "plugin"
        ]
        errors = [
            {"path": str(error.path), "message": error.message}
            for error in registry.errors
        ]
        payload = {
            "schema_version": 1,
            "ok": not errors,
            "command": "plugin list",
            "plugins": plugins,
            "errors": errors,
            "meta": {"count": len(plugins), "error_count": len(errors), "warning_count": 0},
            "error": None,
        }
        if errors:
            payload["error"] = {
                "code": BAD_INPUT,
                "message": f"{len(errors)} invalid platform manifest(s)",
                "hint": "Run `reach-platforms plugin validate` for details.",
            }
        _emit(payload, text=args.text, render_text=_plugins_text)
        return 0 if not errors else 2

    roots = args.paths or args.plugin_dir
    if not roots:
        from agent_reach.core.registry import default_plugin_dirs

        roots = [str(directory) for directory in default_plugin_dirs()]
    reports = inspect_roots(roots, trusted_plugins=_trusted_plugins(args))
    plugins = [report.as_dict() for report in reports]
    errors = [
        {"path": plugin["path"], "message": message}
        for plugin in plugins
        for message in plugin["errors"]
    ]
    for path in args.paths or ():
        if not Path(path).expanduser().exists():
            errors.append({"path": str(path), "message": "path does not exist"})
    warnings = [warning for report in reports for warning in report.warnings]
    payload = {
        "schema_version": 1,
        "ok": not errors,
        "command": "plugin validate",
        "plugins": plugins,
        "errors": errors,
        "meta": {
            "count": len(plugins),
            "error_count": len(errors),
            "warning_count": len(warnings),
        },
        "error": None,
    }
    if errors:
        payload["error"] = {
            "code": BAD_INPUT,
            "message": f"{len(errors)} invalid plugin manifest(s)",
            "hint": "Fix the listed platform.yaml files and run the command again.",
        }
    _emit(payload, text=args.text, render_text=_plugins_text)
    return 0 if not errors else 2


def _cmd_skill(args: argparse.Namespace) -> int:
    registry = _discover(args)
    document = render_skill(registry)
    if args.output:
        try:
            Path(args.output).expanduser().write_text(document, encoding="utf-8")
        except OSError as exc:
            _emit(
                _error_payload(BAD_INPUT, f"cannot write {args.output}: {exc}"),
                text=True,
                render_text=lambda payload: print(
                    payload["error"]["message"], file=sys.stderr
                ),
            )
            return 2
    else:
        print(document)
    for error in registry.errors:
        print(f"invalid: {error.path}: {error.message}", file=sys.stderr)
    return 0 if not registry.errors else 2


def _result_text(payload: dict) -> None:
    if not payload.get("ok"):
        error = payload.get("error") or {}
        print(f"{error.get('code')}: {error.get('message')}", file=sys.stderr)
        if error.get("hint"):
            print(f"hint: {error['hint']}", file=sys.stderr)
        for attempt in (payload.get("meta") or {}).get("attempts") or []:
            detail = attempt.get("error") or attempt.get("result")
            print(
                f"  {attempt.get('platform')}/{attempt.get('backend')}: {detail}",
                file=sys.stderr,
            )
        for item in (payload.get("meta") or {}).get("errors") or []:
            if isinstance(item, dict):
                print(
                    f"  manifest {item.get('path')}: {item.get('message')}",
                    file=sys.stderr,
                )
        return
    items = payload.get("items") or []
    if not items:
        print("(no results)")
        return
    read_mode = payload.get("action") == "read"
    for item in items:
        print(item.get("title") or "(untitled)")
        if item.get("url"):
            print(f"  {item['url']}")
        if item.get("platform"):
            print(f"  from {item['platform']}/{item.get('source') or '-'}")
        text = item.get("text")
        if text:
            if read_mode:
                print(text)
            else:
                print(" ".join(str(text).split())[:400])
        print()
    meta = payload.get("meta") or {}
    if meta.get("enriched"):
        print(f"enriched {meta['enriched']} result(s)", file=sys.stderr)
    for entry in meta.get("enrich_errors") or []:
        print(
            f"  enrich {entry.get('url')}: {entry.get('code')} {entry.get('message')}",
            file=sys.stderr,
        )


def _action_error(
    action: str,
    code: str,
    message: str,
    *,
    hint: str = "",
    text: bool = False,
    meta: Optional[dict] = None,
) -> int:
    payload = envelope(
        channel=None,
        action=action,
        error=ReachError(code, message, hint),
        meta=meta,
    )
    _emit(payload, text=text, render_text=_result_text)
    return 2


def _prepare_action(
    args: argparse.Namespace, action: str
) -> tuple[Optional[Registry], Optional[dict], int]:
    try:
        routing = load_routing(path=args.routing)
    except ConfigError as exc:
        return None, None, _action_error(action, BAD_INPUT, str(exc), text=args.text)
    registry = _discover(args)
    if registry.errors:
        errors = [
            {"path": str(error.path), "message": error.message}
            for error in registry.errors
        ]
        payload = envelope(
            channel=None,
            action=action,
            error=ReachError(
                BAD_INPUT,
                f"{len(errors)} invalid platform manifest(s)",
                "Run `reach-platforms platforms` for details.",
            ),
            meta={"errors": errors},
        )
        _emit(payload, text=args.text, render_text=_result_text)
        return None, None, 2
    return registry, routing, 0


def _exit_code(payload: dict) -> int:
    if payload.get("ok"):
        return 0
    code = (payload.get("error") or {}).get("code")
    return 2 if code == BAD_INPUT else 1


def _make_context(args: argparse.Namespace) -> tuple[Optional[Context], Optional[int]]:
    try:
        return Context(timeout=args.timeout), None
    except ReachError as exc:
        return None, _action_error(
            getattr(args, "command", "search"),
            exc.code,
            exc.message,
            hint=exc.hint or "",
            text=args.text,
        )


def _cmd_search(args: argparse.Namespace) -> int:
    registry, routing, code = _prepare_action(args, "search")
    if registry is None or routing is None:
        return code
    context, error_code = _make_context(args)
    if context is None:
        return int(error_code)
    params = {"query": args.query}
    if args.limit is not None:
        params["limit"] = args.limit
    payload = run_action(
        registry,
        routing,
        action="search",
        params=params,
        ctx=context,
        platform=args.platform,
        intent=args.intent,
        mode=args.mode,
    )
    if args.enrich:
        payload = enrich_items(
            payload,
            limit=args.enrich,
            read_one=lambda target: run_action(
                registry,
                routing,
                action="read",
                params={"target": target},
                ctx=context,
                url=target,
                mode="first_success",
            ),
        )
    _emit(payload, text=args.text, render_text=_result_text)
    return _exit_code(payload)


def _cmd_read(args: argparse.Namespace) -> int:
    registry, routing, code = _prepare_action(args, "read")
    if registry is None or routing is None:
        return code
    context, error_code = _make_context(args)
    if context is None:
        return int(error_code)
    payload = run_action(
        registry,
        routing,
        action="read",
        params={"target": args.target},
        ctx=context,
        platform=args.platform,
        url=args.target,
        mode=args.mode,
    )
    _emit(payload, text=args.text, render_text=_result_text)
    return _exit_code(payload)


def _add_action_args(parser: argparse.ArgumentParser, *, search: bool) -> None:
    _add_discovery_args(parser)
    parser.add_argument("--platform", help="Force one platform id.")
    if search:
        parser.add_argument("--intent", help="Named intent from routing config.")
    parser.add_argument(
        "--mode",
        choices=("first_success", "aggregate"),
        help="Override the configured mode.",
    )
    parser.add_argument("--routing", metavar="FILE", help="Extra routing config layer.")
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="Per-source timeout, capped by the shared policy.",
    )
    parser.add_argument(
        "--text", action="store_true", help="Human-readable output instead of JSON."
    )


def _add_discovery_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--plugin-dir",
        action="append",
        metavar="DIR",
        help="Plugin root to scan (repeatable). Defaults to $REACH_PLUGIN_PATH, "
        "~/.agent-reach/plugins and the project plugin directory.",
    )
    parser.add_argument(
        "--no-builtin",
        action="store_true",
        help="Skip the platforms bundled with this package.",
    )
    parser.add_argument(
        "--trust-plugin",
        action="append",
        metavar="ID",
        help="Trust a plugin id so its Python hooks may run (repeatable). "
        "Equivalent to listing it in REACH_TRUSTED_PLUGINS.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reach-platforms",
        description="Config-driven, platform-centric internet access for AI agents.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    platforms = subparsers.add_parser(
        "platforms", help="List discovered platforms and their actions."
    )
    _add_discovery_args(platforms)
    platforms.add_argument(
        "--text", action="store_true", help="Human-readable table instead of JSON."
    )
    platforms.set_defaults(func=_cmd_platforms)

    doctor = subparsers.add_parser(
        "doctor", help="Check platform manifests, tools, adapters and login hints."
    )
    _add_discovery_args(doctor)
    doctor.add_argument(
        "--text", action="store_true", help="Human-readable report instead of JSON."
    )
    doctor.set_defaults(func=_cmd_doctor)

    explain = subparsers.add_parser(
        "explain", help="Show which platforms a request would use."
    )
    _add_discovery_args(explain)
    explain.add_argument("--platform", help="Force one platform id.")
    explain.add_argument("--intent", help="Named intent from routing config.")
    explain.add_argument("--url", help="Route by the URL's declared domains.")
    explain.add_argument("--action", help="Only consider platforms supporting this action.")
    explain.add_argument(
        "--mode",
        choices=("first_success", "aggregate"),
        help="Override the configured mode.",
    )
    explain.add_argument("--routing", metavar="FILE", help="Extra routing config layer.")
    explain.add_argument(
        "--text", action="store_true", help="Human-readable plan and source list."
    )
    explain.set_defaults(func=_cmd_explain)

    plugin = subparsers.add_parser(
        "plugin", help="List or validate third-party platform plugins."
    )
    plugin_sub = plugin.add_subparsers(dest="plugin_command")
    plugin_list = plugin_sub.add_parser(
        "list", help="List discovered plugins (origin: plugin)."
    )
    _add_discovery_args(plugin_list)
    plugin_list.add_argument(
        "--text", action="store_true", help="Human-readable table instead of JSON."
    )
    plugin_list.set_defaults(func=_cmd_plugin)

    plugin_validate = plugin_sub.add_parser(
        "validate", help="Validate plugin directories without running them."
    )
    plugin_validate.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Plugin directory or plugin root. Defaults to the standard plugin roots.",
    )
    _add_discovery_args(plugin_validate)
    plugin_validate.add_argument(
        "--text", action="store_true", help="Human-readable report instead of JSON."
    )
    plugin_validate.set_defaults(func=_cmd_plugin)

    skill = subparsers.add_parser(
        "skill", help="Render an agent-facing SKILL.md from the registry."
    )
    _add_discovery_args(skill)
    skill.add_argument(
        "--output", metavar="FILE", help="Write the document to FILE instead of stdout."
    )
    skill.set_defaults(func=_cmd_skill)

    search = subparsers.add_parser(
        "search", help="Search through the selected platform(s)."
    )
    search.add_argument("query", help="Search query.")
    search.add_argument(
        "--limit", type=int, help="Maximum results per platform (platform default otherwise)."
    )
    search.add_argument(
        "--enrich",
        type=int,
        metavar="N",
        help="Read the first N results that only have a short snippet; "
        "failures are reported in meta.enrich_errors.",
    )
    _add_action_args(search, search=True)
    search.set_defaults(func=_cmd_search)

    read = subparsers.add_parser(
        "read", help="Read a page or item through the selected platform(s)."
    )
    read.add_argument("target", help="Public URL or platform-specific identifier.")
    _add_action_args(read, search=False)
    read.set_defaults(func=_cmd_read)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
