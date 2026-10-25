from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

import agent_reach.cli as cli
import agent_reach.core.pipeline as pipeline
import agent_reach.transcribe as tr
from agent_reach.core.config import DEFAULT_ROUTING
from agent_reach.core.pipeline import run_action
from agent_reach.core.registry import discover
from agent_reach.core.result import (
    BAD_INPUT,
    NEED_SETUP,
    NOT_FOUND,
    RATE_LIMITED,
    TIMEOUT,
    UPSTREAM_BROKEN,
    ReachError,
    envelope,
    make_item,
)

EPISODE_URL = "https://www.xiaoyuzhoufm.com/episode/6adcced4c96c8942ec11a649"
AUDIO_URL = "https://media.xyzcdn.net/6974dcfecc1b94e8e864d34c/lpmN7sFd0gjjgTA_Qi5VyszEu0fh.m4a"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str = "",
        headers: dict[str, str] | None = None,
        blocks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = dict(headers or {})
        self.ok = status_code < 400
        self.content = text.encode("utf-8")
        self._blocks = list(blocks or [])
        self.closed = False

    def iter_content(self, chunk_size: int = 1024):  # noqa: ARG002 - requests API
        yield from self._blocks

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def json(self) -> Any:
        return json.loads(self.text)


class FakeCtx:
    timeout = 30
    user_agent = "reach-tests/1.0"

    def __init__(
        self,
        *,
        secrets: dict[str, str] | None = None,
        http_text: str = "",
        run=None,
    ) -> None:
        self.secrets = dict(secrets or {})
        self.http_text = http_text
        self.run_impl = run
        self.http_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    def http(self, url: str, **kwargs: Any) -> Any:
        self.http_calls.append(url)
        return type("Raw", (), {"text": self.http_text, "data": None})()

    def run(self, argv, **kwargs: Any) -> Any:
        self.run_calls.append(list(argv))
        if self.run_impl is None:  # pragma: no cover - test bug
            raise AssertionError("unexpected subprocess")
        return self.run_impl(list(argv), kwargs)


@pytest.fixture(autouse=True)
def ffmpeg_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tr.shutil, "which", lambda name: f"/usr/bin/{name}")


def _work_files(
    monkeypatch: pytest.MonkeyPatch,
    texts: tuple[str, ...] = ("第一段", "第二段"),
    *,
    chunked: bool = True,
) -> list[dict[str, Any]]:
    """Patch the media pipeline and return the recorded provider calls."""

    def fake_download(url: str, directory: Path, **kwargs: Any) -> Path:
        path = Path(directory) / "source.m4a"
        path.write_bytes(b"downloaded audio")
        return path

    def fake_probe(ctx: Any, path: Path) -> float:
        return 120.0

    def fake_compress(ctx: Any, source: Path, directory: Path) -> Path:
        path = Path(directory) / "compressed.m4a"
        path.write_bytes(b"x" * (10 if chunked else 4))
        return path

    calls: list[dict[str, Any]] = []

    def fake_chunk(ctx: Any, source: Path, directory: Path) -> list[Path]:
        chunks = []
        for index in range(len(texts)):
            path = Path(directory) / f"chunk_{index:03d}.m4a"
            path.write_bytes(b"c")
            chunks.append(path)
        return chunks

    def fake_transcribe(chunk: Path, **kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return texts[len(calls) - 1]

    monkeypatch.setattr(tr, "download_audio", fake_download)
    monkeypatch.setattr(tr, "probe_duration", fake_probe)
    monkeypatch.setattr(tr, "compress_audio", fake_compress)
    monkeypatch.setattr(tr, "chunk_audio", fake_chunk)
    monkeypatch.setattr(tr, "transcribe_chunk", fake_transcribe)
    if chunked:
        # The compressed file (10 bytes) is above the threshold, and each
        # chunk (1 byte) stays below the per-chunk upload limit.
        monkeypatch.setattr(tr, "SIZE_LIMIT_BYTES", 8)
    return calls


# ---------------------------------------------------------------------------
# duration and provider selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Duration: 00:18:13.00, start: 0.000000", 1093.0),
        ("Duration: 01:02:03.50", 3723.5),
        ("no duration here", None),
        ("Duration: 00:00:00.00", None),
        ("Duration: 00:00:01.00", 1.0),
    ],
)
def test_parse_duration(text: str, expected: float | None) -> None:
    assert tr.parse_duration(text) == expected


def test_resolve_provider_prefers_groq_then_openai() -> None:
    both = FakeCtx(secrets={"GROQ_API_KEY": "g", "OPENAI_API_KEY": "o"})
    assert tr.resolve_provider("auto", both) == ("groq", "whisper-large-v3", "g")

    openai_only = FakeCtx(secrets={"OPENAI_API_KEY": "o"})
    assert tr.resolve_provider("auto", openai_only) == ("openai", "whisper-1", "o")

    assert tr.resolve_provider("openai", both, model="custom") == (
        "openai",
        "custom",
        "o",
    )


