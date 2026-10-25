from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent_reach.adapters.base import RawResult
from agent_reach.core.result import NOT_FOUND, UPSTREAM_BROKEN, ReachError

PLATFORMS = Path(__file__).resolve().parents[1] / "agent_reach" / "platforms"


def _load(platform: str, filename: str):
    path = PLATFORMS / platform / filename
    spec = importlib.util.spec_from_file_location(
        f"test_{platform}_{path.stem}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(platform: str, name: str):
    path = PLATFORMS / platform / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_raw_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REACH_RAW_DIR", str(tmp_path / "_raw"))


class HttpStub:
    def __init__(self, routes, *, secrets=None):
        self.routes = list(routes)
        self.secrets = dict(secrets or {})
        self.calls: list[tuple[str, dict]] = []

    def secret(self, name):
        return self.secrets.get(name)

    def http(self, url, *, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        for needle, result in self.routes:
            if needle in url:
                return result
        raise ReachError(NOT_FOUND, f"unexpected URL: {url}")


def test_github_search_parser_keeps_limit_and_provenance_fields():
    parser = _load("github", "parse.py")
    raw = RawResult(data=_fixture("github", "search.json"), meta={"params": {"limit": 1}})
    items = parser.github_repos(raw)
    assert len(items) == 1
    assert items[0]["title"] == "pallets/flask"
    assert items[0]["url"] == "https://github.com/pallets/flask"
    assert items[0]["author"] == "pallets"
    assert items[0]["raw"]["stars"] == 69123
    assert items[0]["raw"]["topics"] == ["flask", "python", "web"]


def test_github_search_parser_reports_api_error_message():
    parser = _load("github", "parse.py")
    raw = RawResult(data={"message": "API rate limit exceeded"})
    with pytest.raises(ReachError) as info:
        parser.github_repos(raw)
    assert "rate limit" in info.value.message


def test_github_read_repository_includes_description_and_readme():
    module = _load("github", "read.py")
    readme = (PLATFORMS / "github" / "fixtures" / "readme.md").read_text(encoding="utf-8")
    ctx = HttpStub(
        [
            ("/readme", RawResult(text=readme, content_type="text/plain")),
            ("/repos/pallets/flask", RawResult(data=_fixture("github", "repository.json"))),
        ]
    )
    items = module.read("https://github.com/pallets/flask", ctx=ctx)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "pallets/flask"
    assert item["author"] == "pallets"
    assert "micro framework" in item["text"]
    assert "lightweight WSGI" in item["text"]
    assert item["raw"]["readme_chars"] > 100


def test_github_read_issue_appends_comments():
    module = _load("github", "read.py")
    ctx = HttpStub(
        [
            (
                "/issues/42/comments",
                RawResult(data=_fixture("github", "issue_comments.json")),
            ),
            ("/issues/42", RawResult(data=_fixture("github", "issue.json"))),
        ]
    )
    items = module.read("pallets/flask/issues/42", ctx=ctx)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Clarify application factory docs"
    assert "application factory section" in item["text"]
    assert "@maintainer" in item["text"]
    assert item["raw"]["kind"] == "issue"
    assert item["raw"]["labels"] == ["docs", "good first issue"]
    assert item["raw"]["comments"] == 1


def test_github_read_sends_token_but_never_returns_it():
    module = _load("github", "read.py")
    ctx = HttpStub(
        [
            ("/readme", RawResult(text="# Demo", content_type="text/plain")),
            ("/repos/o/r", RawResult(data={"full_name": "o/r", "html_url": "https://github.com/o/r"})),
        ],
        secrets={"GITHUB_TOKEN": "ghp_secret_value"},
    )
    items = module.read("o/r", ctx=ctx)
    assert ctx.calls[0][1]["Accept"] == "application/vnd.github+json"
    assert ctx.calls[0][1]["Authorization"] == "Bearer ghp_secret_value"
    assert "ghp_secret_value" not in json.dumps(items, ensure_ascii=False)


def test_github_read_ignores_unsupported_targets():
    module = _load("github", "read.py")
    assert module.read("https://gist.github.com/someone/abc", ctx=HttpStub([])) == []
    assert module.read("https://example.com/pallets/flask", ctx=HttpStub([])) == []


def test_v2ex_search_parser_prefers_highlight_and_builds_topic_url():
    parser = _load("v2ex", "parse.py")
    raw = RawResult(data=_fixture("v2ex", "search.json"), meta={"params": {"limit": 5}})
    items = parser.v2ex_search(raw)
    assert [item["title"] for item in items][0].startswith("[全职 / AI 公司]")
    assert items[0]["url"] == "https://www.v2ex.com/t/1251611"
    assert items[0]["author"] == "biteaglelabs"
    assert "Prompt 缓存" in items[0]["text"]
    assert "<em>" not in items[0]["text"]
    assert items[1]["text"].endswith("。")
    assert items[1]["raw"]["replies"] == 12


def test_v2ex_read_topic_includes_replies_and_metadata():
    module = _load("v2ex", "read.py")
    ctx = HttpStub(
        [
            ("/replies/show.json", RawResult(data=_fixture("v2ex", "replies.json"))),
            ("/topics/show.json", RawResult(data=_fixture("v2ex", "topic.json"))),
        ]
    )
    items = module.read_topic("https://www.v2ex.com/t/1", ctx=ctx)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Project Babel"
    assert item["author"] == "Livid"
    assert item["published_at"] == "2010-04-25T14:52:53Z"
    assert "first topic" in item["text"]
    assert "@Jay" in item["text"]
    assert "Still a useful community" in item["text"]
    assert item["raw"]["node"] == "babel"
    assert item["raw"]["replies_read"] == 2


def test_v2ex_read_accepts_numeric_id_and_rejects_other_targets():
    module = _load("v2ex", "read.py")
    ctx = HttpStub(
        [
            ("/replies/show.json", RawResult(data=[])),
            ("/topics/show.json?id=1", RawResult(data=_fixture("v2ex", "topic.json"))),
        ]
    )
    assert module.read_topic("1", ctx=ctx)[0]["title"] == "Project Babel"
    assert module.read_topic("https://example.com/t/1", ctx=HttpStub([])) == []


def test_v2ex_missing_topic_is_not_found():
    module = _load("v2ex", "read.py")
    with pytest.raises(ReachError) as info:
        module.read_topic("999999", ctx=HttpStub([("/topics/show.json", RawResult(data=[]))]))
    assert info.value.code == NOT_FOUND


def _bilibili_raw(**overrides):
    payload = {
        "title": "费曼的 47 本笔记",
        "owner": "示例UP主",
        "pubdate": 1700000000,
        "duration": 367,
        "bvid": "BV1S1dPBYEG9",
        "cid": 38164562135,
        "track_count": 1,
        "subtitles": [
            {
                "lan": "ai-zh",
                "lan_doc": "中文",
                "url": "//aisubtitle.hdslb.com/bfs/ai_subtitle/demo.json",
            }
        ],
    }
    payload.update(overrides)
    return RawResult(
        data=payload,
        meta={"page_url": "https://www.bilibili.com/video/BV1S1dPBYEG9/"},
    )


def _cues(*contents):
    return RawResult(
        data={
            "body": [
                {"from": float(index), "to": float(index + 1), "content": content}
                for index, content in enumerate(contents)
            ]
        }
    )


def test_bilibili_subtitles_downloads_and_formats_cues():
    parser = _load("bilibili", "parse.py")
    ctx = HttpStub([("aisubtitle.hdslb.com", _cues("第一句", "第二句"))])

    items = parser.subtitles(_bilibili_raw(), ctx=ctx)

    assert len(items) == 1
    item = items[0]
    assert item["title"] == "费曼的 47 本笔记"
    assert item["url"] == "https://www.bilibili.com/video/BV1S1dPBYEG9/"
    assert item["author"] == "示例UP主"
    assert item["published_at"] == "2023-11-14T22:13:20Z"
    assert item["text"] == "第一句\n第二句"
    assert item["raw"]["lan"] == "ai-zh"
    assert item["raw"]["cues"] == 2
    assert ctx.calls[0][0].startswith("https://aisubtitle.hdslb.com/")
    assert (
        ctx.calls[0][1]["Referer"]
        == "https://www.bilibili.com/video/BV1S1dPBYEG9/"
    )
    saved = item["raw"]["saved_files"]
    assert len(saved) == 2
    assert saved[0].endswith(".txt") and saved[1].endswith(".srt")
    assert Path(saved[0]).read_text(encoding="utf-8").strip() == "第一句\n第二句"
    assert "00:00:00,000 --> 00:00:01,000" in Path(saved[1]).read_text(
        encoding="utf-8"
    )


def test_bilibili_subtitles_label_multiple_languages():
    parser = _load("bilibili", "parse.py")
    ctx = HttpStub([("aisubtitle.hdslb.com", _cues("hello"))])
    raw = _bilibili_raw(
        track_count=2,
        subtitles=[
            {"lan": "ai-zh", "lan_doc": "中文", "url": "//aisubtitle.hdslb.com/zh.json"},
            {"lan": "en-US", "lan_doc": "English", "url": "//aisubtitle.hdslb.com/en.json"},
        ],
    )

    items = parser.subtitles(raw, ctx=ctx)

    assert [item["title"] for item in items] == [
        "费曼的 47 本笔记（中文）",
        "费曼的 47 本笔记（English）",
    ]


def test_bilibili_subtitles_ignores_tracks_that_do_not_cover_the_video():
    parser = _load("bilibili", "parse.py")
    ctx = HttpStub(
        [
            (
                "/short.json",
                RawResult(data={"body": [{"from": 0, "to": 12, "content": "别的视频"}]}),
            ),
            (
                "/full.json",
                RawResult(
                    data={
                        "body": [
                            {"from": 0, "to": 1132, "content": "1939年"},
                            {"from": 1132, "to": 1133, "content": "结尾"},
                        ]
                    }
                ),
            ),
            (
                "/long.json",
                RawResult(
                    data={"body": [{"from": 0, "to": 3275, "content": "无关英文"}]}
                ),
            ),
        ]
    )
    raw = _bilibili_raw(
        duration=1133,
        track_count=3,
        subtitles=[
            {"lan": "ai-zh", "lan_doc": "中文", "url": "//aisubtitle.hdslb.com/short.json"},
            {"lan": "ai-zh", "lan_doc": "中文", "url": "//aisubtitle.hdslb.com/full.json"},
            {"lan": "ai-zh", "lan_doc": "中文", "url": "//aisubtitle.hdslb.com/long.json"},
        ],
    )

    items = parser.subtitles(raw, ctx=ctx)

    assert len(items) == 1
    assert items[0]["text"] == "1939年\n结尾"
    assert items[0]["raw"]["coverage"] == 1.0


def test_bilibili_subtitles_prefers_a_manual_track_over_the_ai_track():
    parser = _load("bilibili", "parse.py")
    ctx = HttpStub(
        [
            (
                "/ai.json",
                RawResult(data={"body": [{"from": 0, "to": 100, "content": "AI"}]}),
            ),
            (
                "/manual.json",
                RawResult(data={"body": [{"from": 0, "to": 100, "content": "人工"}]}),
            ),
        ]
    )
    raw = _bilibili_raw(
        duration=100,
        track_count=2,
        subtitles=[
            {"lan": "ai-zh", "lan_doc": "中文", "url": "//aisubtitle.hdslb.com/ai.json"},
            {"lan": "zh-CN", "lan_doc": "中文（中国）", "url": "//aisubtitle.hdslb.com/manual.json"},
        ],
    )

    items = parser.subtitles(raw, ctx=ctx)

    assert len(items) == 1
    assert items[0]["raw"]["lan"] == "zh-CN"
    assert items[0]["text"] == "人工"


def test_bilibili_subtitles_reports_missing_or_unissued_tracks():
    parser = _load("bilibili", "parse.py")

    with pytest.raises(ReachError) as info:
        parser.subtitles(_bilibili_raw(track_count=0, subtitles=[]), ctx=HttpStub([]))
    assert info.value.code == NOT_FOUND

    with pytest.raises(ReachError) as info:
        parser.subtitles(_bilibili_raw(track_count=1, subtitles=[]), ctx=HttpStub([]))
    assert info.value.code == UPSTREAM_BROKEN


def test_bilibili_subtitles_surfaces_browser_errors():
    parser = _load("bilibili", "parse.py")

    with pytest.raises(ReachError) as info:
        parser.subtitles(
            RawResult(data={"error": "Bilibili view API -400: bad bvid"}),
            ctx=HttpStub([]),
        )
    assert info.value.code == UPSTREAM_BROKEN
    assert "Bilibili view API -400" in info.value.message
