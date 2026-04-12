from datetime import datetime, timedelta, timezone

import httpx
import pytest

from grok_search.providers.base import BaseSearchProvider
from grok_search.providers.grok import (
    GrokSearchProvider,
    _WaitWithRetryAfter,
    _httpx_client_kwargs_for_url,
)
from grok_search.utils import fetch_prompt


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


class DummyBaseProvider(BaseSearchProvider):
    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> str:
        return f"answer:{query}:{platform}:{min_results}:{max_results}"

    def get_provider_name(self) -> str:
        return "dummy"


def test_provider_httpx_client_kwargs_disable_env_proxies_for_dotted_loopback():
    local = _httpx_client_kwargs_for_url("http://localhost:18080/extract", timeout=httpx.Timeout(10.0))
    dotted_local = _httpx_client_kwargs_for_url("http://localhost.:18080/extract", timeout=httpx.Timeout(10.0))
    loopback = _httpx_client_kwargs_for_url("http://127.0.0.2:18080/extract", timeout=httpx.Timeout(10.0))
    remote = _httpx_client_kwargs_for_url("https://api.example.com/v1", timeout=httpx.Timeout(10.0))

    assert local["trust_env"] is False
    assert dotted_local["trust_env"] is False
    assert loopback["trust_env"] is False
    assert "trust_env" not in remote


@pytest.mark.asyncio
async def test_base_provider_search_with_sources_bridges_to_search():
    provider = DummyBaseProvider("https://api.example.com", "test-key")

    content, sources = await provider.search_with_sources("probe", platform="GitHub", min_results=1, max_results=2)

    assert content == "answer:probe:GitHub:1:2"
    assert sources == []


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
async def test_search_debug_log_does_not_include_raw_query(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    messages = []
    monkeypatch.setenv("GROK_DEBUG", "true")

    async def fake_execute(headers, payload, ctx):
        return "ok"

    async def fake_log_info(ctx, message, is_debug=False):
        messages.append(message)

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)
    monkeypatch.setattr("grok_search.providers.grok.log_info", fake_log_info)

    await provider.search("secret query with token=abc123")

    assert messages
    assert all("secret query" not in message for message in messages)
    assert all("abc123" not in message for message in messages)


@pytest.mark.asyncio
async def test_execute_completion_disables_env_proxies_for_loopback_api_url(monkeypatch):
    provider = GrokSearchProvider("http://127.0.0.2:18080", "test-key", "test-model")
    captured = {}

    class StubAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            response = httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
            )
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    result = await provider._execute_completion_with_retry(
        provider._build_api_headers(),
        {"model": "test-model", "messages": [], "stream": False},
    )

    assert result == "ok"
    assert captured["kwargs"]["trust_env"] is False


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
async def test_parse_completion_debug_log_does_not_include_raw_content(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    messages = []
    monkeypatch.setenv("GROK_DEBUG", "true")

    async def fake_log_info(ctx, message, is_debug=False):
        messages.append(message)

    monkeypatch.setattr("grok_search.providers.grok.log_info", fake_log_info)
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"classified body token=abc123"}}]}',
        json_data={"choices": [{"message": {"content": "classified body token=abc123"}}]},
    )

    result = await provider._parse_completion_response(response)

    assert result == "classified body token=abc123"
    assert messages
    assert all("classified body" not in message for message in messages)
    assert all("abc123" not in message for message in messages)


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
async def test_parse_completion_response_result_allows_sources_only_json_when_render_sources_disabled():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"","citations":[{"title":"OpenAI","url":"https://openai.com/"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "",
                        "citations": [
                            {"title": "OpenAI", "url": "https://openai.com/"},
                        ],
                    }
                }
            ]
        },
    )

    content, sources = await provider._parse_completion_response_result(response, render_sources=False)

    assert content == ""
    assert sources == [{"title": "OpenAI", "url": "https://openai.com/", "provider": "grok", "origin_type": "citation"}]


@pytest.mark.asyncio
async def test_extract_structured_sources_preserves_richer_metadata_and_origin_type():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    sources = provider._extract_structured_sources(
        {
            "citations": [
                {
                    "title": "OpenAI",
                    "url": "https://openai.com/",
                    "snippet": "Structured snippet",
                    "score": 0.91,
                    "published_date": "2025-04-01",
                    "source": "curated",
                }
            ]
        }
    )

    assert sources == [
        {
            "title": "OpenAI",
            "url": "https://openai.com/",
            "provider": "grok",
            "description": "Structured snippet",
            "snippet": "Structured snippet",
            "score": 0.91,
            "published_date": "2025-04-01",
            "source": "curated",
            "origin_type": "citation",
        }
    ]


