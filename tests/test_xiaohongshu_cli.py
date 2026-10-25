from __future__ import annotations

import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from omnihub.platforms.xiaohongshu import xiaohongshu_cli as xhs
from omnihub.platforms.xiaohongshu.xiaohongshu_cli import (
    CliError,
    DaemonClient,
    TokenCache,
    read,
    search,
)

NOTE_A = "a" * 24
NOTE_B = "b" * 24
AUTHOR_ID = "c" * 24
PROFILE_URL = (
    f"{xhs.SITE}/user/profile/{AUTHOR_ID}"
    "?xsec_token=author-token&xsec_source=pc_search"
)


# ---------------------------------------------------------------------------
# fake daemon
# ---------------------------------------------------------------------------


def _default_responder(
    overrides: dict[str, Any] | None = None,
    *,
    wait: bool | Callable[[str], bool] = True,
) -> Callable[[dict[str, Any]], Any]:
    overrides = overrides or {}

    def respond(request: dict[str, Any]) -> Any:
        action = request.get("action")
        args = request.get("args") or {}
        if action == "find_tab":
            return {"success": True, "url": xhs.SITE, "tabId": 1}
        if action == "navigate":
            return {"success": True, "url": args.get("url"), "tabId": 1}
        if action == "list_browser_tabs":
            return {"tabs": [{"tabId": 7, "url": xhs.SITE + "/explore"}]}
        if action == "attach_tab":
            return {"success": True}
        if action == "close_tab":
            return {"success": True}
        if action == "evaluate":
            code = args.get("code", "")
            if code.startswith("!!document.querySelector"):
                present = wait(code) if callable(wait) else wait
                return {"type": "boolean", "value": present}
            for marker, value in overrides.items():
                if marker in code:
                    return {"type": "string", "value": json.dumps(value, ensure_ascii=False)}
            return {"type": "string", "value": "{}"}
        raise AssertionError(f"unexpected daemon action: {action}")

    return respond


