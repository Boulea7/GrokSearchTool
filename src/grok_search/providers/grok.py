import copy
import httpx
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from ipaddress import ip_address
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base
from .base import BaseSearchProvider
from ..sources import merge_sources, sanitize_answer_text, split_answer_and_sources
from ..utils import search_prompt, fetch_prompt, url_describe_prompt, rank_sources_prompt
from ..logger import log_info
from ..config import config


def get_local_time_info() -> str:
    """获取本地时间信息，用于注入到搜索查询中"""
    try:
        # 尝试获取系统本地时区
        local_tz = datetime.now().astimezone().tzinfo
        local_now = datetime.now(local_tz)
    except Exception:
        # 降级使用 UTC
        local_now = datetime.now(timezone.utc)

    # 格式化时间信息
    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays_cn[local_now.weekday()]

    return (
        f"[Current Time Context]\n"
        f"- Date: {local_now.strftime('%Y-%m-%d')} ({weekday})\n"
        f"- Time: {local_now.strftime('%H:%M:%S')}\n"
        f"- Timezone: {local_now.tzname() or 'Local'}\n"
    )


def _needs_time_context(query: str) -> bool:
    """检查查询是否需要时间上下文"""
    # 中文时间相关关键词
    cn_keywords = [
        "当前", "现在", "今天", "明天", "昨天",
        "本周", "上周", "下周", "这周",
        "本月", "上月", "下月", "这个月",
        "今年", "去年", "明年",
        "最新", "最近", "近期", "刚刚", "刚才",
        "实时", "即时", "目前",
    ]
    # 英文时间相关关键词
    en_keywords = [
        "current", "now", "today", "tomorrow", "yesterday",
        "this week", "last week", "next week",
        "this month", "last month", "next month",
        "this year", "last year", "next year",
        "latest", "recent", "recently", "just now",
        "real-time", "up-to-date",
    ]

    query_lower = query.lower()

    for keyword in cn_keywords:
        if keyword in query:
            return True

    for keyword in en_keywords:
        if re.search(rf"\b{re.escape(keyword)}\b", query_lower):
            return True

    return False


