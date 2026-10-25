from __future__ import annotations

import shutil

import yaml

from agent_reach.core.registry import discover


def test_discovers_builtin_noop():
    registry = discover(plugin_dirs=[])
    assert "noop" in registry.platforms
    assert registry.errors == []


def test_discovers_plugin(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "alpha", priority=10)
    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert set(registry.platforms) == {"alpha"}
    assert registry.errors == []


def test_project_root_overrides_builtin(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "noop", description="shadowed")
    registry = discover(plugin_dirs=[root])
    assert registry.platforms["noop"].description == "shadowed"
    assert registry.platforms["noop"].origin == "plugin"


def test_invalid_plugin_is_reported_and_skipped(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "alpha")
    broken = root / "broken"
    broken.mkdir()
    (broken / "platform.yaml").write_text("id: [unclosed", encoding="utf-8")

    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert set(registry.platforms) == {"alpha"}
    assert len(registry.errors) == 1
    assert "broken" in str(registry.errors[0].path)


def test_missing_platform_dependency_is_removed(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "needs_missing", requires={"platforms": ["missing"]})
    make_platform(root, "standalone")

    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert set(registry.platforms) == {"standalone"}
    assert any("missing" in error.message for error in registry.errors)


def test_directory_name_must_match_id(tmp_path):
    root = tmp_path / "plugins"
    wrong = root / "wrong"
    wrong.mkdir(parents=True)
    (wrong / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "right",
                "description": "misnamed",
                "actions": {
                    "search": {
                        "sources": [{"adapter": "builtin", "call": "search"}]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    registry = discover(plugin_dirs=[root], include_builtin=False)
    assert registry.platforms == {}
    assert any("must equal platform id" in error.message for error in registry.errors)


def test_for_url_matches_real_subdomains_only(make_platform, tmp_path):
    root = tmp_path / "plugins"
    make_platform(root, "video", priority=20, domains=["bilibili.com", "b23.tv"])
    make_platform(root, "other", priority=5, domains=["example.com"])
    registry = discover(plugin_dirs=[root], include_builtin=False)

    matches = registry.for_url("https://www.bilibili.com/video/1")
    assert [platform.id for platform in matches] == ["video"]

    assert registry.for_url("https://bilibili.com.evil.test/video/1") == []
    assert registry.for_url("https://user:pass@bilibili.com/video/1") == []


def test_deleting_directory_removes_platform(make_platform, tmp_path):
    root = tmp_path / "plugins"
    directory = make_platform(root, "alpha")
    before = discover(plugin_dirs=[root], include_builtin=False)
    assert "alpha" in before.platforms

    shutil.rmtree(directory)
    after = discover(plugin_dirs=[root], include_builtin=False)
    assert "alpha" not in after.platforms


def test_catalog_hides_source_args(make_platform, tmp_path):
    root = tmp_path / "plugins"
    directory = make_platform(root, "alpha")
    manifest = yaml.safe_load((directory / "platform.yaml").read_text(encoding="utf-8"))
    manifest["actions"]["search"]["sources"][0]["args"] = {"token": "secret"}
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    registry = discover(plugin_dirs=[root], include_builtin=False)
    entry = registry.catalog()[0]
    source = entry["actions"]["search"]["sources"][0]
    assert "args" not in source
    assert "secret" not in repr(entry)
