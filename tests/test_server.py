import json

import httpx
import pytest

from grok_search import server
from grok_search.sources import SourcesCache


@pytest.fixture(autouse=True)
def reset_server_state(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_SOURCES_CACHE", SourcesCache(max_size=32))
    monkeypatch.setattr(server, "_AVAILABLE_MODELS_CACHE", {})
    monkeypatch.setattr(server.config, "_project_root", lambda: tmp_path)
    server.config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")


class StubAsyncClient:
    def __init__(self, responses=None, exc=None, *args, **kwargs):
        self._responses = responses or {}
        self._exc = exc or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def _take(self, store, key):
        items = store[key]
        if not isinstance(items, list):
            return items
        item = items.pop(0)
        if items:
            store[key] = items
        else:
            del store[key]
        return item

    async def get(self, url, headers=None):
        key = ("GET", url)
        if key in self._exc:
            raise self._take(self._exc, key)
        response = self._take(self._responses, key)
        response.request = httpx.Request("GET", url, headers=headers)
        return response

    async def post(self, url, headers=None, json=None):
        key = ("POST", url)
        if key in self._exc:
            raise self._take(self._exc, key)
        if key not in self._responses and url.endswith("/chat/completions"):
            response = httpx.Response(
                200,
                json={"choices": [{"message": {"content": "probe ok"}}]},
            )
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response
        response = self._take(self._responses, key)
        response.request = httpx.Request("POST", url, headers=headers, json=json)
        return response


def patch_async_client(monkeypatch, responses=None, exceptions=None):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: StubAsyncClient(responses, exceptions, *args, **kwargs),
    )


async def load_config_info():
    return json.loads(await server.get_config_info())


