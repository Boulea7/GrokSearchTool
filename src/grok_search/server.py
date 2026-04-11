import asyncio
import inspect
import re
import sys
import time
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from fastmcp import FastMCP, Context
from pydantic import Field, ValidationError

# 支持直接运行：添加 src 目录到 Python 路径
src_dir = Path(__file__).parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

# 尝试使用绝对导入（支持 mcp run）
try:
    from grok_search.providers.grok import GrokSearchProvider
    from grok_search.logger import log_info, log_warning
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
    from .logger import log_info, log_warning
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
_AVAILABLE_MODELS_CACHE: dict[tuple[str, str], tuple[list[str], float | None]] = {}
_AVAILABLE_MODELS_LOCK = asyncio.Lock()
_AVAILABLE_MODELS_CACHE_TTL_SECONDS = 300.0
_AVAILABLE_MODELS_CACHE_FAILURE_TTL_SECONDS = 5.0
_SEARCH_PROBE_QUERY = "Reply with the single word ready."
_FETCH_PROBE_URL = "https://example.com"
_PREFERRED_GROK_MODEL = "grok-4.20-0309"
_MODEL_FALLBACK_WARNING = "model_fallback_applied"
_BODY_MISSING_SOURCES_ONLY_WARNING = "body_missing_sources_only"
_BODY_PROBABLY_TRUNCATED_WARNING = "body_probably_truncated"


def _available_models_cache_now() -> float:
    return time.monotonic()


def _available_models_cache_expires_at() -> float | None:
    if _AVAILABLE_MODELS_CACHE_TTL_SECONDS <= 0:
        return None
    return _available_models_cache_now() + _AVAILABLE_MODELS_CACHE_TTL_SECONDS


def _available_models_failure_cache_expires_at() -> float | None:
    if _AVAILABLE_MODELS_CACHE_FAILURE_TTL_SECONDS <= 0:
        return None
    return _available_models_cache_now() + _AVAILABLE_MODELS_CACHE_FAILURE_TTL_SECONDS


async def _fetch_available_models(api_url: str, api_key: str) -> list[str]:
    import httpx

    models_url = f"{api_url.rstrip('/')}/models"
    async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(models_url, timeout=10.0)) as client:
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
        cached = _AVAILABLE_MODELS_CACHE.get(key)
        if cached is not None:
            models, expires_at = cached
            if expires_at is None or expires_at > _available_models_cache_now():
                return models
            _AVAILABLE_MODELS_CACHE.pop(key, None)

    try:
        models = await _fetch_available_models(api_url, api_key)
    except Exception:
        async with _AVAILABLE_MODELS_LOCK:
            _AVAILABLE_MODELS_CACHE[key] = ([], _available_models_failure_cache_expires_at())
        return []

    async with _AVAILABLE_MODELS_LOCK:
        _AVAILABLE_MODELS_CACHE[key] = (models, _available_models_cache_expires_at())
    return models


def _parse_grok_model_parts(model: str) -> tuple[int, int, tuple[int, ...], str] | None:
    text = (model or "").strip().lower()
    match = re.match(r"^grok-(\d+)\.(\d+)(?:-(.*))?$", text)
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2))
    remainder = (match.group(3) or "").strip()
    numeric_parts: list[int] = []
    semantic_parts: list[str] = []

    if remainder:
        for part in remainder.split("-"):
            if part.isdigit() and not semantic_parts:
                numeric_parts.append(int(part))
            elif part:
                semantic_parts.append(part)

    return major, minor, tuple(numeric_parts), "-".join(semantic_parts)


def _is_flexible_grok_model(model: str) -> bool:
    parts = _parse_grok_model_parts(model)
    if not parts:
        return False
    major, minor, _, _ = parts
    return (major, minor) >= (4, 1)


def _grok_model_preference_key(model: str) -> tuple:
    parts = _parse_grok_model_parts(model)
    if not parts:
        return (-1, -1, (), -1)

    major, minor, numeric_parts, semantic_suffix = parts
    padded_numeric = numeric_parts + (0, 0, 0)
    semantic_preference = {
        "": 3,
        "non-reasoning": 2,
        "reasoning": 1,
    }.get(semantic_suffix, 0)
    return (major, minor, padded_numeric[:3], semantic_preference)


def _pick_flexible_grok_model(available_models: list[str]) -> str | None:
    candidates = [model for model in available_models if _is_flexible_grok_model(model)]
    if not candidates:
        return None
    if _PREFERRED_GROK_MODEL in candidates:
        return _PREFERRED_GROK_MODEL
    return max(candidates, key=_grok_model_preference_key)


def _ordered_flexible_grok_models(available_models: list[str]) -> list[str]:
    candidates = [model for model in available_models if _is_flexible_grok_model(model)]
    if not candidates:
        return []
    return sorted(candidates, key=_grok_model_preference_key, reverse=True)


def _resolve_model_against_available_models(requested_model: str, available_models: list[str]) -> tuple[str | None, str | None]:
    normalized_model = (requested_model or "").strip()
    if not normalized_model:
        return normalized_model, None
    if not available_models:
        return normalized_model, None
    if normalized_model in available_models:
        return normalized_model, None
    if _is_flexible_grok_model(normalized_model):
        fallback_model = _pick_flexible_grok_model(available_models)
        if fallback_model:
            return fallback_model, _MODEL_FALLBACK_WARNING
    return None, "invalid_model"


def _fallback_candidates_for_model(requested_model: str, current_model: str, available_models: list[str]) -> list[str]:
    if not _is_flexible_grok_model(requested_model):
        return []
    return [model for model in _ordered_flexible_grok_models(available_models) if model != current_model]


def _is_grok_model_unavailable_message(message: str) -> bool:
    normalized = (message or "").strip().lower()
    if not normalized:
        return False
    markers = (
        "no available channel for model",
        "unsupported model",
        "invalid model",
        "model not found",
        "model is not available",
        "model unavailable",
        "no model named",
    )
    return any(marker in normalized for marker in markers)


def _is_model_unavailable_check(check: dict) -> bool:
    return check.get("reason_code") == "model_unavailable" or _is_grok_model_unavailable_message(
        check.get("message", "")
    )


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


