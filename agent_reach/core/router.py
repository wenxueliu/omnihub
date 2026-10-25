"""Turn a registry plus routing config into an ordered platform plan.

Resolution order is intentionally simple and inspectable:

    --platform  >  URL domain  >  intent  >  platform priority  >  id
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agent_reach.core.registry import PlatformManifest, Registry, Source

VALID_MODES = ("first_success", "aggregate")


class RoutingError(ValueError):
    """The requested route cannot be satisfied."""


@dataclass(frozen=True)
class Plan:
    platform_ids: tuple[str, ...]
    mode: str
    reason: str
    intent: Optional[str] = None
    top: Optional[int] = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "intent": self.intent,
            "platforms": list(self.platform_ids),
            "top": self.top,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


def _routing_platform(routing: Mapping[str, Any], platform_id: str) -> Mapping[str, Any]:
    entry = (routing.get("platforms") or {}).get(platform_id)
    return entry if isinstance(entry, Mapping) else {}


def _is_enabled(routing: Mapping[str, Any], platform_id: str) -> bool:
    return bool(_routing_platform(routing, platform_id).get("enabled", True))


def _priority(
    routing: Mapping[str, Any], platform: PlatformManifest
) -> int:
    entry = _routing_platform(routing, platform.id)
    if "priority" in entry and entry["priority"] is not None:
        return int(entry["priority"])
    return platform.priority


def _aggregate_top(mode: str, routing: Mapping[str, Any]) -> Optional[int]:
    if mode != "aggregate":
        return None
    value = int((routing.get("aggregate") or {}).get("top", 3))
    if value < 1:
        raise RoutingError("aggregate.top must be at least 1")
    return value


def _enabled_platforms(
    registry: Registry, routing: Mapping[str, Any], *, action: Optional[str]
) -> dict[str, PlatformManifest]:
    selected = {}
    for platform_id, platform in registry.platforms.items():
        if not _is_enabled(routing, platform_id):
            continue
        if action is not None and not platform.supports(action):
            continue
        selected[platform_id] = platform
    return selected


def _default_order(
    registry: Registry,
    routing: Mapping[str, Any],
    enabled: Mapping[str, PlatformManifest],
) -> list[str]:
    return sorted(
        enabled,
        key=lambda platform_id: (_priority(routing, registry.platforms[platform_id]), platform_id),
    )


def _url_order(
    registry: Registry,
    routing: Mapping[str, Any],
    enabled: Mapping[str, PlatformManifest],
    url: str,
    action: Optional[str],
) -> list[str]:
    matched = [
        platform.id
        for platform in registry.for_url(url, action=action)
        if platform.id in enabled
    ]
    matched.sort(
        key=lambda platform_id: (
            _priority(routing, registry.platforms[platform_id]),
            platform_id,
        )
    )
    return matched


def build_plan(
    registry: Registry,
    routing: Mapping[str, Any],
    *,
    platform: Optional[str] = None,
    intent: Optional[str] = None,
    url: Optional[str] = None,
    action: Optional[str] = None,
    mode: Optional[str] = None,
) -> Plan:
    """Resolve a request to ordered platform ids.

    ``action`` filters platforms that do not declare that action. ``url`` is
    used for read-style routing before intents are considered.
    """
    selected_mode = mode or str(routing.get("mode") or "first_success")
    if selected_mode not in VALID_MODES:
        raise RoutingError(
            f"unknown mode {selected_mode!r}; expected one of {', '.join(VALID_MODES)}"
        )
    top = _aggregate_top(selected_mode, routing)
    warnings: list[str] = []
    enabled = _enabled_platforms(registry, routing, action=action)

    if platform is not None:
        manifest = registry.platforms.get(platform)
        if manifest is None:
            raise RoutingError(
                f"unknown platform {platform!r}; run `reach-platforms platforms`"
            )
        if not _is_enabled(routing, platform):
            raise RoutingError(f"platform {platform!r} is disabled in routing")
        if action is not None and not manifest.supports(action):
            raise RoutingError(
                f"platform {platform!r} does not support action {action!r}"
            )
        return Plan(
            platform_ids=(platform,),
            mode=selected_mode,
            reason="explicit platform",
            top=top,
        )

    if url:
        matched = _url_order(registry, routing, enabled, url, action)
        if matched:
            # A domain-specific read is tried first; the public-web platform is
            # the last resort when that platform cannot fetch the page.
            if action == "read" and "web" in enabled and "web" not in matched:
                matched.append("web")
            return Plan(
                platform_ids=tuple(matched),
                mode=selected_mode,
                reason=f"url domain match: {url}",
                top=top,
            )
        warnings.append(
            f"no enabled platform declares a domain for {url!r}; using routing order"
        )

    if intent is not None:
        intents = routing.get("intents") or {}
        if intent not in intents:
            configured = ", ".join(sorted(intents)) or "<none>"
            raise RoutingError(
                f"unknown intent {intent!r}; configured intents: {configured}"
            )
        selected: list[str] = []
        for platform_id in intents[intent]:
            if platform_id not in registry.platforms:
                warnings.append(
                    f"intent {intent!r}: unknown platform {platform_id!r} skipped"
                )
                continue
            if platform_id not in enabled:
                warning = f"intent {intent!r}: platform {platform_id!r} disabled"
                if action is not None:
                    warning += f" or does not support action {action!r}"
                warnings.append(warning + " skipped")
                continue
            if platform_id not in selected:
                selected.append(platform_id)
        if selected:
            return Plan(
                platform_ids=tuple(selected),
                mode=selected_mode,
                reason=f"intent {intent!r}",
                intent=intent,
                top=top,
                warnings=tuple(warnings),
            )
        warnings.append(
            f"intent {intent!r} resolved to no usable platform; using priority order"
        )

    ordered = _default_order(registry, routing, enabled)
    if not ordered:
        suffix = f" for action {action!r}" if action is not None else ""
        raise RoutingError(f"no enabled platform{suffix}")

    reason = "default priority order"
    if intent is not None:
        reason += f" after intent {intent!r}"
    return Plan(
        platform_ids=tuple(ordered),
        mode=selected_mode,
        reason=reason,
        intent=intent,
        top=top,
        warnings=tuple(warnings),
    )


def resolve_sources(platform: PlatformManifest, action: str) -> tuple[Source, ...]:
    """Ordered enabled sources for one action."""
    declared = platform.actions.get(action)
    if declared is None:
        raise RoutingError(
            f"platform {platform.id!r} does not support action {action!r}"
        )
    return declared.enabled_sources()