def doctor_checks(payload):
    return {check["check_id"]: check for check in payload["doctor"]["checks"]}


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
async def test_web_search_masks_sensitive_redirect_target_details(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
            response = httpx.Response(
                307,
                request=request,
                headers={
                    "location": (
                        "https://user:pass@login.example.com/callback"
                        "?code=one-time-code&access_token=abc123#auth_token=secret456"
                    )
                },
            )
            raise httpx.HTTPStatusError("redirect", request=request, response=response)

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")

    assert "HTTP 307" in result["content"]
    assert "login.example.com" in result["content"]
    assert "user:pass" not in result["content"]
    assert "one-time-code" not in result["content"]
    assert "abc123" not in result["content"]
    assert "secret456" not in result["content"]


@pytest.mark.asyncio
async def test_get_config_info_returns_doctor_and_feature_readiness(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert payload["connection_test"]["status"] == "连接成功"
    assert payload["connection_test"]["scope"] == "models_endpoint"
    assert payload["GROK_TIME_CONTEXT_MODE"] == "auto"
    assert payload["doctor"]["status"] == "ok"
    assert payload["doctor"]["checks"]
    assert checks["grok_search_probe"]["status"] == "ok"
    assert checks["web_fetch_probe"]["status"] == "ok"
    assert payload["feature_readiness"]["web_search"]["status"] == "ready"
    assert payload["feature_readiness"]["get_sources"]["status"] == "partial_ready"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "ready"
    assert payload["feature_readiness"]["web_fetch"]["providers"]["verified_path"] == "tavily"
    assert payload["feature_readiness"]["web_fetch"]["providers"]["tavily"]["status"] == "ready"
    assert payload["feature_readiness"]["web_fetch"]["providers"]["firecrawl"]["status"] == "ready"
    assert payload["feature_readiness"]["web_map"]["status"] == "ready"
    assert payload["feature_readiness"]["toggle_builtin_tools"]["client_specific"] is True


@pytest.mark.asyncio
async def test_get_config_info_marks_missing_grok_config_as_not_ready(monkeypatch):
    monkeypatch.delenv("GROK_API_URL", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    patch_async_client(monkeypatch, {}, {})

    payload = await load_config_info()

    assert payload["connection_test"]["status"] == "配置错误"
    assert payload["doctor"]["status"] == "error"
    assert payload["feature_readiness"]["web_search"]["status"] == "not_ready"
    assert payload["feature_readiness"]["get_sources"]["status"] == "not_ready"
    assert payload["doctor"]["recommendations"]


@pytest.mark.asyncio
async def test_probe_json_endpoint_masks_sensitive_http_error_text(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")
    patch_async_client(
        monkeypatch,
        {
            ("GET", "https://api.example.com/v1/models"): httpx.Response(
                403,
                text='{"error":"Bearer sk-secret-value token=abc123"}',
                headers={"content-type": "application/json"},
            ),
        },
    )

    result = await server._probe_json_endpoint(
        "grok_models",
        "GET",
        "https://api.example.com/v1/models",
        {"Authorization": "Bearer sk-secret-value"},
    )

    assert result["status"] == "error"
    assert "sk-secret-value" not in result["message"]
    assert "abc123" not in result["message"]
    assert "Bearer ***" in result["message"]
    assert "token=***" in result["message"]


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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "skipped"
    assert checks["firecrawl_scrape"]["status"] == "skipped"
    assert checks["tavily_map"]["status"] == "skipped"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "not_ready"
    assert payload["feature_readiness"]["web_map"]["status"] == "not_ready"
    assert (
        payload["feature_readiness"]["web_fetch"]["providers"]["tavily"]["skipped_reason"]
        == "TAVILY_API_KEY 未配置"
    )
    assert (
        payload["feature_readiness"]["web_fetch"]["providers"]["firecrawl"]["skipped_reason"]
        == "FIRECRAWL_API_KEY 未配置"
    )
    assert payload["doctor"]["recommendations_detail"]
    assert {
        item["check_id"] for item in payload["doctor"]["recommendations_detail"]
    } >= {"tavily_extract", "tavily_map", "firecrawl_scrape"}


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
    patch_async_client(monkeypatch, responses, exceptions)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert payload["doctor"]["status"] == "partial"
    assert checks["tavily_extract"]["status"] == "error"
    assert checks["tavily_map"]["status"] == "error"
    assert payload["feature_readiness"]["web_map"]["status"] == "degraded"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"
    assert payload["doctor"]["recommendations"]


@pytest.mark.asyncio
async def test_get_config_info_rejects_tavily_login_html_probe(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            text="<html><body>Please login to continue</body></html>",
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "error"
    assert "登录页" in checks["tavily_extract"]["message"]
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_get_config_info_rejects_malformed_tavily_probe_shape(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"unexpected": []},
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"wrong": []},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "error"
    assert "响应结构异常" in checks["tavily_extract"]["message"]
    assert checks["tavily_map"]["status"] == "error"
    assert "响应结构异常" in checks["tavily_map"]["message"]


@pytest.mark.asyncio
async def test_get_config_info_rejects_tavily_map_non_string_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
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
            json={"results": [{"url": "https://example.com"}]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_map"]["status"] == "error"
    assert "results[0]" in checks["tavily_map"]["message"]


@pytest.mark.asyncio
async def test_get_config_info_rejects_empty_tavily_probe_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": []},
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "error"
    assert "results 为空" in checks["tavily_extract"]["message"]
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_get_config_info_rejects_empty_tavily_probe_content(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": [{"raw_content": ""}]},
        ),
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "error"
    assert "内容为空" in checks["tavily_extract"]["message"]
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_get_config_info_marks_empty_firecrawl_markdown_as_warning(monkeypatch):
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
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"data": {"markdown": ""}},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["firecrawl_scrape"]["status"] == "warning"
    assert "markdown 为空" in checks["firecrawl_scrape"]["message"]
    assert payload["doctor"]["status"] == "partial"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "ready"


@pytest.mark.asyncio
async def test_get_config_info_accepts_firecrawl_top_level_markdown_shape(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"markdown": "# flat markdown"},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["firecrawl_scrape"]["status"] == "ok"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "ready"


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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()

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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()

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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["grok_api_url_format"]["status"] == "warning"
    assert payload["doctor"]["status"] == "partial"
    assert any("/v1" in item for item in payload["doctor"]["recommendations"])


@pytest.mark.asyncio
async def test_get_config_info_maps_models_login_page_to_connection_failure(monkeypatch):
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            text="<html><body>Please login to continue</body></html>",
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()

    assert payload["connection_test"]["status"] == "连接失败"
    assert "登录页" in payload["connection_test"]["message"]


@pytest.mark.parametrize(
    ("error_kind", "expected_status"),
    [
        ("timeout", "连接超时"),
        ("request_error", "连接失败"),
        ("http_error", "连接异常"),
        ("config_error", "配置错误"),
        ("html_response", "连接异常"),
        ("invalid_json", "连接异常"),
    ],
)
def test_build_connection_test_from_models_check_maps_error_kinds(error_kind, expected_status):
    result = server._build_connection_test_from_models_check(
        {
            "status": "error",
            "message": "probe failed",
            "error_kind": error_kind,
            "response_time_ms": 12.3,
        }
    )

    assert result["status"] == expected_status
    assert result["scope"] == "models_endpoint"
    assert result["message"] == "probe failed"


def test_httpx_client_kwargs_disable_env_proxies_for_full_loopback_range():
    local = server._httpx_client_kwargs_for_url("http://localhost:18080/extract", timeout=10.0)
    loopback = server._httpx_client_kwargs_for_url("http://127.0.0.1:18080/map", timeout=10.0)
    loopback_alias = server._httpx_client_kwargs_for_url("http://127.0.0.2:18080/map", timeout=10.0)
    short_loopback = server._httpx_client_kwargs_for_url("http://127.1:18080/map", timeout=10.0)
    remote = server._httpx_client_kwargs_for_url("https://api.tavily.com/extract", timeout=10.0)

    assert local["trust_env"] is False
    assert loopback["trust_env"] is False
    assert loopback_alias["trust_env"] is False
    assert short_loopback["trust_env"] is False
    assert "trust_env" not in remote


@pytest.mark.parametrize(
    ("url", "expected_message"),
    [
        ("http://localhost./", "目标 URL 不能指向本地或私有网络"),
        ("http://127.1/", "目标 URL 不能指向本地或私有网络"),
        ("http://127.0.1/", "目标 URL 不能指向本地或私有网络"),
        ("http://127.0.0.1.nip.io/", "目标 URL 不能指向本地或私有网络"),
        ("http://app.127.0.0.1.sslip.io/", "目标 URL 不能指向本地或私有网络"),
        ("http://127-0-0-1.xip.io/", "目标 URL 不能指向本地或私有网络"),
        ("http://10.0.0.8.nip.io/", "目标 URL 不能指向本地或私有网络"),
        ("http://192-168-1-20.sslip.io/", "目标 URL 不能指向本地或私有网络"),
        ("http://foo.localhost/", "目标 URL 不能指向本地或私有网络"),
        ("http://localhost.localdomain/", "目标 URL 不能指向本地或私有网络"),
        ("http://224.0.0.1/", "目标 URL 不能指向本地或私有网络"),
        ("http://[ff02::1]/", "目标 URL 不能指向本地或私有网络"),
    ],
)
def test_validate_public_target_url_rejects_local_and_multicast_variants(url, expected_message):
    assert server._validate_public_target_url(url) == expected_message


@pytest.mark.parametrize(
    "url",
    [
        "https://127.org",
        "https://127.com",
        "https://127.example.com",
    ],
)
def test_validate_public_target_url_allows_public_domains_with_numeric_prefix(url):
    assert server._validate_public_target_url(url) is None


def test_mask_sensitive_text_redacts_bearer_and_query_tokens(monkeypatch):
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret-value")
    server.config.reset_runtime_state()

    masked = server._mask_sensitive_text(
        "Bearer sk-secret-value https://example.com?token=abc123&sig=zzz tvly-secret-value fc-secret-value"
    )

    assert "sk-secret-value" not in masked
    assert "tvly-secret-value" not in masked
    assert "fc-secret-value" not in masked
    assert "Bearer ***" in masked
    assert "token=***" in masked
    assert "sig=***" in masked


def test_mask_sensitive_text_redacts_extended_auth_and_code_tokens():
    masked = server._mask_sensitive_text(
        "https://example.com/callback?access_token=abc123&auth_token=def456&code=ghi789#code=jkl012"
    )

    assert "abc123" not in masked
    assert "def456" not in masked
    assert "ghi789" not in masked
    assert "jkl012" not in masked
    assert "access_token=***" in masked
    assert "auth_token=***" in masked
    assert "code=***" in masked


def test_build_doctor_check_masks_sensitive_endpoint_url():
    check = server._build_doctor_check(
        "demo",
        "ok",
        "ok",
        endpoint="https://user:pass@example.com/v1/models?access_token=abc123#code=otp987",
    )

    assert check["endpoint"] == "https://example.com/v1/models?access_token=***#code=***"


def test_build_doctor_check_tolerates_invalid_port_in_endpoint_url():
    check = server._build_doctor_check(
        "demo",
        "ok",
        "ok",
        endpoint="https://example.com:abc/v1/models?access_token=abc123",
    )

    assert check["endpoint"] == "https://example.com:abc/v1/models?access_token=***"


@pytest.mark.asyncio
async def test_probe_web_fetch_uses_single_firecrawl_attempt_in_doctor_mode(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/scrape"): [
            httpx.Response(200, json={"data": {"markdown": ""}}),
            httpx.Response(200, json={"data": {"markdown": "# recovered"}}),
        ],
    }
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: StubAsyncClient(responses, {}, *args, **kwargs),
    )

    result = await server._probe_web_fetch()

    assert result["status"] == "error"
    assert "Firecrawl 返回空 markdown" in result["message"]


@pytest.mark.asyncio
async def test_get_config_info_marks_configured_model_mismatch_as_degraded(monkeypatch):
    monkeypatch.setenv("GROK_MODEL", "missing-model")

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}, {"id": "grok-4-fast"}]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()

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
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()

    assert payload["feature_readiness"]["web_search"]["status"] == "degraded"
    assert "persisted-model" in payload["feature_readiness"]["web_search"]["message"]
    assert any("persisted-model" in item for item in payload["doctor"]["recommendations"])
    grok_check = next(
        check for check in payload["doctor"]["checks"] if check.get("check_id") == "grok_model_selection"
    )
    assert grok_check["status"] == "warning"
    assert "persisted-model" in grok_check["message"]


