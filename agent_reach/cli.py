"""Agent-facing CLI. JSON is the default output; ``--text`` is for humans."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_reach import __version__
from agent_reach.core.config import ConfigError, load_routing
from agent_reach.core.context import Context
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import Registry, discover
from agent_reach.core.result import BAD_INPUT, ReachError, envelope
from agent_reach.core.router import RoutingError, build_plan


def _discover(args: argparse.Namespace) -> Registry:
    plugin_dirs = [Path(entry) for entry in args.plugin_dir] if args.plugin_dir else None
    return discover(
        plugin_dirs=plugin_dirs,
        include_builtin=not args.no_builtin,
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


def _plan_text(payload: dict) -> None:
    plan = payload.get("plan") or {}
    print(f"mode:      {plan.get('mode')}")
    print(f"platforms: {' -> '.join(plan.get('platforms') or [])}")
    print(f"reason:    {plan.get('reason')}")
    if plan.get("top") is not None:
        print(f"top:       {plan['top']}")
    for warning in plan.get("warnings") or []:
        print(f"warning:   {warning}", file=sys.stderr)


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

    payload = {
        "schema_version": 1,
        "ok": True,
        "plan": plan.as_dict(),
        "error": None,
    }
    _emit(payload, text=args.text, render_text=_plan_text)
    return 0


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
        "--text", action="store_true", help="Human-readable plan instead of JSON."
    )
    explain.set_defaults(func=_cmd_explain)

    search = subparsers.add_parser(
        "search", help="Search through the selected platform(s)."
    )
    search.add_argument("query", help="Search query.")
    search.add_argument(
        "--limit", type=int, help="Maximum results per platform (platform default otherwise)."
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
