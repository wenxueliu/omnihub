"""Concrete execution context: bounded subprocesses, safe HTTP and secrets.

Adapters receive one ``Context`` per action. It is the only place that spawns
processes or opens sockets, which keeps adapters small and makes the policy
enforceable in shared code rather than in platform YAML.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import requests
import yaml

from agent_reach.adapters.base import ProcessResult, RawResult
from agent_reach.core.policy import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    clamp_max_bytes,
    clamp_timeout,
    ensure_public_url,
)
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_LOGIN,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
)
from agent_reach.utils.process import utf8_subprocess_env

_MAX_REDIRECTS = 5
_SECRETS_FILE = ".agent-reach/secrets.yaml"
_DEFAULT_USER_AGENT = "reach-platforms/0.1 (+https://github.com/)"


class CommandFailed(ReachError):
    """A child process exited non-zero; the message stays human-readable."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(code, message)
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    return scheme, (parts.hostname or "").lower(), parts.port or (443 if scheme == "https" else 80)


def _read_limited(pipe: Any, limit: int) -> tuple[bytes, bool]:
    """Drain a pipe, keeping at most *limit* bytes in memory."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = pipe.read(65536)
        if not chunk:
            break
        if total < limit:
            keep = chunk[: limit - total]
            chunks.append(keep)
            total += len(keep)
        if total >= limit:
            truncated = True
    return b"".join(chunks), truncated


def _command_error_code(stderr: str) -> str:
    text = (stderr or "").lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return RATE_LIMITED
    if "401" in text or "login" in text or "log in" in text or "sign in" in text or "auth" in text:
        return NEED_LOGIN
    if "captcha" in text or "verify" in text or "verification" in text or "blocked" in text:
        return PLATFORM_BLOCKED
    if "404" in text or "not found" in text:
        return NOT_FOUND
    return UPSTREAM_BROKEN


class Context:
    """Action-scoped resource owner implementing the adapter ``Context`` protocol."""

    def __init__(
        self,
        *,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
        user_agent: str = _DEFAULT_USER_AGENT,
        secrets_path: Optional[Path | str] = None,
    ) -> None:
        self.timeout = clamp_timeout(timeout, default=DEFAULT_TIMEOUT)
        self.max_bytes = clamp_max_bytes(max_bytes, default=DEFAULT_MAX_BYTES)
        self.user_agent = user_agent
        self._secrets_path = (
            Path(secrets_path).expanduser()
            if secrets_path is not None
            else Path.home() / _SECRETS_FILE
        )
        self._secrets: Optional[Mapping[str, Any]] = None
        self._secrets_stamp: Optional[tuple[int, int]] = None

    # -- subprocess -----------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
        input_data: Optional[str] = None,
        cwd: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> ProcessResult:
        if not argv:
            raise ReachError(BAD_INPUT, "empty command")
        parts = [str(part) for part in argv]
        executable = parts[0]
        if os.sep in executable or (os.altsep and os.altsep in executable):
            resolved = executable
            if not Path(resolved).is_file():
                raise ReachError(NEED_SETUP, f"missing executable: {executable}")
        else:
            resolved = shutil.which(executable)
            if not resolved:
                raise ReachError(NEED_SETUP, f"missing executable: {executable}")
        parts[0] = resolved

        seconds = clamp_timeout(timeout, default=self.timeout)
        cap = clamp_max_bytes(max_bytes, default=self.max_bytes)
        child_env = utf8_subprocess_env({**os.environ, **dict(env or {})})
        popen_kwargs: dict[str, Any] = {
            "args": parts,
            "stdin": subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(Path(cwd).expanduser() if cwd else Path.home()),
            "env": child_env,
            "start_new_session": os.name == "posix",
        }
        try:
            process = subprocess.Popen(**popen_kwargs)
        except FileNotFoundError:
            raise ReachError(NEED_SETUP, f"missing executable: {parts[0]}") from None
        except OSError as exc:
            raise ReachError(UPSTREAM_BROKEN, f"cannot start {parts[0]!r}: {exc}") from None

        if input_data is not None and process.stdin is not None:
            try:
                process.stdin.write(input_data.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        captured: dict[str, tuple[bytes, bool]] = {}

        def reader(name: str, pipe: Any) -> None:
            captured[name] = _read_limited(pipe, cap)

        threads = [
            threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
            else:
                process.kill()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                returncode = -9
        for thread in threads:
            thread.join(timeout=5)

        out_bytes, out_truncated = captured.get("stdout", (b"", False))
        err_bytes, err_truncated = captured.get("stderr", (b"", False))
        stdout = out_bytes.decode("utf-8", errors="replace")
        stderr = err_bytes.decode("utf-8", errors="replace")

        if timed_out:
            raise ReachError(TIMEOUT, f"command timed out after {seconds:g}s: {parts[0]}")
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
            raise CommandFailed(
                _command_error_code(stderr),
                f"{parts[0]} failed: {detail[:500]}",
                argv=parts,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return ProcessResult(
            stdout=stdout,
            stderr=stderr,
            returncode=int(returncode),
            truncated=bool(out_truncated or err_truncated),
        )

    # -- HTTP -----------------------------------------------------------
    def http(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Optional[Mapping[str, str]] = None,
        data: Any = None,
        timeout: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> RawResult:
        current = ensure_public_url(url)
        verb = str(method or "GET").upper()
        seconds = clamp_timeout(timeout, default=self.timeout)
        cap = clamp_max_bytes(max_bytes, default=self.max_bytes)
        request_headers = {"User-Agent": self.user_agent}
        request_headers.update({str(key): str(value) for key, value in (headers or {}).items()})
        payload = data
        response: Optional[requests.Response] = None

        for _ in range(_MAX_REDIRECTS + 1):
            try:
                response = requests.request(
                    verb,
                    current,
                    headers=request_headers,
                    data=payload,
                    timeout=seconds,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.Timeout:
                raise ReachError(TIMEOUT, f"upstream did not answer within {seconds:g}s") from None
            except requests.RequestException as exc:
                raise ReachError(UPSTREAM_BROKEN, f"network error: {exc}") from None

            location = response.headers.get("Location")
            if response.status_code in {301, 302, 303, 307, 308} and location:
                next_url = ensure_public_url(urljoin(current, location))
                if _origin(current) != _origin(next_url):
                    # Never forward credentials across an origin change.
                    for key in list(request_headers):
                        if key.lower() in {"authorization", "cookie", "proxy-authorization"}:
                            request_headers.pop(key, None)
                response.close()
                current = next_url
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and verb == "POST"
                ):
                    verb = "GET"
                    payload = None
                continue
            break
        else:
            if response is not None:
                response.close()
            raise ReachError(UPSTREAM_BROKEN, "too many redirects")

        assert response is not None
        with response:
            status = response.status_code
            if status >= 400:
                body = self._read(response, 4096, strict=False)[0]
                snippet = body.decode("utf-8", errors="replace").strip()[:300]
                if status == 404:
                    raise ReachError(NOT_FOUND, f"HTTP 404: {snippet or 'not found'}")
                if status == 429:
                    raise ReachError(RATE_LIMITED, "HTTP 429")
                # Transport-level 401/403 are retryable with another source.
                # Login walls and captchas are recognized by tool adapters.
                raise ReachError(UPSTREAM_BROKEN, f"HTTP {status}: {snippet or 'upstream error'}")
            if 300 <= status < 400:
                raise ReachError(
                    UPSTREAM_BROKEN, f"HTTP {status} without a usable Location header"
                )

            raw_body, truncated = self._read(response, cap, strict=True)
            content_type = response.headers.get("Content-Type", "")
            text = raw_body.decode("utf-8", errors="replace")
            parsed: Any = None
            if "json" in content_type.lower():
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
            elif text.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
            return RawResult(
                ok=True,
                data=parsed,
                text=text,
                content_type=content_type,
                meta={
                    "url": current,
                    "status": status,
                    "bytes": len(raw_body),
                    "truncated": truncated,
                },
            )

    @staticmethod
    def _read(response: requests.Response, cap: int, *, strict: bool) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            if total + len(chunk) > cap:
                if strict:
                    raise ReachError(
                        UPSTREAM_BROKEN, f"response exceeds the {cap}-byte limit"
                    )
                chunks.append(chunk[: cap - total])
                return b"".join(chunks), True
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), False

    # -- secrets --------------------------------------------------------
    def secret(self, name: str) -> Optional[str]:
        for key in (name, name.upper()):
            value = os.environ.get(key)
            if value:
                return value
        self._load_secrets()
        value = (self._secrets or {}).get(name)
        if value is None:
            value = (self._secrets or {}).get(name.upper())
        return None if value is None else str(value)

    def _load_secrets(self) -> None:
        try:
            stat = self._secrets_path.stat()
        except OSError:
            self._secrets = {}
            self._secrets_stamp = None
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._secrets is not None and stamp == self._secrets_stamp:
            return
        try:
            loaded = yaml.safe_load(self._secrets_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            loaded = {}
        self._secrets = loaded if isinstance(loaded, Mapping) else {}
        self._secrets_stamp = stamp
