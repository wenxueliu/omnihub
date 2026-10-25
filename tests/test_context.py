from __future__ import annotations

import sys
import time

import pytest

from agent_reach.core.context import CommandFailed, Context
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
)


class FakeResponse:
    def __init__(self, status=200, *, headers=None, body=b"ok"):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def iter_content(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_run_captures_utf8_stdout():
    ctx = Context(timeout=5)
    result = ctx.run([sys.executable, "-c", "print('héllo')"])
    assert result.stdout.strip() == "héllo"
    assert result.returncode == 0


def test_run_truncates_output_but_drains_pipe():
    ctx = Context(timeout=5)
    result = ctx.run(
        [sys.executable, "-c", "print('x' * 1000)"],
        max_bytes=10,
    )
    assert result.truncated is True
    assert len(result.stdout) <= 10


def test_run_nonzero_raises_command_failed():
    ctx = Context(timeout=5)
    with pytest.raises(CommandFailed) as info:
        ctx.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad news'); sys.exit(3)"]
        )
    assert info.value.returncode == 3
    assert "bad news" in info.value.message


def test_run_timeout_kills_process_group():
    ctx = Context(timeout=5)
    started = time.monotonic()
    with pytest.raises(ReachError) as info:
        ctx.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert info.value.code == TIMEOUT
    assert time.monotonic() - started < 4


def test_run_missing_executable_is_need_setup():
    ctx = Context(timeout=5)
    with pytest.raises(ReachError) as info:
        ctx.run(["definitely-not-a-real-command-xyz"])
    assert info.value.code == NEED_SETUP


def test_secret_reads_environment(monkeypatch):
    monkeypatch.setenv("REACH_TEST_TOKEN", "from-env")
    assert Context().secret("REACH_TEST_TOKEN") == "from-env"


def test_secret_reads_yaml_file(tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text("token: from-file\n", encoding="utf-8")
    assert Context(secrets_path=path).secret("token") == "from-file"


def test_http_rejects_private_url_before_network():
    with pytest.raises(ReachError) as info:
        Context().http("http://127.0.0.1:8080/")
    assert info.value.code == BAD_INPUT


def test_http_drops_credentials_on_cross_origin_redirect(monkeypatch):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((url, dict(kwargs["headers"])))
        if len(seen) == 1:
            return FakeResponse(302, headers={"Location": "https://other.example/b"})
        return FakeResponse(200, body=b"done")

    monkeypatch.setattr("agent_reach.core.context.requests.request", fake_request)
    result = Context().http(
        "https://example.com/a",
        headers={"Authorization": "Bearer secret", "X-Keep": "1"},
    )
    assert result.text == "done"
    assert result.meta["url"] == "https://other.example/b"
    assert "Authorization" not in seen[1][1]
    assert seen[1][1]["X-Keep"] == "1"


def test_http_keeps_credentials_on_same_origin_redirect(monkeypatch):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((url, dict(kwargs["headers"])))
        if len(seen) == 1:
            return FakeResponse(307, headers={"Location": "/b"})
        return FakeResponse(200, body=b"done")

    monkeypatch.setattr("agent_reach.core.context.requests.request", fake_request)
    Context().http("https://example.com/a", headers={"Authorization": "Bearer secret"})
    assert seen[1][1]["Authorization"] == "Bearer secret"


def test_http_403_is_retryable(monkeypatch):
    monkeypatch.setattr(
        "agent_reach.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(403, body=b"denied"),
    )
    with pytest.raises(ReachError) as info:
        Context().http("https://example.com/a")
    assert info.value.code == UPSTREAM_BROKEN