def _mask_sensitive_text(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    try:
        configured_grok_key = config.grok_api_key
    except ValueError:
        configured_grok_key = None

    for configured in (configured_grok_key, config.tavily_api_key, config.firecrawl_api_key):
        if configured:
            text = text.replace(configured, "***")

    patterns = [
        (r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***"),
        (r"\bsk-[A-Za-z0-9_\-]+\b", "sk-***"),
        (r"\bfc-[A-Za-z0-9_\-]+\b", "fc-***"),
        (r"\btvly-[A-Za-z0-9_\-]+\b", "tvly-***"),
        (
            rf"([?#&](?:{_SENSITIVE_TEXT_PARAM_NAME_PATTERN})=)[^&#\s]+",
            r"\1***",
        ),
        (
            rf"((?:{_SENSITIVE_TEXT_PARAM_NAME_PATTERN})=)[^&#\s\"'}}]+",
            r"\1***",
        ),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _mask_sensitive_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""

    try:
        split = urlsplit(text)
    except ValueError:
        return _mask_sensitive_text(text)

    if split.scheme.lower() not in {"http", "https"} or not split.netloc:
        return _mask_sensitive_text(text)

    hostname = split.hostname or ""
    if not hostname:
        return _mask_sensitive_text(text)

    if ":" in hostname and not hostname.startswith("["):
        host = f"[{hostname}]"
    else:
        host = hostname
    raw_port = ""
    hostinfo = split.netloc.rsplit("@", 1)[-1]
    if hostinfo.startswith("["):
        closing_idx = hostinfo.find("]")
        if closing_idx != -1 and closing_idx + 1 < len(hostinfo) and hostinfo[closing_idx + 1] == ":":
            raw_port = hostinfo[closing_idx + 2 :]
    else:
        if ":" in hostinfo:
            raw_port = hostinfo.rsplit(":", 1)[-1]
    if raw_port:
        netloc = f"{host}:{raw_port}"
    else:
        netloc = host

    query = urlencode(
        [
            (key, "***" if key.lower() in _SENSITIVE_URL_PARAM_KEYS else value)
            for key, value in parse_qsl(split.query, keep_blank_values=True)
        ],
        doseq=True,
        safe="*",
    )
    fragment = split.fragment
    if fragment and any(token in fragment for token in ("=", "&")):
        fragment = urlencode(
            [
                (key, "***" if key.lower() in _SENSITIVE_URL_PARAM_KEYS else value)
                for key, value in parse_qsl(fragment, keep_blank_values=True)
            ],
            doseq=True,
            safe="*",
        )

    return urlunsplit((split.scheme, netloc, split.path, query, fragment))


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
                return _mask_sensitive_text(message)

    body_text = (getattr(response, "text", "") or "").strip()
    if not body_text:
        return ""

    normalized = body_text.lower()
    if "<html" in normalized and "bad gateway" in normalized:
        return "html_5xx_page"
    if "<html" in normalized and _looks_like_login_page(body_text):
        return "login_page"

    snippet = body_text[:180].replace("\n", " ").strip()
    snippet = _mask_sensitive_text(snippet)
    return snippet


def _format_grok_error(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "搜索失败: 上游请求超时，请稍后重试"

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        location = _mask_sensitive_url(exc.response.headers.get("location", "").strip())
        summary = _extract_error_summary(exc.response)
        if status_code in {301, 302, 303, 307, 308} and location:
            message = f"搜索失败: 上游返回 HTTP {status_code} 重定向到 {location}，请检查代理认证状态"
        else:
            message = f"搜索失败: 上游返回 HTTP {status_code}"
        if summary:
            message += f"，摘要={summary}"
        return message

    message = _mask_sensitive_text(str(exc).strip())
    if message:
        return f"搜索失败: {message}"
    return "搜索失败: 上游请求异常"


def _looks_like_login_page(body_text: str) -> bool:
    normalized = (body_text or "").strip().lower()
    if "<html" not in normalized:
        return False
    return any(token in normalized for token in ("login", "sign in", "signin", "auth"))


def _looks_like_html_response(body_text: str) -> bool:
    return "<html" in ((body_text or "").strip().lower())


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


def _assess_search_body_quality(answer: str, sources: list[dict]) -> str | None:
    stripped_answer = (answer or "").strip()
    if not stripped_answer and sources:
        return _BODY_MISSING_SOURCES_ONLY_WARNING
    if stripped_answer and _is_probably_truncated_content(stripped_answer, min_length=10):
        return _BODY_PROBABLY_TRUNCATED_WARNING
    return None


def _search_probe_quality_message(warning_code: str) -> str:
    if warning_code == _BODY_MISSING_SOURCES_ONLY_WARNING:
        return "真实搜索探针返回成功，但上游只返回了信源列表，未返回正文。"
    if warning_code == _BODY_PROBABLY_TRUNCATED_WARNING:
        return "真实搜索探针返回成功，但正文疑似截断。"
    return "真实搜索探针返回成功，但正文质量存在疑点。"


async def _provider_search_with_sources(
    provider,
    query: str,
    *,
    platform: str = "",
    min_results: int = 3,
    max_results: int = 10,
    ctx=None,
) -> tuple[str, list[dict]]:
    async def _call_with_supported_kwargs(method):
        kwargs = {
            "platform": platform,
            "min_results": min_results,
            "max_results": max_results,
            "ctx": ctx,
        }
        parameters = inspect.signature(method).parameters
        supported_kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        return await method(query, **supported_kwargs)

    if hasattr(provider, "search_with_sources"):
        content, structured_sources = await _call_with_supported_kwargs(provider.search_with_sources)
        return content, structured_sources

    content = await _call_with_supported_kwargs(provider.search)
    return content, []


def _format_fetch_error(provider: str, exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return f"{provider} 请求超时"

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        location = _mask_sensitive_url(exc.response.headers.get("location", "").strip())
        summary = _extract_error_summary(exc.response)
        if status_code in {301, 302, 303, 307, 308} and location:
            message = f"{provider} 返回 HTTP {status_code} 重定向到 {location}，请检查认证状态"
        elif status_code in {401, 403}:
            message = f"{provider} 返回 HTTP {status_code}，请检查认证状态"
        else:
            message = f"{provider} 返回 HTTP {status_code}"
        if summary:
            message += f"，摘要={summary}"
        return message

    message = _mask_sensitive_text(str(exc).strip())
    if message:
        return f"{provider} 请求失败: {message}"
    return f"{provider} 请求失败"


def _extra_results_to_sources(
    tavily_results: list[dict] | None,
    firecrawl_results: list[dict] | None,
) -> list[dict]:
    firecrawl_sources: list[dict] = []
    tavily_sources: list[dict] = []

    if firecrawl_results:
        for r in firecrawl_results:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            item: dict = {"url": url, "provider": "firecrawl"}
            title = (r.get("title") or "").strip()
            if title:
                item["title"] = title
            desc = (r.get("description") or "").strip()
            if desc:
                item["description"] = desc
            firecrawl_sources.append(item)

    if tavily_results:
        for r in tavily_results:
            url = (r.get("url") or "").strip()
            if not url:
                continue
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
            published_at = (r.get("published_at") or "").strip()
            if published_at:
                item["published_at"] = published_at
            published_date = (r.get("published_date") or "").strip()
            if published_date:
                item["published_date"] = published_date
            tavily_sources.append(item)

    return merge_sources(firecrawl_sources, tavily_sources)


def _extract_firecrawl_markdown_payload(data: dict) -> str:
    if not isinstance(data, dict):
        return ""

    nested_data = data.get("data")
    if isinstance(nested_data, dict):
        markdown = nested_data.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown

    markdown = data.get("markdown")
    if isinstance(markdown, str):
        return markdown

    return ""


def _validate_tavily_extract_payload(data: object) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, "Tavily 响应结构异常：缺少顶层对象"

    results = data.get("results")
    if not isinstance(results, list):
        return None, "Tavily 响应结构异常：缺少 results 列表"

    if not results:
        return None, "Tavily 提取成功但 results 为空"

    first_item = results[0]
    if not isinstance(first_item, dict):
        return None, "Tavily 响应结构异常：results[0] 必须是对象"

    content = first_item.get("raw_content", "")
    if isinstance(content, str) and content.strip():
        return content, None

    return None, "Tavily 提取成功但内容为空"


def _extract_firecrawl_search_payload(data: dict) -> list[dict]:
    if not isinstance(data, dict):
        return []

    empty_result: list[dict] | None = None
    nested_data = data.get("data")
    if isinstance(nested_data, dict):
        for key in ("web", "results"):
            results = nested_data.get(key)
            if isinstance(results, list):
                if results:
                    return results
                if empty_result is None:
                    empty_result = results

    for key in ("web", "results"):
        flat_results = data.get(key)
        if isinstance(flat_results, list):
            if flat_results:
                return flat_results
            if empty_result is None:
                empty_result = flat_results

    return empty_result or []


_VALID_SEARCH_TOPICS = {"general", "news", "finance"}
_VALID_TIME_RANGES = {"day", "week", "month", "year"}
_TIME_RANGE_ALIASES = {"d": "day", "w": "week", "m": "month", "y": "year"}
_DOMAIN_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PRIVATE_HOST_SUFFIXES = (".internal", ".local", ".lan", ".home", ".corp")
_LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}
_LOOPBACK_HELPER_SUFFIXES = ("localtest.me", "lvh.me")
_DNS_ALIAS_IP_SUFFIXES = ("nip.io", "xip.io", "sslip.io")


@dataclass(frozen=True)
class _TargetPreflightResult:
    status: Literal["allow", "reject", "skipped_due_to_error"]
    message: str | None = None


def _allow_target_preflight() -> _TargetPreflightResult:
    return _TargetPreflightResult("allow")


def _reject_target_preflight(message: str) -> _TargetPreflightResult:
    return _TargetPreflightResult("reject", message)


def _skip_target_preflight(message: str) -> _TargetPreflightResult:
    return _TargetPreflightResult("skipped_due_to_error", message)

_SENSITIVE_URL_PARAM_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "code",
    "id_token",
    "password",
    "refresh_token",
    "token",
    "signature",
    "sig",
    "x-amz-credential",
    "x-amz-signature",
    "x-amz-security-token",
    "x-goog-credential",
    "x-goog-signature",
    "x-ms-signature",
    "googleaccessid",
}
_SENSITIVE_TEXT_PARAM_NAME_PATTERN = "|".join(
    sorted((re.escape(key) for key in _SENSITIVE_URL_PARAM_KEYS), key=len, reverse=True)
)


def _normalize_domain_list(domains: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    for item in domains or []:
        if not isinstance(item, str):
            continue
        domain = item.strip().lower().rstrip(".")
        if not domain:
            continue
        if domain not in normalized:
            normalized.append(domain)
    return normalized


def _is_valid_domain_filter(domain: str) -> bool:
    candidate = (domain or "").strip().lower().rstrip(".")
    if not candidate:
        return False
    if "://" in candidate or "/" in candidate or any(ch.isspace() for ch in candidate):
        return False
    if candidate == "localhost":
        return True

    labels = candidate.split(".")
    return all(_DOMAIN_LABEL_PATTERN.fullmatch(label) for label in labels)


def _validate_public_target_url(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return "仅支持 http/https URL"

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "仅支持 http/https URL"
    if host in _LOCAL_HOSTNAMES or any(host.endswith(f".{name}") for name in _LOCAL_HOSTNAMES):
        return "目标 URL 不能指向本地或私有网络"
    if host in _LOOPBACK_HELPER_SUFFIXES or any(host.endswith(f".{name}") for name in _LOOPBACK_HELPER_SUFFIXES):
        return "目标 URL 不能指向本地或私有网络"
    if _looks_like_ipv4_loopback_shorthand(host):
        return "目标 URL 不能指向本地或私有网络"
    alias_ip = _extract_dns_alias_ip(host)
    if alias_ip and (
        alias_ip.is_loopback
        or alias_ip.is_private
        or alias_ip.is_link_local
        or alias_ip.is_reserved
        or alias_ip.is_multicast
        or alias_ip.is_unspecified
    ):
        return "目标 URL 不能指向本地或私有网络"

    try:
        ip = ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(_PRIVATE_HOST_SUFFIXES):
            return "目标 URL 不能指向本地或私有网络"
        return None

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "目标 URL 不能指向本地或私有网络"
    return None


def _is_non_public_ip(ip) -> bool:
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _resolve_and_validate_public_target(url: str) -> str | None:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None

    try:
        host_ip = ip_address(host)
    except ValueError:
        # Provider-backed fetch/map runs remotely, so local DNS answers are not
        # authoritative enough to hard-block ordinary public hostnames here.
        return None

    if _is_non_public_ip(host_ip):
        return "目标 URL 不能指向本地或私有网络"
    return None


async def _preflight_redirect_targets(url: str, *, max_redirects: int = 5) -> _TargetPreflightResult:
    import httpx

    current_url = url
    for _ in range(max_redirects):
        try:
            async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(current_url, timeout=5.0)) as client:
                response = await client.get(current_url, headers={"Accept": "*/*"})
        except httpx.TimeoutException:
            return _skip_target_preflight("目标 URL 重定向预检超时")
        except httpx.RequestError:
            return _skip_target_preflight("目标 URL 重定向预检失败")

        location = (response.headers.get("location") or "").strip()
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            return _allow_target_preflight()

        next_url = urljoin(current_url, location)
        validation_error = _validate_public_target_url(next_url)
        if validation_error:
            return _reject_target_preflight(validation_error)
        resolution_error = _resolve_and_validate_public_target(next_url)
        if resolution_error:
            return _reject_target_preflight(resolution_error)
        current_url = next_url

    return _reject_target_preflight("目标 URL 重定向次数过多")


async def _preflight_public_target_url(url: str) -> _TargetPreflightResult:
    validation_error = _validate_public_target_url(url)
    if validation_error:
        return _reject_target_preflight(validation_error)

    resolution_error = _resolve_and_validate_public_target(url)
    if resolution_error:
        return _reject_target_preflight(resolution_error)

    return await _preflight_redirect_targets(url)


def _looks_like_ipv4_loopback_shorthand(host: str) -> bool:
    labels = host.split(".")
    return (
        2 <= len(labels) <= 4
        and labels[0] == "127"
        and all(label.isdigit() for label in labels)
    )


def _extract_dns_alias_ip(host: str):
    normalized_host = (host or "").lower().rstrip(".")
    for suffix in _DNS_ALIAS_IP_SUFFIXES:
        if normalized_host == suffix or not normalized_host.endswith(f".{suffix}"):
            continue

        alias_labels = normalized_host[: -(len(suffix) + 1)].split(".")
        dotted_candidate = ".".join(alias_labels[-4:])
        if len(alias_labels) >= 4 and all(label.isdigit() for label in alias_labels[-4:]):
            try:
                return ip_address(dotted_candidate)
            except ValueError:
                pass

        dashed_label = alias_labels[-1] if alias_labels else ""
        dashed_parts = dashed_label.split("-")
        if len(dashed_parts) == 4 and all(part.isdigit() for part in dashed_parts):
            try:
                return ip_address(".".join(dashed_parts))
            except ValueError:
                pass

    return None


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


def _normalize_search_warnings(search_warnings: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    for item in search_warnings or []:
        if not isinstance(item, str):
            continue
        warning = item.strip()
        if warning and warning not in normalized:
            normalized.append(warning)
    return normalized


def _build_sources_cache_entry(
    sources: list[dict],
    *,
    search_status: str,
    search_error: str | None,
    search_warnings: Optional[list[str]] = None,
) -> dict:
    return {
        "sources": sources,
        "search_status": search_status,
        "search_error": search_error,
        "search_warnings": _normalize_search_warnings(search_warnings),
        "source_state": _derive_source_state(sources, search_status),
    }


def _derive_source_state(sources: list[dict], search_status: str) -> str:
    if sources:
        return "available"
    if search_status == "error":
        return "unavailable_due_to_search_error"
    return "empty"


def _normalize_sources_cache_entry(entry: object) -> dict | None:
    if isinstance(entry, dict) and isinstance(entry.get("sources"), list):
        search_status = entry.get("search_status") or "ok"
        sources = entry.get("sources", [])
        return {
            "sources": sources,
            "search_status": search_status,
            "search_error": entry.get("search_error"),
            "search_warnings": _normalize_search_warnings(entry.get("search_warnings")),
            "source_state": entry.get("source_state") or _derive_source_state(sources, search_status),
        }

    if isinstance(entry, list):
        return _build_sources_cache_entry(
            entry,
            search_status="ok",
            search_error=None,
            search_warnings=[],
        )

    return None


def _classify_sources_cache_entry(entry: object) -> tuple[dict | None, str]:
    normalized_entry = _normalize_sources_cache_entry(entry)
    if normalized_entry is None:
        return None, "unreadable"
    if normalized_entry["sources"] and not standardize_sources(normalized_entry["sources"]):
        return normalized_entry, "unreadable"
    if normalized_entry["search_status"] == "error":
        return normalized_entry, "error"
    return normalized_entry, "readable"


def _validate_search_inputs(
    query: str,
    topic: str,
    time_range: str,
    include_domains: Optional[list[str]],
    exclude_domains: Optional[list[str]],
    extra_sources: int,
) -> tuple[dict, str | None]:
    normalized_query = query.strip()
    normalized_topic = (topic or "general").strip() or "general"
    raw_time_range = (time_range or "").strip()
    normalized_time_range = _TIME_RANGE_ALIASES.get(raw_time_range, raw_time_range) or None
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
        return effective_params, "搜索失败: topic 仅支持 general、news 或 finance"

    if normalized_time_range and normalized_time_range not in _VALID_TIME_RANGES:
        return effective_params, "搜索失败: time_range 仅支持 day、week、month、year（或 d、w、m、y）"

    if isinstance(extra_sources, bool) or not isinstance(extra_sources, int):
        return effective_params, "搜索失败: extra_sources 仅支持整数"

    if extra_sources < 0:
        return effective_params, "搜索失败: extra_sources 不能为负数"

    raw_domain_items = [*(include_domains or []), *(exclude_domains or [])]
    if any(not isinstance(item, str) or not item.strip() for item in raw_domain_items):
        return effective_params, "搜索失败: include_domains 和 exclude_domains 仅支持非空字符串"
    if any(not _is_valid_domain_filter(item) for item in raw_domain_items):
        return effective_params, "搜索失败: include_domains 和 exclude_domains 仅支持合法域名"

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
    Prefer `plan_* -> web_search` for non-trivial or ambiguous research tasks, but clear single-hop lookups may directly use `web_search` when planning would add little value.
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
    topic: Annotated[str, "Optional search topic: general | news | finance."] = "general",
    time_range: Annotated[str, "Optional freshness filter: day | week | month | year (or d | w | m | y)."] = "",
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
        extra_sources=extra_sources,
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
        await _SOURCES_CACHE.set(
            session_id,
            _build_sources_cache_entry([], search_status="error", search_error="validation_error"),
        )
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
        await _SOURCES_CACHE.set(
            session_id,
            _build_sources_cache_entry([], search_status="error", search_error="config_error"),
        )
        return _build_search_response(
            session_id,
            f"配置错误: {str(e)}",
            0,
            status="error",
            effective_params=effective_params,
            error="config_error",
        )

    available_models = await _get_available_models_cached(api_url, api_key)
    requested_model = config.grok_model
    effective_model = requested_model
    warnings: list[str] = []
    if model:
        normalized_explicit_model = config._apply_model_suffix(model)
        requested_model = normalized_explicit_model
        resolved_model, resolution = _resolve_model_against_available_models(normalized_explicit_model, available_models)
        if resolution == "invalid_model":
            await _SOURCES_CACHE.set(
                session_id,
                _build_sources_cache_entry([], search_status="error", search_error="invalid_model"),
            )
            return _build_search_response(
                session_id,
                f"无效模型: {model}",
                0,
                status="error",
                effective_params=effective_params,
                error="invalid_model",
            )
        if resolution == _MODEL_FALLBACK_WARNING:
            warnings.append(_MODEL_FALLBACK_WARNING)
        effective_model = resolved_model or normalized_explicit_model
        effective_params["model"] = effective_model
    else:
        resolved_model, resolution = _resolve_model_against_available_models(effective_model, available_models)
        if resolution == _MODEL_FALLBACK_WARNING:
            warnings.append(_MODEL_FALLBACK_WARNING)
        if resolved_model:
            effective_model = resolved_model
        effective_params["model"] = effective_model

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

    async def _run_grok_with_model(current_model: str) -> tuple[str, list[dict], str | None, str | None]:
        grok_provider = GrokSearchProvider(api_url, api_key, current_model)
        grok_provider.time_context_required = bool(
            effective_params["topic"] != "general" or effective_params["time_range"]
        )
        try:
            result, structured_sources = await _provider_search_with_sources(
                grok_provider,
                validated_params["query"],
                platform=platform,
            )
        except Exception as exc:
            return "", [], _format_grok_error(exc), "upstream_request_failed"
        if (not result or not result.strip()) and not structured_sources:
            return "", structured_sources, "搜索失败: 上游返回空响应，请检查模型或代理配置", "upstream_empty_response"
        return result, structured_sources, None, None

    async def _safe_grok() -> tuple[str, list[dict], str | None, str | None, str, bool]:
        result, structured_sources, error_message, error_code = await _run_grok_with_model(effective_model)
        if error_message is None:
            return result, structured_sources, None, None, effective_model, False
        if not _is_grok_model_unavailable_message(error_message):
            return "", [], error_message, error_code, effective_model, False

        for candidate in _fallback_candidates_for_model(requested_model, effective_model, available_models):
            retry_result, retry_sources, retry_error_message, retry_error_code = await _run_grok_with_model(candidate)
            if retry_error_message is None:
                return retry_result, retry_sources, None, None, candidate, True
            error_message = retry_error_message
            error_code = retry_error_code
            if not _is_grok_model_unavailable_message(retry_error_message):
                break

        return "", [], error_message, error_code, effective_model, False

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

    grok_result, grok_structured_sources, grok_error, grok_error_code, actual_grok_model, runtime_fallback_applied = gathered[0]
    if runtime_fallback_applied and actual_grok_model != effective_model:
        effective_model = actual_grok_model
        effective_params["model"] = actual_grok_model
        if _MODEL_FALLBACK_WARNING not in warnings:
            warnings.append(_MODEL_FALLBACK_WARNING)
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
    grok_sources = merge_sources(grok_structured_sources, grok_sources)
    extra = _extra_results_to_sources(tavily_results, firecrawl_results)
    all_sources = merge_sources(grok_sources, extra)
    content = answer.strip()
    body_quality_warning = _assess_search_body_quality(content, all_sources)
    if body_quality_warning and body_quality_warning not in warnings:
        warnings.append(body_quality_warning)
    if not content:
        if grok_error:
            content = grok_error
        elif all_sources:
            content = "搜索成功，但上游只返回了信源列表，未返回正文。可调用 get_sources 查看信源。"
        else:
            content = sanitize_answer_text(grok_result).strip() or "搜索失败: 上游未返回可用正文"

    standardized_sources = standardize_sources(all_sources)
    status = "ok"
    error = None
    if grok_error:
        status = "error"
        error = grok_error_code or "upstream_request_failed"
    elif warnings:
        status = "partial"
    await _SOURCES_CACHE.set(
        session_id,
        _build_sources_cache_entry(
            standardized_sources,
            search_status=status,
            search_error=error,
            search_warnings=warnings,
        ),
    )

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
    cached_entry = await _SOURCES_CACHE.get(session_id)
    if cached_entry is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }
    normalized_entry = _normalize_sources_cache_entry(cached_entry)
    if normalized_entry is None:
        return {
            "session_id": session_id,
            "sources": [],
            "sources_count": 0,
            "error": "session_id_not_found_or_expired",
        }

    standardized_sources = standardize_sources(normalized_entry["sources"])
    recalculated_state = _build_sources_cache_entry(
        standardized_sources,
        search_status=normalized_entry["search_status"],
        search_error=normalized_entry["search_error"],
    )["source_state"]
    updated_entry = {
        **normalized_entry,
        "sources": standardized_sources,
        "source_state": recalculated_state,
    }
    if updated_entry != cached_entry:
        if isinstance(cached_entry, list):
            await _SOURCES_CACHE.set(session_id, standardized_sources)
        else:
            await _SOURCES_CACHE.set(session_id, updated_entry)

    return {
        "session_id": session_id,
        "sources": standardized_sources,
        "sources_count": len(standardized_sources),
        "search_status": normalized_entry["search_status"],
        "search_error": normalized_entry["search_error"],
        "search_warnings": normalized_entry["search_warnings"],
        "source_state": updated_entry["source_state"],
    }


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
        async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=60.0)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            if _looks_like_login_page(response.text):
                return None, "Tavily 返回登录页或认证页面，请检查代理认证状态"
            data = response.json()
            content, payload_error = _validate_tavily_extract_payload(data)
            if payload_error:
                return None, payload_error
            if content and _is_probably_truncated_content(content):
                return None, "Tavily 提取结果疑似被截断"
            return content, None
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
        "max_results": min(max(int(max_results), 1), 20),
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
        async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=90.0)) as client:
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
    body = {"query": query, "limit": min(max(int(limit), 1), 100)}
    try:
        async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=90.0)) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            results = _extract_firecrawl_search_payload(data)
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "description": r.get("description", "")}
                for r in results
            ] if results else []
    except Exception:
        return None


