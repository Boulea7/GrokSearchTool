import httpx
import pytest

from grok_search import server
from grok_search.sources import SourcesCache


@pytest.fixture(autouse=True)
def reset_server_state(monkeypatch):
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache(max_size=32))
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")


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
async def test_web_search_surfaces_empty_upstream_response(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "   "

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert result["content"] == "搜索失败: 上游返回空响应，请检查模型或代理配置"
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
