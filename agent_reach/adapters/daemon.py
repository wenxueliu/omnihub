"""kimi-webbridge adapter: drive the user's real browser through a local daemon.

The daemon listens on the loopback interface and answers ``POST /command``
with ``{"ok": true, "data": ...}`` or ``{"ok": false, "error": ...}``. Its
address is discovered from the environment, ``~/.kimi-webbridge/daemon.addr``
or the daemon config; only loopback literals are accepted, so a manifest can
never point this adapter at a remote host.

A source names the daemon action in ``call``. ``snapshot`` and friends are
passed through as-is; ``read`` is a composite that navigates to the target and
pages through the ARIA snapshot until the character budget runs out. Cursors
expire after five minutes, so an expired cursor simply ends the read with
``truncated_reason: cursor_expired`` instead of failing the action.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from agent_reach.adapters.base import Health, RawResult, ToolCall
from agent_reach.core.result import (
    BAD_INPUT,
    BUSY,
    NEED_LOGIN,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
)

DEFAULT_ADDRESS = "127.0.0.1:10086"
ADDRESS_ENV = "KIMI_WEBBRIDGE_ADDR"
DEFAULT_SESSION = "reach-platforms"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_READ_CHUNK_MAX = 60000  # daemon-side ceiling per snapshot call
_DEFAULT_READ_CHARS = 20000
_MAX_READ_CHARS = 200_000
_DEFAULT_MAX_PAGES = 8
_MAX_PAGES = 20
_DEFAULT_WAIT_MS = 10_000
_MAX_WAIT_MS = 30_000
_POLL_SECONDS = 0.25
_MAX_CODE_CHARS = 100_000
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50


class ExpiredCursor(ReachError):
    """A snapshot cursor expired mid-read; the caller stops paginating."""

    def __init__(self, message: str) -> None:
        super().__init__(UPSTREAM_BROKEN, message)


def normalize_address(value: Any) -> str:
    """Return ``http://<loopback>:<port>`` or raise ``ValueError``."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty kimi-webbridge address")
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid kimi-webbridge address {raw!r}: {exc}") from None
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"kimi-webbridge address must be http(s), not {parts.scheme!r}")
    if parts.username or parts.password:
        raise ValueError("kimi-webbridge address must not contain credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("kimi-webbridge address must be a bare host:port")
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing non-loopback kimi-webbridge address {raw!r}; "
            "the daemon must stay on 127.0.0.1"
        )
    resolved = port if port is not None else 10086
    if not 1 <= resolved <= 65535:
        raise ValueError(f"invalid kimi-webbridge port in {raw!r}")
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{shown}:{resolved}"


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_config_addr(path: Path) -> Optional[str]:
    text = _read_text(path)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    addr = payload.get("addr")
    return str(addr) if addr else None


def discover_address(
    *, home: Optional[Path | str] = None, environ: Optional[Mapping[str, str]] = None
) -> str:
    """Resolve the daemon base URL from the env or the user's kimi directory.

    A stale or foreign value in a local file is ignored in favour of the
    default loopback address; an explicit environment override is strict.
    """
    env = os.environ if environ is None else environ
    override = (env.get(ADDRESS_ENV) or "").strip()
    if override:
        return normalize_address(override)

    base = Path(home).expanduser() if home is not None else Path.home()
    directory = base / ".kimi-webbridge"
    for path, loader in (
        (directory / "daemon.addr", _read_text),
        (directory / "config.json", _read_config_addr),
    ):
        value = loader(path)
        if not value:
            continue
        try:
            return normalize_address(value)
        except ValueError:
            continue
    return normalize_address(DEFAULT_ADDRESS)


