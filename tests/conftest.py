from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep discovery and routing away from the developer's real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("REACH_PLUGIN_PATH", raising=False)
    monkeypatch.delenv("REACH_ROUTING", raising=False)
    monkeypatch.delenv("REACH_TRUSTED_PLUGINS", raising=False)
    monkeypatch.delenv("REACH_STATE_DIR", raising=False)
    # Keep health probes away from a daemon the developer may have running.
    monkeypatch.setenv("KIMI_WEBBRIDGE_ADDR", "127.0.0.1:1")
    return home


def _default_actions():
    return {
        "search": {
            "params": {"query": {"type": "str", "required": True}},
            "sources": [
                {"adapter": "builtin", "call": "search", "parse": "json", "priority": 10}
            ],
        }
    }


@pytest.fixture
def make_platform(tmp_path):
    """Write a minimal valid platform directory and return it."""

    def _make(
        root,
        platform_id: str,
        *,
        priority: int = 100,
        domains=None,
        actions=None,
        requires=None,
        auth=None,
        level: str = "default",
        description: str | None = None,
    ) -> Path:
        directory = Path(root) / platform_id
        directory.mkdir(parents=True)
        data = {
            "id": platform_id,
            "description": description or f"{platform_id} test platform",
            "level": level,
            "priority": priority,
            "domains": list(domains or []),
            "actions": actions or _default_actions(),
        }
        if requires:
            data["requires"] = requires
        if auth:
            data["auth"] = auth
        (directory / "platform.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        return directory

    return _make
