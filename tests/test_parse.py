from __future__ import annotations

import pytest

from agent_reach.adapters.base import RawResult
from agent_reach.parse.generic import ParseError, extract_items, loads_json, parse_with_chain
from agent_reach.parse.web import exa_read


def test_loads_json_from_noisy_stdout():
    assert loads_json('log line\n{"a": 1}\ntrailing') == {"a": 1}


def test_loads_json_from_fenced_block():
    text = 'here you go\n```json\n{"items": [1]}\n```\n'
    assert loads_json(text) == {"items": [1]}


def test_extract_items_from_nested_response():
    payload = {
        "data": {
            "results": [
                {
                    "title": "T",
                    "link": "https://example.com/a",
                    "snippet": "S",
                    "author": "A",
                    "published": "2024-01-01",
                }
            ]
        }
    }
    items = extract_items(payload)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "T"
    assert item["url"] == "https://example.com/a"
    assert item["text"] == "S"
    assert item["author"] == "A"
    assert item["published_at"] == "2024-01-01"


def test_parse_chain_prefers_json_then_normalizes():
    raw = RawResult(text='warning: noisy\n{"items": [{"title": "T", "url": "https://x.example"}]}')
    items = parse_with_chain(raw, ["json_items"])
    assert items[0]["title"] == "T"
    assert items[0]["url"] == "https://x.example"


def test_parse_chain_falls_back_to_yaml_items():
    raw = RawResult(text="ok: true\nitems:\n  - title: Y\n    link: https://y.example\n")
    items = parse_with_chain(raw, ["json_items", "yaml_items"])
    assert items[0]["title"] == "Y"


def test_opencli_error_envelope_is_a_parse_failure():
    raw = RawResult(text="ok: false\nerror:\n  message: not logged in\n")
    with pytest.raises(ParseError, match="OpenCLI error"):
        parse_with_chain(raw, ["opencli"])


def test_exa_read_parses_a_markdown_page_header():
    target = "https://www.youtube.com/watch?v=example1234"
    raw = RawResult(
        data={
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "# asyncio in Python - Full Tutorial\n"
                            "URL: https://www.youtube.com/watch?v=example1234\n"
                            "Published: 2024-01-15\n"
                            "Author: TechWithTim\n"
                            "\n"
                            "Event loops and coroutines."
                        ),
                    }
                ]
            }
        },
        meta={"params": {"target": target}},
    )
    items = exa_read(raw)
    assert items[0]["title"] == "asyncio in Python - Full Tutorial"
    assert items[0]["url"] == target
    assert items[0]["author"] == "TechWithTim"
    assert items[0]["published_at"] == "2024-01-15"
    assert items[0]["text"] == "Event loops and coroutines."


def test_parse_chain_reports_all_failures():
    with pytest.raises(ParseError, match="json.*yaml"):
        parse_with_chain(RawResult(text="a: [unclosed"), ["json", "yaml"])


def test_json_selector():
    raw = RawResult(data={"data": {"items": [{"title": "T"}]}})
    items = parse_with_chain(raw, ["json_items:data.items"])
    assert items[0]["title"] == "T"