def test_resolve_provider_requires_a_key() -> None:
    with pytest.raises(ReachError) as excinfo:
        tr.resolve_provider("auto", FakeCtx())
    assert excinfo.value.code == NEED_SETUP
    assert "GROQ_API_KEY" in excinfo.value.message
    assert "OPENAI_API_KEY" in excinfo.value.message


def test_resolve_provider_rejects_unknown_name() -> None:
    with pytest.raises(ReachError) as excinfo:
        tr.resolve_provider("azure", FakeCtx(secrets={"GROQ_API_KEY": "g"}))
    assert excinfo.value.code == BAD_INPUT


# ---------------------------------------------------------------------------
# provider requests
# ---------------------------------------------------------------------------


def _chunk(tmp_path: Path) -> Path:
    path = tmp_path / "chunk_000.m4a"
    path.write_bytes(b"audio")
    return path


def test_transcribe_chunk_posts_expected_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        kwargs["files"]["file"][1].read()
        return FakeResponse(200, text="转写结果")

    monkeypatch.setattr(tr.requests, "post", fake_post)
    text = tr.transcribe_chunk(
        _chunk(tmp_path),
        provider="groq",
        key="secret-key",
        model="whisper-large-v3",
        language="zh",
        prompt="中文提示",
    )

    assert text == "转写结果"
    assert captured["url"].startswith("https://api.groq.com/")
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["data"]["model"] == "whisper-large-v3"
    assert captured["data"]["language"] == "zh"
    assert captured["data"]["prompt"] == "中文提示"
    assert captured["data"]["response_format"] == "text"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse(429, headers={"Retry-After": "12.5"}), RATE_LIMITED),
        (FakeResponse(401, text="bad key"), NEED_SETUP),
        (FakeResponse(500, text="boom"), UPSTREAM_BROKEN),
    ],
)
def test_transcribe_chunk_maps_http_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse,
    code: str,
) -> None:
    monkeypatch.setattr(tr.requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(ReachError) as excinfo:
        tr.transcribe_chunk(
            _chunk(tmp_path), provider="groq", key="k", model="m"
        )
    assert excinfo.value.code == code
    if code == RATE_LIMITED:
        assert excinfo.value.retry_after_seconds == 12.5


@pytest.mark.parametrize(
    ("error", "code"),
    [(requests.Timeout(), TIMEOUT), (requests.ConnectionError("down"), UPSTREAM_BROKEN)],
)
def test_transcribe_chunk_maps_network_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(tr.requests, "post", fail)
    with pytest.raises(ReachError) as excinfo:
        tr.transcribe_chunk(_chunk(tmp_path), provider="groq", key="k", model="m")
    assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_audio_streams_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "ensure_public_resolution", lambda url: [url])
    response = FakeResponse(200, blocks=[b"abc", b"def"])
    monkeypatch.setattr(tr.requests, "get", lambda *args, **kwargs: response)

    path = tr.download_audio(AUDIO_URL, tmp_path)

    assert path.name == "source.m4a"
    assert path.read_bytes() == b"abcdef"
    assert response.closed is True


def test_download_audio_rejects_oversized_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "ensure_public_resolution", lambda url: [url])
    monkeypatch.setattr(tr, "MAX_SOURCE_BYTES", 4)
    response = FakeResponse(200, blocks=[b"123456"])
    monkeypatch.setattr(tr.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises(ReachError) as excinfo:
        tr.download_audio(AUDIO_URL, tmp_path)
    assert excinfo.value.code == BAD_INPUT


def test_download_audio_maps_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "ensure_public_resolution", lambda url: [url])
    monkeypatch.setattr(
        tr.requests, "get", lambda *args, **kwargs: FakeResponse(404)
    )

    with pytest.raises(ReachError) as excinfo:
        tr.download_audio(AUDIO_URL, tmp_path)
    assert excinfo.value.code == NOT_FOUND


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def test_transcribe_audio_joins_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _work_files(monkeypatch)
    result = tr.transcribe_audio(
        AUDIO_URL, ctx=FakeCtx(secrets={"GROQ_API_KEY": "g"}), language="zh"
    )

    assert result["text"] == "第一段\n第二段"
    assert result["provider"] == "groq"
    assert result["model"] == "whisper-large-v3"
    assert result["chunks"] == 2
    assert result["duration_seconds"] == 120.0
    assert result["polished"] is False
    assert len(calls) == 2
    assert calls[0]["language"] == "zh"
    assert "中文" in calls[0]["prompt"]


