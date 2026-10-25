from __future__ import annotations

import json

import yaml

from agent_reach.cli import main


def _payload(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out), captured


def test_platforms_lists_builtin_noop(capsys):
    assert main(["platforms"]) == 0
    payload, _ = _payload(capsys)
    assert payload["ok"] is True
    assert payload["error"] is None
    ids = [platform["id"] for platform in payload["platforms"]]
    assert "noop" in ids
    noop = payload["platforms"][ids.index("noop")]
    assert set(noop["actions"]) == {"search", "read"}


def test_platforms_text_mode(capsys):
    assert main(["platforms", "--text"]) == 0
    captured = capsys.readouterr()
    assert "noop" in captured.out


def test_platforms_can_run_plugin_only(make_platform, tmp_path, capsys):
    root = tmp_path / "plugins"
    make_platform(root, "alpha")

    assert main(["platforms", "--plugin-dir", str(root), "--no-builtin"]) == 0
    payload, _ = _payload(capsys)
    assert [platform["id"] for platform in payload["platforms"]] == ["alpha"]


def test_invalid_manifest_makes_platforms_fail(tmp_path, capsys):
    root = tmp_path / "plugins"
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "platform.yaml").write_text("id: [unclosed", encoding="utf-8")

    assert main(["platforms", "--plugin-dir", str(root), "--no-builtin"]) == 2
    payload, _ = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BAD_INPUT"
    assert len(payload["errors"]) == 1


def test_explain_explicit_platform(capsys):
    assert main(["explain", "--platform", "noop", "--action", "search"]) == 0
    payload, _ = _payload(capsys)
    assert payload["ok"] is True
    assert payload["plan"]["platforms"] == ["noop"]
    assert payload["plan"]["reason"] == "explicit platform"


def test_explain_url_domain(capsys):
    assert (
        main(["explain", "--url", "https://www.example.com/a", "--action", "read"])
        == 0
    )
    payload, _ = _payload(capsys)
    # The public-web platform is appended as the last-resort read fallback.
    assert payload["plan"]["platforms"] == ["noop", "web"]
    assert "url domain" in payload["plan"]["reason"]


def test_explain_intent_from_routing_file(tmp_path, capsys):
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(
        yaml.safe_dump({"version": 1, "intents": {"code": ["noop"]}}),
        encoding="utf-8",
    )
    assert main(["explain", "--intent", "code", "--routing", str(routing_path)]) == 0
    payload, _ = _payload(capsys)
    assert payload["plan"]["platforms"] == ["noop"]
    assert payload["plan"]["intent"] == "code"
    assert "intent" in payload["plan"]["reason"]


def test_explain_unknown_platform_is_bad_input(capsys):
    assert main(["explain", "--platform", "nope"]) == 2
    payload, _ = _payload(capsys)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BAD_INPUT"


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "platforms" in captured.out
