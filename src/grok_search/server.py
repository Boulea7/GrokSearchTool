import asyncio
import re
import sys
from pathlib import Path
from typing import Annotated, Optional

from fastmcp import FastMCP, Context
from pydantic import Field, ValidationError

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

# 尝试使用绝对导入（支持 mcp run）
try:
    from grok_search.providers.grok import GrokSearchProvider
    from grok_search.logger import log_info
    from grok_search.config import config
    from grok_search.sources import (
        SourcesCache,
        extract_sources_from_text,
        merge_sources,
        new_session_id,
        sanitize_answer_text,
        split_answer_and_sources,
        standardize_sources,
    )
    from grok_search.planning import (
        ComplexityOutput,
        ExecutionOrderOutput,
        IntentOutput,
        PHASE_NAMES,
        SearchTerm,
        StrategyOutput,
        SubQuery,
        ToolPlanItem,
        engine as planning_engine,
        _split_csv,
    )
except ImportError:
    from .providers.grok import GrokSearchProvider
    from .logger import log_info
    from .config import config
    from .sources import (
        SourcesCache,
        extract_sources_from_text,
        merge_sources,
        new_session_id,
        sanitize_answer_text,
        split_answer_and_sources,
        standardize_sources,
    )
    from .planning import (
        ComplexityOutput,
        ExecutionOrderOutput,
        IntentOutput,
        PHASE_NAMES,
        SearchTerm,
        StrategyOutput,
        SubQuery,
        ToolPlanItem,
        engine as planning_engine,
        _split_csv,
    )

mcp = FastMCP("grok-search")

_SOURCES_CACHE = SourcesCache(max_size=256)
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], list[str]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()


async def _fetch_available_models(api_url: str, api_key: str) -> list[str]:
    import httpx

    models_url = f"{api_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            models_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    models: list[str] = []
    for item in (data or {}).get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    return models


async def _get_available_models_cached(api_url: str, api_key: str) -> list[str]:
    key = (api_url, api_key)
    async with _AVAILABLE_MODELS_LOCK:
        if key in _AVAILABLE_MODELS_CACHE:
            return _AVAILABLE_MODELS_CACHE[key]

    try:
        models = await _fetch_available_models(api_url, api_key)
    except Exception:
        models = []

    async with _AVAILABLE_MODELS_LOCK:
        _AVAILABLE_MODELS_CACHE[key] = models
    return models


def _planning_session_error(session_id: str) -> str:
    import json

    return json.dumps(
        {
            "error": "session_not_found",
            "message": f"Session '{session_id}' not found. Call plan_intent first.",
            "expected_phase_order": [
                "intent_analysis",
                "complexity_assessment",
                "query_decomposition",
                "search_strategy",
                "tool_selection",
                "execution_order",
            ],
            "restart_from_intent_analysis": True,
        },
        ensure_ascii=False,
        indent=2,
    )