def test_transcribe_audio_skips_chunking_for_small_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_files(monkeypatch, texts=("一段",), chunked=False)
    result = tr.transcribe_audio(
        AUDIO_URL, ctx=FakeCtx(secrets={"OPENAI_API_KEY": "o"}), language="en"
    )

    assert result["text"] == "一段"
    assert result["chunks"] == 1
    assert result["provider"] == "openai"


def test_transcribe_audio_polishes_with_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_files(monkeypatch, texts=("没有标点",), chunked=False)
    monkeypatch.setattr(
        tr, "_polish_once", lambda text, key: (text + "。", "stop")
    )
    result = tr.transcribe_audio(
        AUDIO_URL,
        ctx=FakeCtx(secrets={"GROQ_API_KEY": "g"}),
        polish=True,
    )

    assert result["text"] == "没有标点。"
    assert result["polished"] is True
    assert result["polish_error"] is None


def test_transcribe_audio_keeps_raw_text_when_polish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_files(monkeypatch, texts=("没有标点",), chunked=False)

    def fail(text: str, key: str) -> Any:
        raise ReachError(UPSTREAM_BROKEN, "polish exploded")

    monkeypatch.setattr(tr, "_polish_once", fail)
    result = tr.transcribe_audio(
        AUDIO_URL,
        ctx=FakeCtx(secrets={"GROQ_API_KEY": "g"}),
        polish=True,
    )

    assert result["text"] == "没有标点"
    assert result["polished"] is False
    assert "polish exploded" in result["polish_error"]


def test_transcribe_audio_requires_groq_key_for_polish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_files(monkeypatch)
    with pytest.raises(ReachError) as excinfo:
        tr.transcribe_audio(
            AUDIO_URL,
            ctx=FakeCtx(secrets={"OPENAI_API_KEY": "o"}),
            polish=True,
        )
    assert excinfo.value.code == NEED_SETUP
    assert "GROQ_API_KEY" in excinfo.value.message


def test_transcribe_audio_rejects_empty_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_files(monkeypatch, texts=("",), chunked=False)
    with pytest.raises(ReachError) as excinfo:
        tr.transcribe_audio(
            AUDIO_URL, ctx=FakeCtx(secrets={"GROQ_API_KEY": "g"})
        )
    assert excinfo.value.code == UPSTREAM_BROKEN


# ---------------------------------------------------------------------------
# xiaoyuzhou episode extraction
# ---------------------------------------------------------------------------


def _episode_html(episode: dict[str, Any]) -> str:
    payload = {"props": {"pageProps": {"episode": episode}}}
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def _episode(**overrides: Any) -> dict[str, Any]:
    data = {
        "eid": "6adcced4c96c8942ec11a649",
        "title": "西安赏秋",
        "duration": 1093,
        "pubDate": "2026-10-24T23:02:00.000Z",
        "enclosure": {"url": AUDIO_URL},
        "podcast": {"title": "科技早8点"},
        "payType": "FREE",
        "isPrivateMedia": False,
    }
    data.update(overrides)
    return data


def _platform_module(name: str) -> Any:
    registry = discover(plugin_dirs=[], include_builtin=True)
    platform = registry.get("xiaoyuzhou")
    assert platform is not None
    module = pipeline.load_platform_module(platform, name)
    assert module is not None
    return module


def test_extract_episode_from_next_data() -> None:
    module = _platform_module("transcribe.py")
    episode = module.extract_episode(_episode_html(_episode()))

    assert episode["audio_url"] == AUDIO_URL
    assert episode["title"] == "西安赏秋"
    assert episode["podcast"] == "科技早8点"
    assert episode["duration_seconds"] == 1093
    assert episode["published_at"] == "2026-10-24T23:02:00.000Z"


def test_extract_episode_falls_back_to_regex() -> None:
    module = _platform_module("transcribe.py")
    html = (
        '<meta property="og:title" content="标题">'
        '"duration":300, "enclosure":{"url":"https:\\/\\/media.xyzcdn.net\\/a.m4a"}'
    )
    episode = module.extract_episode(html)

    assert episode["audio_url"] == "https://media.xyzcdn.net/a.m4a"
    assert episode["title"] == "标题"
    assert episode["duration_seconds"] == 300


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"isPrivateMedia": True}, BAD_INPUT),
        ({"payType": "PAID"}, BAD_INPUT),
        ({"enclosure": {}, "media": {"source": {"mode": "PRIVATE", "url": ""}}}, BAD_INPUT),
        ({"enclosure": {}}, UPSTREAM_BROKEN),
    ],
)
def test_extract_episode_rejects_unplayable_audio(
    overrides: dict[str, Any], code: str
) -> None:
    module = _platform_module("transcribe.py")
    with pytest.raises(ReachError) as excinfo:
        module.extract_episode(_episode_html(_episode(**overrides)))
    assert excinfo.value.code == code