@pytest.mark.asyncio
async def test_get_config_info_marks_real_search_probe_failure_as_degraded(monkeypatch):
    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
    }
    exceptions = {
        ("POST", "https://api.example.com/v1/chat/completions"): httpx.TimeoutException("timeout"),
    }
    patch_async_client(monkeypatch, responses, exceptions)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert payload["connection_test"]["status"] == "连接成功"
    assert checks["grok_search_probe"]["status"] == "error"
    assert payload["feature_readiness"]["web_search"]["status"] == "degraded"
    assert "真实搜索探针" in payload["feature_readiness"]["web_search"]["message"]


@pytest.mark.asyncio
async def test_get_config_info_marks_web_fetch_as_degraded_when_only_firecrawl_probe_warns(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    responses = {
        ("GET", "https://api.example.com/v1/models"): httpx.Response(
            200,
            json={"data": [{"id": "grok-4.1-fast"}]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"data": {"markdown": ""}},
        ),
    }
    patch_async_client(monkeypatch, responses)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert payload["connection_test"]["status"] == "连接成功"
    assert checks["firecrawl_scrape"]["status"] == "warning"
    assert checks["web_fetch_probe"]["status"] == "error"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"
    assert payload["feature_readiness"]["web_fetch"]["providers"]["verified_path"] is None
    assert payload["feature_readiness"]["web_fetch"]["providers"]["firecrawl"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_get_available_models_cached_reuses_cached_results(monkeypatch):
    calls = {"count": 0}

    async def fake_fetch(api_url, api_key):
        calls["count"] += 1
        return ["grok-4.1-fast", "grok-4.1-mini"]

    monkeypatch.setattr(server, "_fetch_available_models", fake_fetch)

    first = await server._get_available_models_cached("https://api.example.com/v1", "test-key")
    second = await server._get_available_models_cached("https://api.example.com/v1", "test-key")

    assert first == ["grok-4.1-fast", "grok-4.1-mini"]
    assert second == first
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_get_available_models_cached_caches_empty_result_after_failure(monkeypatch):
    calls = {"count": 0}

    async def failing_fetch(api_url, api_key):
        calls["count"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_fetch_available_models", failing_fetch)

    first = await server._get_available_models_cached("https://api.example.com/v1", "test-key")
    second = await server._get_available_models_cached("https://api.example.com/v1", "test-key")

    assert first == []
    assert second == []
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_get_available_models_cached_isolated_by_cache_key(monkeypatch):
    calls = []

    async def fake_fetch(api_url, api_key):
        calls.append((api_url, api_key))
        return [f"model-for-{api_key}"]

    monkeypatch.setattr(server, "_fetch_available_models", fake_fetch)

    first = await server._get_available_models_cached("https://api.example.com/v1", "key-a")
    second = await server._get_available_models_cached("https://api.example.com/v1", "key-b")

    assert first == ["model-for-key-a"]
    assert second == ["model-for-key-b"]
    assert calls == [
        ("https://api.example.com/v1", "key-a"),
        ("https://api.example.com/v1", "key-b"),
    ]


@pytest.mark.asyncio
async def test_get_config_info_marks_web_fetch_as_degraded_when_real_probe_fails_after_provider_checks_pass(monkeypatch):
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
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"results": ["https://example.com"]},
        ),
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"data": {"markdown": "ok"}},
        ),
    }
    patch_async_client(monkeypatch, responses)
    async def fake_probe_web_fetch():
        return server._build_doctor_check(
            "web_fetch_probe",
            "error",
            "真实抓取探针失败: Firecrawl: Firecrawl 请求超时",
            error_kind="probe_failed",
        )

    monkeypatch.setattr(server, "_probe_web_fetch", fake_probe_web_fetch)

    payload = await load_config_info()
    checks = doctor_checks(payload)

    assert checks["tavily_extract"]["status"] == "ok"
    assert checks["firecrawl_scrape"]["status"] == "ok"
    assert checks["web_fetch_probe"]["status"] == "error"
    assert payload["feature_readiness"]["web_fetch"]["status"] == "degraded"
    assert "真实抓取探针失败" in payload["feature_readiness"]["web_fetch"]["message"]
    assert payload["feature_readiness"]["web_fetch"]["providers"]["verified_path"] is None


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
        "model": server.config.grok_model,
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
async def test_web_search_accepts_finance_topic_for_tavily_search(monkeypatch):
    captured = {}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        captured["topic"] = kwargs["topic"]
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search(
        "test query",
        topic="finance",
        extra_sources=1,
    )

    assert result["status"] == "ok"
    assert result["effective_params"]["topic"] == "finance"
    assert captured["topic"] == "finance"