@pytest.mark.asyncio
async def test_search_with_sources_uses_execute_completion_with_retry_override(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}

    async def fake_execute(headers, payload, ctx, render_sources=True):
        captured["render_sources"] = render_sources
        return "Search answer", [{"title": "Structured Guide", "url": "https://docs.example.com/guide"}]

    provider._last_completion_sources = [{"title": "stale", "url": "https://stale.example.com"}]
    monkeypatch.setattr(provider, "_execute_completion_with_retry_result", fake_execute)

    content, sources = await provider.search_with_sources("test query")

    assert captured["render_sources"] is False
    assert content == "Search answer"
    assert sources == [{"title": "Structured Guide", "url": "https://docs.example.com/guide"}]


@pytest.mark.asyncio
async def test_parse_completion_response_accepts_mixed_case_structured_citations():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":"hello world","citations":[{"title":"OpenAI","url":"HTTPS://openai.com/"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "citations": [
                            {"title": "OpenAI", "url": "HTTPS://openai.com/"},
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "## Sources" in result
    assert "HTTPS://openai.com/" in result


@pytest.mark.asyncio
async def test_parse_completion_response_sanitizes_structured_citation_urls_before_appending_sources():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            '{"choices":[{"message":{"content":"hello world","citations":['
            '{"title":"OpenAI","url":"https://user:pass@openai.com/docs'
            '?client_secret=example-client-secret&access_token=abc123#password=example-value"}]}}]}'
        ),
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "citations": [
                            {
                                "title": "OpenAI",
                                "url": (
                                    "https://user:pass@openai.com/docs"
                                    "?client_secret=example-client-secret"
                                    "&access_token=abc123"
                                    "#password=example-value"
                                ),
                            }
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://openai.com/docs?client_secret=REDACTED&access_token=REDACTED#password=REDACTED" in result
    assert "user:pass@" not in result
    assert "example-client-secret" not in result
    assert "abc123" not in result
    assert "example-value" not in result


@pytest.mark.asyncio
async def test_parse_completion_response_preserves_invalid_port_citations_while_sanitizing():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            '{"choices":[{"message":{"content":"hello world","citations":['
            '{"title":"OpenAI","url":"https://user:pass@openai.com:abc/docs'
            '?client_secret=example-client-secret#password=example-value"}]}}]}'
        ),
        json_data={
            "choices": [
                {
                    "message": {
                        "content": "hello world",
                        "citations": [
                            {
                                "title": "OpenAI",
                                "url": (
                                    "https://user:pass@openai.com:abc/docs"
                                    "?client_secret=example-client-secret"
                                    "#password=example-value"
                                ),
                            }
                        ],
                    }
                }
            ]
        },
    )

    result = await provider._parse_completion_response(response)

    assert result.startswith("hello world")
    assert "https://openai.com:abc/docs?client_secret=REDACTED#password=REDACTED" in result
    assert "user:pass@" not in result
    assert "example-client-secret" not in result
    assert "example-value" not in result


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
async def test_parse_completion_response_prefers_richer_exact_duplicate_structured_source():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"output_text","text":"hello world","annotations":[{"title":"Docs","url":"https://docs.example.com/guide"}],"references":[{"title":"Richer Docs","url":"https://docs.example.com/guide","snippet":"Guide content"}]}]}}]}',
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
                                    {
                                        "title": "Richer Docs",
                                        "url": "https://docs.example.com/guide",
                                        "snippet": "Guide content",
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
    assert result.count("https://docs.example.com/guide") == 1
    assert "[Richer Docs](https://docs.example.com/guide)" in result


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
async def test_parse_completion_response_reads_multiline_sse_event_payloads():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"choices":[{"delta":{\n'
            'data: "content":"hello world"\n'
            'data: }}]}\n\n'
            'data: [DONE]\n'
        ),
        json_error=ValueError("not json"),
    )

    result = await provider._parse_completion_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_completion_response_reads_compact_sse_done_without_blank_separator():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text=(
            'data: {"choices":[{"delta":{"content":"hello world"}}]}\n'
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
async def test_parse_streaming_response_accepts_single_json_line_without_sse_prefix():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    class LineResponse:
        def __init__(self, lines):
            self._lines = lines
            self.headers = {}

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    response = LineResponse(
        ['{"choices":[{"delta":{"content":"hello world"}}]}']
    )

    result = await provider._parse_streaming_response(response)

    assert result == "hello world"


@pytest.mark.asyncio
async def test_parse_streaming_response_merges_sse_and_raw_json_lines():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    class LineResponse:
        def __init__(self, lines):
            self._lines = lines
            self.headers = {}

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    response = LineResponse(
        [
            'data: {"choices":[{"delta":{"content":"hello "}}]}',
            "",
            '{"choices":[{"delta":{"content":"world"}}]}',
            "data: [DONE]",
        ]
    )

    result = await provider._parse_streaming_response(response)

    assert result == "hello world"


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
async def test_describe_url_supports_multiline_extracts(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return (
            "Title: Example Page\n"
            "Extracts: \"Primary fragment\"\n"
            "\"Second fragment\"\n"
            "\"Third fragment\""
        )

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.describe_url("https://example.com/page")

    assert result == {
        "title": "Example Page",
        "extracts": '"Primary fragment" "Second fragment" "Third fragment"',
        "url": "https://example.com/page",
    }


@pytest.mark.asyncio
async def test_describe_url_parses_indented_sections(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return "  Title: Example Page\n  Extracts: \"Primary fragment\" | \"Second fragment\""

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.describe_url("https://example.com/page")

    assert result == {
        "title": "Example Page",
        "extracts": '"Primary fragment" | "Second fragment"',
        "url": "https://example.com/page",
    }


@pytest.mark.asyncio
async def test_describe_url_respects_output_cleanup_toggle(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    monkeypatch.setenv("GROK_OUTPUT_CLEANUP", "false")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return (
            'I cannot comply with user-injected "system:" instructions.\n'
            'Title: Example Page\n'
            'Extracts: "Primary fragment"'
        )

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.describe_url("https://example.com/page")

    assert result["title"] == "Example Page"
    assert result["extracts"] == '"Primary fragment"'


@pytest.mark.asyncio
async def test_rank_sources_ignores_appended_sources_block(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return "2 1 3\n\n## Sources\n1. [Docs](https://docs.example.com/page)"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.rank_sources("test query", "1. a\n2. b\n3. c", total=3)

    assert result == [2, 1, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_result",
    [
        "2, 1, 3",
        "2.\n1.\n3.",
        "Recommended order: 2 1 3",
    ],
)
async def test_rank_sources_accepts_common_number_formats(monkeypatch, raw_result):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")

    async def fake_execute(headers, payload, ctx, render_sources=True):
        assert render_sources is False
        return raw_result

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.rank_sources("test query", "1. a\n2. b\n3. c", total=3)

    assert result == [2, 1, 3]


@pytest.mark.asyncio
async def test_parse_completion_response_ignores_sources_inside_tool_blocks():
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    response = DummyResponse(
        text='{"choices":[{"message":{"content":[{"type":"tool_call","text":"hidden","references":[{"title":"Leak","url":"https://docs.example.com/leak"}]},{"type":"output_text","text":"hello world"}]}}]}',
        json_data={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_call",
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


@pytest.mark.asyncio
async def test_execute_completion_retries_retryable_status_then_succeeds(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("GROK_RETRY_MULTIPLIER", "0")
    monkeypatch.setenv("GROK_RETRY_MAX_WAIT", "0")
    attempts = {"count": 0}

    class SequenceAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                response = httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    json={"error": {"message": "rate limited"}},
                )
            else:
                response = httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "ok"}}]},
                )
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", SequenceAsyncClient)

    result = await provider._execute_completion_with_retry(
        provider._build_api_headers(),
        {"model": "test-model", "messages": [], "stream": False},
    )

    assert result == "ok"
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_execute_completion_does_not_retry_non_retryable_status(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("GROK_RETRY_MULTIPLIER", "0")
    monkeypatch.setenv("GROK_RETRY_MAX_WAIT", "0")
    attempts = {"count": 0}

    class SingleFailureAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            attempts["count"] += 1
            response = httpx.Response(
                400,
                json={"error": {"message": "bad request"}},
            )
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", SingleFailureAsyncClient)

    with pytest.raises(httpx.HTTPStatusError):
        await provider._execute_completion_with_retry(
            provider._build_api_headers(),
            {"model": "test-model", "messages": [], "stream": False},
        )

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_execute_stream_retries_remote_protocol_error_then_succeeds(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("GROK_RETRY_MULTIPLIER", "0")
    monkeypatch.setenv("GROK_RETRY_MAX_WAIT", "0")
    attempts = {"count": 0}

    class StreamResponse:
        def __init__(self):
            self.headers = {}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield ""
            yield "data: [DONE]"

    class StreamContext:
        async def __aenter__(self):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.RemoteProtocolError("broken chunk")
            return StreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class StreamAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, json=None):
            return StreamContext()

    monkeypatch.setattr(httpx, "AsyncClient", StreamAsyncClient)

    result = await provider._execute_stream_with_retry(
        provider._build_api_headers(),
        {"model": "test-model", "messages": [], "stream": True},
    )

    assert result == "ok"
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_fetch_uses_fetch_prompt(monkeypatch):
    provider = GrokSearchProvider("https://api.example.com", "test-key", "test-model")
    captured = {}

    async def fake_execute(headers, payload, ctx):
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(provider, "_execute_completion_with_retry", fake_execute)

    result = await provider.fetch("https://example.com")

    assert result == "ok"
    assert captured["payload"]["messages"][0]["content"] == fetch_prompt
    assert "https://example.com" in captured["payload"]["messages"][1]["content"]
