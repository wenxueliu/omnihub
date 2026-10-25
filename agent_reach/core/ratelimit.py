"""Cross-process cooldowns, backend pauses and per-source pacing.

The ledger is advisory and fails open: a corrupt, unreadable or unwritable
state file never blocks a request. Entries contain only platform and source
names plus timestamps -- never inputs, URLs or credentials.

Scopes:

- ``platform`` cooldown: ``RATE_LIMITED`` / ``PLATFORM_BLOCKED`` pause every
  source of that platform, because the limit applies to the site.
- ``source`` pause: ``TIMEOUT`` / ``UPSTREAM_BROKEN`` / ``NEED_SETUP`` pause
  one backend so other sources of the same platform can still answer.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from agent_reach.core.config import state_dir
from agent_reach.core.result import (
    NEED_SETUP,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
)

STATE_VERSION = 1
STATE_FILE = "rate_limits.json"
LOCK_FILE = ".rate_limits.lock"

DEFAULT_COOLDOWN = 300.0
BLOCKED_COOLDOWN = 900.0
MAX_COOLDOWN = 6 * 60 * 60.0
MAX_PACING = 10.0
BACKEND_PAUSES = {TIMEOUT: 60.0, UPSTREAM_BROKEN: 120.0, NEED_SETUP: 60.0}
RETENTION = 24 * 60 * 60.0

_WRITE_LOCK = threading.Lock()


def default_state_path() -> Path:
    return state_dir() / STATE_FILE


def _seconds(
    value: Any, *, default: float, ceiling: float, minimum: float = 1.0
) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds):
        return default
    return min(max(seconds, minimum), ceiling)


class RateLimits:
    """Read and update the rate-limit ledger for one action."""

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else default_state_path()
        self._now = now
        self._sleep = sleep

    # -- keys ------------------------------------------------------------
    @staticmethod
    def platform_key(platform_id: str) -> str:
        return f"platform:{platform_id}"

    @staticmethod
    def source_key(platform_id: str, source_label: str) -> str:
        return f"source:{platform_id}:{source_label}"

    # -- reads -----------------------------------------------------------
    def blocked(
        self, platform_id: str, source_label: Optional[str] = None
    ) -> Optional[tuple[float, str, str]]:
        """Return ``(seconds, code, scope)`` if this platform/source is cooling."""
        now = self._now()
        state = self._load()
        candidates = [(self.platform_key(platform_id), "cooldowns", "platform")]
        if source_label is not None:
            candidates.append(
                (self.source_key(platform_id, source_label), "pauses", "source")
            )
        best: Optional[tuple[float, str, str]] = None
        for key, bucket_name, scope in candidates:
            entry = state[bucket_name].get(key)
            if not entry:
                continue
            remaining = float(entry.get("until") or 0) - now
            if remaining <= 0:
                continue
            code = str(entry.get("code") or RATE_LIMITED)
            if best is None or remaining > best[0]:
                best = (remaining, code, scope)
        return best

    # -- writes ----------------------------------------------------------
    def note_error(
        self,
        platform_id: str,
        source_label: str,
        code: str,
        *,
        retry_after: Optional[float] = None,
    ) -> Optional[float]:
        """Record a cooldown or pause; return how long it lasts, if any."""
        now = self._now()
        if code in (RATE_LIMITED, PLATFORM_BLOCKED):
            default = DEFAULT_COOLDOWN if code == RATE_LIMITED else BLOCKED_COOLDOWN
            seconds = _seconds(retry_after, default=default, ceiling=MAX_COOLDOWN)
            key = self.platform_key(platform_id)

            def mutate(state: dict) -> None:
                entry = state["cooldowns"].get(key) or {}
                until = max(float(entry.get("until") or 0), now + seconds)
                state["cooldowns"][key] = {"code": code, "until": until, "at": now}

            self._mutate(mutate)
            return seconds
        pause = BACKEND_PAUSES.get(code)
        if pause is None:
            return None
        seconds = _seconds(pause, default=pause, ceiling=MAX_COOLDOWN)
        key = self.source_key(platform_id, source_label)

        def mutate(state: dict) -> None:
            entry = state["pauses"].get(key) or {}
            until = max(float(entry.get("until") or 0), now + seconds)
            state["pauses"][key] = {"code": code, "until": until, "at": now}

        self._mutate(mutate)
        return seconds

    def note_success(self, platform_id: str, source_label: str) -> None:
        """Clear this backend's pause; a platform cooldown ages out on its own."""
        key = self.source_key(platform_id, source_label)
        if key not in self._load()["pauses"]:
            return

        def mutate(state: dict) -> None:
            state["pauses"].pop(key, None)

        self._mutate(mutate)

    def begin(
        self,
        platform_id: str,
        source_label: str,
        *,
        min_interval: Optional[float] = None,
    ) -> float:
        """Pace repeated calls to one source; return the seconds slept."""
        if not min_interval:
            return 0.0
        interval = _seconds(
            min_interval, default=MAX_PACING, ceiling=MAX_PACING, minimum=0.0
        )
        if interval <= 0:
            return 0.0
        key = self.source_key(platform_id, source_label)
        waited = 0.0
        last = self._load()["last_attempt"].get(key)
        if last is not None:
            remaining = float(last) + interval - self._now()
            if remaining > 0:
                waited = min(remaining, interval)
                self._sleep(waited)

        def mutate(state: dict) -> None:
            state["last_attempt"][key] = self._now()

        self._mutate(mutate)
        return waited

    # -- state file ------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, Mapping):
            data = {}
        return {
            "cooldowns": self._entries(data.get("cooldowns")),
            "pauses": self._entries(data.get("pauses")),
            "last_attempt": self._timestamps(data.get("last_attempt")),
        }

    @staticmethod
    def _entries(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, Mapping):
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in value.items():
            if not isinstance(entry, Mapping):
                continue
            try:
                until = float(entry.get("until"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(until):
                continue
            entries[str(key)] = {
                "code": str(entry.get("code") or RATE_LIMITED),
                "until": until,
            }
        return entries

    @staticmethod
    def _timestamps(value: Any) -> dict[str, float]:
        if not isinstance(value, Mapping):
            return {}
        stamps: dict[str, float] = {}
        for key, stamp in value.items():
            try:
                parsed = float(stamp)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                stamps[str(key)] = parsed
        return stamps

    def _mutate(self, mutate: Callable[[dict], None]) -> None:
        if self.path.is_symlink():
            return
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError:
            return
        with _WRITE_LOCK:
            with self._exclusive(directory):
                state = self._load()
                mutate(state)
                self._prune(state)
                self._save(directory, state)

    @contextmanager
    def _exclusive(self, directory: Path) -> Iterator[None]:
        """Best-effort cross-process lock; pacing data may be lost, not block."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX
            yield
            return
        try:
            handle = (directory / LOCK_FILE).open("a+")
        except OSError:
            yield
            return
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                yield
                return
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _save(self, directory: Path, state: dict) -> None:
        payload = {"version": STATE_VERSION, **state}
        handle = None
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".rate-limits-", dir=str(directory))
            handle = os.fdopen(fd, "w", encoding="utf-8")
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.chmod(tmp_path, 0o600)
            if self.path.is_symlink():
                return
            os.replace(tmp_path, self.path)
        except OSError:
            return
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _prune(self, state: dict) -> None:
        now = self._now()
        for bucket in ("cooldowns", "pauses"):
            for key in [
                key
                for key, entry in state[bucket].items()
                if float(entry.get("until") or 0) <= now
            ]:
                state[bucket].pop(key, None)
        for key in [
            key
            for key, stamp in state["last_attempt"].items()
            if stamp < now - RETENTION
        ]:
            state["last_attempt"].pop(key, None)
