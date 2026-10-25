"""HTTP adapter: one request described by a manifest source."""

from __future__ import annotations

import json
from typing import Any

from agent_reach.adapters.base import Health, RawResult, ToolCall
from agent_reach.core.policy import ensure_public_url
from agent_reach.core.result import BAD_INPUT, ReachError


class HttpAdapter:
    kind = "http"

    def probe(self) -> Health:
        return Health(ok=True)

    def invoke(self, call: ToolCall, *, ctx: Any) -> RawResult:
        if not call.url:
            raise ReachError(BAD_INPUT, "http source requires a url")
        url = ensure_public_url(call.url)
        headers = {str(key): str(value) for key, value in call.headers.items()}
        data = call.data
        if call.body is not None:
            data = json.dumps(call.body, ensure_ascii=False).encode("utf-8")
            if not any(key.lower() == "content-type" for key in headers):
                headers["Content-Type"] = "application/json"
        return ctx.http(
            url,
            method=call.method or "GET",
            headers=headers,
            data=data,
            timeout=call.timeout,
            max_bytes=call.max_bytes,
        )
