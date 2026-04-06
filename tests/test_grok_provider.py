from datetime import datetime, timedelta, timezone

import pytest

from grok_search.providers.grok import GrokSearchProvider, _WaitWithRetryAfter


class DummyResponse:
    def __init__(self, text="", json_data=None, json_error=None, headers=None):
        self.text = text
        self._json_data = json_data
        self._json_error = json_error
        self.headers = headers or {}

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


@pytest.mark.asyncio
async def test_search_uses_non_stream_completion_and_user_agent(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}

    async def fake_execute(headers, payload, ctx):
        captured["headers"] = headers
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.search("What is Scrape.do?")

    assert result == "ok"
    assert captured["headers"]["User-Agent"] == "grok-search-mcp/0.1.0"
    assert captured["headers"]["Accept"] == "application/json, text/event-stream"
    assert captured["payload"]["stream"] is False
    assert "[Current Time Context]" in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_search_auto_mode_skips_time_context_for_static_query(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    await provider.search("Explain FastAPI dependency injection")

    assert "[Current Time Context]" not in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_search_auto_mode_injects_time_context_for_temporal_query(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    await provider.search("What changed this week in FastAPI?")

    assert "[Current Time Context]" in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_search_auto_mode_does_not_match_partial_english_tokens(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    await provider.search("Explain currentColor in CSS and Firebase Realtime Database")

    assert "[Current Time Context]" not in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_search_auto_mode_injects_time_context_when_runtime_hint_is_set(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    provider.time_context_required = True
    captured = {}
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    await provider.search("OpenAI release notes")

    assert "[Current Time Context]" in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_search_never_mode_skips_time_context_even_for_temporal_query(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "never")

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    await provider.search("What changed this week in FastAPI?")

    assert "[Current Time Context]" not in captured["payload"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_parse_completion_response_reads_json_message():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world"}}]}',
        json_data={"choices": [{"message": {"content": "hello world"}}]},
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_reads_choice_text_variant():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"text":"hello world"}]}',
        json_data={"choices": [{"text": "hello world"}]},
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_reads_top_level_output_text():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"output_text":"hello world"}',
        json_data={"output_text": "hello world"},
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_reads_message_content_blocks():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"text","text":"hello"},{"type":"output_text","text":" world"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "output_text", "text": " world"},
                        ]
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_skips_reasoning_blocks():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"reasoning","text":"hidden"},{"type":"output_text","text":"visible"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "reasoning", "text": "hidden"},
                            {"type": "output_text", "text": "visible"},
                        ]
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result == "visible"


@pytest.mark.asyncio
async def test_parse_completion_response_appends_sources_from_nested_content_blocks():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"output_text","text":"hello world","annotations":[{"title":"Nested Docs","url":"https://docs.example.com/nested"}]}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "hello world",
                                "annotations": [
                                    {
                                        "title": "Nested Docs",
                                        "url": "https://docs.example.com/nested",
                                    }
                                ],
                            }
                        ]
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "## Sources" in result
    assert "https://docs.example.com/nested" in result


@pytest.mark.asyncio
async def test_parse_completion_response_appends_structured_citations():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world","citations":[{"title":"OpenAI","url":"https://openai.com/"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "citations": [
                            {"title": "OpenAI", "url": "https://openai.com/"},
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "## Sources" in result
    assert "https://openai.com/" in result


@pytest.mark.asyncio
async def test_parse_completion_response_appends_annotation_sources():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world","annotations":[{"title":"Docs","url":"https://docs.example.com/"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "annotations": [
                            {"title": "Docs", "url": "https://docs.example.com/"},
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://docs.example.com/" in result