def test_extract_episode_requires_an_audio_url_in_html() -> None:
    module = _platform_module("transcribe.py")
    with pytest.raises(ReachError) as excinfo:
        module.extract_episode("<html>nothing here</html>")
    assert excinfo.value.code == UPSTREAM_BROKEN


# ---------------------------------------------------------------------------
# platform hook
# ---------------------------------------------------------------------------


def _transcript_result(text: str = "正文") -> dict[str, Any]:
    return {
        "text": text,
        "provider": "groq",
        "model": "whisper-large-v3",
        "language": "zh",
        "duration_seconds": 1093.0,
        "chunks": 2,
        "polished": False,
        "polish_error": None,
    }


def test_hook_reads_episode_page_and_returns_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _platform_module("transcribe.py")
    ctx = FakeCtx(http_text=_episode_html(_episode()))
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_transcribe(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((url, kwargs))
        return _transcript_result()

    monkeypatch.setattr(module, "transcribe_audio", fake_transcribe)
    items = module.transcribe(
        params={"target": EPISODE_URL, "provider": "groq", "language": "zh"},
        ctx=ctx,
    )

    assert ctx.http_calls == [EPISODE_URL]
    assert calls[0][0] == AUDIO_URL
    assert calls[0][1]["provider"] == "groq"
    item = items[0]
    assert item["title"] == "西安赏秋"
    assert item["url"] == EPISODE_URL
    assert item["author"] == "科技早8点"
    assert item["text"] == "正文"
    assert item["raw"]["episode_id"] == "6adcced4c96c8942ec11a649"
    assert item["raw"]["chunks"] == 2


def test_hook_accepts_direct_audio_url(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _platform_module("transcribe.py")
    ctx = FakeCtx()
    seen: list[str] = []

    def fake_transcribe(url: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(url)
        return _transcript_result()

    monkeypatch.setattr(module, "transcribe_audio", fake_transcribe)
    items = module.transcribe(params={"target": AUDIO_URL}, ctx=ctx)

    assert ctx.http_calls == []
    assert seen == [AUDIO_URL]
    assert items[0]["title"] is None
    assert items[0]["raw"]["podcast"] is None


def test_hook_rejects_missing_target() -> None:
    module = _platform_module("transcribe.py")
    with pytest.raises(ReachError) as excinfo:
        module.transcribe(params={}, ctx=FakeCtx())
    assert excinfo.value.code == BAD_INPUT


# ---------------------------------------------------------------------------
# pipeline and CLI wiring
# ---------------------------------------------------------------------------


def test_builtin_adapter_dispatches_transcribe_action(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    directory = root / "audiofix"
    directory.mkdir(parents=True)
    (directory / "platform.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "audiofix",
                "description": "audio fixture",
                "capabilities": [],
                "actions": {
                    "transcribe": {
                        "params": {"target": {"type": "str", "required": True}},
                        "sources": [
                            {"adapter": "builtin", "call": "transcribe", "priority": 10}
                        ],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (directory / "transcribe.py").write_text(
        "def transcribe(*, params, ctx):\n"
        "    return [{'title': 't', 'url': params['target'], 'text': 'body'}]\n",
        encoding="utf-8",
    )

    registry = discover(
        plugin_dirs=[root], include_builtin=False, trusted_plugins=["audiofix"]
    )
    payload = run_action(
        registry,
        DEFAULT_ROUTING,
        action="transcribe",
        params={"target": "https://example.com/a"},
        ctx=FakeCtx(),
        platform="audiofix",
    )

    assert payload["ok"] is True
    assert payload["action"] == "transcribe"
    assert payload["items"][0]["text"] == "body"


def test_cli_transcribe_forwards_options_and_prints_full_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}
    long_text = "转写正文" * 200

    def fake_run_action(registry: Any, routing: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return envelope(
            channel="xiaoyuzhou",
            action="transcribe",
            items=[
                make_item(title="标题", url=kwargs["params"]["target"], text=long_text)
            ],
            elapsed_ms=1,
        )

    monkeypatch.setattr(cli, "run_action", fake_run_action)
    code = cli.main(
        [
            "transcribe",
            EPISODE_URL,
            "--provider",
            "groq",
            "--language",
            "en",
            "--polish",
            "--text",
        ]
    )

    assert code == 0
    assert captured["action"] == "transcribe"
    assert captured["url"] == EPISODE_URL
    assert captured["params"] == {
        "target": EPISODE_URL,
        "provider": "groq",
        "language": "en",
        "polish": True,
    }
    assert long_text in capsys.readouterr().out
