import asyncio
import datetime as dt
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .config import config
from .deep_research_store import DeepResearchStore
from .deep_research_types import (
    DeepResearchCheckpointState,
    DeepResearchClaim,
    DeepResearchContinuation,
    DeepResearchContinuationState,
    DeepResearchEvidenceItem,
    DeepResearchJob,
    DeepResearchPlan,
    DeepResearchReportSection,
    DeepResearchResearchUnit,
    DeepResearchSectionCitations,
    utc_now_iso,
)
from .providers.base import _filter_supported_search_kwargs
from .providers.grok import GrokSearchProvider
from .sources import merge_sources, split_answer_and_sources, standardize_sources
from .utils import extract_unique_urls


Runner = Callable[["DeepResearchRuntime", str], Awaitable[None]]
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_MAX_CLAIM_LENGTH = 220
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "later",
    "of",
    "on",
    "or",
    "the",
    "to",
    "up",
    "use",
    "with",
}
_NOISY_EVIDENCE_MARKERS = (
    "for more information about",
    "real-world analogy",
    "think of `",
    "sitemap",
    "open in app",
    "communities for your favorite technologies",
    "your communities",
    "dev community",
    "collectives",
    "stack overflow for teams",
    "hot network questions",
    "site design / logo",
    "sign in",
    "sign up",
    "skip to content",
    "table of contents",
    "english * deutsch",
    "español",
    "privacy policy",
    "cookie policy",
    "we use essential cookies",
    "performance cookies",
    "anonymous statistics",
    "integrated platform for monitoring",
    "visibility into your stack",
    "observability end-to-end",
    "topics can help you to resolve common issues",
    "help you to resolve common issues",
    "following, you can find topics about troubleshooting issues",
    "these topics can help you to resolve common issues",
    "starts the replication task",
)
_PREFERRED_TECHNICAL_TERMS = (
    "checkpoint",
    "recovery",
    "runtime",
    "migration",
    "database",
    "sqlite",
    "postgresql",
    "resumability",
    "continuation",
    "state",
)
_COMMUNITY_SOURCE_DOMAINS = {
    "stackoverflow.com",
    "stackexchange.com",
    "reddit.com",
    "news.ycombinator.com",
    "dev.to",
    "medium.com",
}
_GAP_SECTION_MARKERS = ("remaining gap", "remaining gaps", "open question", "open questions")
_GAP_EVIDENCE_MARKERS = (
    "needs confirmation",
    "need confirmation",
    "unclear",
    "unknown",
    "not enough evidence",
    "insufficient evidence",
    "requires follow-up",
    "further research",
    "remaining gap",
    "open question",
)
_VALID_SEARCH_STRATEGY_APPROACHES = {"targeted", "breadth_first", "depth_first"}
_UNSAFE_NORMALIZE_ACTION_PREFIXES = (
    "filled_search_query:",
    "degraded_fetch_without_url_to_search:",
    "degraded_map_without_url_to_search:",
    "dropped_unknown_dependency:",
    "dropped_forward_or_cyclic_dependency:",
    "added_sub_question_search_unit:",
)
_UNSAFE_VALIDATION_ISSUES = {
    "missing_search_query",
    "missing_fetch_url",
    "missing_map_url",
    "missing_sub_question_unit_coverage",
    "unknown_dependency",
    "forward_or_cyclic_dependency",
}
_FINAL_ARTIFACT_KINDS = ("sources.json", "citations.json", "report.json", "final_report.md")
_DEFAULT_SEARCH_QUERY_FN = None
_RUNTIME_RECONCILE_STALE_SECONDS = 30