@pytest.mark.asyncio
async def test_web_search_normalizes_time_range_alias_for_tavily_search(monkeypatch):
    captured = {}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer"

    async def fake_tavily(query, max_results, **kwargs):
        captured["time_range"] = kwargs["time_range"]
        return [{"title": "Tavily", "url": "https://tavily.example.com", "content": "t"}]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server.web_search(
        "test query",
        topic="news",
        time_range="w",
        extra_sources=1,
    )

    assert result["status"] == "ok"
    assert result["effective_params"]["time_range"] == "week"
    assert captured["time_range"] == "week"


@pytest.mark.asyncio
async def test_web_search_sets_time_context_hint_for_auto_mode_when_controls_require_recency(monkeypatch):
    captured = {}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            self.time_context_required = False

        async def search(self, query, platform):
            captured["time_context_required"] = self.time_context_required
            return "Search answer"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "auto")

    result = await server.web_search(
        "OpenAI release notes",
        topic="news",
        time_range="week",
    )

    assert result["status"] in {"ok", "partial"}
    assert captured["time_context_required"] is True


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
@pytest.mark.parametrize(
    ("query", "kwargs", "expected_substring", "expected_effective"),
    [
        ("   ", {}, "query 不能为空", {"topic": "general", "time_range": None}),
        (
            "test query",
            {"time_range": "hour"},
            "time_range 仅支持 day、week、month、year（或 d、w、m、y）",
            {"time_range": "hour"},
        ),
        (
            "test query",
            {"include_domains": ["openai.com", 123]},
            "include_domains 和 exclude_domains 仅支持非空字符串",
            {},
        ),
        (
            "test query",
            {"exclude_domains": ["https://mirror.example.com/path"]},
            "include_domains 和 exclude_domains 仅支持合法域名",
            {},
        ),
        (
            "test query",
            {"include_domains": ["example .com"]},
            "include_domains 和 exclude_domains 仅支持合法域名",
            {},
        ),
        (
            "test query",
            {"include_domains": ["openai.com."], "exclude_domains": ["openai.com"]},
            "同时出现在 include_domains 与 exclude_domains",
            {},
        ),
        ("test query", {"extra_sources": -1}, "extra_sources 不能为负数", {}),
        ("test query", {"extra_sources": True}, "extra_sources 仅支持整数", {}),
        ("test query", {"extra_sources": 1.5}, "extra_sources 仅支持整数", {}),
    ],
)
async def test_web_search_rejects_invalid_inputs(query, kwargs, expected_substring, expected_effective):
    result = await server.web_search(query, **kwargs)

    assert result["status"] == "error"
    assert result["error"] == "validation_error"
    assert expected_substring in result["content"]
    assert result["sources_count"] == 0
    for key, value in expected_effective.items():
        assert result["effective_params"][key] == value


