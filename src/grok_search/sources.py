import ast
import asyncio
import datetime as dt
import json
import re
import secrets
import time
from collections.abc import Mapping
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from .config import config
from .utils import extract_unique_urls


_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)", re.IGNORECASE)
_SOURCES_HEADING_PATTERN = re.compile(
    r"(?im)^"
    r"(?:#{1,6}\s*)?"
    r"(?:\*\*|__)?\s*"
    r"(sources?|references?|citations?|信源|参考资料|参考|引用|来源列表|来源)"
    r"\s*(?:\*\*|__)?"
    r"(?:\s*[（(][^)\n]*[)）])?"
    r"\s*[:：]?\s*$"
)
_SOURCES_FUNCTION_PATTERN = re.compile(
    r"(?im)(^|\n)\s*(sources|source|citations|citation|references|reference|citation_card|source_cards|source_card)\s*\("
)
_GENERIC_LINK_LIST_HEADING_PATTERN = re.compile(
    r"(?i)^(?:useful|related|helpful|official|reference|references|docs?|documentation|links?|resources?|endpoints?)"
    r"(?:\s+[a-z0-9][\w/-]*)*$"
)
_REAL_SOURCE_LIST_HEADING_PATTERN = re.compile(
    r"(?i)^(?:#{1,6}\s*)?(?:sources?(?:\s+i\s+used)?|references?|citations?|related\s+sources?|further\s+reading)\s*:?\s*$"
)
_THINK_BLOCK_PATTERN = re.compile(r"(?is)<think>.*?</think>")
_SENSITIVE_URL_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "code",
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
_LEADING_POLICY_PATTERNS = [
    re.compile(r"(?is)^\s*\**\s*i cannot comply\b.*"),
    re.compile(r"(?is)^\s*\**\s*i do not accept\b.*"),
    re.compile(r"(?is)^\s*\**\s*i do not follow\b.*"),
    re.compile(r"(?is)^\s*\**\s*i don't follow\b.*"),
    re.compile(r"(?is)^\s*\**\s*i don't adopt\b.*"),
    re.compile(r"(?is)^\s*\**\s*refusal\s*[:：].*"),
    re.compile(r"(?is)^\s*\**\s*refuse to\b.*"),
    re.compile(r"(?is)^\s*\**\s*rejected?\b.*"),
    re.compile(r"(?is)^\s*\**\s*拒绝执行\b.*"),
    re.compile(r"(?is)^\s*\**\s*无法遵循\b.*"),
]
_POLICY_META_KEYWORDS = (
    "cannot comply",
    "refuse",
    "refusal",
    "do not follow",
    "don't follow",
    "don't adopt",
    "override my core",
    "core behavior",
    "custom rules",
    "用户提供的自定义",
    "覆盖我的核心",
    "核心行为",
    "拒绝执行",
    "无法遵循",
)
_POLICY_CONTEXT_KEYWORDS = (
    "jailbreak",
    "prompt injection",
    "system instructions",
    "system prompt",
    "user-injected",
    "注入",
    "越狱",
    "系统指令",
    "系统提示",
    "自定义“system”",
)


def new_session_id() -> str:
    return secrets.token_hex(16)