class PlannerGenerationError(RuntimeError):
    def __init__(self, stage: str, message: str, *, trace: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.trace = trace or {}


def _json_markdown_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _trim_text(value: str, *, limit: int = 400) -> str:
    text = _normalize_whitespace(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        normalized = _normalize_whitespace(item)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(normalized)
    return unique


def _stable_text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _tokenize_keywords(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*", (value or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in _STOPWORDS and not token.isdigit()]


def _is_noisy_text(value: str) -> bool:
    text = (value or "").strip()
    lowered = text.lower()
    if not text:
        return True
    if "![" in text:
        return True
    if any(marker in lowered for marker in _NOISY_EVIDENCE_MARKERS):
        return True
    if lowered.startswith("[") and "](" in lowered:
        return True
    if len(re.findall(r"https?://", lowered)) >= 3 and len(text) < 500:
        return True
    if extract_unique_urls(text):
        stripped = re.sub(r"https?://\S+", " ", text)
        stripped = re.sub(r"[-*•`>#()\[\],.;:]+", " ", stripped)
        if len(_normalize_whitespace(stripped)) < 12:
            return True
    if len(text) <= 80 and sum(1 for marker in ("english", "deutsch", "español", "français") if marker in lowered) >= 2:
        return True
    return False


def _extract_meaningful_lines(value: str) -> list[str]:
    meaningful: list[str] = []
    for raw_line in (value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if _is_noisy_text(line):
            continue
        meaningful.append(line)
    return meaningful


def _final_report_text_is_meaningful(text: str, *, report_value: dict[str, Any] | None = None) -> bool:
    meaningful_lines = _extract_meaningful_lines(text)
    if not meaningful_lines:
        return False
    body_text = _normalize_whitespace(" ".join(meaningful_lines))
    report_summary = ""
    if isinstance(report_value, dict):
        report_summary = _normalize_whitespace(str(report_value.get("summary", "")))
    if report_summary:
        summary_tokens = _tokenize_keywords(report_summary)
        if summary_tokens and _count_keyword_overlap(body_text, summary_tokens) == 0:
            return False
        if len(body_text) >= 12:
            return True
        return False
    if len(body_text) < 24:
        return False
    return True


def _summarize_evidence_text(value: str, *, limit: int = _MAX_CLAIM_LENGTH) -> str:
    lines = _extract_meaningful_lines(value)
    if not lines:
        sentence_candidates = [
            _normalize_whitespace(chunk)
            for chunk in re.split(r"(?<=[.!?])\s+", _normalize_whitespace(value))
            if _normalize_whitespace(chunk)
        ]
        lines = [chunk for chunk in sentence_candidates if not _is_noisy_text(chunk)]
    text = " ".join(lines[:4]) if lines else ""
    return _trim_text(text, limit=limit)


def _sanitize_detail_text(value: str, *, limit: int = 1200) -> str:
    lines = _extract_meaningful_lines(value)
    if not lines:
        return _summarize_evidence_text(value, limit=min(limit, _MAX_CLAIM_LENGTH))
    text = "\n".join(lines[:8])
    return _trim_text(text, limit=limit)


def _extract_relevant_excerpt(
    value: str,
    *,
    reference_texts: list[str],
    line_limit: int,
    char_limit: int,
    multiline: bool = False,
) -> str:
    lines = _extract_meaningful_lines(value)
    if not lines:
        if multiline:
            return _sanitize_detail_text(value, limit=char_limit)
        return _summarize_evidence_text(value, limit=char_limit)
    keywords = _dedupe_preserve_order(
        [token for text in reference_texts for token in _tokenize_keywords(text)]
    )
    ranked: list[tuple[int, int, str]] = []
    for line in lines:
        overlap = _count_keyword_overlap(line, keywords) if keywords else 0
        ranked.append((overlap, len(line), line))
    ranked.sort(reverse=True)
    max_overlap = ranked[0][0] if ranked else 0
    overlap_threshold = max(1, max_overlap - 1) if max_overlap > 0 else 0
    selected = [line for overlap, _, line in ranked if overlap >= overlap_threshold and overlap > 0][:line_limit]
    if not selected:
        selected = [line for _, _, line in ranked[:line_limit]]
    separator = "\n" if multiline else " "
    return _trim_text(separator.join(selected), limit=char_limit)


def _extract_markdown_title(value: str) -> str:
    for raw_line in (value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return _normalize_whitespace(line.lstrip("#").strip())
    return ""


def _guess_title_from_url(url: str) -> str:
    split = urlsplit((url or "").strip())
    path = (split.path or "").strip("/")
    if not path:
        return ""
    candidate = path.split("/")[-1]
    candidate = re.sub(r"\.[a-z0-9]{1,6}$", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.replace("-", " ").replace("_", " ")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if len(candidate) < 4 or candidate.isdigit():
        return ""
    return candidate[:1].upper() + candidate[1:]


def _count_keyword_overlap(text: str, keywords: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _has_noisy_source_metadata(source: dict[str, Any]) -> bool:
    for key in ("title", "description", "snippet"):
        value = source.get(key)
        if isinstance(value, str) and value.strip() and _is_noisy_text(value):
            return True
    return False


def _continuation_anchor_terms(continuation: DeepResearchContinuationState) -> list[str]:
    texts = [
        continuation.continuation_goal,
        " ".join(continuation.confirmed_claims),
        " ".join(continuation.open_questions),
        " ".join(continuation.trusted_source_headers),
        " ".join(section.get("title", "") for section in continuation.carry_forward_sections),
    ]
    keyword_counts: dict[str, int] = {}
    for text in texts:
        for token in _tokenize_keywords(text):
            if token.endswith("s") and token[:-1] in _PREFERRED_TECHNICAL_TERMS:
                token = token[:-1]
            keyword_counts[token] = keyword_counts.get(token, 0) + 1

    anchors: list[str] = []
    for preferred in _PREFERRED_TECHNICAL_TERMS:
        if keyword_counts.get(preferred, 0) > 0:
            anchors.append(preferred)
    if not anchors:
        anchors = sorted(keyword_counts, key=lambda token: (-keyword_counts[token], token))
    return anchors[:4]


def _compact_continuation(continuation: DeepResearchContinuationState) -> DeepResearchContinuation:
    return DeepResearchContinuation(
        mode=continuation.mode,
        source_job_id=continuation.source_job_id,
        source_job_status=continuation.source_job_status,
        continuation_identity=continuation.continuation_identity,
        previous_summary=continuation.previous_summary,
        prior_plan_summary=continuation.prior_plan_summary,
        continuation_goal=continuation.continuation_goal,
        source_count=continuation.source_count,
        checkpoint_key=continuation.checkpoint_key,
        state_version=continuation.state_version,
        confirmed_claims=list(continuation.confirmed_claims),
        open_questions=list(continuation.open_questions),
        trusted_source_headers=list(continuation.trusted_source_headers),
        carry_forward_constraints=dict(continuation.carry_forward_constraints),
    )


def _should_apply_continuation_rewrite(query: str, continuation: DeepResearchContinuationState) -> bool:
    if continuation.mode != "continue":
        return False
    lowered = (query or "").lower()
    if not re.search(r"\b(resume|continue)\b", lowered):
        return False
    query_terms = set(_tokenize_keywords(query))
    anchor_terms = set(_continuation_anchor_terms(continuation))
    technical_terms = set(_PREFERRED_TECHNICAL_TERMS)
    return bool(query_terms & technical_terms) or bool(anchor_terms & technical_terms)


def _rewrite_research_query(query: str, continuation: DeepResearchContinuationState) -> str:
    text = _normalize_whitespace(query)
    if not text or not _should_apply_continuation_rewrite(text, continuation):
        return text

    if re.search(r"(?<!checkpoint )\bresume\b", text, flags=re.IGNORECASE):
        text = re.sub(r"(?<!checkpoint )\bresume\b", "checkpoint resume", text, flags=re.IGNORECASE)

    anchors = _continuation_anchor_terms(continuation)
    if anchors:
        lowered = text.lower()
        missing = [anchor for anchor in anchors if anchor not in lowered]
        if missing:
            text = f"{text} {' '.join(missing[:2])}"
    return _normalize_whitespace(text)


def _best_text_claim(lines: list[str], fallback: str) -> str:
    for line in lines:
        if not _is_noisy_text(line):
            return line
    return _normalize_whitespace(fallback)


def _normalize_citations_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    if "source_registry" in value:
        return value
    if value and all(isinstance(item, dict) and "url" in item for item in value.values()):
        return {"source_registry": value, "sections": []}
    return value


def _is_gap_section(section: DeepResearchReportSection | dict[str, Any]) -> bool:
    title = section.title if isinstance(section, DeepResearchReportSection) else str(section.get("title", ""))
    goal = section.goal if isinstance(section, DeepResearchReportSection) else str(section.get("goal", ""))
    lowered = f"{title} {goal}".lower()
    return any(marker in lowered for marker in _GAP_SECTION_MARKERS)


def _has_gap_signal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _GAP_EVIDENCE_MARKERS)


def _source_registry_key(url: str) -> str:
    split = urlsplit((url or "").strip())
    if not split.scheme or not split.netloc:
        return (url or "").strip().lower()
    netloc = split.netloc.lower()
    return f"{split.scheme.lower()}://{netloc}{split.path}?{split.query}#{split.fragment}"


def _next_source_id(existing_sources: list[dict[str, Any]]) -> str:
    used_numbers: set[int] = set()
    for item in existing_sources:
        source_id = str(item.get("source_id", "")).strip()
        if source_id.startswith("R") and source_id[1:].isdigit():
            used_numbers.add(int(source_id[1:]))
    next_number = 1
    while next_number in used_numbers:
        next_number += 1
    return f"R{next_number}"


def _source_sort_tuple(item: dict[str, Any]) -> tuple:
    rank_hint = item.get("rank")
    if not isinstance(rank_hint, int):
        rank_hint = 10_000
    score = item.get("score")
    quality_score = _source_quality_score(item)
    return (
        -quality_score,
        -(item.get("topic_match_score") if isinstance(item.get("topic_match_score"), int) else 0),
        -_source_quality_bias(item),
        -(item.get("citation_count") if isinstance(item.get("citation_count"), int) else 0),
        -(item.get("section_count") if isinstance(item.get("section_count"), int) else 0),
        0 if item.get("title") else 1,
        0 if item.get("snippet") or item.get("description") else 1,
        0 if item.get("provider") else 1,
        0 if score is not None else 1,
        -(score if isinstance(score, (int, float)) else 0.0),
        rank_hint,
        str(item.get("url", "")),
    )


def _source_quality_score(item: dict[str, Any]) -> int:
    score = 0
    score += _source_quality_bias(item) * 10
    if item.get("title"):
        score += 4
    if item.get("snippet"):
        score += 4
    if item.get("description"):
        score += 2
    if item.get("contributors"):
        score += 1
    if item.get("provider"):
        score += 1
    if _has_noisy_source_metadata(item):
        score -= 20
    return score


def _source_quality_tier(item: dict[str, Any]) -> str:
    bias = _source_quality_bias(item)
    if bias >= 3:
        return "official"
    if bias >= 1:
        return "high_signal"
    if bias <= -2:
        return "community"
    return "standard"


def _preferred_citation_ids(source_ids: list[str], source_registry: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    if not source_ids:
        return []
    source_ids = _dedupe_preserve_order(source_ids)
    registry_by_id = {item.get("source_id"): item for item in source_registry if item.get("source_id")}
    ranked = sorted(
        [registry_by_id[source_id] for source_id in source_ids if source_id in registry_by_id],
        key=_source_sort_tuple,
    )
    narrowed = [item["source_id"] for item in ranked[:limit]]
    return narrowed or source_ids[:limit]


def _merge_source_metadata(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in candidate.items():
        if key == "rank":
            continue
        current = merged.get(key)
        if key in {"description", "snippet"} and value == "" and _is_noisy_text(str(current or "")):
            merged[key] = ""
            continue
        if value in ("", None, []):
            continue
        if current in ("", None, []):
            if key not in {"description", "snippet"} or not _is_noisy_text(str(value)):
                merged[key] = value
            continue
        if key in {"description", "snippet"} and not _is_noisy_text(str(value)) and len(str(value)) > len(str(current)):
            merged[key] = value
            continue
        if key == "title" and len(str(value)) > len(str(current)):
            merged[key] = value
            continue
        if key == "score" and isinstance(value, (int, float)):
            if not isinstance(current, (int, float)) or value > current:
                merged[key] = value
            continue
        if key in {"provider", "origin_type"}:
            if not current and value:
                merged[key] = value
                continue
            if current in {"grok", "unknown"} and value:
                merged[key] = value
                continue
        if key == "published_at" and isinstance(value, str):
            current_dt = _parse_utc_iso(str(current)) if current else None
            value_dt = _parse_utc_iso(value)
            if current_dt is None or (value_dt is not None and value_dt > current_dt):
                merged[key] = value
            continue
        if key == "contributors":
            merged[key] = value
            continue
    if not merged.get("title") and merged.get("url"):
        merged["title"] = _guess_title_from_url(str(merged["url"]))
    return merged


def _enrich_source_from_fetched_text(source: dict[str, Any], fetched_text: str) -> dict[str, Any]:
    enriched = dict(source)
    title = _extract_markdown_title(fetched_text)
    summary = _summarize_evidence_text(fetched_text, limit=280)
    if title and not enriched.get("title"):
        enriched["title"] = title
    if summary:
        if not enriched.get("snippet"):
            enriched["snippet"] = summary
        if not enriched.get("description"):
            enriched["description"] = summary
    if _has_noisy_source_metadata(enriched):
        for key in ("description", "snippet"):
            value = enriched.get(key)
            if isinstance(value, str) and _is_noisy_text(value):
                enriched[key] = ""
    if not enriched.get("domain") and enriched.get("url"):
        enriched["domain"] = urlsplit(enriched["url"]).netloc.lower()
    return enriched


def _build_report_summary(plan: DeepResearchPlan, sections: list[dict[str, Any]]) -> str:
    claim_texts: list[str] = []
    for section in sections:
        for claim in section.get("claims", []):
            text = _summarize_evidence_text(str(claim.get("text", "")), limit=180)
            if not text or _is_noisy_text(text):
                continue
            if text not in claim_texts:
                claim_texts.append(text)
    if not claim_texts:
        return _trim_text(plan.brief.objective, limit=220)
    summary = " ".join(claim_texts[:2])
    section_confidences = {str(section.get("confidence", "")) for section in sections if section.get("confidence")}
    confidence_prefix = ""
    if "high" in section_confidences:
        confidence_prefix = "High confidence: "
    elif "medium" in section_confidences:
        confidence_prefix = "Medium confidence: "
    elif "low" in section_confidences:
        confidence_prefix = "Low confidence: "
    if len(claim_texts) == 1:
        summary = f"{plan.brief.objective}: {summary}"
    return _trim_text(f"{confidence_prefix}{summary}".strip(), limit=320)


def _report_artifact_contract_error(job: DeepResearchJob) -> dict[str, str]:
    errors: dict[str, str] = {}
    if job.status == "completed":
        for kind in ("sources.json", "citations.json", "report.json", "final_report.md"):
            errors[kind] = "missing_required_artifact"
    return errors


def _parse_utc_iso(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return None


def _parse_json_object(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        raise ValueError("planner returned empty content")
    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    parsed = json.loads(text)
    if isinstance(parsed, str):
        nested = parsed.strip()
        if nested.startswith("{") or nested.startswith("["):
            parsed = json.loads(nested)
    if not isinstance(parsed, dict):
        raise ValueError("planner returned non-object json")
    return parsed


def _parse_json_object_with_trace(value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    text = (value or "").strip()
    trace = {
        "raw_preview": _trim_text(text, limit=280),
        "raw_length": len(text),
        "fenced_json_detected": False,
        "parse_path": "direct_json",
    }
    if not text:
        raise ValueError("planner returned empty content")
    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        trace["fenced_json_detected"] = True
        trace["parse_path"] = "fenced_json"
        text = fenced.group(1).strip()
    parsed = json.loads(text)
    trace["parsed_type"] = type(parsed).__name__
    if isinstance(parsed, str):
        nested = parsed.strip()
        if nested.startswith("{") or nested.startswith("["):
            trace["parse_path"] = "string_wrapped_json"
            parsed = json.loads(nested)
            trace["parsed_type"] = type(parsed).__name__
    if not isinstance(parsed, dict):
        raise ValueError("planner returned non-object json")
    return parsed, trace


def _safe_load_json_artifact(value: str | None) -> tuple[Any | None, str | None]:
    if value is None:
        return None, None
    try:
        return json.loads(value), None
    except Exception:
        return None, "invalid_json"


def _validate_json_artifact_shape(kind: str, value: Any) -> str | None:
    if value is None:
        return None
    if kind == "sources.json":
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return "invalid_shape"
        return None
    if kind == "report.json":
        if not isinstance(value, dict):
            return "invalid_shape"
        sections = value.get("sections", [])
        if not isinstance(sections, list):
            return "invalid_shape"
        for section in sections:
            if not isinstance(section, dict):
                return "invalid_shape"
            claims = section.get("claims", [])
            if not isinstance(claims, list) or not all(
                isinstance(claim, dict)
                and isinstance(claim.get("text"), str)
                and bool(_normalize_whitespace(claim.get("text", "")))
                for claim in claims
            ):
                return "invalid_shape"
        return None
    if kind == "citations.json":
        normalized = _normalize_citations_payload(value)
        if not isinstance(normalized, dict):
            return "invalid_shape"
        if not isinstance(normalized.get("source_registry"), dict):
            return "invalid_shape"
        if not isinstance(normalized.get("sections"), list):
            return "invalid_shape"
        for section in normalized.get("sections", []):
            if not isinstance(section, dict):
                return "invalid_shape"
            claims = section.get("claims", [])
            if not isinstance(claims, list) or not all(
                isinstance(claim, dict)
                and isinstance(claim.get("text"), str)
                and bool(_normalize_whitespace(claim.get("text", "")))
                for claim in claims
            ):
                return "invalid_shape"
        return None
    return None


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _current_final_artifact_bundle(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    artifacts = [artifact for artifact in store.list_artifacts(job_id) if artifact.kind in _FINAL_ARTIFACT_KINDS]
    if len(artifacts) != len(_FINAL_ARTIFACT_KINDS):
        return None
    batch_ids = {str(artifact.metadata.get("batch_id", "")).strip() for artifact in artifacts}
    if len(batch_ids) != 1:
        return None
    batch_id = next(iter(batch_ids))
    if not batch_id:
        return None
    paths: dict[str, Path] = {}
    for artifact in artifacts:
        path = store.root_dir / artifact.path
        if _read_text_if_exists(path) is None:
            return None
        paths[artifact.kind] = path
    return {"batch_id": batch_id, "paths": paths}


def _artifact_bundle_is_usable(bundle: dict[str, Any] | None) -> bool:
    if bundle is None:
        return False
    paths = bundle.get("paths") or {}
    report_value: dict[str, Any] | None = None
    for kind in _FINAL_ARTIFACT_KINDS:
        text = _read_text_if_exists(paths.get(kind))
        if text is None:
            return False
        if kind.endswith(".json"):
            value, error = _safe_load_json_artifact(text)
            if error is not None or _validate_json_artifact_shape(kind, value) is not None:
                return False
            if kind == "report.json" and isinstance(value, dict):
                report_value = value
        elif kind == "final_report.md":
            if not _final_report_text_is_meaningful(text, report_value=report_value):
                return False
    return True


def _complete_batch_bundles(store: DeepResearchStore, job_id: str) -> list[dict[str, Any]]:
    batches_dir = store.artifacts_dir / job_id / "batches"
    if not batches_dir.exists():
        return []
    candidates = sorted(
        (path for path in batches_dir.iterdir() if path.is_dir()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    bundles: list[dict[str, Any]] = []
    for batch_dir in candidates:
        paths = {kind: batch_dir / kind for kind in _FINAL_ARTIFACT_KINDS}
        if all(_read_text_if_exists(path) is not None for path in paths.values()):
            bundles.append({"batch_id": batch_dir.name, "paths": paths})
    return bundles


def _latest_complete_batch_bundle(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    candidates = _complete_batch_bundles(store, job_id)
    return candidates[0] if candidates else None


def _resolve_final_artifact_bundle(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    current_bundle = _current_final_artifact_bundle(store, job_id)
    if _artifact_bundle_is_usable(current_bundle):
        return current_bundle
    first_candidate: dict[str, Any] | None = None
    for candidate in _complete_batch_bundles(store, job_id):
        if first_candidate is None:
            first_candidate = candidate
        if current_bundle is not None and candidate.get("batch_id") == current_bundle.get("batch_id"):
            continue
        if _artifact_bundle_is_usable(candidate):
            return candidate
    return current_bundle or first_candidate


def _final_artifact_bundle_is_usable(store: DeepResearchStore, job_id: str) -> bool:
    bundle = _resolve_final_artifact_bundle(store, job_id)
    return _artifact_bundle_is_usable(bundle)


def _artifact_bundle_differs_from_current(store: DeepResearchStore, job_id: str, bundle: dict[str, Any] | None) -> bool:
    if bundle is None:
        return False
    current_bundle = _current_final_artifact_bundle(store, job_id)
    if current_bundle is None:
        return True
    return current_bundle.get("batch_id") != bundle.get("batch_id")


def _artifact_bundle_identity(store: DeepResearchStore, job_id: str) -> str:
    bundle = _resolve_final_artifact_bundle(store, job_id)
    if _artifact_bundle_is_usable(bundle):
        return str(bundle.get("batch_id", "") or "")
    return ""


def _artifact_content_type(kind: str) -> str:
    if kind.endswith(".md"):
        return "text/markdown"
    if kind.endswith(".json"):
        return "application/json"
    return "text/plain"


def _artifact_payloads(
    store: DeepResearchStore,
    job_id: str,
    *,
    final_bundle: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current_artifacts = {artifact.kind: artifact for artifact in store.list_artifacts(job_id)}
    payloads: list[dict[str, Any]] = []
    ordered_kinds = [
        "plan.json",
        "planner_trace.json",
        "continuation.json",
        "partial_report.md",
        *_FINAL_ARTIFACT_KINDS,
        "coverage.json",
        "grounding.json",
    ]
    for kind in ordered_kinds:
        artifact = current_artifacts.get(kind)
        if final_bundle is not None and kind in final_bundle.get("paths", {}):
            path = final_bundle["paths"][kind]
            payloads.append(
                {
                    "job_id": job_id,
                    "kind": kind,
                    "path": str(path.relative_to(store.root_dir)),
                    "content_type": artifact.content_type if artifact is not None else _artifact_content_type(kind),
                    "created_at": artifact.created_at if artifact is not None else "",
                    "updated_at": artifact.updated_at if artifact is not None else "",
                    "metadata": _artifact_metadata(
                        _read_text_if_exists(path) or "",
                        batch_id=final_bundle["batch_id"],
                    ),
                }
            )
            continue
        if artifact is not None:
            payloads.append(artifact.model_dump())
    return payloads


def _job_prefers_resolved_final_bundle(job: DeepResearchJob) -> bool:
    if job.status == "completed":
        return True
    return job.status == "interrupted" and (job.current_checkpoint == "finalizing" or job.phase == "finalizing")


def _normalize_depends_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [] 


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        normalized = _normalize_whitespace(value)
        return [normalized] if normalized else []
    if isinstance(value, list):
        return [normalized for item in value if (normalized := _normalize_whitespace(str(item)))]
    return []


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _normalize_search_strategy_approach(
    value: Any,
    *,
    normalize_actions: list[str],
    validation_issues: list[str],
) -> str:
    normalized = _normalize_whitespace(str(value or ""))
    if normalized in _VALID_SEARCH_STRATEGY_APPROACHES:
        return normalized
    if normalized:
        _append_unique(validation_issues, "invalid_search_strategy_approach")
        _append_unique(
            normalize_actions,
            f"defaulted_invalid_search_strategy_approach:{normalized}->targeted",
        )
    else:
        _append_unique(normalize_actions, "defaulted_missing_search_strategy_approach:targeted")
    return "targeted"


def _normalize_selective_fetch_config(
    value: Any,
    *,
    normalize_actions: list[str],
    validation_issues: list[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        if value is not None:
            _append_unique(normalize_actions, "non_dict_selective_fetch")
        return {"max_urls_per_search": 1, "prefer_titles_matching_outline": True}

    normalized = dict(value)
    max_urls_raw = normalized.get("max_urls_per_search", 1)
    try:
        max_urls = int(max_urls_raw)
    except (TypeError, ValueError):
        max_urls = 1
        _append_unique(validation_issues, "invalid_selective_fetch_max_urls")
        _append_unique(
            normalize_actions,
            f"defaulted_invalid_selective_fetch_max_urls:{max_urls_raw!s}->1",
        )
    else:
        if max_urls < 0:
            _append_unique(validation_issues, "invalid_selective_fetch_max_urls")
            _append_unique(normalize_actions, f"clamped_selective_fetch_max_urls:{max_urls}->1")
            max_urls = 1
        elif max_urls > 10:
            _append_unique(validation_issues, "invalid_selective_fetch_max_urls")
            _append_unique(normalize_actions, f"clamped_selective_fetch_max_urls:{max_urls}->10")
            max_urls = 10

    prefer_titles = normalized.get("prefer_titles_matching_outline", True)
    if not isinstance(prefer_titles, bool):
        _append_unique(validation_issues, "invalid_selective_fetch_prefer_titles_matching_outline")
        _append_unique(
            normalize_actions,
            f"defaulted_invalid_selective_fetch_prefer_titles_matching_outline:{prefer_titles!s}->True",
        )
        prefer_titles = True

    return {
        "max_urls_per_search": max_urls,
        "prefer_titles_matching_outline": prefer_titles,
    }


def _normalize_brief_payload(
    value: Any,
    *,
    job: DeepResearchJob,
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        if value is not None:
            normalize_actions.append("non_dict_brief")
        objective = _rewrite_research_query(job.query, continuation)
        return {
            "objective": objective,
            "deliverable": "A structured deep research report with citations.",
            "success_criteria": ["Answer the query with source-backed sections."],
            "must_cover": [objective],
            "out_of_scope": [],
            "preferred_sources": list(job.include_domains),
            "stop_policy": {},
            "continuation_focus": [],
        }

    normalized = dict(value)
    if not isinstance(normalized.get("success_criteria"), list):
        normalize_actions.append("string_success_criteria")
        normalized["success_criteria"] = _normalize_string_list(normalized.get("success_criteria"))
    normalized["must_cover"] = _normalize_string_list(normalized.get("must_cover"))
    normalized["out_of_scope"] = _normalize_string_list(normalized.get("out_of_scope"))
    normalized["preferred_sources"] = _normalize_string_list(normalized.get("preferred_sources"))
    if not isinstance(normalized.get("scope"), dict):
        if normalized.get("scope") is not None:
            normalize_actions.append("non_dict_scope")
        normalized["scope"] = {}
    if not isinstance(normalized.get("coverage_checklist"), list):
        if normalized.get("coverage_checklist") is not None:
            normalize_actions.append("string_coverage_checklist")
        normalized["coverage_checklist"] = _normalize_string_list(normalized.get("coverage_checklist"))
    if not isinstance(normalized.get("stop_policy"), dict):
        if normalized.get("stop_policy") is not None:
            normalize_actions.append("non_dict_stop_policy")
        normalized["stop_policy"] = {}
    normalized["continuation_focus"] = _normalize_string_list(normalized.get("continuation_focus"))
    return normalized


def _finalize_brief_payload(
    brief: dict[str, Any],
    *,
    job: DeepResearchJob,
    continuation: DeepResearchContinuationState,
    sub_questions: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(brief)
    objective = _normalize_whitespace(str(normalized.get("objective") or _rewrite_research_query(job.query, continuation)))
    normalized["objective"] = objective or _rewrite_research_query(job.query, continuation)
    normalized["deliverable"] = _normalize_whitespace(
        str(normalized.get("deliverable") or "A structured deep research report with citations.")
    ) or "A structured deep research report with citations."
    success_criteria = _normalize_string_list(normalized.get("success_criteria"))
    if not success_criteria:
        success_criteria = ["Answer the query with source-backed sections."]
    normalized["success_criteria"] = success_criteria

    must_cover = _normalize_string_list(normalized.get("must_cover"))
    if not must_cover:
        must_cover = [
            question
            for item in sub_questions
            if isinstance(item, dict)
            for question in [_normalize_whitespace(str(item.get("question", "")))]
            if question
        ] or [_rewrite_research_query(job.query, continuation)]
    normalized["must_cover"] = _dedupe_preserve_order(must_cover)
    normalized["out_of_scope"] = _dedupe_preserve_order(_normalize_string_list(normalized.get("out_of_scope")))

    preferred_sources = _normalize_string_list(normalized.get("preferred_sources"))
    if not preferred_sources and job.include_domains:
        preferred_sources = list(job.include_domains)
    normalized["preferred_sources"] = _dedupe_preserve_order(preferred_sources)
    scope = dict(normalized.get("scope") or {})
    scope.setdefault("allowed_sources", list(normalized["preferred_sources"]))
    scope.setdefault("include_domains", list(job.include_domains))
    scope.setdefault("exclude_domains", list(job.exclude_domains))
    scope.setdefault("continuation_mode", continuation.mode)
    normalized["scope"] = scope

    continuation_focus = _normalize_string_list(normalized.get("continuation_focus"))
    if not continuation_focus and continuation.mode == "continue":
        continuation_focus = _dedupe_preserve_order(
            [
                item
                for item in (
                    continuation.continuation_goal,
                    *continuation.confirmed_claims,
                    *continuation.open_questions,
                    *continuation.trusted_source_headers,
                )
                if _normalize_whitespace(item)
            ]
        )
    normalized["continuation_focus"] = continuation_focus

    stop_policy = dict(normalized.get("stop_policy") or {})
    search_queries = _normalize_string_list(strategy.get("search_queries"))
    selective_fetch = strategy.get("selective_fetch") if isinstance(strategy.get("selective_fetch"), dict) else {}
    stop_policy.setdefault("stop_on_sufficient_coverage", True)
    stop_policy.setdefault("max_search_queries", max(1, len(search_queries) or config.deep_research_max_concurrency))
    stop_policy.setdefault("max_urls_per_search", int(selective_fetch.get("max_urls_per_search", 1) or 1))
    stop_policy.setdefault("max_runtime_seconds", int(job.resolved_budget_seconds))
    normalized["stop_policy"] = stop_policy
    coverage_checklist = _normalize_string_list(normalized.get("coverage_checklist"))
    if not coverage_checklist:
        coverage_checklist = list(normalized["must_cover"])
    normalized["coverage_checklist"] = _dedupe_preserve_order(coverage_checklist)
    return normalized


def _unsafe_plan_reason(
    *,
    planner: str,
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
    blocked_reasons: list[str],
) -> dict[str, Any] | None:
    if planner == "fallback":
        return None
    for action in normalize_actions:
        if any(action.startswith(prefix) for prefix in _UNSAFE_NORMALIZE_ACTION_PREFIXES):
            return {"issue": action, "reason": "unsafe_normalize_action"}
    for issue in validation_issues:
        if issue in _UNSAFE_VALIDATION_ISSUES:
            return {"issue": issue, "reason": "unsafe_validation_issue"}
        if continuation.mode == "continue" and issue == "generic_continuation_outline":
            return {"issue": issue, "reason": "unsafe_continuation_outline"}
    if blocked_reasons:
        return {"issue": blocked_reasons[0], "reason": "blocked_plan_dependency"}
    return None


def _first_url_from_texts(*values: str) -> str:
    for value in values:
        urls = extract_unique_urls(value or "")
        if urls:
            return urls[0]
    return ""


def _plan_payload_hash(value: Any) -> str:
    if value is None:
        return ""
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return ""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _continuation_identity_for_source_job(
    store: DeepResearchStore,
    job: DeepResearchJob,
    *,
    plan_payload: dict[str, Any] | None = None,
) -> str:
    payload = {
        "source_job_id": job.job_id,
        "status": job.status,
        "current_checkpoint": job.current_checkpoint,
        "resolved_artifact_batch_id": _artifact_bundle_identity(store, job.job_id),
        "plan_hash": _plan_payload_hash(plan_payload),
    }
    return hashlib.sha256(_json_markdown_block(payload).encode("utf-8")).hexdigest()


def _unit_query_fallback(
    unit: dict[str, Any],
    *,
    sub_questions: list[dict[str, Any]],
    job_query: str,
    continuation: DeepResearchContinuationState,
) -> str:
    candidates = [
        str(unit.get("query", "")),
        str(unit.get("goal", "")),
        str(unit.get("title", "")),
        str(unit.get("notes", "")),
        str(unit.get("instructions", "")),
    ]
    candidates.extend(str(item.get("question", "")) for item in sub_questions)
    candidates.append(job_query)
    for candidate in candidates:
        normalized = _normalize_whitespace(candidate)
        if normalized:
            return _rewrite_research_query(_trim_text(normalized, limit=220), continuation)
    return _rewrite_research_query(job_query, continuation)


def _repair_research_units(
    units: list[dict[str, Any]],
    *,
    sub_questions: list[dict[str, Any]],
    job_query: str,
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
    blocked_reasons: list[str],
) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    duplicate_counts: dict[str, int] = {}
    for unit in units:
        normalized = dict(unit)
        base_unit_id = _normalize_whitespace(str(normalized.get("unit_id", ""))) or "unit"
        unit_id = base_unit_id
        if unit_id in used_ids:
            duplicate_counts[base_unit_id] = duplicate_counts.get(base_unit_id, 1) + 1
            unit_id = f"{base_unit_id}-{duplicate_counts[base_unit_id]}"
            _append_unique(validation_issues, "duplicate_unit_id")
            _append_unique(normalize_actions, f"renamed_duplicate_unit_id:{base_unit_id}->{unit_id}")
        else:
            duplicate_counts.setdefault(base_unit_id, 1)
        normalized["unit_id"] = unit_id
        used_ids.add(unit_id)

        unit_type = str(normalized.get("unit_type", "search") or "search").strip()
        normalized["unit_type"] = unit_type
        if unit_type == "search":
            if not _normalize_whitespace(str(normalized.get("query", ""))):
                normalized["query"] = _unit_query_fallback(
                    normalized,
                    sub_questions=sub_questions,
                    job_query=job_query,
                    continuation=continuation,
                )
                _append_unique(validation_issues, "missing_search_query")
                _append_unique(normalize_actions, f"filled_search_query:{unit_id}")
        elif unit_type in {"fetch", "map"}:
            url = _normalize_whitespace(str(normalized.get("url", "")))
            if not url:
                url = _first_url_from_texts(
                    str(normalized.get("instructions", "")),
                    str(normalized.get("notes", "")),
                    str(normalized.get("goal", "")),
                    str(normalized.get("title", "")),
                )
            if url:
                normalized["url"] = url
                _append_unique(normalize_actions, f"filled_{unit_type}_url:{unit_id}")
            else:
                normalized["unit_type"] = "search"
                normalized["query"] = _unit_query_fallback(
                    normalized,
                    sub_questions=sub_questions,
                    job_query=job_query,
                    continuation=continuation,
                )
                _append_unique(validation_issues, f"missing_{unit_type}_url")
                _append_unique(normalize_actions, f"degraded_{unit_type}_without_url_to_search:{unit_id}")
        repaired.append(normalized)

    index_by_id = {unit["unit_id"]: index for index, unit in enumerate(repaired)}
    for index, unit in enumerate(repaired):
        repaired_deps: list[str] = []
        for dependency in _dedupe_preserve_order(_normalize_depends_on(unit.get("depends_on"))):
            if dependency == unit["unit_id"]:
                _append_unique(validation_issues, "self_dependency")
                _append_unique(blocked_reasons, f"self_dependency:{unit['unit_id']}")
                _append_unique(normalize_actions, f"dropped_self_dependency:{unit['unit_id']}")
                continue
            dep_index = index_by_id.get(dependency)
            if dep_index is None:
                _append_unique(validation_issues, "unknown_dependency")
                _append_unique(blocked_reasons, f"unknown_dependency:{unit['unit_id']}->{dependency}")
                _append_unique(normalize_actions, f"dropped_unknown_dependency:{unit['unit_id']}->{dependency}")
                continue
            if dep_index >= index:
                _append_unique(validation_issues, "forward_or_cyclic_dependency")
                _append_unique(blocked_reasons, f"forward_or_cyclic_dependency:{unit['unit_id']}->{dependency}")
                _append_unique(normalize_actions, f"dropped_forward_or_cyclic_dependency:{unit['unit_id']}->{dependency}")
                continue
            repaired_deps.append(dependency)
        unit["depends_on"] = repaired_deps
    return repaired


def _unit_covers_sub_question(unit: dict[str, Any], sub_question: dict[str, Any]) -> bool:
    question = _normalize_whitespace(str(sub_question.get("question", "")))
    if not question:
        return False
    haystack = " ".join(
        _normalize_whitespace(str(unit.get(key, "")))
        for key in ("title", "goal", "query", "notes", "instructions")
    )
    if not haystack:
        return False
    question_key = _stable_text_key(question)
    haystack_key = _stable_text_key(haystack)
    if question_key and question_key in haystack_key:
        return True
    question_tokens = _tokenize_keywords(question)
    if not question_tokens:
        return False
    overlap = _count_keyword_overlap(haystack, question_tokens)
    return overlap >= max(1, min(2, len(question_tokens)))


def _ensure_sub_question_unit_coverage(
    units: list[dict[str, Any]],
    *,
    sub_questions: list[dict[str, Any]],
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
) -> list[dict[str, Any]]:
    if len(sub_questions) <= 1:
        return list(units)
    covered_units = list(units)
    used_ids = {str(unit.get("unit_id", "")).strip() for unit in covered_units}
    auto_index = 1
    for sub_question in sub_questions:
        if any(_unit_covers_sub_question(unit, sub_question) for unit in covered_units):
            continue
        while f"unit-search-auto-{auto_index}" in used_ids:
            auto_index += 1
        unit_id = f"unit-search-auto-{auto_index}"
        used_ids.add(unit_id)
        query = _rewrite_research_query(str(sub_question.get("question", "")), continuation)
        covered_units.append(
            {
                "unit_id": unit_id,
                "unit_type": "search",
                "title": _trim_text(query, limit=96),
                "goal": query,
                "query": query,
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        )
        _append_unique(validation_issues, "missing_sub_question_unit_coverage")
        _append_unique(normalize_actions, f"added_sub_question_search_unit:{sub_question.get('id', unit_id)}")
    return covered_units


def _outline_from_sub_questions(
    outline: list[dict[str, Any]],
    *,
    sub_questions: list[dict[str, Any]],
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
) -> list[dict[str, Any]]:
    if continuation.mode == "continue" or len(sub_questions) <= 1:
        return outline
    generic_titles = {"executive summary", "key findings", "open questions", "summary"}
    if not outline:
        return outline
    first_title = str(outline[0].get("title", "")).strip()
    remainder = outline[1:]
    if remainder and not all(str(item.get("title", "")).strip().lower() in generic_titles for item in remainder):
        return outline
    summary_section = outline[0] if first_title.lower() == "executive summary" else {
        "section_id": "executive-summary",
        "title": "Executive Summary",
        "goal": "Summarize the answer.",
    }
    expanded = [summary_section]
    for item in sub_questions:
        title = _trim_text(_normalize_whitespace(str(item.get("question", "")).rstrip(" ?")), limit=96)
        if not title:
            continue
        expanded.append(
            {
                "section_id": _slugify(title),
                "title": title,
                "goal": title,
            }
        )
    if len(expanded) <= len(outline):
        return outline
    _append_unique(validation_issues, "generic_outline_for_sub_questions")
    _append_unique(normalize_actions, "expanded_outline_from_sub_questions")
    return expanded


def _dedupe_sub_questions(sub_questions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    changed = False
    for item in sub_questions:
        question = _normalize_whitespace(str(item.get("question", "")))
        key = question.lower()
        if not key:
            changed = True
            continue
        if key in seen:
            changed = True
            continue
        seen.add(key)
        deduped.append(item)
    return deduped, changed


def _is_generic_outline(report_outline: list[dict[str, Any]]) -> bool:
    titles: list[str] = []
    for item in report_outline:
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip().lower()
        else:
            title = str(item).strip().lower()
        if title:
            titles.append(title)
    return titles == ["executive summary", "key findings", "open questions"]


def _validate_research_units(units: list[DeepResearchResearchUnit]) -> None:
    known_ids = {unit.unit_id for unit in units}
    for unit in units:
        unknown = [dependency for dependency in unit.depends_on if dependency not in known_ids]
        if unknown:
            raise ValueError(f"unknown dependencies for {unit.unit_id}: {', '.join(unknown)}")

    visiting: set[str] = set()
    visited: set[str] = set()
    units_by_id = {unit.unit_id: unit for unit in units}

    def visit(unit_id: str) -> None:
        if unit_id in visited:
            return
        if unit_id in visiting:
            raise ValueError(f"cyclic dependency detected at {unit_id}")
        visiting.add(unit_id)
        for dependency in units_by_id[unit_id].depends_on:
            visit(dependency)
        visiting.remove(unit_id)
        visited.add(unit_id)

    for unit in units:
        visit(unit.unit_id)


def _sanitize_source_id_list(source_ids: list[str], source_registry: list[dict[str, Any]]) -> list[str]:
    valid_ids = {item["source_id"] for item in source_registry if item.get("source_id")}
    return [source_id for source_id in source_ids if source_id in valid_ids]


def _sanitize_unit_results(
    unit_results: dict[str, dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    sanitized: dict[str, dict[str, Any]] = {}
    for unit_id, result in unit_results.items():
        normalized = dict(result)
        normalized["source_ids"] = _sanitize_source_id_list(list(normalized.get("source_ids", [])), source_registry)
        normalized["citations"] = _sanitize_source_id_list(list(normalized.get("citations", [])), source_registry)
        normalized["summary"] = _summarize_evidence_text(
            str(normalized.get("summary") or normalized.get("detail") or ""),
            limit=180,
        )
        normalized["detail"] = _sanitize_detail_text(str(normalized.get("detail") or normalized.get("summary") or ""))
        sanitized[unit_id] = normalized
    return sanitized


def _sanitize_evidence_items(
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in evidence_items:
        normalized = dict(item)
        summary = _summarize_evidence_text(normalized.get("summary") or normalized.get("detail") or "")
        if not summary or _is_noisy_text(summary):
            continue
        normalized["source_ids"] = _sanitize_source_id_list(list(normalized.get("source_ids", [])), source_registry)
        normalized["source_urls"] = _dedupe_preserve_order(
            [str(url) for url in normalized.get("source_urls", []) if str(url).strip()]
        )
        normalized["summary"] = summary
        normalized["detail"] = _sanitize_detail_text(str(normalized.get("detail") or normalized.get("summary") or ""))
        sanitized.append(normalized)
    return sanitized


def _sanitize_sections(
    sections: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for section in sections:
        normalized_section = dict(section)
        normalized_claims: list[dict[str, Any]] = []
        for claim in section.get("claims", []):
            normalized_claim = dict(claim)
            normalized_claim["citations"] = _sanitize_source_id_list(list(normalized_claim.get("citations", [])), source_registry)
            normalized_claim["text"] = _summarize_evidence_text(str(normalized_claim.get("text", "")), limit=_MAX_CLAIM_LENGTH)
            if not normalized_claim["citations"] or not normalized_claim["text"] or _is_noisy_text(normalized_claim["text"]):
                continue
            normalized_claims.append(normalized_claim)
        normalized_section["claims"] = normalized_claims
        normalized_section["summary"] = _summarize_evidence_text(str(normalized_section.get("summary", "")), limit=260)
        normalized_section["citations"] = sorted(
            {citation for claim in normalized_claims for citation in claim.get("citations", [])}
        )
        if not normalized_claims:
            continue
        sanitized.append(normalized_section)
    return sanitized


def _sanitize_continuation_sections(
    sections: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for section in sections:
        normalized_section = dict(section)
        title = str(normalized_section.get("title", "")).strip()
        if not title:
            continue
        normalized_section["summary"] = _summarize_evidence_text(str(normalized_section.get("summary", "")), limit=260)
        normalized_claims: list[dict[str, Any]] = []
        for claim in section.get("claims", []):
            normalized_claim = dict(claim)
            normalized_claim["citations"] = _sanitize_source_id_list(list(normalized_claim.get("citations", [])), source_registry)
            normalized_claim["text"] = _summarize_evidence_text(str(normalized_claim.get("text", "")), limit=_MAX_CLAIM_LENGTH)
            if not normalized_claim["text"] or _is_noisy_text(normalized_claim["text"]):
                continue
            normalized_claims.append(normalized_claim)
        normalized_section["claims"] = normalized_claims
        normalized_section["citations"] = sorted(
            {citation for claim in normalized_claims for citation in claim.get("citations", [])}
        )
        if not normalized_section["summary"] and normalized_claims:
            normalized_section["summary"] = _build_section_summary(normalized_claims)
        if normalized_section["summary"] or normalized_claims:
            sanitized.append(normalized_section)
    return sanitized


def _continuation_used_source_ids(
    unit_results: dict[str, dict[str, Any]],
    evidence_items: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> set[str]:
    used_source_ids: set[str] = set()
    for result in unit_results.values():
        used_source_ids.update(str(source_id) for source_id in result.get("source_ids", []) if str(source_id).strip())
        used_source_ids.update(str(source_id) for source_id in result.get("citations", []) if str(source_id).strip())
    for item in evidence_items:
        used_source_ids.update(str(source_id) for source_id in item.get("source_ids", []) if str(source_id).strip())
    for section in sections:
        used_source_ids.update(str(source_id) for source_id in section.get("citations", []) if str(source_id).strip())
        for claim in section.get("claims", []):
            used_source_ids.update(str(source_id) for source_id in claim.get("citations", []) if str(source_id).strip())
    return used_source_ids


def _source_is_high_trust(source: dict[str, Any]) -> bool:
    source_type = str(source.get("source_type", "") or "").strip().lower()
    return source_type in {"official_docs", "standard", "paper"}


def _focused_continuation_sources(
    sources: list[dict[str, Any]],
    *,
    used_source_ids: set[str],
) -> list[dict[str, Any]]:
    if used_source_ids:
        return [
            dict(source)
            for source in sources
            if str(source.get("source_id", "")).strip() in used_source_ids
        ]
    focused: list[dict[str, Any]] = []
    for source in sources:
        source_id = str(source.get("source_id", "")).strip()
        if not source_id:
            continue
        if source_id in used_source_ids or _source_is_high_trust(source):
            focused.append(dict(source))
    if focused:
        return focused
    return [dict(source) for source in sources if str(source.get("source_id", "")).strip()]


def _best_continuation_summary(
    *,
    report: dict[str, Any],
    final_report: str,
    partial_report: str,
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
    prior_plan_summary: str,
    job: DeepResearchJob,
) -> str:
    candidates: list[str] = []
    for section in sections:
        candidates.append(str(section.get("summary") or ""))
        for claim in section.get("claims", []):
            candidates.append(str(claim.get("text") or ""))
    for result in unit_results.values():
        candidates.append(str(result.get("summary") or result.get("detail") or ""))
    if report:
        candidates.append(str(report.get("summary") or ""))
    for body in (final_report, partial_report):
        lines = [line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")]
        if lines:
            candidates.extend(lines[:2])
    if prior_plan_summary:
        candidates.append(prior_plan_summary)
    candidates.append(job.query)
    for candidate in candidates:
        summary = _summarize_evidence_text(candidate, limit=220)
        if summary and not _is_noisy_text(summary):
            return summary
    return _trim_text(job.query, limit=220)


def _collect_continuation_open_questions(report: dict[str, Any]) -> list[str]:
    coverage = report.get("coverage") if isinstance(report, dict) else {}
    if not isinstance(coverage, dict):
        return []
    items: list[str] = []
    for value in coverage.get("uncovered_sub_questions", []) or []:
        normalized = _normalize_whitespace(str(value))
        if normalized:
            items.append(normalized)
    for value in coverage.get("unanswered_sections", []) or []:
        normalized = _normalize_whitespace(str(value))
        if normalized:
            items.append(normalized)
    return _dedupe_preserve_order(items)


def _collect_confirmed_claims(sections: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    claims: list[str] = []
    for section in sections:
        for claim in section.get("claims", []):
            text = _summarize_evidence_text(str(claim.get("text") or ""), limit=180)
            if not text or _is_noisy_text(text):
                continue
            claims.append(text)
            if len(claims) >= limit:
                return _dedupe_preserve_order(claims)
    return _dedupe_preserve_order(claims)


def _trusted_source_headers(sources: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    headers: list[str] = []
    for source in sources:
        if not _source_is_high_trust(source):
            continue
        title = _normalize_whitespace(str(source.get("title") or ""))
        domain = _normalize_whitespace(str(source.get("domain") or ""))
        header = title if not domain else f"{title} ({domain})" if title else domain
        if header:
            headers.append(header)
        if len(headers) >= limit:
            break
    return _dedupe_preserve_order(headers)


def _carry_forward_constraints(
    *,
    job: DeepResearchJob,
    plan_payload: dict[str, Any],
) -> dict[str, Any]:
    plan_brief = plan_payload.get("brief") if isinstance(plan_payload, dict) else {}
    if not isinstance(plan_brief, dict):
        plan_brief = {}
    plan_scope = plan_brief.get("scope") if isinstance(plan_brief.get("scope"), dict) else {}
    preferred_sources = _normalize_string_list(plan_brief.get("preferred_sources"))
    allowed_sources = _normalize_string_list(plan_scope.get("allowed_sources"))
    return {
        "include_domains": _dedupe_preserve_order(
            _normalize_string_list(plan_scope.get("include_domains")) or list(job.include_domains)
        ),
        "exclude_domains": _dedupe_preserve_order(
            _normalize_string_list(plan_scope.get("exclude_domains")) or list(job.exclude_domains)
        ),
        "preferred_sources": _dedupe_preserve_order(preferred_sources or allowed_sources),
        "allowed_sources": _dedupe_preserve_order(allowed_sources or preferred_sources),
    }


def _result_text_covers_item(result: dict[str, Any], item: str) -> bool:
    normalized_item = _normalize_whitespace(item)
    if not normalized_item:
        return False
    tokens = _tokenize_keywords(normalized_item)
    if not tokens:
        return False
    threshold = max(2, min(4, max(1, len(tokens) // 2)))
    haystack = " ".join(
        [
            _normalize_whitespace(str(result.get("summary") or "")),
            _normalize_whitespace(str(result.get("detail") or "")),
        ]
    )
    if not haystack:
        return False
    return _count_keyword_overlap(haystack, tokens) >= threshold


def _stop_policy_targets(plan: DeepResearchPlan) -> list[str]:
    targets = _normalize_string_list(plan.brief.coverage_checklist)
    if targets:
        return targets
    targets = _normalize_string_list(plan.brief.must_cover)
    if targets:
        return targets
    return [item.question for item in plan.sub_questions if _normalize_whitespace(item.question)]


def _has_sufficient_runtime_coverage(
    plan: DeepResearchPlan,
    unit_results: dict[str, dict[str, Any]],
) -> bool:
    targets = _stop_policy_targets(plan)
    if not targets:
        return False
    if not unit_results:
        return False
    for target in targets:
        if not any(
            (result.get("source_ids") or result.get("citations"))
            and _result_text_covers_item(result, target)
            for result in unit_results.values()
        ):
            return False
    return True


def _artifact_metadata(content: str, *, batch_id: str = "") -> dict[str, Any]:
    metadata = {"bytes": len(content.encode("utf-8"))}
    if batch_id:
        metadata["batch_id"] = batch_id
    return metadata


def _build_carry_forward_evidence(
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_items: list[dict[str, Any]] = []
    for unit_id, result in unit_results.items():
        summary = _summarize_evidence_text(result.get("summary") or result.get("detail") or "")
        if not summary:
            continue
        evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"carry-forward-{unit_id}",
                unit_id=unit_id,
                source_ids=list(result.get("source_ids") or result.get("citations") or []),
                source_urls=[],
                summary=summary,
                detail=result.get("detail", ""),
            ).model_dump()
        )
    if evidence_items:
        return evidence_items

    for section in sections:
        for index, claim in enumerate(section.get("claims", []), start=1):
            text = _summarize_evidence_text(claim.get("text", ""))
            if not text:
                continue
            evidence_items.append(
                DeepResearchEvidenceItem(
                    evidence_id=f"carry-forward-{section.get('section_id', 'section')}-{index}",
                    unit_id=section.get("section_id", "section"),
                    source_ids=list(claim.get("citations", [])),
                    source_urls=[],
                    summary=text,
                    detail=claim.get("text", ""),
                ).model_dump()
            )
    return evidence_items


def _hydrate_evidence_source_ids(
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_id_by_url: dict[str, str] = {}
    for item in source_registry:
        url = str(item.get("url", "")).strip()
        source_id = str(item.get("source_id", "")).strip()
        if not url or not source_id:
            continue
        source_id_by_url[url] = source_id
    hydrated: list[dict[str, Any]] = []
    for item in evidence_items:
        normalized = dict(item)
        hydrated_ids = _sanitize_source_id_list(list(normalized.get("source_ids", [])), source_registry)
        if not hydrated_ids:
            hydrated_ids = _dedupe_preserve_order(
                [
                    source_id_by_url[url]
                    for url in normalized.get("source_urls", [])
                    if url in source_id_by_url
                ]
            )
        normalized["source_ids"] = hydrated_ids
        hydrated.append(normalized)
    return hydrated


def _planner_continuation_payload(continuation: DeepResearchContinuationState) -> dict[str, Any]:
    payload = _compact_continuation(continuation).model_dump()
    if continuation.mode != "continue":
        return payload
    payload["carry_forward_sources"] = [
        {
            "source_id": item.get("source_id", ""),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "source_type": item.get("source_type", ""),
        }
        for item in continuation.carry_forward_sources[:5]
        if item.get("url")
    ]
    payload["carry_forward_evidence"] = [
        {
            "evidence_id": item.get("evidence_id", ""),
            "unit_id": item.get("unit_id", ""),
            "summary": _summarize_evidence_text(str(item.get("summary") or item.get("detail") or ""), limit=180),
            "source_ids": list(item.get("source_ids") or []),
        }
        for item in continuation.carry_forward_evidence[:5]
        if _summarize_evidence_text(str(item.get("summary") or item.get("detail") or ""), limit=180)
    ]
    payload["carry_forward_sections"] = [
        {
            "section_id": item.get("section_id", ""),
            "title": item.get("title", ""),
            "summary": _summarize_evidence_text(str(item.get("summary", "")), limit=180),
        }
        for item in continuation.carry_forward_sections[:4]
        if item.get("title")
    ]
    payload["confirmed_claims"] = list(continuation.confirmed_claims[:4])
    payload["open_questions"] = list(continuation.open_questions[:4])
    payload["trusted_source_headers"] = list(continuation.trusted_source_headers[:4])
    payload["carry_forward_constraints"] = dict(continuation.carry_forward_constraints)
    payload.pop("previous_summary", None)
    payload.pop("prior_plan_summary", None)
    return payload


def _source_quality_bias(source: dict[str, Any]) -> int:
    url = str(source.get("url", "")).lower()
    domain = str(source.get("domain", "") or "").lower()
    if not domain and "://" in url:
        try:
            domain = urlsplit(url).netloc.lower()
        except Exception:
            domain = ""

    if url.startswith("https://docs.") or url.startswith("http://docs.") or domain.startswith("docs.") or "/docs/" in url or "/documentation/" in url:
        return 3
    if domain.startswith("standards.") or "standards." in domain or "/rfc" in url or "/spec" in url or "/standard" in url:
        return 3
    if domain == "arxiv.org" or domain.endswith(".arxiv.org") or domain.endswith(".acm.org") or domain.endswith(".ieee.org") or url.endswith(".pdf"):
        return 2
    if domain.startswith("blog.") or "/blog/" in url:
        return -1
    for community in _COMMUNITY_SOURCE_DOMAINS:
        if community in url or domain == community or domain.endswith(f".{community}"):
            return -2
    return 0


def _source_type(source: dict[str, Any]) -> str:
    url = str(source.get("url", "")).lower()
    domain = str(source.get("domain", "") or "").lower()
    if not domain and "://" in url:
        try:
            domain = urlsplit(url).netloc.lower()
        except Exception:
            domain = ""
    if url.startswith("https://docs.") or url.startswith("http://docs.") or domain.startswith("docs.") or "/docs/" in url:
        return "official_docs"
    if domain.startswith("standards.") or "standards." in domain or "/rfc" in url or "/spec" in url or "/standard" in url:
        return "standard"
    if domain == "arxiv.org" or domain.endswith(".arxiv.org") or domain.endswith(".acm.org") or domain.endswith(".ieee.org") or url.endswith(".pdf"):
        return "paper"
    if domain in _COMMUNITY_SOURCE_DOMAINS or any(domain.endswith(f".{item}") for item in _COMMUNITY_SOURCE_DOMAINS):
        return "community"
    return "third_party"


def _source_ranking_reasons(
    source: dict[str, Any],
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    domain = str(source.get("domain", "") or "").lower()
    if not domain and source.get("url"):
        try:
            domain = urlsplit(str(source["url"])).netloc.lower()
        except Exception:
            domain = ""
    if include_domains and any(domain == item or domain.endswith(f".{item}") for item in include_domains):
        reasons.append("allowlisted_domain")
    if exclude_domains and any(domain == item or domain.endswith(f".{item}") for item in exclude_domains):
        reasons.append("denylisted_domain")
    source_type = _source_type(source)
    if source_type:
        reasons.append(source_type)
    if source.get("winner_provider"):
        reasons.append("winner_provider")
    if source.get("title"):
        reasons.append("has_title")
    if source.get("snippet") or source.get("description"):
        reasons.append("has_summary_text")
    if _has_noisy_source_metadata(source):
        reasons.append("noisy_metadata_penalty")
    return reasons


def _source_topic_match_score(source: dict[str, Any], reference_texts: list[str] | None = None) -> int:
    text = " ".join(
        str(source.get(key, ""))
        for key in ("title", "description", "snippet", "url", "domain")
    )
    keywords = _dedupe_preserve_order(
        [token for value in (reference_texts or []) for token in _tokenize_keywords(value)]
    )
    return _count_keyword_overlap(text, keywords) if keywords else 0


def _domain_matches(domain: str, candidates: list[str]) -> bool:
    return any(domain == candidate or domain.endswith(f".{candidate}") for candidate in candidates)


def _apply_domain_constraints(
    sources: list[dict[str, Any]],
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_include = [item.lower() for item in include_domains or [] if item]
    normalized_exclude = [item.lower() for item in exclude_domains or [] if item]
    if not normalized_include and not normalized_exclude:
        return list(sources)
    filtered: list[dict[str, Any]] = []
    for source in sources:
        url = str(source.get("url", "")).strip()
        if not url:
            continue
        domain = str(source.get("domain", "") or "").strip().lower()
        if not domain:
            try:
                domain = urlsplit(url).netloc.lower()
            except Exception:
                domain = ""
        if normalized_include and not _domain_matches(domain, normalized_include):
            continue
        if normalized_exclude and _domain_matches(domain, normalized_exclude):
            continue
        filtered.append(source)
    return filtered


async def _build_runtime_grok_provider(current_model: str) -> tuple[GrokSearchProvider, dict[str, Any]]:
    from . import server as server_module

    provider_chain = config.grok_provider_chain(model_override=current_model)
    primary = dict(provider_chain[0])
    available_models = await server_module._get_available_models_cached(primary["api_url"], primary["api_key"])
    requested_model = primary["model"]
    resolved_model, resolution = server_module._resolve_model_against_available_models(
        requested_model,
        available_models,
    )
    if resolved_model:
        primary["model"] = resolved_model
        for fallback_provider in provider_chain[1:]:
            provider_name = str(fallback_provider.get("name", ""))
            suffix_match = re.fullmatch(r"provider_(\d+)", provider_name)
            explicit_model = (
                config._get_env_value(f"GROK_MODEL_{suffix_match.group(1)}")
                if suffix_match is not None
                else None
            )
            fallback_model = str(fallback_provider.get("model", "") or "")
            inherited_requested_model = fallback_model.split(":", 1)[0] == requested_model.split(":", 1)[0]
            explicit_model_is_distinct = (
                explicit_model is not None
                and str(explicit_model).split(":", 1)[0] != requested_model.split(":", 1)[0]
            )
            if not explicit_model_is_distinct and inherited_requested_model:
                fallback_provider["model"] = config._apply_model_suffix_for_url(resolved_model, fallback_provider["api_url"])
    provider = GrokSearchProvider(
        primary["api_url"],
        primary["api_key"],
        primary["model"],
        fallback_providers=provider_chain[1:],
    )
    return provider, {
        "requested_model": requested_model,
        "effective_model": primary["model"],
        "available_models": available_models,
        "resolution": resolution,
    }


class DeepResearchRuntime:
    def __init__(self, root_dir: Path, *, runner: Runner | None = None):
        self.store = DeepResearchStore(root_dir)
        self._runner = runner or _default_runner
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_lock = asyncio.Lock()
        self._startup_reconciled = False

    async def start(
        self,
        *,
        query: str,
        context: str = "",
        effort: str = "standard",
        time_budget_seconds: int | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        continue_from_job_id: str = "",
        plan_only: bool = False,
        force_new: bool = False,
        schedule: bool = True,
    ) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        source_job = None
        if continue_from_job_id:
            source_job = self.store.get_job(continue_from_job_id)
            if source_job.status not in {"completed", "failed", "interrupted", "canceled"}:
                raise ValueError(
                    f"continue_from_job_id must reference a finished or recoverable job, got {source_job.status}"
                )
        if include_domains is None and source_job is not None:
            normalized_include_domains = list(source_job.include_domains)
        else:
            normalized_include_domains = list(include_domains or [])
        if exclude_domains is None and source_job is not None:
            normalized_exclude_domains = list(source_job.exclude_domains)
        else:
            normalized_exclude_domains = list(exclude_domains or [])
        resolved_budget_seconds = self._resolve_budget_seconds(time_budget_seconds, effort)
        continuation = self._build_continuation_context(continue_from_job_id)
        request_fingerprint = self._request_fingerprint(
            query=query,
            context=context,
            effort=effort,
            include_domains=normalized_include_domains,
            exclude_domains=normalized_exclude_domains,
            continue_from_job_id=continue_from_job_id,
            plan_only=plan_only,
            continuation_identity=continuation.continuation_identity,
        )

        reused_job = None
        if not force_new:
            reused_job = self.store.find_reusable_job(
                request_fingerprint,
                recent_reuse_seconds=config.deep_research_recent_reuse_seconds,
            )
            if reused_job is not None and bool(reused_job.plan_only) != bool(plan_only):
                reused_job = None
            if reused_job is not None and reused_job.status in {"completed", "interrupted"}:
                if not _final_artifact_bundle_is_usable(self.store, reused_job.job_id):
                    reused_job = None
        if reused_job is not None:
            if reused_job.status == "interrupted" and _job_prefers_resolved_final_bundle(reused_job):
                final_bundle = _resolve_final_artifact_bundle(self.store, reused_job.job_id)
                if _artifact_bundle_is_usable(final_bundle):
                    completed_at = reused_job.finished_at or utc_now_iso()
                    reused_job = self.store.update_job(
                        reused_job.job_id,
                        status="completed",
                        phase="finalizing",
                        progress_pct=100.0,
                        finished_at=completed_at,
                        heartbeat_at=utc_now_iso(),
                        last_error="",
                    )
                    self.store.append_event(
                        reused_job.job_id,
                        type="job_resolved_from_final_batch",
                        phase="finalizing",
                        message="Deep research recovered a usable final artifact batch without rerunning finalization.",
                        data={"resolved_artifact_batch_id": final_bundle["batch_id"]},
                    )
            return self._job_payload(reused_job, reused=True)

        initial_status = "draft" if plan_only else "queued"
        job = self.store.create_job(
            query=query,
            request_fingerprint=request_fingerprint,
            status=initial_status,
            phase="planning",
            effort=effort,
            context=context,
            include_domains=normalized_include_domains,
            exclude_domains=normalized_exclude_domains,
            plan_only=plan_only,
            force_new=force_new,
            resolved_budget_seconds=resolved_budget_seconds,
            continued_from_job_id=continue_from_job_id,
        )
        plan = await self._build_plan(job, continuation)
        self.write_artifact(job.job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
        planner_trace = dict(plan.planner_metadata.get("trace") or {})
        if planner_trace:
            self.write_artifact(
                job.job_id,
                "planner_trace.json",
                _json_markdown_block(planner_trace),
                "application/json",
            )
        self.store.save_checkpoint(
            job.job_id,
            phase="planning",
            checkpoint_key="planning",
            state=DeepResearchCheckpointState(
                plan=plan,
                unit_results=dict(continuation.carry_forward_unit_results),
                sources=list(continuation.carry_forward_sources),
                evidence_items=list(continuation.carry_forward_evidence),
                sections=list(continuation.carry_forward_sections),
            ).model_dump(),
        )
        if continuation.mode == "continue":
            self.write_artifact(
                job.job_id,
                "continuation.json",
                _json_markdown_block(continuation.model_dump()),
                "application/json",
            )
        fallback_reason = plan.planner_metadata.get("fallback_reason")
        if isinstance(fallback_reason, dict):
            self.store.append_event(
                job.job_id,
                type="planner_fallback",
                phase="planning",
                message="Planner fell back to the deterministic backup plan.",
                data=fallback_reason,
            )
        self.store.append_event(
            job.job_id,
            type="job_created",
            phase="planning",
            message="Deep research job created.",
            data={"plan_only": plan_only, "continuation_mode": continuation.mode},
        )
        job = self.store.get_job(job.job_id)

        if not plan_only and schedule:
            await self._schedule(job.job_id)

        return self._job_payload(job, reused=False)

    async def status(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        payload = self._serialize_job(job)
        final_bundle = _resolve_final_artifact_bundle(self.store, job_id) if _job_prefers_resolved_final_bundle(job) else None
        artifacts = _artifact_payloads(self.store, job_id, final_bundle=final_bundle)
        payload["artifact_kinds"] = [artifact["kind"] for artifact in artifacts]
        payload["artifacts"] = artifacts
        payload["artifact_fallback_used"] = _artifact_bundle_differs_from_current(self.store, job_id, final_bundle)
        payload["resolved_artifact_batch_id"] = final_bundle["batch_id"] if final_bundle is not None else ""
        return payload

    async def events(self, job_id: str, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        events = self.store.list_events(job_id, after_seq=after_seq, limit=limit)
        return {
            "job_id": job_id,
            "events": [event.model_dump() for event in events],
            "next_after_seq": events[-1].seq if events else after_seq,
        }

    async def result(self, job_id: str, *, include_partial: bool = True) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        plan_text = self.store.read_artifact_text(job_id, "plan.json")
        partial_text = self.store.read_artifact_text(job_id, "partial_report.md") if include_partial else None
        final_bundle = _resolve_final_artifact_bundle(self.store, job_id) if _job_prefers_resolved_final_bundle(job) else None
        final_text = (
            _read_text_if_exists(final_bundle["paths"]["final_report.md"])
            if final_bundle is not None
            else self.store.read_artifact_text(job_id, "final_report.md")
        )
        citations_text = (
            _read_text_if_exists(final_bundle["paths"]["citations.json"])
            if final_bundle is not None
            else self.store.read_artifact_text(job_id, "citations.json")
        )
        report_text = (
            _read_text_if_exists(final_bundle["paths"]["report.json"])
            if final_bundle is not None
            else self.store.read_artifact_text(job_id, "report.json")
        )
        sources_text = (
            _read_text_if_exists(final_bundle["paths"]["sources.json"])
            if final_bundle is not None
            else self.store.read_artifact_text(job_id, "sources.json")
        )
        artifact_errors: dict[str, str] = {}
        plan_value, plan_error = _safe_load_json_artifact(plan_text)
        report_value, report_error = _safe_load_json_artifact(report_text)
        sources_value, sources_error = _safe_load_json_artifact(sources_text)
        citations_value, citations_error = _safe_load_json_artifact(citations_text)
        if plan_error:
            artifact_errors["plan.json"] = plan_error
        if report_error:
            artifact_errors["report.json"] = report_error
        if sources_error:
            artifact_errors["sources.json"] = sources_error
        if citations_error:
            artifact_errors["citations.json"] = citations_error
        citations = _normalize_citations_payload(citations_value)
        for kind, value in (
            ("report.json", report_value),
            ("sources.json", sources_value),
            ("citations.json", citations),
        ):
            shape_error = _validate_json_artifact_shape(kind, value)
            if shape_error:
                artifact_errors[kind] = shape_error
                if kind == "report.json":
                    report_value = None
                elif kind == "sources.json":
                    sources_value = None
                elif kind == "citations.json":
                    citations = None
        required = _report_artifact_contract_error(job)
        for kind, error_code in required.items():
            artifact_text = (
                _read_text_if_exists(final_bundle["paths"][kind])
                if final_bundle is not None and kind in final_bundle["paths"]
                else self.store.read_artifact_text(job_id, kind)
            )
            if artifact_text is None:
                artifact_errors[kind] = error_code
        return {
            "job_id": job_id,
            "status": job.status,
            "phase": job.phase,
            "plan": plan_value,
            "partial_report": partial_text,
            "final_report": final_text,
            "sources": sources_value,
            "citations": citations,
            "report": report_value,
            "artifact_errors": artifact_errors,
            "artifact_fallback_used": _artifact_bundle_differs_from_current(self.store, job_id, final_bundle),
            "resolved_artifact_batch_id": final_bundle["batch_id"] if final_bundle is not None else "",
            "artifacts": _artifact_payloads(self.store, job_id, final_bundle=final_bundle),
        }

    def read_artifact_text(self, job_id: str, kind: str) -> str | None:
        job = self.store.get_job(job_id)
        final_bundle = _resolve_final_artifact_bundle(self.store, job_id) if _job_prefers_resolved_final_bundle(job) else None
        if final_bundle is not None and kind in final_bundle["paths"]:
            return _read_text_if_exists(final_bundle["paths"][kind])
        return self.store.read_artifact_text(job_id, kind)

    async def resume(self, job_id: str, *, schedule: bool = True) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        if job.plan_only:
            self.store.append_event(
                job_id,
                type="plan_only_execution_blocked",
                phase=job.phase,
                message="Plan-only jobs cannot be resumed into execution.",
                data={},
            )
            return self._job_payload(job, reused=False)
        if job.status not in {"draft", "failed", "interrupted"}:
            return self._job_payload(job, reused=False)
        if job.status == "interrupted" and _job_prefers_resolved_final_bundle(job):
            final_bundle = _resolve_final_artifact_bundle(self.store, job_id)
            if _artifact_bundle_is_usable(final_bundle):
                completed_at = job.finished_at or utc_now_iso()
                job = self.store.update_job(
                    job_id,
                    status="completed",
                    phase="finalizing",
                    progress_pct=100.0,
                    finished_at=completed_at,
                    heartbeat_at=utc_now_iso(),
                    last_error="",
                )
                self.store.append_event(
                    job_id,
                    type="job_resolved_from_final_batch",
                    phase="finalizing",
                    message="Deep research recovered a usable final artifact batch without rerunning finalization.",
                    data={"resolved_artifact_batch_id": final_bundle["batch_id"]},
                )
                return self._job_payload(job, reused=False)
        job = self.store.update_job(
            job_id,
            status="queued",
            progress_pct=0.0,
            started_at="",
            finished_at="",
            last_error="",
            cancel_requested=False,
            heartbeat_at=utc_now_iso(),
        )
        self.store.append_event(
            job_id,
            type="job_resumed",
            phase=job.phase,
            message="Deep research job resumed from checkpoint.",
            data={"checkpoint_key": job.current_checkpoint},
        )
        if schedule:
            await self._schedule(job_id)
        return self._job_payload(job, reused=False)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        if job.status in {"completed", "failed", "canceled", "interrupted"}:
            if job.cancel_requested:
                job = self.store.update_job(job_id, cancel_requested=False)
            payload = self._job_payload(job, reused=False)
            payload["cancel_requested"] = False
            return payload
        job = self.store.update_job(job_id, cancel_requested=True)
        self.store.append_event(
            job_id,
            type="cancel_requested",
            phase=job.phase,
            message="Cancel requested.",
            data={},
        )
        if job.status in {"queued", "draft"}:
            job = self.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
            self.store.append_event(
                job_id,
                type="job_canceled",
                phase=job.phase,
                message="Deep research canceled before execution.",
                data={},
            )
        return {
            "job_id": job_id,
            "cancel_requested": True,
            "status": self.store.get_job(job_id).status,
        }

    async def list_jobs(self, *, status: str = "", limit: int = 50) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        jobs = self.store.list_jobs(status=status, limit=limit)
        return {
            "jobs": [self._serialize_job(job) for job in jobs],
        }

    async def run_job(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        if job.plan_only:
            self.store.append_event(
                job_id,
                type="plan_only_execution_blocked",
                phase=job.phase,
                message="Plan-only jobs cannot be executed.",
                data={},
            )
            return await self.result(job_id)
        await self._run(job_id)
        return await self.result(job_id)

    def write_artifact(self, job_id: str, kind: str, content: str, content_type: str) -> dict[str, Any]:
        path = self.store.artifact_abspath(job_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{kind}.{secrets.token_hex(4)}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
        relative_path = str(path.relative_to(self.store.root_dir))
        artifact = self.store.upsert_artifact(
            job_id,
            kind=kind,
            path=relative_path,
            content_type=content_type,
            metadata=_artifact_metadata(content),
        )
        return artifact.model_dump()

    def write_artifact_batch(self, job_id: str, artifacts: list[dict[str, str]]) -> list[dict[str, Any]]:
        batch_id = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "") + "-" + secrets.token_hex(4)
        persisted: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        batch_dir = self.store.artifacts_dir / job_id / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        for item in artifacts:
            kind = item["kind"]
            content = item["content"]
            content_type = item["content_type"]
            path = batch_dir / kind
            temp_path = path.with_name(f".{kind}.{secrets.token_hex(4)}.tmp")
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(path)
            payloads.append(
                {
                    "kind": kind,
                    "path": str(path.relative_to(self.store.root_dir)),
                    "content_type": content_type,
                    "metadata": _artifact_metadata(content, batch_id=batch_id),
                }
            )
        persisted_artifacts = self.store.upsert_artifact_batch(job_id, artifacts=payloads)
        for artifact in persisted_artifacts:
            persisted.append(artifact.model_dump())
        return persisted

    async def _ensure_startup_reconciled(self) -> None:
        if self._startup_reconciled:
            return
        self.store.reconcile_incomplete_jobs(stale_after_seconds=_RUNTIME_RECONCILE_STALE_SECONDS)
        self._startup_reconciled = True

    async def _schedule(self, job_id: str) -> None:
        async with self._task_lock:
            existing = self._tasks.get(job_id)
            if existing and not existing.done():
                return
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))

    async def _run(self, job_id: str) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.cancel_requested:
                _mark_canceled(self, job_id, job.phase, data={"reason": "cancel_requested_before_start"})
                return
            await self._runner(self, job_id)
        except Exception as exc:
            current_job = self.store.get_job(job_id)
            if current_job.cancel_requested:
                _mark_canceled(self, job_id, current_job.phase, data={"error": str(exc), "reason": "cancel_requested"})
                return
            self.store.update_job(
                job_id,
                status="failed",
                last_error=str(exc),
                finished_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
            )
            self.store.append_event(
                job_id,
                type="job_failed",
                phase=self.store.get_job(job_id).phase,
                message="Deep research failed.",
                data={"error": str(exc)},
            )
        finally:
            async with self._task_lock:
                self._tasks.pop(job_id, None)

    async def _build_plan(self, job: DeepResearchJob, continuation: DeepResearchContinuationState) -> DeepResearchPlan:
        try:
            generated = await self._generate_plan_with_model(job, continuation)
            planner_trace: dict[str, Any] = {}
            raw_plan = generated
            if isinstance(generated, tuple) and len(generated) == 2:
                raw_plan, planner_trace = generated
            try:
                return self._normalize_plan_payload(job, raw_plan, continuation, planner_trace=planner_trace)
            except Exception as exc:
                if isinstance(exc, PlannerGenerationError) and exc.stage == "unsafe_plan":
                    raise
                planner_trace["repair_attempted"] = True
                planner_trace["repair_stage"] = "normalize"
                try:
                    repaired_plan = await self._repair_plan_after_normalize_failure(
                        job,
                        continuation,
                        raw_plan,
                        planner_trace,
                        exc,
                    )
                    normalized = self._normalize_plan_payload(
                        job,
                        repaired_plan,
                        continuation,
                        planner_trace=planner_trace,
                    )
                    normalized.planner_metadata.setdefault("trace", {}).update(
                        {
                            "repair_attempted": True,
                            "repair_succeeded": True,
                            "repair_stage": "normalize",
                        }
                    )
                    return normalized
                except Exception as repair_exc:
                    planner_trace["repair_succeeded"] = False
                    planner_trace["repair_error"] = _trim_text(str(repair_exc), limit=200)
                    raise PlannerGenerationError("normalize", str(exc), trace=planner_trace) from exc
        except Exception as exc:
            stage = exc.stage if isinstance(exc, PlannerGenerationError) else "generation"
            return self._build_fallback_plan(
                job,
                continuation,
                fallback_reason={"stage": stage, "error": _trim_text(str(exc), limit=280)},
                planner_trace=getattr(exc, "trace", None),
            )

    async def _generate_plan_with_model(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuationState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        provider, _ = await _build_runtime_grok_provider(config.grok_model)
        provider_meta = _
        planner_trace: dict[str, Any] = {
            "requested_model": provider_meta.get("requested_model"),
            "effective_model": provider_meta.get("effective_model"),
            "model_resolution": provider_meta.get("resolution"),
            "available_model_count": len(provider_meta.get("available_models") or []),
            "repair_attempted": False,
            "repair_succeeded": False,
            "normalize_actions": [],
            "validation_issues": [],
            "blocked_reasons": [],
            "fallback_used": False,
            "final_status": "generated",
        }
        planner_prompt = (
            "You are planning a deep research job.\n"
            "Return valid JSON only with keys: brief, sub_questions, search_strategy, report_outline, research_units, planner_metadata.\n"
            "Keep the plan lightweight and execution-ready.\n"
            "research_units must be an array of objects with unit_id, unit_type, title, goal, query/url/instructions, depends_on, status, notes.\n"
            "Prefer search units, and only include fetch/map units when clearly justified.\n"
            "Keep report_outline concise and aligned with the research goal.\n"
            "When continuation.mode=continue, treat ambiguous terms like resume/continue as technical workflow concepts unless the prior job is explicitly career-related.\n"
            "Reuse the previous job's evidence and unfinished angles when refining search queries and outline.\n"
        )
        user_prompt = _json_markdown_block(
            {
                "query": job.query,
                "context": job.context,
                "effort": job.effort,
                "time_budget_seconds": job.resolved_budget_seconds,
                "include_domains": job.include_domains,
                "exclude_domains": job.exclude_domains,
                "continuation": _planner_continuation_payload(continuation),
            }
        )
        headers = provider._build_api_headers()
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": planner_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        content, _ = await provider._execute_completion_with_retry_result(headers, payload, render_sources=False)
        planner_trace["provider_name"] = provider._last_success_provider_name
        planner_trace["provider_model"] = provider._last_success_provider_model
        planner_trace["provider_api_url"] = provider._last_success_provider_api_url
        try:
            raw_plan, parse_trace = _parse_json_object_with_trace(content)
            planner_trace.update(parse_trace)
            return raw_plan, planner_trace
        except Exception as exc:
            planner_trace["repair_attempted"] = True
            planner_trace["initial_parse_error"] = _trim_text(str(exc), limit=200)
            repair_payload = {
                "model": provider.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Repair the following into valid JSON matching the requested schema. Return JSON only.",
                    },
                    {"role": "user", "content": content},
                ],
                "stream": False,
            }
            repaired, _ = await provider._execute_completion_with_retry_result(
                headers,
                repair_payload,
                render_sources=False,
            )
            try:
                raw_plan, parse_trace = _parse_json_object_with_trace(repaired)
                planner_trace["repair_succeeded"] = True
                planner_trace["repair_provider_name"] = provider._last_success_provider_name
                planner_trace["repair_provider_model"] = provider._last_success_provider_model
                planner_trace["repair_provider_api_url"] = provider._last_success_provider_api_url
                planner_trace.update({f"repair_{key}": value for key, value in parse_trace.items()})
                return raw_plan, planner_trace
            except Exception as repair_exc:
                planner_trace["repair_error"] = _trim_text(str(repair_exc), limit=200)
                planner_trace["final_status"] = "repair_failed"
                raise PlannerGenerationError("repair", str(repair_exc), trace=planner_trace) from exc

    async def _repair_plan_after_normalize_failure(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuationState,
        raw_plan: dict[str, Any],
        planner_trace: dict[str, Any],
        error: Exception,
    ) -> dict[str, Any]:
        provider, provider_meta = await _build_runtime_grok_provider(config.grok_model)
        planner_trace["repair_requested_model"] = provider_meta.get("requested_model")
        planner_trace["repair_effective_model"] = provider_meta.get("effective_model")
        planner_trace["repair_model_resolution"] = provider_meta.get("resolution")
        headers = provider._build_api_headers()
        payload = {
            "model": provider.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Repair the deep research plan into valid JSON only. "
                        "Use keys: brief, sub_questions, search_strategy, report_outline, research_units, planner_metadata. "
                        "Ensure brief.success_criteria is a list of strings; research_units only use unit_type search/fetch/map; "
                        "unknown statuses become pending; each sub_question has coverage; dependencies must be acyclic and only point backward; "
                        "report_outline must be concise and aligned with the query."
                    ),
                },
                {
                    "role": "user",
                    "content": _json_markdown_block(
                        {
                            "query": job.query,
                            "context": job.context,
                            "continuation": _planner_continuation_payload(continuation),
                            "validation_error": _trim_text(str(error), limit=400),
                            "raw_plan": raw_plan,
                        }
                    ),
                },
            ],
            "stream": False,
        }
        repaired, _ = await provider._execute_completion_with_retry_result(headers, payload, render_sources=False)
        repaired_plan, parse_trace = _parse_json_object_with_trace(repaired)
        planner_trace["repair_succeeded"] = True
        planner_trace["repair_provider_name"] = provider._last_success_provider_name
        planner_trace["repair_provider_model"] = provider._last_success_provider_model
        planner_trace["repair_provider_api_url"] = provider._last_success_provider_api_url
        planner_trace.update({f"repair_{key}": value for key, value in parse_trace.items()})
        return repaired_plan

    def _build_fallback_plan(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuationState,
        *,
        fallback_reason: dict[str, Any] | None = None,
        planner_trace: dict[str, Any] | None = None,
    ) -> DeepResearchPlan:
        query = _rewrite_research_query(job.query.strip(), continuation)
        search_queries = [query]
        if continuation.mode == "continue":
            for candidate in (
                continuation.continuation_goal,
                *continuation.open_questions,
            ):
                normalized = _normalize_whitespace(candidate)
                if normalized:
                    search_queries.append(_rewrite_research_query(_trim_text(normalized, limit=220), continuation))
        if job.effort == "deep":
            search_queries.append(f"{query} tradeoffs")
        unique_queries: list[str] = []
        seen: set[str] = set()
        for item in search_queries:
            normalized = _normalize_whitespace(item)
            if normalized and normalized not in seen:
                unique_queries.append(normalized)
                seen.add(normalized)

        raw_plan = {
            "brief": {
                "objective": query,
                "deliverable": "A structured deep research report with citations.",
                "success_criteria": [
                    "Answer the main research question.",
                    "Ground each section in verifiable sources.",
                ],
                "must_cover": unique_queries[:3] or [query],
                "out_of_scope": [],
                "preferred_sources": list(job.include_domains),
                "stop_policy": {
                    "stop_on_sufficient_coverage": True,
                    "max_search_queries": max(1, min(len(unique_queries), config.deep_research_max_concurrency)),
                    "max_urls_per_search": 1 if job.effort != "deep" else 2,
                },
                "continuation_focus": _dedupe_preserve_order(
                    [
                        item
                        for item in (
                            continuation.continuation_goal,
                            continuation.previous_summary,
                        )
                        if _normalize_whitespace(item)
                    ]
                ),
            },
            "sub_questions": [
                {"id": f"sq{index}", "question": item, "reason": "Cover the core research surface."}
                for index, item in enumerate(unique_queries[:3], start=1)
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": unique_queries[: max(1, config.deep_research_max_concurrency)],
                "selective_fetch": {
                    "max_urls_per_search": 1 if job.effort != "deep" else 2,
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
            ],
            "research_units": [
                {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": f"Research query {index}",
                    "goal": f"Investigate: {item}",
                    "query": item,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
                for index, item in enumerate(unique_queries[: max(1, config.deep_research_max_concurrency)], start=1)
            ],
            "planner_metadata": {
                "planner": "fallback",
                "used_fallback": True,
                "fallback_reason": fallback_reason or {"stage": "generation", "error": "unknown_planner_failure"},
            },
        }
        return self._normalize_plan_payload(job, raw_plan, continuation, planner_trace=planner_trace)

    def _normalize_plan_payload(
        self,
        job: DeepResearchJob,
        raw_plan: dict[str, Any],
        continuation: DeepResearchContinuationState,
        *,
        planner_trace: dict[str, Any] | None = None,
    ) -> DeepResearchPlan:
        planner_trace = {
            "requested_model": "",
            "effective_model": "",
            "model_resolution": None,
            "available_model_count": 0,
            "repair_attempted": False,
            "repair_succeeded": False,
            "normalize_actions": [],
            "validation_issues": [],
            "blocked_reasons": [],
            "fallback_used": False,
            "final_status": "normalized",
            **dict(planner_trace or {}),
        }
        normalize_actions: list[str] = list(planner_trace.get("normalize_actions") or [])
        if raw_plan.get("search_queries") and not raw_plan.get("search_strategy"):
            normalize_actions.append("top_level_search_queries_to_strategy")
            raw_plan = {
                **raw_plan,
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": raw_plan.get("search_queries") or [],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
            }
        if raw_plan.get("report_sections") and not raw_plan.get("report_outline"):
            normalize_actions.append("report_sections_to_report_outline")
            raw_plan = {
                **raw_plan,
                "report_outline": [
                    {"section_id": _slugify(title), "title": title, "goal": title}
                    for title in raw_plan.get("report_sections") or []
                ],
            }
        validation_issues: list[str] = list(planner_trace.get("validation_issues") or [])
        normalized_brief = _normalize_brief_payload(
            raw_plan.get("brief"),
            job=job,
            continuation=continuation,
            normalize_actions=normalize_actions,
        )
        sub_questions = raw_plan.get("sub_questions") or []
        if isinstance(sub_questions, dict):
            normalize_actions.append("dict_sub_questions_wrapped")
            sub_questions = [sub_questions]
        elif isinstance(sub_questions, str):
            normalize_actions.append("string_sub_questions_wrapped")
            sub_questions = [sub_questions]
        if not sub_questions:
            sub_questions = [{"id": "sq1", "question": _rewrite_research_query(job.query, continuation), "reason": "Cover the primary question."}]
        else:
            normalized_sub_questions: list[dict[str, Any]] = []
            saw_string_sub_questions = False
            for index, item in enumerate(sub_questions, start=1):
                if isinstance(item, str):
                    saw_string_sub_questions = True
                    normalized_sub_questions.append(
                        {
                            "id": f"sq{index}",
                            "question": _rewrite_research_query(item, continuation),
                            "reason": "Cover the primary question.",
                        }
                    )
                    continue
                if not isinstance(item, dict):
                    validation_issues.append("invalid_sub_question_items")
                    continue
                normalized_sub_questions.append(
                    {
                        **item,
                        "question": _rewrite_research_query(str(item.get("question", "")), continuation),
                    }
                )
            if saw_string_sub_questions:
                validation_issues.append("string_sub_question_items")
            sub_questions = normalized_sub_questions
        sub_questions, deduped_sub_questions = _dedupe_sub_questions(sub_questions)
        if deduped_sub_questions:
            validation_issues.append("duplicate_sub_questions")

        report_outline = raw_plan.get("report_outline") or [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Present the main evidence."},
        ]
        saw_string_report_outline = False
        if isinstance(report_outline, dict):
            normalize_actions.append("dict_report_outline_wrapped")
            report_outline = [report_outline]
        elif isinstance(report_outline, str):
            normalize_actions.append("string_report_outline_wrapped")
            report_outline = [report_outline]
            saw_string_report_outline = True
        elif isinstance(report_outline, list):
            saw_string_report_outline = any(isinstance(item, str) for item in report_outline)
        if saw_string_report_outline:
            validation_issues.append("string_report_outline_items")
        if continuation.mode == "continue" and _is_generic_outline(report_outline):
            report_outline = [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the follow-up answer."},
                {"section_id": "follow-up-findings", "title": "Follow-up Findings", "goal": "Extend or revise prior findings with new evidence."},
                {"section_id": "remaining-gaps", "title": "Remaining Gaps", "goal": "Call out what still needs confirmation."},
            ]
            validation_issues.append("generic_continuation_outline")
        research_units = raw_plan.get("research_units") or []
        if isinstance(research_units, dict):
            normalize_actions.append("dict_research_units_wrapped")
            research_units = [research_units]
        elif isinstance(research_units, str):
            normalize_actions.append("string_research_units_wrapped")
            research_units = [research_units]
        if not research_units:
            search_queries = (raw_plan.get("search_strategy") or {}).get("search_queries") or [job.query]
            research_units = [
                {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": f"Search {index}",
                    "goal": f"Investigate {query}",
                    "query": query,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
                for index, query in enumerate(search_queries[: max(1, config.deep_research_max_concurrency)], start=1)
            ]

        normalized_units: list[dict[str, Any]] = []
        saw_string_research_units = False
        for index, item in enumerate(research_units, start=1):
            if isinstance(item, str):
                saw_string_research_units = True
                item = {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": f"Research query {index}",
                    "goal": f"Investigate: {item}",
                    "query": item,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
            if not isinstance(item, dict):
                validation_issues.append("invalid_research_unit_items")
                continue
            unit_id = item.get("unit_id") or f"unit-{item.get('unit_type', 'search')}-{index}"
            unit_query = item.get("query", "")
            unit_type = str(item.get("unit_type", "search") or "search").strip()
            unit_type_aliases = {
                "browse": "fetch",
                "browse_page": "fetch",
                "web_fetch": "fetch",
                "web_map": "map",
                "search_web": "search",
            }
            normalized_unit_type = unit_type_aliases.get(unit_type, unit_type)
            if normalized_unit_type != unit_type:
                normalize_actions.append(f"aliased_unit_type:{unit_type}->{normalized_unit_type}")
            if normalized_unit_type not in {"search", "fetch", "map"}:
                inferred_unit_type = "fetch" if _normalize_whitespace(str(item.get("url", ""))) else "search"
                normalize_actions.append(f"inferred_unknown_unit_type:{unit_type}->{inferred_unit_type}")
                normalized_unit_type = inferred_unit_type
            raw_status = str(item.get("status", "pending") or "pending").strip()
            status_aliases = {
                "ready": "pending",
            }
            normalized_status = status_aliases.get(raw_status, raw_status)
            if normalized_status != raw_status:
                normalize_actions.append(f"aliased_unit_status:{raw_status}->{normalized_status}")
            if normalized_status not in {"pending", "running", "completed", "failed", "skipped"}:
                normalize_actions.append(f"defaulted_unknown_unit_status:{raw_status}->pending")
                normalized_status = "pending"
            if normalized_unit_type == "search":
                unit_query = _rewrite_research_query(str(unit_query), continuation)
            normalized_units.append(
                {
                    "unit_id": unit_id,
                    "unit_type": normalized_unit_type,
                    "title": item.get("title") or f"Unit {index}",
                    "goal": item.get("goal") or unit_query or item.get("url") or f"Unit {index}",
                    "query": unit_query,
                    "url": item.get("url", ""),
                    "instructions": item.get("instructions", ""),
                    "depends_on": _normalize_depends_on(item.get("depends_on")),
                    "status": normalized_status,
                    "notes": item.get("notes", ""),
                }
            )
        if saw_string_research_units:
            validation_issues.append("string_research_unit_items")
        deduped_units: list[dict[str, Any]] = []
        seen_unit_keys: set[tuple[str, str, str, str]] = set()
        for index, unit in enumerate(normalized_units, start=1):
            unit_key = (
                unit["unit_type"],
                _normalize_whitespace(unit.get("query", "")).lower(),
                _normalize_whitespace(unit.get("url", "")).lower(),
                _normalize_whitespace(unit.get("instructions", "")).lower(),
            )
            if unit_key in seen_unit_keys:
                validation_issues.append("duplicate_research_units")
                continue
            seen_unit_keys.add(unit_key)
            if continuation.mode == "continue" and re.match(r"^(search|research query)\b", unit["title"].lower()):
                unit["title"] = f"Follow-up {unit['unit_type']} {index}"
            deduped_units.append(unit)
        normalized_units = _repair_research_units(
            deduped_units or normalized_units,
            sub_questions=sub_questions,
            job_query=job.query,
            continuation=continuation,
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
            blocked_reasons=planner_trace["blocked_reasons"],
        )

        normalized_outline: list[dict[str, Any]] = []
        seen_outline_ids: dict[str, int] = {}
        for index, item in enumerate(report_outline, start=1):
            if isinstance(item, str):
                item = {"section_id": _slugify(item), "title": item, "goal": item}
            if not isinstance(item, dict):
                validation_issues.append("invalid_report_outline_items")
                continue
            title = item.get("title") or f"Section {index}"
            base_section_id = _normalize_whitespace(
                str(item.get("section_id") or _slugify(title) or f"section-{index}")
            )
            section_id = base_section_id
            count = seen_outline_ids.get(base_section_id, 0)
            if count > 0:
                renamed_section_id = f"{base_section_id}-{count + 1}"
                _append_unique(validation_issues, "duplicate_report_outline_section_id")
                _append_unique(
                    normalize_actions,
                    f"renamed_duplicate_report_outline_section_id:{base_section_id}->{renamed_section_id}",
                )
                section_id = renamed_section_id
            seen_outline_ids[base_section_id] = count + 1
            normalized_outline.append(
                {
                    "section_id": section_id,
                    "title": title,
                    "goal": item.get("goal") or title,
                }
            )
        normalized_units = _ensure_sub_question_unit_coverage(
            normalized_units,
            sub_questions=sub_questions,
            continuation=continuation,
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
        )
        normalized_outline = _outline_from_sub_questions(
            normalized_outline,
            sub_questions=sub_questions,
            continuation=continuation,
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
        )

        strategy = raw_plan.get("search_strategy") or {}
        if not isinstance(strategy, dict):
            normalize_actions.append("non_dict_search_strategy")
            strategy = {"search_queries": _normalize_string_list(strategy)}
        strategy["approach"] = _normalize_search_strategy_approach(
            strategy.get("approach"),
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
        )
        search_queries = strategy.get("search_queries")
        if isinstance(search_queries, str):
            normalize_actions.append("string_search_queries")
            strategy["search_queries"] = _normalize_string_list(search_queries)
        elif isinstance(search_queries, dict):
            normalize_actions.append("dict_search_queries")
            strategy["search_queries"] = _normalize_string_list(search_queries.values())
        elif search_queries is not None and not isinstance(search_queries, list):
            normalize_actions.append("non_list_search_queries")
            strategy["search_queries"] = _normalize_string_list(search_queries)
        strategy["selective_fetch"] = _normalize_selective_fetch_config(
            strategy.get("selective_fetch"),
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
        )
        if not strategy.get("search_queries"):
            strategy["search_queries"] = [
                unit["query"] for unit in normalized_units if unit["unit_type"] == "search" and unit["query"]
            ] or [job.query]
        strategy["search_queries"] = _dedupe_preserve_order(
            [
                _rewrite_research_query(str(query), continuation)
                for query in [
                    *(strategy.get("search_queries") or [job.query]),
                    *[unit["query"] for unit in normalized_units if unit["unit_type"] == "search" and unit["query"]],
                ]
            ]
        )
        normalized_brief = _finalize_brief_payload(
            normalized_brief,
            job=job,
            continuation=continuation,
            sub_questions=sub_questions,
            strategy=strategy,
        )

        raw_planner_metadata = raw_plan.get("planner_metadata")
        if not isinstance(raw_planner_metadata, dict):
            normalize_actions.append("non_dict_planner_metadata")
            raw_planner_metadata = {"planner": "model", "used_fallback": False}
        planner_metadata = dict(raw_planner_metadata)
        if validation_issues:
            planner_metadata["validation"] = {
                "issues": _dedupe_preserve_order(validation_issues),
                "repaired": True,
            }
        planner_trace["normalize_actions"] = _dedupe_preserve_order(normalize_actions)
        planner_trace["validation_issues"] = _dedupe_preserve_order(validation_issues)
        planner_trace["blocked_reasons"] = _dedupe_preserve_order(list(planner_trace.get("blocked_reasons") or []))
        planner_trace["fallback_used"] = bool(planner_metadata.get("used_fallback"))
        planner_trace["fallback_reason"] = planner_metadata.get("fallback_reason")
        unsafe_plan_reason = _unsafe_plan_reason(
            planner=str(planner_metadata.get("planner", "") or ""),
            continuation=continuation,
            normalize_actions=planner_trace["normalize_actions"],
            validation_issues=planner_trace["validation_issues"],
            blocked_reasons=planner_trace["blocked_reasons"],
        )
        planner_trace["unsafe_plan"] = bool(planner_trace.get("unsafe_plan")) or unsafe_plan_reason is not None
        if unsafe_plan_reason is not None:
            planner_trace["unsafe_plan_reason"] = unsafe_plan_reason
            planner_trace["final_status"] = "unsafe_plan"
            raise PlannerGenerationError(
                "unsafe_plan",
                f"{unsafe_plan_reason['reason']}: {unsafe_plan_reason['issue']}",
                trace=planner_trace,
            )
        if planner_metadata.get("used_fallback"):
            planner_trace["final_status"] = str(planner_trace.get("final_status") or "fallback")
        else:
            planner_trace["final_status"] = "normalized"
        planner_metadata["trace"] = planner_trace

        normalized = {
            "query": job.query,
            "context": job.context,
            "effort": job.effort,
            "time_budget_seconds": job.resolved_budget_seconds,
            "include_domains": job.include_domains,
            "exclude_domains": job.exclude_domains,
            "brief": normalized_brief,
            "sub_questions": sub_questions,
            "search_strategy": strategy,
            "report_outline": normalized_outline,
            "research_units": normalized_units,
            "continuation": _compact_continuation(continuation).model_dump(),
            "planner_metadata": planner_metadata,
        }
        plan = DeepResearchPlan.model_validate(normalized)
        _validate_research_units(plan.research_units)
        return plan

    def _build_continuation_context(self, continue_from_job_id: str) -> DeepResearchContinuationState:
        if not continue_from_job_id:
            return DeepResearchContinuationState(mode="fresh")
        job = self.store.get_job(continue_from_job_id)

        final_bundle = _resolve_final_artifact_bundle(self.store, continue_from_job_id)
        use_final_bundle = _artifact_bundle_is_usable(final_bundle)
        current_report_text = self.store.read_artifact_text(continue_from_job_id, "report.json") or ""
        current_final_report = self.store.read_artifact_text(continue_from_job_id, "final_report.md") or ""
        current_sources_text = self.store.read_artifact_text(continue_from_job_id, "sources.json") or "[]"
        current_citations_text = self.store.read_artifact_text(continue_from_job_id, "citations.json") or ""
        plan_text = self.store.read_artifact_text(continue_from_job_id, "plan.json") or ""
        checkpoints = self.store.list_checkpoints(continue_from_job_id)
        checkpoint_state, checkpoint_meta = self._load_checkpoint_state(job)
        latest_state = checkpoints[-1].state or {} if checkpoints else {}
        report_text = (
            _read_text_if_exists(final_bundle["paths"]["report.json"])
            if use_final_bundle
            else current_report_text
        )
        final_report = (
            _read_text_if_exists(final_bundle["paths"]["final_report.md"])
            if use_final_bundle
            else current_final_report
        )
        partial_report = self.store.read_artifact_text(continue_from_job_id, "partial_report.md") or ""
        sources_text = (
            _read_text_if_exists(final_bundle["paths"]["sources.json"])
            if use_final_bundle
            else current_sources_text
        ) or "[]"
        citations_text = (
            _read_text_if_exists(final_bundle["paths"]["citations.json"])
            if use_final_bundle
            else current_citations_text
        ) or ""
        report: dict[str, Any] = {}
        if report_text:
            report_value, _ = _safe_load_json_artifact(report_text)
            if _validate_json_artifact_shape("report.json", report_value) is None and isinstance(report_value, dict):
                report = report_value

        prior_plan_summary = ""
        plan_payload: dict[str, Any] = {}
        if plan_text:
            try:
                parsed_plan = json.loads(plan_text)
                if isinstance(parsed_plan, dict):
                    plan_payload = parsed_plan
                sub_questions = plan_payload.get("sub_questions") or []
                prior_plan_summary = "; ".join(
                    item.get("question", "").strip() for item in sub_questions if item.get("question", "").strip()
                )
            except Exception:
                prior_plan_summary = ""
        if not prior_plan_summary and checkpoints:
            latest_state = checkpoints[-1].state or {}
            plan_state = latest_state.get("plan") if isinstance(latest_state, dict) else None
            if isinstance(plan_state, dict):
                prior_plan_summary = "; ".join(
                    item.get("question", "").strip()
                    for item in plan_state.get("sub_questions", []) or []
                    if item.get("question", "").strip()
                )

        carry_forward_sources: list[dict[str, Any]] = []
        if sources_text:
            sources_value, _ = _safe_load_json_artifact(sources_text)
            if _validate_json_artifact_shape("sources.json", sources_value) is None and isinstance(sources_value, list):
                carry_forward_sources = list(sources_value)
        if not carry_forward_sources and citations_text:
            citations_value, _ = _safe_load_json_artifact(citations_text)
            normalized_citations = _normalize_citations_payload(citations_value)
            if (
                _validate_json_artifact_shape("citations.json", normalized_citations) is None
                and normalized_citations
                and isinstance(normalized_citations.get("source_registry"), dict)
            ):
                carry_forward_sources = list(normalized_citations["source_registry"].values())

        if not carry_forward_sources and not use_final_bundle and checkpoint_state:
            carry_forward_sources = list(checkpoint_state.sources)
        elif not carry_forward_sources and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_sources = list(latest_state.get("sources") or [])
        source_count = len(carry_forward_sources)

        carry_forward_sections = list(report.get("sections") or []) if isinstance(report.get("sections"), list) else []
        if not carry_forward_sections and not use_final_bundle and checkpoint_state:
            carry_forward_sections = list(checkpoint_state.sections)
        elif not carry_forward_sections and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_sections = list(latest_state.get("sections") or [])

        report_unit_results = report.get("unit_results") if isinstance(report.get("unit_results"), dict) else {}
        carry_forward_unit_results = dict(report_unit_results or {})
        if not carry_forward_unit_results and not use_final_bundle and checkpoint_state:
            carry_forward_unit_results = dict(checkpoint_state.unit_results)
        elif not carry_forward_unit_results and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_unit_results = dict(latest_state.get("unit_results") or {})

        carry_forward_evidence = []
        if not use_final_bundle and checkpoint_state:
            carry_forward_evidence = list(checkpoint_state.evidence_items)
        elif not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_evidence = list(latest_state.get("evidence_items") or [])
        if not carry_forward_evidence:
            carry_forward_evidence = _build_carry_forward_evidence(carry_forward_unit_results, carry_forward_sections)
        carry_forward_unit_results = _sanitize_unit_results(carry_forward_unit_results, carry_forward_sources)
        carry_forward_sections = _sanitize_continuation_sections(carry_forward_sections, carry_forward_sources)
        carry_forward_evidence = _sanitize_evidence_items(carry_forward_evidence, carry_forward_sources)
        used_source_ids = _continuation_used_source_ids(
            carry_forward_unit_results,
            carry_forward_evidence,
            carry_forward_sections,
        )
        carry_forward_sources = _focused_continuation_sources(
            carry_forward_sources,
            used_source_ids=used_source_ids,
        )
        carry_forward_unit_results = _sanitize_unit_results(carry_forward_unit_results, carry_forward_sources)
        carry_forward_sections = _sanitize_continuation_sections(carry_forward_sections, carry_forward_sources)
        carry_forward_evidence = _sanitize_evidence_items(carry_forward_evidence, carry_forward_sources)
        previous_summary = _best_continuation_summary(
            report=report,
            final_report=final_report,
            partial_report=partial_report,
            unit_results=carry_forward_unit_results,
            sections=carry_forward_sections,
            prior_plan_summary=prior_plan_summary,
            job=job,
        )

        checkpoint_key = (
            checkpoint_meta.get("fallback_to", "") if checkpoint_meta else ""
        ) or job.current_checkpoint or (checkpoints[-1].checkpoint_key if checkpoints else "")
        continuation_goal = plan_payload.get("query") or job.query
        source_count = len(carry_forward_sources)
        carry_forward_constraints = _carry_forward_constraints(job=job, plan_payload=plan_payload)
        continuation_identity = _continuation_identity_for_source_job(
            self.store,
            job,
            plan_payload=plan_payload,
        )

        return DeepResearchContinuationState(
            mode="continue",
            source_job_id=continue_from_job_id,
            source_job_status=job.status,
            continuation_identity=continuation_identity,
            previous_summary=_trim_text(previous_summary, limit=400),
            prior_plan_summary=_trim_text(prior_plan_summary, limit=400),
            continuation_goal=_trim_text(continuation_goal, limit=200),
            source_count=source_count,
            checkpoint_key=checkpoint_key,
            state_version=2,
            confirmed_claims=_collect_confirmed_claims(carry_forward_sections),
            open_questions=_collect_continuation_open_questions(report),
            trusted_source_headers=_trusted_source_headers(carry_forward_sources),
            carry_forward_constraints=carry_forward_constraints,
            carry_forward_sources=carry_forward_sources,
            carry_forward_evidence=carry_forward_evidence,
            carry_forward_sections=carry_forward_sections,
            carry_forward_unit_results=carry_forward_unit_results,
        )

    def _request_fingerprint(
        self,
        *,
        query: str,
        context: str,
        effort: str,
        include_domains: list[str],
        exclude_domains: list[str],
        continue_from_job_id: str,
        plan_only: bool,
        continuation_identity: str = "",
    ) -> str:
        payload = {
            "query": query.strip(),
            "context": context.strip(),
            "effort": effort.strip(),
            "include_domains": include_domains,
            "exclude_domains": exclude_domains,
            "continue_from_job_id": continue_from_job_id.strip(),
            "plan_only": bool(plan_only),
            "continuation_identity": continuation_identity.strip(),
        }
        return hashlib.sha256(_json_markdown_block(payload).encode("utf-8")).hexdigest()

    def _resolve_budget_seconds(self, requested_budget_seconds: int | None, effort: str) -> int:
        if requested_budget_seconds and requested_budget_seconds > 0:
            return min(requested_budget_seconds, config.deep_research_hard_timeout_seconds)
        if effort == "deep":
            candidate = int(config.deep_research_default_budget_seconds * 1.5)
        else:
            candidate = config.deep_research_default_budget_seconds
        return min(candidate, config.deep_research_hard_timeout_seconds)

    def _job_payload(self, job: DeepResearchJob, *, reused: bool) -> dict[str, Any]:
        payload = self._serialize_job(job)
        final_bundle = _resolve_final_artifact_bundle(self.store, job.job_id) if _job_prefers_resolved_final_bundle(job) else None
        payload["reused"] = reused
        plan_text = self.store.read_artifact_text(job.job_id, "plan.json")
        plan_value, _ = _safe_load_json_artifact(plan_text)
        payload["plan"] = plan_value
        payload["artifact_fallback_used"] = _artifact_bundle_differs_from_current(self.store, job.job_id, final_bundle)
        payload["resolved_artifact_batch_id"] = final_bundle["batch_id"] if final_bundle is not None else ""
        return payload

    def _serialize_job(self, job: DeepResearchJob) -> dict[str, Any]:
        return job.model_dump()

    def _continuation_from_planning_checkpoint(self, job: DeepResearchJob) -> DeepResearchContinuationState | None:
        checkpoints = self.store.list_checkpoints(job.job_id)
        planning_checkpoint = next((item for item in reversed(checkpoints) if item.checkpoint_key == "planning"), None)
        if planning_checkpoint is None:
            return None
        raw_state = planning_checkpoint.state or {}
        if not isinstance(raw_state, dict):
            return None
        plan_payload = raw_state.get("plan") if isinstance(raw_state.get("plan"), dict) else {}
        continuation_payload = plan_payload.get("continuation") if isinstance(plan_payload.get("continuation"), dict) else {}
        if not continuation_payload.get("source_job_id"):
            return None
        base = DeepResearchContinuationState.model_validate(
            {
                **continuation_payload,
                "carry_forward_sources": raw_state.get("sources") or [],
                "carry_forward_evidence": raw_state.get("evidence_items") or [],
                "carry_forward_sections": raw_state.get("sections") or [],
                "carry_forward_unit_results": raw_state.get("unit_results") or {},
            }
        )
        return base

    def _read_runtime_continuation(self, job: DeepResearchJob) -> DeepResearchContinuationState:
        if not job.continued_from_job_id:
            return DeepResearchContinuationState(mode="fresh")
        continuation_text = self.store.read_artifact_text(job.job_id, "continuation.json")
        if continuation_text:
            try:
                return DeepResearchContinuationState.model_validate(json.loads(continuation_text))
            except Exception:
                pass
        checkpoint_continuation = self._continuation_from_planning_checkpoint(job)
        if checkpoint_continuation is not None:
            return checkpoint_continuation
        return self._build_continuation_context(job.continued_from_job_id)

    def _read_plan(self, job_id: str, job: DeepResearchJob | None = None) -> DeepResearchPlan:
        plan_text = self.store.read_artifact_text(job_id, "plan.json")
        if plan_text:
            raw_plan = json.loads(plan_text)
            if job is None:
                job = self.store.get_job(job_id)
            continuation = self._read_runtime_continuation(job)
            plan = self._normalize_plan_payload(job, raw_plan, continuation)
            if raw_plan != plan.model_dump():
                self.write_artifact(job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
            return plan
        if job is None:
            job = self.store.get_job(job_id)
        continuation = self._read_runtime_continuation(job)
        return self._build_fallback_plan(job, continuation)

    def _load_checkpoint_state(self, job: DeepResearchJob) -> tuple[DeepResearchCheckpointState | None, dict[str, Any] | None]:
        checkpoints = self.store.list_checkpoints(job.job_id)
        if not checkpoints:
            return None, None
        candidates: list[Any] = []
        invalid_checkpoint_keys: list[str] = []
        if job.current_checkpoint:
            candidates.extend(
                checkpoint
                for checkpoint in checkpoints
                if checkpoint.checkpoint_key == job.current_checkpoint
            )
        candidates.extend(
            checkpoint
            for checkpoint in reversed(checkpoints)
            if checkpoint.checkpoint_key != job.current_checkpoint
        )
        for checkpoint in candidates:
            raw_state = checkpoint.state or {}
            if "plan" not in raw_state and isinstance(raw_state, dict) and raw_state.get("query"):
                raw_state = {"plan": raw_state}
            if "plan" not in raw_state:
                invalid_checkpoint_keys.append(checkpoint.checkpoint_key)
                continue
            try:
                if checkpoint.phase != "planning" and not any(
                    key in raw_state for key in ("completed_unit_ids", "unit_results", "sources", "evidence_items", "sections")
                ):
                    raise ValueError("checkpoint missing runtime state")
                if isinstance(raw_state.get("plan"), dict):
                    raw_state = {
                        **raw_state,
                        "plan": self._normalize_plan_payload(
                            job,
                            raw_state["plan"],
                            self._build_continuation_context(job.continued_from_job_id),
                        ).model_dump(),
                    }
                state = DeepResearchCheckpointState.model_validate(raw_state)
                if job.current_checkpoint and checkpoint.checkpoint_key != job.current_checkpoint:
                    return (
                        state,
                        {
                            "fallback_from": job.current_checkpoint,
                            "fallback_to": checkpoint.checkpoint_key,
                            "invalid_checkpoint_keys": invalid_checkpoint_keys,
                        },
                    )
                return state, None
            except Exception:
                invalid_checkpoint_keys.append(checkpoint.checkpoint_key)
                continue
        return None, None


async def _default_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    job = runtime.store.get_job(job_id)
    now_iso = utc_now_iso()
    started_at = job.started_at or now_iso
    job = runtime.store.update_job(
        job_id,
        status="running",
        started_at=started_at,
        heartbeat_at=now_iso,
        attempt_count=job.attempt_count + 1,
        last_error="",
    )
    checkpoint_state, checkpoint_meta = runtime._load_checkpoint_state(job)
    plan = checkpoint_state.plan if checkpoint_state else runtime._read_plan(job_id, job)
    runtime.write_artifact(job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
    if checkpoint_meta:
        runtime.store.update_job(job_id, current_checkpoint=checkpoint_meta["fallback_to"])
        runtime.store.append_event(
            job_id,
            type="checkpoint_fallback",
            phase=job.phase,
            message="Latest checkpoint was not readable; resumed from an earlier checkpoint.",
            data=checkpoint_meta,
        )

    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="planning",
        message="Planning started.",
        data={"unit_count": len(plan.research_units)},
    )
    runtime.store.update_job(job_id, phase="planning", progress_pct=10.0, heartbeat_at=utc_now_iso())

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, "planning")
        return

    continuation = runtime._read_runtime_continuation(job)
    completed_unit_ids = list(checkpoint_state.completed_unit_ids) if checkpoint_state else []
    failed_unit_ids: list[str] = []
    failed_units: list[dict[str, Any]] = []
    skipped_unit_ids: list[str] = []
    skipped_units: list[dict[str, Any]] = []
    unit_results = dict(checkpoint_state.unit_results) if checkpoint_state else dict(continuation.carry_forward_unit_results)
    source_registry = list(checkpoint_state.sources) if checkpoint_state else list(continuation.carry_forward_sources)
    source_registry = _apply_domain_constraints(
        source_registry,
        include_domains=plan.include_domains,
        exclude_domains=plan.exclude_domains,
    )
    evidence_items = list(checkpoint_state.evidence_items) if checkpoint_state else list(continuation.carry_forward_evidence)
    sections = list(checkpoint_state.sections) if checkpoint_state else list(continuation.carry_forward_sections)
    unit_results = _sanitize_unit_results(unit_results, source_registry)
    evidence_items = _sanitize_evidence_items(evidence_items, source_registry)
    sections = _sanitize_sections(sections, source_registry)
    started_at_dt = _parse_utc_iso(job.started_at) or dt.datetime.now(dt.UTC)

    runtime.store.update_job(job_id, phase="researching", progress_pct=20.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="researching",
        message="Researching source material.",
        data={"completed_units": len(completed_unit_ids)},
    )

    unit_total = max(len(plan.research_units), 1)
    stop_policy = dict(plan.brief.stop_policy or {})
    max_search_queries = max(1, int(stop_policy.get("max_search_queries", len(plan.search_strategy.search_queries) or unit_total) or 1))
    stop_on_sufficient_coverage = bool(stop_policy.get("stop_on_sufficient_coverage", True))
    while len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids) < len(plan.research_units):
        ready_units = [
            unit
            for unit in plan.research_units
            if unit.unit_id not in completed_unit_ids
            and unit.unit_id not in failed_unit_ids
            and unit.unit_id not in skipped_unit_ids
            and all(dep in completed_unit_ids for dep in unit.depends_on)
        ]
        terminal_or_skipped_unit_ids = set(completed_unit_ids) | set(failed_unit_ids) | set(skipped_unit_ids)
        executed_search_units = sum(
            1
            for unit in plan.research_units
            if unit.unit_type == "search" and unit.unit_id in terminal_or_skipped_unit_ids
        )
        remaining_search_budget = max(0, max_search_queries - executed_search_units)
        if remaining_search_budget == 0:
            budget_ready_units: list[DeepResearchResearchUnit] = []
        else:
            budget_ready_units = []
            consumed_search_budget = 0
            for unit in ready_units:
                if unit.unit_type != "search":
                    budget_ready_units.append(unit)
                    continue
                if consumed_search_budget < remaining_search_budget:
                    budget_ready_units.append(unit)
                    consumed_search_budget += 1
                    continue
                skipped_unit_ids.append(unit.unit_id)
                skipped_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "unit_type": unit.unit_type,
                        "reason": "max_search_queries_reached",
                    }
                )
                runtime.store.append_event(
                    job_id,
                    type="research_unit_skipped",
                    phase="researching",
                    message=f"Skipped {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "reason": "max_search_queries_reached"},
                )
            ready_units = budget_ready_units
        if executed_search_units >= max_search_queries:
            skipped_any = False
            for unit in ready_units:
                if unit.unit_type != "search":
                    continue
                skipped_unit_ids.append(unit.unit_id)
                skipped_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "unit_type": unit.unit_type,
                        "reason": "max_search_queries_reached",
                    }
                )
                runtime.store.append_event(
                    job_id,
                    type="research_unit_skipped",
                    phase="researching",
                    message=f"Skipped {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "reason": "max_search_queries_reached"},
                )
                skipped_any = True
            if skipped_any:
                progress = 20.0 + ((len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids)) / unit_total) * 50.0
                runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())
                continue
        if not ready_units:
            skipped_any = False
            for unit in plan.research_units:
                if (
                    unit.unit_id in completed_unit_ids
                    or unit.unit_id in failed_unit_ids
                    or unit.unit_id in skipped_unit_ids
                ):
                    continue
                if any(dep in failed_unit_ids or dep in skipped_unit_ids for dep in unit.depends_on):
                    skipped_unit_ids.append(unit.unit_id)
                    skipped_units.append(
                        {
                            "unit_id": unit.unit_id,
                            "unit_type": unit.unit_type,
                            "reason": "dependency_failed",
                        }
                    )
                    runtime.store.append_event(
                        job_id,
                        type="research_unit_skipped",
                        phase="researching",
                        message=f"Skipped {unit.unit_id}.",
                        data={"unit_type": unit.unit_type, "reason": "dependency_failed"},
                    )
                    skipped_any = True
            if skipped_any:
                progress = 20.0 + ((len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids)) / unit_total) * 50.0
                runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())
                continue
            remaining_units = [
                unit.unit_id
                for unit in plan.research_units
                if unit.unit_id not in completed_unit_ids
                and unit.unit_id not in failed_unit_ids
                and unit.unit_id not in skipped_unit_ids
            ]
            raise ValueError(f"research_plan_blocked: unresolved dependencies for {', '.join(remaining_units)}")
        if runtime.store.get_job(job_id).cancel_requested:
            _mark_canceled(runtime, job_id, "researching")
            return
        elapsed_seconds = (dt.datetime.now(dt.UTC) - started_at_dt).total_seconds()
        if elapsed_seconds >= job.resolved_budget_seconds:
            _write_partial_outputs(runtime, job_id, plan, completed_unit_ids, unit_results, sections)
            runtime.store.update_job(
                job_id,
                status="interrupted",
                phase="researching",
                last_error="time_budget_exceeded",
                finished_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
            )
            runtime.store.append_event(
                job_id,
                type="job_interrupted",
                phase="researching",
                message="Deep research paused after reaching the time budget.",
                data={"completed_units": len(completed_unit_ids)},
            )
            return

        batch_units = ready_units[: max(1, config.deep_research_max_concurrency)]
        runtime.store.update_job(job_id, heartbeat_at=utc_now_iso())
        batch_results = await asyncio.gather(
            *[_execute_research_unit(runtime, plan, unit) for unit in batch_units],
            return_exceptions=True,
        )
        batch_error: Exception | None = None
        for unit, batch_result in zip(batch_units, batch_results, strict=False):
            if isinstance(batch_result, Exception):
                failed_unit_ids.append(unit.unit_id)
                failed_units.append({"unit_id": unit.unit_id, "unit_type": unit.unit_type, "reason": "execution_error"})
                runtime.store.append_event(
                    job_id,
                    type="research_unit_failed",
                    phase="researching",
                    message=f"Failed {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "error": str(batch_result)},
                )
                if batch_error is None:
                    batch_error = batch_result
                continue
            unit_result, new_sources, new_evidence = batch_result
            constrained_sources = _apply_domain_constraints(
                new_sources,
                include_domains=plan.include_domains,
                exclude_domains=plan.exclude_domains,
            )
            if unit.unit_type in {"fetch", "map"} and not unit_result.get("summary") and not constrained_sources and not new_evidence:
                failed_unit_ids.append(unit.unit_id)
                failed_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "unit_type": unit.unit_type,
                        "reason": f"empty_{unit.unit_type}_result",
                    }
                )
                runtime.store.append_event(
                    job_id,
                    type="research_unit_failed",
                    phase="researching",
                    message=f"Failed {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "error": f"empty_{unit.unit_type}_result"},
                )
                progress = 20.0 + ((len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids)) / unit_total) * 50.0
                runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())
                continue
            source_registry, source_ids = _merge_source_registry(
                source_registry,
                constrained_sources,
                include_domains=plan.include_domains,
                exclude_domains=plan.exclude_domains,
            )
            preferred_citations = _preferred_citation_ids(source_ids, source_registry)
            new_evidence = _hydrate_evidence_source_ids(new_evidence, source_registry)
            completed_unit_ids.append(unit.unit_id)
            unit_results[unit.unit_id] = {
                "unit_id": unit.unit_id,
                "unit_type": unit.unit_type,
                "summary": unit_result["summary"],
                "detail": unit_result["detail"],
                "citations": preferred_citations,
                "source_ids": preferred_citations,
                "warnings": list(unit_result.get("warnings") or []),
                "requested_model": unit_result.get("requested_model", ""),
                "effective_model": unit_result.get("effective_model", ""),
                "provider_name": unit_result.get("provider_name", ""),
                "provider_model": unit_result.get("provider_model", ""),
                "provider_api_url": unit_result.get("provider_api_url", ""),
            }
            evidence_items.extend(new_evidence)
            unit_results = _sanitize_unit_results(unit_results, source_registry)
            evidence_items = _sanitize_evidence_items(evidence_items, source_registry)
            checkpoint_state = DeepResearchCheckpointState(
                plan=plan,
                completed_unit_ids=completed_unit_ids,
                unit_results=unit_results,
                sources=source_registry,
                evidence_items=evidence_items,
                sections=sections,
            )
            runtime.store.save_checkpoint(
                job_id,
                phase="researching",
                checkpoint_key=f"researching-{unit.unit_id}",
                state=checkpoint_state.model_dump(),
            )
            runtime.store.append_event(
                job_id,
                type="research_unit_completed",
                phase="researching",
                message=f"Completed {unit.unit_id}.",
                data={
                    "unit_type": unit.unit_type,
                    "sources_count": len(source_ids),
                    "warnings": list(unit_result.get("warnings") or []),
                    "provider_name": unit_result.get("provider_name", ""),
                    "provider_model": unit_result.get("provider_model", ""),
                    "effective_model": unit_result.get("effective_model", ""),
                },
            )
            progress = 20.0 + ((len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids)) / unit_total) * 50.0
            runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())
        if batch_error is not None:
            if runtime.store.get_job(job_id).cancel_requested:
                _mark_canceled(
                    runtime,
                    job_id,
                    "researching",
                    data={"error": str(batch_error), "reason": "cancel_requested_during_batch"},
                )
                return
            raise batch_error
        pending_units = [
            unit
            for unit in plan.research_units
            if unit.unit_id not in completed_unit_ids
            and unit.unit_id not in failed_unit_ids
            and unit.unit_id not in skipped_unit_ids
        ]
        has_pending_dependency_chain = any(unit.depends_on for unit in pending_units)
        if (
            stop_on_sufficient_coverage
            and not has_pending_dependency_chain
            and _has_sufficient_runtime_coverage(plan, unit_results)
        ):
            skipped_any = False
            for unit in plan.research_units:
                if (
                    unit.unit_id in completed_unit_ids
                    or unit.unit_id in failed_unit_ids
                    or unit.unit_id in skipped_unit_ids
                ):
                    continue
                skipped_unit_ids.append(unit.unit_id)
                skipped_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "unit_type": unit.unit_type,
                        "reason": "sufficient_coverage_reached",
                    }
                )
                runtime.store.append_event(
                    job_id,
                    type="research_unit_skipped",
                    phase="researching",
                    message=f"Skipped {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "reason": "sufficient_coverage_reached"},
                )
                skipped_any = True
            if skipped_any:
                progress = 20.0 + ((len(completed_unit_ids) + len(failed_unit_ids) + len(skipped_unit_ids)) / unit_total) * 50.0
                runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())

    sections = _build_section_citations(plan, evidence_items, source_registry)
    source_registry = _annotate_source_usage(
        source_registry,
        sections,
        reference_texts=[plan.query, *(f"{section.title} {section.goal}" for section in plan.report_outline)],
        include_domains=plan.include_domains,
        exclude_domains=plan.exclude_domains,
    )
    unit_results = _sanitize_unit_results(unit_results, source_registry)
    runtime.store.update_job(job_id, phase="synthesizing", progress_pct=82.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="synthesizing",
        message="Synthesizing findings.",
        data={"section_count": len(sections)},
    )
    partial_report = _build_partial_report(plan, completed_unit_ids, unit_results, sections)
    runtime.write_artifact(job_id, "partial_report.md", partial_report, "text/markdown")
    runtime.store.save_checkpoint(
        job_id,
        phase="synthesizing",
        checkpoint_key="synthesizing",
        state=DeepResearchCheckpointState(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
        ).model_dump(),
    )

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, "synthesizing")
        return

    citations = {
        "source_registry": {item["source_id"]: item for item in source_registry},
        "sections": _sanitize_sections(sections, source_registry),
    }
    report_summary = _build_report_summary(plan, citations["sections"])
    runtime_warnings = sorted({warning for result in unit_results.values() for warning in result.get("warnings", [])})
    report_coverage = _coverage_for_report(plan, citations["sections"])
    coverage_diagnostics = {
        "query": plan.query,
        "must_cover": list(plan.brief.must_cover),
        "coverage_checklist": list(plan.brief.coverage_checklist),
        **report_coverage,
    }
    grounding_diagnostics = _build_grounding_diagnostics(citations["sections"], citations["source_registry"])
    runtime_warnings = sorted({*runtime_warnings, *_coverage_warning_codes(report_coverage)})
    report_status = "degraded" if runtime_warnings or not citations["sections"] or failed_units else "completed"
    report_confidence = _cluster_confidence(
        source_count=len({citation for section in citations["sections"] for citation in section.get("citations", [])}),
        evidence_count=sum(len(section.get("claims", [])) for section in citations["sections"]),
    )
    report = {
        "query": plan.query,
        "summary": report_summary,
        "confidence": report_confidence,
        "status": report_status,
        "sections": citations["sections"],
        "coverage": report_coverage,
        "unit_results": unit_results,
        "runtime": {
            "warnings": runtime_warnings,
            "coverage": {
                "must_cover_count": len(plan.brief.must_cover),
                "uncovered_sub_question_count": len(report_coverage.get("uncovered_sub_questions", [])),
                "unanswered_section_count": len(report_coverage.get("unanswered_sections", [])),
            },
            "grounding": {
                "total_claims": grounding_diagnostics["total_claims"],
                "ungrounded_claims": grounding_diagnostics["ungrounded_claims"],
                "single_source_claims": grounding_diagnostics["single_source_claims"],
            },
            "failed_units": failed_units,
            "skipped_units": skipped_units,
            "provider_winners": [
                {
                    "unit_id": result.get("unit_id", ""),
                    "provider_name": result.get("provider_name", ""),
                    "provider_model": result.get("provider_model", ""),
                    "effective_model": result.get("effective_model", ""),
                }
                for result in unit_results.values()
                if result.get("provider_name")
            ],
        },
    }
    final_report = _build_final_report(plan, citations["sections"], citations["source_registry"], report_summary)
    runtime.write_artifact(job_id, "coverage.json", _json_markdown_block(coverage_diagnostics), "application/json")
    runtime.write_artifact(job_id, "grounding.json", _json_markdown_block(grounding_diagnostics), "application/json")
    runtime.store.update_job(job_id, phase="finalizing", progress_pct=94.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="finalizing",
        message="Finalizing report.",
        data={},
    )
    persisted_artifacts = runtime.write_artifact_batch(
        job_id,
        [
            {"kind": "sources.json", "content": _json_markdown_block(source_registry), "content_type": "application/json"},
            {"kind": "citations.json", "content": _json_markdown_block(citations), "content_type": "application/json"},
            {"kind": "report.json", "content": _json_markdown_block(report), "content_type": "application/json"},
            {"kind": "final_report.md", "content": final_report, "content_type": "text/markdown"},
        ],
    )
    artifact_batch_id = next(
        (artifact.get("metadata", {}).get("batch_id", "") for artifact in persisted_artifacts if artifact.get("metadata")),
        "",
    )
    runtime.store.save_checkpoint(
        job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state=DeepResearchCheckpointState(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
        ).model_dump(),
    )
    runtime.store.update_job(
        job_id,
        status="completed",
        phase="finalizing",
        progress_pct=100.0,
        finished_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
    )
    runtime.store.append_event(
        job_id,
        type="job_completed",
        phase="finalizing",
        message="Deep research completed.",
        data={"sources_count": len(source_registry), "artifact_batch_id": artifact_batch_id},
    )


def _mark_canceled(
    runtime: DeepResearchRuntime,
    job_id: str,
    phase: str,
    *,
    data: dict[str, Any] | None = None,
) -> None:
    current_job = runtime.store.get_job(job_id)
    if current_job.status == "canceled" and current_job.finished_at:
        return
    runtime.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
    runtime.store.append_event(job_id, type="job_canceled", phase=phase, message="Canceled.", data=data or {})


def _write_partial_outputs(
    runtime: DeepResearchRuntime,
    job_id: str,
    plan: DeepResearchPlan,
    completed_unit_ids: list[str],
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> None:
    partial_report = _build_partial_report(plan, completed_unit_ids, unit_results, sections)
    runtime.write_artifact(job_id, "partial_report.md", partial_report, "text/markdown")


async def _execute_research_unit(
    runtime: DeepResearchRuntime,
    plan: DeepResearchPlan,
    unit: DeepResearchResearchUnit,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    reference_texts = [plan.query, unit.title, unit.goal, unit.query, unit.instructions]
    if unit.unit_type == "fetch":
        fetched = await _fetch_url(unit.url)
        if not fetched:
            return (
                {"summary": "", "detail": ""},
                [],
                [],
            )
        detail = _extract_relevant_excerpt(
            fetched,
            reference_texts=reference_texts,
            line_limit=8,
            char_limit=1200,
            multiline=True,
        )
        summary = _extract_relevant_excerpt(
            fetched,
            reference_texts=reference_texts,
            line_limit=3,
            char_limit=180,
        )
        source = {"url": unit.url, "title": unit.title}
        return (
            {"summary": summary, "detail": detail},
            [source],
            [
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}",
                    unit_id=unit.unit_id,
                    source_urls=[unit.url] if unit.url else [],
                    summary=_extract_relevant_excerpt(
                        fetched,
                        reference_texts=reference_texts,
                        line_limit=4,
                        char_limit=_MAX_CLAIM_LENGTH,
                    ),
                    detail=detail,
                    evidence_kind="fetch",
                    weight=1.0,
                    derived_from_source_url=unit.url,
                ).model_dump()
            ],
        )

    if unit.unit_type == "map":
        mapped = await _map_url(unit.url, unit.instructions)
        if not mapped:
            return (
                {"summary": "", "detail": ""},
                [],
                [],
            )
        mapped_raw = mapped
        detail = _extract_relevant_excerpt(
            mapped,
            reference_texts=reference_texts,
            line_limit=8,
            char_limit=1200,
            multiline=True,
        )
        sources = [{"url": unit.url, "title": unit.title}]
        evidence_items = [
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-map",
                unit_id=unit.unit_id,
                source_urls=[unit.url] if unit.url else [],
                summary=_extract_relevant_excerpt(
                    mapped,
                    reference_texts=reference_texts,
                    line_limit=4,
                    char_limit=_MAX_CLAIM_LENGTH,
                ),
                detail=detail,
                evidence_kind="map",
                weight=0.6,
                derived_from_source_url=unit.url,
            ).model_dump()
        ]
        candidate_sources = [
            {"url": candidate, "title": _guess_title_from_url(candidate)}
            for candidate in extract_unique_urls(mapped_raw)
            if candidate and candidate != unit.url
        ]
        fetch_limit = max(0, plan.search_strategy.selective_fetch.max_urls_per_search)
        ranked_candidate_sources = _select_fetch_sources(candidate_sources, plan, unit)
        for candidate_source in ranked_candidate_sources[:fetch_limit]:
            candidate_url = candidate_source["url"]
            fetched = await _fetch_url(candidate_url)
            if not fetched:
                continue
            fetched_source = _enrich_source_from_fetched_text(
                candidate_source,
                fetched,
            )
            sources.append(fetched_source)
            evidence_items.append(
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}-fetch-{len(evidence_items)}",
                    unit_id=unit.unit_id,
                    source_urls=[candidate_url],
                    summary=_extract_relevant_excerpt(
                        fetched,
                        reference_texts=reference_texts + [candidate_url, fetched_source.get("title", "")],
                        line_limit=4,
                        char_limit=_MAX_CLAIM_LENGTH,
                    ),
                    detail=_extract_relevant_excerpt(
                        fetched,
                        reference_texts=reference_texts + [candidate_url, fetched_source.get("title", "")],
                        line_limit=8,
                        char_limit=1200,
                        multiline=True,
                    ),
                    evidence_kind="fetch",
                    weight=1.0,
                    derived_from_source_url=candidate_url,
                ).model_dump()
            )
        return (
            {
                "summary": _extract_relevant_excerpt(
                    mapped,
                    reference_texts=reference_texts,
                    line_limit=3,
                    char_limit=180,
                ),
                "detail": detail,
            },
            sources,
            evidence_items,
        )

    if _search_query is _DEFAULT_SEARCH_QUERY_FN:
        search_result = await _search_query_with_details(unit.query or unit.goal)
        answer = search_result["answer"]
        sources = search_result["sources"]
    else:
        answer, sources = await _search_query(unit.query or unit.goal)
        search_result = {
            "answer": answer,
            "sources": sources,
            "warning_code": None,
            "requested_model": "",
            "effective_model": "",
            "provider_name": "",
            "provider_model": "",
            "provider_api_url": "",
        }
    answer_summary = _extract_relevant_excerpt(
        search_result["answer"],
        reference_texts=reference_texts,
        line_limit=4,
        char_limit=_MAX_CLAIM_LENGTH,
    )
    answer_detail = _extract_relevant_excerpt(
        search_result["answer"],
        reference_texts=reference_texts,
        line_limit=8,
        char_limit=1200,
        multiline=True,
    )
    selective_fetch = plan.search_strategy.selective_fetch
    fetch_limit = max(0, selective_fetch.max_urls_per_search)
    selected_sources_for_grounding = _select_fetch_sources(sources, plan, unit)
    if fetch_limit == 0:
        search_support_sources = list(selected_sources_for_grounding)
    else:
        search_support_sources = selected_sources_for_grounding[: max(1, min(len(selected_sources_for_grounding), fetch_limit))]
    primary_search_support_source = search_support_sources[:1]
    evidence_items: list[dict[str, Any]] = []
    if not search_result.get("warning_code"):
        evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-search",
                unit_id=unit.unit_id,
                source_urls=[source.get("url", "") for source in primary_search_support_source if source.get("url")],
                summary=answer_summary,
                detail=answer_detail,
                evidence_kind="search",
                weight=0.9,
            ).model_dump()
        )
    fetched_evidence_items: list[dict[str, Any]] = []
    for source in selected_sources_for_grounding[:fetch_limit]:
        fetched = await _fetch_url(source["url"])
        if not fetched:
            continue
        enriched_source = _enrich_source_from_fetched_text(source, fetched)
        for index, original_source in enumerate(sources):
            if original_source.get("url") == enriched_source.get("url"):
                sources[index] = enriched_source
                break
        fetched_evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-fetch-{len(evidence_items)}",
                unit_id=unit.unit_id,
                source_urls=[source["url"]],
                summary=_extract_relevant_excerpt(
                    fetched,
                    reference_texts=reference_texts + [source["url"], enriched_source.get("title", "")],
                    line_limit=4,
                    char_limit=_MAX_CLAIM_LENGTH,
                ),
                detail=_extract_relevant_excerpt(
                    fetched,
                    reference_texts=reference_texts + [source["url"], enriched_source.get("title", "")],
                    line_limit=8,
                    char_limit=1200,
                    multiline=True,
                ),
                evidence_kind="fetch",
                weight=1.0,
                derived_from_source_url=source["url"],
            ).model_dump()
        )
    evidence_items.extend(fetched_evidence_items)
    return (
        {
            "summary": _trim_text(answer_summary, limit=180),
            "detail": answer_detail,
            "warnings": [search_result["warning_code"]] if search_result.get("warning_code") else [],
            "requested_model": search_result.get("requested_model", ""),
            "effective_model": search_result.get("effective_model", ""),
            "provider_name": search_result.get("provider_name", ""),
            "provider_model": search_result.get("provider_model", ""),
            "provider_api_url": search_result.get("provider_api_url", ""),
        },
        sources,
        evidence_items,
    )


def _merge_source_registry(
    existing_sources: list[dict[str, Any]],
    new_sources: list[dict[str, Any]],
    *,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    existing_by_key: dict[str, dict[str, Any]] = {}
    for item in existing_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        existing_by_key[_source_registry_key(url)] = dict(item)
    merged_ids: list[str] = []
    for source in standardize_sources(new_sources):
        url = source.get("url", "").strip()
        if not url:
            continue
        key = _source_registry_key(url)
        current = existing_by_key.get(key)
        if current is None:
            current = dict(source)
            current["source_id"] = current.get("source_id") or _next_source_id(list(existing_by_key.values()))
        else:
            current = _merge_source_metadata(current, source)
        if not current.get("title") and current.get("url"):
            current["title"] = _guess_title_from_url(str(current["url"]))
        current["source_key"] = key
        current["quality_score"] = _source_quality_score(current)
        current["quality_tier"] = _source_quality_tier(current)
        current["source_type"] = _source_type(current)
        current["winner_provider"] = current.get("provider", "")
        current["ranking_reasons"] = _source_ranking_reasons(
            current,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        existing_by_key[key] = current
        merged_ids.append(current["source_id"])
    merged_sources = sorted(existing_by_key.values(), key=_source_sort_tuple)
    for rank, item in enumerate(merged_sources, start=1):
        item["rank"] = rank
    return merged_sources, merged_ids


def _annotate_source_usage(
    source_registry: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    *,
    reference_texts: list[str] | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> list[dict[str, Any]]:
    citation_count: dict[str, int] = {}
    section_count: dict[str, int] = {}
    for section in sections:
        section_citations = {citation for citation in section.get("citations", []) if citation}
        for citation in section_citations:
            section_count[citation] = section_count.get(citation, 0) + 1
        for claim in section.get("claims", []):
            for citation in claim.get("citations", []):
                citation_count[citation] = citation_count.get(citation, 0) + 1
    annotated: list[dict[str, Any]] = []
    for item in source_registry:
        normalized = dict(item)
        source_id = str(normalized.get("source_id", ""))
        normalized["citation_count"] = citation_count.get(source_id, 0)
        normalized["section_count"] = section_count.get(source_id, 0)
        normalized["winner_provider"] = normalized.get("winner_provider") or normalized.get("provider", "")
        normalized["topic_match_score"] = _source_topic_match_score(normalized, reference_texts)
        normalized["ranking_reasons"] = _source_ranking_reasons(
            normalized,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        normalized["ranking_penalties"] = []
        annotated.append(normalized)
    strongest_used_topic_by_domain: dict[str, int] = {}
    for item in annotated:
        domain = str(item.get("domain", "") or "").strip().lower()
        if not domain:
            continue
        if item.get("citation_count", 0) <= 0 and item.get("section_count", 0) <= 0:
            continue
        strongest_used_topic_by_domain[domain] = max(
            strongest_used_topic_by_domain.get(domain, 0),
            int(item.get("topic_match_score", 0) or 0),
        )
    filtered: list[dict[str, Any]] = []
    for item in annotated:
        domain = str(item.get("domain", "") or "").strip().lower()
        topic_match_score = int(item.get("topic_match_score", 0) or 0)
        if (
            domain
            and domain in strongest_used_topic_by_domain
            and item.get("citation_count", 0) <= 0
            and item.get("section_count", 0) <= 0
            and strongest_used_topic_by_domain.get(domain, 0) >= topic_match_score
        ):
            item["ranking_penalties"].append("same_domain_off_topic")
            continue
        filtered.append(item)
    if any(item.get("citation_count", 0) > 0 or item.get("section_count", 0) > 0 for item in filtered):
        used_only: list[dict[str, Any]] = []
        for item in filtered:
            if item.get("citation_count", 0) > 0 or item.get("section_count", 0) > 0:
                used_only.append(item)
            else:
                item["ranking_penalties"].append("unused_source")
        filtered = used_only
    annotated = sorted(filtered, key=_source_sort_tuple)
    for rank, item in enumerate(annotated, start=1):
        item["rank"] = rank
    return annotated


def _select_fetch_sources(
    sources: list[dict[str, Any]],
    plan: DeepResearchPlan,
    unit: DeepResearchResearchUnit,
) -> list[dict[str, Any]]:
    prefer_outline = bool(plan.search_strategy.selective_fetch.prefer_titles_matching_outline)
    outline_keywords = _tokenize_keywords(" ".join(f"{section.title} {section.goal}" for section in plan.report_outline))
    query_keywords = _tokenize_keywords(f"{plan.query} {unit.title} {unit.goal} {unit.query}")

    query_intent_text = f"{plan.query} {unit.title} {unit.goal} {unit.query}".lower()

    def score(source: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
        title = f"{source.get('title', '')} {source.get('description', '')} {source.get('url', '')}"
        quality_bias = _source_quality_bias(source)
        lowered_title = title.lower()
        shell_penalty = 0
        if "troubleshooting" not in query_intent_text and "troubleshooting" in lowered_title:
            shell_penalty -= 2
        if "support" not in query_intent_text and "support" in lowered_title:
            shell_penalty -= 1
        return (
            quality_bias,
            _count_keyword_overlap(title, query_keywords),
            _count_keyword_overlap(title, outline_keywords) if prefer_outline else 0,
            _source_topic_match_score(source, [plan.query, unit.goal, unit.query]),
            shell_penalty,
            source.get("url", ""),
        )

    ranked = sorted(sources, key=score, reverse=True)
    if any(_count_keyword_overlap(f"{source.get('title', '')} {source.get('description', '')} {source.get('url', '')}", query_keywords) > 0 for source in ranked):
        ranked = [
            source
            for source in ranked
            if _count_keyword_overlap(f"{source.get('title', '')} {source.get('description', '')} {source.get('url', '')}", query_keywords) > 0
        ]
    return ranked


def _evidence_similarity(left: DeepResearchEvidenceItem, right: DeepResearchEvidenceItem) -> float:
    left_text = _summarize_evidence_text(left.summary or left.detail, limit=_MAX_CLAIM_LENGTH)
    right_text = _summarize_evidence_text(right.summary or right.detail, limit=_MAX_CLAIM_LENGTH)
    if not left_text or not right_text:
        return 0.0
    left_key = _stable_text_key(left_text)
    right_key = _stable_text_key(right_text)
    if left_key and right_key and (left_key in right_key or right_key in left_key):
        return 1.0
    left_tokens = set(_tokenize_keywords(left_text))
    right_tokens = set(_tokenize_keywords(right_text))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if union == 0:
        return 0.0
    return intersection / union


def _supporting_domain_count(source_ids: list[str], source_registry: dict[str, dict[str, Any]]) -> int:
    domains: set[str] = set()
    for source_id in source_ids:
        source = source_registry.get(source_id, {})
        domain = str(source.get("domain", "") or "").strip().lower()
        if not domain and source.get("url"):
            try:
                domain = urlsplit(str(source["url"])).netloc.lower()
            except Exception:
                domain = ""
        if domain:
            domains.add(domain)
    return len(domains)


def _coverage_for_report(
    plan: DeepResearchPlan,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    answered_section_ids = [str(section.get("section_id", "")).strip() for section in sections if str(section.get("section_id", "")).strip()]
    covered_sub_question_ids: list[str] = []
    uncovered_sub_questions: list[str] = []
    sub_question_coverage: list[dict[str, Any]] = []
    for item in plan.sub_questions:
        question = item.question.strip()
        if not question:
            continue
        question_tokens = _tokenize_keywords(question)
        coverage_threshold = max(2, min(4, max(1, len(question_tokens) // 2)))
        matching_section_ids: list[str] = []
        matching_claim_ids: list[str] = []
        for section in sections:
            section_id = str(section.get("section_id", "")).strip()
            section_matched = False
            for claim in section.get("claims", []):
                claim_text = str(claim.get("text", ""))
                if _count_keyword_overlap(claim_text, question_tokens) >= coverage_threshold:
                    if section_id and section_id not in matching_section_ids:
                        matching_section_ids.append(section_id)
                    claim_id = str(claim.get("claim_id", "")).strip()
                    if claim_id and claim_id not in matching_claim_ids:
                        matching_claim_ids.append(claim_id)
                    section_matched = True
            if not section_matched:
                section_text = " ".join([str(section.get("title", "")), str(section.get("summary", ""))])
                if _count_keyword_overlap(section_text, question_tokens) >= max(coverage_threshold, 3):
                    if section_id and section_id not in matching_section_ids:
                        matching_section_ids.append(section_id)
        covered = bool(matching_claim_ids)
        if covered:
            covered_sub_question_ids.append(item.id)
        else:
            uncovered_sub_questions.append(question)
        sub_question_coverage.append(
            {
                "sub_question_id": item.id,
                "question": question,
                "covered": covered,
                "section_ids": matching_section_ids,
                "claim_ids": matching_claim_ids,
            }
        )
    return {
        "planned_section_ids": [section.section_id for section in plan.report_outline],
        "answered_section_ids": answered_section_ids,
        "unanswered_sections": [
            section.title
            for section in plan.report_outline
            if section.section_id not in answered_section_ids
        ],
        "planned_sub_question_ids": [item.id for item in plan.sub_questions],
        "covered_sub_question_ids": covered_sub_question_ids,
        "uncovered_sub_questions": uncovered_sub_questions,
        "sub_questions": sub_question_coverage,
    }


def _coverage_warning_codes(coverage: dict[str, Any]) -> list[str]:
    if coverage.get("unanswered_sections") or coverage.get("uncovered_sub_questions"):
        return ["coverage_incomplete"]
    return []


def _build_grounding_diagnostics(
    sections: list[dict[str, Any]],
    source_registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    section_diagnostics: list[dict[str, Any]] = []
    total_claims = 0
    grounded_claims = 0
    single_source_claims = 0
    low_confidence_claims = 0
    for section in sections:
        claims = section.get("claims", [])
        claim_diagnostics: list[dict[str, Any]] = []
        for claim in claims:
            total_claims += 1
            citation_ids = [citation for citation in claim.get("citations", []) if citation in source_registry]
            grounded = bool(citation_ids)
            if grounded:
                grounded_claims += 1
            if len(set(citation_ids)) <= 1:
                single_source_claims += 1
            if str(claim.get("confidence", "")).lower() == "low":
                low_confidence_claims += 1
            claim_diagnostics.append(
                {
                    "claim_id": claim.get("claim_id", ""),
                    "citation_ids": citation_ids,
                    "grounded": grounded,
                    "supporting_source_count": len(set(citation_ids)),
                    "supporting_domain_count": _supporting_domain_count(citation_ids, source_registry),
                    "confidence": claim.get("confidence", ""),
                }
            )
        section_diagnostics.append(
            {
                "section_id": section.get("section_id", ""),
                "title": section.get("title", ""),
                "claim_count": len(claim_diagnostics),
                "grounded_claim_count": sum(1 for claim in claim_diagnostics if claim["grounded"]),
                "claims": claim_diagnostics,
            }
        )
    return {
        "total_claims": total_claims,
        "grounded_claims": grounded_claims,
        "ungrounded_claims": max(0, total_claims - grounded_claims),
        "single_source_claims": single_source_claims,
        "low_confidence_claims": low_confidence_claims,
        "sections": section_diagnostics,
    }


def _cluster_confidence(*, source_count: int, evidence_count: int, cluster_type: str = "", domain_count: int = 0) -> str:
    if cluster_type == "gap":
        return "low"
    if domain_count >= 2 and source_count >= 2 and evidence_count >= 2:
        return "high"
    if source_count >= 1:
        return "medium"
    return "low"


def _cluster_type_for_items(items: list[DeepResearchEvidenceItem]) -> str:
    if any(_has_gap_signal(item.summary) or _has_gap_signal(item.detail) for item in items):
        return "gap"
    source_ids = {source_id for item in items for source_id in item.source_ids}
    if len(source_ids) >= 2:
        return "consensus"
    return "single_source"


def _best_cluster_claim_text(items: list[DeepResearchEvidenceItem]) -> str:
    ranked = sorted(
        items,
        key=lambda item: (
            item.weight,
            len(item.source_ids),
            len(_summarize_evidence_text(item.summary or item.detail, limit=_MAX_CLAIM_LENGTH)),
        ),
        reverse=True,
    )
    for item in ranked:
        text = _summarize_evidence_text(item.summary or item.detail, limit=_MAX_CLAIM_LENGTH)
        if text and not _is_noisy_text(text):
            return text
    return ""


def _build_section_citations(
    plan: DeepResearchPlan,
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outline = plan.report_outline
    registry_by_id = {item["source_id"]: item for item in source_registry if item.get("source_id")}
    claims_pool = [
        DeepResearchEvidenceItem.model_validate(item)
        for item in evidence_items
        if item.get("summary") and not _is_noisy_text(item.get("summary", ""))
    ]
    if not claims_pool:
        claims_pool = [
            DeepResearchEvidenceItem(
                evidence_id="evidence-empty",
                unit_id="none",
                summary="No conclusive evidence was collected.",
                detail="No conclusive evidence was collected.",
            )
        ]

    sections: list[dict[str, Any]] = []
    claim_index = 1
    used_evidence_ids: set[str] = set()
    used_claim_keys: set[str] = set()
    query_keywords = _tokenize_keywords(plan.query)
    for section in outline:
        if _is_gap_section(section) and not any(
            _has_gap_signal(item.summary) or _has_gap_signal(item.detail) for item in claims_pool
        ):
            continue
        is_generic_section = section.title.lower() in {"executive summary", "key findings", "summary"}
        section_keywords = _tokenize_keywords(f"{section.title} {section.goal}")
        if is_generic_section:
            section_keywords = query_keywords
        if not section_keywords:
            section_keywords = query_keywords
        ranked_pool = sorted(
            claims_pool,
            key=lambda evidence: (
                _count_keyword_overlap(f"{evidence.summary} {evidence.detail}", section_keywords),
                evidence.weight,
                0 if evidence.evidence_id in used_evidence_ids else 1,
                sum(_source_quality_score(registry_by_id.get(source_id, {})) for source_id in evidence.source_ids),
                len(evidence.source_ids),
                evidence.evidence_id,
            ),
            reverse=True,
        )
        relevant_evidence: list[DeepResearchEvidenceItem] = []
        for evidence in ranked_pool:
            overlap_score = _count_keyword_overlap(f"{evidence.summary} {evidence.detail}", section_keywords)
            if not evidence.source_ids:
                continue
            if overlap_score <= 0 and section_keywords and not is_generic_section:
                continue
            relevant_evidence.append(evidence)
        section_claims: list[dict[str, Any]] = []
        clusters: list[list[DeepResearchEvidenceItem]] = []
        cluster_seed_items = [evidence for evidence in relevant_evidence if evidence.evidence_kind != "search"] or relevant_evidence
        supplemental_items = [evidence for evidence in relevant_evidence if evidence not in cluster_seed_items]
        for evidence in cluster_seed_items:
            placed = False
            for cluster in clusters:
                if any(_evidence_similarity(evidence, existing) >= 0.55 for existing in cluster):
                    cluster.append(evidence)
                    placed = True
                    break
            if not placed:
                clusters.append([evidence])
        for evidence in supplemental_items:
            best_cluster: list[DeepResearchEvidenceItem] | None = None
            best_similarity = 0.0
            for cluster in clusters:
                similarity = max(_evidence_similarity(evidence, existing) for existing in cluster)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_cluster = cluster
            cluster_source_ids = {source_id for item in (best_cluster or []) for source_id in item.source_ids}
            single_source_match = len(set(evidence.source_ids)) == 1 and bool(set(evidence.source_ids) & cluster_source_ids)
            if best_cluster is not None and (best_similarity >= 0.35 or single_source_match):
                best_cluster.append(evidence)
            else:
                clusters.append([evidence])
        ranked_clusters = sorted(
            clusters,
            key=lambda cluster: (
                max(_count_keyword_overlap(f"{item.summary} {item.detail}", section_keywords) for item in cluster),
                sum(item.weight for item in cluster),
                len({source_id for item in cluster for source_id in item.source_ids}),
                len(cluster),
            ),
            reverse=True,
        )
        for cluster in ranked_clusters:
            claim_text = _best_cluster_claim_text(cluster)
            if not claim_text or _is_noisy_text(claim_text):
                continue
            claim_key = _stable_text_key(claim_text)
            if claim_key in used_claim_keys:
                continue
            cluster_source_ids = _dedupe_preserve_order([source_id for item in cluster for source_id in item.source_ids])
            supporting_domain_count = _supporting_domain_count(cluster_source_ids, registry_by_id)
            cluster_type = _cluster_type_for_items(cluster)
            claim = DeepResearchClaim(
                claim_id=f"{section.section_id}-claim-{claim_index}",
                text=claim_text,
                citations=_preferred_citation_ids(cluster_source_ids, source_registry, limit=3),
                unit_id=cluster[0].unit_id,
                evidence_ids=_dedupe_preserve_order([item.evidence_id for item in cluster]),
                cluster_type=cluster_type,
                supporting_source_count=len(set(cluster_source_ids)),
                supporting_domain_count=supporting_domain_count,
                confidence=_cluster_confidence(
                    source_count=len(set(cluster_source_ids)),
                    evidence_count=len(cluster),
                    cluster_type=cluster_type,
                    domain_count=supporting_domain_count,
                ),
            )
            section_claims.append(claim.model_dump())
            for item in cluster:
                used_evidence_ids.add(item.evidence_id)
            used_claim_keys.add(claim_key)
            claim_index += 1
            if len(section_claims) >= 2:
                break
        if not section_claims:
            continue
        section_summary = _build_section_summary(section_claims)
        section_source_count = len({citation for claim in section_claims for citation in claim.get("citations", [])})
        section_domain_count = _supporting_domain_count(
            [citation for claim in section_claims for citation in claim.get("citations", [])],
            registry_by_id,
        )
        section_model = DeepResearchSectionCitations(
            section_id=section.section_id,
            title=section.title,
            summary=section_summary,
            claims=section_claims,
            citations=sorted({citation for claim in section_claims for citation in claim.get("citations", [])}),
            confidence=_cluster_confidence(
                source_count=section_source_count,
                evidence_count=sum(len(claim.get("evidence_ids", [])) for claim in section_claims),
                domain_count=section_domain_count,
            ),
            claim_cluster_count=len(section_claims),
            supporting_source_count=section_source_count,
            supporting_domain_count=section_domain_count,
        )
        sections.append(section_model.model_dump())
    return sections


def _build_section_summary(section_claims: list[dict[str, Any]]) -> str:
    summary_parts: list[str] = []
    for claim in section_claims:
        text = _summarize_evidence_text(str(claim.get("text", "")), limit=180)
        if not text:
            continue
        if text in summary_parts:
            continue
        summary_parts.append(text)
        if len(summary_parts) >= 2:
            break
    if not summary_parts:
        return ""
    claim_confidences = {str(claim.get("confidence", "")) for claim in section_claims if claim.get("confidence")}
    confidence_prefix = ""
    if "high" in claim_confidences:
        confidence_prefix = "High confidence: "
    elif "medium" in claim_confidences:
        confidence_prefix = "Medium confidence: "
    elif "low" in claim_confidences:
        confidence_prefix = "Low confidence: "
    if len(summary_parts) == 1:
        return _trim_text(f"{confidence_prefix}Key point: {summary_parts[0]}", limit=260)
    return _trim_text(f"{confidence_prefix}{' '.join(summary_parts)}", limit=260)


def _build_partial_report(
    plan: DeepResearchPlan,
    completed_unit_ids: list[str],
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> str:
    lines = [
        "# Partial Report",
        "",
        "## Progress",
        "",
        f"- Completed units: {len(completed_unit_ids)} / {len(plan.research_units)}",
        "",
        "## Research Brief",
        "",
        f"- Objective: {plan.brief.objective}",
        f"- Deliverable: {plan.brief.deliverable}",
        "",
        "## Completed Units",
        "",
    ]
    for unit_id in completed_unit_ids:
        result = unit_results.get(unit_id, {})
        lines.append(f"- `{unit_id}`: {result.get('summary', 'Completed.')}")
    if sections:
        lines.extend(["", "## Draft Sections", ""])
        for section in sections:
            lines.append(f"### {section['title']}")
            lines.append("")
            if section.get("summary"):
                lines.append(section["summary"])
                lines.append("")
            for claim in section.get("claims", []):
                refs = ", ".join(claim.get("citations", []))
                lines.append(f"- {claim['text']} [{refs}]".rstrip())
            lines.append("")
    pending_titles = [unit.title for unit in plan.research_units if unit.unit_id not in completed_unit_ids]
    if pending_titles:
        lines.extend(["## Remaining Work", ""])
        lines.extend(f"- {title}" for title in pending_titles)
    return "\n".join(lines).strip() + "\n"


def _build_final_report(
    plan: DeepResearchPlan,
    sections: list[dict[str, Any]],
    source_registry: dict[str, dict[str, Any]],
    summary: str,
) -> str:
    lines = [f"# {plan.query}", ""]
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    for section in sections:
        lines.append(f"## {section['title']}")
        lines.append("")
        if section.get("summary"):
            lines.append(section["summary"])
            lines.append("")
        for claim in section.get("claims", []):
            refs = ", ".join(claim.get("citations", []))
            lines.append(f"- {claim['text']} [{refs}]".rstrip())
        lines.append("")
    lines.extend(["## Sources", ""])
    for source_id, item in source_registry.items():
        title = item.get("title") or item["url"]
        descriptors = [value for value in (item.get("source_type"), item.get("domain")) if value]
        reasons = ", ".join(item.get("ranking_reasons") or [])
        meta = "; ".join(descriptors)
        if reasons:
            meta = f"{meta}; reasons: {reasons}" if meta else f"reasons: {reasons}"
        if meta:
            lines.append(f"- [{source_id}] {title} ({meta}) - {item['url']}")
        else:
            lines.append(f"- [{source_id}] {title} - {item['url']}")
    return "\n".join(lines).strip() + "\n"


async def _search_query(query: str) -> tuple[str, list[dict]]:
    result = await _search_query_with_details(query)
    return result["answer"], result["sources"]


async def _search_query_with_details(query: str) -> dict[str, Any]:
    from . import server as server_module

    provider, provider_meta = await _build_runtime_grok_provider(config.grok_model)
    content, sources = await _provider_search_with_sources(provider, query, min_results=3, max_results=8)
    answer, extracted_sources = split_answer_and_sources(content)
    merged = standardize_sources(merge_sources(sources, extracted_sources))
    if not merged:
        merged = standardize_sources([{"url": url} for url in extract_unique_urls(answer or content)])
    warning_code = server_module._assess_search_body_quality(answer, merged)
    return {
        "answer": answer.strip() or content.strip(),
        "sources": merged,
        "warning_code": warning_code,
        "requested_model": provider_meta.get("requested_model"),
        "effective_model": provider_meta.get("effective_model"),
        "provider_name": provider._last_success_provider_name,
        "provider_model": provider._last_success_provider_model,
        "provider_api_url": provider._last_success_provider_api_url,
    }


async def _provider_search_with_sources(
    provider: GrokSearchProvider,
    query: str,
    *,
    platform: str = "",
    min_results: int = 3,
    max_results: int = 10,
) -> tuple[str, list[dict]]:
    kwargs = {
        "platform": platform,
        "min_results": min_results,
        "max_results": max_results,
        "ctx": None,
    }
    supported_kwargs = _filter_supported_search_kwargs(provider.search_with_sources, kwargs)
    content, sources = await provider.search_with_sources(query, **supported_kwargs)
    return content, sources


_DEFAULT_SEARCH_QUERY_FN = _search_query


async def _fetch_url(url: str) -> str | None:
    from . import server

    result = await server.web_fetch(url)
    if not result or result.startswith("提取失败:") or result.startswith("配置错误:"):
        return None
    return result


async def _map_url(url: str, instructions: str = "") -> str | None:
    from . import server

    result = await server.web_map(url, instructions=instructions)
    if not result or result.startswith("映射失败:") or result.startswith("配置错误:"):
        return None
    return result