@pytest.mark.asyncio
async def test_web_search_rejects_unknown_explicit_model(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            raise AssertionError("provider should not be created for an invalid explicit model")

    async def fake_models(api_url, api_key):
        return ["grok-4.1-fast", "grok-4-fast"]

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)

    result = await server.web_search("test query", model="missing-model")

    assert result["status"] == "error"
    assert result["error"] == "invalid_model"
    assert "无效模型" in result["content"]
    assert "missing-model" in result["content"]
    assert result["sources_count"] == 0


@pytest.mark.asyncio
async def test_web_search_normalizes_openrouter_explicit_model_before_validation(monkeypatch):
    captured = {}

    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            captured["model"] = model

        async def search(self, query, platform):
            return "Search answer"

    async def fake_models(api_url, api_key):
        return ["openai/gpt-4.1:online"]

    monkeypatch.setenv("GROK_API_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)

    result = await server.web_search("test query", model="openai/gpt-4.1")

    assert result["status"] == "ok"
    assert result["error"] is None
    assert result["effective_params"]["model"] == "openai/gpt-4.1:online"
    assert captured["model"] == "openai/gpt-4.1:online"


@pytest.mark.asyncio
async def test_get_sources_returns_missing_session_error():
    result = await server.get_sources("missing-session")

    assert result == {
        "session_id": "missing-session",
        "sources": [],
        "sources_count": 0,
        "error": "session_id_not_found_or_expired",
    }


@pytest.mark.asyncio
async def test_get_sources_marks_failed_search_session_as_unavailable():
    result = await server.web_search("   ")

    cached = await server.get_sources(result["session_id"])

    assert cached["sources"] == []
    assert cached["sources_count"] == 0
    assert cached["search_status"] == "error"
    assert cached["search_error"] == "validation_error"
    assert cached["source_state"] == "unavailable_due_to_search_error"


@pytest.mark.asyncio
async def test_get_sources_distinguishes_successful_empty_source_sessions(monkeypatch):
    class DummyProvider:
        def __init__(self, api_url, api_key, model):
            pass

        async def search(self, query, platform):
            return "Search answer without citations"

    monkeypatch.setattr(server, "GrokSearchProvider", DummyProvider)

    result = await server.web_search("test query")
    cached = await server.get_sources(result["session_id"])

    assert cached["sources"] == []
    assert cached["sources_count"] == 0
    assert cached["search_status"] == "ok"
    assert cached["search_error"] is None
    assert cached["source_state"] == "empty"


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


@pytest.mark.asyncio
async def test_get_sources_migrates_legacy_error_cache_to_unavailable_state():
    session_id = "legacy-error-session"
    await server._SOURCES_CACHE.set(
        session_id,
        {
            "sources": [],
            "search_status": "error",
            "search_error": "validation_error",
        },
    )

    cached = await server.get_sources(session_id)

    assert cached["sources"] == []
    assert cached["sources_count"] == 0
    assert cached["search_status"] == "error"
    assert cached["search_error"] == "validation_error"
    assert cached["source_state"] == "unavailable_due_to_search_error"


@pytest.mark.asyncio
async def test_get_sources_does_not_keep_available_state_for_invalid_legacy_urls():
    session_id = "legacy-invalid-session"
    await server._SOURCES_CACHE.set(
        session_id,
        {
            "sources": [{"title": "Bad Source", "url": "not-a-valid-url"}],
            "search_status": "ok",
            "search_error": None,
            "source_state": "available",
        },
    )

    cached = await server.get_sources(session_id)

    assert cached["sources"] == []
    assert cached["sources_count"] == 0
    assert cached["source_state"] == "empty"


def test_probably_truncated_content_detects_obvious_markers():
    assert server._is_probably_truncated_content("Partial answer [...]", min_length=10) is True
    assert server._is_probably_truncated_content("```\ncode block never closes", min_length=10) is True
    assert server._is_probably_truncated_content("A complete sentence.\n\nAnother paragraph.", min_length=10) is False


def test_extract_firecrawl_markdown_payload_falls_back_to_top_level_markdown_when_nested_is_blank():
    assert server._extract_firecrawl_markdown_payload(
        {
            "data": {"markdown": ""},
            "markdown": "# top level markdown",
        }
    ) == "# top level markdown"


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


def test_main_exits_zero_on_keyboard_interrupt(monkeypatch):
    import os
    import signal

    def fake_run(**kwargs):
        raise KeyboardInterrupt

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 0


def test_main_exits_nonzero_on_unexpected_runtime_error(monkeypatch):
    import os
    import signal

    def fake_run(**kwargs):
        raise RuntimeError("boom")

    def fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "_exit", fake_exit)

    with pytest.raises(SystemExit) as exc:
        server.main()

    assert exc.value.code == 1


