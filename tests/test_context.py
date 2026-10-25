from __future__ import annotations

import ipaddress
import socket
import sys
import time

import pytest

from omnihub.core.context import CommandFailed, Context
from omnihub.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    OmniHubError,
)


def _answer(*addresses):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))
        for address in addresses
    ]


_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """Keep the suite offline: public names resolve, literal IPs stay real."""

    def resolve(host, port, *args, **kwargs):
        try:
            address = ipaddress.ip_address(str(host))
        except ValueError:
            return _answer("93.184.216.34")
        return _REAL_GETADDRINFO(str(address), port, *args, **kwargs)

    monkeypatch.setattr("omnihub.core.policy.socket.getaddrinfo", resolve)


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
    with pytest.raises(OmniHubError) as info:
        ctx.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert info.value.code == TIMEOUT
    assert time.monotonic() - started < 4


def test_run_missing_executable_is_need_setup():
    ctx = Context(timeout=5)
    with pytest.raises(OmniHubError) as info:
        ctx.run(["definitely-not-a-real-command-xyz"])
    assert info.value.code == NEED_SETUP


def test_secret_reads_environment(monkeypatch):
    monkeypatch.setenv("OMNIHUB_TEST_TOKEN", "from-env")
    assert Context().secret("OMNIHUB_TEST_TOKEN") == "from-env"


def test_secret_reads_yaml_file(tmp_path):
    path = tmp_path / "secrets.yaml"
    path.write_text("token: from-file\n", encoding="utf-8")
    assert Context(secrets_path=path).secret("token") == "from-file"


def test_http_rejects_private_url_before_network():
    with pytest.raises(OmniHubError) as info:
        Context().http("http://127.0.0.1:8080/")
    assert info.value.code == BAD_INPUT


def test_loopback_http_rejects_public_hosts_and_credentials():
    for url in (
        "http://example.com/status",
        "http://user:pass@127.0.0.1:10086/status",
        "file:///tmp/socket",
    ):
        with pytest.raises(OmniHubError) as info:
            Context().loopback_http(url)
        assert info.value.code == BAD_INPUT


def test_loopback_http_parses_json_and_maps_connection_failure(monkeypatch):
    import requests

    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(body=b'{"running": true}'),
    )
    result = Context().loopback_http("http://127.0.0.1:10086/status")
    assert result.data == {"running": True}

    def refused(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("omnihub.core.context.requests.request", refused)
    with pytest.raises(OmniHubError) as info:
        Context(timeout=1).loopback_http("http://127.0.0.1:9/status", timeout=1)
    assert info.value.code == NEED_SETUP
    assert "local daemon" in info.value.message


def test_http_drops_credentials_on_cross_origin_redirect(monkeypatch):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((url, dict(kwargs["headers"])))
        if len(seen) == 1:
            return FakeResponse(302, headers={"Location": "https://other.example/b"})
        return FakeResponse(200, body=b"done")

    monkeypatch.setattr("omnihub.core.context.requests.request", fake_request)
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

    monkeypatch.setattr("omnihub.core.context.requests.request", fake_request)
    Context().http("https://example.com/a", headers={"Authorization": "Bearer secret"})
    assert seen[1][1]["Authorization"] == "Bearer secret"


def test_http_403_is_retryable(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(403, body=b"denied"),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/a")
    assert info.value.code == UPSTREAM_BROKEN


def test_http_403_with_rate_limit_headers_is_final(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(
            403, headers={"X-RateLimit-Remaining": "0"}, body=b"API rate limit exceeded"
        ),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/a")
    assert info.value.code == RATE_LIMITED


def test_http_429_exposes_retry_after_seconds(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(
            429, headers={"Retry-After": "42"}, body=b"slow down"
        ),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/a")
    assert info.value.code == RATE_LIMITED
    assert info.value.retry_after_seconds == 42


def test_http_429_without_retry_after_has_no_hint(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(429, body=b"slow down"),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/a")
    assert info.value.retry_after_seconds is None


def test_http_403_parses_an_http_date_retry_after(monkeypatch):
    import datetime as dt
    from email.utils import format_datetime

    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120)
    monkeypatch.setattr(
        "omnihub.core.context.requests.request",
        lambda *args, **kwargs: FakeResponse(
            403,
            headers={"Retry-After": format_datetime(when, usegmt=True)},
            body=b"blocked",
        ),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/a")
    assert 100 < info.value.retry_after_seconds <= 120


def test_loopback_http_talks_to_a_real_local_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"running": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = Context().loopback_http(
            f"http://127.0.0.1:{server.server_port}/status"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.data == {"running": True}
    assert result.content_type == "application/json"


def test_http_rejects_a_hostname_that_resolves_to_loopback(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.policy.socket.getaddrinfo",
        lambda *args, **kwargs: _answer("127.0.0.1"),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/")
    assert info.value.code == BAD_INPUT
    assert "non-public address" in info.value.message


def test_http_rejects_a_mixed_public_and_private_answer(monkeypatch):
    monkeypatch.setattr(
        "omnihub.core.policy.socket.getaddrinfo",
        lambda *args, **kwargs: _answer("93.184.216.34", "10.0.0.5"),
    )
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/")
    assert info.value.code == BAD_INPUT


def test_http_accepts_a_global_dns_answer(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return FakeResponse(body=b"hello")

    monkeypatch.setattr("omnihub.core.context.requests.request", fake_request)
    result = Context().http("https://example.com/a")
    assert result.text == "hello"
    assert calls == ["https://example.com/a"]


def test_http_reports_dns_failure_as_upstream_broken(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr("omnihub.core.policy.socket.getaddrinfo", fail)
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/")
    assert info.value.code == UPSTREAM_BROKEN
    assert "cannot resolve" in info.value.message


def test_redirect_to_a_private_resolution_is_rejected(monkeypatch):
    requested = []

    def fake_request(method, url, **kwargs):
        requested.append(url)
        return FakeResponse(
            status=302, headers={"Location": "https://internal.example/"}
        )

    def resolve(host, *args, **kwargs):
        return _answer("127.0.0.1" if host == "internal.example" else "93.184.216.34")

    monkeypatch.setattr("omnihub.core.policy.socket.getaddrinfo", resolve)
    monkeypatch.setattr("omnihub.core.context.requests.request", fake_request)
    with pytest.raises(OmniHubError) as info:
        Context().http("https://example.com/start")
    assert info.value.code == BAD_INPUT
    assert requested == ["https://example.com/start"]


def test_literal_public_ip_needs_no_dns(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a literal IP must not be resolved")

    def fake_request(method, url, **kwargs):
        return FakeResponse(body=b"ok")

    monkeypatch.setattr("omnihub.core.policy.socket.getaddrinfo", explode)
    monkeypatch.setattr("omnihub.core.context.requests.request", fake_request)
    assert Context().http("https://93.184.216.34/").text == "ok"
