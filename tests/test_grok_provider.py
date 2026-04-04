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


def test_wait_with_retry_after_parses_seconds_header():
    strategy = _WaitWithRetryAfter(multiplier=1, max_wait=10)
    request = DummyResponse(headers={"Retry-After": "3"})

    assert strategy._parse_retry_after(request) == 3.0
