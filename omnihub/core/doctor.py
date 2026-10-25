"""Non-network health report for discovered platforms.

``doctor`` answers "will this platform work on this machine?" without making
real requests: it checks declared tools, adapters, secrets and, for trusted
built-in platforms, the optional ``auth.status`` command. Plugin manifests are
never executed here; their auth state is reported as unknown.
"""

from __future__ import annotations

import shutil
from typing import Any, Mapping, Optional

from omnihub.core.result import OmniHubError
from omnihub.core.registry import PlatformManifest, Registry


def _tool_rows(platform: PlatformManifest) -> list[dict]:
    required = {str(name) for name in platform.requires.get("tools") or ()}
    used: set[str] = set(required)
    for action in platform.actions.values():
        for source in action.sources:
            if source.tool:
                used.add(source.tool)
    rows = []
    for name in sorted(used):
        path = shutil.which(name)
        rows.append(
            {
                "name": name,
                "required": name in required,
                "ok": bool(path),
                "detail": path or "not found on PATH",
            }
        )
    return rows


def _adapter_kinds(platform: PlatformManifest) -> tuple[set[str], set[str]]:
    """All adapter kinds used, and the ones the platform cannot work without.

    An adapter named in ``requires.adapters`` is required. So is one used by an
    unguarded source. An adapter that only appears behind a ``when`` guard is
    optional: the platform still works when that source is skipped.
    """
    required = {str(kind) for kind in platform.requires.get("adapters") or ()}
    used: set[str] = set(required)
    for action in platform.actions.values():
        for source in action.sources:
            used.add(source.adapter)
            if not source.when:
                required.add(source.adapter)
    return used, required


def _adapter_rows(platform: PlatformManifest, adapters: Mapping[str, Any]) -> list[dict]:
    kinds, required = _adapter_kinds(platform)
    rows = []
    for kind in sorted(kinds):
        is_required = kind in required
        adapter = adapters.get(kind)
        if adapter is None:
            rows.append(
                {
                    "kind": kind,
                    "required": is_required,
                    "ok": False,
                    "detail": "adapter not implemented",
                }
            )
            continue
        try:
            health = adapter.probe()
        except Exception as exc:  # a broken probe must not break the report
            rows.append(
                {
                    "kind": kind,
                    "required": is_required,
                    "ok": False,
                    "detail": str(exc)[:300],
                }
            )
            continue
        rows.append(
            {
                "kind": kind,
                "required": is_required,
                "ok": bool(health.ok),
                "detail": health.detail,
                "version": health.version,
            }
        )
    return rows


def _secret_rows(platform: PlatformManifest, ctx: Any) -> list[dict]:
    rows = []
    for name in sorted(str(value) for value in platform.requires.get("secrets") or ()):
        present = bool(ctx.secret(name))
        rows.append(
            {
                "name": name,
                "ok": present,
                "detail": "configured" if present else "missing",
            }
        )
    return rows


def _auth_row(platform: PlatformManifest, ctx: Any) -> dict:
    auth = platform.auth or {}
    status = auth.get("status")
    if not auth:
        return {"configured": False, "ok": None, "detail": "no login required"}
    if not status:
        secret = auth.get("secret")
        if secret:
            present = bool(ctx.secret(str(secret)))
            return {
                "configured": present,
                "ok": None,
                "detail": (
                    f"credential {secret} configured; login state not probed"
                    if present
                    else f"optional credential {secret} is not configured; public sources are used"
                ),
            }
        session = auth.get("session")
        if session:
            found = bool(shutil.which(str(session)))
            return {
                "configured": True,
                "ok": None,
                "detail": (
                    f"session tool {session!r} found; login state not probed"
                    if found
                    else f"session tool {session!r} not found"
                ),
            }
        return {"configured": True, "ok": None, "detail": "login not probed"}
    if platform.origin != "builtin":
        return {
            "configured": True,
            "ok": None,
            "detail": "auth.status is only run for built-in platforms",
        }
    argv = [str(part) for part in status] if isinstance(status, (list, tuple)) else []
    if not argv:
        return {"configured": True, "ok": None, "detail": "auth.status must be an argv list"}
    try:
        ctx.run(argv, timeout=5)
    except OmniHubError as exc:
        return {
            "configured": True,
            "ok": False,
            "detail": exc.message[:300],
            "code": exc.code,
        }
    except Exception as exc:  # a broken status command must not break doctor
        return {"configured": True, "ok": False, "detail": str(exc)[:300]}
    return {"configured": True, "ok": True, "detail": "logged in"}


def _platform_row(
    platform: PlatformManifest,
    *,
    ctx: Any,
    adapters: Mapping[str, Any],
    cooldowns: Optional[list[dict]] = None,
) -> dict:
    tools = _tool_rows(platform)
    adapter_rows = _adapter_rows(platform, adapters)
    secrets = _secret_rows(platform, ctx)
    auth = _auth_row(platform, ctx)
    cooldowns = cooldowns or []

    required_failures = [
        row for row in tools if row["required"] and not row["ok"]
    ] + [row for row in adapter_rows if row["required"] and not row["ok"]] + [
        row for row in secrets if not row["ok"]
    ]
    optional_failures = [
        row for row in tools if not row["required"] and not row["ok"]
    ] + [row for row in adapter_rows if not row["required"] and not row["ok"]]
    if required_failures:
        status = "error"
    elif optional_failures:
        status = "degraded"
    else:
        status = "ok"
    if auth.get("configured") and auth.get("ok") is False:
        status = "degraded" if status == "ok" else status

    return {
        "id": platform.id,
        "description": platform.description,
        "origin": platform.origin,
        "priority": platform.priority,
        "level": platform.level,
        "status": status,
        "actions": sorted(platform.actions),
        "tools": tools,
        "adapters": adapter_rows,
        "secrets": secrets,
        "auth": auth,
        "cooldowns": cooldowns,
    }


def build_doctor_report(
    registry: Registry,
    *,
    ctx: Any,
    adapters: Mapping[str, Any],
    rate_limits: Any = None,
) -> dict:
    """Return an agent-friendly, secret-free health report.

    ``cooldowns`` come from the local rate-limit ledger when ``rate_limits``
    is passed; they describe transient pauses, not a health problem, so they
    never change ``status``.
    """
    active = rate_limits.active() if rate_limits is not None else []
    rows = [
        _platform_row(
            platform,
            ctx=ctx,
            adapters=adapters,
            cooldowns=[row for row in active if row["platform"] == platform.id],
        )
        for platform in sorted(registry.platforms.values(), key=lambda p: (p.priority, p.id))
    ]
    problems = sum(1 for row in rows if row["status"] == "error")
    errors = [
        {"path": str(error.path), "message": error.message}
        for error in registry.errors
    ]
    return {
        "schema_version": 1,
        "ok": not errors and problems == 0,
        "platforms": rows,
        "errors": errors,
        "meta": {
            "count": len(rows),
            "error_count": problems,
            "manifest_error_count": len(errors),
            "degraded_count": sum(1 for row in rows if row["status"] == "degraded"),
            "cooling_count": sum(1 for row in rows if row["cooldowns"]),
        },
        "error": None,
    }
