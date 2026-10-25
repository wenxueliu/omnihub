"""OpenCLI adapter.

OpenCLI prints ``ok: false`` YAML envelopes even when ``-f json`` was asked
for, so a zero exit code is not sufficient. The adapter preserves the upstream
message while translating the error into a OmniHub code.
"""

from __future__ import annotations

import json
import re
import shutil
from typing import Any, Optional

import yaml

from omnihub.adapters.base import Health, RawResult, ToolCall
from omnihub.adapters.cli import args_to_argv
from omnihub.core.context import CommandFailed
from omnihub.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    UPSTREAM_BROKEN,
    OmniHubError,
)

_OK_FALSE = re.compile(r"(?m)^\s*ok\s*:\s*false\s*$")


def _opencli_error(text: str) -> Optional[OmniHubError]:
    if not _OK_FALSE.search(text or ""):
        return None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        payload = None
    error = (payload or {}).get("error") if isinstance(payload, dict) else None
    message = ""
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "").upper()
        message = str(error.get("message") or error.get("help") or "").strip()
    elif error:
        message = str(error).strip()
    lowered = f"{code} {message}".lower()
    if code in {"NEED_LOGIN", "LOGIN_REQUIRED", "AUTH_REQUIRED", "UNAUTHORIZED"} or any(
        token in lowered for token in ("login", "log in", "sign in", "unauthorized")
    ):
        return OmniHubError(NEED_LOGIN, message or "login required")
    if code in {"RATE_LIMITED", "TOO_MANY_REQUESTS"} or any(
        token in lowered for token in ("rate limit", "429", "too many requests")
    ):
        return OmniHubError(RATE_LIMITED, message or "rate limited")
    if code in {"PLATFORM_BLOCKED", "BLOCKED", "CAPTCHA"} or any(
        token in lowered for token in ("captcha", "verification", "blocked")
    ):
        return OmniHubError(PLATFORM_BLOCKED, message or "platform blocked the request")
    if code in {"NOT_FOUND", "404"} or "not found" in lowered:
        return OmniHubError(NOT_FOUND, message or "not found")
    return OmniHubError(UPSTREAM_BROKEN, message or "OpenCLI reported an error")


class OpenCliAdapter:
    kind = "opencli"

    def probe(self) -> Health:
        if shutil.which("opencli"):
            return Health(ok=True)
        return Health(ok=False, detail="opencli is not installed")

    def invoke(self, call: ToolCall, *, ctx: Any) -> RawResult:
        if not call.site:
            raise OmniHubError(BAD_INPUT, "opencli source requires a site")
        argv = ["opencli", str(call.site)]
        if call.call:
            argv.append(str(call.call))
        argv.extend(args_to_argv(dict(call.args) or dict(call.params)))
        argv.extend(["-f", "json"])

        try:
            process = ctx.run(argv, timeout=call.timeout, max_bytes=call.max_bytes)
        except CommandFailed as exc:
            upstream = _opencli_error(exc.stdout) or _opencli_error(exc.stderr)
            if upstream is not None:
                raise upstream from None
            raise
        except OmniHubError as exc:
            if exc.code == NEED_SETUP:
                raise OmniHubError(NEED_SETUP, "opencli is not installed", exc.hint) from None
            raise

        text = process.stdout or ""
        upstream = _opencli_error(text)
        if upstream is not None:
            raise upstream
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        return RawResult(
            ok=True,
            data=parsed,
            text=text,
            content_type="application/json" if parsed is not None else "text/plain",
            meta={"argv": argv, "returncode": process.returncode, "truncated": process.truncated},
        )
