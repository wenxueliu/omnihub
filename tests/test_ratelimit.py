from __future__ import annotations

import json
import os

import pytest

from agent_reach.core.config import state_dir
from agent_reach.core.ratelimit import (
    BACKEND_PAUSES,
    BLOCKED_COOLDOWN,
    DEFAULT_COOLDOWN,
    MAX_COOLDOWN,
    MAX_PACING,
    RateLimits,
    default_state_path,
)
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    NOT_FOUND,
    PLATFORM_BLOCKED,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
)


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _limiter(tmp_path, clock=None, sleeps=None):
    clock = clock or Clock()
    sleeps = sleeps if sleeps is not None else []
    limiter = RateLimits(
        tmp_path / "state" / "rate_limits.json", now=clock, sleep=sleeps.append
    )
    return limiter, clock, sleeps


def test_rate_limit_cools_the_whole_platform(tmp_path):
    limiter, clock, _ = _limiter(tmp_path)
    assert limiter.note_error("reddit", "exa", RATE_LIMITED, retry_after=42) == 42
    remaining, code, scope = limiter.blocked("reddit")
    assert remaining == 42
    assert (code, scope) == (RATE_LIMITED, "platform")
    assert limiter.blocked("reddit", "anything")[1:] == (RATE_LIMITED, "platform")
    clock.advance(42)
    assert limiter.blocked("reddit") is None


def test_default_cooldowns_and_platform_blocks(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    assert limiter.note_error("a", "x", RATE_LIMITED) == DEFAULT_COOLDOWN
    assert limiter.note_error("b", "x", PLATFORM_BLOCKED) == BLOCKED_COOLDOWN
    assert limiter.blocked("a")[1] == RATE_LIMITED
    assert limiter.blocked("b")[1] == PLATFORM_BLOCKED


def test_retry_after_is_capped_and_non_numeric_values_fall_back(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    assert limiter.note_error("a", "x", RATE_LIMITED, retry_after=10**9) == MAX_COOLDOWN
    assert limiter.note_error("b", "x", RATE_LIMITED, retry_after="soon") == DEFAULT_COOLDOWN
    assert limiter.note_error("c", "x", RATE_LIMITED, retry_after=0) == 1.0


def test_backend_pause_only_affects_that_source(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    assert limiter.note_error("web", "jina", UPSTREAM_BROKEN) == BACKEND_PAUSES[UPSTREAM_BROKEN]
    assert limiter.blocked("web") is None
    assert limiter.blocked("web", "exa") is None
    remaining, code, scope = limiter.blocked("web", "jina")
    assert (remaining, code, scope) == (
        BACKEND_PAUSES[UPSTREAM_BROKEN],
        UPSTREAM_BROKEN,
        "source",
    )


@pytest.mark.parametrize("code", [TIMEOUT, UPSTREAM_BROKEN, NEED_SETUP])
def test_retryable_codes_pause_the_source(tmp_path, code):
    limiter, _, _ = _limiter(tmp_path)
    assert limiter.note_error("p", "s", code) == BACKEND_PAUSES[code]


@pytest.mark.parametrize("code", [NOT_FOUND, BAD_INPUT, "NEED_LOGIN", "BUSY"])
def test_non_transient_codes_do_not_pause(tmp_path, code):
    limiter, _, _ = _limiter(tmp_path)
    assert limiter.note_error("p", "s", code) is None
    assert limiter.blocked("p", "s") is None
    assert not limiter.path.exists()


def test_a_shorter_new_cooldown_never_shortens_the_old_one(tmp_path):
    limiter, clock, _ = _limiter(tmp_path)
    limiter.note_error("p", "s", RATE_LIMITED, retry_after=100)
    clock.advance(10)
    assert limiter.note_error("p", "s", RATE_LIMITED, retry_after=5) == 5
    assert limiter.blocked("p")[0] == 90


def test_success_clears_the_source_pause_but_not_the_platform_cooldown(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    limiter.note_error("p", "s", UPSTREAM_BROKEN)
    limiter.note_success("p", "s")
    assert limiter.blocked("p", "s") is None

    limiter.note_error("p", "s", RATE_LIMITED, retry_after=60)
    limiter.note_success("p", "s")
    assert limiter.blocked("p") is not None


def test_pacing_waits_only_the_remaining_interval(tmp_path):
    limiter, clock, sleeps = _limiter(tmp_path)
    assert limiter.begin("p", "s", min_interval=2) == 0
    clock.advance(0.5)
    assert limiter.begin("p", "s", min_interval=2) == 1.5
    assert sleeps == [1.5]
    clock.advance(2)
    assert limiter.begin("p", "s", min_interval=2) == 0
    assert sleeps == [1.5]


def test_pacing_is_capped_and_different_sources_are_independent(tmp_path):
    limiter, clock, sleeps = _limiter(tmp_path)
    assert limiter.begin("p", "a", min_interval=1000) == 0
    waited = limiter.begin("p", "a", min_interval=1000)
    assert waited == MAX_PACING
    assert sleeps == [MAX_PACING]
    clock.advance(1)
    assert limiter.begin("p", "b", min_interval=1000) == 0


def test_corrupt_state_fails_open_and_is_replaced(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    limiter.path.parent.mkdir(parents=True)
    limiter.path.write_text("{not json", encoding="utf-8")
    assert limiter.blocked("p", "s") is None
    limiter.note_error("p", "s", RATE_LIMITED, retry_after=5)
    assert limiter.blocked("p")[0] == 5
    payload = json.loads(limiter.path.read_text(encoding="utf-8"))
    assert payload["version"] == 1


def test_expired_entries_are_pruned_on_the_next_write(tmp_path):
    limiter, clock, _ = _limiter(tmp_path)
    limiter.note_error("old", "s", RATE_LIMITED, retry_after=10)
    clock.advance(20)
    limiter.note_error("new", "s", RATE_LIMITED, retry_after=10)
    payload = json.loads(limiter.path.read_text(encoding="utf-8"))
    assert set(payload["cooldowns"]) == {"platform:new"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_state_file_and_directory_are_private(tmp_path):
    limiter, _, _ = _limiter(tmp_path)
    limiter.note_error("p", "s", RATE_LIMITED)
    assert limiter.path.stat().st_mode & 0o777 == 0o600
    assert limiter.path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_symlinked_state_path_is_never_written(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("untouched", encoding="utf-8")
    link = tmp_path / "state.json"
    link.symlink_to(target)
    limiter = RateLimits(link)
    limiter.note_error("p", "s", RATE_LIMITED)
    assert target.read_text(encoding="utf-8") == "untouched"
    assert link.is_symlink()


def test_state_path_honours_reach_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("REACH_STATE_DIR", str(tmp_path / "custom"))
    assert state_dir() == tmp_path / "custom"
    assert default_state_path() == tmp_path / "custom" / "rate_limits.json"
