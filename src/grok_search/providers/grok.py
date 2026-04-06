import httpx
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import urlparse
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base
from .base import BaseSearchProvider, SearchResult
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


def _is_retryable_exception(exc) -> bool:
    """检查异常是否可重试"""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES
    return False


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
    def __init__(self, api_url: str, api_key: str, model: str = "grok-4.1-fast"):
        super().__init__(api_url, api_key)
        self.model = model

    def get_provider_name(self) -> str:
        return "Grok"

    def _build_api_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "grok-search-mcp/0.1.0",
        }

    async def search(self, query: str, platform: str = "", min_results: int = 3, max_results: int = 10, ctx=None) -> List[SearchResult]:
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

        await log_info(ctx, f"platform_prompt: { query + platform_prompt}", config.debug_enabled)

        return await self._execute_completion_with_retry(headers, payload, ctx)

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

    def _normalize_source_items(self, data) -> list[dict]:
        items = data if isinstance(data, list) else [data]
        normalized: list[dict] = []

        for item in items:
            if isinstance(item, str):
                url = item.strip()
                parsed = urlparse(url)
                if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                    normalized.append({"url": url})
                continue

            if not isinstance(item, dict):
                continue

            url = item.get("url") or item.get("href") or item.get("link")
            if not isinstance(url, str):
                continue
            url = url.strip()
            parsed = urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue

            source = {"url": url}
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

            normalized.append(source)

        return normalized

    def _extract_structured_sources(self, data: dict) -> list[dict]:
        candidate_keys = (
            "citations",
            "references",
            "sources",
            "source_cards",
            "source_card",
            "annotations",
            "search_results",
            "searchResults",
            "urls",
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
            for key in candidate_keys:
                if key in mapping:
                    collected = merge_sources(collected, self._normalize_source_items(mapping[key]))

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
                event_data_lines.append(stripped[5:].lstrip())

        if event_data_lines:
            process_event("\n".join(event_data_lines))

        if not content and empty_placeholder_detected:
            raise self._build_placeholder_error(response_headers)

        content = self._finalize_content(content, collected_sources, render_sources=render_sources)

        await log_info(ctx, f"content: {content}", config.debug_enabled)

        return content

    async def _parse_completion_response(self, response: httpx.Response, ctx=None, *, render_sources: bool = True) -> str:
        content = ""
        body_text = response.text or ""

        try:
            data = response.json()
        except Exception:
            data = None

        if isinstance(data, dict):
            content, sources, is_placeholder = self._extract_payload_content_and_sources(data)
            if is_placeholder:
                raise self._build_placeholder_error(response.headers)
            content = self._finalize_content(content, sources, render_sources=render_sources)

        if not content and any(line.lstrip().startswith("data:") for line in body_text.splitlines()):
            class _LineResponse:
                def __init__(self, text: str, headers):
                    self._lines = text.splitlines()
                    self.headers = headers

                async def aiter_lines(self):
                    for line in self._lines:
                        yield line

            content = await self._parse_streaming_response(
                _LineResponse(body_text, response.headers),
                ctx,
                render_sources=render_sources,
            )

        if not content and body_text.strip():
            normalized = body_text.lower()
            if "<html" in normalized and "login" in normalized:
                raise ValueError("API 代理返回了登录页面，请检查认证状态")
            raise ValueError("上游返回了无法解析的 completion 响应")

        await log_info(ctx, f"content: {content}", config.debug_enabled)

        return content

    async def _execute_stream_with_retry(self, headers: dict, payload: dict, ctx=None, *, render_sources: bool = True) -> str:
        """执行带重试机制的流式 HTTP 请求"""
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(config.retry_max_attempts + 1),
                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            ):
                with attempt:
                    async with client.stream(
                        "POST",
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        return await self._parse_streaming_response(response, ctx, render_sources=render_sources)

    async def _execute_completion_with_retry(self, headers: dict, payload: dict, ctx=None, *, render_sources: bool = True) -> str:
        """执行带重试机制的非流式 HTTP 请求，兼容 JSON completion 与 SSE 文本响应。"""
        timeout = httpx.Timeout(connect=6.0, read=120.0, write=10.0, pool=None)

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(config.retry_max_attempts + 1),
                wait=_WaitWithRetryAfter(config.retry_multiplier, config.retry_max_wait),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            ):
                with attempt:
                    response = await client.post(
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    return await self._parse_completion_response(response, ctx, render_sources=render_sources)

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