@pytest.mark.asyncio
async def test_parse_completion_response_deduplicates_nested_structured_sources():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"output_text","text":"hello world","annotations":[{"title":"Docs","url":"https://docs.example.com/guide"}],"references":[{"title":"Docs Ref","url":"https://docs.example.com/guide"}]}],"citations":[{"title":"Docs Citation","url":"https://docs.example.com/guide"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "output_text",
                                "text": "hello world",
                                "annotations": [
                                    {"title": "Docs", "url": "https://docs.example.com/guide"},
                                ],
                                "references": [
                                    {"title": "Docs Ref", "url": "https://docs.example.com/guide"},
                                ],
                            }
                        ],
                        "citations": [
                            {"title": "Docs Citation", "url": "https://docs.example.com/guide"},
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert result.count("https://docs.example.com/guide") == 1


@pytest.mark.asyncio
async def test_parse_completion_response_ignores_sources_inside_reasoning_blocks():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"reasoning","text":"hidden","references":[{"title":"Leak","url":"https://docs.example.com/leak"}]},{"type":"output_text","text":"hello world"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "reasoning",
                                "text": "hidden",
                                "references": [
                                    {"title": "Leak", "url": "https://docs.example.com/leak"},
                                ],
                            },
                            {"type": "output_text", "text": "hello world"},
                        ]
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_accepts_source_alias_shapes():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"output_text":"hello world","source_cards":[{"label":"Docs","href":"https://docs.example.com/alias"}],"searchResults":[{"name":"Guide","link":"https://docs.example.com/guide","snippet":"Guide text"}]}',
        json_data={
            "output_text": "hello world",
            "source_cards": [
                {"label": "Docs", "href": "https://docs.example.com/alias"},
            ],
            "searchResults": [
                {
                    "name": "Guide",
                    "link": "https://docs.example.com/guide",
                    "snippet": "Guide text",
                }
            ],
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://docs.example.com/alias" in result
    assert "https://docs.example.com/guide" in result


@pytest.mark.asyncio
async def test_parse_completion_response_accepts_whitespace_padded_source_urls():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world","annotations":[{"title":"Docs","url":" https://docs.example.com/padded "} ]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "annotations": [
                            {"title": "Docs", "url": " https://docs.example.com/padded "},
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://docs.example.com/padded" in result


@pytest.mark.asyncio
async def test_parse_completion_response_accepts_whitespace_padded_string_url_lists():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world"}}],"urls":[" https://docs.example.com/list "]}',
        json_data={
            "choices": [{"message": {"content": "hello world"}}],
            "urls": [" https://docs.example.com/list "],
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://docs.example.com/list" in result


@pytest.mark.asyncio
async def test_parse_completion_response_appends_url_list_sources():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world"}}],"urls":["https://example.com/guide"]}',
        json_data={
            "choices": [{"message": {"content": "hello world"}}],
            "urls": ["https://example.com/guide"],
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://example.com/guide" in result


@pytest.mark.asyncio
async def test_parse_completion_response_falls_back_to_sse_text():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            'data: [DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_falls_back_to_sse_text_and_sources():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"choices":[{"delta":{"content":[{"type":"output_text","text":"hello"}]}}]}\n\n'
            'data: {"choices":[{"delta":{"content":[{"type":"output_text","text":" world","references":[{"title":"Docs","url":"https://docs.example.com/sse"}]}]}}]}\n\n'
            'data:[DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "## Sources" in result
    assert "https://docs.example.com/sse" in result


@pytest.mark.asyncio
async def test_parse_completion_response_reads_sse_top_level_output_text():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"output_text":"hello"}\n\n'
            'data: {"output_text":" world"}\n\n'
            'data: [DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_reads_sse_top_level_output_variant():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"output":{"type":"output_text","text":"hello"}}\n\n'
            'data: {"output":{"type":"output_text","text":" world"}}\n\n'
            'data: [DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_raises_on_empty_placeholder_sse():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"id":"","object":"","created":0,"model":"","system_fingerprint":null,"choices":null,"usage":null}\n\n'
            'data: [DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    with pytest.raises(ValueError, match="空的占位 completion 帧"):
        await provider._parse_completion_response(response)


@pytest.mark.asyncio
async def test_parse_completion_response_raises_on_empty_placeholder_json():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"id":"","object":"","model":"","choices":null,"usage":null}',
        json_data={"id": "", "object": "", "model": "", "choices": None, "usage": None},
    )

    with pytest.raises(ValueError, match="空的占位 completion 帧"):
        await provider._parse_completion_response(response)


@pytest.mark.asyncio
async def test_parse_completion_response_raises_on_login_html():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text="<html><body>Please login to continue</body></html>",
        json_error=ValueError("not json"),
    )

    with pytest.raises(ValueError, match="登录页面"):
        await provider._parse_completion_response(response)


@pytest.mark.asyncio
async def test_parse_streaming_response_does_not_duplicate_existing_sources_heading():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    class LineResponse:
        def __init__(self, text: str):
            self._lines = text.splitlines()
            self.headers = {}

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    response = LineResponse(
        'data: {"choices":[{"delta":{"content":"hello world\\n\\n## Sources\\n1. [Docs](https://docs.example.com/already)"}}],"citations":[{"title":"Docs","url":"https://docs.example.com/already"}]}\n\n'
        'data: [DONE]\n'
    )

    result = await provider._parse_streaming_response(response)

    assert result.count("## Sources") == 1
    assert result.count("https://docs.example.com/already") == 1


@pytest.mark.asyncio
async def test_describe_url_ignores_appended_sources_block(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return (
            "Title: Example Page\n"
            "Extracts: \"Primary fragment\" | \"Second fragment\"\n\n"
            "## Sources\n"
            "1. [Docs](https://docs.example.com/page)"
        )

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.describe_url("https://example.com/page")

    assert result == {
        "title": "Example Page",
        "extracts": '"Primary fragment" | "Second fragment"',
        "url": "https://example.com/page",
    }


@pytest.mark.asyncio
async def test_rank_sources_ignores_appended_sources_block(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return "2 1 3\n\n## Sources\n1. [Docs](https://docs.example.com/page)"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.rank_sources("test query", "1. a\n2. b\n3. c", total=3)

    assert result == [2, 1, 3]


def test_build_placeholder_error_includes_request_id():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    error = provider._build_placeholder_error({"x-request-id": "req-123"})

    assert "req-123" in str(error)


def test_wait_with_retry_after_parses_seconds_header():
    strategy = _WaitWithRetryAfter(multiplier=1, max_wait=10)
    request = DummyResponse(headers={"Retry-After": "3"})

    assert strategy._parse_retry_after(request) == 3.0


def test_wait_with_retry_after_parses_http_date_header():
    strategy = _WaitWithRetryAfter(multiplier=1, max_wait=10)
    future_time = datetime.now(timezone.utc) + timedelta(seconds=5)
    request = DummyResponse(headers={"Retry-After": future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")})

    delay = strategy._parse_retry_after(request)

    assert delay is not None
    assert 0.0 <= delay <= 5.5
