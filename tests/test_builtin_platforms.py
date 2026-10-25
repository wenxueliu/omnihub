from __future__ import annotations

from agent_reach.adapters.base import ADAPTER_KINDS
from agent_reach.core.registry import BUILTIN_PLATFORM_DIR, discover


def _builtin_dirs():
    return sorted(
        path.name
        for path in BUILTIN_PLATFORM_DIR.iterdir()
        if path.is_dir() and (path / "platform.yaml").is_file()
    )


def test_all_builtin_manifests_validate():
    registry = discover(plugin_dirs=[])
    assert registry.errors == []
    assert set(_builtin_dirs()) <= set(registry.platforms)
    for platform in registry.platforms.values():
        assert platform.origin == "builtin"
        assert platform.actions
        for name, action in platform.actions.items():
            assert action.sources, f"{platform.id}.{name} has no sources"
            for source in action.sources:
                assert source.adapter in ADAPTER_KINDS
                assert not source.when or source.when.split(":", 1)[0] in {
                    "secret",
                    "tool",
                }