def _daemon_error(error: Any, *, action: str) -> ReachError:
    """Translate the daemon's error envelope into a Reach code."""
    code = ""
    message = ""
    if isinstance(error, Mapping):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
    elif error:
        message = str(error).strip()
    detail = message or code or "unknown error"
    label = f"kimi-webbridge {action}: {detail}"
    lowered = f"{code} {message}".lower()

    if "cursor" in lowered and any(
        token in lowered for token in ("expire", "unknown", "invalid", "missing")
    ):
        raise ExpiredCursor(label)
    if code == "extension_not_connected" or "extension_not_connected" in lowered:
        return ReachError(
            NEED_SETUP,
            "kimi-webbridge is running but the Kimi browser extension is not connected",
            "Open the Kimi browser extension and connect it to the local daemon, "
            "or keep the next source available.",
        )
    if any(token in lowered for token in ("not_running", "daemon_not", "no daemon")):
        return ReachError(NEED_SETUP, label)
    if "busy" in lowered or "queue" in lowered or "too many" in lowered:
        return ReachError(BUSY, label)
    if "timeout" in lowered or "timed out" in lowered:
        return ReachError(TIMEOUT, label)
    if "login" in lowered or "sign in" in lowered or "unauthorized" in lowered:
        return ReachError(NEED_LOGIN, label)
    if "not found" in lowered or code in {"no_tab", "tab_not_found"}:
        return ReachError(NOT_FOUND, label)
    if "invalid" in lowered and ("arg" in lowered or "param" in lowered or "request" in lowered):
        return ReachError(BAD_INPUT, label)
    return ReachError(UPSTREAM_BROKEN, label)


def _positive_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ReachError(BAD_INPUT, f"{name} must be an integer") from None
    if number < minimum or number > maximum:
        raise ReachError(BAD_INPUT, f"{name} must be between {minimum} and {maximum}")
    return number


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _evaluate_value(payload: Any) -> Any:
    """Unwrap the daemon's ``evaluate`` reply ``{type, value}``."""
    if isinstance(payload, Mapping) and "value" in payload:
        return payload.get("value")
    return payload


def _maybe_json(value: Any) -> Any:
    """Decode a JSON string returned by site JavaScript.

    JavaScript snippets cannot always hand a live object back through the
    extension, so manifests may return ``JSON.stringify(items)``. A plain
    string that is not JSON is kept as-is.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _limit_value(value: Any, limit: int) -> Any:
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, Mapping):
        for key in ("items", "results", "data"):
            entries = value.get(key)
            if isinstance(entries, list):
                copy = dict(value)
                copy[key] = entries[:limit]
                return copy
    return value


def _page_key(url: str) -> Optional[tuple[str, str]]:
    """Normalize a URL to ``(host-without-www, path)`` for tab matching."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    return host, parts.path.rstrip("/") or "/"