class SourcesCache:
    def __init__(
        self,
        max_size: int = 256,
        ttl_seconds: float = 3600.0,
        now_fn=None,
    ):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._now = now_fn or time.monotonic
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float | None, object]] = OrderedDict()

    def _expires_at(self) -> float | None:
        if self._ttl_seconds <= 0:
            return None
        return self._now() + self._ttl_seconds

    def _purge_expired_locked(self) -> None:
        if self._ttl_seconds <= 0:
            return

        now = self._now()
        expired_ids = [
            session_id
            for session_id, (expires_at, _) in self._cache.items()
            if expires_at is not None and expires_at <= now
        ]
        for session_id in expired_ids:
            self._cache.pop(session_id, None)

    async def set(self, session_id: str, sources: object) -> None:
        async with self._lock:
            self._purge_expired_locked()
            self._cache[session_id] = (self._expires_at(), sources)
            self._cache.move_to_end(session_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    async def get(self, session_id: str) -> object | None:
        async with self._lock:
            self._purge_expired_locked()
            cached = self._cache.get(session_id)
            if cached is None:
                return None
            _, sources = cached
            self._cache.move_to_end(session_id)
            return sources

    async def size(self) -> int:
        async with self._lock:
            self._purge_expired_locked()
            return len(self._cache)

    async def snapshot(self) -> list[object]:
        async with self._lock:
            self._purge_expired_locked()
            return [value for _, value in self._cache.values()]


def merge_sources(*source_lists: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for sources in source_lists:
        for item in sources or []:
            url = (item or {}).get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            if url in seen:
                continue
            seen.add(url)
            merged.append(item)
    return merged


def standardize_sources(sources: list[dict], retrieved_at: str | None = None) -> list[dict]:
    timestamp = retrieved_at or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    standardized_by_url: OrderedDict[str, dict] = OrderedDict()

    for index, item in enumerate(sources or []):
        if not isinstance(item, Mapping):
            continue

        raw_item = dict(item)
        url = _normalize_url(raw_item.get("url"))
        if not url:
            continue

        snippet = _normalize_snippet(raw_item)
        description = _normalize_text(raw_item.get("description"))
        if not description:
            description = snippet

        raw_item["title"] = _normalize_text(raw_item.get("title"))
        raw_item["url"] = url
        raw_item["provider"] = _normalize_provider(raw_item.get("provider") or raw_item.get("source"))
        raw_item["description"] = description
        raw_item["source_type"] = "web_page"
        raw_item["snippet"] = snippet
        raw_item["domain"] = _extract_domain(url)
        raw_item["score"] = _normalize_score(raw_item.get("score"))
        raw_item["published_at"] = _normalize_optional_text(raw_item.get("published_at") or raw_item.get("published_date"))
        raw_item["retrieved_at"] = _normalize_optional_text(raw_item.get("retrieved_at")) or timestamp
        raw_item["_source_order"] = index
        existing = standardized_by_url.get(url)
        if existing is None or _should_replace_standardized_source(existing, raw_item):
            standardized_by_url[url] = raw_item

    standardized = list(standardized_by_url.values())
    standardized.sort(key=_source_priority_key)
    for rank, item in enumerate(standardized, start=1):
        item["rank"] = rank
        item.pop("_source_order", None)

    return standardized


def _source_priority_key(item: dict) -> tuple:
    score = item.get("score")
    title = (item.get("title") or "").strip()
    description = (item.get("description") or "").strip()
    provider = (item.get("provider") or "").strip().lower()
    return (
        0 if provider == "grok" else 1,
        0 if score is not None else 1,
        -(score if isinstance(score, (int, float)) else 0.0),
        0 if title else 1,
        0 if description else 1,
        item.get("_source_order", 0),
    )


def _should_replace_standardized_source(existing: dict, candidate: dict) -> bool:
    return _source_priority_key(candidate) < _source_priority_key(existing)


def split_answer_and_sources(text: str) -> tuple[str, list[dict]]:
    raw = (text or "").strip()
    if not raw:
        return "", []

    if config.output_cleanup_enabled:
        cleaned = sanitize_answer_text(raw)
        if cleaned:
            raw = cleaned

    split = _split_function_call_sources(raw)
    if split:
        return split

    split = _split_heading_sources(raw)
    if split:
        return split

    split = _split_details_block_sources(raw)
    if split:
        return split

    split = _split_tail_link_block(raw)
    if split:
        return split

    return raw, []


def sanitize_answer_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    cleaned = _THINK_BLOCK_PATTERN.sub("", raw).strip()
    paragraphs = _split_paragraphs(cleaned)
    filtered = [paragraph for paragraph in paragraphs if not _looks_like_policy_block(paragraph)]
    if filtered:
        return "\n\n".join(filtered).strip()
    return cleaned


def _split_paragraphs(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _looks_like_policy_block(text: str) -> bool:
    normalized = _normalize_policy_text(text)
    if not normalized:
        return False

    if any(pattern.match(normalized) for pattern in _LEADING_POLICY_PATTERNS):
        return True

    return any(keyword in normalized for keyword in _POLICY_META_KEYWORDS) and any(
        keyword in normalized for keyword in _POLICY_CONTEXT_KEYWORDS
    )


def _normalize_policy_text(text: str) -> str:
    normalized = re.sub(r"[>*_`#-]+", " ", text or "")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def _split_function_call_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_FUNCTION_PATTERN.finditer(text))
    if not matches:
        return None

    for m in reversed(matches):
        if _is_inside_fenced_code_block(text, m.start()):
            continue
        open_paren_idx = m.end() - 1
        extracted = _extract_balanced_call_at_end(text, open_paren_idx)
        if not extracted:
            continue

        close_paren_idx, args_text = extracted
        sources = _parse_sources_payload(args_text)
        if not sources:
            continue

        answer = text[: m.start()].rstrip()
        return answer, sources

    return None


def _extract_balanced_call_at_end(text: str, open_paren_idx: int) -> tuple[int, str] | None:
    if open_paren_idx < 0 or open_paren_idx >= len(text) or text[open_paren_idx] != "(":
        return None

    depth = 1
    in_string: str | None = None
    escape = False

    for idx in range(open_paren_idx + 1, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == in_string:
                in_string = None
            continue

        if ch in ("'", '"'):
            in_string = ch
            continue

        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                if text[idx + 1 :].strip():
                    return None
                args_text = text[open_paren_idx + 1 : idx]
                return idx, args_text

    return None


def _split_heading_sources(text: str) -> tuple[str, list[dict]] | None:
    matches = list(_SOURCES_HEADING_PATTERN.finditer(text))
    if not matches:
        return None

    for m in reversed(matches):
        if _is_inside_fenced_code_block(text, m.start()):
            continue
        start = m.start()
        sources_text = text[start:]
        sources = extract_sources_from_text(sources_text)
        if not sources:
            continue
        answer = text[:start].rstrip()
        return answer, sources
    return None


def _split_tail_link_block(text: str) -> tuple[str, list[dict]] | None:
    lines = text.splitlines()
    if not lines:
        return None

    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return None

    tail_end = idx
    link_like_count = 0
    while idx >= 0:
        line = lines[idx].strip()
        if not line:
            idx -= 1
            continue
        if not _is_link_only_line(line):
            break
        link_like_count += 1
        idx -= 1

    tail_start = idx + 1
    if link_like_count < 2:
        return None

    block_text = "\n".join(lines[tail_start : tail_end + 1])
    sources = extract_sources_from_text(block_text)
    if not sources:
        return None

    answer_end = tail_start
    heading_index = tail_start - 1
    if heading_index >= 0 and _REAL_SOURCE_LIST_HEADING_PATTERN.fullmatch(lines[heading_index].strip()):
        answer_end = heading_index

    answer = "\n".join(lines[:answer_end]).rstrip()
    if _looks_like_list_intro(answer):
        return None
    return answer, sources


def _split_details_block_sources(text: str) -> tuple[str, list[dict]] | None:
    lower = text.lower()
    close_idx = lower.rfind("</details>")
    if close_idx == -1:
        return None
    tail = text[close_idx + len("</details>") :].strip()
    if tail:
        return None

    open_idx = lower.rfind("<details", 0, close_idx)
    if open_idx == -1:
        return None

    block_text = text[open_idx : close_idx + len("</details>")]
    sources = extract_sources_from_text(block_text)
    if len(sources) < 2:
        return None

    answer = text[:open_idx].rstrip()
    return answer, sources


def _is_link_only_line(line: str) -> bool:
    stripped = re.sub(r"^\s*(?:[-*]|\d+\.)\s*", "", line).strip()
    if not stripped:
        return False
    normalized_url = _normalize_url(stripped)
    if normalized_url:
        return True
    if _MD_LINK_PATTERN.search(stripped):
        return True
    return False


def _is_inside_fenced_code_block(text: str, index: int) -> bool:
    return text[:index].count("```") % 2 == 1


def _looks_like_list_intro(text: str) -> bool:
    stripped = (text or "").rstrip()
    if not stripped:
        return False
    last_line = stripped.splitlines()[-1].strip()
    if _REAL_SOURCE_LIST_HEADING_PATTERN.fullmatch(last_line):
        return False
    return bool(last_line) and (
        last_line.endswith(":") or _GENERIC_LINK_LIST_HEADING_PATTERN.fullmatch(last_line) is not None
    )


def _sanitize_url_for_output(url: str) -> str:
    split = urlsplit(url)
    if not split.username and not split.password and not split.query and not split.fragment:
        return url

    sanitized_query = _sanitize_url_params(split.query)
    sanitized_fragment = split.fragment
    if split.fragment and any(token in split.fragment for token in ("=", "&")):
        sanitized_fragment = _sanitize_url_params(split.fragment)

    sanitized_netloc = _sanitize_netloc(split)
    return urlunsplit((split.scheme, sanitized_netloc, split.path, sanitized_query, sanitized_fragment))


def _sanitize_url_params(params: str) -> str:
    if not params:
        return params

    pairs = parse_qsl(params, keep_blank_values=True)
    return urlencode(
        [
            (key, "REDACTED" if key.lower() in _SENSITIVE_URL_QUERY_KEYS else value)
            for key, value in pairs
        ],
        doseq=True,
    )


def _sanitize_netloc(split) -> str:
    hostname = split.hostname or ""
    if not hostname:
        return split.netloc.rsplit("@", 1)[-1]

    if ":" in hostname and not hostname.startswith("["):
        host = f"[{hostname}]"
    else:
        host = hostname

    if split.port is not None:
        return f"{host}:{split.port}"
    return host


def _parse_sources_payload(payload: str) -> list[dict]:
    payload = (payload or "").strip().rstrip(";")
    if not payload:
        return []

    data: Any = None
    try:
        data = json.loads(payload)
    except Exception:
        try:
            data = ast.literal_eval(payload)
        except Exception:
            data = None

    if data is None:
        return extract_sources_from_text(payload)

    if isinstance(data, dict):
        for key in ("sources", "citations", "references", "urls"):
            if key in data:
                return _normalize_sources(data[key])
        return _normalize_sources(data)

    return _normalize_sources(data)


def _normalize_sources(data: Any) -> list[dict]:
    items: list[Any]
    if isinstance(data, (list, tuple)):
        items = list(data)
    elif isinstance(data, dict):
        items = [data]
    else:
        items = [data]

    normalized: list[dict] = []
    seen: set[str] = set()

    for item in items:
        if isinstance(item, str):
            for url in extract_unique_urls(item):
                if url not in seen:
                    seen.add(url)
                    normalized.append({"url": url})
            continue

        if isinstance(item, (list, tuple)) and len(item) >= 2:
            title, url = item[0], item[1]
            normalized_url = _normalize_url(url)
            if normalized_url and normalized_url not in seen:
                seen.add(normalized_url)
                out: dict = {"url": url}
                if isinstance(title, str) and title.strip():
                    out["title"] = title.strip()
                normalized.append(out)
            continue

        if isinstance(item, dict):
            url = item.get("url") or item.get("href") or item.get("link")
            normalized_url = _normalize_url(url)
            if not normalized_url:
                continue
            if normalized_url in seen:
                continue
            seen.add(normalized_url)
            out: dict = {"url": url}
            title = item.get("title") or item.get("name") or item.get("label")
            if isinstance(title, str) and title.strip():
                out["title"] = title.strip()
            desc = item.get("description") or item.get("snippet") or item.get("content")
            if isinstance(desc, str) and desc.strip():
                out["description"] = desc.strip()
            normalized.append(out)
            continue

    return normalized


def extract_sources_from_text(text: str) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()

    for title, url in _MD_LINK_PATTERN.findall(text or ""):
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (title or "").strip()
        if title:
            sources.append({"title": title, "url": url})
        else:
            sources.append({"url": url})

    for url in extract_unique_urls(text or ""):
        if url in seen:
            continue
        seen.add(url)
        sources.append({"url": url})

    return sources


def _normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    if not parsed.netloc:
        return ""
    try:
        return _sanitize_url_for_output(url)
    except ValueError:
        return ""


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalize_optional_text(value: Any) -> str | None:
    text = _normalize_text(value)
    return text or None


def _normalize_provider(value: Any) -> str:
    provider = _normalize_text(value)
    return provider or "grok"


def _normalize_snippet(item: dict[str, Any]) -> str:
    for key in ("snippet", "description", "content", "text"):
        text = _normalize_text(item.get(key))
        if text:
            return text
    return ""


def _normalize_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or "").lower()