class FakeDaemon:
    def __init__(self, responder: Callable[[dict[str, Any]], Any]) -> None:
        self.requests: list[dict[str, Any]] = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                requests.append(request)
                try:
                    payload = {"ok": True, "data": responder(request)}
                except CliError as exc:
                    payload = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep selector-wait timeouts short so failure paths stay fast."""
    monkeypatch.setattr(xhs, "WAIT_SELECTOR_MS", 200)


@pytest.fixture
def daemon():
    started: list[FakeDaemon] = []

    def start(
        overrides: dict[str, Any] | None = None,
        *,
        wait: bool | Callable[[str], bool] = True,
    ) -> FakeDaemon:
        fake = FakeDaemon(_default_responder(overrides, wait=wait))
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.close()


@pytest.fixture
def cache(tmp_path: Path) -> TokenCache:
    return TokenCache(tmp_path / "notes.json")


# ---------------------------------------------------------------------------
# daemon address handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:10086", "http://127.0.0.1:10086"),
        ("http://localhost:10086/", "http://localhost:10086"),
        ("http://[::1]:10086", "http://[::1]:10086"),
    ],
)
def test_normalize_daemon_url(value: str, expected: str) -> None:
    assert xhs.normalize_daemon_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["example.com:10086", "http://10.0.0.5:10086", "ftp://127.0.0.1:10086", "http://u:p@127.0.0.1"],
)
def test_normalize_daemon_url_rejects_unsafe(value: str) -> None:
    with pytest.raises(CliError):
        xhs.normalize_daemon_url(value)


def test_discover_daemon_url_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(xhs.DAEMON_ENV, "127.0.0.1:20000")
    assert xhs.discover_daemon_url("http://127.0.0.1:30000") == "http://127.0.0.1:30000"


# ---------------------------------------------------------------------------
# target parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "note_id", "token"),
    [
        (NOTE_A, NOTE_A, None),
        (f"https://www.xiaohongshu.com/explore/{NOTE_A}", NOTE_A, None),
        (
            f"https://www.xiaohongshu.com/explore/{NOTE_A}?xsec_token=tok&xsec_source=pc_user",
            NOTE_A,
            "tok",
        ),
        (
            f"https://www.xiaohongshu.com/search_result/{NOTE_A}?xsec_token=tok2&xsec_source=",
            NOTE_A,
            "tok2",
        ),
        (
            f"/user/profile/{AUTHOR_ID}/{NOTE_A}?xsec_token=tok3&xsec_source=pc_user",
            NOTE_A,
            "tok3",
        ),
        (f"www.xiaohongshu.com/explore/{NOTE_A}", NOTE_A, None),
    ],
)
def test_parse_target(target: str, note_id: str, token: str | None) -> None:
    assert xhs.parse_target(target) == (note_id, token)


@pytest.mark.parametrize("target", ["", "not-an-id", f"{xhs.SITE}/user/profile/{AUTHOR_ID}"])
def test_parse_target_rejects_invalid(target: str) -> None:
    with pytest.raises(CliError) as excinfo:
        xhs.parse_target(target)
    assert excinfo.value.code == "bad_input"


# ---------------------------------------------------------------------------
# token cache
# ---------------------------------------------------------------------------


def test_token_cache_roundtrip_and_permissions(cache: TokenCache) -> None:
    cache.put_many(
        [
            {
                "id": NOTE_A,
                "title": "note a",
                "author_url": PROFILE_URL,
                "xsec_token": "tok-a",
                "query": "must not be stored",
            }
        ]
    )
    entry = cache.get(NOTE_A)
    assert entry is not None
    assert entry["xsec_token"] == "tok-a"
    assert entry["author_url"] == PROFILE_URL
    assert "query" not in entry
    assert stat.S_IMODE(cache.path.stat().st_mode) == 0o600

    cache.delete(NOTE_A)
    assert cache.get(NOTE_A) is None


def test_token_cache_ignores_items_without_token(cache: TokenCache) -> None:
    cache.put_many([{"id": NOTE_A, "title": "no token"}, {"id": "bad", "xsec_token": "x"}])
    assert cache.load() == {}


# ---------------------------------------------------------------------------
# daemon client
# ---------------------------------------------------------------------------


def test_client_maps_daemon_error(daemon) -> None:
    def responder(request: dict[str, Any]) -> Any:
        raise CliError("platform_blocked", "verification page")

    fake = FakeDaemon(responder)
    try:
        with pytest.raises(CliError) as excinfo:
            DaemonClient(fake.url).command("evaluate", {"code": "1"})
        assert excinfo.value.code == "platform_blocked"
    finally:
        fake.close()


def test_client_reports_unreachable_daemon() -> None:
    with pytest.raises(CliError) as excinfo:
        DaemonClient("http://127.0.0.1:1").command("evaluate", {"code": "1"})
    assert excinfo.value.code == "daemon_unreachable"


def test_goto_replaces_stuck_error_tab() -> None:
    state: dict[str, Any] = {"fresh": False, "navigations": []}
    base = _default_responder({})

    def responder(request: dict[str, Any]) -> Any:
        action = request.get("action")
        args = request.get("args") or {}
        if action == "navigate":
            state["navigations"].append(dict(args))
            if args.get("newTab"):
                state["fresh"] = True
            return {"success": True, "url": args.get("url"), "tabId": 1}
        if action == "evaluate" and args.get("code") == "location.href.startsWith('chrome-error://')":
            return {"type": "boolean", "value": not state["fresh"]}
        return base(request)

    fake = FakeDaemon(responder)
    try:
        browser = xhs.XiaohongshuBrowser(DaemonClient(fake.url))
        browser.goto(xhs.SITE + "/explore")
    finally:
        fake.close()

    actions = [request["action"] for request in fake.requests]
    assert "close_tab" in actions
    assert any(navigation.get("newTab") for navigation in state["navigations"])
    assert state["fresh"] is True


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_extracts_items_and_caches_tokens(daemon, cache: TokenCache) -> None:
    search_payload = {
        "state": {"url": xhs.SITE + "/search_result", "login": False, "blocked": False},
        "items": [
            {
                "id": NOTE_A,
                "title": "西安赏红叶",
                "author": "作者甲",
                "author_id": AUTHOR_ID,
                "author_url": PROFILE_URL,
                "url": f"{xhs.SITE}/search_result/{NOTE_A}?xsec_token=tok-a&xsec_source=",
                "xsec_token": "tok-a",
                "raw_text": "西安赏红叶\n作者甲\n1小时前",
            },
            {
                "id": NOTE_B,
                "title": "西安赏秋",
                "author": "作者乙",
                "author_id": None,
                "author_url": None,
                "url": f"{xhs.SITE}/explore/{NOTE_B}",
                "xsec_token": "tok-b",
                "raw_text": "西安赏秋\n作者乙\n3天前",
            },
        ],
    }
    fake = daemon({"const items = []": search_payload})
    result = search(
        DaemonClient(fake.url), "西安 红叶", limit=2, scroll=0, cache=cache
    )

    assert result["query"] == "西安 红叶"
    assert result["count"] == 2
    assert [item["id"] for item in result["items"]] == [NOTE_A, NOTE_B]
    assert cache.get(NOTE_A)["xsec_token"] == "tok-a"
    assert cache.get(NOTE_B)["xsec_token"] == "tok-b"
    navigated = [request for request in fake.requests if request["action"] == "navigate"]
    assert "keyword=%E8%A5%BF%E5%AE%89+%E7%BA%A2%E5%8F%B6" in navigated[-1]["args"]["url"]


def test_search_reports_login_state(daemon, cache: TokenCache) -> None:
    fake = daemon(
        {"unavailable:": {"login": True, "blocked": False}},
        wait=False,
    )
    with pytest.raises(CliError) as excinfo:
        search(DaemonClient(fake.url), "西安", limit=2, scroll=0, cache=cache)
    assert excinfo.value.code == "not_logged_in"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _detail_payload() -> dict[str, Any]:
    return {
        "url": f"{xhs.SITE}/explore/{NOTE_A}?xsec_token=tok-a",
        "title": "西安赏秋TOP10",
        "text": "正文内容",
        "author": "作者甲",
        "author_url": PROFILE_URL,
        "date": "2天前 陕西",
        "tags": ["#西安赏秋"],
        "stats": {"likes": "52", "collects": "68", "comments": "1"},
    }


def test_read_uses_cached_token(daemon, cache: TokenCache) -> None:
    cache.put_one(NOTE_A, xsec_token="tok-a", author_url=PROFILE_URL, title="西安赏秋TOP10")
    fake = daemon({"const title = text('#detail-title')": _detail_payload()})
    result = read(DaemonClient(fake.url), NOTE_A, cache=cache)

    assert result["id"] == NOTE_A
    assert result["title"] == "西安赏秋TOP10"
    assert result["token_source"] == "cache"
    navigated = [request for request in fake.requests if request["action"] == "navigate"]
    assert "xsec_token=tok-a" in navigated[-1]["args"]["url"]


def test_read_requires_a_token(daemon, cache: TokenCache) -> None:
    fake = daemon({})
    with pytest.raises(CliError) as excinfo:
        read(DaemonClient(fake.url), NOTE_A, cache=cache)
    assert excinfo.value.code == "missing_xsec_token"


def test_read_reresolves_token_from_author_profile(cache: TokenCache) -> None:
    cache.put_one(NOTE_A, xsec_token="expired", author_url=PROFILE_URL, title="旧标题")
    state = {"url": ""}

    def wait(code: str) -> bool:
        if "#detail-title" in code:
            return "fresh-token" in state["url"]
        return True

    base = _default_responder(
        {
            "unavailable:": {"unavailable": True, "url": xhs.SITE + "/404"},
            "const notes = []": {
                "url": PROFILE_URL,
                "notes": [{"id": NOTE_A, "href": PROFILE_URL, "xsec_token": "fresh-token"}],
            },
            "const title = text('#detail-title')": _detail_payload(),
        },
        wait=wait,
    )

    def responder(request: dict[str, Any]) -> Any:
        if request.get("action") == "navigate":
            state["url"] = str((request.get("args") or {}).get("url") or "")
        return base(request)

    fake = FakeDaemon(responder)
    try:
        result = read(DaemonClient(fake.url), NOTE_A, cache=cache)
    finally:
        fake.close()

    assert result["token_source"] == "author_profile"
    assert cache.get(NOTE_A)["xsec_token"] == "fresh-token"
    navigated = [request for request in fake.requests if request["action"] == "navigate"]
    assert any("fresh-token" in str(request["args"].get("url")) for request in navigated)


def test_read_requires_author_url_for_expired_token(daemon, cache: TokenCache) -> None:
    cache.put_one(NOTE_A, xsec_token="expired")
    fake = daemon(
        {"unavailable:": {"unavailable": True, "url": xhs.SITE + "/404"}},
        wait=False,
    )
    with pytest.raises(CliError) as excinfo:
        read(DaemonClient(fake.url), NOTE_A, cache=cache)
    assert excinfo.value.code == "note_unavailable"


# ---------------------------------------------------------------------------
# CLI envelope
# ---------------------------------------------------------------------------


def test_cli_prints_ok_envelope(daemon, cache: TokenCache, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache.path.parent))
    fake = daemon(
        {
            "const items = []": {
                "state": {"login": False, "blocked": False},
                "items": [
                    {
                        "id": NOTE_A,
                        "title": "西安赏秋",
                        "author": "作者甲",
                        "author_id": AUTHOR_ID,
                        "author_url": PROFILE_URL,
                        "url": f"{xhs.SITE}/explore/{NOTE_A}",
                        "xsec_token": "tok-a",
                        "raw_text": "",
                    }
                ],
            }
        }
    )
    code = xhs.main(["search", "西安", "--limit", "1", "--scroll", "0", "--daemon-url", fake.url])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["data"]["count"] == 1


def test_cli_prints_error_envelope(daemon, capsys) -> None:
    fake = daemon({})
    code = xhs.main(["read", "not-a-note-id", "--daemon-url", fake.url])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "bad_input"


def test_login_reports_site_unreachable(daemon) -> None:
    fake = daemon({"unavailable:": {"error_page": True, "url": "chrome-error://chromewebdata/"}})
    with pytest.raises(CliError) as excinfo:
        xhs.login_status(DaemonClient(fake.url))
    assert excinfo.value.code == "site_unreachable"


def test_cli_accepts_daemon_url_before_subcommand(daemon, capsys) -> None:
    fake = daemon({"logged_in: !!loggedIn": {"logged_in": True, "user": {"nickname": "三只猫"}}})
    code = xhs.main(["--daemon-url", fake.url, "login-status"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["data"] == {"logged_in": True, "user": {"nickname": "三只猫"}}