async def _call_firecrawl_scrape(
    url: str,
    ctx=None,
    *,
    max_retries: int | None = None,
) -> tuple[str | None, str | None]:
    import httpx
    api_url = config.firecrawl_api_url
    api_key = config.firecrawl_api_key
    if not api_key:
        return None, None
    endpoint = f"{api_url.rstrip('/')}/scrape"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    max_retries = max_retries or config.retry_max_attempts
    last_error: str | None = None
    for attempt in range(max_retries):
        body = {
            "url": url,
            "formats": ["markdown"],
            "timeout": 60000,
            "waitFor": (attempt + 1) * 1500,
        }
        try:
            async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=90.0)) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                if _looks_like_login_page(response.text):
                    return None, "Firecrawl 返回登录页或认证页面，请检查代理认证状态"
                data = response.json()
                if not isinstance(data, dict):
                    return None, "Firecrawl 响应结构异常：缺少顶层对象"
                markdown = _extract_firecrawl_markdown_payload(data)
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
            await log_info(ctx, f"Firecrawl scrape failed: {last_error}", config.debug_enabled)
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
    preflight = await _preflight_public_target_url(url)
    if preflight.status == "reject":
        return f"提取失败: {preflight.message}"
    if preflight.status == "skipped_due_to_error":
        await log_warning(ctx, f"Warning: Redirect preflight skipped: {preflight.message}")
        await log_info(ctx, f"Redirect preflight skipped: {preflight.message}", config.debug_enabled)

    await log_info(ctx, "Begin Fetch request", config.debug_enabled)

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
    body = {
        "url": url,
        "max_depth": max_depth,
        "max_breadth": max_breadth,
        "limit": limit,
        "timeout": timeout,
    }
    if instructions:
        body["instructions"] = instructions
    try:
        async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=float(timeout + 10))) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
            if _looks_like_login_page(response.text):
                return "映射失败: Tavily 返回登录页或认证页面，请检查代理认证状态"
            try:
                data = response.json()
            except Exception:
                if _looks_like_html_response(response.text):
                    return "映射失败: Tavily 返回 HTML 页面，不是合法 JSON"
                return "映射失败: Tavily 返回非法 JSON"
            if not isinstance(data, dict):
                return "映射失败: Tavily map 响应结构异常：缺少顶层对象"
            payload_error = _validate_tavily_map_probe_payload(data)
            if payload_error:
                return f"映射失败: Tavily map {payload_error.rstrip('。')}"
            return json.dumps({
                "base_url": data.get("base_url", ""),
                "results": data.get("results", []),
                "response_time": data.get("response_time", 0)
            }, ensure_ascii=False, indent=2)
    except httpx.TimeoutException:
        return f"映射超时: 请求超过{timeout}秒"
    except httpx.HTTPStatusError as e:
        return f"HTTP错误: {e.response.status_code} - {_mask_sensitive_text(e.response.text)[:200]}"
    except Exception as e:
        return f"映射错误: {_mask_sensitive_text(str(e))}"


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
    timeout: Annotated[int, Field(description="Maximum time in seconds for the operation.", ge=10, le=150)] = 150,
    ctx: Context = None,
) -> str:
    preflight = await _preflight_public_target_url(url)
    if preflight.status == "reject":
        return f"映射失败: {preflight.message}"
    if preflight.status == "skipped_due_to_error":
        await log_warning(ctx, f"Warning: Redirect preflight skipped: {preflight.message}")
        await log_info(None, f"Redirect preflight skipped: {preflight.message}", config.debug_enabled)

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
        check["endpoint"] = _mask_sensitive_url(endpoint)
    if response_time_ms is not None:
        check["response_time_ms"] = round(float(response_time_ms), 2)
    if skipped_reason:
        check["skipped_reason"] = skipped_reason
    for key, value in extra.items():
        if value is not None:
            check[key] = value
    return check