class DaemonAdapter:
    kind = "daemon"

    def __init__(
        self,
        *,
        context: Any = None,
        home: Optional[Path | str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._context = context
        self._home = home
        self._environ = environ

    # -- address / health ------------------------------------------------
    @property
    def addr(self) -> str:
        try:
            return discover_address(home=self._home, environ=self._environ)
        except ValueError as exc:
            raise ReachError(NEED_SETUP, str(exc)) from None

    def _ctx(self, ctx: Any = None) -> Any:
        if ctx is not None:
            return ctx
        if self._context is not None:
            return self._context
        from agent_reach.core.context import Context  # local import keeps adapters light

        return Context()

    def probe(self) -> Health:
        try:
            base = self.addr
            raw = self._ctx().loopback_http(f"{base}/status", timeout=2)
        except ReachError as exc:
            return Health(
                ok=False,
                detail=f"kimi-webbridge daemon is not reachable: {exc.message}",
            )
        except Exception as exc:  # a broken probe must not break doctor
            return Health(ok=False, detail=str(exc)[:300])
        data = raw.data if isinstance(raw.data, Mapping) else {}
        version = data.get("version")
        if data.get("extension_connected") is False:
            return Health(
                ok=False,
                version=version,
                logged_in=False,
                detail="kimi-webbridge daemon is running but the browser extension is not connected",
            )
        return Health(
            ok=True,
            version=version,
            logged_in=True,
            detail=f"kimi-webbridge {version or '(unknown version)'} is running",
        )

    # -- invocation ------------------------------------------------------
    def invoke(self, call: ToolCall, *, ctx: Any) -> RawResult:
        action = str(call.call or "").strip()
        if not action:
            raise ReachError(BAD_INPUT, "daemon source requires a call (e.g. snapshot, read)")
        base = self.addr
        session = call.session or f"reach-{call.raw.get('platform_id') or DEFAULT_SESSION}"
        args = dict(call.args or {})
        extra_meta: dict[str, Any] = {}
        if action == "read":
            data = self._read(
                base, session, args, call, ctx, timeout=call.timeout, max_bytes=call.max_bytes
            )
        elif action == "browse":
            if not call.raw.get("platform_trusted"):
                raise ReachError(
                    NEED_SETUP,
                    "daemon browse runs JavaScript in the user's browser and requires "
                    "a built-in or explicitly trusted platform",
                )
            data, extra_meta = self._browse(
                base, session, args, call, ctx, timeout=call.timeout, max_bytes=call.max_bytes
            )
        else:
            data = self._command(
                base,
                action,
                args,
                session,
                ctx,
                timeout=call.timeout,
                max_bytes=call.max_bytes,
            )
        return RawResult(
            ok=True,
            data=data,
            content_type="application/json",
            meta={"daemon_action": action, "session": session, **extra_meta},
        )

    def _command(
        self,
        base: str,
        action: str,
        args: Mapping[str, Any],
        session: str,
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> Any:
        body = json.dumps(
            {"action": action, "args": dict(args), "session": session},
            ensure_ascii=False,
        ).encode("utf-8")
        raw = self._ctx(ctx).loopback_http(
            f"{base}/command",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            data=body,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        payload = raw.data if isinstance(raw.data, Mapping) else None
        if payload is None:
            raise ReachError(UPSTREAM_BROKEN, "kimi-webbridge returned a non-JSON response")
        if payload.get("ok") is not True:
            raise _daemon_error(payload.get("error"), action=action)
        return payload.get("data")

    def _read(
        self,
        base: str,
        session: str,
        args: dict[str, Any],
        call: ToolCall,
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> dict:
        target = args.pop("url", None) or call.url or (call.params or {}).get("target")
        if not target:
            raise ReachError(
                BAD_INPUT, "daemon read requires a url (args.url or the action target)"
            )
        from agent_reach.core.policy import ensure_public_url

        target = ensure_public_url(str(target))
        budget = _positive_int(
            args.pop("max_chars", _DEFAULT_READ_CHARS),
            "max_chars",
            minimum=1,
            maximum=_MAX_READ_CHARS,
        )
        max_pages = _positive_int(
            args.pop("max_pages", _DEFAULT_MAX_PAGES),
            "max_pages",
            minimum=1,
            maximum=_MAX_PAGES,
        )
        new_tab = _as_bool(args.pop("new_tab", True))
        group_title = args.pop("group_title", None)

        navigate_args: dict[str, Any] = {"url": target, "newTab": new_tab}
        if group_title:
            navigate_args["group_title"] = str(group_title)
        navigation = self._command(
            base,
            "navigate",
            navigate_args,
            session,
            ctx,
            timeout=timeout,
            max_bytes=max_bytes,
        )

        snapshot = self._snapshot(
            base,
            session,
            min(budget, _READ_CHUNK_MAX),
            None,
            ctx,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        chunks = [str(snapshot.get("tree_text") or "")]
        pages = 1
        truncated = bool(snapshot.get("truncated"))
        cursor = snapshot.get("next_cursor")
        reason = ""

        while truncated and cursor and pages < max_pages and sum(map(len, chunks)) < budget:
            remaining = budget - sum(map(len, chunks))
            chunk_size = max(1, min(_READ_CHUNK_MAX, remaining))
            try:
                snapshot = self._snapshot(
                    base,
                    session,
                    chunk_size,
                    str(cursor),
                    ctx,
                    timeout=timeout,
                    max_bytes=max_bytes,
                )
            except ExpiredCursor:
                reason = "cursor_expired"
                break
            chunks.append(str(snapshot.get("tree_text") or ""))
            pages += 1
            truncated = bool(snapshot.get("truncated"))
            cursor = snapshot.get("next_cursor")

        raw_total = sum(map(len, chunks))
        text = "".join(chunks)[:budget]
        truncated = bool(truncated or cursor or raw_total > budget)
        if truncated and not reason:
            reason = "max_chars" if raw_total >= budget else "max_pages"

        page_url = snapshot.get("url")
        if not page_url and isinstance(navigation, Mapping):
            page_url = navigation.get("url")
        return {
            "url": page_url or target,
            "title": snapshot.get("title"),
            "text": text,
            "truncated": truncated,
            "truncated_reason": reason or None,
            "pages": pages,
            "tab_id": navigation.get("tabId") if isinstance(navigation, Mapping) else None,
        }

    def _browse(
        self,
        base: str,
        session: str,
        args: dict[str, Any],
        call: ToolCall,
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Navigate, optionally wait for a selector, then run site JavaScript.

        ``browse`` exists because a site search is not one daemon action: the
        page has to load (often client-rendered) before a snippet of
        site-specific JavaScript can turn it into items. The JavaScript runs
        inside the user's page, so it only executes for trusted platforms and
        never receives query text through string interpolation.
        """
        target = args.pop("url", None) or call.url or (call.params or {}).get("target")
        if not target:
            raise ReachError(
                BAD_INPUT, "daemon browse requires a url (args.url or the action target)"
            )
        from agent_reach.core.policy import ensure_public_url

        target = ensure_public_url(str(target))
        code = args.pop("code", None)
        if not isinstance(code, str) or not code.strip():
            raise ReachError(BAD_INPUT, "daemon browse requires a non-empty code argument")
        if len(code) > _MAX_CODE_CHARS:
            raise ReachError(
                BAD_INPUT, f"daemon browse code exceeds {_MAX_CODE_CHARS} characters"
            )
        wait_for = args.pop("wait_for", None)
        if wait_for is not None and not str(wait_for).strip():
            wait_for = None
        wait_ms = _positive_int(
            args.pop("wait_ms", _DEFAULT_WAIT_MS),
            "wait_ms",
            minimum=0,
            maximum=_MAX_WAIT_MS,
        )
        limit = _positive_int(
            args.pop("limit", _DEFAULT_LIMIT),
            "limit",
            minimum=1,
            maximum=_MAX_LIMIT,
        )
        new_tab = _as_bool(args.pop("new_tab", False))
        group_title = args.pop("group_title", None)
        login_markers = _string_list(args.pop("login_url", None))

        navigate_args: dict[str, Any] = {"url": target, "newTab": new_tab}
        if group_title:
            navigate_args["group_title"] = str(group_title)
        navigation: Any = {}
        nav_timed_out = False
        nav_recovered = False
        nav_error: Optional[ReachError] = None
        try:
            navigation = self._command(
                base,
                "navigate",
                navigate_args,
                session,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
        except ReachError as exc:
            # Client-rendered sites often keep a connection open and never fire
            # the load event. The page can still be usable, so let the selector
            # wait and the site JavaScript decide instead of failing here.
            if exc.code != TIMEOUT:
                raise
            nav_timed_out = True
            nav_error = exc

        if nav_timed_out:
            # A failed load event can still leave a usable tab behind, but the
            # daemon does not attach it to the session. Find the tab that the
            # navigation created (its group title is the most reliable marker)
            # and attach it so evaluate/snapshot have a target.
            recovered = self._recover_tab(
                base,
                session,
                target,
                group_title,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if recovered is None:
                raise nav_error or ReachError(
                    UPSTREAM_BROKEN, "kimi-webbridge navigation did not produce a usable tab"
                )
            navigation = recovered
            nav_recovered = True

        page_info = self._command(
            base,
            "evaluate",
            {"code": "JSON.stringify({url: location.href, title: document.title})"},
            session,
            ctx,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        info = _maybe_json(_evaluate_value(page_info))
        if isinstance(info, Mapping):
            page_url = str(info.get("url") or target)
            page_title = str(info.get("title") or "")
        else:
            page_url = (
                str(navigation.get("url") or target)
                if isinstance(navigation, Mapping) and navigation.get("url")
                else target
            )
            page_title = ""
        for marker in login_markers:
            if marker in page_url:
                raise ReachError(
                    NEED_LOGIN,
                    f"kimi-webbridge browse landed on a login page ({marker}); "
                    "log in to this site in the connected browser",
                )

        waited_out = False
        if wait_for:
            waited_out = not self._wait_for_selector(
                base,
                session,
                str(wait_for),
                wait_ms,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )

        payload = self._command(
            base,
            "evaluate",
            {"code": code},
            session,
            ctx,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        value = _evaluate_value(payload)
        value = _maybe_json(value)
        if isinstance(value, Mapping):
            message = str(value.get("message") or value.get("reason") or "").strip()
            if value.get("login"):
                raise ReachError(
                    NEED_LOGIN,
                    f"kimi-webbridge browse needs a logged-in session"
                    + (f": {message}" if message else ""),
                )
            if value.get("blocked"):
                raise ReachError(
                    PLATFORM_BLOCKED,
                    "the site returned a captcha or risk-control page"
                    + (f": {message}" if message else ""),
                )
            if value.get("not_found"):
                raise ReachError(
                    NOT_FOUND, message or "the site returned no matching page"
                )
        value = _limit_value(value, limit)
        meta = {
            "page_url": page_url,
            "page_title": page_title,
            "tab_id": navigation.get("tabId") if isinstance(navigation, Mapping) else None,
            "wait_timed_out": waited_out,
            "nav_timed_out": nav_timed_out,
            "nav_recovered": nav_recovered,
        }
        return value, meta

    def _recover_tab(
        self,
        base: str,
        session: str,
        target: str,
        group_title: Any,
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> Optional[Mapping[str, Any]]:
        """Attach the tab a timed-out navigation left behind, if identifiable."""
        # Reusing a session tab (newTab=false) leaves that tab current even when
        # navigate reports a load timeout, so no attach is needed.
        try:
            current = self._command(
                base,
                "list_tabs",
                {},
                session,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
        except ReachError:
            current = None
        session_tabs = current.get("tabs") if isinstance(current, Mapping) else None
        if isinstance(session_tabs, list):
            owned = [
                tab
                for tab in session_tabs
                if isinstance(tab, Mapping) and tab.get("tabId") is not None
            ]
            if owned:
                tagged_now = [
                    tab
                    for tab in owned
                    if group_title and tab.get("groupTitle") == str(group_title)
                ]
                return max(
                    tagged_now or owned, key=lambda tab: int(tab.get("tabId") or 0)
                )

        try:
            data = self._command(
                base,
                "list_browser_tabs",
                {},
                session,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
        except ReachError:
            return None
        tabs = data.get("tabs") if isinstance(data, Mapping) else None
        if not isinstance(tabs, list):
            return None
        candidates = [
            tab
            for tab in tabs
            if isinstance(tab, Mapping) and tab.get("tabId") is not None
        ]
        wanted = _page_key(target)
        wanted_host = wanted[0] if wanted else None
        tagged = [
            tab
            for tab in candidates
            if group_title and tab.get("groupTitle") == str(group_title)
        ]
        same_page = [
            tab for tab in candidates if _page_key(str(tab.get("url") or "")) == wanted
        ]
        # Pages redirect (search_result -> explore), and a reused tab keeps the
        # group title of the earlier navigation. Stay inside our own groups
        # before considering any tab on the same host.
        same_host_owned = [
            tab
            for tab in candidates
            if wanted_host
            and (_page_key(str(tab.get("url") or "")) or ("", ""))[0] == wanted_host
            and str(tab.get("groupTitle") or "").startswith("reach:")
        ]
        pool = tagged or same_page or same_host_owned
        if not pool:
            return None
        chosen = max(pool, key=lambda tab: int(tab.get("tabId") or 0))
        try:
            attached = self._command(
                base,
                "attach_tab",
                {"tabId": chosen["tabId"]},
                session,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
        except ReachError:
            return None
        return attached if isinstance(attached, Mapping) else chosen

    def _wait_for_selector(
        self,
        base: str,
        session: str,
        selector: str,
        wait_ms: int,
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> bool:
        """Poll until ``selector`` exists, bounded by ``wait_ms``."""
        deadline = time.monotonic() + (wait_ms / 1000)
        expression = f"!!document.querySelector({json.dumps(selector)})"
        while True:
            payload = self._command(
                base,
                "evaluate",
                {"code": expression},
                session,
                ctx,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if bool(_evaluate_value(payload)):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_POLL_SECONDS, remaining))

    def _snapshot(
        self,
        base: str,
        session: str,
        max_chars: int,
        cursor: Optional[str],
        ctx: Any,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> Mapping[str, Any]:
        args: dict[str, Any] = {"format": "aria", "max_chars": max_chars}
        if cursor:
            args["cursor"] = cursor
        data = self._command(
            base, "snapshot", args, session, ctx, timeout=timeout, max_bytes=max_bytes
        )
        if not isinstance(data, Mapping):
            raise ReachError(UPSTREAM_BROKEN, "kimi-webbridge snapshot returned no data")
        return data