@pytest.mark.asyncio
async def test_switch_model_persists_to_temp_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GROK_MODEL", raising=False)
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(server.config, "_config_file", config_file, raising=False)
    server.config.reset_runtime_state()

    payload = json.loads(await server.switch_model("grok-4.1-mini"))

    assert payload["status"] == "成功"
    assert payload["current_model"] == "grok-4.1-mini"
    assert payload["config_file"] == str(config_file)
    assert json.loads(config_file.read_text(encoding="utf-8"))["model"] == "grok-4.1-mini"


@pytest.mark.asyncio
async def test_switch_model_tool_keeps_env_model_active_in_current_process(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_MODEL", "env-model")
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(server.config, "_config_file", config_file, raising=False)
    server.config.reset_runtime_state()

    payload = json.loads(await server.switch_model("persisted-model"))

    assert payload["status"] == "成功"
    assert payload["previous_model"] == "env-model"
    assert payload["current_model"] == "env-model"
    assert json.loads(config_file.read_text(encoding="utf-8"))["model"] == "persisted-model"


@pytest.mark.asyncio
async def test_switch_model_returns_stable_failure_when_save_fails(monkeypatch):
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(server.config, "_save_config_file", lambda data: (_ for _ in ()).throw(ValueError("disk full")))
    server.config.reset_runtime_state()

    payload = json.loads(await server.switch_model("grok-4.1-mini"))

    assert payload["status"] == "失败"
    assert "disk full" in payload["message"]


@pytest.mark.asyncio
async def test_toggle_builtin_tools_updates_project_settings_file(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    git_root.mkdir()
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)

    status = json.loads(await server.toggle_builtin_tools("status"))
    enabled = json.loads(await server.toggle_builtin_tools("on"))
    disabled = json.loads(await server.toggle_builtin_tools("off"))

    assert status["blocked"] is False
    assert enabled["blocked"] is True
    assert sorted(enabled["deny_list"]) == ["WebFetch", "WebSearch"]
    assert disabled["blocked"] is False
    assert disabled["deny_list"] == []
    assert (git_root / ".claude" / "settings.json").exists()


@pytest.mark.asyncio
async def test_toggle_builtin_tools_preserves_unrelated_deny_entries(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    settings_path = git_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"permissions": {"deny": ["OtherTool"]}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)

    enabled = json.loads(await server.toggle_builtin_tools("on"))
    disabled = json.loads(await server.toggle_builtin_tools("off"))

    assert sorted(enabled["deny_list"]) == ["OtherTool", "WebFetch", "WebSearch"]
    assert disabled["deny_list"] == ["OtherTool"]


@pytest.mark.asyncio
async def test_toggle_builtin_tools_returns_stable_error_for_invalid_settings_json(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    settings_path = git_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)

    payload = json.loads(await server.toggle_builtin_tools("status"))

    assert payload["blocked"] is False
    assert payload["deny_list"] == []
    assert payload["error"] == "settings_file_invalid"
    assert "无法读取" in payload["message"]


@pytest.mark.asyncio
async def test_toggle_builtin_tools_returns_stable_error_for_non_list_deny(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    settings_path = git_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"permissions": {"deny": "WebSearch"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)

    payload = json.loads(await server.toggle_builtin_tools("on"))

    assert payload["blocked"] is False
    assert payload["deny_list"] == []
    assert payload["error"] == "settings_file_invalid"
    assert "deny" in payload["message"]


@pytest.mark.asyncio
async def test_toggle_builtin_tools_returns_stable_error_when_write_fails(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    git_root.mkdir()
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)
    original_dump = json.dump

    def failing_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", failing_dump)
    try:
        payload = json.loads(await server.toggle_builtin_tools("on"))
    finally:
        monkeypatch.setattr(json, "dump", original_dump)

    assert payload["blocked"] is False
    assert payload["deny_list"] == []
    assert payload["error"] == "settings_write_failed"
    assert "disk full" in payload["message"]


@pytest.mark.asyncio
async def test_toggle_builtin_tools_returns_stable_error_when_git_root_is_missing(monkeypatch):
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: None)

    payload = json.loads(await server.toggle_builtin_tools("status"))

    assert payload["blocked"] is False
    assert payload["deny_list"] == []
    assert payload["error"] == "git_root_not_found"


@pytest.mark.asyncio
async def test_toggle_builtin_tools_rejects_unknown_action(monkeypatch, tmp_path):
    git_root = tmp_path / "repo"
    git_root.mkdir()
    monkeypatch.setattr(server, "_find_git_root", lambda start=None: git_root)

    payload = json.loads(await server.toggle_builtin_tools("garbage"))

    assert payload["blocked"] is False
    assert payload["deny_list"] == []
    assert payload["error"] == "invalid_action"
    assert "status" in payload["message"]


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
async def test_web_fetch_rejects_loopback_target_before_provider_calls(monkeypatch):
    calls = {"tavily": 0, "firecrawl": 0}

    async def fake_tavily(url):
        calls["tavily"] += 1
        return "# Tavily", None

    async def fake_firecrawl(url, ctx):
        calls["firecrawl"] += 1
        return "# Firecrawl", None

    monkeypatch.setattr(server, "_call_tavily_extract", fake_tavily)
    monkeypatch.setattr(server, "_call_firecrawl_scrape", fake_firecrawl)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    result = await server.web_fetch("http://127.0.0.1/private")

    assert "不能指向本地或私有网络" in result
    assert calls == {"tavily": 0, "firecrawl": 0}


@pytest.mark.asyncio
async def test_web_map_rejects_invalid_scheme_before_provider_calls(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")

    result = await server.web_map("file:///tmp/secret.txt")

    assert "仅支持 http/https URL" in result


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


@pytest.mark.asyncio
async def test_call_tavily_extract_rejects_login_page(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            text="<html><body>Please login to continue</body></html>",
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_tavily_extract("https://example.com")

    assert content is None
    assert "登录页或认证页面" in error


@pytest.mark.asyncio
async def test_call_tavily_extract_reports_empty_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": []},
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_tavily_extract("https://example.com")

    assert content is None
    assert "results 为空" in error


@pytest.mark.asyncio
async def test_call_tavily_extract_reports_truncated_content(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": [{"raw_content": f"{'A' * 140}[...]"}]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_tavily_extract("https://example.com")

    assert content is None
    assert "疑似被截断" in error


@pytest.mark.asyncio
async def test_call_tavily_extract_reports_invalid_response_shape_for_non_dict_payload(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json=["unexpected"],
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_tavily_extract("https://example.com")

    assert content is None
    assert "响应结构异常" in error
    assert "缺少顶层对象" in error


@pytest.mark.asyncio
async def test_call_tavily_extract_reports_invalid_result_entry_shape(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/extract"): httpx.Response(
            200,
            json={"results": ["unexpected"]},
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_tavily_extract("https://example.com")

    assert content is None
    assert "响应结构异常" in error
    assert "results[0]" in error


@pytest.mark.asyncio
async def test_call_tavily_map_returns_config_error_when_disabled(monkeypatch):
    monkeypatch.setenv("TAVILY_ENABLED", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    result = await server._call_tavily_map("https://example.com")

    assert "配置错误" in result
    assert "TAVILY_ENABLED=false" in result


@pytest.mark.asyncio
async def test_call_tavily_map_returns_config_error_when_key_missing(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    result = await server._call_tavily_map("https://example.com")

    assert "配置错误" in result
    assert "TAVILY_API_KEY 未配置" in result


@pytest.mark.asyncio
async def test_call_tavily_map_surfaces_timeout(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    exceptions = {
        ("POST", "https://api.tavily.com/map"): httpx.TimeoutException("timeout"),
    }
    patch_async_client(monkeypatch, {}, exceptions)

    result = await server._call_tavily_map("https://example.com", timeout=12)

    assert "映射超时" in result
    assert "12秒" in result


@pytest.mark.asyncio
async def test_call_tavily_map_converts_public_timeout_seconds_to_provider_milliseconds(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    captured = {}

    class CapturingAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            response = httpx.Response(200, json={"results": ["https://example.com/docs"]})
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    result = await server._call_tavily_map("https://example.com", timeout=12)

    assert json.loads(result)["results"] == ["https://example.com/docs"]
    assert captured["json"]["timeout"] == 12000
    assert captured["kwargs"]["timeout"] == 22.0


@pytest.mark.asyncio
async def test_call_tavily_map_surfaces_http_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            502,
            text="bad gateway",
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com")

    assert "HTTP错误" in result
    assert "502" in result


@pytest.mark.asyncio
async def test_call_tavily_map_masks_sensitive_http_error_text(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            502,
            text='{"error":"Bearer tvly-test token=abc123"}',
            headers={"content-type": "application/json"},
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com")

    assert "tvly-test" not in result
    assert "abc123" not in result
    assert "Bearer ***" in result
    assert "token=***" in result


@pytest.mark.asyncio
async def test_call_tavily_map_rejects_login_html_response(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            text="<html><body>Please login to continue</body></html>",
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com")

    assert "映射失败" in result
    assert "登录页或认证页面" in result


@pytest.mark.asyncio
async def test_call_tavily_map_reports_invalid_json_response(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            text="not-json",
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com")

    assert "映射失败" in result
    assert "非法 JSON" in result


@pytest.mark.asyncio
async def test_call_tavily_map_reports_invalid_shape(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={"base_url": "https://example.com"},
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com")

    assert "映射失败" in result
    assert "缺少 results 列表" in result


@pytest.mark.asyncio
async def test_call_tavily_map_returns_serialized_result(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    responses = {
        ("POST", "https://api.tavily.com/map"): httpx.Response(
            200,
            json={
                "base_url": "https://example.com",
                "results": ["https://example.com/docs"],
                "response_time": 1.5,
            },
        ),
    }
    patch_async_client(monkeypatch, responses)

    result = await server._call_tavily_map("https://example.com", instructions="only docs")

    assert json.loads(result) == {
        "base_url": "https://example.com",
        "results": ["https://example.com/docs"],
        "response_time": 1.5,
    }


@pytest.mark.asyncio
async def test_call_firecrawl_scrape_accepts_top_level_markdown_shape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/scrape"): httpx.Response(
            200,
            json={"markdown": "# flat markdown"},
        ),
    }
    patch_async_client(monkeypatch, responses)

    content, error = await server._call_firecrawl_scrape("https://example.com")

    assert error is None
    assert content == "# flat markdown"


@pytest.mark.asyncio
async def test_call_firecrawl_scrape_retries_empty_markdown_then_succeeds(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/scrape"): [
            httpx.Response(200, json={"data": {"markdown": ""}}),
            httpx.Response(200, json={"data": {"markdown": "# recovered"}}),
        ],
    }
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: StubAsyncClient(responses, {}, *args, **kwargs),
    )

    content, error = await server._call_firecrawl_scrape("https://example.com")

    assert error is None
    assert content == "# recovered"


@pytest.mark.asyncio
async def test_call_firecrawl_scrape_returns_truncated_error_after_retry_budget_exhausted(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("GROK_RETRY_MAX_ATTEMPTS", "2")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/scrape"): [
            httpx.Response(200, json={"data": {"markdown": f"{'A' * 140}[...]"}}),
            httpx.Response(200, json={"data": {"markdown": f"{'B' * 140}[...]"}}),
        ],
    }
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: StubAsyncClient(responses, {}, *args, **kwargs),
    )

    content, error = await server._call_firecrawl_scrape("https://example.com")

    assert content is None
    assert "markdown 疑似被截断" in error


@pytest.mark.asyncio
async def test_call_firecrawl_search_accepts_flat_web_shape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/search"): httpx.Response(
            200,
            json={
                "web": [
                    {
                        "title": "Flat Result",
                        "url": "https://example.com/flat",
                        "description": "Flat response shape",
                    }
                ]
            },
        ),
    }
    patch_async_client(monkeypatch, responses)

    results = await server._call_firecrawl_search("test query", limit=3)

    assert results == [
        {
            "title": "Flat Result",
            "url": "https://example.com/flat",
            "description": "Flat response shape",
        }
    ]


@pytest.mark.asyncio
async def test_call_firecrawl_search_accepts_nested_web_shape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/search"): httpx.Response(
            200,
            json={
                "data": {
                    "web": [
                        {
                            "title": "Nested Result",
                            "url": "https://example.com/nested",
                            "description": "Nested response shape",
                        }
                    ]
                }
            },
        ),
    }
    patch_async_client(monkeypatch, responses)

    results = await server._call_firecrawl_search("test query", limit=3)

    assert results == [
        {
            "title": "Nested Result",
            "url": "https://example.com/nested",
            "description": "Nested response shape",
        }
    ]


@pytest.mark.asyncio
async def test_call_firecrawl_search_accepts_results_shape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.com/v2/search"): httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Results Entry",
                        "url": "https://example.com/results",
                        "description": "Results response shape",
                    }
                ]
            },
        ),
    }
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://api.firecrawl.com/v2")
    patch_async_client(monkeypatch, responses)

    results = await server._call_firecrawl_search("test query", limit=3)

    assert results == [
        {
            "title": "Results Entry",
            "url": "https://example.com/results",
            "description": "Results response shape",
        }
    ]