def _planning_validation_error(code: str, message: str, details: list | None = None) -> str:
    import json

    payload = {"error": code, "message": message}
    if details:
        payload["details"] = details
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_validation_details(exc: ValidationError) -> list[dict]:
    return [
        {
            "field": ".".join(str(part) for part in err["loc"]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]


def _extract_request_id(headers) -> str:
    if not headers:
        return ""

    return (
        headers.get("x-oneapi-request-id", "")
        or headers.get("x-request-id", "")
        or headers.get("request-id", "")
    ).strip()


def _extract_error_summary(response) -> str:
    if response is None:
        return ""

    try:
        data = response.json()
    except Exception:
        data = None

    if isinstance(data, dict):
        error = data.get("error", {})
        if isinstance(error, dict):
            message = (error.get("message") or "").strip()
            if message:
                return message

    body_text = (getattr(response, "text", "") or "").strip()
    if not body_text:
        return ""

    normalized = body_text.lower()
    if "<html" in normalized and "bad gateway" in normalized:
        return "html_5xx_page"
    if "<html" in normalized and _looks_like_login_page(body_text):
        return "login_page"

    snippet = body_text[:180].replace("\n", " ").strip()
    return snippet


def _format_grok_error(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "搜索失败: 上游请求超时，请稍后重试"

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        location = exc.response.headers.get("location", "").strip()
        request_id = _extract_request_id(exc.response.headers)
        summary = _extract_error_summary(exc.response)
        if status_code in {301, 302, 303, 307, 308} and location:
            message = f"搜索失败: 上游返回 HTTP {status_code} 重定向到 {location}，请检查代理认证状态"
        else:
            message = f"搜索失败: 上游返回 HTTP {status_code}"
        if summary:
            message += f"，摘要={summary}"
        if request_id:
            message += f"，request_id={request_id}"
        return message

    message = str(exc).strip()
    if message:
        return f"搜索失败: {message}"
    return "搜索失败: 上游请求异常"


def _looks_like_login_page(body_text: str) -> bool:
    normalized = (body_text or "").strip().lower()
    if "<html" not in normalized:
        return False
    return any(token in normalized for token in ("login", "sign in", "signin", "auth"))


def _is_probably_truncated_content(content: str, min_length: int = 120) -> bool:
    stripped = (content or "").strip()
    if len(stripped) < min_length:
        return False

    lowered = stripped.lower()
    markers = (
        "[...]",
        "[truncated]",
        "(truncated)",
        "<truncated>",
        "output truncated",
        "content truncated",
    )
    if any(marker in lowered for marker in markers):
        return True

    if stripped.count("```") % 2 == 1:
        return True

    if re.search(r"\[[^\]]*\]\([^)]*$", stripped):
        return True

    return False


def _format_fetch_error(provider: str, exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return f"{provider} 请求超时"

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        location = exc.response.headers.get("location", "").strip()
        request_id = _extract_request_id(exc.response.headers)
        summary = _extract_error_summary(exc.response)
        if status_code in {301, 302, 303, 307, 308} and location:
            message = f"{provider} 返回 HTTP {status_code} 重定向到 {location}，请检查认证状态"
        elif status_code in {401, 403}:
            message = f"{provider} 返回 HTTP {status_code}，请检查认证状态"
        else:
            message = f"{provider} 返回 HTTP {status_code}"
        if summary:
            message += f"，摘要={summary}"
        if request_id:
            message += f"，request_id={request_id}"
        return message

    message = str(exc).strip()
    if message:
        return f"{provider} 请求失败: {message}"
    return f"{provider} 请求失败"


def _extra_results_to_sources(
    tavily_results: list[dict] | None,
    firecrawl_results: list[dict] | None,
) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()

    if firecrawl_results:
        for r in firecrawl_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "firecrawl"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            desc = (r.get("description") or "").strip()
            if desc:
                item["description"] = desc
            sources.append(item)

    if tavily_results:
        for r in tavily_results:
            url = (r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            item: dict = {"url": url, "provider": "tavily"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            content = (r.get("content") or "").strip()
            if content:
                item["description"] = content
            score = r.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                item["score"] = score
            sources.append(item)

    return sources


_VALID_SEARCH_TOPICS = {"general", "news"}
_VALID_TIME_RANGES = {"day", "week", "month", "year"}


def _normalize_domain_list(domains: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    for item in domains or []:
        if not isinstance(item, str):
            continue
        domain = item.strip().lower()
        if not domain:
            continue
        if domain not in normalized:
            normalized.append(domain)
    return normalized


def _build_search_response(
    session_id: str,
    content: str,
    sources_count: int,
    *,
    status: str,
    effective_params: dict,
    warnings: Optional[list[str]] = None,
    error: Optional[str] = None,
) -> dict:
    return {
        "session_id": session_id,
        "content": content,
        "sources_count": sources_count,
        "status": status,
        "effective_params": effective_params,
        "warnings": warnings or [],
        "error": error,
    }


def _validate_search_inputs(
    query: str,
    topic: str,
    time_range: str,
    include_domains: Optional[list[str]],
    exclude_domains: Optional[list[str]],
) -> tuple[dict, str | None]:
    normalized_query = query.strip()
    normalized_topic = (topic or "general").strip() or "general"
    normalized_time_range = (time_range or "").strip() or None
    normalized_include_domains = _normalize_domain_list(include_domains)
    normalized_exclude_domains = _normalize_domain_list(exclude_domains)

    effective_params = {
        "query": normalized_query,
        "topic": normalized_topic,
        "time_range": normalized_time_range,
        "include_domains": normalized_include_domains,
        "exclude_domains": normalized_exclude_domains,
    }

    if not normalized_query:
        return effective_params, "搜索失败: query 不能为空"

    if normalized_topic not in _VALID_SEARCH_TOPICS:
        return effective_params, "搜索失败: topic 仅支持 general 或 news"

    if normalized_time_range and normalized_time_range not in _VALID_TIME_RANGES:
        return effective_params, "搜索失败: time_range 仅支持 day、week、month、year"

    overlap = sorted(set(normalized_include_domains) & set(normalized_exclude_domains))
    if overlap:
        overlap_text = ", ".join(overlap)
        return effective_params, (
            "搜索失败: 以下域名同时出现在 include_domains 与 exclude_domains 中: "
            f"{overlap_text}"
        )

    return effective_params, None


@mcp.tool(
    name="web_search",
    output_schema=None,
    description="""
    Before using this tool, please use the plan_intent tool to plan the search carefully.
    Performs a deep web search based on the given query and returns Grok's answer directly.

    This tool extracts sources if provided by upstream, caches them, and returns:
    - session_id: string (When you feel confused or curious about the main content, use this field to invoke the get_sources tool to obtain the corresponding list of information sources)
    - content: string (answer only)
    - sources_count: int
    """,
    meta={"version": "2.0.0", "author": "guda.studio"},
)
async def web_search(
    query: Annotated[str, "Clear, self-contained natural-language search query."],
    platform: Annotated[str, "Target platform to focus on (e.g., 'Twitter', 'GitHub', 'Reddit'). Leave empty for general web search."] = "",
    model: Annotated[str, "Optional model ID for this request only. This value is used ONLY when user explicitly provided."] = "",
    extra_sources: Annotated[int, "Number of additional reference results from Tavily/Firecrawl. Set 0 to disable. Default 0."] = 0,
    topic: Annotated[str, "Optional search topic: general | news."] = "general",
    time_range: Annotated[str, "Optional freshness filter: day | week | month | year."] = "",
    include_domains: Annotated[Optional[list[str]], "Optional domain allowlist for supplemental Tavily search."] = None,
    exclude_domains: Annotated[Optional[list[str]], "Optional domain denylist for supplemental Tavily search."] = None,
) -> dict:
    session_id = new_session_id()
    validated_params, validation_error = _validate_search_inputs(
        query=query,
        topic=topic,
        time_range=time_range,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
    )
    effective_params = {
        "platform": platform,
        "topic": validated_params["topic"],
        "time_range": validated_params["time_range"],
        "include_domains": validated_params["include_domains"],
        "exclude_domains": validated_params["exclude_domains"],
        "model": model,
        "extra_sources": extra_sources,
    }

    if validation_error:
        await _SOURCES_CACHE.set(session_id, [])
        return _build_search_response(
            session_id,
            validation_error,
            0,
            status="error",
            effective_params=effective_params,
            error="validation_error",
        )

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
    except ValueError as e:
        await _SOURCES_CACHE.set(session_id, [])
        return _build_search_response(
            session_id,
            f"配置错误: {str(e)}",
            0,
            status="error",
            effective_params=effective_params,
            error="config_error",
        )

    effective_model = config.grok_model
    if model:
        available = await _get_available_models_cached(api_url, api_key)
        if available and model not in available:
            await _SOURCES_CACHE.set(session_id, [])
            return _build_search_response(
                session_id,
                f"无效模型: {model}",
                0,
                status="error",
                effective_params=effective_params,
                error="invalid_model",
            )
        effective_model = model

    grok_provider = GrokSearchProvider(api_url, api_key, effective_model)
    warnings: list[str] = []

    # 计算额外信源配额
    has_tavily = config.tavily_enabled and bool(config.tavily_api_key)
    has_firecrawl = bool(config.firecrawl_api_key)
    needs_tavily_controls = (
        effective_params["topic"] != "general"
        or bool(effective_params["time_range"])
        or bool(effective_params["include_domains"])
        or bool(effective_params["exclude_domains"])
    )
    firecrawl_count = 0
    tavily_count = 0
    if extra_sources > 0:
        if has_tavily and needs_tavily_controls:
            tavily_count = extra_sources
            firecrawl_count = 0
        elif has_firecrawl and has_tavily:
            firecrawl_count = max(1, round(extra_sources * 0.7))
            firecrawl_count = min(firecrawl_count, extra_sources - 1) if extra_sources > 1 else extra_sources
            tavily_count = extra_sources - firecrawl_count
        elif has_firecrawl:
            firecrawl_count = extra_sources
        elif has_tavily:
            tavily_count = extra_sources

    # 并行执行搜索任务
    if needs_tavily_controls and tavily_count == 0:
        if not has_tavily:
            if effective_params["topic"] != "general":
                warnings.append("topic_not_applied_without_tavily")
            if effective_params["include_domains"] or effective_params["exclude_domains"]:
                warnings.append("domain_controls_not_applied_without_tavily")
            if effective_params["time_range"]:
                warnings.append("time_range_not_applied_without_tavily")
        else:
            if effective_params["topic"] != "general":
                warnings.append("topic_not_applied_without_tavily_search")
            if effective_params["include_domains"] or effective_params["exclude_domains"]:
                warnings.append("domain_controls_not_applied_without_tavily_search")
            if effective_params["time_range"]:
                warnings.append("time_range_not_applied_without_tavily_search")

    async def _safe_grok() -> tuple[str, str | None, str | None]:
        try:
            result = await grok_provider.search(validated_params["query"], platform)
        except Exception as exc:
            return "", _format_grok_error(exc), "upstream_request_failed"
        if not result or not result.strip():
            return "", "搜索失败: 上游返回空响应，请检查模型或代理配置", "upstream_empty_response"
        return result, None, None

    async def _safe_tavily() -> tuple[list[dict] | None, str | None]:
        try:
            if tavily_count:
                results = await _call_tavily_search(
                    validated_params["query"],
                    tavily_count,
                    topic=effective_params["topic"],
                    time_range=effective_params["time_range"],
                    include_domains=effective_params["include_domains"],
                    exclude_domains=effective_params["exclude_domains"],
                )
                if results is None:
                    return None, "tavily_search_unavailable"
                return results, None
        except Exception:
            return None, "tavily_search_unavailable"
        return None, None

    async def _safe_firecrawl() -> tuple[list[dict] | None, str | None]:
        try:
            if firecrawl_count:
                results = await _call_firecrawl_search(validated_params["query"], firecrawl_count)
                if results is None:
                    return None, "firecrawl_search_unavailable"
                return results, None
        except Exception:
            return None, "firecrawl_search_unavailable"
        return None, None

    coros: list = [_safe_grok()]
    if tavily_count > 0:
        coros.append(_safe_tavily())
    if firecrawl_count > 0:
        coros.append(_safe_firecrawl())

    gathered = await asyncio.gather(*coros)

    grok_result, grok_error, grok_error_code = gathered[0]
    tavily_results: list[dict] | None = None
    firecrawl_results: list[dict] | None = None
    idx = 1
    if tavily_count > 0:
        tavily_results, tavily_warning = gathered[idx]
        if tavily_warning:
            warnings.append(tavily_warning)
        idx += 1
    if firecrawl_count > 0:
        firecrawl_results, firecrawl_warning = gathered[idx]
        if firecrawl_warning:
            warnings.append(firecrawl_warning)

    answer, grok_sources = split_answer_and_sources(grok_result)
    if not grok_sources:
        grok_sources = extract_sources_from_text(grok_result)
    extra = _extra_results_to_sources(tavily_results, firecrawl_results)
    all_sources = merge_sources(grok_sources, extra)
    content = answer.strip()
    if not content:
        if grok_error:
            content = grok_error
        elif all_sources:
            content = "搜索成功，但上游只返回了信源列表，未返回正文。可调用 get_sources 查看信源。"
        else:
            content = sanitize_answer_text(grok_result).strip() or "搜索失败: 上游未返回可用正文"

    standardized_sources = standardize_sources(all_sources)
    await _SOURCES_CACHE.set(session_id, standardized_sources)
    status = "ok"
    error = None
    if grok_error:
        status = "error"
        error = grok_error_code or "upstream_request_failed"
    elif warnings:
        status = "partial"

    return _build_search_response(
        session_id,
        content,
        len(standardized_sources),
        status=status,
        effective_params=effective_params,
        warnings=warnings,
        error=error,
    )


@mcp.tool(
    name="get_sources",
    description="""
    When you feel confused or curious about the search response content, use the session_id returned by web_search to invoke the this tool to obtain the corresponding list of information sources.
    Retrieve all cached sources for a previous web_search call.
    Provide the session_id returned by web_search to get the full source list.
    """,
    meta={"version": "1.0.0", "author": "guda.studio"},
)
async def get_sources(
    session_id: Annotated[str, "Session ID from previous web_search call."]
) -> dict:
    sources = await _SOURCES_CACHE.get(session_id)
    if sources is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    standardized_sources = standardize_sources(sources)
    if standardized_sources != sources:
        await _SOURCES_CACHE.set(session_id, standardized_sources)
    return {"session_id": session_id, "sources": standardized_sources, "sources_count": len(standardized_sources)}


async def _call_tavily_extract(url: str) -> tuple[str | None, str | None]:
    import httpx
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not config.tavily_enabled or not api_key:
        return None, None
    endpoint = f"{api_url.rstrip('/')}/extract"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"urls": [url], "format": "markdown"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            if _looks_like_login_page(response.text):
                return None, "Tavily 返回登录页或认证页面，请检查代理认证状态"
            data = response.json()
            if data.get("results") and len(data["results"]) > 0:
                content = data["results"][0].get("raw_content", "")
                if content and content.strip():
                    if _is_probably_truncated_content(content):
                        return None, "Tavily 提取结果疑似被截断"
                    return content, None
                return None, "Tavily 提取成功但内容为空"
            return None, "Tavily 提取成功但 results 为空"
    except Exception as exc:
        return None, _format_fetch_error("Tavily", exc)


async def _call_tavily_search(
    query: str,
    max_results: int = 6,
    *,
    topic: str = "general",
    time_range: str | None = None,
    include_domains: Optional[list[str]] = None,
    exclude_domains: Optional[list[str]] = None,
) -> list[dict] | None:
    import httpx
    api_key = config.tavily_api_key
    if not config.tavily_enabled or not api_key:
        return None
    endpoint = f"{config.tavily_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_raw_content": False,
        "include_answer": False,
        "topic": topic,
    }
    if time_range:
        body["time_range"] = time_range
    if include_domains:
        body["include_domains"] = include_domains
    if exclude_domains:
        body["exclude_domains"] = exclude_domains
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", ""), "score": r.get("score")}
                for r in results
            ] if results else []
    except Exception:
        return None


async def _call_firecrawl_search(query: str, limit: int = 14) -> list[dict] | None:
    import httpx
    api_key = config.firecrawl_api_key
    if not api_key:
        return None
    endpoint = f"{config.firecrawl_api_url.rstrip('/')}/search"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"query": query, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = data.get("data", {}).get("web", [])
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
                for r in results
            ] if results else []
    except Exception:
        return None


async def _call_firecrawl_scrape(url: str, ctx=None) -> tuple[str | None, str | None]:
    import httpx
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None, None
    endpoint = f"{api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = config.retry_max_attempts
    last_error: str | None = None
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                if _looks_like_login_page(response.text):
                    return None, "Firecrawl 返回登录页或认证页面，请检查代理认证状态"
                data = response.json()
                markdown = data.get("data", {}).get("markdown", "")
                if markdown and markdown.strip():
                    if _is_probably_truncated_content(markdown):
                        last_error = "Firecrawl 返回的 markdown 疑似被截断"
                        await log_info(ctx, f"Firecrawl: markdown疑似截断, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
                        continue
                    return markdown, None
                last_error = "Firecrawl 返回空 markdown"
                await log_info(ctx, f"Firecrawl: markdown为空, 重试 {attempt + 1}/{max_retries}", config.debug_enabled)
        except Exception as e:
            last_error = _format_fetch_error("Firecrawl", e)
            await log_info(ctx, f"Firecrawl error: {e}", config.debug_enabled)
            return None, last_error
    return None, last_error


@mcp.tool(
    name="web_fetch",
    output_schema=None,
    description="""
    Fetches and extracts complete content from a URL, returning it as a structured Markdown document.

    **Key Features:**
        - **Full Content Extraction:** Retrieves and parses all meaningful content (text, images, links, tables, code blocks).
        - **Markdown Conversion:** Converts HTML structure to well-formatted Markdown with preserved hierarchy.
        - **Content Fidelity:** Maintains 100% content fidelity without summarization or modification.

    **Edge Cases & Best Practices:**
        - Ensure URL is complete and accessible (not behind authentication or paywalls).
        - May not capture dynamically loaded content requiring JavaScript execution.
        - Large pages may take longer to process; consider timeout implications.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_fetch(
    url: Annotated[str, "Valid HTTP/HTTPS web address pointing to the target page. Must be complete and accessible."],
    ctx: Context = None
) -> str:
    await log_info(ctx, f"Begin Fetch: {url}", config.debug_enabled)

    tavily_error: str | None = None
    if config.tavily_enabled:
        result, tavily_error = await _call_tavily_extract(url)
        if result:
            await log_info(ctx, "Fetch Finished (Tavily)!", config.debug_enabled)
            return result
        if tavily_error:
            await log_info(ctx, f"Tavily extract failed: {tavily_error}", config.debug_enabled)
    else:
        await log_info(ctx, "Tavily disabled, skipping extract.", config.debug_enabled)

    await log_info(ctx, "Tavily unavailable or failed, trying Firecrawl...", config.debug_enabled)
    result, firecrawl_error = await _call_firecrawl_scrape(url, ctx)
    if result:
        await log_info(ctx, "Fetch Finished (Firecrawl)!", config.debug_enabled)
        return result
    if firecrawl_error:
        await log_info(ctx, f"Firecrawl scrape failed: {firecrawl_error}", config.debug_enabled)

    await log_info(ctx, "Fetch Failed!", config.debug_enabled)
    if not config.tavily_api_key and not config.firecrawl_api_key:
        return "配置错误: TAVILY_API_KEY 和 FIRECRAWL_API_KEY 均未配置"

    errors = [error for error in (tavily_error, firecrawl_error) if error]
    if errors:
        return f"提取失败: {'；'.join(errors)}"
    return "提取失败: 所有提取服务均未能获取内容"


async def _call_tavily_map(url: str, instructions: str = None, max_depth: int = 1,
                           max_breadth: int = 20, limit: int = 50, timeout: int = 150) -> str:
    import httpx
    import json
    api_url = config.tavily_api_url
    api_key = config.tavily_api_key
    if not config.tavily_enabled:
        return "配置错误: TAVILY_ENABLED=false，Tavily map 已禁用"
    if not api_key:
        return "配置错误: TAVILY_API_KEY 未配置，请设置环境变量 TAVILY_API_KEY"
    endpoint = f"{api_url.rstrip('/')}/map"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth, "limit": limit, "timeout": timeout}
    if instructions:
        body["instructions"] = instructions
    try:
        async with httpx.AsyncClient(timeout=float(timeout + 10)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "results": data.get("results", []),
                "response_time": data.get("response_time", 0)
            }, ensure_ascii=False, indent=2)
    except httpx.TimeoutException:
        return f"映射超时: 请求超过{timeout}秒"
    except httpx.HTTPStatusError as e:
        return f"HTTP错误: {e.response.status_code} - {e.response.text[:200]}"
    except Exception as e:
        return f"映射错误: {str(e)}"


@mcp.tool(
    name="web_map",
    description="""
    Maps a website's structure by traversing it like a graph, discovering URLs and generating a comprehensive site map.

    **Key Features:**
        - **Graph Traversal:** Explores website structure starting from root URL.
        - **Depth & Breadth Control:** Configure traversal limits to balance coverage and performance.
        - **Instruction Filtering:** Use natural language to focus crawler on specific content types.

    **Edge Cases & Best Practices:**
        - Start with low max_depth (1-2) for initial exploration, increase if needed.
        - Use instructions to filter for specific content (e.g., "only documentation pages").
        - Large sites may hit timeout limits; adjust timeout and limit parameters accordingly.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def web_map(
    url: Annotated[str, "Root URL to begin the mapping (e.g., 'https://docs.example.com')."],
    instructions: Annotated[str, "Natural language instructions for the crawler to filter or focus on specific content."] = "",
    max_depth: Annotated[int, Field(description="Maximum depth of mapping from the base URL.", ge=1, le=5)] = 1,
    max_breadth: Annotated[int, Field(description="Maximum number of links to follow per page.", ge=1, le=500)] = 20,
    limit: Annotated[int, Field(description="Total number of links to process before stopping.", ge=1, le=500)] = 50,
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150
) -> str:
    result = await _call_tavily_map(url, instructions, max_depth, max_breadth, limit, timeout)
    return result


def _build_doctor_check(
    check_id: str,
    status: str,
    message: str,
    *,
    endpoint: str = "",
    response_time_ms: float | int | None = None,
    skipped_reason: str = "",
    **extra,
) -> dict:
    check = {
        "check_id": check_id,
        "status": status,
        "message": message,
    }
    if endpoint:
        check["endpoint"] = endpoint
    if response_time_ms is not None:
        check["response_time_ms"] = round(float(response_time_ms), 2)
    if skipped_reason:
        check["skipped_reason"] = skipped_reason
    for key, value in extra.items():
        if value is not None:
            check[key] = value
    return check


def _append_recommendation(recommendations: list[str], message: str) -> None:
    if message and message not in recommendations:
        recommendations.append(message)


def _find_git_root(start: Path | None = None) -> Path | None:
    root = (start or Path.cwd()).resolve()
    while True:
        if (root / ".git").exists():
            return root
        if root == root.parent:
            return None
        root = root.parent


def _summarize_doctor_status(doctor_status: str) -> str:
    if doctor_status == "ok":
        return "核心配置与可选依赖探测均正常。"
    if doctor_status == "error":
        return "核心 Grok 配置或连通性存在阻塞问题。"
    return "核心 Grok 可用，但部分可选能力未配置、未生效或探测失败。"


async def _probe_json_endpoint(
    check_id: str,
    method: str,
    url: str,
    headers: dict,
    *,
    json_body: dict | None = None,
    timeout: float = 10.0,
) -> dict:
    import time

    import httpx

    start_time = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=json_body)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        response_text = (response.text or "")[:120]
        try:
            data = response.json()
        except Exception:
            data = None

        if response.status_code >= 400:
            return _build_doctor_check(
                check_id,
                "error",
                f"HTTP {response.status_code}: {response_text}",
                endpoint=url,
                response_time_ms=response_time_ms,
                error_kind="http_error",
                status_code=response.status_code,
            )

        return _build_doctor_check(
            check_id,
            "ok",
            "请求成功",
            endpoint=url,
            response_time_ms=response_time_ms,
            data=data,
            status_code=response.status_code,
        )
    except httpx.TimeoutException as exc:
        return _build_doctor_check(
            check_id,
            "error",
            f"请求超时: {str(exc) or 'timeout'}",
            endpoint=url,
            error_kind="timeout",
        )
    except httpx.RequestError as exc:
        return _build_doctor_check(
            check_id,
            "error",
            f"网络错误: {str(exc)}",
            endpoint=url,
            error_kind="request_error",
        )
    except Exception as exc:
        return _build_doctor_check(
            check_id,
            "error",
            f"未知错误: {str(exc)}",
            endpoint=url,
            error_kind="unexpected_error",
        )


def _build_connection_test_from_models_check(models_check: dict) -> dict:
    status_map = {
        "timeout": "连接超时",
        "request_error": "连接失败",
        "http_error": "连接异常",
        "config_error": "配置错误",
    }
    if models_check["status"] == "ok":
        result = {
            "status": "连接成功",
            "message": models_check["message"],
            "response_time_ms": models_check.get("response_time_ms", 0),
        }
        if models_check.get("available_models"):
            result["available_models"] = models_check["available_models"]
        return result

    return {
        "status": status_map.get(models_check.get("error_kind"), "测试失败"),
        "message": models_check["message"],
        "response_time_ms": models_check.get("response_time_ms", 0),
    }


def _build_feature_readiness(checks: list[dict]) -> dict:
    checks_by_id = {check["check_id"]: check for check in checks}
    grok_config = checks_by_id["grok_config"]
    grok_models = checks_by_id["grok_models"]
    grok_model_selection = checks_by_id.get("grok_model_selection")
    tavily_extract = checks_by_id["tavily_extract"]
    firecrawl_scrape = checks_by_id["firecrawl_scrape"]
    tavily_map = checks_by_id["tavily_map"]
    claude_context = checks_by_id["claude_code_project"]

    if grok_config["status"] != "ok":
        web_search_status = "not_ready"
        web_search_message = grok_config["message"]
    elif grok_models["status"] == "ok":
        web_search_status = "ready"
        web_search_message = "Grok 配置完整，/models 探测成功。"
    else:
        web_search_status = "degraded"
        web_search_message = grok_models["message"]

    if (
        web_search_status == "ready"
        and grok_model_selection
        and grok_model_selection["status"] == "warning"
    ):
        web_search_status = "degraded"
        web_search_message = grok_model_selection["message"]

    if tavily_extract["status"] == "ok" and firecrawl_scrape["status"] == "ok":
        web_fetch_status = "ready"
        web_fetch_message = "Tavily 与 Firecrawl 均可用。"
    elif tavily_extract["status"] == "ok" or firecrawl_scrape["status"] == "ok":
        web_fetch_status = "partial_ready"
        web_fetch_message = "仅部分抓取后端已验证可用。"
    elif tavily_extract["status"] == "skipped" and firecrawl_scrape["status"] == "skipped":
        web_fetch_status = "not_ready"
        web_fetch_message = "Tavily / Firecrawl 均未配置。"
    else:
        web_fetch_status = "degraded"
        web_fetch_message = "抓取后端已配置，但当前探测未通过。"

    if tavily_map["status"] == "ok":
        web_map_status = "ready"
        web_map_message = "Tavily map 探测成功。"
    elif tavily_map["status"] == "error":
        web_map_status = "degraded"
        web_map_message = tavily_map["message"]
    else:
        web_map_status = "not_ready"
        web_map_message = "Tavily 未配置或已禁用。"

    toggle_status = "ready" if claude_context["status"] == "ok" else "not_ready"

    return {
        "web_search": {"status": web_search_status, "message": web_search_message},
        "get_sources": {
            "status": "ready",
            "message": "只依赖本地缓存中的历史 session_id，不依赖当前 Grok 配置。",
        },
        "web_fetch": {"status": web_fetch_status, "message": web_fetch_message},
        "web_map": {"status": web_map_status, "message": web_map_message},
        "toggle_builtin_tools": {
            "status": toggle_status,
            "message": claude_context["message"],
            "client_specific": True,
        },
    }


def _feature_affects_overall_doctor_status(item: dict) -> bool:
    return not item.get("client_specific", False)


def _build_doctor_payload(checks: list[dict], feature_readiness: dict, recommendations: list[str]) -> dict:
    web_search_status = feature_readiness["web_search"]["status"]
    if web_search_status == "not_ready":
        doctor_status = "error"
    elif any(check["status"] in {"error", "warning"} for check in checks) or any(
        item["status"] in {"partial_ready", "degraded", "not_ready"}
        for item in feature_readiness.values()
        if _feature_affects_overall_doctor_status(item)
    ):
        doctor_status = "partial"
    else:
        doctor_status = "ok"

    return {
        "status": doctor_status,
        "summary": _summarize_doctor_status(doctor_status),
        "checks": checks,
        "recommendations": recommendations,
    }


@mcp.tool(
    name="get_config_info",
    output_schema=None,
    description="""
    Returns current Grok Search MCP server configuration and tests API connectivity.

    **Key Features:**
        - **Configuration Check:** Verifies environment variables and current settings.
        - **Doctor Checks:** Runs structured readiness checks for Grok, Tavily, Firecrawl, and Claude-specific routing.
        - **Connection Test:** Sends request to /models endpoint to validate API access.
        - **Model Discovery:** Lists all available models from the API.

    **Edge Cases & Best Practices:**
        - Use this tool first when debugging connection, provider readiness, or installation issues.
        - API keys are automatically masked for security in the response.
        - Optional provider probes only run when their configuration is present.
        - Connection test timeout is 10 seconds; network issues may cause delays.
    """,
    meta={"version": "1.4.0", "author": "guda.studio"},
)
async def get_config_info() -> str:
    import json

    config_info = config.get_config_info()
    checks: list[dict] = []
    recommendations: list[str] = []

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
        checks.append(_build_doctor_check("grok_config", "ok", "Grok 核心配置已提供。"))
    except ValueError as exc:
        api_url = ""
        api_key = ""
        checks.append(_build_doctor_check("grok_config", "error", str(exc), error_kind="config_error"))
        _append_recommendation(recommendations, "先配置 GROK_API_URL 与 GROK_API_KEY，再重新运行 get_config_info。")

    if api_url:
        if api_url.rstrip("/").endswith("/v1"):
            checks.append(_build_doctor_check("grok_api_url_format", "ok", "GROK_API_URL 已显式包含 /v1。"))
        else:
            checks.append(_build_doctor_check("grok_api_url_format", "warning", "GROK_API_URL 未显式包含 /v1。"))
            _append_recommendation(recommendations, "将 GROK_API_URL 改为显式包含 /v1 的 OpenAI-compatible 根路径。")

    if api_url and api_key:
        grok_models = await _probe_json_endpoint(
            "grok_models",
            "GET",
            f"{api_url.rstrip('/')}/models",
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        models_data = grok_models.pop("data", None)
        if grok_models["status"] == "ok":
            model_names = []
            for model in (models_data or {}).get("data", []) or []:
                if isinstance(model, dict) and isinstance(model.get("id"), str):
                    model_names.append(model["id"])
            grok_models["message"] = f"成功获取模型列表，共 {len(model_names)} 个模型。"
            if model_names:
                grok_models["available_models"] = model_names
        else:
            _append_recommendation(recommendations, "检查 Grok 中转站是否支持 /models，并确认 API Key 与 URL 可达。")
    else:
        grok_models = _build_doctor_check(
            "grok_models",
            "error",
            "Grok 核心配置缺失，无法执行 /models 探测。",
            error_kind="config_error",
        )

    checks.append(grok_models)
    if grok_models["status"] == "ok":
        configured_model = config.grok_model
        available_models = grok_models.get("available_models") or []
        if configured_model and available_models and configured_model not in available_models:
            checks.append(
                _build_doctor_check(
                    "grok_model_selection",
                    "warning",
                    f"当前配置模型 {configured_model} 不在 /models 返回列表中。",
                    configured_model=configured_model,
                    available_models=available_models,
                )
            )
            available_preview = ", ".join(available_models[:5])
            _append_recommendation(
                recommendations,
                f"将 GROK_MODEL 或本地持久化模型从 {configured_model} 切换到 /models 返回的可用模型，例如：{available_preview}。",
            )
    if config.tavily_enabled and config.tavily_api_key:
        tavily_extract = await _probe_json_endpoint(
            "tavily_extract",
            "POST",
            f"{config.tavily_api_url.rstrip('/')}/extract",
            {
                "Authorization": f"Bearer {config.tavily_api_key}",
                "Content-Type": "application/json",
            },
            json_body={"urls": ["https://example.com"], "format": "markdown"},
            timeout=10.0,
        )
        tavily_extract_data = tavily_extract.pop("data", None)
        if tavily_extract["status"] == "ok":
            result_count = len((tavily_extract_data or {}).get("results", []) or [])
            tavily_extract["message"] = f"Tavily extract 探测成功，返回 {result_count} 条结果。"
        else:
            _append_recommendation(recommendations, "检查 TAVILY_API_KEY / TAVILY_API_URL，确认 Tavily extract 端点可达。")

        tavily_map = await _probe_json_endpoint(
            "tavily_map",
            "POST",
            f"{config.tavily_api_url.rstrip('/')}/map",
            {
                "Authorization": f"Bearer {config.tavily_api_key}",
                "Content-Type": "application/json",
            },
            json_body={"url": "https://example.com", "max_depth": 1, "max_breadth": 1, "limit": 1, "timeout": 10},
            timeout=10.0,
        )
        tavily_map_data = tavily_map.pop("data", None)
        if tavily_map["status"] == "ok":
            result_count = len((tavily_map_data or {}).get("results", []) or [])
            tavily_map["message"] = f"Tavily map 探测成功，返回 {result_count} 条结果。"
        else:
            _append_recommendation(recommendations, "检查 TAVILY_API_KEY / TAVILY_API_URL，确认 Tavily map 端点可达。")
    else:
        tavily_reason = "TAVILY_ENABLED=false" if not config.tavily_enabled else "TAVILY_API_KEY 未配置"
        tavily_extract = _build_doctor_check(
            "tavily_extract",
            "skipped",
            "未执行 Tavily extract 探测。",
            skipped_reason=tavily_reason,
        )
        tavily_map = _build_doctor_check(
            "tavily_map",
            "skipped",
            "未执行 Tavily map 探测。",
            skipped_reason=tavily_reason,
        )
        _append_recommendation(recommendations, "若需要 web_map 或 Tavily-first web_fetch，请配置并启用 Tavily。")
    checks.append(tavily_extract)
    checks.append(tavily_map)

    if config.firecrawl_api_key:
        firecrawl_scrape = await _probe_json_endpoint(
            "firecrawl_scrape",
            "POST",
            f"{config.firecrawl_api_url.rstrip('/')}/scrape",
            {
                "Authorization": f"Bearer {config.firecrawl_api_key}",
                "Content-Type": "application/json",
            },
            json_body={"url": "https://example.com", "formats": ["markdown"], "timeout": 1000},
            timeout=10.0,
        )
        firecrawl_data = firecrawl_scrape.pop("data", None)
        if firecrawl_scrape["status"] == "ok":
            has_markdown = bool((((firecrawl_data or {}).get("data", {}) or {}).get("markdown", "") or "").strip())
            firecrawl_scrape["message"] = "Firecrawl scrape 探测成功。" if has_markdown else "Firecrawl scrape 已响应，但 markdown 为空。"
        else:
            _append_recommendation(recommendations, "检查 FIRECRAWL_API_KEY / FIRECRAWL_API_URL，确认 Firecrawl scrape 端点可达。")
    else:
        firecrawl_scrape = _build_doctor_check(
            "firecrawl_scrape",
            "skipped",
            "未执行 Firecrawl scrape 探测。",
            skipped_reason="FIRECRAWL_API_KEY 未配置",
        )
        _append_recommendation(recommendations, "若需要 Firecrawl fallback，请配置 FIRECRAWL_API_KEY。")
    checks.append(firecrawl_scrape)

    claude_project_root = _find_git_root()
    claude_context_status = "ok" if claude_project_root else "skipped"
    checks.append(
        _build_doctor_check(
            "claude_code_project",
            claude_context_status,
            f"已找到 Claude Code 项目根目录：{claude_project_root}" if claude_context_status == "ok" else "未检测到项目级 Git 上下文。",
            skipped_reason="" if claude_context_status == "ok" else "missing_git_context",
        )
    )

    feature_readiness = _build_feature_readiness(checks)
    doctor = _build_doctor_payload(checks, feature_readiness, recommendations)
    config_info["connection_test"] = _build_connection_test_from_models_check(grok_models)
    config_info["doctor"] = doctor
    config_info["feature_readiness"] = feature_readiness

    return json.dumps(config_info, ensure_ascii=False, indent=2)


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Switches the default Grok model used for search and fetch operations, persisting the setting.

    **Key Features:**
        - **Model Selection:** Change the AI model for web search and content fetching.
        - **Persistent Storage:** Model preference saved to ~/.config/grok-search/config.json.
        - **Immediate Effect:** New model used for all subsequent operations.

    **Edge Cases & Best Practices:**
        - Use get_config_info to verify available models before switching.
        - Invalid model IDs may cause API errors in subsequent requests.
        - Model changes persist across sessions until explicitly changed again.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def switch_model(
    model: Annotated[str, "Model ID to switch to (e.g., 'grok-4-fast', 'grok-2-latest', 'grok-vision-beta')."]
) -> str:
    import json

    try:
        previous_model = config.grok_model
        config.set_model(model)
        current_model = config.grok_model

        result = {
            "status": "成功",
            "previous_model": previous_model,
            "current_model": current_model,
            "message": f"模型已从 {previous_model} 切换到 {current_model}",
            "config_file": str(config.config_file)
        }

        return json.dumps(result, ensure_ascii=False, indent=2)

    except ValueError as e:
        result = {
            "status": "失败",
            "message": f"切换模型失败: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        result = {
            "status": "失败",
            "message": f"未知错误: {str(e)}"
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    name="toggle_builtin_tools",
    output_schema=None,
    description="""
    Toggle Claude Code's built-in WebSearch and WebFetch tools on/off.

    **Key Features:**
        - **Tool Control:** Enable or disable Claude Code's native web tools.
        - **Project Scope:** Changes apply to current project's .claude/settings.json.
        - **Status Check:** Query current state without making changes.

    **Edge Cases & Best Practices:**
        - Use "on" to block built-in tools when preferring this MCP server's implementation.
        - Use "off" to restore Claude Code's native tools.
        - Use "status" to check current configuration without modification.
    """,
    meta={"version": "1.3.0", "author": "guda.studio"},
)
async def toggle_builtin_tools(
    action: Annotated[str, "Action to perform: 'on' (block built-in), 'off' (allow built-in), or 'status' (check current state)."] = "status"
) -> str:
    import json

    root = _find_git_root()
    if root is None:
        return json.dumps({
            "blocked": False,
            "deny_list": [],
            "file": "",
            "message": "未检测到项目级 Git 根目录，无法修改 Claude Code 项目设置"
        }, ensure_ascii=False, indent=2)

    settings_path = root / ".claude" / "settings.json"
    tools = ["WebFetch", "WebSearch"]

    # Load or initialize
    if settings_path.exists():
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
    else:
        settings = {"permissions": {"deny": []}}

    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    blocked = all(t in deny for t in tools)

    # Execute action
    if action in ["on", "enable"]:
        for t in tools:
            if t not in deny:
                deny.append(t)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已禁用"
        blocked = True
    elif action in ["off", "disable"]:
        deny[:] = [t for t in deny if t not in tools]
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        msg = "官方工具已启用"
        blocked = False
    else:
        msg = f"官方工具当前{'已禁用' if blocked else '已启用'}"

    return json.dumps({
        "blocked": blocked,
        "deny_list": deny,
        "file": str(settings_path),
        "message": msg
    }, ensure_ascii=False, indent=2)


def _get_planning_sub_queries(session) -> list[dict]:
    record = session.phases.get("query_decomposition")
    if not record or not isinstance(record.data, list):
        return []
    return [item for item in record.data if isinstance(item, dict)]


def _get_planning_sub_query_ids(session) -> set[str]:
    return {
        item["id"]
        for item in _get_planning_sub_queries(session)
        if isinstance(item.get("id"), str) and item["id"].strip()
    }


def _planning_validation_message(message: str, field: str | None = None) -> str:
    details = None
    if field:
        details = [{"field": field, "message": message, "type": "value_error"}]
    return _planning_validation_error("validation_error", message, details)


def _validate_sub_query_item(session, item: dict, is_revision: bool) -> str | None:
    existing_ids = _get_planning_sub_query_ids(session)
    sub_query_id = item["id"]
    valid_dependency_ids = {sub_query_id} if is_revision else existing_ids

    if is_revision and any(phase in session.phases for phase in ("search_strategy", "tool_selection", "execution_order")):
        return _planning_validation_message(
            "Sub-query revision would invalidate downstream phases. Restart planning from query_decomposition or open a new session.",
            "id",
        )

    if not is_revision and sub_query_id in existing_ids:
        return _planning_validation_message(
            f"Duplicate sub-query id: {sub_query_id}",
            "id",
        )

    dependencies = item.get("depends_on") or []
    unique_dependencies = set()
    for dependency in dependencies:
        if dependency == sub_query_id:
            return _planning_validation_message(
                f"Sub-query '{sub_query_id}' cannot depend on itself.",
                "depends_on",
            )
        if dependency in unique_dependencies:
            return _planning_validation_message(
                f"Duplicate sub-query dependency: {dependency}",
                "depends_on",
            )
        unique_dependencies.add(dependency)
        if dependency not in valid_dependency_ids:
            return _planning_validation_message(
                f"Unknown sub-query dependency: {dependency}",
                "depends_on",
            )

    return None


def _validate_sub_query_reference(session, sub_query_id: str, field_name: str) -> str | None:
    existing_ids = _get_planning_sub_query_ids(session)
    if sub_query_id not in existing_ids:
        return _planning_validation_message(
            f"Unknown sub-query id: {sub_query_id}",
            field_name,
        )
    return None


def _validate_execution_plan(session, parallel: list[list[str]], sequential: list[str]) -> str | None:
    existing_ids = _get_planning_sub_query_ids(session)
    placement_stage: dict[str, int] = {}
    seen_ids: set[str] = set()

    for stage_index, group in enumerate(parallel):
        for sub_query_id in group:
            if sub_query_id not in existing_ids:
                return _planning_validation_message(
                    f"Unknown sub-query id: {sub_query_id}",
                    "parallel",
                )
            if sub_query_id in seen_ids:
                return _planning_validation_message(
                    f"Duplicate execution id: {sub_query_id}",
                    "parallel",
                )
            seen_ids.add(sub_query_id)
            placement_stage[sub_query_id] = stage_index

    sequential_offset = len(parallel)
    for offset, sub_query_id in enumerate(sequential):
        if sub_query_id not in existing_ids:
            return _planning_validation_message(
                f"Unknown sub-query id: {sub_query_id}",
                "sequential",
            )
        if sub_query_id in seen_ids:
            return _planning_validation_message(
                f"Duplicate execution id: {sub_query_id}",
                "sequential",
            )
        seen_ids.add(sub_query_id)
        placement_stage[sub_query_id] = sequential_offset + offset

    missing_ids = sorted(existing_ids - seen_ids)
    if missing_ids:
        return _planning_validation_message(
            f"Missing sub-query ids in execution plan: {', '.join(missing_ids)}",
            "sequential",
        )

    for item in _get_planning_sub_queries(session):
        sub_query_id = item.get("id")
        if sub_query_id not in placement_stage:
            continue
        for dependency in item.get("depends_on") or []:
            dependency_stage = placement_stage.get(dependency)
            current_stage = placement_stage[sub_query_id]
            if dependency_stage is None:
                continue
            if dependency_stage >= current_stage:
                return _planning_validation_message(
                    f"Dependency order violation: {sub_query_id} depends on {dependency}",
                    "parallel",
                )

    return None


def _validate_upstream_phase_revision(session, phase: str) -> str | None:
    try:
        phase_index = PHASE_NAMES.index(phase)
    except ValueError:
        return None
    downstream_phases = PHASE_NAMES[phase_index + 1 :]
    if any(name in session.phases for name in downstream_phases):
        return _planning_validation_message(
            f"{phase} revision would invalidate downstream phases. Restart planning from {phase} or open a new session.",
            "is_revision",
        )
    return None


@mcp.tool(
    name="plan_intent",
    output_schema=None,
    description="""
    Phase 1 of search planning: Analyze user intent. Call this FIRST to create a session.
    Returns session_id for subsequent phases. Required flow:
    plan_intent → plan_complexity → plan_sub_query(×N) → plan_search_term(×N) → plan_tool_mapping(×N) → plan_execution

    Required phases depend on complexity: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.
    """,
)
async def plan_intent(
    thought: Annotated[str, "Reasoning for this phase"],
    core_question: Annotated[str, "Distilled core question in one sentence"],
    query_type: Annotated[str, "factual | comparative | exploratory | analytical"],
    time_sensitivity: Annotated[str, "realtime | recent | historical | irrelevant"],
    session_id: Annotated[str, "Empty for new session, or existing ID to revise"] = "",
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    domain: Annotated[str, "Specific domain if identifiable"] = "",
    premise_valid: Annotated[Optional[bool], "False if the question contains a flawed assumption"] = None,
    ambiguities: Annotated[str, "Comma-separated unresolved ambiguities"] = "",
    unverified_terms: Annotated[str, "Comma-separated external terms to verify"] = "",
    is_revision: Annotated[bool, "True to overwrite existing intent"] = False,
) -> str:
    import json
    session = planning_engine.get_session(session_id) if session_id else None
    if is_revision and not session:
        return _planning_session_error(session_id)
    if is_revision and session:
        revision_error = _validate_upstream_phase_revision(session, "intent_analysis")
        if revision_error:
            return revision_error
    data = {"core_question": core_question, "query_type": query_type, "time_sensitivity": time_sensitivity}
    if domain:
        data["domain"] = domain
    if premise_valid is not None:
        data["premise_valid"] = premise_valid
    if ambiguities:
        data["ambiguities"] = _split_csv(ambiguities)
    if unverified_terms:
        data["unverified_terms"] = _split_csv(unverified_terms)
    try:
        IntentOutput(**data)
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid intent input.", _format_validation_details(exc))
    return json.dumps(planning_engine.process_phase(
        phase="intent_analysis", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_complexity",
    output_schema=None,
    description="Phase 2: Assess search complexity (1-3). Controls required phases: Level 1 = phases 1-3; Level 2 = phases 1-5; Level 3 = all 6.",
)
async def plan_complexity(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for complexity assessment"],
    level: Annotated[int, "Complexity 1-3"],
    estimated_sub_queries: Annotated[int, "Expected number of sub-queries"],
    estimated_tool_calls: Annotated[int, "Expected total tool calls"],
    justification: Annotated[str, "Why this complexity level"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    session = planning_engine.get_session(session_id)
    if not session:
        return _planning_session_error(session_id)
    if is_revision:
        revision_error = _validate_upstream_phase_revision(session, "complexity_assessment")
        if revision_error:
            return revision_error
    try:
        ComplexityOutput(
            level=level,
            estimated_sub_queries=estimated_sub_queries,
            estimated_tool_calls=estimated_tool_calls,
            justification=justification,
        )
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid complexity input.", _format_validation_details(exc))
    return json.dumps(planning_engine.process_phase(
        phase="complexity_assessment", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"level": level, "estimated_sub_queries": estimated_sub_queries,
                     "estimated_tool_calls": estimated_tool_calls, "justification": justification},
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_sub_query",
    output_schema=None,
    description="Phase 3: Add one sub-query. Call once per sub-query; data accumulates across calls. Set is_revision=true to replace all.",
)
async def plan_sub_query(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this sub-query"],
    id: Annotated[str, "Unique ID (e.g., 'sq1')"],
    goal: Annotated[str, "Sub-query goal"],
    expected_output: Annotated[str, "What success looks like"],
    boundary: Annotated[str, "What this excludes — mutual exclusion with siblings"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    depends_on: Annotated[str, "Comma-separated prerequisite IDs"] = "",
    tool_hint: Annotated[str, "web_search | web_fetch | web_map"] = "",
    is_revision: Annotated[bool, "True to replace all sub-queries"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return _planning_session_error(session_id)
    item = {"id": id, "goal": goal, "expected_output": expected_output, "boundary": boundary}
    if depends_on:
        item["depends_on"] = _split_csv(depends_on)
    if tool_hint:
        item["tool_hint"] = tool_hint
    try:
        SubQuery(**item)
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid sub-query input.", _format_validation_details(exc))
    validation_error = _validate_sub_query_item(planning_engine.get_session(session_id), item, is_revision)
    if validation_error:
        return validation_error
    return json.dumps(planning_engine.process_phase(
        phase="query_decomposition", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_search_term",
    output_schema=None,
    description="Phase 4: Add one search term. Call once per term; data accumulates. First call must set approach.",
)
async def plan_search_term(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this search term"],
    term: Annotated[str, "Search query (max 8 words)"],
    purpose: Annotated[str, "Sub-query ID this serves (e.g., 'sq1')"],
    round: Annotated[int, "Execution round: 1=broad, 2+=targeted follow-up"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    approach: Annotated[str, "broad_first | narrow_first | targeted (required on first call)"] = "",
    fallback_plan: Annotated[str, "Fallback if primary searches fail"] = "",
    is_revision: Annotated[bool, "True to replace all search terms"] = False,
) -> str:
    import json
    session = planning_engine.get_session(session_id)
    if not session:
        return _planning_session_error(session_id)
    if (is_revision or "search_strategy" not in session.phases) and not approach:
        return _planning_validation_error(
            "first_search_term_requires_approach",
            "The first search term must include approach=broad_first|narrow_first|targeted.",
        )
    data = {"search_terms": [{"term": term, "purpose": purpose, "round": round}]}
    if approach:
        data["approach"] = approach
    if fallback_plan:
        data["fallback_plan"] = fallback_plan
    try:
        StrategyOutput(
            approach=approach or "targeted",
            search_terms=[SearchTerm(term=term, purpose=purpose, round=round)],
            fallback_plan=fallback_plan or None,
        )
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid search strategy input.", _format_validation_details(exc))
    validation_error = _validate_sub_query_reference(session, purpose, "purpose")
    if validation_error:
        return validation_error
    return json.dumps(planning_engine.process_phase(
        phase="search_strategy", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=data,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_tool_mapping",
    output_schema=None,
    description="Phase 5: Map a sub-query to a tool. Call once per mapping; data accumulates.",
)
async def plan_tool_mapping(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for this mapping"],
    sub_query_id: Annotated[str, "Sub-query ID to map"],
    tool: Annotated[str, "web_search | web_fetch | web_map"],
    reason: Annotated[str, "Why this tool for this sub-query"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    params_json: Annotated[str, "Optional JSON string for tool-specific params"] = "",
    is_revision: Annotated[bool, "True to replace all mappings"] = False,
) -> str:
    import json
    session = planning_engine.get_session(session_id)
    if not session:
        return _planning_session_error(session_id)
    if is_revision and "execution_order" in session.phases:
        return _planning_validation_message(
            "Tool mapping revision would invalidate execution_order. Restart planning from tool_selection or open a new session.",
            "sub_query_id",
        )
    item = {"sub_query_id": sub_query_id, "tool": tool, "reason": reason}
    if params_json:
        try:
            item["params"] = json.loads(params_json)
        except json.JSONDecodeError:
            return _planning_validation_error(
                "validation_error",
                "Invalid tool mapping input.",
                [{"loc": ["params_json"], "msg": "params_json must be valid JSON.", "type": "json_invalid"}],
            )
    try:
        ToolPlanItem(**item)
    except ValidationError as exc:
        if any(detail["type"] == "literal_error" for detail in _format_validation_details(exc)):
            return _planning_validation_error("invalid_tool", "tool must be one of web_search, web_fetch, web_map.")
        return _planning_validation_error("validation_error", "Invalid tool mapping input.", _format_validation_details(exc))
    validation_error = _validate_sub_query_reference(session, sub_query_id, "sub_query_id")
    if validation_error:
        return validation_error
    return json.dumps(planning_engine.process_phase(
        phase="tool_selection", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence, phase_data=item,
    ), ensure_ascii=False, indent=2)


@mcp.tool(
    name="plan_execution",
    output_schema=None,
    description="Phase 6: Define execution order. parallel_groups: semicolon-separated groups of comma-separated IDs (e.g., 'sq1,sq2;sq3').",
)
async def plan_execution(
    session_id: Annotated[str, "Session ID from plan_intent"],
    thought: Annotated[str, "Reasoning for execution order"],
    parallel_groups: Annotated[str, "Parallel batches: 'sq1,sq2;sq3,sq4' (semicolon=groups, comma=IDs)"],
    sequential: Annotated[str, "Comma-separated IDs that must run in order"],
    estimated_rounds: Annotated[int, "Estimated execution rounds"],
    confidence: Annotated[float, "Confidence 0.0-1.0"] = 1.0,
    is_revision: Annotated[bool, "True to overwrite"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return _planning_session_error(session_id)
    parallel = [_split_csv(g) for g in parallel_groups.split(";") if g.strip()] if parallel_groups else []
    seq = _split_csv(sequential)
    session = planning_engine.get_session(session_id)
    try:
        ExecutionOrderOutput(parallel=parallel, sequential=seq, estimated_rounds=estimated_rounds)
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid execution plan input.", _format_validation_details(exc))
    if "tool_selection" in session.phases or "execution_order" in session.phases:
        validation_error = _validate_execution_plan(session, parallel, seq)
        if validation_error:
            return validation_error
    return json.dumps(planning_engine.process_phase(
        phase="execution_order", thought=thought, session_id=session_id,
        is_revision=is_revision, confidence=confidence,
        phase_data={"parallel": parallel, "sequential": seq, "estimated_rounds": estimated_rounds},
    ), ensure_ascii=False, indent=2)


def _configure_windows_event_loop_policy() -> None:
    if sys.platform != "win32":
        return

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:
        return

    asyncio.set_event_loop_policy(policy_cls())


def main():
    import signal
    import os
    import threading

    # 信号处理（仅主线程）
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum, frame):
            os._exit(0)
        signal.signal(signal.SIGINT, handle_shutdown)
        if sys.platform != 'win32':
            signal.signal(signal.SIGTERM, handle_shutdown)

    _configure_windows_event_loop_policy()

    # Windows 父进程监控
    if sys.platform == 'win32':
        import time
        import ctypes
        parent_pid = os.getppid()

        def is_parent_alive(pid):
            """Windows 下检查进程是否存活"""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            result = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return result and exit_code.value == STILL_ACTIVE

        def monitor_parent():
            while True:
                if not is_parent_alive(parent_pid):
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=monitor_parent, daemon=True).start()

    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


if __name__ == "__main__":
    main()