def _check_reason_code(check: dict) -> str | None:
    for key in ("reason_code", "warning_code", "error_kind"):
        value = check.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    skipped_reason = check.get("skipped_reason")
    if isinstance(skipped_reason, str) and skipped_reason.strip():
        return _normalize_skipped_reason_code(skipped_reason)
    return None


def _normalize_skipped_reason_code(skipped_reason: str) -> str:
    normalized = skipped_reason.strip()
    if not normalized:
        return "provider_not_configured"
    if normalized.endswith("ENABLED=false"):
        return "provider_disabled"
    if normalized.endswith("API_KEY 未配置"):
        return "missing_api_key"
    return "provider_not_configured"


def _readiness_cause_from_check(check: dict) -> dict:
    cause = {
        "check_id": check["check_id"],
        "status": check["status"],
    }
    reason_code = _check_reason_code(check)
    if reason_code:
        cause["reason_code"] = reason_code
    return cause


def _runtime_override_active(runtime_model_source: str) -> bool:
    return runtime_model_source in {"process_env", "project_env_local", "project_env"}


def _append_recommendation(
    recommendations: list[str],
    message: str,
    *,
    recommendation_details: list[dict] | None = None,
    check_id: str = "",
    feature: str = "",
    severity: str = "warning",
    extra_detail_fields: dict | None = None,
) -> None:
    if message and message not in recommendations:
        recommendations.append(message)
    if recommendation_details is None or not message:
        return
    detail = {
        "message": message,
        "severity": severity,
    }
    if check_id:
        detail["check_id"] = check_id
    if feature:
        detail["feature"] = feature
    if extra_detail_fields:
        for key, value in extra_detail_fields.items():
            if value is not None:
                detail[key] = value
    if detail not in recommendation_details:
        recommendation_details.append(detail)


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


def _runtime_model_source_label(source: str) -> str:
    labels = {
        "process_env": "进程环境变量 GROK_MODEL",
        "project_env_local": "项目 .env.local",
        "project_env": "项目 .env",
        "persisted_config": "持久化配置",
        "default": "代码默认值",
    }
    return labels.get(source, source or "未知来源")