@pytest.mark.asyncio
async def test_call_firecrawl_search_prefers_non_empty_results_when_web_list_is_empty(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    responses = {
        ("POST", "https://api.firecrawl.dev/v2/search"): httpx.Response(
            200,
            json={
                "web": [],
                "results": [
                    {
                        "title": "Fallback Result",
                        "url": "https://example.com/fallback",
                        "description": "Fallback results shape",
                    }
                ],
            },
        ),
    }
    patch_async_client(monkeypatch, responses)

    results = await server._call_firecrawl_search("test query", limit=3)

    assert results == [
        {
            "title": "Fallback Result",
            "url": "https://example.com/fallback",
            "description": "Fallback results shape",
        }
    ]


@pytest.mark.asyncio
async def test_call_firecrawl_search_clamps_limit_to_provider_max(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    captured = {}

    class CapturingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            response = httpx.Response(200, json={"results": []})
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    results = await server._call_firecrawl_search("test query", limit=150)

    assert results == []
    assert captured["json"]["limit"] == 100


@pytest.mark.asyncio
async def test_call_tavily_search_clamps_max_results_to_provider_limit(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_ENABLED", "true")
    captured = {}

    class CapturingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            response = httpx.Response(200, json={"results": []})
            response.request = httpx.Request("POST", url, headers=headers, json=json)
            return response

    monkeypatch.setattr(httpx, "AsyncClient", CapturingAsyncClient)

    results = await server._call_tavily_search("test query", max_results=25, topic="finance")

    assert results == []
    assert captured["json"]["max_results"] == 20
    assert captured["json"]["topic"] == "finance"
