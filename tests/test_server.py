import json

import httpx
import pytest

from grok_search import server
from grok_search.sources import SourcesCache


@pytest.fixture(autouse=True)
def reset_server_state(monkeypatch):
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache(max_size=32))
    monkeypatch.setattr(server.config, "_cached_model", None, raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")


class FakeAsyncClient:
    def __init__(self, responses=None, exc=None, *args, **kwargs):
        self._responses = responses or {}
        self._exc = exc or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        if ("GET", url) in self._exc:
            raise self._exc[("GET", url)]
        response = self._responses[("GET", url)]
        response.request = httpx.Request("GET", url, headers=headers)
        return response

    async def post(self, url, headers=None, json=None):
        if ("POST", url) in self._exc:
            raise self._exc[("POST", url)]
        response = self._responses[("POST", url)]
        response.request = httpx.Request("POST", url, headers=headers, json=json)
        return response


@pytest.mark.asyncio
async def test_web_search_surfaces_http_redirect(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
            response = httpx.Response(
                307,
                request=request,
                headers={"location": "/zh-CN/login?from=%2Fmodels"},
            )
            raise httpx.HTTPStatusError("redirect", request=request, response=response)

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert "HTTP 307" in result["content"]
    assert "/zh-CN/login" in result["content"]
    assert result["sources_count"] == 0


@pytest.mark.asyncio
async def test_get_config_info_returns_doctor_and_feature_readiness(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": [{"raw_content": "ok"}]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"data": {"markdown": "# ok"}},
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["connection_test"]["status"] == "连接成功"
    assert payload["doctor"]["status"] == "ok"
    assert payload["doctor"]["checks"]
    assert payload["feature_readiness"]["web_search"]["status"] == "ready"
    assert payload["feature_readiness"]["get_sources"]["status"] == "ready"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "ready"
    assert payload["feature_readiness"]["web_map"]["status"] == "ready"
    assert payload["feature_readiness"]["toggle_builtin_tools"]["client_specific"] is True


@pytest.mark.asyncio
async def test_get_config_info_marks_missing_grok_config_as_not_ready(monkeypatch):
    monkeypatch.delenv("GROK_API_URL", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient({}, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["connection_test"]["status"] == "配置错误"
    assert payload["doctor"]["status"] == "error"
    assert payload["feature_readiness"]["web_search"]["status"] == "not_ready"
    assert payload["feature_readiness"]["get_sources"]["status"] == "ready"
    assert payload["doctor"]["recommendations"]


@pytest.mark.asyncio
async def test_get_config_info_skips_unconfigured_optional_providers(monkeypatch):
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())
    checks = {check["check_id"]: check for check in payload["doctor"]["checks"]}

    assert checks["tavily_extract"]["status"] == "skipped"
    assert checks["firecrawl_scrape"]["status"] == "skipped"
    assert checks["tavily_map"]["status"] == "skipped"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "not_ready"
    assert payload["feature_readiness"]["web_map"]["status"] == "not_ready"


@pytest.mark.asyncio
async def test_get_config_info_marks_provider_probe_failures_as_degraded(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    exceptions = {
        ("POST", "https://api.tavily.com/extract"): httpx.TimeoutException("timeout"),
        ("POST", "https://api.tavily.com/map"): httpx.TimeoutException("timeout"),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, exceptions, *args, **kwargs))

    payload = json.loads(await server.get_config_info())
    checks = {check["check_id"]: check for check in payload["doctor"]["checks"]}

    assert payload["doctor"]["status"] == "partial"
    assert checks["tavily_extract"]["status"] == "error"
    assert checks["tavily_map"]["status"] == "error"
    assert payload["feature_readiness"]["web_map"]["status"] == "degraded"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"
    assert payload["doctor"]["recommendations"]


@pytest.mark.asyncio
async def test_get_config_info_finds_claude_project_root_from_subdirectory(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    nested = repo_root / "nested" / "child"
    (repo_root / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["feature_readiness"]["toggle_builtin_tools"]["status"] == "ready"


@pytest.mark.asyncio
async def test_get_config_info_ignores_client_specific_toggle_in_overall_doctor_status(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: None)

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": [{"raw_content": "ok"}]},
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"data": {"markdown": "# ok"}},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["feature_readiness"]["toggle_builtin_tools"]["status"] == "not_ready"
    assert payload["feature_readiness"]["toggle_builtin_tools"]["client_specific"] is True
    assert payload["doctor"]["status"] == "ok"


@pytest.mark.asyncio
async def test_get_config_info_warns_when_api_url_has_no_v1(monkeypatch):
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com")
    responses = {
        ("GET", "https://api.example.com/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())
    checks = {check["check_id"]: check for check in payload["doctor"]["checks"]}

    assert checks["grok_api_url_format"]["status"] == "warning"
    assert payload["doctor"]["status"] == "partial"
    assert any("/v1" in item for item in payload["doctor"]["recommendations"])


@pytest.mark.asyncio
async def test_get_config_info_marks_configured_model_mismatch_as_degraded(monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "missing-model")

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}, {"id": "grok-4-fast"}]},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["connection_test"]["status"] == "连接成功"
    assert payload["feature_readiness"]["web_search"]["status"] == "degraded"
    assert "missing-model" in payload["feature_readiness"]["web_search"]["message"]
    assert any("missing-model" in item for item in payload["doctor"]["recommendations"])
    assert any("grok-4.1-fast" in item for item in payload["doctor"]["recommendations"])


@pytest.mark.asyncio
async def test_get_config_info_marks_persisted_model_mismatch_as_degraded(monkeypatch):
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(server.config, "_load_config_file", lambda: {"model": "persisted-model"})

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient(responses, {}, *args, **kwargs))

    payload = json.loads(await server.get_config_info())

    assert payload["feature_readiness"]["web_search"]["status"] == "degraded"
    assert "persisted-model" in payload["feature_readiness"]["web_search"]["message"]
    assert any("persisted-model" in item for item in payload["doctor"]["recommendations"])
    grok_check = next(
        check for check in payload["doctor"]["checks"] if check.get("check_id") == "grok_model_selection"
    )
    assert grok_check["status"] == "warning"
    assert "persisted-model" in grok_check["message"]


@pytest.mark.asyncio
async def test_web_search_returns_structured_status_fields_for_legacy_call(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert result["status"] == "ok"
    assert result["error"] is None
    assert result["warnings"] == []
    assert result["effective_params"] == {
        "platform": "",
        "topic": "general",
        "time_range": None,
        "include_domains": [],
        "exclude_domains": [],
        "model": "",
        "extra_sources": 0,
    }


@pytest.mark.asyncio
async def test_web_search_echoes_effective_params_for_new_controls(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search(
        "test query",
        topic="news",
        time_range="week",
        include_domains=["openai.com"],
        exclude_domains=["example.com"],
        extra_sources=1,
    )

    assert result["status"] == "ok"
    assert result["effective_params"]["topic"] == "news"
    assert result["effective_params"]["time_range"] == "week"
    assert result["effective_params"]["include_domains"] == ["openai.com"]
    assert result["effective_params"]["exclude_domains"] == ["example.com"]


@pytest.mark.asyncio
async def test_web_search_marks_partial_when_controls_are_not_applied_without_tavily_search(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search(
        "test query",
        topic="news",
        time_range="week",
        include_domains=["openai.com"],
        extra_sources=0,
    )

    assert result["status"] == "partial"
    assert "domain_controls_not_applied_without_tavily_search" in result["warnings"]
    assert "time_range_not_applied_without_tavily_search" in result["warnings"]
    assert "topic_not_applied_without_tavily_search" in result["warnings"]


@pytest.mark.asyncio
async def test_web_search_rejects_overlapping_include_and_exclude_domains():
    result = await server.web_search(
        "test query",
        include_domains=["openai.com"],
        exclude_domains=["openai.com"],
    )

    assert result["status"] == "error"
    assert result["error"] == "validation_error"
    assert "同时出现在 include_domains 与 exclude_domains" in result["content"]
    assert result["sources_count"] == 0


@pytest.mark.asyncio
async def test_web_search_marks_partial_when_controls_cannot_be_applied(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    result = await server.web_search(
        "test query",
        topic="news",
        time_range="week",
        include_domains=["openai.com"],
        extra_sources=0,
    )

    assert result["status"] == "partial"
    assert result["error"] is None
    assert "domain_controls_not_applied_without_tavily" in result["warnings"]
    assert "time_range_not_applied_without_tavily" in result["warnings"]
    assert result["effective_params"]["topic"] == "news"
    assert result["effective_params"]["include_domains"] == ["openai.com"]


@pytest.mark.asyncio
async def test_web_search_prioritizes_tavily_when_controls_need_it(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        calls["tavily"] = max_results
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    async def fake_firecrawl(query, limit):
        calls["firecrawl"] = limit
        return [{"title": "Firecrawl", "url": "https://firecrawl.example.com", "description": "f"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_search(
        "test query",
        topic="news",
        include_domains=["openai.com"],
        extra_sources=1,
    )

    assert calls["tavily"] == 1
    assert calls["firecrawl"] == 0
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_web_search_uses_only_tavily_for_filtered_extra_sources(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        calls["tavily"] = max_results
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    async def fake_firecrawl(query, limit):
        calls["firecrawl"] = limit
        return [{"title": "Firecrawl", "url": "https://firecrawl.example.com", "description": "f"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_search(
        "test query",
        topic="news",
        include_domains=["openai.com"],
        extra_sources=5,
    )

    assert calls["tavily"] == 5
    assert calls["firecrawl"] == 0
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_web_search_marks_partial_when_tavily_extra_search_fails(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search("test query", extra_sources=1)

    assert result["status"] == "partial"
    assert "tavily_search_unavailable" in result["warnings"]
    assert result["error"] is None


@pytest.mark.asyncio
async def test_web_search_surfaces_empty_upstream_response(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "   "

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert result["content"] == "搜索失败: 上游返回空响应，请检查模型或代理配置"
    assert result["status"] == "error"
    assert result["error"] == "upstream_empty_response"
    assert result["sources_count"] == 0


@pytest.mark.asyncio
async def test_web_search_extracts_inline_links_as_sources(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "OpenAI docs: [OpenAI](https://openai.com/)"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")
    cached = await server.get_sources(result["session_id"])

    assert result["content"] == "OpenAI docs: [OpenAI](https://openai.com/)"
    assert result["sources_count"] == 1
    assert cached["sources"][0]["url"] == "https://openai.com/"


@pytest.mark.asyncio
async def test_get_sources_returns_standardized_metadata_for_inline_links(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "OpenAI docs: [OpenAI](https://openai.com/)"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")
    cached = await server.get_sources(result["session_id"])
    source = cached["sources"][0]

    assert source == {
        "title": "OpenAI",
        "url": "https://openai.com/",
        "provider": "grok",
        "source_type": "web_page",
        "description": "",
        "snippet": "",
        "domain": "openai.com",
        "score": None,
        "published_at": None,
        "retrieved_at": source["retrieved_at"],
        "rank": 1,
    }
    assert source["retrieved_at"].endswith("Z")


@pytest.mark.asyncio
async def test_web_search_splits_extra_sources_across_providers(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        calls["tavily"] = max_results
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    async def fake_firecrawl(query, limit):
        calls["firecrawl"] = limit
        return [{"title": "Firecrawl", "url": "https://firecrawl.example.com", "description": "f"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_search("test query", extra_sources=5)

    assert calls["tavily"] > 0
    assert calls["firecrawl"] > 0
    assert calls["tavily"] + calls["firecrawl"] == 5
    assert result["sources_count"] == 2


@pytest.mark.asyncio
async def test_web_search_respects_tavily_enabled_false(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results):
        calls["tavily"] += 1
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    async def fake_firecrawl(query, limit):
        calls["firecrawl"] = limit
        return [{"title": "Firecrawl", "url": "https://firecrawl.example.com", "description": "f"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_search("test query", extra_sources=3)

    assert calls["tavily"] == 0
    assert calls["firecrawl"] == 3
    assert result["sources_count"] == 1


@pytest.mark.asyncio
async def test_get_sources_standardizes_merged_provider_metadata(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        return [
            {
                "title": "OpenAI Blog",
                "url": "https://openai.com/blog",
                "content": "Latest updates",
                "score": 0.91,
            }
        ]

    async def fake_firecrawl(query, limit):
        return [
            {
                "title": "Example Docs",
                "url": "https://docs.example.com/guide",
                "description": "Guide content",
            }
        ]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_search", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_search("test query", extra_sources=2)
    cached = await server.get_sources(result["session_id"])

    assert result["sources_count"] == 2
    assert cached["sources"] == [
        {
            "title": "OpenAI Blog",
            "url": "https://openai.com/blog",
            "provider": "tavily",
            "source_type": "web_page",
            "description": "Latest updates",
            "snippet": "Latest updates",
            "domain": "openai.com",
            "score": 0.91,
            "published_at": None,
            "retrieved_at": cached["sources"][0]["retrieved_at"],
            "rank": 1,
        },
        {
            "title": "Example Docs",
            "url": "https://docs.example.com/guide",
            "provider": "firecrawl",
            "source_type": "web_page",
            "description": "Guide content",
            "snippet": "Guide content",
            "domain": "docs.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": cached["sources"][1]["retrieved_at"],
            "rank": 2,
        },
    ]


@pytest.mark.asyncio
async def test_get_sources_keeps_grok_citations_ahead_of_supplemental_sources(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Primary citation: [Primary Source](https://primary.example.com/)"

    async def fake_tavily(query, max_results, **kwargs):
        return [
            {
                "title": "OpenAI Blog",
                "url": "https://openai.com/blog",
                "content": "Latest updates",
                "score": 0.91,
            }
        ]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search("test query", extra_sources=1)
    cached = await server.get_sources(result["session_id"])

    assert [item["url"] for item in cached["sources"]] == [
        "https://primary.example.com/",
        "https://openai.com/blog",
    ]
    assert [item["provider"] for item in cached["sources"]] == ["grok", "tavily"]


@pytest.mark.asyncio
async def test_get_sources_standardizes_legacy_cached_sources_on_read():
    session_id = "legacy-session"
    await server._SOURCES_CACHE.set(
        session_id,
        [
            {
                "title": "Legacy Source",
                "url": "https://legacy.example.com/page",
                "description": "Legacy description",
                "provider": "firecrawl",
            }
        ],
    )

    cached = await server.get_sources(session_id)

    assert cached["sources"] == [
        {
            "title": "Legacy Source",
            "url": "https://legacy.example.com/page",
            "description": "Legacy description",
            "provider": "firecrawl",
            "source_type": "web_page",
            "snippet": "Legacy description",
            "domain": "legacy.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": cached["sources"][0]["retrieved_at"],
            "rank": 1,
        }
    ]


@pytest.mark.asyncio
async def test_get_sources_reuses_standardized_timestamp_for_legacy_cache():
    session_id = "legacy-session-stable"
    await server._SOURCES_CACHE.set(
        session_id,
        [
            {
                "title": "Legacy Source",
                "url": "https://legacy.example.com/page",
                "description": "Legacy description",
            }
        ],
    )

    first = await server.get_sources(session_id)
    migrated = await server._SOURCES_CACHE.get(session_id)
    second = await server.get_sources(session_id)

    assert migrated[0]["retrieved_at"] == first["sources"][0]["retrieved_at"]
    assert first["sources"][0]["retrieved_at"] == second["sources"][0]["retrieved_at"]


def test_probably_truncated_content_detects_obvious_markers():
    assert server._is_probably_truncated_content("Partial answer [...]", min_length=10) is True
    assert server._is_probably_truncated_content("```\ncode block never closes", min_length=10) is True
    assert server._is_probably_truncated_content("A complete sentence.\n\nAnother paragraph.", min_length=10) is False


@pytest.mark.asyncio
async def test_web_search_surfaces_sources_only_response_without_empty_content(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return """
## Sources
1. [OpenAI](https://openai.com/)
2. [Wikipedia](https://en.wikipedia.org/wiki/OpenAI)
"""

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert "只返回了信源列表" in result["content"]
    assert result["sources_count"] == 2


def test_configure_windows_event_loop_policy(monkeypatch):
    class DummyPolicy:
        pass

    captured = {}

    monkeypatch.setattr(server.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(server.asyncio, "WindowsSelectorEventLoopPolicy", DummyPolicy, raising=False)
    monkeypatch.setattr(
        server.asyncio,
        "set_event_loop_policy",
        lambda policy: captured.setdefault("policy", policy),
    )

    server._configure_windows_event_loop_policy()

    assert isinstance(captured["policy"], DummyPolicy)


@pytest.mark.asyncio
async def test_web_fetch_surfaces_provider_errors(monkeypatch):
    async def fake_tavily(url):
        return None, "Tavily 返回 HTTP 307 重定向到 /login"

    async def fake_firecrawl(url, ctx):
        return None, "Firecrawl 返回 HTTP 401，请检查认证状态"

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_fetch("https://example.com")

    assert "Tavily 返回 HTTP 307" in result
    assert "Firecrawl 返回 HTTP 401" in result


@pytest.mark.asyncio
async def test_web_fetch_skips_tavily_when_disabled(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    async def fake_tavily(url):
        calls["tavily"] += 1
        return None, "should not be called"

    async def fake_firecrawl(url, ctx):
        calls["firecrawl"] += 1
        return "# Firecrawl content", None

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_fetch("https://example.com")

    assert calls["tavily"] == 0
    assert calls["firecrawl"] == 1
    assert result == "# Firecrawl content"


@pytest.mark.asyncio
async def test_web_fetch_preserves_config_error_when_no_extractors(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    async def fake_tavily(url):
        return None, None

    async def fake_firecrawl(url, ctx):
        return None, None

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)

    result = await server.web_fetch("https://example.com")

    assert result == "配置错误: TAVILY_API_KEY 和 FIRECRAWL_API_KEY 均未配置"


@pytest.mark.asyncio
async def test_web_fetch_falls_back_when_tavily_reports_truncated_content(monkeypatch):
    async def fake_tavily(url):
        return None, "Tavily 提取结果疑似被截断"

    async def fake_firecrawl(url, ctx):
        return "# Restored full content", None

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_fetch("https://example.com")

    assert result == "# Restored full content"