def _httpx_client_kwargs_for_url(url: str, *, timeout: float) -> dict:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    kwargs = {"timeout": timeout}
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.startswith("127.")
    if is_loopback:
        kwargs["trust_env"] = False
    return kwargs


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
        async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(url, timeout=timeout)) as client:
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=json_body)

        response_time_ms = (time.perf_counter() - start_time) * 1000
        response_text = _mask_sensitive_text(response.text or "")[:120]
        if _looks_like_login_page(response.text or ""):
            return _build_doctor_check(
                check_id,
                "error",
                "响应看起来是登录页或认证页面。",
                endpoint=url,
                response_time_ms=response_time_ms,
                error_kind="login_page",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except Exception:
            if _looks_like_html_response(response.text or ""):
                return _build_doctor_check(
                    check_id,
                    "error",
                    "响应看起来是 HTML 页面，不是合法 JSON。",
                    endpoint=url,
                    response_time_ms=response_time_ms,
                    error_kind="html_response",
                    status_code=response.status_code,
                )
            return _build_doctor_check(
                check_id,
                "error",
                "响应不是合法 JSON。",
                endpoint=url,
                response_time_ms=response_time_ms,
                error_kind="invalid_json",
                status_code=response.status_code,
            )

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
        masked_message = _mask_sensitive_text(str(exc) or "timeout")
        return _build_doctor_check(
            check_id,
            "error",
            f"请求超时: {masked_message}",
            endpoint=url,
            error_kind="timeout",
        )
    except httpx.RequestError as exc:
        masked_message = _mask_sensitive_text(str(exc))
        return _build_doctor_check(
            check_id,
            "error",
            f"网络错误: {masked_message}",
            endpoint=url,
            error_kind="request_error",
        )
    except Exception as exc:
        masked_message = _mask_sensitive_text(str(exc))
        return _build_doctor_check(
            check_id,
            "error",
            f"未知错误: {masked_message}",
            endpoint=url,
            error_kind="unexpected_error",
        )


def _validate_tavily_extract_probe_payload(data: object) -> str | None:
    _, error = _validate_tavily_extract_payload(data)
    return f"{error}。" if error else None


def _validate_tavily_map_probe_payload(data: object) -> str | None:
    if not isinstance(data, dict):
        return "响应结构异常：缺少顶层对象。"

    results = data.get("results")
    if not isinstance(results, list):
        return "响应结构异常：缺少 results 列表。"

    for index, item in enumerate(results):
        if not isinstance(item, str):
            return f"响应结构异常：results[{index}] 必须是字符串。"

    return None


def _build_connection_test_from_models_check(models_check: dict) -> dict:
    status_map = {
        "timeout": "连接超时",
        "request_error": "连接失败",
        "http_error": "连接异常",
        "config_error": "配置错误",
        "login_page": "连接失败",
        "html_response": "连接异常",
        "invalid_json": "连接异常",
    }
    if models_check["status"] == "ok":
        result = {
            "status": "连接成功",
            "message": models_check["message"],
            "response_time_ms": models_check.get("response_time_ms", 0),
            "scope": "models_endpoint",
        }
        if models_check.get("available_models"):
            result["available_models"] = models_check["available_models"]
        return result

    return {
        "status": status_map.get(models_check.get("error_kind"), "测试失败"),
        "message": models_check["message"],
        "response_time_ms": models_check.get("response_time_ms", 0),
        "scope": "models_endpoint",
    }


async def _probe_web_search(api_url: str, api_key: str, model: str) -> dict:
    import time

    start_time = time.perf_counter()
    provider = GrokSearchProvider(api_url, api_key, model)
    try:
        content, structured_sources = await _provider_search_with_sources(provider, _SEARCH_PROBE_QUERY)
    except Exception as exc:
        check = _build_doctor_check(
            "grok_search_probe",
            "error",
            f"真实搜索探针失败: {_format_grok_error(exc)}",
            endpoint=f"{api_url.rstrip('/')}/chat/completions",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            error_kind="probe_failed",
        )
        if _is_grok_model_unavailable_message(check["message"]):
            check["reason_code"] = "model_unavailable"
        return check

    answer, probe_sources = split_answer_and_sources(content)
    if not probe_sources:
        probe_sources = extract_sources_from_text(content)
    probe_sources = merge_sources(structured_sources, probe_sources)
    body_quality_warning = _assess_search_body_quality(answer, probe_sources)
    if body_quality_warning:
        return _build_doctor_check(
            "grok_search_probe",
            "warning",
            _search_probe_quality_message(body_quality_warning),
            endpoint=f"{api_url.rstrip('/')}/chat/completions",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            warning_code=body_quality_warning,
        )

    if not sanitize_answer_text(content).strip():
        return _build_doctor_check(
            "grok_search_probe",
            "error",
            "真实搜索探针失败: 上游未返回可用正文。",
            endpoint=f"{api_url.rstrip('/')}/chat/completions",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            error_kind="empty_probe_response",
        )

    return _build_doctor_check(
        "grok_search_probe",
        "ok",
        "真实搜索探针成功。",
        endpoint=f"{api_url.rstrip('/')}/chat/completions",
        response_time_ms=(time.perf_counter() - start_time) * 1000,
    )


async def _probe_web_search_with_fallback(
    api_url: str,
    api_key: str,
    requested_model: str,
    available_models: list[str],
) -> dict:
    resolved_model, resolution = _resolve_model_against_available_models(requested_model, available_models)
    current_model = resolved_model or requested_model
    probe_result = await _probe_web_search(api_url, api_key, current_model)
    if probe_result["status"] == "ok":
        if current_model != requested_model:
            probe_result["fallback_model"] = current_model
            probe_result["requested_model"] = requested_model
            probe_result["message"] = f"真实搜索探针成功（已从 {requested_model} 回退到 {current_model}）。"
        return probe_result

    if not _is_model_unavailable_check(probe_result):
        return probe_result

    for candidate in _fallback_candidates_for_model(requested_model, current_model, available_models):
        retry_result = await _probe_web_search(api_url, api_key, candidate)
        if retry_result["status"] == "ok":
            retry_result["fallback_model"] = candidate
            retry_result["requested_model"] = requested_model
            retry_result["message"] = f"真实搜索探针成功（已从 {current_model} 回退到 {candidate}）。"
            return retry_result

    return probe_result


async def _probe_web_fetch() -> dict:
    import time

    start_time = time.perf_counter()
    errors: list[str] = []

    if config.tavily_enabled and config.tavily_api_key:
        content, error = await _call_tavily_extract(_FETCH_PROBE_URL)
        if content:
            return _build_doctor_check(
                "web_fetch_probe",
                "ok",
                "真实抓取探针成功（Tavily）。",
                endpoint=f"{config.tavily_api_url.rstrip('/')}/extract",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
                provider="tavily",
            )
        if error:
            errors.append(f"Tavily: {error}")

    if config.firecrawl_api_key:
        content, error = await _call_firecrawl_scrape(_FETCH_PROBE_URL, max_retries=1)
        if content:
            return _build_doctor_check(
                "web_fetch_probe",
                "ok",
                "真实抓取探针成功（Firecrawl）。",
                endpoint=f"{config.firecrawl_api_url.rstrip('/')}/scrape",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
                provider="firecrawl",
            )
        if error:
            errors.append(f"Firecrawl: {error}")

    if not errors:
        return _build_doctor_check(
            "web_fetch_probe",
            "skipped",
            "未执行真实抓取探针。",
            skipped_reason="no_fetch_provider_configured",
        )

    return _build_doctor_check(
        "web_fetch_probe",
        "error",
        f"真实抓取探针失败: {'；'.join(errors)}",
        response_time_ms=(time.perf_counter() - start_time) * 1000,
        error_kind="probe_failed",
    )


def _build_provider_readiness_item(check: dict, *, not_ready_message: str) -> dict:
    if check["status"] == "ok":
        item = {"status": "ready", "message": check["message"], "check_id": check["check_id"]}
        reason_code = _check_reason_code(check)
        if reason_code:
            item["reason_code"] = reason_code
        return item
    if check["status"] == "skipped":
        item = {"status": "not_ready", "message": not_ready_message, "check_id": check["check_id"]}
        if check.get("skipped_reason"):
            item["skipped_reason"] = check["skipped_reason"]
        reason_code = _check_reason_code(check)
        if reason_code:
            item["reason_code"] = reason_code
        return item
    item = {"status": "degraded", "message": check["message"], "check_id": check["check_id"]}
    reason_code = _check_reason_code(check)
    if reason_code:
        item["reason_code"] = reason_code
    return item


def _build_cache_state_cause(reason_code: str, *, status: str = "degraded") -> dict:
    return {
        "check_id": "source_cache_state",
        "status": status,
        "reason_code": reason_code,
    }


def _build_get_sources_readiness(
    *,
    web_search_status: str,
    has_readable_source_session: bool,
    source_cache_summary: Optional[dict[str, int]] = None,
    based_on_checks: Optional[list[str]] = None,
    upstream_causes: Optional[list[dict]] = None,
) -> dict:
    degraded_by: list[dict] = []
    if not has_readable_source_session:
        degraded_by.append(_build_cache_state_cause(_get_sources_readiness_reason_code(source_cache_summary)))
    if web_search_status == "not_ready":
        degraded_by.extend(upstream_causes or [])

    if has_readable_source_session:
        status = "ready"
        message = "当前进程内已存在可读取的 source session 缓存。"
    else:
        status = "partial_ready" if web_search_status != "not_ready" else "not_ready"
        message = "接口可用，但当前进程内尚无可读取的 source session；需先执行成功的 web_search。"

    return {
        "status": status,
        "message": message,
        "cache_summary": source_cache_summary or _summarize_source_cache_entries([]),
        "transient": True,
        "based_on_checks": based_on_checks or [],
        "probe_scope": "cache_state",
        "degraded_by": degraded_by,
    }


def _get_sources_readiness_reason_code(source_cache_summary: Optional[dict[str, int]]) -> str:
    summary = source_cache_summary or {}
    total_sessions = summary.get("total_sessions", 0)
    error_sessions = summary.get("error_sessions", 0)
    unreadable_sessions = summary.get("unreadable_sessions", 0)
    if total_sessions == 0:
        return "empty_source_cache"
    if error_sessions == total_sessions:
        return "error_only_source_cache"
    if unreadable_sessions == total_sessions:
        return "unreadable_only_source_cache"
    return "no_readable_source_session"


def _has_readable_source_session(cache_entries: list[object]) -> bool:
    for entry in cache_entries:
        _, classification = _classify_sources_cache_entry(entry)
        if classification == "readable":
            return True
    return False


def _summarize_source_cache_entries(cache_entries: list[object]) -> dict[str, int]:
    summary = {
        "total_sessions": len(cache_entries),
        "readable_sessions": 0,
        "error_sessions": 0,
        "partial_sessions": 0,
        "unreadable_sessions": 0,
    }
    for entry in cache_entries:
        normalized_entry, classification = _classify_sources_cache_entry(entry)
        if classification == "unreadable":
            summary["unreadable_sessions"] += 1
            continue
        if classification == "error":
            summary["error_sessions"] += 1
            continue
        summary["readable_sessions"] += 1
        if normalized_entry["search_status"] == "partial":
            summary["partial_sessions"] += 1
    return summary


def _build_feature_readiness(
    checks: list[dict],
    *,
    has_readable_source_session: bool = False,
    source_cache_summary: Optional[dict[str, int]] = None,
) -> dict:
    checks_by_id = {check["check_id"]: check for check in checks}
    grok_config = checks_by_id["grok_config"]
    grok_models = checks_by_id["grok_models"]
    grok_model_selection = checks_by_id.get("grok_model_selection")
    grok_model_runtime_fallback = checks_by_id.get("grok_model_runtime_fallback")
    grok_search_probe = checks_by_id["grok_search_probe"]
    tavily_extract = checks_by_id["tavily_extract"]
    firecrawl_scrape = checks_by_id["firecrawl_scrape"]
    web_fetch_probe = checks_by_id["web_fetch_probe"]
    tavily_map = checks_by_id["tavily_map"]
    claude_context = checks_by_id["claude_code_project"]

    if grok_config["status"] != "ok":
        web_search_status = "not_ready"
        web_search_message = grok_config["message"]
    elif grok_search_probe["status"] == "ok" and grok_models["status"] == "ok":
        web_search_status = "ready"
        web_search_message = "Grok 配置完整，真实搜索探针成功。"
    elif grok_search_probe["status"] == "ok":
        web_search_status = "degraded"
        web_search_message = "真实搜索探针成功，但 /models 或模型可见性探测存在问题。"
    elif grok_search_probe["status"] == "warning":
        web_search_status = "degraded"
        web_search_message = grok_search_probe["message"]
    elif grok_search_probe["status"] == "error":
        web_search_status = "degraded"
        web_search_message = grok_search_probe["message"]
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
    if (
        web_search_status != "not_ready"
        and grok_model_runtime_fallback
        and grok_model_runtime_fallback["status"] == "warning"
    ):
        web_search_status = "degraded"
        web_search_message = grok_model_runtime_fallback["message"]

    if web_fetch_probe["status"] == "ok":
        web_fetch_status = "ready"
        web_fetch_message = web_fetch_probe["message"]
    elif web_fetch_probe["status"] == "error":
        web_fetch_status = "degraded"
        web_fetch_message = web_fetch_probe["message"]
    elif tavily_extract["status"] == "ok" and firecrawl_scrape["status"] == "ok":
        web_fetch_status = "ready"
        web_fetch_message = "Tavily 与 Firecrawl 均可用。"
    elif tavily_extract["status"] == "ok" or firecrawl_scrape["status"] == "ok":
        web_fetch_status = "degraded"
        web_fetch_message = "仅部分抓取后端已验证可用。"
    elif tavily_extract["status"] == "skipped" and firecrawl_scrape["status"] == "skipped":
        web_fetch_status = "not_ready"
        web_fetch_message = "Tavily / Firecrawl 均未配置。"
    else:
        web_fetch_status = "degraded"
        web_fetch_message = "抓取后端已配置，但当前探测未通过。"

    web_fetch_providers = {
        "verified_path": web_fetch_probe.get("provider") if web_fetch_probe["status"] == "ok" else None,
        "tavily": _build_provider_readiness_item(
            tavily_extract,
            not_ready_message="Tavily 未配置或已禁用。",
        ),
        "firecrawl": _build_provider_readiness_item(
            firecrawl_scrape,
            not_ready_message="Firecrawl 未配置。",
        ),
    }

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
    runtime_model_source = config.grok_model_source
    web_search_check_ids = [
        "grok_config",
        "grok_models",
        "grok_model_selection",
        "grok_model_runtime_fallback",
        "grok_search_probe",
    ]
    web_search_degraded_by = [
        _readiness_cause_from_check(check)
        for check in (
            grok_config,
            grok_models,
            grok_model_selection,
            grok_model_runtime_fallback,
            grok_search_probe,
        )
        if check and check["status"] in {"warning", "error"}
    ]
    get_sources_upstream_causes = [_readiness_cause_from_check(grok_config)] if grok_config["status"] != "ok" else []
    web_fetch_check_ids = ["tavily_extract", "firecrawl_scrape", "web_fetch_probe"]
    web_fetch_degraded_by = [
        _readiness_cause_from_check(check)
        for check in (tavily_extract, firecrawl_scrape, web_fetch_probe)
        if check["status"] in {"warning", "error", "skipped"} and web_fetch_status != "ready"
    ]
    web_map_degraded_by = (
        [_readiness_cause_from_check(tavily_map)]
        if web_map_status != "ready"
        else []
    )
    toggle_degraded_by = (
        [_readiness_cause_from_check(claude_context)]
        if toggle_status != "ready"
        else []
    )

    return {
        "web_search": {
            "status": web_search_status,
            "message": web_search_message,
            "based_on_checks": web_search_check_ids,
            "probe_scope": "search_runtime",
            "degraded_by": web_search_degraded_by,
            "runtime_override_active": _runtime_override_active(runtime_model_source),
            "runtime_model_source": runtime_model_source,
        },
        "get_sources": _build_get_sources_readiness(
            web_search_status=web_search_status,
            has_readable_source_session=has_readable_source_session,
            source_cache_summary=source_cache_summary,
            based_on_checks=web_search_check_ids,
            upstream_causes=get_sources_upstream_causes,
        ),
        "web_fetch": {
            "status": web_fetch_status,
            "message": web_fetch_message,
            "providers": web_fetch_providers,
            "based_on_checks": web_fetch_check_ids,
            "probe_scope": "fetch_runtime",
            "degraded_by": web_fetch_degraded_by,
        },
        "web_map": {
            "status": web_map_status,
            "message": web_map_message,
            "based_on_checks": ["tavily_map"],
            "probe_scope": "map_runtime",
            "degraded_by": web_map_degraded_by,
        },
        "toggle_builtin_tools": {
            "status": toggle_status,
            "message": claude_context["message"],
            "client_specific": True,
            "based_on_checks": ["claude_code_project"],
            "probe_scope": "client_context",
            "degraded_by": toggle_degraded_by,
        },
    }


def _feature_affects_overall_doctor_status(item: dict) -> bool:
    return not item.get("client_specific", False) and not item.get("transient", False)


def _build_doctor_payload(
    checks: list[dict],
    feature_readiness: dict,
    recommendations: list[str],
    recommendation_details: list[dict],
) -> dict:
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
        "recommendations_detail": recommendation_details,
    }


