"""A small CLI for Xiaohongshu, driven through the kimi-webbridge daemon.

The CLI runs inside the user's real, logged-in browser.  It never automates
login: the user signs in once in the browser and every later call reuses that
session.

Commands
--------
``login-status``
    Report whether the browser session is logged in.
``search <query>``
    Search notes and return id / title / author / token / URL per note.
``read <target>``
    Read a note body.  ``target`` is a note id or a note URL.

Xiaohongshu only serves a note detail page when the URL carries the short-lived
``xsec_token`` that the search result (or the author's profile) gave it.  The
CLI therefore captures tokens during ``search``, caches them per note, and can
re-resolve an expired token from the author's profile.

Output contract: every command prints one JSON envelope on stdout and exits
non-zero on error::

    {"ok": true, "data": ...}
    {"ok": false, "error": {"code": "...", "message": "..."}}

This module intentionally depends only on the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_DAEMON = "http://127.0.0.1:10086"
DAEMON_ENV = "KIMI_WEBBRIDGE_ADDR"
SESSION = "xiaohongshu"
GROUP_TITLE = "小红书 CLI"
SITE = "https://www.xiaohongshu.com"
SITE_HOST = "www.xiaohongshu.com"
DEFAULT_TIMEOUT = 60.0
WAIT_SELECTOR_MS = 20_000

NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_NOTE_PATH_RE = re.compile(r"^/(?:explore|search_result|discovery/item)/([0-9a-f]{24})/?$")
_PROFILE_NOTE_RE = re.compile(r"^/user/profile/[0-9a-f]+/([0-9a-f]{24})/?$")
_SEARCH_JS = r"""
(() => {
  const state = {
    url: location.href,
    title: document.title,
    login: location.href.includes('/login') ||
      !!document.querySelector('.login-container, .login-modal'),
    blocked: /安全验证|验证码|访问频繁|异常/.test(document.body.innerText || ''),
    snippet: (document.body.innerText || '').slice(0, 400)
  };
  const items = [];
  const seen = new Set();
  for (const s of document.querySelectorAll('section.note-item')) {
    const id = s.getAttribute('data-note-id') || '';
    if (!id || seen.has(id)) continue;
    seen.add(id);
    let href = '';
    for (const a of s.querySelectorAll('a[href*="xsec_token"]')) {
      const h = a.getAttribute('href') || '';
      if (/(?:search_result|explore)\/[0-9a-f]+/.test(h)) { href = h; break; }
    }
    let token = null;
    let abs = location.origin + '/explore/' + id;
    try {
      const u = new URL(href || ('/explore/' + id), location.origin);
      token = u.searchParams.get('xsec_token');
      abs = u.href;
    } catch (e) {}
    const titleEl = s.querySelector('a.title, .title');
    const authorEl = s.querySelector('a.author, .author');
    const profileA = s.querySelector('a[href*="/user/profile/"]');
    let authorUrl = null;
    let authorId = null;
    if (profileA) {
      try {
        const pu = new URL(profileA.getAttribute('href') || '', location.origin);
        authorUrl = pu.href;
        const m = pu.pathname.match(/\/user\/profile\/([0-9a-f]+)/);
        authorId = m ? m[1] : null;
      } catch (e) {}
    }
    const lines = (s.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
    items.push({
      id: id,
      title: (titleEl ? titleEl.innerText : (lines[0] || id)).trim(),
      author: (authorEl ? authorEl.innerText : (lines[1] || '')).split('\n')[0].trim(),
      author_id: authorId,
      author_url: authorUrl,
      url: abs,
      xsec_token: token,
      raw_text: (s.innerText || '').trim()
    });
  }
  return JSON.stringify({state: state, items: items});
})()
"""

_PROFILE_JS = r"""
(() => {
  const notes = [];
  for (const s of document.querySelectorAll('section.note-item')) {
    const id = s.getAttribute('data-note-id') || '';
    if (!id) continue;
    let href = '';
    for (const a of s.querySelectorAll('a[href]')) {
      const h = a.getAttribute('href') || '';
      if (h.includes('xsec_token') && h.includes(id)) { href = h; break; }
    }
    let token = null;
    let abs = location.origin + '/explore/' + id;
    try {
      const u = new URL(href || ('/explore/' + id), location.origin);
      token = u.searchParams.get('xsec_token');
      abs = u.href;
    } catch (e) {}
    notes.push({id: id, href: abs, xsec_token: token});
  }
  return JSON.stringify({url: location.href, notes: notes});
})()
"""

_DETAIL_JS = r"""
(() => {
  const text = (sel) => {
    const e = document.querySelector(sel);
    return e ? e.innerText.trim() : null;
  };
  const title = text('#detail-title');
  const desc = text('#detail-desc');
  const content = text('.note-content');
  const authorEl = document.querySelector(
    '.author-wrapper .username, .author-wrapper .name, .username'
  );
  const linkEl = document.querySelector(
    '.author-wrapper a[href*="/user/profile/"], a.author[href*="/user/profile/"]'
  );
  let authorUrl = null;
  if (linkEl) {
    try { authorUrl = new URL(linkEl.getAttribute('href'), location.origin).href; } catch (e) {}
  }
  const tags = [...document.querySelectorAll('#detail-desc a, .note-content a')]
    .map(a => (a.innerText || '').trim())
    .filter(t => t.startsWith('#'));
  return JSON.stringify({
    url: location.href,
    title: title,
    text: (desc || content || '').trim(),
    author: authorEl ? authorEl.innerText.trim() : null,
    author_url: authorUrl,
    date: text('.date, .bottom-container .date, #detail-date'),
    tags: tags,
    stats: {
      likes: text('.like-wrapper .count'),
      collects: text('.collect-wrapper .count'),
      comments: text('.chat-wrapper .count')
    }
  });
})()
"""

_LOGIN_JS = r"""
(() => {
  const u = (window.__INITIAL_STATE__ || {}).user || {};
  const loggedIn = u.loggedIn && typeof u.loggedIn === 'object' ? u.loggedIn.value : u.loggedIn;
  const info = u.userInfo && typeof u.userInfo === 'object' ? (u.userInfo.value || u.userInfo) : u.userInfo;
  return JSON.stringify({
    logged_in: !!loggedIn,
    user: info ? {nickname: info.nickname || null, user_id: info.userId || null} : null,
    url: location.href
  });
})()
"""

_PAGE_STATE_JS = r"""
(() => JSON.stringify({
  url: location.href,
  title: document.title,
  login: location.href.includes('/login') ||
    !!document.querySelector('.login-container, .login-modal'),
  blocked: /安全验证|验证码|访问频繁|异常/.test(document.body.innerText || ''),
  unavailable: location.pathname.startsWith('/404') ||
    /暂时无法浏览|你访问的页面不见了/.test(document.body.innerText || ''),
  error_page: location.href.startsWith('chrome-error://') ||
    /ERR_(?:TIMED_OUT|CONNECTION|NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|PROXY)/.test(
      document.body.innerText || ''
    ),
  snippet: (document.body.innerText || '').slice(0, 400)
}))()
"""

_SELECTOR_PRESENT_JS = "!!document.querySelector({selector})"


class CliError(Exception):
    """An error that maps to the CLI's JSON error envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# daemon client
# ---------------------------------------------------------------------------


def discover_daemon_url(explicit: Optional[str] = None) -> str:
    """Resolve the daemon address from the flag, env, addr file, then default."""
    if explicit:
        return normalize_daemon_url(explicit)
    addr_file = Path.home() / ".kimi-webbridge" / "daemon.addr"
    for candidate in (os.environ.get(DAEMON_ENV), _read_addr_file(addr_file)):
        if candidate:
            try:
                return normalize_daemon_url(candidate)
            except CliError:
                continue
    return DEFAULT_DAEMON


def _read_addr_file(path: Path) -> Optional[str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def normalize_daemon_url(value: str) -> str:
    """Normalize ``host:port`` into a loopback URL; refuse remote hosts."""
    raw = str(value).strip()
    if not raw:
        raise CliError("bad_input", "empty daemon address")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise CliError("bad_input", f"unsupported daemon scheme {parts.scheme!r}")
    if parts.username or parts.password:
        raise CliError("bad_input", "daemon address must not contain credentials")
    host = parts.hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise CliError(
            "bad_input",
            f"refusing non-loopback daemon address {host!r}; "
            "the browser daemon must stay on this machine",
        )
    netloc = parts.netloc
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


class DaemonClient:
    """Minimal JSON-over-HTTP client for the kimi-webbridge daemon."""

    def __init__(self, base_url: str, *, session: str = SESSION, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = normalize_daemon_url(base_url)
        self.session = session
        self.timeout = timeout

    def command(self, action: str, args: Optional[dict[str, Any]] = None) -> Any:
        body = json.dumps(
            {"action": action, "args": dict(args or {}), "session": self.session},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/command",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _safe_read(exc)
            raise CliError(
                "daemon_error",
                f"daemon returned HTTP {exc.code}" + (f": {detail}" if detail else ""),
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CliError(
                "daemon_unreachable",
                f"cannot reach the kimi-webbridge daemon at {self.base_url}: {reason}",
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CliError("daemon_error", "daemon returned a non-JSON response") from None
        if payload.get("ok") is not True:
            error = payload.get("error") or {}
            raise CliError(
                str(error.get("code") or "daemon_error"),
                str(error.get("message") or "daemon command failed"),
            )
        return payload.get("data")

    def evaluate(self, code: str) -> Any:
        data = self.command("evaluate", {"code": code})
        return data.get("value") if isinstance(data, dict) else data

    def evaluate_json(self, code: str) -> Any:
        value = self.evaluate(code)
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise CliError("daemon_error", "browser returned invalid JSON") from None


def _safe_read(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:  # pragma: no cover - defensive
        return ""


# ---------------------------------------------------------------------------
# browser helpers
# ---------------------------------------------------------------------------


class XiaohongshuBrowser:
    """Tab management, navigation and readiness waits for one browser session."""

    def __init__(self, client: DaemonClient) -> None:
        self.client = client
        self._tab_ready = False

    def ensure_tab(self) -> None:
        if self._tab_ready:
            return
        try:
            self.client.command("find_tab", {"url": SITE, "active": False})
        except CliError:
            try:
                self.client.command(
                    "navigate",
                    {"url": SITE + "/explore", "newTab": True, "group_title": GROUP_TITLE},
                )
            except CliError as exc:
                if not _is_load_timeout(exc):
                    raise
                self._attach_site_tab()
        self._tab_ready = True

    def goto(self, url: str) -> None:
        self.ensure_tab()
        for attempt in range(2):
            try:
                data = self.client.command("navigate", {"url": url, "newTab": False})
            except CliError as exc:
                if not _is_load_timeout(exc):
                    raise
                self._attach_site_tab()
            else:
                if isinstance(data, dict) and data.get("success") is False:
                    self._attach_site_tab()
            if not self._on_error_page():
                return
            if attempt == 0:
                # The reused tab is stuck on a browser error page (a dead
                # proxy/renderer survives in-place navigation); replace it.
                self._replace_tab(url)
        raise CliError(
            "site_unreachable",
            "the browser could not load Xiaohongshu; check the connection and retry",
        )

    def _on_error_page(self) -> bool:
        try:
            return _truthy(
                self.client.evaluate("location.href.startsWith('chrome-error://')")
            )
        except CliError:
            return False

    def _replace_tab(self, url: str) -> None:
        try:
            self.client.command("close_tab", {})
        except CliError:
            pass
        try:
            self.client.command(
                "navigate", {"url": url, "newTab": True, "group_title": GROUP_TITLE}
            )
        except CliError as exc:
            if not _is_load_timeout(exc):
                raise
            self._attach_site_tab()
        self._tab_ready = True

    def _attach_site_tab(self) -> None:
        """Attach the tab a timed-out navigation left behind.

        Client-rendered pages keep a connection open and never fire ``load``,
        so ``navigate`` can report a timeout while the page is already usable.
        """
        data = self.client.command("list_browser_tabs", {})
        tabs = data.get("tabs") if isinstance(data, dict) else None
        candidates = [
            tab
            for tab in (tabs or [])
            if isinstance(tab, dict) and SITE_HOST in str(tab.get("url") or "")
        ]
        if not candidates:
            raise CliError("navigation_failed", "navigation timed out and no site tab was found")
        newest = max(candidates, key=lambda tab: int(tab.get("tabId") or 0))
        self.client.command("attach_tab", {"tabId": newest.get("tabId")})

    def wait_for(self, selector: str, *, timeout_ms: Optional[int] = None) -> bool:
        budget = WAIT_SELECTOR_MS if timeout_ms is None else timeout_ms
        expression = _SELECTOR_PRESENT_JS.format(selector=json.dumps(selector))
        deadline = time.monotonic() + budget / 1000
        while True:
            try:
                if _truthy(self.client.evaluate(expression)):
                    return True
            except CliError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.6)

    def scroll_bottom(self) -> None:
        try:
            self.client.evaluate('window.scrollTo(0, document.body.scrollHeight); "ok"')
        except CliError:
            pass

    def page_state(self) -> dict[str, Any]:
        data = self.client.evaluate_json(_PAGE_STATE_JS)
        return data if isinstance(data, dict) else {}


def _is_load_timeout(exc: CliError) -> bool:
    message = exc.message.lower()
    return "timeout" in message and ("load" in message or "navigate" in message)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _raise_for_state(state: dict[str, Any], action: str) -> None:
    if state.get("error_page"):
        raise CliError(
            "site_unreachable",
            "the browser could not load Xiaohongshu (network or proxy error); "
            "check the connection and retry",
        )
    if state.get("unavailable"):
        raise CliError(
            "note_unavailable",
            "Xiaohongshu says this note is temporarily unavailable; the xsec_token "
            "is missing or expired. Run `search` again or pass --author-url.",
        )
    if state.get("login"):
        raise CliError(
            "not_logged_in",
            "Xiaohongshu requires a logged-in browser; sign in in the browser and retry",
        )
    if state.get("blocked"):
        raise CliError(
            "platform_blocked",
            "Xiaohongshu returned a verification page; resolve it in the browser "
            "and retry (do not hammer the site)",
        )
    raise CliError(f"{action}_timeout", f"Xiaohongshu did not render the {action} page in time")


# ---------------------------------------------------------------------------
# token cache
# ---------------------------------------------------------------------------


class TokenCache:
    """Per-note ``xsec_token`` cache under ``$XDG_CACHE_HOME/xiaohongshu-cli``.

    Stores only note ids, short-lived tokens, author URLs and titles.  Query
    text is never written.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_cache_path()

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        notes = data.get("notes") if isinstance(data, dict) else None
        return notes if isinstance(notes, dict) else {}

    def get(self, note_id: str) -> Optional[dict[str, Any]]:
        entry = self.load().get(note_id)
        return entry if isinstance(entry, dict) else None

    def put_many(self, items: list[dict[str, Any]]) -> None:
        notes = self.load()
        now = time.time()
        changed = False
        for item in items:
            note_id = str(item.get("id") or "")
            token = item.get("xsec_token")
            if not NOTE_ID_RE.match(note_id) or not token:
                continue
            entry = notes.get(note_id) or {}
            entry.update(
                {
                    "xsec_token": token,
                    "author_url": item.get("author_url") or entry.get("author_url"),
                    "title": item.get("title") or entry.get("title"),
                    "updated_at": now,
                }
            )
            notes[note_id] = entry
            changed = True
        if changed:
            self._write({"version": 1, "notes": notes})

    def put_one(self, note_id: str, **fields: Any) -> None:
        self.put_many([{"id": note_id, **fields}])

    def delete(self, note_id: str) -> None:
        notes = self.load()
        if note_id in notes:
            del notes[note_id]
            self._write({"version": 1, "notes": notes})

    def _write(self, payload: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            # A read-only or full disk must not break a read that already worked.
            pass


def default_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "xiaohongshu-cli" / "notes.json"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def login_status(client: DaemonClient) -> dict[str, Any]:
    browser = XiaohongshuBrowser(client)
    browser.goto(SITE + "/explore")
    state = browser.page_state()
    if state.get("error_page"):
        _raise_for_state(state, "login")
    browser.wait_for("#app, .main-container, .side-bar", timeout_ms=10_000)
    data = client.evaluate_json(_LOGIN_JS)
    if not isinstance(data, dict):
        raise CliError("daemon_error", "browser returned an unexpected login payload")
    if not data.get("logged_in"):
        raise CliError(
            "not_logged_in",
            "Xiaohongshu is not logged in; sign in at https://www.xiaohongshu.com "
            "in the browser, then run this command again",
        )
    return {"logged_in": True, "user": data.get("user")}


def search(
    client: DaemonClient,
    query: str,
    *,
    limit: int = 10,
    scroll: int = 1,
    cache: Optional[TokenCache] = None,
) -> dict[str, Any]:
    if not str(query).strip():
        raise CliError("bad_input", "query must not be empty")
    if limit < 1 or limit > 50:
        raise CliError("bad_input", "limit must be between 1 and 50")
    if scroll < 0 or scroll > 20:
        raise CliError("bad_input", "scroll must be between 0 and 20")

    browser = XiaohongshuBrowser(client)
    url = SITE + "/search_result?" + urllib.parse.urlencode(
        {"keyword": query, "source": "web_search_result_notes"}
    )
    browser.goto(url)
    if not browser.wait_for("section.note-item"):
        _raise_for_state(browser.page_state(), "search")

    payload: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    for attempt in range(scroll + 1):
        data = client.evaluate_json(_SEARCH_JS)
        if isinstance(data, dict):
            payload = data
            items = [item for item in data.get("items", []) if isinstance(item, dict)]
        if len(items) >= limit:
            break
        if attempt < scroll:
            browser.scroll_bottom()
            time.sleep(1.5)

    if not items:
        state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
        _raise_for_state(state, "search")

    items = _dedupe(items)[:limit]
    (cache or TokenCache()).put_many(items)
    return {"query": query, "count": len(items), "items": items}


def read(
    client: DaemonClient,
    target: str,
    *,
    token: Optional[str] = None,
    author_url: Optional[str] = None,
    refresh: bool = False,
    cache: Optional[TokenCache] = None,
) -> dict[str, Any]:
    note_id, url_token = parse_target(target)
    cache = cache or TokenCache()
    entry = cache.get(note_id) or {}
    resolved = token or url_token or (None if refresh else entry.get("xsec_token"))
    source = "argument" if (token or url_token) else ("cache" if resolved else None)
    known_author = author_url or entry.get("author_url")

    if not resolved and not known_author:
        raise CliError(
            "missing_xsec_token",
            "this note needs an xsec_token and none is cached; run `search` for it "
            "first, or pass --token / --author-url from a search result",
        )
    if not resolved:
        resolved, source = _token_from_author(client, known_author, note_id), "author_profile"

    try:
        data = _read_note(client, note_id, resolved)
    except CliError as exc:
        if exc.code != "note_unavailable":
            raise
        # The cached token expired; re-resolve from the author profile once.
        cache.delete(note_id)
        if source == "author_profile" or not known_author:
            raise
        resolved, source = _token_from_author(client, known_author, note_id), "author_profile"
        data = _read_note(client, note_id, resolved)

    cache.put_one(
        note_id,
        xsec_token=resolved,
        author_url=data.get("author_url") or known_author,
        title=data.get("title"),
    )
    data["id"] = note_id
    data["token_source"] = source
    return data


def _read_note(client: DaemonClient, note_id: str, token: Optional[str]) -> dict[str, Any]:
    browser = XiaohongshuBrowser(client)
    query = {"xsec_source": "pc_user"}
    if token:
        query["xsec_token"] = token
    url = f"{SITE}/explore/{note_id}?" + urllib.parse.urlencode(query)
    browser.goto(url)
    if not browser.wait_for("#detail-title, #detail-desc, .note-content"):
        _raise_for_state(browser.page_state(), "read")
    data = client.evaluate_json(_DETAIL_JS)
    if not isinstance(data, dict):
        raise CliError("daemon_error", "browser returned an unexpected note payload")
    if not data.get("title") and not data.get("text"):
        _raise_for_state(browser.page_state(), "read")
    return data


def _token_from_author(
    client: DaemonClient, author_url: Optional[str], note_id: str
) -> str:
    if not author_url:
        raise CliError("missing_xsec_token", "no author profile URL is available")
    if author_url.startswith("/"):
        author_url = SITE + author_url
    parsed = urllib.parse.urlsplit(author_url)
    if not parsed.query or "xsec_token=" not in parsed.query:
        raise CliError(
            "missing_xsec_token",
            "the author profile URL needs its own xsec_token (take it from a search result)",
        )
    browser = XiaohongshuBrowser(client)
    browser.goto(author_url)
    if not browser.wait_for("section.note-item", timeout_ms=15_000):
        _raise_for_state(browser.page_state(), "profile")
    for attempt in range(6):
        data = client.evaluate_json(_PROFILE_JS)
        notes = data.get("notes") if isinstance(data, dict) else None
        for note in notes or []:
            if isinstance(note, dict) and note.get("id") == note_id and note.get("xsec_token"):
                return str(note["xsec_token"])
        if attempt < 5:
            browser.scroll_bottom()
            time.sleep(1.8)
    raise CliError(
        "token_not_found",
        f"note {note_id} was not found on the author's profile; it may be deleted or private",
    )


def parse_target(target: str) -> tuple[str, Optional[str]]:
    """Return ``(note_id, xsec_token)`` from a note id or any note URL."""
    raw = str(target or "").strip()
    if not raw:
        raise CliError("bad_input", "note target must not be empty")
    if NOTE_ID_RE.match(raw):
        return raw, None

    if "://" in raw:
        candidate = raw
    elif raw.startswith("/"):
        candidate = SITE + raw
    else:
        candidate = "https://" + raw
    parts = urllib.parse.urlsplit(candidate)
    path = parts.path.rstrip("/")
    note_id = None
    for pattern in (_PROFILE_NOTE_RE, _NOTE_PATH_RE):
        match = pattern.match(path)
        if match:
            note_id = match.group(1)
            break
    if not note_id:
        raise CliError(
            "bad_input",
            "target must be a note id or a URL like "
            "https://www.xiaohongshu.com/explore/<id>?xsec_token=...",
        )
    query = urllib.parse.parse_qs(parts.query)
    token = (query.get("xsec_token") or [None])[0]
    return note_id, token or None


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        note_id = str(item.get("id") or "")
        if not note_id or note_id in seen:
            continue
        seen.add(note_id)
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    def add_daemon_flag(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--daemon-url",
            default=argparse.SUPPRESS,
            help=(
                "kimi-webbridge daemon URL (default: $KIMI_WEBBRIDGE_ADDR, "
                "~/.kimi-webbridge/daemon.addr, then http://127.0.0.1:10086)"
            ),
        )

    common = argparse.ArgumentParser(add_help=False)
    add_daemon_flag(common)

    parser = argparse.ArgumentParser(
        prog="xiaohongshu-cli",
        description="Search and read Xiaohongshu notes through the user's logged-in browser.",
    )
    add_daemon_flag(parser)
    parser.set_defaults(daemon_url=None)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("login-status", parents=[common], help="report the browser login state")
    status.set_defaults(handler=_cmd_login_status)

    search_parser = sub.add_parser("search", parents=[common], help="search notes")
    search_parser.add_argument("query", help="search keywords")
    search_parser.add_argument("--limit", type=int, default=10, help="maximum notes (1-50, default 10)")
    search_parser.add_argument(
        "--scroll", type=int, default=1, help="extra scroll passes to load more notes (default 1)"
    )
    search_parser.set_defaults(handler=_cmd_search)

    read_parser = sub.add_parser("read", parents=[common], help="read a note body")
    read_parser.add_argument("target", help="note id or note URL")
    read_parser.add_argument("--token", default=None, help="xsec_token when the target URL has none")
    read_parser.add_argument(
        "--author-url",
        default=None,
        help="author profile URL (with its own xsec_token) for token re-resolution",
    )
    read_parser.add_argument(
        "--refresh", action="store_true", help="ignore the cached token and re-resolve it"
    )
    read_parser.set_defaults(handler=_cmd_read)

    return parser


def _client(args: argparse.Namespace) -> DaemonClient:
    return DaemonClient(discover_daemon_url(getattr(args, "daemon_url", None)))


def _cmd_login_status(args: argparse.Namespace) -> dict[str, Any]:
    return login_status(_client(args))


def _cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    return search(_client(args), args.query, limit=args.limit, scroll=args.scroll)


def _cmd_read(args: argparse.Namespace) -> dict[str, Any]:
    return read(
        _client(args),
        args.target,
        token=args.token,
        author_url=args.author_url,
        refresh=args.refresh,
    )


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], dict[str, Any]] = args.handler
    try:
        data = handler(args)
    except CliError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "message": exc.message}})
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        _emit({"ok": False, "error": {"code": "interrupted", "message": "interrupted"}})
        return 130
    _emit({"ok": True, "data": data})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