_SENSITIVE_CITATION_URL_PARAM_KEYS = {
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


def _sanitize_citation_url(value: str) -> str:
    if not isinstance(value, str):
        return ""

    url = value.strip()
    if not url:
        return ""

    split = urlsplit(url)
    if split.scheme.lower() not in {"http", "https"} or not split.netloc:
        return ""
    if not split.username and not split.password and not split.query and not split.fragment:
        return url

    hostname = split.hostname or ""
    if not hostname:
        return ""

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
    elif ":" in hostinfo:
        raw_port = hostinfo.rsplit(":", 1)[-1]

    netloc = f"{host}:{raw_port}" if raw_port else host
    query = urlencode(
        [
            (key, "REDACTED" if key.lower() in _SENSITIVE_CITATION_URL_PARAM_KEYS else value)
            for key, value in parse_qsl(split.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    fragment = split.fragment
    if fragment and any(token in fragment for token in ("=", "&")):
        fragment = urlencode(
            [
                (key, "REDACTED" if key.lower() in _SENSITIVE_CITATION_URL_PARAM_KEYS else value)
                for key, value in parse_qsl(fragment, keep_blank_values=True)
            ],
            doseq=True,
        )

    return urlunsplit((split.scheme, netloc, split.path, query, fragment))

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_IGNORED_CONTENT_BLOCK_TYPES = {
    "reasoning",
    "thinking",
    "analysis",
    "thought",
    "tool_call",
    "tool",
    "function_call",
    "function",
    "metadata",
    "usage",
}
_MODEL_UNAVAILABLE_MARKERS = (
    "no available channel for model",
    "unsupported model",
    "invalid model",
    "model not found",
    "model is not available",
    "model unavailable",
    "no model named",
)
_DEFAULT_GROK_MODEL_FALLBACKS = {
    "grok-4.20-0309": [
        "grok-4.20-fast",
        "grok-4.20-expert",
        "grok-4.20-0309-non-reasoning",
        "grok-4.20-0309-reasoning",
        "grok-4.1-fast",
        "grok-4.1-expert",
        "grok-4.1-mini",
        "grok-4.1-thinking",
    ],
    "grok-4.20-0309-reasoning": [
        "grok-4.20-0309-non-reasoning",
        "grok-4.20-expert",
        "grok-4.20-fast",
        "grok-4.1-thinking",
        "grok-4.1-expert",
        "grok-4.1-fast",
        "grok-4.1-mini",
    ],
    "grok-4.20-expert": [
        "grok-4.20-fast",
        "grok-4.20-0309",
        "grok-4.20-0309-non-reasoning",
        "grok-4.1-expert",
        "grok-4.1-fast",
        "grok-4.1-thinking",
        "grok-4.1-mini",
    ],
    "grok-4.20-fast": [
        "grok-4.20-0309",
        "grok-4.20-expert",
        "grok-4.20-0309-non-reasoning",
        "grok-4.1-fast",
        "grok-4.1-mini",
        "grok-4.1-expert",
        "grok-4.1-thinking",
    ],
    "grok-4.20-auto": [
        "grok-4.20-0309",
        "grok-4.20-fast",
        "grok-4.20-expert",
        "grok-4.1-fast",
        "grok-4.1-mini",
    ],
    "grok-4.20-reasoning": [
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309",
        "grok-4.20-fast",
        "grok-4.1-thinking",
        "grok-4.1-fast",
    ],
    "grok-4.20-multi-agent": [
        "grok-4.20-heavy-16-agent",
        "grok-4.20-expert-4-agent",
        "grok-4.20-reasoning",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309",
        "grok-4.20-fast",
        "grok-4.1-fast",
    ],
    "grok-4.20-expert-4-agent": [
        "grok-4.20-multi-agent",
        "grok-4.20-heavy-16-agent",
        "grok-4.20-reasoning",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309",
    ],
    "grok-4.20-heavy-16-agent": [
        "grok-4.20-multi-agent",
        "grok-4.20-expert-4-agent",
        "grok-4.20-reasoning",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309",
    ],
}

_RESPONSES_ONLY_RELAY_MODELS = {
    "grok-4.20-reasoning",
    "grok-4.20-multi-agent",
    "grok-4.20-export-4-agent",
    "grok-4.20-heavy-16-agent",
}


def _is_retryable_exception(exc) -> bool:
    """检查异常是否可重试"""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


def _is_model_unavailable_exception(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    message = ""
    try:
        payload = exc.response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", ""))
            elif error is not None:
                message = str(error)
    except Exception:
        message = exc.response.text
    lowered = message.strip().lower()
    return any(marker in lowered for marker in _MODEL_UNAVAILABLE_MARKERS)


def _provider_model_candidates(model: str, api_url: str) -> list[str]:
    raw_model = (model or "").strip()
    model_prefix = ""
    model_suffix = ""
    normalized_core = raw_model
    if "/" in normalized_core:
        model_prefix, normalized_core = normalized_core.split("/", 1)
        model_prefix = f"{model_prefix}/"
    if ":" in normalized_core:
        normalized_core, trailing = normalized_core.split(":", 1)
        model_suffix = f":{trailing}"
    raw_override = (config._get_env_value("GROK_MODEL_FALLBACKS", "") or "").strip()
    if raw_override:
        configured = [item.strip() for item in raw_override.split(",") if item.strip()]
    else:
        configured = _DEFAULT_GROK_MODEL_FALLBACKS.get(normalized_core, [])
        if not configured and normalized_core.startswith("grok-4.20"):
            configured = [
                "grok-4.20-0309",
                "grok-4.20-fast",
                "grok-4.1-fast",
                "grok-4.1-expert",
                "grok-4.1-mini",
                "grok-4.1-thinking",
            ]
    candidates = [raw_model]
    seen = {raw_model}
    for item in configured:
        candidate = f"{model_prefix}{item}"
        normalized = config._apply_model_suffix_for_url(candidate, api_url)
        if model_suffix and ":" not in normalized:
            normalized = f"{normalized}{model_suffix}"
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(normalized)
    return candidates


def _httpx_client_kwargs_for_url(url: str, *, timeout: httpx.Timeout) -> dict:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    kwargs = {"timeout": timeout, "follow_redirects": True}
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError:
            is_loopback = host.startswith("127.")
    if is_loopback:
        kwargs["trust_env"] = False
    return kwargs


class _WaitWithRetryAfter(wait_base):
    """等待策略：优先使用 Retry-After 头，否则使用指数退避"""

    def __init__(self, multiplier: float, max_wait: int):
        self._base_wait = wait_random_exponential(multiplier=multiplier, max=max_wait)
        self._protocol_error_base = 3.0

    def __call__(self, retry_state):
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = self._parse_retry_after(exc.response)
                if retry_after is not None:
                    return retry_after
            if isinstance(exc, httpx.RemoteProtocolError):
                return self._base_wait(retry_state) + self._protocol_error_base
        return self._base_wait(retry_state)

    def _parse_retry_after(self, response: httpx.Response) -> Optional[float]:
        """解析 Retry-After 头（支持秒数或 HTTP 日期格式）"""
        header = response.headers.get("Retry-After")
        if not header:
            return None
        header = header.strip()

        if header.isdigit():
            return float(header)

        try:
            retry_dt = parsedate_to_datetime(header)
            if retry_dt.tzinfo is None:
                retry_dt = retry_dt.replace(tzinfo=timezone.utc)
            delay = (retry_dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delay)
        except (TypeError, ValueError):
            return None


class GrokSearchProvider(BaseSearchProvider):
    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = "grok-4.20-0309",
        fallback_providers: list[dict[str, Any]] | None = None,
    ):
        super().__init__(api_url, api_key)
        self.model = model
        self._last_completion_sources: list[dict] = []
        self._last_success_provider_name: str = "primary"
        self._last_success_provider_model: str = model
        self._last_success_provider_api_url: str = api_url.rstrip("/")
        self._provider_chain = [
            {"name": "primary", "api_url": api_url, "api_key": api_key, "model": model},
            *[
                {
                    "name": item.get("name", f"fallback_{index}"),
                    "api_url": item["api_url"],
                    "api_key": item["api_key"],
                    "model": item.get("model", model),
                }
                for index, item in enumerate(fallback_providers or [], start=1)
                if item.get("api_url") and item.get("api_key")
            ],
        ]

    @staticmethod
    def _model_core(model: str) -> str:
        text = (model or "").strip().lower()
        if "/" in text:
            text = text.split("/", 1)[1]
        if ":" in text:
            text = text.split(":", 1)[0]
        return text

    def _provider_family_for_url(self, api_url: str) -> str:
        return config.provider_family_for_url(api_url)

    def _uses_multi_agent_family(self, model: str) -> bool:
        core = self._model_core(model)
        return any(token in core for token in ("multi-agent", "heavy-16-agent", "expert-4-agent"))

    def _prefers_responses_endpoint(self, api_url: str, model: str) -> bool:
        core = self._model_core(model)
        provider_family = self._provider_family_for_url(api_url)
        if provider_family == "official_xai" and self._uses_multi_agent_family(core):
            return True
        if provider_family in {"openai_compatible_relay", "grok2api_like"} and core in _RESPONSES_ONLY_RELAY_MODELS:
            return True
        return self._uses_multi_agent_family(core)

    def _prepare_request_for_endpoint(
        self,
        provider_config: dict[str, Any],
        payload: dict[str, Any],
        candidate_model: str,
    ) -> tuple[str, dict[str, Any]]:
        attempt_payload = copy.deepcopy(payload)
        attempt_payload["model"] = candidate_model
        if self._prefers_responses_endpoint(provider_config["api_url"], candidate_model):
            if "input" not in attempt_payload and "messages" in attempt_payload:
                attempt_payload["input"] = attempt_payload.pop("messages")
            endpoint = f"{provider_config['api_url']}/responses"
            return endpoint, attempt_payload
        endpoint = f"{provider_config['api_url']}/chat/completions"
        return endpoint, attempt_payload

    def get_provider_name(self) -> str:
        return "Grok"

    def _build_api_headers(self) -> dict:
        return self._build_api_headers_for_key(self.api_key)

    def _build_api_headers_for_key(self, api_key: str) -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "grok-search-mcp/0.1.0",
        }

    def _iter_provider_configs(self, payload: dict) -> list[dict[str, Any]]:
        configs: list[dict[str, Any]] = []
        for item in self._provider_chain:
            config_model = item.get("model", self.model)
            effective_model = payload.get("model", config_model) or config_model
            effective_model = config._apply_model_suffix_for_url(effective_model, item["api_url"])
            configs.append(
                {
                    "name": item.get("name", "provider"),
                    "api_url": item["api_url"].rstrip("/"),
                    "api_key": item["api_key"],
                    "model": effective_model,
                }
            )
        return configs

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> str:
        body, sources = await self.search_with_sources(
            query,
            platform=platform,
            min_results=min_results,
            max_results=max_results,
            ctx=ctx,
        )
        return self._finalize_content(body, sources, render_sources=True)

    async def search_with_sources(
        self,
        query: str,
        platform: str = "",
        min_results: int = 3,
        max_results: int = 10,
        ctx=None,
    ) -> tuple[str, list[dict]]:
        headers = self._build_api_headers()
        platform_prompt = ""

        if platform:
            platform_prompt = "\n\nYou should search the web for the information you need, and focus on these platform: " + platform + "\n"

        time_context_mode = config.time_context_mode
        time_context_required = bool(getattr(self, "time_context_required", False))
        should_inject_time_context = (
            time_context_mode == "always"
            or (time_context_mode == "auto" and (_needs_time_context(query) or time_context_required))
        )
        time_context = get_local_time_info() + "\n" if should_inject_time_context else ""

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": search_prompt,
                },
                {"role": "user", "content": time_context + query + platform_prompt},
            ],
            "stream": False,
        }

        await log_info(
            ctx,
            f"search request prepared (platform={platform or 'general'}, time_context={should_inject_time_context})",
            config.debug_enabled,
        )

        if (
            "_execute_completion_with_retry" in self.__dict__
            and "_execute_completion_with_retry_result" not in self.__dict__
        ):
            self._last_completion_sources = []
            execute_completion = self._execute_completion_with_retry
            content = await execute_completion(headers, payload, ctx)
            return content, list(self._last_completion_sources)

        content, sources = await self._execute_completion_with_retry_result(
            headers,
            payload,
            ctx,
            render_sources=False,
        )
        return content, sources

    async def fetch(self, url: str, ctx=None) -> str:
        headers = self._build_api_headers()
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": fetch_prompt,
                },
                {"role": "user", "content": url + "\n获取该网页内容并返回其结构化Markdown格式" },
            ],
            "stream": False,
        }
        return await self._execute_completion_with_retry(headers, payload, ctx)

    def _flatten_text_content(self, value) -> str:
        if isinstance(value, str):
            return value

        if isinstance(value, dict):
            block_type = str(value.get("type", "")).strip().lower()
            if block_type in _IGNORED_CONTENT_BLOCK_TYPES:
                return ""
            for key in ("text", "content", "value", "output_text"):
                nested = self._flatten_text_content(value.get(key))
                if nested:
                    return nested
            return ""

        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                nested = self._flatten_text_content(item)
                if nested:
                    parts.append(nested)
            return "".join(parts)

        return ""

    def _normalize_source_items(self, data, *, origin_type: str | None = None) -> list[dict]:
        items = data if isinstance(data, list) else [data]
        normalized: list[dict] = []

        for item in items:
            if isinstance(item, str):
                normalized_url = _sanitize_citation_url(item)
                if normalized_url:
                    normalized.append({"url": normalized_url})
                continue

            if not isinstance(item, dict):
                continue

            url = item.get("url") or item.get("href") or item.get("link")
            normalized_url = _sanitize_citation_url(url)
            if not normalized_url:
                continue

            source = {"url": normalized_url}
            title = item.get("title") or item.get("name") or item.get("label")
            if isinstance(title, str) and title.strip():
                source["title"] = title.strip()

            description = (
                item.get("description")
                or item.get("snippet")
                or item.get("content")
                or item.get("text")
            )
            if isinstance(description, str) and description.strip():
                source["description"] = description.strip()

            snippet = item.get("snippet")
            if isinstance(snippet, str) and snippet.strip():
                source["snippet"] = snippet.strip()

            provider = item.get("provider")
            if isinstance(provider, str) and provider.strip():
                source["provider"] = provider.strip()
            else:
                source["provider"] = "grok"

            upstream_source = item.get("source")
            if isinstance(upstream_source, str) and upstream_source.strip():
                source["source"] = upstream_source.strip()

            published_at = item.get("published_at")
            if isinstance(published_at, str) and published_at.strip():
                source["published_at"] = published_at.strip()

            published_date = item.get("published_date")
            if isinstance(published_date, str) and published_date.strip():
                source["published_date"] = published_date.strip()

            normalized_origin_type = item.get("origin_type") or origin_type
            if isinstance(normalized_origin_type, str) and normalized_origin_type.strip():
                source["origin_type"] = normalized_origin_type.strip()

            score = item.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                source["score"] = score

            normalized.append(source)

        return normalized

    def _extract_structured_sources(self, data: dict) -> list[dict]:
        candidate_keys = (
            ("citations", "citation"),
            ("references", "reference"),
            ("sources", "source"),
            ("source_cards", "source_card"),
            ("source_card", "source_card"),
            ("annotations", "annotation"),
            ("search_results", "search_result"),
            ("searchResults", "search_result"),
            ("urls", "url_list"),
        )
        collected: list[dict] = []

        def collect_nested(value):
            if isinstance(value, dict):
                block_type = str(value.get("type", "")).strip().lower()
                if block_type in _IGNORED_CONTENT_BLOCK_TYPES:
                    return
                collect_from_mapping(value)
                for nested in value.values():
                    collect_nested(nested)
                return

            if isinstance(value, list):
                for item in value:
                    collect_nested(item)

        def collect_from_mapping(mapping):
            nonlocal collected
            if not isinstance(mapping, dict):
                return
            for key, origin_type in candidate_keys:
                if key in mapping:
                    collected = merge_sources(
                        collected,
                        self._normalize_source_items(mapping[key], origin_type=origin_type),
                    )

        if not isinstance(data, dict):
            return []

        collect_nested(data)

        return collected

    def _append_sources_block(self, content: str, sources: list[dict]) -> str:
        if not sources:
            return (content or "").strip()

        existing_content, existing_sources = split_answer_and_sources(content or "")
        if existing_sources:
            return (content or "").strip()

        lines: list[str] = []
        body = existing_content.strip()
        if body:
            lines.append(body)
            lines.append("")

        lines.append("## Sources")
        for index, source in enumerate(sources, start=1):
            title = source.get("title") or source["url"]
            lines.append(f"{index}. [{title}]({source['url']})")

        return "\n".join(lines).strip()

    def _normalize_internal_text(self, content: str) -> str:
        answer, _ = split_answer_and_sources(content or "")
        cleaned = sanitize_answer_text(answer) if config.output_cleanup_enabled else answer
        return (cleaned or answer or "").strip()

    def _extract_payload_content_and_sources(self, data: dict) -> tuple[str, list[dict], bool]:
        if not isinstance(data, dict):
            return "", [], False

        if self._is_empty_placeholder_payload(data):
            return "", [], True

        content = ""
        choices = data.get("choices", [])
        if isinstance(choices, list) and choices:
            content = self._extract_content_from_choice(choices[0])

        if not content:
            for key in ("output_text", "output"):
                content = self._flatten_text_content(data.get(key))
                if content:
                    break

        return content, self._extract_structured_sources(data), False

    def _finalize_content(self, content: str, sources: list[dict], *, render_sources: bool) -> str:
        body = (content or "").strip()
        if not render_sources:
            return body
        return self._append_sources_block(body, sources)

    def _finalize_result(
        self,
        content: str,
        sources: list[dict],
        *,
        render_sources: bool,
    ) -> tuple[str, list[dict]]:
        return self._finalize_content(content, sources, render_sources=render_sources), sources

    def _extract_content_from_choice(self, choice: dict) -> str:
        if not isinstance(choice, dict):
            return ""

        message = choice.get("message", {})
        if isinstance(message, dict):
            content = self._flatten_text_content(message.get("content"))
            if content:
                return content

        delta = choice.get("delta", {})
        if isinstance(delta, dict):
            content = self._flatten_text_content(delta.get("content"))
            if content:
                return content

        for key in ("text", "content"):
            value = self._flatten_text_content(choice.get(key, ""))
            if value:
                return value

        return ""

    def _is_empty_placeholder_payload(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False

        if data.get("choices", object()) is not None:
            return False

        return all(not str(data.get(key, "")).strip() for key in ("id", "object", "model"))

    def _build_placeholder_error(self, headers=None) -> ValueError:
        request_id = ""
        if headers:
            request_id = (
                headers.get("x-oneapi-request-id", "")
                or headers.get("x-request-id", "")
                or headers.get("request-id", "")
            ).strip()

        message = "上游返回了空的占位 completion 帧（choices=null），疑似中转站对 Grok chat/completions 的实现异常"
        if request_id:
            message += f"，request_id={request_id}"
        return ValueError(message)

    async def _parse_streaming_response(self, response, ctx=None, *, render_sources: bool = True) -> str:
        content, _ = await self._parse_streaming_response_result(response, ctx, render_sources=render_sources)
        return content

    async def _parse_streaming_response_result(
        self,
        response,
        ctx=None,
        *,
        render_sources: bool = True,
    ) -> tuple[str, list[dict]]:
        content = ""
        empty_placeholder_detected = False
        response_headers = getattr(response, "headers", None)
        collected_sources: list[dict] = []
        event_data_lines: list[str] = []

        def process_event(event_payload: str) -> None:
            nonlocal content, empty_placeholder_detected, collected_sources
            payload = event_payload.strip()
            if not payload:
                return
            if payload == "[DONE]":
                return
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return

            chunk, chunk_sources, is_placeholder = self._extract_payload_content_and_sources(data)
            if is_placeholder:
                empty_placeholder_detected = True
                return
            collected_sources = merge_sources(collected_sources, chunk_sources)
            if chunk:
                content += chunk

        async for line in response.aiter_lines():
            stripped = line.strip()
            if not stripped:
                if event_data_lines:
                    process_event("\n".join(event_data_lines))
                    event_data_lines.clear()
                continue
            if stripped.startswith("data:"):
                event_payload = stripped[5:].lstrip()
                if event_payload == "[DONE]":
                    if event_data_lines:
                        process_event("\n".join(event_data_lines))
                        event_data_lines.clear()
                    continue
                event_data_lines.append(event_payload)
                continue
            if event_data_lines:
                process_event("\n".join(event_data_lines))
                event_data_lines.clear()
            process_event(stripped)

        if event_data_lines:
            process_event("\n".join(event_data_lines))

        if not content and empty_placeholder_detected:
            raise self._build_placeholder_error(response_headers)

        content, collected_sources = self._finalize_result(
            content,
            collected_sources,
            render_sources=render_sources,
        )

        await log_info(ctx, f"stream completion parsed ({len(content)} chars)", config.debug_enabled)

        return content, collected_sources

    async def _parse_completion_response(self, response: httpx.Response, ctx=None, *, render_sources: bool = True) -> str:
        content, _ = await self._parse_completion_response_result(response, ctx, render_sources=render_sources)
        return content

    async def _parse_completion_response_result(
        self,
        response: httpx.Response,
        ctx=None,
        *,
        render_sources: bool = True,
    ) -> tuple[str, list[dict]]:
        content = ""
        sources: list[dict] = []
        body_text = response.text or ""

        try:
            data = response.json()
        except Exception:
            data = None

        if isinstance(data, dict):
            content, sources, is_placeholder = self._extract_payload_content_and_sources(data)
            if is_placeholder:
                raise self._build_placeholder_error(response.headers)
            content, sources = self._finalize_result(content, sources, render_sources=render_sources)

        if not content and any(line.lstrip().startswith("data:") for line in body_text.splitlines()):
            class _LineResponse:
                def __init__(self, text: str, headers):
                    self._lines = text.splitlines()
                    self.headers = headers

                async def aiter_lines(self):
                    for line in self._lines:
                        yield line

            content, sources = await self._parse_streaming_response_result(
                _LineResponse(body_text, response.headers),
                ctx,
                render_sources=render_sources,
            )

        if not content and not sources and body_text.strip():
            normalized = body_text.lower()
            if "<html" in normalized and "login" in normalized:
                raise ValueError("API 代理返回了登录页面，请检查认证状态")
            raise ValueError("上游返回了无法解析的 completion 响应")

        await log_info(ctx, f"completion parsed ({len(content)} chars)", config.debug_enabled)

        return content, sources

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None, *, render_sources: bool = True) -> str:
        """执行带重试机制的流式 HTTP 请求"""
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
        last_exc: Exception | None = None
        provider_configs = self._iter_provider_configs(payload)
        for index, provider_config in enumerate(provider_configs):
            attempt_headers = self._build_api_headers_for_key(provider_config["api_key"])
            model_candidates = _provider_model_candidates(provider_config["model"], provider_config["api_url"])
            try:
                for model_index, candidate_model in enumerate(model_candidates):
                    endpoint, attempt_payload = self._prepare_request_for_endpoint(
                        provider_config,
                        payload,
                        candidate_model,
                    )
                    async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=timeout)) as client:
                        try:
                            async for attempt in AsyncRetrying(
                                stop=stop_after_attempt(config.retry_max_attempts + 1),
                                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                                retry=retry_if_exception(_is_retryable_exception),
                                reraise=True,
                            ):
                                with attempt:
                                    async with client.stream(
                                        "POST",
                                        endpoint,
                                        headers=attempt_headers,
                                        json=attempt_payload,
                                    ) as response:
                                        response.raise_for_status()
                                        self._last_success_provider_name = provider_config["name"]
                                        self._last_success_provider_model = candidate_model
                                        self._last_success_provider_api_url = provider_config["api_url"]
                                        return await self._parse_streaming_response(response, ctx, render_sources=render_sources)
                        except Exception as exc:
                            last_exc = exc
                            if _is_model_unavailable_exception(exc) and model_index < len(model_candidates) - 1:
                                await log_info(
                                    ctx,
                                    f"model fallback: {candidate_model} -> {model_candidates[model_index + 1]} on {provider_config['name']}",
                                    config.debug_enabled,
                                )
                                continue
                            raise
            except Exception as exc:
                last_exc = exc
                if index == len(provider_configs) - 1:
                    raise
                await log_info(
                    ctx,
                    f"provider failover: {provider_config['name']} -> {provider_configs[index + 1]['name']} ({type(exc).__name__})",
                    config.debug_enabled,
                )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No Grok providers configured")

    async def _execute_completion_with_retry(self, headers: dict, payload: dict, ctx=None, *, render_sources: bool = True) -> str:
        content, sources = await self._execute_completion_with_retry_result(
            headers,
            payload,
            ctx,
            render_sources=render_sources,
        )
        self._last_completion_sources = sources
        return content

    async def _execute_completion_with_retry_result(
        self,
        headers: dict,
        payload: dict,
        ctx=None,
        *,
        render_sources: bool = True,
    ) -> tuple[str, list[dict]]:
        """执行带重试机制的非流式 HTTP 请求，兼容 JSON completion 与 SSE 文本响应。"""
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)
        last_exc: Exception | None = None
        provider_configs = self._iter_provider_configs(payload)
        for index, provider_config in enumerate(provider_configs):
            attempt_headers = self._build_api_headers_for_key(provider_config["api_key"])
            model_candidates = _provider_model_candidates(provider_config["model"], provider_config["api_url"])
            try:
                for model_index, candidate_model in enumerate(model_candidates):
                    endpoint, attempt_payload = self._prepare_request_for_endpoint(
                        provider_config,
                        payload,
                        candidate_model,
                    )
                    async with httpx.AsyncClient(**_httpx_client_kwargs_for_url(endpoint, timeout=timeout)) as client:
                        try:
                            async for attempt in AsyncRetrying(
                                stop=stop_after_attempt(config.retry_max_attempts + 1),
                                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                                retry=retry_if_exception(_is_retryable_exception),
                                reraise=True,
                            ):
                                with attempt:
                                    response = await client.post(
                                        endpoint,
                                        headers=attempt_headers,
                                        json=attempt_payload,
                                    )
                                    response.raise_for_status()
                                    self._last_success_provider_name = provider_config["name"]
                                    self._last_success_provider_model = candidate_model
                                    self._last_success_provider_api_url = provider_config["api_url"]
                                    return await self._parse_completion_response_result(
                                        response,
                                        ctx,
                                        render_sources=render_sources,
                                    )
                        except Exception as exc:
                            last_exc = exc
                            if _is_model_unavailable_exception(exc) and model_index < len(model_candidates) - 1:
                                await log_info(
                                    ctx,
                                    f"model fallback: {candidate_model} -> {model_candidates[model_index + 1]} on {provider_config['name']}",
                                    config.debug_enabled,
                                )
                                continue
                            raise
            except Exception as exc:
                last_exc = exc
                if index == len(provider_configs) - 1:
                    raise
                await log_info(
                    ctx,
                    f"provider failover: {provider_config['name']} -> {provider_configs[index + 1]['name']} ({type(exc).__name__})",
                    config.debug_enabled,
                )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No Grok providers configured")

    async def describe_url(self, url: str, ctx=None) -> dict:
        """让 Grok 阅读单个 URL 并返回 title + extracts"""
        headers = self._build_api_headers()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": url_describe_prompt},
                {"role": "user", "content": url},
            ],
            "stream": False,
        }
        result = self._normalize_internal_text(
            await self._execute_completion_with_retry(headers, payload, ctx, render_sources=False)
        )
        title, extracts = url, ""
        extract_lines: list[str] = []
        reading_extracts = False
        for line in result.strip().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("Title:"):
                title = stripped[6:].strip() or url
                reading_extracts = False
            elif stripped.startswith("Extracts:"):
                extract_lines = []
                first_line = stripped[9:].strip()
                if first_line:
                    extract_lines.append(first_line)
                reading_extracts = True
            elif reading_extracts:
                extract_lines.append(stripped)
        if extract_lines:
            extracts = " ".join(extract_lines).strip()
        return {"title": title, "extracts": extracts, "url": url}

    async def rank_sources(self, query: str, sources_text: str, total: int, ctx=None) -> list[int]:
        """让 Grok 按查询相关度对信源排序，返回排序后的序号列表"""
        headers = self._build_api_headers()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": rank_sources_prompt},
                {"role": "user", "content": f"Query: {query}\n\n{sources_text}"},
            ],
            "stream": False,
        }
        result = self._normalize_internal_text(
            await self._execute_completion_with_retry(headers, payload, ctx, render_sources=False)
        )
        order: list[int] = []
        seen: set[int] = set()
        for token in re.findall(r"\b\d+\b", result):
            try:
                n = int(token)
                if 1 <= n <= total and n not in seen:
                    seen.add(n)
                    order.append(n)
            except ValueError:
                continue
        # 补齐遗漏的序号
        for i in range(1, total + 1):
            if i not in seen:
                order.append(i)
        return order