def _render_config_info_payload(config_info: dict, *, detail: str) -> dict:
    if detail == "full":
        return config_info

    if detail == "summary":
        base_snapshot = {
            key: value
            for key, value in config_info.items()
            if key not in {"connection_test", "doctor", "feature_readiness"}
        }
        doctor = config_info.get("doctor") or {}
        summarized_doctor = {
            "status": doctor.get("status"),
            "summary": doctor.get("summary"),
            "recommendations": doctor.get("recommendations", []),
        }
        return {
            key: value
            for key, value in {
                **base_snapshot,
                "connection_test": config_info.get("connection_test"),
                "doctor": summarized_doctor,
                "feature_readiness": config_info.get("feature_readiness"),
            }.items()
        }

    return {
        "error": "invalid_detail",
        "message": "Invalid detail value. Supported values are 'full' and 'summary'.",
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
        - Use `detail=summary` for a compact machine-readable snapshot; keep the default `detail=full` for complete doctor/probe output.
        - Optional provider probes only run when their configuration is present.
        - The `/models` connection test timeout is 10 seconds; additional real `search/fetch` probes may take longer.
    """,
    meta={"version": "1.4.0", "author": "guda.studio"},
)
async def get_config_info(
    detail: Annotated[str, "Response detail level: full | summary. Defaults to full."] = "full",
) -> str:
    import json

    normalized_detail = (detail or "full").strip().lower() or "full"
    if normalized_detail not in {"full", "summary"}:
        return json.dumps(
            {
                "error": "invalid_detail",
                "message": "Invalid detail value. Supported values are 'full' and 'summary'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    config_info = config.get_config_info()
    checks: list[dict] = []
    recommendations: list[str] = []
    recommendation_details: list[dict] = []

    try:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
        checks.append(_build_doctor_check("grok_config", "ok", "Grok 核心配置已提供。"))
    except ValueError as exc:
        api_url = ""
        api_key = ""
        checks.append(_build_doctor_check("grok_config", "error", str(exc), error_kind="config_error"))
        _append_recommendation(
            recommendations,
            "先配置 GROK_API_URL 与 GROK_API_KEY，再重新运行 get_config_info。",
            recommendation_details=recommendation_details,
            check_id="grok_config",
            feature="web_search",
            severity="error",
        )

    if api_url:
        if api_url.rstrip("/").endswith("/v1"):
            checks.append(_build_doctor_check("grok_api_url_format", "ok", "GROK_API_URL 已显式包含 /v1。"))
        else:
            checks.append(_build_doctor_check("grok_api_url_format", "warning", "GROK_API_URL 未显式包含 /v1。"))
            _append_recommendation(
                recommendations,
                "将 GROK_API_URL 改为显式包含 /v1 的 OpenAI-compatible 根路径。",
                recommendation_details=recommendation_details,
                check_id="grok_api_url_format",
                feature="web_search",
            )

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
            _append_recommendation(
                recommendations,
                "检查 Grok 中转站是否支持 /models，并确认 API Key 与 URL 可达。",
                recommendation_details=recommendation_details,
                check_id="grok_models",
                feature="web_search",
                severity="error",
            )
    else:
        grok_models = _build_doctor_check(
            "grok_models",
            "error",
            "Grok 核心配置缺失，无法执行 /models 探测。",
            error_kind="config_error",
        )

    checks.append(grok_models)
    available_models: list[str] = []
    if grok_models["status"] == "ok":
        configured_model = config.grok_model
        runtime_model_source = config.grok_model_source
        runtime_model_source_label = _runtime_model_source_label(runtime_model_source)
        available_models = grok_models.get("available_models") or []
        resolved_model, resolution = _resolve_model_against_available_models(configured_model, available_models)
        fallback_model = resolved_model if resolution == _MODEL_FALLBACK_WARNING else None
        if configured_model and available_models and configured_model not in available_models:
            if fallback_model:
                warning_message = (
                    f"当前配置模型 {configured_model} 不在 /models 返回列表中；运行时将回退到 {fallback_model}。"
                )
            else:
                warning_message = f"当前配置模型 {configured_model} 不在 /models 返回列表中。"
            checks.append(
                _build_doctor_check(
                    "grok_model_selection",
                    "warning",
                    warning_message,
                    reason_code="configured_model_unavailable",
                    configured_model=configured_model,
                    runtime_model_source=runtime_model_source,
                    runtime_model_source_label=runtime_model_source_label,
                    fallback_model=fallback_model,
                    available_models=available_models,
                )
            )
            available_preview = ", ".join(available_models[:5])
            if runtime_model_source in {"process_env", "project_env_local", "project_env"}:
                if fallback_model:
                    recommendation = (
                        f"当前活动模型 {configured_model} 来自{runtime_model_source_label}，它不在 /models 返回列表中；"
                        f"运行时会先回退到 {fallback_model}。为避免长期漂移，请尽快修改或删除该覆盖。"
                        f"单独调用 switch_model 只会写入持久化配置，不会改变当前进程。"
                    )
                else:
                    recommendation = (
                        f"当前活动模型 {configured_model} 来自{runtime_model_source_label}，但它不在 /models 返回列表中；"
                        f"请先修改或删除该覆盖。单独调用 switch_model 只会写入持久化配置，不会改变当前进程。"
                        f"可切换到例如：{available_preview}。"
                    )
            else:
                if fallback_model:
                    recommendation = (
                        f"当前配置模型 {configured_model} 不在 /models 返回列表中；运行时会先回退到 {fallback_model}。"
                        f"建议将 GROK_MODEL 或持久化模型更新为该可用模型，避免继续依赖隐式回退。"
                    )
                else:
                    recommendation = (
                        f"将 GROK_MODEL 或本地持久化模型从 {configured_model} 切换到 /models 返回的可用模型，"
                        f"例如：{available_preview}。"
                    )
            _append_recommendation(
                recommendations,
                recommendation,
                recommendation_details=recommendation_details,
                check_id="grok_model_selection",
                feature="web_search",
                extra_detail_fields={
                    "runtime_model_source": runtime_model_source,
                    "runtime_model_source_label": runtime_model_source_label,
                    "fallback_model": fallback_model,
                },
            )
        probe_model = resolved_model or configured_model
    else:
        probe_model = config.grok_model
    if api_url and api_key:
        grok_search_probe = await _probe_web_search_with_fallback(api_url, api_key, probe_model, available_models)
        if grok_search_probe.get("fallback_model"):
            fallback_model = grok_search_probe["fallback_model"]
            runtime_model_source = config.grok_model_source
            runtime_model_source_label = _runtime_model_source_label(runtime_model_source)
            checks.append(
                _build_doctor_check(
                    "grok_model_runtime_fallback",
                    "warning",
                    f"真实搜索探针已从 {probe_model} 回退到 {fallback_model}。",
                    reason_code="runtime_model_fallback",
                    configured_model=probe_model,
                    fallback_model=fallback_model,
                    runtime_model_source=runtime_model_source,
                    runtime_model_source_label=runtime_model_source_label,
                )
            )
            if runtime_model_source in {"process_env", "project_env_local", "project_env"}:
                recommendation = (
                    f"当前真实搜索探针需要从 {probe_model} 回退到 {fallback_model} 才能成功；"
                    f"当前活动模型来自{runtime_model_source_label}。请先修改或删除该覆盖。"
                    f"单独调用 switch_model 只会写入持久化配置，不会改变当前进程。"
                )
            else:
                recommendation = (
                    f"当前真实搜索探针需要从 {probe_model} 回退到 {fallback_model} 才能成功；"
                    f"建议尽快将 GROK_MODEL 或持久化模型更新到该可用模型，避免继续依赖运行时回退。"
                )
            _append_recommendation(
                recommendations,
                recommendation,
                recommendation_details=recommendation_details,
                check_id="grok_model_runtime_fallback",
                feature="web_search",
                extra_detail_fields={
                    "runtime_model_source": runtime_model_source,
                    "runtime_model_source_label": runtime_model_source_label,
                    "fallback_model": fallback_model,
                },
            )
        if grok_search_probe["status"] != "ok":
            _append_recommendation(
                recommendations,
                "检查 chat/completions 是否真实可用，并确认当前默认模型能返回可解析正文。",
                recommendation_details=recommendation_details,
                check_id="grok_search_probe",
                feature="web_search",
                severity="error",
            )
    else:
        grok_search_probe = _build_doctor_check(
            "grok_search_probe",
            "skipped",
            "未执行真实搜索探针。",
            skipped_reason="missing_grok_config",
        )
    checks.append(grok_search_probe)

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
            payload_error = _validate_tavily_extract_probe_payload(tavily_extract_data)
            if payload_error:
                tavily_extract["status"] = "error"
                tavily_extract["message"] = payload_error
                tavily_extract["error_kind"] = "invalid_response_shape"
                _append_recommendation(
                    recommendations,
                    "检查 Tavily extract 是否返回了登录页、HTML 页面或异常 JSON 结构。",
                    recommendation_details=recommendation_details,
                    check_id="tavily_extract",
                    feature="web_fetch",
                    severity="error",
                )
            else:
                result_count = len((tavily_extract_data or {}).get("results", []) or [])
                tavily_extract["message"] = f"Tavily extract 探测成功，返回 {result_count} 条结果。"
        else:
            _append_recommendation(
                recommendations,
                "检查 TAVILY_API_KEY / TAVILY_API_URL，确认 Tavily extract 端点可达。",
                recommendation_details=recommendation_details,
                check_id="tavily_extract",
                feature="web_fetch",
                severity="error",
            )

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
            payload_error = _validate_tavily_map_probe_payload(tavily_map_data)
            if payload_error:
                tavily_map["status"] = "error"
                tavily_map["message"] = payload_error
                tavily_map["error_kind"] = "invalid_response_shape"
                _append_recommendation(
                    recommendations,
                    "检查 Tavily map 是否返回了异常 JSON 结构。",
                    recommendation_details=recommendation_details,
                    check_id="tavily_map",
                    feature="web_map",
                    severity="error",
                )
            else:
                result_count = len((tavily_map_data or {}).get("results", []) or [])
                tavily_map["message"] = f"Tavily map 探测成功，返回 {result_count} 条结果。"
        else:
            _append_recommendation(
                recommendations,
                "检查 TAVILY_API_KEY / TAVILY_API_URL，确认 Tavily map 端点可达。",
                recommendation_details=recommendation_details,
                check_id="tavily_map",
                feature="web_map",
                severity="error",
            )
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
        _append_recommendation(
            recommendations,
            "若需要 web_map 或 Tavily-first web_fetch，请配置并启用 Tavily。",
            recommendation_details=recommendation_details,
            check_id="tavily_extract",
            feature="web_fetch",
        )
        _append_recommendation(
            recommendations,
            "若需要 web_map，请配置并启用 Tavily。",
            recommendation_details=recommendation_details,
            check_id="tavily_map",
            feature="web_map",
        )
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
            has_markdown = bool(_extract_firecrawl_markdown_payload(firecrawl_data or {}).strip())
            if has_markdown:
                firecrawl_scrape["message"] = "Firecrawl scrape 探测成功。"
            else:
                firecrawl_scrape["status"] = "warning"
                firecrawl_scrape["message"] = "Firecrawl scrape 已响应，但 markdown 为空。"
                _append_recommendation(
                    recommendations,
                    "检查 Firecrawl scrape 返回内容是否为空，避免 web_fetch 被误判为 fully ready。",
                    recommendation_details=recommendation_details,
                    check_id="firecrawl_scrape",
                    feature="web_fetch",
                )
        else:
            _append_recommendation(
                recommendations,
                "检查 FIRECRAWL_API_KEY / FIRECRAWL_API_URL，确认 Firecrawl scrape 端点可达。",
                recommendation_details=recommendation_details,
                check_id="firecrawl_scrape",
                feature="web_fetch",
                severity="error",
            )
    else:
        firecrawl_scrape = _build_doctor_check(
            "firecrawl_scrape",
            "skipped",
            "未执行 Firecrawl scrape 探测。",
            skipped_reason="FIRECRAWL_API_KEY 未配置",
        )
        _append_recommendation(
            recommendations,
            "若需要 Firecrawl fallback，请配置 FIRECRAWL_API_KEY。",
            recommendation_details=recommendation_details,
            check_id="firecrawl_scrape",
            feature="web_fetch",
        )
    checks.append(firecrawl_scrape)
    web_fetch_probe = await _probe_web_fetch()
    if web_fetch_probe["status"] == "error":
        _append_recommendation(
            recommendations,
            "检查真实 web_fetch 路径是否能抓到最小页面正文，并确认提取结果不是登录页、空内容或异常结构。",
            recommendation_details=recommendation_details,
            check_id="web_fetch_probe",
            feature="web_fetch",
            severity="error",
        )
    checks.append(web_fetch_probe)

    claude_project_root = _find_git_root()
    claude_context_status = "ok" if claude_project_root else "skipped"
    checks.append(
        _build_doctor_check(
            "claude_code_project",
            claude_context_status,
            "已检测到 Claude Code 项目级 Git 上下文。" if claude_context_status == "ok" else "未检测到项目级 Git 上下文。",
            skipped_reason="" if claude_context_status == "ok" else "missing_git_context",
        )
    )

    source_cache_entries = await _SOURCES_CACHE.snapshot()
    source_cache_summary = _summarize_source_cache_entries(source_cache_entries)
    feature_readiness = _build_feature_readiness(
        checks,
        has_readable_source_session=_has_readable_source_session(source_cache_entries),
        source_cache_summary=source_cache_summary,
    )
    doctor = _build_doctor_payload(checks, feature_readiness, recommendations, recommendation_details)
    config_info["connection_test"] = _build_connection_test_from_models_check(grok_models)
    config_info["doctor"] = doctor
    config_info["feature_readiness"] = feature_readiness

    return json.dumps(
        _render_config_info_payload(config_info, detail=normalized_detail),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="switch_model",
    output_schema=None,
    description="""
    Switches the default Grok model used for Grok-backed search and runtime model selection, persisting the setting.

    **Key Features:**
        - **Model Selection:** Change the AI model used for Grok-backed web search and related diagnostics.
        - **Persistent Storage:** Model preference saved to ~/.config/grok-search/config.json.
        - **Runtime Awareness:** Reports when higher-priority env or project overrides keep the current process on a different active model.

    **Edge Cases & Best Practices:**
        - Use get_config_info to verify available models before switching.
        - If the active model currently comes from process env or project `.env.local` / `.env`, this tool updates persisted config only and does not change the current process immediately.
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
        previous_model_source = config.grok_model_source
        config.set_model(model)
        current_model = config.grok_model
        current_model_source = config.grok_model_source
        current_model_source_label = _runtime_model_source_label(current_model_source)

        if current_model_source in {"process_env", "project_env_local", "project_env"}:
            message = (
                f"模型已写入持久化配置，但当前活动模型仍为 {current_model}；"
                f"它来自{current_model_source_label}。请先修改或删除该覆盖，"
                f"单独调用 switch_model 不会改变当前进程。"
            )
        else:
            message = f"模型已从 {previous_model} 切换到 {current_model}"

        result = {
            "status": "成功",
            "previous_model": previous_model,
            "previous_model_source": previous_model_source,
            "current_model": current_model,
            "runtime_model_source": current_model_source,
            "runtime_model_source_label": current_model_source_label,
            "message": message,
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

    def build_error(message: str, *, file_path: str = "", error_code: str) -> str:
        return json.dumps({
            "blocked": False,
            "deny_list": [],
            "file": file_path,
            "message": message,
            "error": error_code,
        }, ensure_ascii=False, indent=2)

    root = _find_git_root()
    if root is None:
        return build_error(
            "未检测到项目级 Git 根目录，无法修改 Claude Code 项目设置",
            error_code="git_root_not_found",
        )

    settings_path = root / ".claude" / "settings.json"
    tools = ["WebFetch", "WebSearch"]

    # Load or initialize
    if settings_path.exists():
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return build_error(
                f"无法读取 Claude Code 项目设置: {str(exc)}",
                file_path=str(settings_path),
                error_code="settings_file_invalid",
            )
    else:
        settings = {"permissions": {"deny": []}}

    if not isinstance(settings, dict):
        return build_error(
            "Claude Code 项目设置格式无效: 顶层对象必须是 JSON object",
            file_path=str(settings_path),
            error_code="settings_file_invalid",
        )

    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        return build_error(
            "Claude Code 项目设置格式无效: permissions 必须是对象",
            file_path=str(settings_path),
            error_code="settings_file_invalid",
        )

    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        return build_error(
            "Claude Code 项目设置格式无效: permissions.deny 必须是数组",
            file_path=str(settings_path),
            error_code="settings_file_invalid",
        )

    blocked = all(t in deny for t in tools)

    # Execute action
    if action in ["on", "enable"]:
        for t in tools:
            if t not in deny:
                deny.append(t)
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            return build_error(
                f"写入 Claude Code 项目设置失败: {str(exc)}",
                file_path=str(settings_path),
                error_code="settings_write_failed",
            )
        msg = "官方工具已禁用"
        blocked = True
    elif action in ["off", "disable"]:
        deny[:] = [t for t in deny if t not in tools]
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            return build_error(
                f"写入 Claude Code 项目设置失败: {str(exc)}",
                file_path=str(settings_path),
                error_code="settings_write_failed",
            )
        msg = "官方工具已启用"
        blocked = False
    elif action not in ["status"]:
        return build_error(
            "无效 action: 仅支持 on、off 或 status",
            file_path=str(settings_path),
            error_code="invalid_action",
        )
    else:
        msg = f"官方工具当前{'已禁用' if blocked else '已启用'}"

    return json.dumps({
        "blocked": blocked,
        "deny_list": deny,
        "file": str(settings_path),
        "message": msg,
        "error": None,
    }, ensure_ascii=False, indent=2)


def _get_planning_sub_queries(session) -> list[dict]:
    record = session.phases.get("query_decomposition")
    if not record or not isinstance(record.data, list):
        return []
    return [item for item in record.data if isinstance(item, dict)]


def _get_planning_sub_query_ids(session) -> set[str]:
    return {
        item["id"].strip()
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
    sub_query_id = item["id"].strip()
    valid_dependency_ids = {sub_query_id} if is_revision else existing_ids

    if is_revision and any(phase in session.phases for phase in ("search_strategy", "tool_selection", "execution_order")):
        return _planning_validation_message(
            "Sub-query revision would invalidate downstream phases. Open a new session to restart planning from query_decomposition.",
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
    normalized_sub_query_id = sub_query_id.strip()
    if normalized_sub_query_id not in existing_ids:
        return _planning_validation_message(
            f"Unknown sub-query id: {normalized_sub_query_id}",
            field_name,
        )
    return None


def _validate_search_strategy_coverage(session) -> str | None:
    missing_ids = sorted(session.missing_search_term_ids())
    if missing_ids:
        return _planning_validation_message(
            f"Missing search term for sub-query ids: {', '.join(missing_ids)}",
            "purpose",
        )
    return None


def _validate_tool_mapping_item(session, sub_query_id: str, is_revision: bool = False) -> str | None:
    if not is_revision and sub_query_id in session.tool_mapping_ids():
        return _planning_validation_message(
            f"Duplicate tool mapping for sub-query id: {sub_query_id}",
            "sub_query_id",
        )
    return None


def _validate_tool_mapping_coverage(session) -> str | None:
    missing_ids = sorted(session.missing_tool_mapping_ids())
    if missing_ids:
        return _planning_validation_message(
            f"Missing tool mapping for sub-query ids: {', '.join(missing_ids)}",
            "sub_query_id",
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
            f"{phase} revision would invalidate downstream phases. Open a new session to restart planning from {phase}.",
            "is_revision",
        )
    return None


def _validate_singleton_phase_overwrite(session, phase: str, is_revision: bool) -> str | None:
    if is_revision or phase not in session.phases:
        return None
    return _planning_validation_message(
        f"{phase} already exists for this session. Set is_revision=true to replace it explicitly.",
        "is_revision",
    )


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
    if session:
        overwrite_error = _validate_singleton_phase_overwrite(session, "intent_analysis", is_revision)
        if overwrite_error:
            return overwrite_error
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
    overwrite_error = _validate_singleton_phase_overwrite(session, "complexity_assessment", is_revision)
    if overwrite_error:
        return overwrite_error
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
    tool_hint: Annotated[Optional[Literal["web_search", "web_fetch", "web_map"]], "web_search | web_fetch | web_map"] = None,
    is_revision: Annotated[bool, "True to replace all sub-queries"] = False,
) -> str:
    import json
    if not planning_engine.get_session(session_id):
        return _planning_session_error(session_id)
    normalized_id = id.strip()
    item = {"id": normalized_id, "goal": goal, "expected_output": expected_output, "boundary": boundary}
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
    description="Phase 4: Add one search term. Call once per term; data accumulates. First call must set approach. Later non-revision calls append search_terms only and do not overwrite existing approach/fallback_plan; use is_revision=true to replace the strategy.",
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
    if any(phase in session.phases for phase in ("tool_selection", "execution_order")):
        return _planning_validation_message(
            "Search strategy mutation would invalidate downstream phases. Open a new session to rebuild search_strategy.",
            "is_revision",
        )
    if (is_revision or "search_strategy" not in session.phases) and not approach:
        return _planning_validation_error(
            "first_search_term_requires_approach",
            "The first search term must include approach=broad_first|narrow_first|targeted.",
        )
    normalized_purpose = purpose.strip()
    data = {"search_terms": [{"term": term, "purpose": normalized_purpose, "round": round}]}
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
    validation_error = _validate_sub_query_reference(session, normalized_purpose, "purpose")
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
            "Tool mapping revision would invalidate execution_order. Open a new session to rebuild tool_selection.",
            "sub_query_id",
        )
    normalized_sub_query_id = sub_query_id.strip()
    item = {"sub_query_id": normalized_sub_query_id, "tool": tool, "reason": reason}
    if params_json:
        try:
            parsed_params = json.loads(params_json)
        except json.JSONDecodeError:
            return _planning_validation_error(
                "validation_error",
                "Invalid tool mapping input.",
                [{"field": "params_json", "message": "params_json must be valid JSON.", "type": "json_invalid"}],
            )
        if parsed_params is None:
            parsed_params = None
        elif not isinstance(parsed_params, dict):
            return _planning_validation_error(
                "validation_error",
                "Invalid tool mapping input.",
                [{"field": "params_json", "message": "params_json must decode to a JSON object.", "type": "dict_type"}],
            )
        if parsed_params is not None:
            item["params"] = parsed_params
    try:
        ToolPlanItem(**item)
    except ValidationError as exc:
        if any(detail["type"] == "literal_error" for detail in _format_validation_details(exc)):
            return _planning_validation_error("invalid_tool", "tool must be one of web_search, web_fetch, web_map.")
        return _planning_validation_error("validation_error", "Invalid tool mapping input.", _format_validation_details(exc))
    validation_error = _validate_sub_query_reference(session, normalized_sub_query_id, "sub_query_id")
    if validation_error:
        return validation_error
    validation_error = _validate_tool_mapping_item(session, normalized_sub_query_id, is_revision=is_revision)
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
    overwrite_error = _validate_singleton_phase_overwrite(session, "execution_order", is_revision)
    if overwrite_error:
        return overwrite_error
    try:
        ExecutionOrderOutput(parallel=parallel, sequential=seq, estimated_rounds=estimated_rounds)
    except ValidationError as exc:
        return _planning_validation_error("validation_error", "Invalid execution plan input.", _format_validation_details(exc))
    if "tool_selection" in session.phases or "execution_order" in session.phases:
        validation_error = _validate_search_strategy_coverage(session)
        if validation_error:
            return validation_error
        validation_error = _validate_tool_mapping_coverage(session)
        if validation_error:
            return validation_error
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

    exit_code = 0
    try:
        mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        pass
    except Exception:
        exit_code = 1
    finally:
        os._exit(exit_code)


if __name__ == "__main__":
    main()
