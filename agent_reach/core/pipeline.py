"""Execute one action: plan, try sources, parse, normalize and envelope.

The pipeline is deliberately small. Platforms supply ordered sources; the
pipeline owns fallback semantics, provenance, error mapping and the output
shape. A source failure that is not final moves to the next source; a final
code (login wall, captcha, rate limit, bad input) stops immediately.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from agent_reach.adapters.base import RawResult, ToolCall
from agent_reach.adapters.registry import build_adapters
from agent_reach.core.normalize import dedupe_items, normalize_items
from agent_reach.core.policy import ensure_public_url
from agent_reach.core.result import (
    BAD_INPUT,
    FINAL_CODES,
    NEED_SETUP,
    UPSTREAM_BROKEN,
    ReachError,
    classify,
    envelope,
)
from agent_reach.core.registry import Action, PlatformManifest, Registry, Source
from agent_reach.core.router import Plan, RoutingError, build_plan
from agent_reach.core.template import resolve_argv, resolve_templates
from agent_reach.parse.generic import parse_with_chain

_PARSE_LOCK = threading.Lock()
_PARSE_MODULES: dict[tuple[str, int, int], Any] = {}


@dataclass
class PlatformOutcome:
    platform_id: str
    items: list[dict] = field(default_factory=list)
    backend: Optional[str] = None
    attempts: list[dict] = field(default_factory=list)
    error: Optional[ReachError] = None
    final: bool = False


def source_label(source: Source) -> str:
    return source.name or source.tool or source.call or source.adapter


def prepare_params(
    platform: PlatformManifest,
    action_name: str,
    params: Mapping[str, Any],
    *,
    apply_defaults: bool = True,
) -> dict[str, Any]:
    """Apply declared defaults, coerce primitive types and validate URLs."""
    action = platform.actions.get(action_name)
    if action is None:
        raise ReachError(
            BAD_INPUT, f"platform {platform.id!r} does not support action {action_name!r}"
        )
    resolved = dict(params)
    for name, raw_spec in action.params.items():
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        if name not in resolved:
            if "default" in spec and apply_defaults:
                resolved[name] = spec["default"]
            elif spec.get("required"):
                raise ReachError(
                    BAD_INPUT,
                    f"missing required parameter {name!r} for platform {platform.id!r}",
                )
            else:
                continue
        value = resolved[name]
        if value is None:
            if spec.get("required"):
                raise ReachError(BAD_INPUT, f"parameter {name!r} is required")
            continue
        kind = str(spec.get("type") or "str")
        try:
            if kind == "str":
                value = str(value)
            elif kind == "int":
                value = int(value)
            elif kind == "float":
                value = float(value)
            elif kind == "bool":
                value = (
                    value.lower() in {"1", "true", "yes", "on"}
                    if isinstance(value, str)
                    else bool(value)
                )
            elif kind == "list":
                if isinstance(value, str):
                    value = [value]
                elif isinstance(value, (list, tuple)):
                    value = list(value)
                else:
                    value = [value]
        except (TypeError, ValueError):
            raise ReachError(BAD_INPUT, f"parameter {name!r} must be {kind}") from None
        choices = spec.get("choices")
        if choices and value not in choices:
            allowed = ", ".join(repr(choice) for choice in choices)
            raise ReachError(BAD_INPUT, f"parameter {name!r} must be one of {allowed}")
        if spec.get("format") == "url":
            value = ensure_public_url(str(value))
        resolved[name] = value
    return resolved


def load_platform_module(platform: PlatformManifest, filename: str) -> Optional[Any]:
    """Load a trusted built-in platform module, or return None."""
    if platform.origin != "builtin":
        return None
    path = platform.path.parent / filename
    if not path.is_file():
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _PARSE_LOCK:
        cached = _PARSE_MODULES.get(key)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(
            f"reach_platform_{platform.id}_{path.stem}_{abs(hash(key))}", path
        )
        if spec is None or spec.loader is None:
            raise ReachError(NEED_SETUP, f"cannot load platform module {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PARSE_MODULES[key] = module
        return module


def _custom_parsers(platform: PlatformManifest) -> dict[str, Any]:
    try:
        module = load_platform_module(platform, "parse.py")
    except ReachError:
        raise
    except Exception as exc:
        raise ReachError(NEED_SETUP, f"cannot load platform parser: {exc}") from None
    if module is None:
        return {}
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("_") and callable(value)
    }


def _build_call(
    source: Source,
    platform: PlatformManifest,
    action_name: str,
    params: Mapping[str, Any],
    ctx: Any,
) -> ToolCall:
    raw = dict(source.raw)
    return ToolCall(
        adapter=source.adapter,
        call=source.call,
        action=action_name,
        tool=source.tool,
        args=dict(resolve_templates(dict(source.args), params, ctx) or {}),
        params=dict(params),
        argv=resolve_argv(raw.get("argv"), params, ctx),
        method=str(raw.get("method") or "GET").upper(),
        headers=dict(resolve_templates(dict(raw.get("headers") or {}), params, ctx) or {}),
        body=resolve_templates(raw.get("body"), params, ctx),
        data=resolve_templates(raw.get("data"), params, ctx),
        output=str(raw.get("output") or "json"),
        url=resolve_templates(source.url, params, ctx) if source.url else None,
        site=resolve_templates(source.site, params, ctx) if source.site else None,
        session=resolve_templates(source.session, params, ctx) if source.session else None,
        parse=source.parse,
        timeout=source.timeout,
        max_bytes=source.max_bytes,
        raw={
            "platform_id": platform.id,
            "platform_path": str(platform.path.parent),
            "platform_origin": platform.origin,
            "source_name": source_label(source),
            "source": raw,
        },
    )


def _parse_source(
    raw: RawResult,
    source: Source,
    platform: PlatformManifest,
    action_name: str,
    params: Mapping[str, Any],
    ctx: Any,
) -> list[dict]:
    raw.meta.setdefault("params", dict(params))
    parsed = parse_with_chain(
        raw,
        source.parse,
        custom=_custom_parsers(platform),
        action=action_name,
        ctx=ctx,
    )
    items = normalize_items(
        parsed,
        platform=platform.id,
        source=source_label(source),
        priority=source.priority,
        action=action_name,
        target=params.get("target"),
    )
    if not items and action_name == "read":
        text = (raw.text or "").strip()
        if text:
            items = normalize_items(
                [{"url": params.get("target"), "text": text, "raw": {"content_type": raw.content_type}}],
                platform=platform.id,
                source=source_label(source),
                priority=source.priority,
                action=action_name,
                target=params.get("target"),
            )
    return items


def _attempt(
    platform: PlatformManifest,
    source: Source,
    result: str,
    started: float,
    *,
    error: Optional[ReachError] = None,
) -> dict:
    entry = {
        "platform": platform.id,
        "backend": source_label(source),
        "result": result,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if error is not None:
        entry["code"] = error.code
        entry["error"] = error.message
    return entry


def _try_platform(
    platform: PlatformManifest,
    action_name: str,
    params: Mapping[str, Any],
    *,
    ctx: Any,
    adapters: Mapping[str, Any],
) -> PlatformOutcome:
    action: Optional[Action] = platform.actions.get(action_name)
    if action is None:
        return PlatformOutcome(
            platform_id=platform.id,
            error=ReachError(
                BAD_INPUT,
                f"platform {platform.id!r} does not support action {action_name!r}",
            ),
            final=True,
        )
    try:
        prepared = prepare_params(platform, action_name, params)
    except ReachError as exc:
        return PlatformOutcome(platform_id=platform.id, error=exc, final=True)

    sources = action.enabled_sources()
    if not sources:
        return PlatformOutcome(
            platform_id=platform.id,
            error=ReachError(NEED_SETUP, f"platform {platform.id!r} has no enabled sources"),
        )

    attempts: list[dict] = []
    last_error: Optional[ReachError] = None
    for source in sources:
        started = time.monotonic()
        try:
            adapter = adapters.get(source.adapter)
            if adapter is None:
                raise ReachError(
                    NEED_SETUP,
                    f"adapter {source.adapter!r} is not implemented yet",
                )
            health = adapter.probe()
            if not health.ok:
                raise ReachError(
                    NEED_SETUP,
                    health.detail or f"{source_label(source)} is not available",
                )
            call = _build_call(source, platform, action_name, prepared, ctx)
            raw = adapter.invoke(call, ctx=ctx)
            items = _parse_source(raw, source, platform, action_name, prepared, ctx)
            if not items:
                last_error = ReachError(
                    UPSTREAM_BROKEN, f"{source_label(source)} returned no results"
                )
                attempts.append(_attempt(platform, source, "EMPTY", started))
                continue
            attempts.append(_attempt(platform, source, "OK", started))
            return PlatformOutcome(
                platform_id=platform.id,
                items=items,
                backend=source_label(source),
                attempts=attempts,
            )
        except ReachError as exc:
            last_error = exc
            attempts.append(_attempt(platform, source, "ERROR", started, error=exc))
            if exc.code in FINAL_CODES:
                return PlatformOutcome(
                    platform_id=platform.id,
                    attempts=attempts,
                    error=exc,
                    final=True,
                )
        except Exception as exc:
            error = classify(exc)
            last_error = error
            attempts.append(_attempt(platform, source, "ERROR", started, error=error))
            if error.code in FINAL_CODES:
                return PlatformOutcome(
                    platform_id=platform.id,
                    attempts=attempts,
                    error=error,
                    final=True,
                )

    return PlatformOutcome(
        platform_id=platform.id,
        attempts=attempts,
        error=last_error
        or ReachError(UPSTREAM_BROKEN, f"platform {platform.id!r} produced no result"),
    )


def _meta(plan: Plan, **extra: Any) -> dict:
    payload = {
        "mode": plan.mode,
        "reason": plan.reason,
        "intent": plan.intent,
        "warnings": list(plan.warnings),
    }
    payload.update(extra)
    return payload


def _run_first_success(
    registry: Registry,
    plan: Plan,
    action_name: str,
    params: Mapping[str, Any],
    *,
    ctx: Any,
    adapters: Mapping[str, Any],
    started: float,
) -> dict:
    attempts: list[dict] = []
    last_error: Optional[ReachError] = None
    last_platform: Optional[str] = None
    for platform_id in plan.platform_ids:
        last_platform = platform_id
        outcome = _try_platform(
            registry.platforms[platform_id], action_name, params, ctx=ctx, adapters=adapters
        )
        attempts.extend(outcome.attempts)
        if outcome.items:
            return envelope(
                channel=platform_id,
                action=action_name,
                backend=outcome.backend,
                items=outcome.items,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
                meta=_meta(plan, platform=platform_id),
            )
        if outcome.error is not None:
            last_error = outcome.error
        if outcome.final:
            break

    return envelope(
        channel=last_platform,
        action=action_name,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        error=last_error or ReachError(UPSTREAM_BROKEN, "no platform produced results"),
        meta=_meta(plan),
    )


def _run_aggregate(
    registry: Registry,
    plan: Plan,
    action_name: str,
    params: Mapping[str, Any],
    *,
    ctx: Any,
    adapters: Mapping[str, Any],
    started: float,
    per_platform_limit: int = 5,
    dedupe: bool = True,
) -> dict:
    limit = plan.top or len(plan.platform_ids)
    platform_ids = list(plan.platform_ids[:limit])
    outcomes: dict[str, PlatformOutcome] = {}

    if len(platform_ids) == 1:
        outcomes[platform_ids[0]] = _try_platform(
            registry.platforms[platform_ids[0]], action_name, params, ctx=ctx, adapters=adapters
        )
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(platform_ids))) as pool:
            futures = {
                pool.submit(
                    _try_platform,
                    registry.platforms[platform_id],
                    action_name,
                    params,
                    ctx=ctx,
                    adapters=adapters,
                ): platform_id
                for platform_id in platform_ids
            }
            for future in as_completed(futures):
                platform_id = futures[future]
                try:
                    outcomes[platform_id] = future.result()
                except Exception as exc:  # defensive: a worker must not lose the batch
                    outcomes[platform_id] = PlatformOutcome(
                        platform_id=platform_id, error=classify(exc)
                    )

    ordered = [outcomes[platform_id] for platform_id in platform_ids if platform_id in outcomes]
    attempts = [attempt for outcome in ordered for attempt in outcome.attempts]
    successful = [outcome for outcome in ordered if outcome.items]
    if not successful:
        error = next((outcome.error for outcome in ordered if outcome.error), None)
        return envelope(
            channel="@aggregate",
            action=action_name,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
            error=error or ReachError(UPSTREAM_BROKEN, "no platform produced results"),
            meta=_meta(plan, platforms=[]),
        )

    items = [
        item
        for outcome in ordered
        for item in outcome.items[: max(1, per_platform_limit)]
    ]
    merged = dedupe_items(items) if dedupe else items
    return envelope(
        channel="@aggregate",
        action=action_name,
        backend=None,
        items=merged,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts,
        meta=_meta(
            plan,
            platforms=[outcome.platform_id for outcome in successful],
            per_platform_limit=per_platform_limit,
            dedupe=dedupe,
        ),
    )


def run_action(
    registry: Registry,
    routing: Mapping[str, Any],
    *,
    action: str,
    params: Mapping[str, Any],
    ctx: Any,
    platform: Optional[str] = None,
    intent: Optional[str] = None,
    url: Optional[str] = None,
    mode: Optional[str] = None,
    adapters: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Plan and execute one action, always returning a valid envelope."""
    started = time.monotonic()
    try:
        plan = build_plan(
            registry,
            routing,
            platform=platform,
            intent=intent,
            url=url,
            action=action,
            mode=mode,
        )
    except RoutingError as exc:
        return envelope(
            channel=None,
            action=action,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=ReachError(BAD_INPUT, str(exc)),
        )

    if not plan.platform_ids:
        return envelope(
            channel=None,
            action=action,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=ReachError(BAD_INPUT, "no platform available for this action"),
        )

    first = registry.platforms[plan.platform_ids[0]]
    try:
        # Validate the caller's input against the first route, but leave each
        # platform to apply its own defaults when it is actually tried.
        prepared = prepare_params(first, action, params, apply_defaults=False)
    except ReachError as exc:
        return envelope(
            channel=first.id,
            action=action,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=exc,
            meta=_meta(plan),
        )

    selected_adapters = adapters if adapters is not None else build_adapters(context=ctx)
    if plan.mode == "aggregate":
        aggregate = routing.get("aggregate") or {}
        try:
            per_platform_limit = int(aggregate.get("per_platform_limit", 5))
        except (TypeError, ValueError):
            return envelope(
                channel="@aggregate",
                action=action,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=ReachError(BAD_INPUT, "aggregate.per_platform_limit must be an integer"),
                meta=_meta(plan),
            )
        if per_platform_limit < 1:
            return envelope(
                channel="@aggregate",
                action=action,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=ReachError(BAD_INPUT, "aggregate.per_platform_limit must be at least 1"),
                meta=_meta(plan),
            )
        return _run_aggregate(
            registry,
            plan,
            action,
            prepared,
            ctx=ctx,
            adapters=selected_adapters,
            started=started,
            per_platform_limit=per_platform_limit,
            dedupe=bool(aggregate.get("dedupe", True)),
        )
    return _run_first_success(
        registry,
        plan,
        action,
        prepared,
        ctx=ctx,
        adapters=selected_adapters,
        started=started,
    )
