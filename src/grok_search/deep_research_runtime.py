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
from .deep_research_evidence import (
    build_evidence_ledger_entries,
    candidate_evidence_ids_by_section,
    evidence_source_ids,
    initialize_section_banks,
    reconcile_section_banks_with_materialized_sections,
    seed_section_banks_from_question_bindings,
    matched_unit_ids_by_section,
    merge_evidence_ledger,
    rejected_evidence_ids_by_section,
    selected_evidence_ids_by_section,
    sync_section_banks_to_outline,
    update_section_banks,
)
from .deep_research_section_graph import initialize_section_graph, update_section_graph
from .deep_research_synthesis import (
    build_synthesis_outline,
    evidence_pool_for_section,
    is_key_findings_section_title,
    is_open_questions_section_title,
    is_summary_section_title,
)
from .deep_research_store import DeepResearchStore
from .deep_research_types import (
    DeepResearchCheckpointState,
    DeepResearchClaim,
    DeepResearchContinuation,
    DeepResearchContinuationState,
    DeepResearchEvidenceItem,
    DeepResearchJob,
    DeepResearchOutlineVersion,
    DeepResearchPlan,
    DeepResearchReportSection,
    DeepResearchResearchUnit,
    DeepResearchSectionCitations,
    DeepResearchSectionGraphState,
    DeepResearchSectionNode,
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
    "for more information, see",
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
    "validate checkpoint information",
    "follow the migration pattern shell",
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
_OFFICIAL_DOC_HOST_PREFIXES = (
    "docs.",
    "developer.",
    "developers.",
    "learn.",
    "platform.",
)
_OFFICIAL_DOC_HOST_EXACT = {
    "adk.dev",
    "ai.google.dev",
    "cloud.google.com",
    "developers.google.com",
    "learn.microsoft.com",
    "platform.openai.com",
}
_COMMUNITY_SOURCE_DOMAINS = {
    "stackoverflow.com",
    "stackexchange.com",
    "reddit.com",
    "news.ycombinator.com",
    "dev.to",
    "medium.com",
}
_GAP_SECTION_MARKERS = ("remaining gap", "remaining gaps", "open question", "open questions")
_GENERIC_CONTINUATION_QUESTION_TITLES = {
    "executive summary",
    "key findings",
    "summary",
    "open questions",
    "open question",
}
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
_GENERIC_OUTLINE_TITLES = {
    "executive summary",
    "summary",
    "key findings",
    "findings",
    "main findings",
    "open questions",
    "remaining gaps",
    "next steps",
    "recommendations",
}
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
_SAFE_SEARCH_ONLY_REPAIR_ACTION_PREFIXES = (
    "filled_search_query:",
    "degraded_fetch_without_url_to_search:",
    "degraded_map_without_url_to_search:",
    "dropped_unknown_dependency:",
    "added_sub_question_search_unit:",
)
_SAFE_SEARCH_ONLY_REPAIR_ISSUES = {
    "missing_search_query",
    "missing_fetch_url",
    "missing_map_url",
    "missing_sub_question_unit_coverage",
    "unknown_dependency",
}
_SAFE_SEARCH_ONLY_BLOCKED_REASON_PREFIXES = ("unknown_dependency:",)
_STRUCTURAL_SAFE_QUERY_REPAIR_SOURCES = {
    "sub_question",
    "search_strategy",
    "unit_goal",
    "unit_title",
}
_NON_BLOCKING_VERIFIER_REASON_CODES = {
    "medium_single_source_search_only",
    "selected_evidence_unused",
}
_EVIDENCE_ITEMS_ARTIFACT_KIND = "evidence_items.json"
_OUTLINE_STATE_ARTIFACT_KIND = "outline_state.json"
_OUTLINE_VERSIONS_ARTIFACT_KIND = "outline_versions.json"
_EVIDENCE_LEDGER_ARTIFACT_KIND = "evidence_ledger.json"
_SECTION_BANKS_ARTIFACT_KIND = "section_banks.json"
_SELECTED_BANK_ARTIFACT_KIND = "selected_bank.json"
_EVIDENCE_BANK_ARTIFACT_KIND = "evidence_bank.json"
_SOURCE_POLICY_ARTIFACT_KIND = "source_policy.json"
_LINEAGE_ARTIFACT_KIND = "lineage.json"
_VERIFICATION_ARTIFACT_KIND = "verification.json"
_COVERAGE_GAPS_ARTIFACT_KIND = "coverage_gaps.json"
_CORE_FINAL_ARTIFACT_KINDS = ("sources.json", "citations.json", "report.json", "final_report.md")
_PROVENANCE_FINAL_ARTIFACT_KINDS = (
    _EVIDENCE_ITEMS_ARTIFACT_KIND,
    "coverage.json",
    "grounding.json",
    "verifier.json",
)
_FINAL_ARTIFACT_KINDS = _CORE_FINAL_ARTIFACT_KINDS + _PROVENANCE_FINAL_ARTIFACT_KINDS
_ADDITIVE_FINAL_ARTIFACT_KINDS = (
    _SELECTED_BANK_ARTIFACT_KIND,
    _EVIDENCE_BANK_ARTIFACT_KIND,
    _VERIFICATION_ARTIFACT_KIND,
    _COVERAGE_GAPS_ARTIFACT_KIND,
)
_RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS = _FINAL_ARTIFACT_KINDS + _ADDITIVE_FINAL_ARTIFACT_KINDS
_UNRESOLVED_BATCH_BACKED_FINAL_ARTIFACTS_HIDDEN = "unresolved_batch_backed_final_artifacts_hidden"
_DEFAULT_SEARCH_QUERY_FN = None
_RUNTIME_RECONCILE_STALE_SECONDS = 30


def _effort_selective_fetch_limit(effort: str) -> int:
    normalized = (effort or "").strip().lower()
    if normalized == "ultra":
        return 3
    if normalized == "deep":
        return 2
    return 1


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


def _stable_string_list(items: list[str]) -> list[str]:
    return sorted(
        {
            _normalize_whitespace(item).lower()
            for item in items
            if _normalize_whitespace(item)
        }
    )


def _stable_text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _tokenize_keywords(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*", (value or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in _STOPWORDS and not token.isdigit()]


def _query_identifier_terms(value: str) -> list[str]:
    identifiers: list[str] = []
    for raw_term in re.findall(r"[A-Za-z][A-Za-z0-9_]{4,}", str(value or "")):
        normalized_term = raw_term.strip().lower()
        if (
            normalized_term
            and (
                "_" in raw_term
                or re.search(r"[A-Z]", raw_term)
                or len(normalized_term) >= 14
            )
            and normalized_term not in identifiers
        ):
            identifiers.append(normalized_term)
    return identifiers


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
    excerpt, _, _ = _extract_relevant_excerpt_with_span(
        value,
        reference_texts=reference_texts,
        line_limit=line_limit,
        char_limit=char_limit,
        multiline=multiline,
    )
    return excerpt


def _extract_relevant_excerpt_with_span(
    value: str,
    *,
    reference_texts: list[str],
    line_limit: int,
    char_limit: int,
    multiline: bool = False,
) -> tuple[str, int | None, int | None]:
    meaningful_lines: list[tuple[int, str]] = []
    for index, raw_line in enumerate((value or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if _is_noisy_text(line):
            continue
        meaningful_lines.append((index, line))
    if not meaningful_lines:
        fallback = _sanitize_detail_text(value, limit=char_limit) if multiline else _summarize_evidence_text(value, limit=char_limit)
        return fallback, None, None
    keywords = _dedupe_preserve_order(
        [token for text in reference_texts for token in _tokenize_keywords(text)]
    )
    ranked: list[tuple[int, int, int, str]] = []
    for index, line in meaningful_lines:
        overlap = _count_keyword_overlap(line, keywords) if keywords else 0
        ranked.append((overlap, len(line), index, line))
    ranked.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
    max_overlap = ranked[0][0] if ranked else 0
    overlap_threshold = max(1, max_overlap - 1) if max_overlap > 0 else 0
    selected = [item for item in ranked if item[0] >= overlap_threshold and item[0] > 0][:line_limit]
    if not selected:
        selected = ranked[:line_limit]
    selected.sort(key=lambda item: item[2])
    separator = "\n" if multiline else " "
    excerpt = _trim_text(separator.join(item[3] for item in selected), limit=char_limit)
    if not excerpt:
        return excerpt, None, None
    return excerpt, selected[0][2], selected[-1][2]


def _find_excerpt_line_span(source_text: str, excerpt: str) -> tuple[int | None, int | None]:
    raw_lines = list((source_text or "").splitlines())
    excerpt_lines = [line.strip() for line in (excerpt or "").splitlines() if line.strip()]
    if not raw_lines or not excerpt_lines:
        return None, None

    normalized_raw = [_normalize_whitespace(line) for line in raw_lines]
    normalized_excerpt = [_normalize_whitespace(line) for line in excerpt_lines]

    window_size = len(normalized_excerpt)
    for index in range(len(normalized_raw) - window_size + 1):
        window = normalized_raw[index : index + window_size]
        if window == normalized_excerpt:
            return index + 1, index + window_size

    excerpt_text = _normalize_whitespace(excerpt)
    if not excerpt_text:
        return None, None
    for index, line in enumerate(raw_lines, start=1):
        if excerpt_text in _normalize_whitespace(line):
            return index, index
    return None, None


def _extract_markdown_title(value: str) -> str:
    for raw_line in (value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            candidate = _normalize_whitespace(line.lstrip("#").strip())
            if not _is_low_signal_title(candidate):
                return candidate
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


def _is_low_signal_title(value: str) -> bool:
    text = _normalize_whitespace(value)
    if not text:
        return True
    lowered = text.lower()
    if re.fullmatch(r"(?:section|chapter|step|part)?\s*\d+(?:\.\d+)*", lowered):
        return True
    if re.fullmatch(r"[ivxlcdm]+", lowered):
        return True
    if len(text) <= 2 and not re.search(r"[a-z]{2}", lowered):
        return True
    return False


def _count_keyword_overlap(text: str, keywords: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _has_noisy_source_metadata(source: dict[str, Any]) -> bool:
    for key in ("title", "description", "snippet"):
        value = source.get(key)
        if key == "title" and isinstance(value, str) and value.strip() and _is_low_signal_title(value):
            return True
        if isinstance(value, str) and value.strip() and _is_noisy_text(value):
            return True
    return False


def _source_doc_traits(source: dict[str, Any]) -> set[str]:
    url = str(source.get("url", "")).lower()
    domain = str(source.get("domain", "") or "").lower()
    text = " ".join(
        str(source.get(key, "") or "").lower()
        for key in ("title", "description", "snippet")
    )
    combined = f"{url} {domain} {text}"
    traits: set[str] = set()
    if "/apireference/" in url or re.search(r"\bapi(?: reference)?\b", combined):
        traits.add("api_reference")
    if "/userguide/" in url or "user guide" in combined or "developer guide" in combined:
        traits.add("user_guide")
    if "/reference/" in url or re.search(r"\breference\b", combined):
        traits.add("reference")
    if (
        "/prescriptive-guidance/" in url
        or "/patterns/" in url
        or "prescriptive guidance" in combined
        or re.search(r"\bpatterns?\b", combined)
    ):
        traits.add("prescriptive_guidance")
    if "troubleshooting" in combined:
        traits.add("troubleshooting")
    return traits


def _docs_aws_namespace_priority(source: dict[str, Any], reference_texts: list[str] | None = None) -> int:
    url = str(source.get("url", "") or "").lower()
    domain = str(source.get("domain", "") or "").lower()
    if "docs.aws.amazon.com" not in url and domain != "docs.aws.amazon.com":
        return 0
    reference = " ".join(str(value or "") for value in reference_texts or []).lower()
    dms_signals = (
        "aws dms",
        " describereplicationtasks",
        " startreplicationtask",
        " modifyreplicationtask",
        " cdcstartposition",
        " awsdms_txn_state",
        " recoverytimeout",
        " replication task",
    )
    if not any(signal in f" {reference} " for signal in dms_signals):
        return 0
    if "/dms/latest/" in url:
        return 2
    return -2


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


def _normalize_outline_sections_payload(items: list[Any]) -> list[dict[str, Any]]:
    normalized_sections: list[dict[str, Any]] = []
    seen_section_ids: dict[str, int] = {}
    for index, item in enumerate(items or [], start=1):
        if isinstance(item, str):
            item = {"section_id": _slugify(item), "title": item, "goal": item}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or f"Section {index}").strip()
        goal = str(item.get("goal") or title).strip()
        base_section_id = _normalize_whitespace(
            str(item.get("section_id") or _slugify(title) or f"section-{index}")
        )
        count = seen_section_ids.get(base_section_id, 0)
        section_id = base_section_id if count == 0 else f"{base_section_id}-{count + 1}"
        seen_section_ids[base_section_id] = count + 1
        normalized_sections.append(
            {
                "section_id": section_id,
                "title": title,
                "goal": goal,
                "status": _normalize_whitespace(str(item.get("status", "") or "")),
                "coverage_state": dict(item.get("coverage_state") or {})
                if isinstance(item.get("coverage_state"), dict)
                else {},
                "rewrite_reason": _normalize_whitespace(str(item.get("rewrite_reason", "") or "")),
            }
        )
    return normalized_sections


def _make_outline_version(
    sections: list[dict[str, Any]],
    *,
    version_id: str,
    parent_version_id: str = "",
    kind: str = "planned",
    reason_codes: list[str] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    return DeepResearchOutlineVersion(
        version_id=version_id,
        parent_version_id=parent_version_id,
        kind=kind,
        sections=[DeepResearchReportSection.model_validate(section) for section in sections],
        reason_codes=_dedupe_preserve_order(
            [str(code).strip() for code in reason_codes or [] if str(code).strip()]
        ),
        created_at=created_at or utc_now_iso(),
    ).model_dump()


def _normalize_outline_versions(
    raw_outline_versions: Any,
    *,
    latest_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_versions: list[dict[str, Any]] = []
    if isinstance(raw_outline_versions, list):
        for index, item in enumerate(raw_outline_versions, start=1):
            if not isinstance(item, dict):
                continue
            sections = _normalize_outline_sections_payload(list(item.get("sections") or []))
            if not sections:
                continue
            version_id = _normalize_whitespace(str(item.get("version_id") or f"outline-v{index}")) or f"outline-v{index}"
            normalized_versions.append(
                _make_outline_version(
                    sections,
                    version_id=version_id,
                    parent_version_id=_normalize_whitespace(str(item.get("parent_version_id", "") or "")),
                    kind=_normalize_whitespace(str(item.get("kind", "") or "planned")) or "planned",
                    reason_codes=list(item.get("reason_codes") or []),
                    created_at=_normalize_whitespace(str(item.get("created_at", "") or "")),
                )
            )
    if not normalized_versions:
        return [
            _make_outline_version(
                latest_sections,
                version_id="outline-v1",
                kind="planned",
                reason_codes=["initial_outline"],
            )
        ]
    if normalized_versions[-1]["sections"] != latest_sections:
        normalized_versions.append(
            _make_outline_version(
                latest_sections,
                version_id=f"outline-v{len(normalized_versions) + 1}",
                parent_version_id=str(normalized_versions[-1].get("version_id", "")).strip(),
                kind="normalized",
                reason_codes=["normalized_latest_outline"],
            )
        )
    return normalized_versions


def _append_outline_version_if_changed(
    outline_versions: list[dict[str, Any]],
    *,
    sections: list[dict[str, Any]],
    kind: str,
    reason_codes: list[str] | None = None,
    created_at: str = "",
) -> list[dict[str, Any]]:
    normalized_sections = _normalize_outline_sections_payload(sections)
    if not normalized_sections:
        return list(outline_versions)
    versions = list(outline_versions)
    latest_sections = versions[-1]["sections"] if versions else []
    if latest_sections == normalized_sections:
        return versions
    versions.append(
        _make_outline_version(
            normalized_sections,
            version_id=f"outline-v{len(versions) + 1}",
            parent_version_id=str(versions[-1].get("version_id", "")).strip() if versions else "",
            kind=kind,
            reason_codes=reason_codes or [],
            created_at=created_at or utc_now_iso(),
        )
    )
    return versions


def _carry_forward_outline_versions(
    *,
    plan_payload: dict[str, Any],
    carry_forward_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_outline_versions = plan_payload.get("outline_versions")
    if isinstance(raw_outline_versions, list) and raw_outline_versions:
        latest_sections = _normalize_outline_sections_payload(
            list((raw_outline_versions[-1] or {}).get("sections") or [])
        ) or _normalize_outline_sections_payload(list(plan_payload.get("report_outline") or []))
        normalized = _normalize_outline_versions(
            raw_outline_versions,
            latest_sections=latest_sections or _normalize_outline_sections_payload(carry_forward_sections),
        )
        if normalized:
            return normalized
    latest_sections = _normalize_outline_sections_payload(
        list(plan_payload.get("report_outline") or [])
    ) or _normalize_outline_sections_payload(carry_forward_sections)
    if not latest_sections:
        return []
    return _normalize_outline_versions([], latest_sections=latest_sections)


def _compact_continuation(continuation: DeepResearchContinuationState) -> DeepResearchContinuation:
    return DeepResearchContinuation(
        mode=continuation.mode,
        source_job_id=continuation.source_job_id,
        source_job_status=continuation.source_job_status,
        lineage_root_job_id=continuation.lineage_root_job_id,
        parent_job_id=continuation.parent_job_id,
        continuation_identity=continuation.continuation_identity,
        compaction_policy=continuation.compaction_policy,
        compaction_reason_codes=list(continuation.compaction_reason_codes),
        focused_snapshot=dict(continuation.focused_snapshot),
        previous_summary=continuation.previous_summary,
        prior_plan_summary=continuation.prior_plan_summary,
        continuation_goal=continuation.continuation_goal,
        source_count=continuation.source_count,
        checkpoint_key=continuation.checkpoint_key,
        resume_from_checkpoint_key=continuation.resume_from_checkpoint_key,
        replay_from_checkpoint_key=continuation.replay_from_checkpoint_key,
        state_version=continuation.state_version,
        confirmed_claims=list(continuation.confirmed_claims),
        open_questions=list(continuation.open_questions),
        trusted_source_headers=list(continuation.trusted_source_headers),
        carry_forward_constraints=dict(continuation.carry_forward_constraints),
        skipped_unit_ids=list(continuation.skipped_unit_ids),
    )


def _focused_continuation_snapshot(
    *,
    source_job_id: str,
    source_job_status: str,
    lineage_root_job_id: str,
    parent_job_id: str,
    checkpoint_key: str,
    resume_from_checkpoint_key: str,
    replay_from_checkpoint_key: str,
    continuation_goal: str,
    previous_summary: str,
    prior_plan_summary: str,
    confirmed_claims: list[str],
    open_questions: list[str],
    trusted_source_headers: list[str],
    carry_forward_constraints: dict[str, Any],
    skipped_unit_ids: list[str],
    carry_forward_sources: list[dict[str, Any]],
    carry_forward_outline_versions: list[dict[str, Any]],
    carry_forward_sections: list[dict[str, Any]],
    carry_forward_unit_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source_job_id": source_job_id.strip(),
        "source_job_status": source_job_status.strip(),
        "lineage_root_job_id": lineage_root_job_id.strip(),
        "parent_job_id": parent_job_id.strip(),
        "checkpoint_key": checkpoint_key.strip(),
        "resume_from_checkpoint_key": resume_from_checkpoint_key.strip(),
        "replay_from_checkpoint_key": replay_from_checkpoint_key.strip(),
        "continuation_goal": _trim_text(continuation_goal, limit=200),
        "previous_summary": _trim_text(previous_summary, limit=400),
        "prior_plan_summary": _trim_text(prior_plan_summary, limit=400),
        "confirmed_claims": _dedupe_preserve_order(confirmed_claims),
        "open_questions": _dedupe_preserve_order(open_questions),
        "trusted_source_headers": _dedupe_preserve_order(trusted_source_headers),
        "carry_forward_constraints": dict(carry_forward_constraints or {}),
        "skipped_unit_ids": _dedupe_preserve_order(skipped_unit_ids),
        "source_ids": [
            str(item.get("source_id", "")).strip()
            for item in carry_forward_sources
            if str(item.get("source_id", "")).strip()
        ],
        "sources": [
            {
                "source_id": str(item.get("source_id", "")).strip(),
                "url": str(item.get("url", "")).strip(),
                "title": _normalize_whitespace(str(item.get("title", ""))),
                "source_type": _normalize_whitespace(str(item.get("source_type", ""))),
            }
            for item in carry_forward_sources
            if str(item.get("source_id", "")).strip() and str(item.get("url", "")).strip()
        ],
        "outline_versions": [
            {
                "version_id": str(item.get("version_id", "")).strip(),
                "kind": str(item.get("kind", "")).strip(),
                "reason_codes": [str(code).strip() for code in item.get("reason_codes", []) or [] if str(code).strip()],
                "section_ids": [
                    str(section.get("section_id", "")).strip()
                    for section in item.get("sections", []) or []
                    if isinstance(section, dict) and str(section.get("section_id", "")).strip()
                ],
            }
            for item in carry_forward_outline_versions
            if isinstance(item, dict) and str(item.get("version_id", "")).strip()
        ],
        "sections": [
            {
                "section_id": str(item.get("section_id", "")).strip(),
                "title": _normalize_whitespace(str(item.get("title", ""))),
                "summary": _summarize_evidence_text(str(item.get("summary", "")), limit=180),
                "citations": [
                    str(citation).strip()
                    for citation in item.get("citations", [])
                    if str(citation).strip()
                ],
            }
            for item in carry_forward_sections
            if str(item.get("section_id", "")).strip() and _normalize_whitespace(str(item.get("title", "")))
        ],
        "unit_results": {
            str(unit_id): {
                "summary": _summarize_evidence_text(str(result.get("summary") or result.get("detail") or ""), limit=180),
                "source_ids": [
                    str(source_id).strip()
                    for source_id in result.get("source_ids", []) or result.get("citations", []) or []
                    if str(source_id).strip()
                ],
            }
            for unit_id, result in sorted(carry_forward_unit_results.items())
            if _summarize_evidence_text(str(result.get("summary") or result.get("detail") or ""), limit=180)
        },
    }


def _append_reason_code(reason_codes: list[str], value: str) -> None:
    normalized = _normalize_whitespace(value)
    if normalized and normalized not in reason_codes:
        reason_codes.append(normalized)


def _continuation_has_material_carry_forward_state(continuation: DeepResearchContinuationState) -> bool:
    return any(
        (
            continuation.source_count > 0,
            bool(continuation.carry_forward_sources),
            bool(continuation.carry_forward_outline_versions),
            bool(continuation.carry_forward_evidence),
            bool(continuation.carry_forward_sections),
            bool(continuation.carry_forward_unit_results),
        )
    )


def _checkpoint_state_has_material_carry_forward(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    return any(
        (
            bool(state.get("completed_unit_ids")),
            bool(state.get("sources")),
            bool(state.get("outline_versions")),
            bool(state.get("evidence_items")),
            bool(state.get("sections")),
            bool(state.get("unit_results")),
        )
    )


def _continuation_compaction_reason_codes(
    *,
    source_job: DeepResearchJob,
    use_final_bundle: bool,
    bundle_candidate_used: bool,
    current_sources_used: bool,
    citations_registry_fallback_used: bool,
    checkpoint_sources_used: bool,
    latest_state_sources_used: bool,
    checkpoint_sections_used: bool,
    latest_state_sections_used: bool,
    checkpoint_unit_results_used: bool,
    latest_state_unit_results_used: bool,
    checkpoint_evidence_used: bool,
    latest_state_evidence_used: bool,
    reconstructed_evidence_used: bool,
    filtered_to_focused_sources: bool,
    confirmed_claims_filtered: bool,
    open_questions_filtered: bool,
) -> list[str]:
    reason_codes: list[str] = []
    _append_reason_code(reason_codes, "focused_snapshot_v1")
    _append_reason_code(reason_codes, f"source_job_status:{source_job.status}")
    if use_final_bundle:
        _append_reason_code(reason_codes, "resolved_final_bundle")
    elif bundle_candidate_used:
        _append_reason_code(reason_codes, "latest_complete_batch_candidate")
    else:
        _append_reason_code(reason_codes, "current_artifacts")
    if current_sources_used:
        _append_reason_code(reason_codes, "sources_from_current_artifacts")
    if citations_registry_fallback_used:
        _append_reason_code(reason_codes, "sources_from_citations_registry")
    if checkpoint_sources_used:
        _append_reason_code(reason_codes, "sources_from_checkpoint_state")
    if latest_state_sources_used:
        _append_reason_code(reason_codes, "sources_from_latest_checkpoint_snapshot")
    if checkpoint_sections_used:
        _append_reason_code(reason_codes, "sections_from_checkpoint_state")
    if latest_state_sections_used:
        _append_reason_code(reason_codes, "sections_from_latest_checkpoint_snapshot")
    if checkpoint_unit_results_used:
        _append_reason_code(reason_codes, "unit_results_from_checkpoint_state")
    if latest_state_unit_results_used:
        _append_reason_code(reason_codes, "unit_results_from_latest_checkpoint_snapshot")
    if checkpoint_evidence_used:
        _append_reason_code(reason_codes, "evidence_from_checkpoint_state")
    if latest_state_evidence_used:
        _append_reason_code(reason_codes, "evidence_from_latest_checkpoint_snapshot")
    if reconstructed_evidence_used:
        _append_reason_code(reason_codes, "evidence_reconstructed_from_sections")
    if filtered_to_focused_sources:
        _append_reason_code(reason_codes, "focused_source_subset")
    if confirmed_claims_filtered:
        _append_reason_code(reason_codes, "confirmed_claims_filtered")
    if open_questions_filtered:
        _append_reason_code(reason_codes, "open_questions_filtered")
    return reason_codes


def _continuation_identity_from_snapshot(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_canonicalize_identity_value(snapshot), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _canonicalize_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonicalize_identity_value(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        canonical_items = [_canonicalize_identity_value(item) for item in value]
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in canonical_items):
            return sorted(canonical_items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        if all(isinstance(item, dict) for item in canonical_items):
            return sorted(
                canonical_items,
                key=lambda item: (
                    str(item.get("source_id", "")),
                    str(item.get("section_id", "")),
                    str(item.get("evidence_id", "")),
                    str(item.get("unit_id", "")),
                    str(item.get("url", "")),
                    str(item.get("title", "")),
                    json.dumps(item, ensure_ascii=False, sort_keys=True),
                ),
            )
        return canonical_items
    if isinstance(value, str):
        return _normalize_whitespace(value)
    return value


def _continuation_capsule(continuation: DeepResearchContinuationState) -> dict[str, Any]:
    return {
        "mode": continuation.mode,
        "source_job_id": continuation.source_job_id,
        "source_job_status": continuation.source_job_status,
        "lineage_root_job_id": continuation.lineage_root_job_id,
        "parent_job_id": continuation.parent_job_id,
        "continuation_identity": continuation.continuation_identity,
        "checkpoint_key": continuation.checkpoint_key,
        "resume_from_checkpoint_key": continuation.resume_from_checkpoint_key,
        "replay_from_checkpoint_key": continuation.replay_from_checkpoint_key,
        "continuation_goal": continuation.continuation_goal,
        "source_count": continuation.source_count,
        "compaction_policy": continuation.compaction_policy,
        "compaction_reason_codes": list(continuation.compaction_reason_codes),
        "confirmed_claims": list(continuation.confirmed_claims[:4]),
        "open_questions": list(continuation.open_questions[:4]),
        "trusted_source_headers": list(continuation.trusted_source_headers[:4]),
        "carry_forward_constraints": dict(continuation.carry_forward_constraints),
        "skipped_unit_ids": list(continuation.skipped_unit_ids[:10]),
        "carry_forward_source_ids": [
            str(item.get("source_id", "")).strip()
            for item in continuation.carry_forward_sources[:5]
            if str(item.get("source_id", "")).strip()
        ],
        "carry_forward_section_ids": [
            str(item.get("section_id", "")).strip()
            for item in continuation.carry_forward_sections[:5]
            if str(item.get("section_id", "")).strip()
        ],
        "carry_forward_outline_version_ids": [
            str(item.get("version_id", "")).strip()
            for item in continuation.carry_forward_outline_versions[:3]
            if str(item.get("version_id", "")).strip()
        ],
    }


def _hydrate_continuation_snapshot(continuation: DeepResearchContinuationState) -> DeepResearchContinuationState:
    if continuation.mode != "continue":
        return continuation
    snapshot = dict(continuation.focused_snapshot or {})
    if not snapshot:
        snapshot = _focused_continuation_snapshot(
            source_job_id=continuation.source_job_id,
            source_job_status=continuation.source_job_status,
            lineage_root_job_id=continuation.lineage_root_job_id,
            parent_job_id=continuation.parent_job_id,
            checkpoint_key=continuation.checkpoint_key,
            resume_from_checkpoint_key=continuation.resume_from_checkpoint_key,
            replay_from_checkpoint_key=continuation.replay_from_checkpoint_key,
            continuation_goal=continuation.continuation_goal,
            previous_summary=continuation.previous_summary,
            prior_plan_summary=continuation.prior_plan_summary,
            confirmed_claims=list(continuation.confirmed_claims),
            open_questions=list(continuation.open_questions),
            trusted_source_headers=list(continuation.trusted_source_headers),
            carry_forward_constraints=dict(continuation.carry_forward_constraints),
            skipped_unit_ids=list(continuation.skipped_unit_ids),
            carry_forward_sources=list(continuation.carry_forward_sources),
            carry_forward_outline_versions=list(continuation.carry_forward_outline_versions),
            carry_forward_sections=list(continuation.carry_forward_sections),
            carry_forward_unit_results=dict(continuation.carry_forward_unit_results),
        )
    identity = continuation.continuation_identity or _continuation_identity_from_snapshot(snapshot)
    if snapshot == continuation.focused_snapshot and identity == continuation.continuation_identity:
        return continuation
    return continuation.model_copy(
        update={
            "focused_snapshot": snapshot,
            "continuation_identity": identity,
        }
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


def _stable_text_equivalent(left: str, right: str) -> bool:
    left_key = _stable_text_key(left)
    right_key = _stable_text_key(right)
    return bool(left_key and right_key and left_key == right_key)


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
        if key == "title" and _is_low_signal_title(str(current)) and not _is_low_signal_title(str(value)):
            merged[key] = value
            continue
        if key == "title" and len(str(value)) > len(str(current)) and not _is_low_signal_title(str(value)):
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
    guessed_title = _guess_title_from_url(str(merged.get("url") or ""))
    if (not merged.get("title") or _is_low_signal_title(str(merged.get("title") or ""))) and guessed_title:
        merged["title"] = guessed_title
    return merged


def _enrich_source_from_fetched_text(source: dict[str, Any], fetched_text: str) -> dict[str, Any]:
    enriched = dict(source)
    title = _extract_markdown_title(fetched_text)
    summary = _summarize_evidence_text(fetched_text, limit=280)
    if title and (not enriched.get("title") or _is_low_signal_title(str(enriched.get("title") or ""))):
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


def _build_report_summary(plan: DeepResearchPlan, sections: list[dict[str, Any]], *, confidence: str = "") -> str:
    def _strip_confidence_prefix(value: str) -> str:
        text = _normalize_whitespace(value)
        lowered = text.lower()
        for prefix in ("high confidence:", "medium confidence:", "low confidence:"):
            if lowered.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    def _dedupe_summary_key(value: str) -> str:
        text = _strip_confidence_prefix(value)
        text = re.sub(r"^(overall,\s*)+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^(key point:\s*)+", "", text, flags=re.IGNORECASE)
        text = _strip_confidence_prefix(text)
        return _stable_text_key(text)

    concrete_sections = [
        section
        for section in sections
        if not is_summary_section_title(str(section.get("title", "")))
        and not is_key_findings_section_title(str(section.get("title", "")))
        and not is_open_questions_section_title(str(section.get("title", "")))
    ]
    source_sections = concrete_sections or sections
    section_summaries: list[str] = []
    claim_texts: list[str] = []
    for section in source_sections:
        section_summary = _summarize_evidence_text(str(section.get("summary", "")), limit=220)
        section_summary = _strip_confidence_prefix(section_summary)
        if section_summary and not _is_noisy_text(section_summary) and section_summary not in section_summaries:
            section_summaries.append(section_summary)
        for claim in section.get("claims", []):
            text = _summarize_evidence_text(str(claim.get("text", "")), limit=180)
            if not text or _is_noisy_text(text):
                continue
            if text not in claim_texts:
                claim_texts.append(text)
    deduped_summary_chunks: list[str] = []
    seen_summary_keys: set[str] = set()
    for chunk in section_summaries:
        normalized_chunk = _normalize_whitespace(chunk)
        dedupe_key = _dedupe_summary_key(normalized_chunk)
        if dedupe_key and dedupe_key in seen_summary_keys:
            continue
        if dedupe_key and any(
            dedupe_key in existing_key or existing_key in dedupe_key
            for existing_key in seen_summary_keys
        ):
            continue
        if any(
            normalized_chunk in existing
            or existing in normalized_chunk
            for existing in deduped_summary_chunks
        ):
            continue
        deduped_summary_chunks.append(normalized_chunk)
        if dedupe_key:
            seen_summary_keys.add(dedupe_key)
    summary_chunks = deduped_summary_chunks[:2] or claim_texts[:2]
    if not summary_chunks:
        return _trim_text(plan.brief.objective, limit=220)
    deduped_sentences: list[str] = []
    seen_sentence_keys: set[str] = set()
    for chunk in summary_chunks:
        for sentence in re.split(r"(?<=[.!?])\s+", chunk):
            normalized_sentence = _normalize_whitespace(sentence)
            if not normalized_sentence:
                continue
            sentence_key = _dedupe_summary_key(normalized_sentence)
            if sentence_key and sentence_key in seen_sentence_keys:
                continue
            deduped_sentences.append(normalized_sentence)
            if sentence_key:
                seen_sentence_keys.add(sentence_key)
    summary = " ".join(deduped_sentences or summary_chunks)
    section_confidences = {str(section.get("confidence", "")) for section in sections if section.get("confidence")}
    confidence_prefix = ""
    explicit_confidence = str(confidence or "").strip().lower()
    if explicit_confidence == "high":
        confidence_prefix = "High confidence: "
    elif explicit_confidence == "medium":
        confidence_prefix = "Medium confidence: "
    elif explicit_confidence == "low":
        confidence_prefix = "Low confidence: "
    elif "high" in section_confidences:
        confidence_prefix = "High confidence: "
    elif "medium" in section_confidences:
        confidence_prefix = "Medium confidence: "
    elif "low" in section_confidences:
        confidence_prefix = "Low confidence: "
    if len(summary_chunks) == 1 and not section_summaries:
        summary = f"{plan.brief.objective}: {summary}"
    return _trim_text(f"{confidence_prefix}{summary}".strip(), limit=320)


def _strip_summary_scaffolding(value: str) -> str:
    text = _normalize_whitespace(value)
    lowered = text.lower()
    prefixes = (
        "high confidence:",
        "medium confidence:",
        "low confidence:",
        "key point:",
        "key finding:",
        "overall,",
    )
    changed = True
    while changed and text:
        changed = False
        lowered = text.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                text = _normalize_whitespace(text[len(prefix) :].lstrip(" ,:"))
                changed = True
                break
    return text


def _synthesize_rollup_claim_text(section_title: str, summary_text: str) -> str:
    normalized_summary = _strip_summary_scaffolding(
        _summarize_evidence_text(summary_text, limit=_MAX_CLAIM_LENGTH)
    )
    if not normalized_summary:
        return ""
    normalized_summary = normalized_summary.rstrip(".")
    title = _normalize_whitespace(section_title)
    if not title:
        return f"{normalized_summary}."
    if is_key_findings_section_title(title):
        return _trim_text(f"Key finding: {normalized_summary}.", limit=_MAX_CLAIM_LENGTH)
    if is_summary_section_title(title):
        return _trim_text(f"Overall, {normalized_summary}.", limit=_MAX_CLAIM_LENGTH)
    return _trim_text(f"{title}: {normalized_summary}.", limit=_MAX_CLAIM_LENGTH)


def _report_artifact_contract_error(job: DeepResearchJob) -> dict[str, str]:
    errors: dict[str, str] = {}
    if job.status == "completed" or _job_prefers_resolved_final_bundle(job):
        for kind in _FINAL_ARTIFACT_KINDS:
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
    if kind == _EVIDENCE_ITEMS_ARTIFACT_KIND:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return "invalid_shape"
        return None
    if kind in {_SELECTED_BANK_ARTIFACT_KIND, _EVIDENCE_BANK_ARTIFACT_KIND}:
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return "invalid_shape"
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
    if kind in {"coverage.json", "grounding.json", "verifier.json"}:
        if not isinstance(value, dict):
            return "invalid_shape"
        return None
    if kind == _VERIFICATION_ARTIFACT_KIND:
        if not isinstance(value, dict):
            return "invalid_shape"
        packet_to_prose = value.get("packet_to_prose_fidelity")
        if packet_to_prose is not None and not isinstance(packet_to_prose, dict):
            return "invalid_shape"
        return None
    if kind == _COVERAGE_GAPS_ARTIFACT_KIND:
        if not isinstance(value, dict):
            return "invalid_shape"
        for key in ("unanswered_sections", "uncovered_sub_questions", "hard_uncovered_targets", "gaps"):
            if not isinstance(value.get(key, []), list):
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


def _batch_bundle_candidate_from_dir(batch_dir: Path) -> dict[str, Any]:
    return {
        "batch_id": batch_dir.name,
        "paths": {
            kind: batch_dir / kind
            for kind in _FINAL_ARTIFACT_KINDS
            if (batch_dir / kind).exists()
        },
    }


def _batch_bundle_candidates(store: DeepResearchStore, job_id: str) -> list[dict[str, Any]]:
    batches_dir = store.artifacts_dir / job_id / "batches"
    if not batches_dir.exists():
        return []
    candidates = sorted(
        (path for path in batches_dir.iterdir() if path.is_dir()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    return [_batch_bundle_candidate_from_dir(path) for path in candidates]


def _latest_batch_bundle_candidate(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    candidates = _batch_bundle_candidates(store, job_id)
    return candidates[0] if candidates else None


def _provenance_sidecars_match_report(
    *,
    report_value: dict[str, Any] | None,
    coverage_value: dict[str, Any] | None,
    grounding_value: dict[str, Any] | None,
    verifier_value: dict[str, Any] | None,
) -> bool:
    allowed_coverage_keys = {
        "query",
        "must_cover",
        "coverage_checklist",
        "coverage_state",
        "planned_section_ids",
        "answered_section_ids",
        "unanswered_sections",
        "planned_sub_question_ids",
        "covered_sub_question_ids",
        "uncovered_sub_questions",
        "sub_questions",
        "section_coverage",
        "hard_coverage_targets",
        "hard_uncovered_targets",
        "hard_coverage_gate_passed",
        "coverage_gate_passed",
    }
    allowed_grounding_keys = {
        "total_claims",
        "grounded_claims",
        "ungrounded_claims",
        "single_source_claims",
        "low_confidence_claims",
        "missing_evidence_binding_claims",
        "total_evidence_bindings",
        "source_backed_binding_count",
        "search_only_binding_count",
        "null_span_binding_count",
        "grounded_claims_without_source_backed_binding",
        "sections",
        "sources",
    }
    if not isinstance(report_value, dict):
        return False
    runtime_payload = report_value.get("runtime")
    report_coverage = report_value.get("coverage")
    if not isinstance(runtime_payload, dict) or not isinstance(report_coverage, dict):
        return False
    report_grounding = runtime_payload.get("grounding")
    report_verifier = runtime_payload.get("verifier")
    if not isinstance(report_grounding, dict) or not isinstance(report_verifier, dict):
        return False
    if not isinstance(coverage_value, dict) or not isinstance(grounding_value, dict) or not isinstance(verifier_value, dict):
        return False
    if set(coverage_value) - allowed_coverage_keys:
        return False
    if set(grounding_value) - allowed_grounding_keys:
        return False
    for key, value in report_coverage.items():
        if coverage_value.get(key) != value:
            return False
    for key, value in report_grounding.items():
        if grounding_value.get(key) != value:
            return False
    if verifier_value != report_verifier:
        return False
    return True


def _selected_bank_matches_bundle(
    *,
    selected_bank_value: list[dict[str, Any]] | None,
    report_value: dict[str, Any] | None,
    evidence_items_value: list[dict[str, Any]] | None,
) -> bool:
    if selected_bank_value is None:
        return True
    if not isinstance(report_value, dict) or not isinstance(evidence_items_value, list):
        return False
    sections = report_value.get("sections")
    if not isinstance(sections, list):
        return False
    section_ids = {
        str(section.get("section_id", "")).strip()
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    claim_ids_by_section = {
        str(section.get("section_id", "")).strip(): {
            str(claim.get("claim_id", "")).strip()
            for claim in section.get("claims", []) or []
            if isinstance(claim, dict) and str(claim.get("claim_id", "")).strip()
        }
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    evidence_ids = {
        str(item.get("evidence_id", "")).strip()
        for item in evidence_items_value
        if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
    }
    for bank in selected_bank_value:
        if not isinstance(bank, dict):
            return False
        section_id = str(bank.get("section_id", "")).strip()
        if not section_id or section_id not in section_ids:
            return False
        for field in ("selected_evidence_ids", "candidate_evidence_ids", "rejected_evidence_ids"):
            values = [
                str(item).strip()
                for item in bank.get(field, []) or []
                if str(item).strip()
            ]
            if any(value not in evidence_ids for value in values):
                return False
        for row in bank.get("selected_rows", []) or []:
            if not isinstance(row, dict):
                return False
            evidence_id = str(row.get("evidence_id", "")).strip()
            if evidence_id and evidence_id not in evidence_ids:
                return False
            selected_section_id = str(row.get("selected_section_id", "")).strip()
            if selected_section_id and selected_section_id != section_id:
                return False
            claim_ids = [
                str(claim_id).strip()
                for claim_id in row.get("claim_ids", []) or []
                if str(claim_id).strip()
            ]
            if any(claim_id not in claim_ids_by_section.get(section_id, set()) for claim_id in claim_ids):
                return False
    return True


def _evidence_bank_matches_bundle(
    *,
    evidence_bank_value: list[dict[str, Any]] | None,
    report_value: dict[str, Any] | None,
    evidence_items_value: list[dict[str, Any]] | None,
    citations_value: dict[str, Any] | None,
) -> bool:
    if evidence_bank_value is None:
        return True
    if not isinstance(report_value, dict) or not isinstance(evidence_items_value, list) or not isinstance(citations_value, dict):
        return False
    sections = report_value.get("sections")
    source_registry = citations_value.get("source_registry")
    if not isinstance(sections, list) or not isinstance(source_registry, dict):
        return False
    section_ids = {
        str(section.get("section_id", "")).strip()
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    claim_ids = {
        str(claim.get("claim_id", "")).strip()
        for section in sections
        if isinstance(section, dict)
        for claim in section.get("claims", []) or []
        if isinstance(claim, dict) and str(claim.get("claim_id", "")).strip()
    }
    evidence_ids = {
        str(item.get("evidence_id", "")).strip()
        for item in evidence_items_value
        if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
    }
    source_ids = {str(source_id).strip() for source_id in source_registry if str(source_id).strip()}
    for entry in evidence_bank_value:
        if not isinstance(entry, dict):
            return False
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if evidence_id and evidence_id not in evidence_ids:
            return False
        if any(
            str(section_id).strip() not in section_ids
            for section_id in entry.get("used_by_section_ids", []) or []
            if str(section_id).strip()
        ):
            return False
        if any(
            str(claim_id).strip() not in claim_ids
            for claim_id in entry.get("used_by_claim_ids", []) or []
            if str(claim_id).strip()
        ):
            return False
        if any(
            str(source_id).strip() not in source_ids
            for source_id in entry.get("source_ids", []) or []
            if str(source_id).strip()
        ):
            return False
    return True


def _verification_matches_report(
    *,
    verification_value: dict[str, Any] | None,
    report_value: dict[str, Any] | None,
) -> bool:
    if verification_value is None:
        return True
    if not isinstance(report_value, dict):
        return False
    runtime_payload = report_value.get("runtime")
    if not isinstance(runtime_payload, dict):
        return False
    report_verification = runtime_payload.get("verification")
    if not isinstance(report_verification, dict):
        return False
    return all(verification_value.get(key) == value for key, value in report_verification.items())


def _coverage_gaps_matches_report(
    *,
    coverage_gaps_value: dict[str, Any] | None,
    report_value: dict[str, Any] | None,
) -> bool:
    if coverage_gaps_value is None:
        return True
    if not isinstance(report_value, dict):
        return False
    report_coverage = report_value.get("coverage")
    if not isinstance(report_coverage, dict):
        return False
    expected_payload = _coverage_gaps_payload(
        query=str(coverage_gaps_value.get("query", "") or report_value.get("query", "") or ""),
        coverage=report_coverage,
    )
    return coverage_gaps_value == expected_payload


def _artifact_bundle_is_usable(bundle: dict[str, Any] | None) -> bool:
    if bundle is None:
        return False
    paths = bundle.get("paths") or {}
    report_value: dict[str, Any] | None = None
    coverage_value: dict[str, Any] | None = None
    grounding_value: dict[str, Any] | None = None
    verifier_value: dict[str, Any] | None = None
    sources_value: list[dict[str, Any]] | None = None
    citations_value: dict[str, Any] | None = None
    evidence_items_value: list[dict[str, Any]] | None = None
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
            elif kind == "sources.json" and isinstance(value, list):
                sources_value = value
            elif kind == "citations.json":
                normalized_citations = _normalize_citations_payload(value)
                if _validate_json_artifact_shape(kind, normalized_citations) is not None:
                    return False
                citations_value = normalized_citations
            elif kind == _EVIDENCE_ITEMS_ARTIFACT_KIND and isinstance(value, list):
                evidence_items_value = value
            elif kind == "coverage.json" and isinstance(value, dict):
                coverage_value = value
            elif kind == "grounding.json" and isinstance(value, dict):
                grounding_value = value
            elif kind == "verifier.json" and isinstance(value, dict):
                verifier_value = value
        elif kind == "final_report.md":
            if not _final_report_text_is_meaningful(text, report_value=report_value):
                return False
    selected_bank_value: list[dict[str, Any]] | None = None
    evidence_bank_value: list[dict[str, Any]] | None = None
    verification_value: dict[str, Any] | None = None
    coverage_gaps_value: dict[str, Any] | None = None
    for kind in _ADDITIVE_FINAL_ARTIFACT_KINDS:
        text = _read_batch_artifact_text(bundle, kind)
        if text is None:
            continue
        value, error = _safe_load_json_artifact(text)
        if error is not None or _validate_json_artifact_shape(kind, value) is not None:
            return False
        if kind == _SELECTED_BANK_ARTIFACT_KIND and isinstance(value, list):
            selected_bank_value = value
        elif kind == _EVIDENCE_BANK_ARTIFACT_KIND and isinstance(value, list):
            evidence_bank_value = value
        elif kind == _VERIFICATION_ARTIFACT_KIND and isinstance(value, dict):
            verification_value = value
        elif kind == _COVERAGE_GAPS_ARTIFACT_KIND and isinstance(value, dict):
            coverage_gaps_value = value
    if not _provenance_sidecars_match_report(
        report_value=report_value,
        coverage_value=coverage_value,
        grounding_value=grounding_value,
        verifier_value=verifier_value,
    ):
        return False
    if _validate_provenance_bundle(
        report_value=report_value,
        sources_value=sources_value,
        citations_value=citations_value,
        evidence_items_value=evidence_items_value,
    ):
        return False
    if not _selected_bank_matches_bundle(
        selected_bank_value=selected_bank_value,
        report_value=report_value,
        evidence_items_value=evidence_items_value,
    ):
        return False
    if not _evidence_bank_matches_bundle(
        evidence_bank_value=evidence_bank_value,
        report_value=report_value,
        evidence_items_value=evidence_items_value,
        citations_value=citations_value,
    ):
        return False
    if not _verification_matches_report(
        verification_value=verification_value,
        report_value=report_value,
    ):
        return False
    if not _coverage_gaps_matches_report(
        coverage_gaps_value=coverage_gaps_value,
        report_value=report_value,
    ):
        return False
    return True


def _read_batch_artifact_text(bundle: dict[str, Any] | None, kind: str) -> str | None:
    if bundle is None:
        return None
    paths = bundle.get("paths") or {}
    direct_path = paths.get(kind)
    if direct_path is not None:
        return _read_text_if_exists(direct_path)
    anchor_path = next(iter(paths.values()), None)
    if anchor_path is None:
        return None
    return _read_text_if_exists(anchor_path.parent / kind)


def _complete_batch_bundles(store: DeepResearchStore, job_id: str) -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    for bundle in _batch_bundle_candidates(store, job_id):
        paths = bundle.get("paths") or {}
        if all(paths.get(kind) is not None and _read_text_if_exists(paths[kind]) is not None for kind in _FINAL_ARTIFACT_KINDS):
            bundles.append(bundle)
    return bundles


def _latest_complete_batch_bundle(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    candidates = _complete_batch_bundles(store, job_id)
    return candidates[0] if candidates else None


def _resolve_final_artifact_bundle(store: DeepResearchStore, job_id: str) -> dict[str, Any] | None:
    current_bundle = _current_final_artifact_bundle(store, job_id)
    if _artifact_bundle_is_usable(current_bundle):
        return current_bundle
    for candidate in _complete_batch_bundles(store, job_id):
        if current_bundle is not None and candidate.get("batch_id") == current_bundle.get("batch_id"):
            continue
        if _artifact_bundle_is_usable(candidate):
            return candidate
    return current_bundle if _artifact_bundle_is_usable(current_bundle) else None


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


def _artifact_surface_context(
    store: DeepResearchStore,
    job_id: str,
    *,
    job: DeepResearchJob | None = None,
) -> tuple[dict[str, Any] | None, bool, str]:
    current_job = job or store.get_job(job_id)
    final_bundle = _resolve_final_artifact_bundle(store, job_id) if _job_prefers_resolved_final_bundle(current_job) else None
    unresolved_batch_backed = bool(
        current_job.status != "completed"
        and _job_prefers_resolved_final_bundle(current_job)
        and final_bundle is None
        and any(_current_artifact_is_batch_backed(store, job_id, kind) for kind in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS)
    )
    visibility_reason = _UNRESOLVED_BATCH_BACKED_FINAL_ARTIFACTS_HIDDEN if unresolved_batch_backed else ""
    return final_bundle, unresolved_batch_backed, visibility_reason


def _artifact_hidden_by_visibility_reason(kind: str, visibility_reason: str) -> bool:
    return bool(visibility_reason) and kind in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS


def _required_artifact_error_code(kind: str, visibility_reason: str) -> str:
    if bool(visibility_reason) and kind in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS:
        return visibility_reason
    return "missing_required_artifact"


def _artifact_bundle_identity(store: DeepResearchStore, job_id: str) -> str:
    bundle = _resolve_final_artifact_bundle(store, job_id)
    if _artifact_bundle_is_usable(bundle):
        return str(bundle.get("batch_id", "") or "")
    return ""


def _attempt_id(attempt_count: int | None) -> str:
    try:
        normalized = int(attempt_count or 0)
    except (TypeError, ValueError):
        normalized = 0
    return f"attempt-{normalized}" if normalized > 0 else ""


def _continuation_source_is_recoverable(
    store: DeepResearchStore,
    source_job: DeepResearchJob,
    continuation: DeepResearchContinuationState,
) -> bool:
    final_bundle = _resolve_final_artifact_bundle(store, source_job.job_id)
    if _artifact_bundle_is_usable(final_bundle):
        return True
    if _continuation_has_material_carry_forward_state(continuation):
        return True
    for checkpoint in store.list_checkpoints(source_job.job_id):
        if _checkpoint_kind(checkpoint.checkpoint_key) != "research_unit":
            continue
        if _checkpoint_state_has_material_carry_forward(checkpoint.state or {}):
            return True
    if source_job.status == "completed":
        if any(
            (
                _normalize_whitespace(continuation.previous_summary),
                _normalize_whitespace(continuation.prior_plan_summary),
            )
        ):
            if store.read_artifact_text(source_job.job_id, "report.json"):
                return True
            if store.read_artifact_text(source_job.job_id, "plan.json"):
                return True
            if store.list_checkpoints(source_job.job_id):
                return True
    return False


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
    bundle_candidate: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current_artifacts = {artifact.kind: artifact for artifact in store.list_artifacts(job_id)}
    payloads: list[dict[str, Any]] = []
    ordered_kinds = [
        "plan.json",
        "planner_trace.json",
        "continuation.json",
        "continuation_capsule.json",
        _LINEAGE_ARTIFACT_KIND,
        _SOURCE_POLICY_ARTIFACT_KIND,
        _OUTLINE_VERSIONS_ARTIFACT_KIND,
        _OUTLINE_STATE_ARTIFACT_KIND,
        _EVIDENCE_LEDGER_ARTIFACT_KIND,
        _SECTION_BANKS_ARTIFACT_KIND,
        _SELECTED_BANK_ARTIFACT_KIND,
        "partial_report.md",
        *_RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS,
    ]
    for kind in ordered_kinds:
        artifact = current_artifacts.get(kind)
        candidate_bundle = final_bundle or bundle_candidate
        bundle_text = _read_batch_artifact_text(candidate_bundle, kind) if candidate_bundle is not None else None
        if candidate_bundle is not None and bundle_text is not None:
            anchor_path = next(iter(candidate_bundle["paths"].values()))
            path = anchor_path.parent / kind
            payloads.append(
                {
                    "job_id": job_id,
                    "kind": kind,
                    "path": str(path.relative_to(store.root_dir)),
                    "content_type": artifact.content_type if artifact is not None else _artifact_content_type(kind),
                    "created_at": artifact.created_at if artifact is not None else "",
                    "updated_at": artifact.updated_at if artifact is not None else "",
                    "metadata": _artifact_metadata(
                        bundle_text,
                        batch_id=candidate_bundle["batch_id"],
                    ),
                }
            )
            continue
        if candidate_bundle is not None and kind in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS:
            continue
        if artifact is not None:
            payloads.append(artifact.model_dump())
    return payloads


def _current_artifact_is_batch_backed(store: DeepResearchStore, job_id: str, kind: str) -> bool:
    current_artifacts = {artifact.kind: artifact for artifact in store.list_artifacts(job_id)}
    artifact = current_artifacts.get(kind)
    if artifact is None:
        return False
    return f"/{job_id}/batches/" in artifact.path or artifact.path.startswith(f"artifacts/{job_id}/batches/")


def _job_runtime_diagnostics(
    store: DeepResearchStore,
    job_id: str,
    *,
    final_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan_text = store.read_artifact_text(job_id, "plan.json")
    report_text = (
        _read_text_if_exists(final_bundle["paths"]["report.json"])
        if final_bundle is not None and "report.json" in final_bundle.get("paths", {})
        else store.read_artifact_text(job_id, "report.json")
    )
    diagnostics = {
        "planner_fallback_used": False,
        "runtime_warnings": [],
        "constraint_violations": [],
    }
    plan_value, _ = _safe_load_json_artifact(plan_text)
    if isinstance(plan_value, dict):
        planner_metadata = plan_value.get("planner_metadata")
        if isinstance(planner_metadata, dict):
            diagnostics["planner_fallback_used"] = bool(planner_metadata.get("used_fallback"))
    report_value, _ = _safe_load_json_artifact(report_text)
    if isinstance(report_value, dict):
        runtime_payload = report_value.get("runtime")
        if isinstance(runtime_payload, dict):
            diagnostics["runtime_warnings"] = list(runtime_payload.get("warnings") or [])
            diagnostics["constraint_violations"] = list(runtime_payload.get("constraint_violations") or [])
    return diagnostics


def _operator_summary_payload(
    job: DeepResearchJob,
    *,
    diagnostics: dict[str, Any],
    final_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_checkpoint = job.current_checkpoint
    return {
        "job_id": job.job_id,
        "status": job.status,
        "phase": job.phase,
        "attempt_count": job.attempt_count,
        "attempt_id": _attempt_id(job.attempt_count),
        "current_checkpoint": current_checkpoint,
        "current_checkpoint_kind": _checkpoint_kind(current_checkpoint),
        "current_checkpoint_seq": 0,
        "cancel_requested": job.cancel_requested,
        "continued_from_job_id": job.continued_from_job_id,
        "resolved_artifact_batch_id": final_bundle["batch_id"] if final_bundle is not None else "",
        "planner_fallback_used": diagnostics["planner_fallback_used"],
        "runtime_warnings": diagnostics["runtime_warnings"],
        "constraint_violations": diagnostics["constraint_violations"],
        "artifact_fallback_used": False,
        "artifact_visibility_reason": "",
    }


def _job_prefers_resolved_final_bundle(job: DeepResearchJob) -> bool:
    if job.status == "completed":
        return True
    return job.status in {"failed", "canceled", "interrupted"} and (
        job.current_checkpoint == "finalizing" or job.phase == "finalizing"
    )


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


def _entry_question_ids(entry: dict[str, Any]) -> list[str]:
    if not isinstance(entry, dict):
        return []
    return _dedupe_preserve_order(
        [
            str(question_id).strip()
            for question_id in (
                list(entry.get("question_ids", []) or [])
                + ([entry.get("question_id", "")] if entry.get("question_id") else [])
            )
            if str(question_id).strip()
        ]
    )


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _window_has_terminal_event(events: list[Any], *, job_terminal: bool) -> bool:
    terminal_event_types = {"job_completed", "job_failed", "job_canceled", "job_interrupted"}
    if any(str(getattr(event, "type", "")).strip() in terminal_event_types for event in events):
        return True
    return job_terminal and any(
        str(getattr(event, "type", "")).strip() == "job_resolved_from_final_batch" for event in events
    )


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


def _source_policy_from_job(
    job: DeepResearchJob,
    *,
    continuation: DeepResearchContinuationState,
) -> dict[str, Any]:
    return _source_policy_payload(job=job, continuation=continuation)


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
        continuation_focus = _sanitize_follow_up_surface_items(
            [
                continuation.continuation_goal,
                *continuation.confirmed_claims,
                *continuation.open_questions,
                *continuation.trusted_source_headers,
            ],
            limit=6,
        )
    normalized["continuation_focus"] = _dedupe_preserve_order(continuation_focus)

    stop_policy = dict(normalized.get("stop_policy") or {})
    search_queries = _normalize_string_list(strategy.get("search_queries"))
    selective_fetch = strategy.get("selective_fetch") if isinstance(strategy.get("selective_fetch"), dict) else {}
    official_doc_mode = bool(job.include_domains) and all(
        _domain_looks_like_official_docs(domain) for domain in job.include_domains
    )
    stop_policy.setdefault("stop_on_sufficient_coverage", True)
    stop_policy.setdefault("max_search_queries", max(1, len(search_queries) or config.deep_research_max_concurrency))
    if official_doc_mode:
        stop_policy["max_search_queries"] = max(
            int(stop_policy.get("max_search_queries", 1) or 1),
            len(search_queries),
            len(sub_questions),
        )
    stop_policy.setdefault("max_urls_per_search", int(selective_fetch.get("max_urls_per_search", 1) or 1))
    stop_policy.setdefault("max_runtime_seconds", int(job.resolved_budget_seconds))
    normalized["stop_policy"] = stop_policy
    coverage_checklist = _normalize_string_list(normalized.get("coverage_checklist"))
    if not coverage_checklist:
        coverage_checklist = list(normalized["must_cover"])
    normalized["coverage_checklist"] = _sanitize_follow_up_surface_items(coverage_checklist, limit=6)
    return normalized


def _unsafe_plan_reason(
    *,
    planner: str,
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
    blocked_reasons: list[str],
    query_repair_details: list[dict[str, Any]] | None = None,
    include_domains: list[str] | None = None,
    research_units: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if planner == "fallback":
        return None
    raw_constraint_domains = _dedupe_preserve_order(
        [
            candidate
            for candidate in [
                *(_normalize_string_list(include_domains)),
                *(_normalize_string_list(continuation.carry_forward_constraints.get("include_domains"))),
                *(_normalize_string_list(continuation.carry_forward_constraints.get("allowed_sources"))),
                *(_normalize_string_list(continuation.carry_forward_constraints.get("preferred_sources"))),
            ]
            if candidate and "://" not in candidate
        ]
    )
    safe_search_only_repairs_allowed = (
        bool(raw_constraint_domains)
        and all(_domain_looks_like_official_docs(domain) for domain in raw_constraint_domains)
        and bool(research_units)
        and all(
            str(unit.get("unit_type", "")).strip() == "search" for unit in (research_units or [])
        )
    )
    structural_safe_query_repairs = {
        str(item.get("unit_id", "")).strip()
        for item in query_repair_details or []
        if str(item.get("unit_type", "")).strip() == "search"
        and str(item.get("source_kind", "")).strip() in _STRUCTURAL_SAFE_QUERY_REPAIR_SOURCES
        and _stable_text_equivalent(str(item.get("query", "")), str(item.get("source_value", "")))
    }
    for action in normalize_actions:
        if action.startswith("filled_search_query:"):
            unit_id = action.split(":", 1)[1].strip()
            if unit_id and unit_id in structural_safe_query_repairs:
                continue
        if (
            safe_search_only_repairs_allowed
            and action.startswith(_SAFE_SEARCH_ONLY_REPAIR_ACTION_PREFIXES)
        ):
            continue
        if any(action.startswith(prefix) for prefix in _UNSAFE_NORMALIZE_ACTION_PREFIXES):
            return {"issue": action, "reason": "unsafe_normalize_action"}
    for issue in validation_issues:
        if issue == "missing_search_query" and structural_safe_query_repairs:
            continue
        if safe_search_only_repairs_allowed and issue in _SAFE_SEARCH_ONLY_REPAIR_ISSUES:
            continue
        if issue in _UNSAFE_VALIDATION_ISSUES:
            return {"issue": issue, "reason": "unsafe_validation_issue"}
        if (
            continuation.mode == "continue"
            and issue == "generic_continuation_outline"
            and "expanded_outline_from_follow_up_surface" not in normalize_actions
        ):
            return {"issue": issue, "reason": "unsafe_continuation_outline"}
    effective_blocked_reasons = list(blocked_reasons)
    if safe_search_only_repairs_allowed:
        effective_blocked_reasons = [
            reason
            for reason in effective_blocked_reasons
            if not reason.startswith(_SAFE_SEARCH_ONLY_BLOCKED_REASON_PREFIXES)
        ]
    if effective_blocked_reasons:
        return {"issue": effective_blocked_reasons[0], "reason": "blocked_plan_dependency"}
    return None


def _first_url_from_texts(*values: str) -> str:
    for value in values:
        urls = extract_unique_urls(value or "")
        if urls:
            return urls[0]
    return ""


def _continuation_identity_for_source_job(
    snapshot: dict[str, Any],
) -> str:
    return _continuation_identity_from_snapshot(snapshot)


def _unit_query_fallback(
    unit: dict[str, Any],
    *,
    sub_questions: list[dict[str, Any]],
    search_queries: list[str] | None = None,
    job_query: str,
    continuation: DeepResearchContinuationState,
) -> dict[str, str]:
    candidates: list[tuple[str, str]] = [
        ("existing_query", str(unit.get("_raw_query", unit.get("query", "")))),
        ("unit_goal", str(unit.get("_raw_goal", unit.get("goal", "")))),
        ("unit_title", str(unit.get("_raw_title", unit.get("title", "")))),
        ("unit_notes", str(unit.get("_raw_notes", unit.get("notes", "")))),
        ("unit_instructions", str(unit.get("_raw_instructions", unit.get("instructions", "")))),
    ]
    candidates.extend(("search_strategy", str(item)) for item in search_queries or [])
    if continuation.mode == "continue":
        candidates.extend(
            [
                ("continuation_goal", continuation.continuation_goal),
                *[("continuation_open_question", item) for item in continuation.open_questions],
                *[("continuation_claim", item) for item in continuation.confirmed_claims],
                *[("trusted_source_header", item) for item in continuation.trusted_source_headers],
                ("continuation_summary", continuation.previous_summary),
                *[
                    ("continuation_constraint", str(item))
                    for key in ("allowed_sources", "preferred_sources", "include_domains")
                    for item in continuation.carry_forward_constraints.get(key, []) or []
                ],
            ]
        )
    candidates.extend(("sub_question", str(item.get("question", ""))) for item in sub_questions)
    candidates.append(("job_query", job_query))
    for source_kind, candidate in candidates:
        normalized = _normalize_whitespace(candidate)
        if normalized:
            rewritten = _rewrite_research_query(_trim_text(normalized, limit=220), continuation)
            if rewritten:
                return {
                    "query": rewritten,
                    "source_kind": source_kind,
                    "source_value": _trim_text(normalized, limit=220),
                }
    rewritten_job_query = _rewrite_research_query(job_query, continuation)
    return {
        "query": rewritten_job_query,
        "source_kind": "job_query",
        "source_value": _trim_text(_normalize_whitespace(job_query), limit=220),
    }


def _official_doc_constraint_domains(
    *,
    continuation: DeepResearchContinuationState,
    include_domains: list[str] | None = None,
) -> list[str]:
    candidates = [
        *(_normalize_string_list(include_domains)),
        *(_normalize_string_list(continuation.carry_forward_constraints.get("include_domains"))),
        *(_normalize_string_list(continuation.carry_forward_constraints.get("allowed_sources"))),
        *(_normalize_string_list(continuation.carry_forward_constraints.get("preferred_sources"))),
    ]
    return _dedupe_preserve_order(
        [
            candidate
            for candidate in candidates
            if candidate and "://" not in candidate and _domain_looks_like_official_docs(candidate)
        ]
    )


def _official_doc_carry_forward_url(
    unit: dict[str, Any],
    *,
    continuation: DeepResearchContinuationState,
) -> str:
    allowed_domains = set(
        _official_doc_constraint_domains(
            continuation=continuation,
            include_domains=_normalize_string_list(continuation.carry_forward_constraints.get("include_domains")),
        )
    )
    official_sources = []
    for source in continuation.carry_forward_sources:
        if not isinstance(source, dict):
            continue
        url = _normalize_whitespace(str(source.get("url", "")))
        if not url:
            continue
        try:
            domain = urlsplit(url).netloc.lower()
        except Exception:
            domain = ""
        if allowed_domains and domain not in allowed_domains:
            continue
        if not _domain_looks_like_official_docs(domain):
            continue
        official_sources.append(
            {
                "url": url,
                "domain": domain,
                "title": _normalize_whitespace(str(source.get("title", ""))),
            }
        )
    if not official_sources:
        return ""

    unit_text = " ".join(
        [
            _normalize_whitespace(str(unit.get("title", ""))),
            _normalize_whitespace(str(unit.get("goal", ""))),
            _normalize_whitespace(str(unit.get("query", ""))),
            _normalize_whitespace(str(unit.get("notes", ""))),
            _normalize_whitespace(str(unit.get("instructions", ""))),
        ]
    ).strip()
    unit_keywords = _tokenize_keywords(unit_text)
    ranked_sources = sorted(
        official_sources,
        key=lambda item: (
            _count_keyword_overlap(f"{item['title']} {item['url']}", unit_keywords),
            1 if item["title"] else 0,
            item["url"],
        ),
        reverse=True,
    )
    return ranked_sources[0]["url"]


def _repair_research_units(
    units: list[dict[str, Any]],
    *,
    sub_questions: list[dict[str, Any]],
    search_queries: list[str] | None = None,
    job_query: str,
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
    validation_issues: list[str],
    blocked_reasons: list[str],
    query_repair_details: list[dict[str, Any]] | None = None,
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
                query_repair = _unit_query_fallback(
                    normalized,
                    sub_questions=sub_questions,
                    search_queries=search_queries,
                    job_query=job_query,
                    continuation=continuation,
                )
                normalized["query"] = query_repair["query"]
                _append_unique(validation_issues, "missing_search_query")
                _append_unique(normalize_actions, f"filled_search_query:{unit_id}")
                if query_repair_details is not None:
                    query_repair_details.append(
                        {
                            "unit_id": unit_id,
                            "unit_type": "search",
                            "query": query_repair["query"],
                            "source_kind": query_repair["source_kind"],
                            "source_value": query_repair["source_value"],
                        }
                    )
        elif unit_type in {"fetch", "map"}:
            url = _normalize_whitespace(str(normalized.get("url", "")))
            if not url:
                url = _first_url_from_texts(
                    str(normalized.get("instructions", "")),
                    str(normalized.get("notes", "")),
                    str(normalized.get("goal", "")),
                    str(normalized.get("title", "")),
                )
            if not url and continuation.mode == "continue":
                url = _official_doc_carry_forward_url(normalized, continuation=continuation)
                if url:
                    _append_unique(
                        normalize_actions,
                        f"filled_{unit_type}_url_from_carry_forward:{unit_id}",
                    )
            if url:
                normalized["url"] = url
                _append_unique(normalize_actions, f"filled_{unit_type}_url:{unit_id}")
            else:
                normalized["unit_type"] = "search"
                query_repair = _unit_query_fallback(
                    normalized,
                    sub_questions=sub_questions,
                    search_queries=search_queries,
                    job_query=job_query,
                    continuation=continuation,
                )
                normalized["query"] = query_repair["query"]
                _append_unique(validation_issues, f"missing_{unit_type}_url")
                _append_unique(normalize_actions, f"degraded_{unit_type}_without_url_to_search:{unit_id}")
                if query_repair_details is not None:
                    query_repair_details.append(
                        {
                            "unit_id": unit_id,
                            "unit_type": "search",
                            "query": query_repair["query"],
                            "source_kind": query_repair["source_kind"],
                            "source_value": query_repair["source_value"],
                            "degraded_from_unit_type": unit_type,
                        }
                    )
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
        for transient_key in ("_raw_title", "_raw_goal", "_raw_query", "_raw_notes", "_raw_instructions"):
            unit.pop(transient_key, None)
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


def _ensure_sub_question_search_query_coverage(
    search_queries: list[str],
    *,
    sub_questions: list[dict[str, Any]],
    continuation: DeepResearchContinuationState,
    normalize_actions: list[str],
) -> list[str]:
    covered_queries = list(search_queries)
    filtered_queries = _sanitize_follow_up_surface_items(
        covered_queries,
        limit=max(1, len(covered_queries) + len(sub_questions)),
    )
    if filtered_queries != covered_queries:
        _append_unique(normalize_actions, "filtered_low_signal_search_queries")
        covered_queries = filtered_queries
    for sub_question in sub_questions:
        question = _normalize_whitespace(str(sub_question.get("question", "")))
        question_key = _stable_text_key(question) if question else ""
        question_tokens = _tokenize_keywords(question)
        coverage_threshold = max(3, min(4, max(1, len(question_tokens) - 1)))
        if any(
            (
                question_key
                and question_key in _stable_text_key(query)
            )
            or (
                question_tokens
                and _count_keyword_overlap(query, question_tokens) >= coverage_threshold
            )
            for query in covered_queries
        ):
            continue
        query = _rewrite_research_query(question, continuation)
        if not query:
            continue
        covered_queries.append(query)
        _append_unique(normalize_actions, f"added_sub_question_search_query:{sub_question.get('id', query)}")
    return _dedupe_preserve_order(covered_queries)


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
    if not outline:
        return outline
    first_title = str(outline[0].get("title", "")).strip()
    remainder = outline[1:]
    if remainder and not all(_is_generic_section_title(str(item.get("title", "")).strip()) for item in remainder):
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
                "status": "planned",
                "coverage_state": {},
                "rewrite_reason": "sub_questions",
            }
        )
    if len(expanded) <= len(outline):
        return outline
    _append_unique(validation_issues, "generic_outline_for_sub_questions")
    _append_unique(normalize_actions, "expanded_outline_from_sub_questions")
    return expanded


def _outline_from_follow_up_surface(
    outline: list[dict[str, Any]],
    *,
    continuation_focus: list[str],
    normalize_actions: list[str],
) -> list[dict[str, Any]]:
    if not outline:
        return outline
    focused_titles = [
        _trim_text(_normalize_whitespace(item.rstrip(" ?")), limit=96)
        for item in continuation_focus
        if _trim_text(_normalize_whitespace(item.rstrip(" ?")), limit=96)
    ]
    if not focused_titles:
        return outline
    rewriteable_generic_sections = [
        item
        for item in outline
        if _is_generic_section_title(str(item.get("title", "")).strip())
        and str(item.get("title", "")).strip().lower() != "executive summary"
    ]
    if not rewriteable_generic_sections:
        return outline
    expanded: list[dict[str, Any]] = []
    used_section_ids: set[str] = set()
    seen_titles: set[str] = set()
    for item in outline:
        title = _trim_text(_normalize_whitespace(str(item.get("title", "")).rstrip(" ?")), limit=96)
        if _is_generic_section_title(title) and title.lower() != "executive summary":
            continue
        candidate = dict(item)
        expanded.append(candidate)
        section_id = str(candidate.get("section_id", "")).strip()
        if section_id:
            used_section_ids.add(section_id)
        if title:
            seen_titles.add(title.lower())
    added = 0
    for title in focused_titles:
        if title.lower() in seen_titles:
            continue
        section_id = _slugify(title)
        candidate_id = section_id
        suffix = 2
        while candidate_id in used_section_ids:
            candidate_id = f"{section_id}-{suffix}"
            suffix += 1
        used_section_ids.add(candidate_id)
        expanded.append(
            {
                "section_id": candidate_id,
                "title": title[:1].upper() + title[1:] if title and title[0].islower() else title,
                "goal": title,
                "status": "planned",
                "coverage_state": {},
                "rewrite_reason": "follow_up_surface",
            }
        )
        seen_titles.add(title.lower())
        added += 1
    if added == 0:
        return outline
    _append_unique(normalize_actions, "expanded_outline_from_follow_up_surface")
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
    content_titles: list[str] = []
    for item in report_outline:
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip().lower()
        else:
            title = str(item).strip().lower()
        if title:
            if title == "executive summary":
                continue
            content_titles.append(title)
    return bool(content_titles) and all(_is_generic_section_title(title) for title in content_titles)


def _is_generic_section_title(title: str) -> bool:
    normalized = _normalize_whitespace(title).lower()
    return normalized in _GENERIC_OUTLINE_TITLES


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
        if normalized.lower() in _GENERIC_CONTINUATION_QUESTION_TITLES:
            continue
        if normalized:
            items.append(normalized)
    for value in coverage.get("unanswered_sections", []) or []:
        normalized = _normalize_whitespace(str(value))
        if normalized.lower() in _GENERIC_CONTINUATION_QUESTION_TITLES:
            continue
        if normalized:
            items.append(normalized)
    return _dedupe_preserve_order(items)


def _collect_confirmed_claims(sections: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    claims: list[str] = []
    for section in sections:
        for claim in section.get("claims", []):
            citations = [citation for citation in claim.get("citations", []) if str(citation).strip()]
            if not citations:
                continue
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


def _sanitize_follow_up_surface_items(
    items: list[str],
    *,
    limit: int,
) -> list[str]:
    sanitized: list[str] = []
    for item in items:
        normalized = _summarize_evidence_text(str(item), limit=220) or _normalize_whitespace(str(item))
        if not normalized:
            continue
        if normalized.lower() in _GENERIC_CONTINUATION_QUESTION_TITLES:
            continue
        if _is_noisy_text(normalized):
            continue
        sanitized.append(normalized)
        if len(sanitized) >= limit:
            break
    return _dedupe_preserve_order(sanitized)


def _bounded_salvage_queries(
    raw_plan: dict[str, Any] | None,
    *,
    continuation: DeepResearchContinuationState,
) -> list[str]:
    if not isinstance(raw_plan, dict):
        return []
    candidates: list[str] = []
    for item in raw_plan.get("sub_questions") or []:
        if isinstance(item, dict):
            candidates.append(str(item.get("question", "")))
    strategy = raw_plan.get("search_strategy")
    if isinstance(strategy, dict):
        candidates.extend(str(value) for value in strategy.get("search_queries") or [])
    for item in raw_plan.get("research_units") or []:
        if not isinstance(item, dict):
            continue
        for key in ("query", "goal", "title", "notes", "instructions"):
            candidates.append(str(item.get(key, "")))
    return _sanitize_follow_up_surface_items(
        [_rewrite_research_query(candidate, continuation) for candidate in candidates],
        limit=3,
    )


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
    *,
    coverage_state: dict[str, Any] | None = None,
) -> bool:
    targets = _stop_policy_targets(plan)
    if not targets:
        return False
    if not unit_results:
        return False
    items = coverage_state.get("items") if isinstance(coverage_state, dict) else None
    if isinstance(items, list) and items:
        normalized_targets = {_normalize_whitespace(target) for target in targets if _normalize_whitespace(target)}
        matched_items = [
            item
            for item in items
            if _normalize_whitespace(str(item.get("target", ""))) in normalized_targets
        ]
        if len(matched_items) < len(normalized_targets):
            return False
        if not all(bool(item.get("satisfied")) for item in matched_items):
            return False
        distinct_unit_ids = {
            unit_id
            for item in matched_items
            for unit_id in item.get("matched_unit_ids", [])
            if _normalize_whitespace(str(unit_id))
        }
        if len(normalized_targets) > 1 and len(distinct_unit_ids) < len(normalized_targets):
            return False
        return True
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


def _source_policy_payload(
    *,
    job: DeepResearchJob,
    continuation: DeepResearchContinuationState | None = None,
) -> dict[str, Any]:
    include_domains = list(job.include_domains)
    exclude_domains = list(job.exclude_domains)
    official_doc_mode = any(domain.startswith("docs.") or domain == "docs.aws.amazon.com" for domain in include_domains)
    return {
        "mode": "official_docs_only" if official_doc_mode else "mixed_web",
        "allowed_domains": include_domains,
        "blocked_domains": exclude_domains,
        "allowed_tools": ["search", "fetch", "map"],
        "user_data_refs": [],
        "trust_tiers": ["official_docs", "standard", "paper", "third_party", "community"],
        "freshness_window": "current_web",
        "must_cite_per_section": True,
        "continuation_mode": continuation.mode if continuation is not None else "fresh",
    }


def _lineage_payload(
    *,
    job: DeepResearchJob,
    continuation: DeepResearchContinuationState,
) -> dict[str, Any]:
    root_job_id = continuation.lineage_root_job_id or continuation.source_job_id or job.job_id
    return {
        "root_job_id": root_job_id,
        "parent_job_id": continuation.parent_job_id or continuation.source_job_id,
        "continued_from_job_id": job.continued_from_job_id,
        "continued_from_checkpoint": continuation.resume_from_checkpoint_key or continuation.checkpoint_key,
        "replay_from_checkpoint": continuation.replay_from_checkpoint_key or continuation.checkpoint_key,
        "fork_type": continuation.mode,
        "carried_forward_source_ids": [
            str(item.get("source_id", "")).strip()
            for item in continuation.carry_forward_sources
            if str(item.get("source_id", "")).strip()
        ],
        "carried_forward_evidence_ids": [
            str(item.get("evidence_id", "")).strip()
            for item in continuation.carry_forward_evidence
            if str(item.get("evidence_id", "")).strip()
        ],
        "skipped_unit_ids": list(continuation.skipped_unit_ids),
        "supersedes_job_id": (
            continuation.parent_job_id or continuation.source_job_id
            if continuation.mode == "continue"
            else ""
        ),
    }


def _selected_bank_payload(
    *,
    section_banks: list[dict[str, Any]],
    evidence_ledger: list[dict[str, Any]],
    sections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    decisions_by_section_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    valid_claim_ids_by_section: dict[str, set[str]] = {}
    if sections:
        valid_claim_ids_by_section = {
            str(section.get("section_id", "")).strip(): {
                str(claim.get("claim_id", "")).strip()
                for claim in section.get("claims", []) or []
                if isinstance(claim, dict) and str(claim.get("claim_id", "")).strip()
            }
            for section in sections
            if isinstance(section, dict) and str(section.get("section_id", "")).strip()
        }
    for entry in evidence_ledger:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        selected_section_id = str(entry.get("selected_section_id", "")).strip()
        if selected_section_id:
            decisions_by_section_evidence[(selected_section_id, evidence_id)] = {
                "selection_reason": str(entry.get("disposition_reason", "")).strip(),
                "coverage_tags": list(entry.get("question_ids", []) or []),
                "rejected_reason": "",
            }
        for rejected_section_id in entry.get("rejected_section_ids", []) or []:
            normalized_section_id = str(rejected_section_id).strip()
            if not normalized_section_id:
                continue
            decisions_by_section_evidence[(normalized_section_id, evidence_id)] = {
                "selection_reason": "",
                "coverage_tags": list(entry.get("question_ids", []) or []),
                "rejected_reason": str(entry.get("disposition_reason", "")).strip() or "not_selected_for_section",
            }
    payload: list[dict[str, Any]] = []
    for bank in section_banks:
        if not isinstance(bank, dict):
            continue
        section_id = str(bank.get("section_id", "")).strip()
        if not section_id:
            continue
        selected_rows = []
        for packet in bank.get("selected_packets", []) or []:
            if not isinstance(packet, dict):
                continue
            evidence_id = str(packet.get("evidence_id", "")).strip()
            if not evidence_id:
                continue
            valid_claim_ids = valid_claim_ids_by_section.get(section_id)
            claim_ids = [
                claim_id
                for claim_id in (
                    str(raw_claim_id).strip()
                    for raw_claim_id in packet.get("claim_ids", []) or []
                )
                if claim_id and (valid_claim_ids is None or claim_id in valid_claim_ids)
            ]
            selection_details = decisions_by_section_evidence.get((section_id, evidence_id), {})
            selected_rows.append(
                {
                    "evidence_id": evidence_id,
                    "selected_section_id": section_id,
                    "selection_reason": str(selection_details.get("selection_reason", "")).strip(),
                    "coverage_tags": list(selection_details.get("coverage_tags", []) or []),
                    "rejected_reason": str(selection_details.get("rejected_reason", "")).strip(),
                    "claim_ids": claim_ids,
                    "question_ids": list(packet.get("question_ids", []) or []),
                }
            )
        payload.append(
            {
                "section_id": section_id,
                "selected_evidence_ids": list(bank.get("selected_evidence_ids", []) or []),
                "selected_rows": selected_rows,
                "candidate_evidence_ids": list(bank.get("candidate_evidence_ids", []) or []),
                "rejected_evidence_ids": list(bank.get("rejected_evidence_ids", []) or []),
            }
        )
    return payload


def _evidence_bank_payload(
    *,
    evidence_items: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    usage_by_evidence_id: dict[str, dict[str, set[str]]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", "")).strip()
            for evidence_id in claim.get("evidence_ids", []) or []:
                normalized_evidence_id = str(evidence_id).strip()
                if not normalized_evidence_id:
                    continue
                usage = usage_by_evidence_id.setdefault(
                    normalized_evidence_id,
                    {"section_ids": set(), "claim_ids": set()},
                )
                if section_id:
                    usage["section_ids"].add(section_id)
                if claim_id:
                    usage["claim_ids"].add(claim_id)

    payload: list[dict[str, Any]] = []
    for item in evidence_items:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        usage = usage_by_evidence_id.get(evidence_id, {"section_ids": set(), "claim_ids": set()})
        snippet = _summarize_evidence_text(str(item.get("summary") or item.get("detail") or ""), limit=220)
        payload.append(
            {
                "evidence_id": evidence_id,
                "source_ids": [
                    str(source_id).strip()
                    for source_id in item.get("source_ids", []) or []
                    if str(source_id).strip()
                ],
                "source_url": _normalize_whitespace(
                    str(item.get("derived_from_source_url") or (item.get("source_urls") or [""])[0] or "")
                ),
                "snippet_or_excerpt": snippet,
                "source_backed": str(item.get("evidence_kind", "")).strip() != "search",
                "line_span": {
                    "start": item.get("line_start"),
                    "end": item.get("line_end"),
                },
                "origin_tool": str(item.get("evidence_kind", "")).strip() or "search",
                "used_by_section_ids": sorted(usage["section_ids"]),
                "used_by_claim_ids": sorted(usage["claim_ids"]),
            }
        )
    return payload


def _build_section_prose(section: dict[str, Any]) -> str:
    summary = _strip_summary_scaffolding(_normalize_whitespace(str(section.get("summary", "") or "")))
    raw_claim_texts = [
        _strip_summary_scaffolding(_normalize_whitespace(str(claim.get("text", "") or "")))
        for claim in section.get("claims", []) or []
        if isinstance(claim, dict) and _normalize_whitespace(str(claim.get("text", "") or ""))
    ]
    unique_sentences: list[str] = []
    seen_sentence_keys: set[str] = set()
    for candidate in [summary, *raw_claim_texts]:
        if not candidate or _is_noisy_text(candidate):
            continue
        normalized_candidate = candidate.rstrip(".")
        candidate_key = _stable_text_key(normalized_candidate)
        if candidate_key and candidate_key in seen_sentence_keys:
            continue
        replacement_index: int | None = None
        should_skip = False
        for index, existing in enumerate(unique_sentences):
            existing_text = existing.rstrip(".")
            if _stable_text_equivalent(normalized_candidate, existing_text):
                should_skip = True
                break
            if normalized_candidate in existing:
                should_skip = True
                break
            if existing_text in normalized_candidate:
                if len(normalized_candidate) > len(existing_text) + 12:
                    replacement_index = index
                    break
                should_skip = True
                break
        if should_skip:
            continue
        if replacement_index is not None:
            existing_key = _stable_text_key(unique_sentences[replacement_index].rstrip("."))
            if existing_key:
                seen_sentence_keys.discard(existing_key)
            unique_sentences.pop(replacement_index)
        unique_sentences.append(normalized_candidate + ".")
        if candidate_key:
            seen_sentence_keys.add(candidate_key)
        if len(unique_sentences) >= 3:
            break
    return _trim_text(" ".join(unique_sentences), limit=700)


def _supported_claim_ids(verifier: dict[str, Any]) -> set[str]:
    flagged_claim_ids = {
        str(claim_id).strip()
        for claim_id in verifier.get("flagged_claim_ids", []) or []
        if str(claim_id).strip()
    }
    return {
        str(claim.get("claim_id", "")).strip()
        for claim in verifier.get("supported_claims", []) or []
        if isinstance(claim, dict)
        and str(claim.get("claim_id", "")).strip()
        and str(claim.get("claim_id", "")).strip() not in flagged_claim_ids
    }


def _supported_claim_inventory(
    sections: list[dict[str, Any]],
    verifier: dict[str, Any],
) -> tuple[set[str], set[str], set[str]]:
    supported_ids = _supported_claim_ids(verifier)
    flagged_claim_ids = {
        str(claim_id).strip()
        for claim_id in verifier.get("flagged_claim_ids", []) or []
        if str(claim_id).strip()
    }
    supported_evidence_ids: set[str] = set()
    supported_source_ids: set[str] = set()
    has_non_generic_claim_inventory = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("title", "") or "")
        section_is_generic = (
            is_summary_section_title(section_title)
            or is_key_findings_section_title(section_title)
            or is_open_questions_section_title(section_title)
        )
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", "")).strip()
            claim_is_supported = bool(claim_id and claim_id in supported_ids)
            if not claim_is_supported and supported_ids:
                continue
            if not claim_is_supported and not supported_ids:
                if section_is_generic or (claim_id and claim_id in flagged_claim_ids):
                    continue
            if not section_is_generic:
                has_non_generic_claim_inventory = True
            for evidence_id in claim.get("evidence_ids", []) or []:
                normalized_evidence_id = str(evidence_id).strip()
                if normalized_evidence_id:
                    supported_evidence_ids.add(normalized_evidence_id)
            for source_id in [*(claim.get("source_ids", []) or []), *(claim.get("citations", []) or [])]:
                normalized_source_id = str(source_id).strip()
                if normalized_source_id:
                    supported_source_ids.add(normalized_source_id)
    if not supported_ids and not has_non_generic_claim_inventory:
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title", "") or "")
            if not (
                is_summary_section_title(section_title)
                or is_key_findings_section_title(section_title)
            ):
                continue
            for claim in section.get("claims", []) or []:
                if not isinstance(claim, dict):
                    continue
                claim_id = str(claim.get("claim_id", "")).strip()
                if claim_id and claim_id in flagged_claim_ids:
                    continue
                for evidence_id in claim.get("evidence_ids", []) or []:
                    normalized_evidence_id = str(evidence_id).strip()
                    if normalized_evidence_id:
                        supported_evidence_ids.add(normalized_evidence_id)
                for source_id in [*(claim.get("source_ids", []) or []), *(claim.get("citations", []) or [])]:
                    normalized_source_id = str(source_id).strip()
                    if normalized_source_id:
                        supported_source_ids.add(normalized_source_id)
    return supported_ids, supported_evidence_ids, supported_source_ids


def _rebuild_verified_rollup_sections(
    sections: list[dict[str, Any]],
    verifier: dict[str, Any],
) -> list[dict[str, Any]]:
    supported_ids, supported_evidence_ids, supported_source_ids = _supported_claim_inventory(sections, verifier)
    rebuilt_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            rebuilt_sections.append(section)
            continue
        title = str(section.get("title", "") or "")
        normalized_section = dict(section)
        claims = [dict(claim) for claim in normalized_section.get("claims", []) or [] if isinstance(claim, dict)]
        if is_summary_section_title(title) or is_key_findings_section_title(title):
            filtered_claims: list[dict[str, Any]] = []
            for claim in claims:
                claim_id = str(claim.get("claim_id", "")).strip()
                claim_evidence_ids = {
                    str(evidence_id).strip()
                    for evidence_id in claim.get("evidence_ids", []) or []
                    if str(evidence_id).strip()
                }
                claim_source_ids = {
                    str(source_id).strip()
                    for source_id in [*(claim.get("source_ids", []) or []), *(claim.get("citations", []) or [])]
                    if str(source_id).strip()
                }
                if claim_id and claim_id in supported_ids:
                    filtered_claims.append(claim)
                    continue
                if claim_evidence_ids and claim_evidence_ids & supported_evidence_ids:
                    filtered_claims.append(claim)
                    continue
                if claim_source_ids and claim_source_ids & supported_source_ids:
                    filtered_claims.append(claim)
            normalized_section["claims"] = filtered_claims
            normalized_section["summary"] = _build_section_summary(filtered_claims) if filtered_claims else ""
            normalized_section["prose"] = _build_section_prose(normalized_section) if filtered_claims else ""
            normalized_section["citations"] = sorted(
                {
                    str(citation).strip()
                    for claim in filtered_claims
                    for citation in claim.get("citations", []) or []
                    if str(citation).strip()
                }
            )
            normalized_section["source_ids"] = sorted(
                {
                    str(source_id).strip()
                    for claim in filtered_claims
                    for source_id in claim.get("source_ids", []) or []
                    if str(source_id).strip()
                }
            )
            normalized_section["evidence_ids"] = sorted(
                {
                    str(evidence_id).strip()
                    for claim in filtered_claims
                    for evidence_id in claim.get("evidence_ids", []) or []
                    if str(evidence_id).strip()
                }
            )
            if filtered_claims:
                normalized_section["confidence"] = _cluster_confidence(
                    source_count=len({citation for claim in filtered_claims for citation in claim.get("citations", [])}),
                    evidence_count=sum(len(claim.get("evidence_ids", []) or []) for claim in filtered_claims),
                )
        elif claims:
            normalized_section["prose"] = _build_section_prose(normalized_section)
        rebuilt_sections.append(normalized_section)
    return rebuilt_sections


def _packet_to_prose_fidelity_payload(
    *,
    selected_bank: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    final_report: str = "",
) -> dict[str, Any]:
    section_by_id = {
        str(section.get("section_id", "")).strip(): dict(section)
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    missing_selected_packet_ids: list[str] = []
    checked_packet_count = 0
    final_report_text = _normalize_whitespace(final_report)
    for bank in selected_bank:
        if not isinstance(bank, dict):
            continue
        section_id = str(bank.get("section_id", "")).strip()
        section = section_by_id.get(section_id, {})
        section_text = _normalize_whitespace(
            " ".join(
                [
                    str(section.get("summary", "") or ""),
                    str(section.get("prose", "") or ""),
                    *[
                        str(claim.get("text", "") or "")
                        for claim in section.get("claims", []) or []
                        if isinstance(claim, dict)
                    ],
                ]
            )
        )
        for row in bank.get("selected_rows", []) or []:
            if not isinstance(row, dict):
                continue
            evidence_id = str(row.get("evidence_id", "")).strip()
            if not evidence_id:
                continue
            checked_packet_count += 1
            coverage_tags = [
                _normalize_whitespace(str(tag))
                for tag in row.get("coverage_tags", []) or []
                if _normalize_whitespace(str(tag))
            ]
            claim_ids = [
                str(claim_id).strip()
                for claim_id in row.get("claim_ids", []) or []
                if str(claim_id).strip()
            ]
            packet_reflected = bool(claim_ids)
            if not packet_reflected and coverage_tags:
                packet_reflected = any(
                    _count_keyword_overlap(section_text, _tokenize_keywords(tag)) > 0
                    or _count_keyword_overlap(final_report_text, _tokenize_keywords(tag)) > 0
                    for tag in coverage_tags
                )
            if not packet_reflected:
                missing_selected_packet_ids.append(evidence_id)
    passed = not missing_selected_packet_ids
    return {
        "passed": passed,
        "checked_packet_count": checked_packet_count,
        "missing_selected_packet_ids": _dedupe_preserve_order(missing_selected_packet_ids),
        "reason_codes": [] if passed else ["selected_packet_missing_from_prose"],
    }


def _verification_payload(
    *,
    sections: list[dict[str, Any]],
    coverage: dict[str, Any],
    verifier: dict[str, Any],
    selected_bank: list[dict[str, Any]],
    final_report: str,
) -> dict[str, Any]:
    flagged_claim_ids = {
        str(claim_id).strip()
        for claim_id in verifier.get("flagged_claim_ids", []) or []
        if str(claim_id).strip()
    }
    supported_claims: list[dict[str, Any]] = []
    single_source_claims: list[dict[str, Any]] = []
    conflicted_claims: list[dict[str, Any]] = []
    confidence_by_section: dict[str, str] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        if section_id:
            confidence_by_section[section_id] = str(section.get("confidence", "") or "")
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", "")).strip()
            claim_payload = {
                "claim_id": claim_id,
                "section_id": section_id,
                "text": str(claim.get("text", "") or ""),
                "confidence": str(claim.get("confidence", "") or ""),
            }
            if claim_id and claim_id not in flagged_claim_ids:
                supported_claims.append(claim_payload)
            supporting_source_count = len(
                {
                    str(source_id).strip()
                    for source_id in claim.get("source_ids", []) or claim.get("citations", []) or []
                    if str(source_id).strip()
                }
            )
            if supporting_source_count <= 1:
                single_source_claims.append(claim_payload)
    packet_to_prose_fidelity = _packet_to_prose_fidelity_payload(
        selected_bank=selected_bank,
        sections=sections,
        final_report=final_report,
    )
    if "conflict" in set(verifier.get("reason_codes", []) or []):
        conflicted_claims = [
            claim_payload
            for claim_payload in [*supported_claims, *single_source_claims]
            if str(claim_payload.get("claim_id", "")).strip() in flagged_claim_ids
        ]
        if not conflicted_claims:
            conflicted_claims = [
                {
                    "claim_id": str(claim.get("claim_id", "")).strip(),
                    "section_id": str(claim.get("section_id", "") or ""),
                    "text": str(claim.get("text", "") or ""),
                    "confidence": str(claim.get("confidence", "") or ""),
                }
                for claim in verifier.get("conflicted_claims", []) or []
                if isinstance(claim, dict) and str(claim.get("claim_id", "")).strip()
            ]
    unresolved_sections = [
        str(title)
        for title in [
            *(coverage.get("unanswered_sections", []) or []),
            *(coverage.get("hard_uncovered_targets", []) or []),
        ]
        if str(title).strip()
    ]
    selected_evidence_ids = {
        str(row.get("evidence_id", "")).strip()
        for bank in selected_bank
        if isinstance(bank, dict)
        for row in bank.get("selected_rows", []) or []
        if isinstance(row, dict) and str(row.get("evidence_id", "")).strip()
    }
    used_evidence_ids = {
        str(evidence_id).strip()
        for section in sections
        if isinstance(section, dict)
        for claim in section.get("claims", []) or []
        if isinstance(claim, dict)
        for evidence_id in claim.get("evidence_ids", []) or []
        if str(evidence_id).strip()
    }
    return {
        "supported_claims": supported_claims,
        "single_source_claims": single_source_claims,
        "conflicted_claims": conflicted_claims,
        "unmapped_evidence_ids": sorted(selected_evidence_ids - used_evidence_ids),
        "confidence_by_section": confidence_by_section,
        "unresolved_sections": _dedupe_preserve_order(unresolved_sections),
        "packet_to_prose_fidelity": packet_to_prose_fidelity,
    }


def _coverage_gaps_payload(
    *,
    query: str,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    unanswered_sections = _dedupe_preserve_order(
        [str(item) for item in coverage.get("unanswered_sections", []) or [] if str(item).strip()]
    )
    uncovered_sub_questions = _dedupe_preserve_order(
        [str(item) for item in coverage.get("uncovered_sub_questions", []) or [] if str(item).strip()]
    )
    hard_uncovered_targets = _dedupe_preserve_order(
        [str(item) for item in coverage.get("hard_uncovered_targets", []) or [] if str(item).strip()]
    )
    gaps = [
        {"gap_type": "unanswered_section", "target": item, "blocking": True}
        for item in unanswered_sections
    ] + [
        {"gap_type": "uncovered_sub_question", "target": item, "blocking": True}
        for item in uncovered_sub_questions
    ] + [
        {"gap_type": "hard_uncovered_target", "target": item, "blocking": False}
        for item in hard_uncovered_targets
    ]
    return {
        "query": query,
        "unanswered_sections": unanswered_sections,
        "uncovered_sub_questions": uncovered_sub_questions,
        "hard_uncovered_targets": hard_uncovered_targets,
        "coverage_gate_passed": bool(coverage.get("coverage_gate_passed", not gaps)),
        "hard_coverage_gate_passed": bool(coverage.get("hard_coverage_gate_passed", not hard_uncovered_targets)),
        "blocking_gap_count": len(unanswered_sections) + len(uncovered_sub_questions),
        "hard_gap_count": len(hard_uncovered_targets),
        "total_gap_count": len(gaps),
        "gaps": gaps,
    }


def _claim_conflict_reason(left_text: str, right_text: str) -> str:
    left = _normalize_whitespace(left_text)
    right = _normalize_whitespace(right_text)
    if not left or not right:
        return ""
    left_tokens = set(_tokenize_keywords(left))
    right_tokens = set(_tokenize_keywords(right))
    overlap = left_tokens & right_tokens
    if len(overlap) < 2:
        return ""
    left_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", left))
    right_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", right))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return "numeric_mismatch"
    negation_markers = (" no ", " not ", " never ", " without ", " cannot ", " can't ", " unavailable ")
    left_has_negation = any(marker in f" {left.lower()} " for marker in negation_markers)
    right_has_negation = any(marker in f" {right.lower()} " for marker in negation_markers)
    if left_has_negation != right_has_negation:
        return "negation_mismatch"
    return ""


def _write_internal_state_artifacts(
    runtime: "DeepResearchRuntime",
    job_id: str,
    *,
    source_policy: dict[str, Any],
    lineage: dict[str, Any],
    outline_versions: list[dict[str, Any]],
    section_graph: dict[str, Any],
    evidence_ledger: list[dict[str, Any]],
    section_banks: list[dict[str, Any]],
) -> None:
    runtime.write_artifact(
        job_id,
        _SOURCE_POLICY_ARTIFACT_KIND,
        _json_markdown_block(source_policy),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _LINEAGE_ARTIFACT_KIND,
        _json_markdown_block(lineage),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _OUTLINE_VERSIONS_ARTIFACT_KIND,
        _json_markdown_block(outline_versions),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _OUTLINE_STATE_ARTIFACT_KIND,
        _json_markdown_block(section_graph),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _EVIDENCE_LEDGER_ARTIFACT_KIND,
        _json_markdown_block(evidence_ledger),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _SECTION_BANKS_ARTIFACT_KIND,
        _json_markdown_block(section_banks),
        "application/json",
    )
    runtime.write_artifact(
        job_id,
        _SELECTED_BANK_ARTIFACT_KIND,
        _json_markdown_block(
            _selected_bank_payload(
                section_banks=section_banks,
                evidence_ledger=evidence_ledger,
            )
        ),
        "application/json",
    )


def _reconcile_section_graph_with_materialized_sections(
    section_graph: dict[str, Any] | None,
    *,
    planned_outline: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    updated_at: str = "",
) -> dict[str, Any]:
    base = dict(section_graph or {}) if isinstance(section_graph, dict) else {}
    base_nodes = {
        str(node.get("section_id", "")).strip(): dict(node)
        for node in base.get("nodes", [])
        if isinstance(node, dict) and str(node.get("section_id", "")).strip()
    }
    sections_by_id = {
        str(section.get("section_id", "")).strip(): dict(section)
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    nodes: list[dict[str, Any]] = []
    root_section_ids: list[str] = []
    for planned in planned_outline:
        if not isinstance(planned, dict):
            continue
        section_id = str(planned.get("section_id", "")).strip()
        if not section_id:
            continue
        root_section_ids.append(section_id)
        materialized = sections_by_id.get(section_id, {})
        base_node = base_nodes.get(section_id, {})
        materialized_evidence_ids = [
            str(evidence_id).strip()
            for evidence_id in materialized.get("evidence_ids", []) or []
            if str(evidence_id).strip()
        ]
        base_selected_evidence_ids = [
            str(evidence_id).strip()
            for evidence_id in base_node.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip()
        ]
        selected_evidence_ids = (
            base_selected_evidence_ids
            if base_selected_evidence_ids and set(base_selected_evidence_ids) == set(materialized_evidence_ids)
            else materialized_evidence_ids or base_selected_evidence_ids
        )
        node = {
            **base_node,
            "section_id": section_id,
            "title": str(planned.get("title", "") or base_node.get("title", "")).strip(),
            "goal": str(planned.get("goal", "") or base_node.get("goal", "")).strip(),
            "status": "grounded" if materialized.get("claims") else str(base_node.get("status", "") or "planned"),
            "rewrite_reason": str(planned.get("rewrite_reason", "") or base_node.get("rewrite_reason", "")).strip(),
            "evidence_ids": selected_evidence_ids or list(base_node.get("evidence_ids") or []),
            "selected_evidence_ids": selected_evidence_ids,
            "source_ids": [
                str(source_id).strip()
                for source_id in materialized.get("source_ids", []) or []
                if str(source_id).strip()
            ] or list(base_node.get("source_ids") or []),
            "coverage_state": {
                **dict(base_node.get("coverage_state") or {}),
                "grounded_claim_ids": [
                    str(claim.get("claim_id", "")).strip()
                    for claim in materialized.get("claims", []) or []
                    if str(claim.get("claim_id", "")).strip()
                ],
                "grounded_evidence_ids": materialized_evidence_ids,
                "grounded_source_ids": [
                    str(source_id).strip()
                    for source_id in materialized.get("source_ids", []) or []
                    if str(source_id).strip()
                ],
                "pool_mode": str(materialized.get("pool_mode", "") or "").strip(),
                "question_coverage": [
                    str(question_id).strip()
                    for question_id in materialized.get("question_ids", []) or []
                    if str(question_id).strip()
                ],
                "binding_summary": {
                    "claim_count": len(materialized.get("claims", []) or []),
                    "evidence_binding_count": sum(
                        len(claim.get("evidence_bindings", []) or [])
                        for claim in materialized.get("claims", []) or []
                        if isinstance(claim, dict)
                    ),
                },
                "explainable_by": ["claim_evidence_bindings", "section_packets"]
                if materialized.get("claims")
                else [],
            },
            "last_updated_at": updated_at or str(base_node.get("last_updated_at", "") or ""),
        }
        nodes.append(DeepResearchSectionNode.model_validate(node).model_dump())
    return DeepResearchSectionGraphState(
        version=int(base.get("version", 1) or 1),
        root_section_ids=root_section_ids,
        nodes=nodes,
    ).model_dump()


def _checkpoint_kind(checkpoint_key: str) -> str:
    normalized = (checkpoint_key or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("researching-dispatch-"):
        return "research_dispatch"
    if normalized.startswith("researching-"):
        return "research_unit"
    if normalized == "planning":
        return "planning"
    if normalized == "synthesizing":
        return "synthesizing"
    if normalized == "finalizing":
        return "finalizing"
    return "unknown"


def _checkpoint_state_payload(
    *,
    plan: DeepResearchPlan,
    completed_unit_ids: list[str],
    failed_unit_ids: list[str],
    failed_units: list[dict[str, Any]],
    skipped_unit_ids: list[str],
    skipped_units: list[dict[str, Any]],
    constraint_violations: list[dict[str, Any]],
    coverage_state: dict[str, Any],
    unit_results: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]],
    evidence_items: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    outline_versions: list[dict[str, Any]] | None = None,
    section_graph: dict[str, Any] | None = None,
    evidence_ledger: list[dict[str, Any]] | None = None,
    section_banks: list[dict[str, Any]] | None = None,
) -> DeepResearchCheckpointState:
    return DeepResearchCheckpointState(
        plan=plan,
        completed_unit_ids=list(completed_unit_ids),
        failed_unit_ids=list(failed_unit_ids),
        failed_units=[dict(item) for item in failed_units],
        skipped_unit_ids=list(skipped_unit_ids),
        skipped_units=[dict(item) for item in skipped_units],
        constraint_violations=[dict(item) for item in constraint_violations],
        coverage_state=dict(coverage_state),
        unit_results=dict(unit_results),
        sources=list(sources),
        evidence_items=list(evidence_items),
        sections=list(sections),
        outline_versions=list(outline_versions or []),
        section_graph=dict(section_graph or {}),
        evidence_ledger=list(evidence_ledger or []),
        section_banks=list(section_banks or []),
    )


def _runtime_coverage_state(
    plan: DeepResearchPlan,
    unit_results: dict[str, dict[str, Any]],
    *,
    completed_unit_ids: list[str],
    failed_unit_ids: list[str],
    skipped_unit_ids: list[str],
    section_banks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks or []
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    sections_by_id = {
        section.section_id: section
        for section in plan.report_outline
    }
    for target in _stop_policy_targets(plan):
        target_tokens = _tokenize_keywords(target)
        threshold = max(2, min(4, max(1, len(target_tokens) // 2))) if target_tokens else 0
        matched_unit_ids = [
            unit_id
            for unit_id, result in unit_results.items()
            if (result.get("source_ids") or result.get("citations")) and _result_text_covers_item(result, target)
        ]
        grounded_source_ids = sorted(
            {
                source_id
                for unit_id in matched_unit_ids
                for source_id in unit_results.get(unit_id, {}).get("source_ids", [])
            }
        )
        packet_backed_section_ids = [
            section_id
            for section_id, bank in banks_by_section.items()
            if (
                any(str(evidence_id).strip() for evidence_id in bank.get("selected_evidence_ids", []) or [])
                or any(
                    isinstance(packet, dict)
                    and str(packet.get("evidence_id", "")).strip()
                    for packet in bank.get("selected_packets", []) or []
                )
            )
            and (
                any(
                    isinstance(packet, dict)
                    and str(packet.get("evidence_id", "")).strip()
                    for packet in bank.get("selected_packets", []) or []
                )
                or any(str(evidence_id).strip() for evidence_id in bank.get("selected_evidence_ids", []) or [])
            )
            and (
                (
                    section_id in sections_by_id
                    and _count_keyword_overlap(
                        f"{sections_by_id[section_id].title} {sections_by_id[section_id].goal}",
                        target_tokens,
                    )
                    >= threshold
                )
                or any(
                    isinstance(packet, dict)
                    and _count_keyword_overlap(
                        " ".join(str(question_id) for question_id in packet.get("question_ids", []) or []),
                        target_tokens,
                    )
                    >= threshold
                    for packet in bank.get("selected_packets", []) or []
                )
            )
        ]
        items.append(
            {
                "target": target,
                "matched_unit_ids": matched_unit_ids,
                "grounded_source_ids": grounded_source_ids,
                "candidate_section_ids": packet_backed_section_ids,
                "satisfied": bool(matched_unit_ids and grounded_source_ids and packet_backed_section_ids),
            }
        )
    return {
        "items": items,
        "completed_unit_ids": list(completed_unit_ids),
        "failed_unit_ids": list(failed_unit_ids),
        "skipped_unit_ids": list(skipped_unit_ids),
    }


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


def _domain_looks_like_official_docs(domain: str) -> bool:
    normalized = (domain or "").strip().lower()
    if not normalized:
        return False
    if normalized in _OFFICIAL_DOC_HOST_EXACT:
        return True
    return normalized.startswith(_OFFICIAL_DOC_HOST_PREFIXES)


def _source_looks_like_official_docs(source: dict[str, Any]) -> bool:
    if not isinstance(source, dict):
        return False
    source_type = str(source.get("source_type", "") or "").strip().lower()
    if source_type == "official_docs":
        return True
    quality_tier = str(source.get("quality_tier", "") or "").strip().lower()
    if quality_tier == "official":
        return True
    ranking_reasons = {
        str(reason).strip().lower()
        for reason in source.get("ranking_reasons", []) or []
        if str(reason).strip()
    }
    if "official_docs" in ranking_reasons:
        return True
    domain = str(source.get("domain", "") or "").strip().lower()
    if not domain and source.get("url"):
        try:
            domain = urlsplit(str(source["url"])).netloc.lower()
        except Exception:
            domain = ""
    return _domain_looks_like_official_docs(domain)


def _bootstrap_internal_state(
    plan: DeepResearchPlan,
    *,
    unit_results: dict[str, dict[str, Any]],
    evidence_items: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
    updated_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    hydrated_evidence_items = _hydrate_evidence_source_ids(evidence_items, source_registry)
    if not hydrated_evidence_items and sections:
        hydrated_evidence_items = _hydrate_evidence_source_ids(
            _build_carry_forward_evidence(unit_results, sections),
            source_registry,
        )
    ledger_entries: list[dict[str, Any]] = []
    for evidence in hydrated_evidence_items:
        ledger_entries = merge_evidence_ledger(
            ledger_entries,
            build_evidence_ledger_entries(
                plan,
                unit_id=str(evidence.get("unit_id", "")).strip() or "carry-forward",
                origin_query=plan.query,
                evidence_items=[evidence],
                updated_at=updated_at,
            ),
        )
    section_banks = update_section_banks(
        initialize_section_banks(plan, updated_at=updated_at),
        ledger_entries=ledger_entries,
        updated_at=updated_at,
    )
    section_graph = update_section_graph(
        initialize_section_graph(plan, updated_at=updated_at),
        plan=plan,
        source_registry=source_registry,
        selected_evidence_ids_by_section=selected_evidence_ids_by_section(section_banks),
        candidate_evidence_ids_by_section=candidate_evidence_ids_by_section(section_banks),
        rejected_evidence_ids_by_section=rejected_evidence_ids_by_section(section_banks),
        matched_unit_ids_by_section=matched_unit_ids_by_section(ledger_entries),
        evidence_source_ids=evidence_source_ids(ledger_entries),
        updated_at=updated_at,
    )
    return section_graph, ledger_entries, section_banks


def _planner_continuation_payload(continuation: DeepResearchContinuationState) -> dict[str, Any]:
    payload = _continuation_capsule(continuation)
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
    payload["carry_forward_outline_versions"] = [
        {
            "version_id": str(item.get("version_id", "")).strip(),
            "kind": str(item.get("kind", "")).strip(),
            "reason_codes": [str(code).strip() for code in item.get("reason_codes", []) or [] if str(code).strip()],
            "section_titles": [
                str(section.get("title", "")).strip()
                for section in item.get("sections", []) or []
                if isinstance(section, dict) and str(section.get("title", "")).strip()
            ][:6],
        }
        for item in continuation.carry_forward_outline_versions[:2]
        if isinstance(item, dict) and str(item.get("version_id", "")).strip()
    ]
    payload["confirmed_claims"] = list(continuation.confirmed_claims[:4])
    payload["open_questions"] = list(continuation.open_questions[:4])
    payload["trusted_source_headers"] = list(continuation.trusted_source_headers[:4])
    payload["carry_forward_constraints"] = dict(continuation.carry_forward_constraints)
    return payload


def _source_quality_bias(source: dict[str, Any]) -> int:
    url = str(source.get("url", "")).lower()
    domain = str(source.get("domain", "") or "").lower()
    if not domain and "://" in url:
        try:
            domain = urlsplit(url).netloc.lower()
        except Exception:
            domain = ""

    traits = _source_doc_traits(source)
    if url.startswith("https://docs.") or url.startswith("http://docs.") or domain.startswith("docs.") or "/docs/" in url or "/documentation/" in url:
        bias = 3
        if "api_reference" in traits or "reference" in traits:
            bias += 2
        elif "user_guide" in traits:
            bias += 1
        if "prescriptive_guidance" in traits:
            bias -= 2
        if "troubleshooting" in traits:
            bias -= 1
        return bias
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
    traits = _source_doc_traits(source)
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
    reasons.extend(sorted(traits))
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


async def _build_runtime_grok_provider(
    current_model: str = "",
    *,
    effort: str = "standard",
) -> tuple[GrokSearchProvider, dict[str, Any]]:
    from . import server as server_module

    resolved_model = current_model
    if not resolved_model:
        if config._has_explicit_runtime_model():
            resolved_model = config.grok_model
        else:
            try:
                resolved_model = config.resolve_deep_research_model_for_url(config.grok_api_url, effort=effort)
            except ValueError:
                resolved_model = config.grok_model
    provider_chain = config.grok_provider_chain(model_override=resolved_model)
    primary = dict(provider_chain[0])
    available_models, _ = await server_module._get_provider_chain_available_models(provider_chain)
    requested_model = primary["model"]
    preferred_models = config.preferred_deep_research_models_for_url(primary["api_url"], effort=effort)
    resolved_model = next((model for model in preferred_models if model in available_models), None)
    resolution = "preferred_profile_match" if resolved_model and resolved_model != requested_model else None
    if resolved_model is None:
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
        if (
            source_job is not None
            and not _continuation_source_is_recoverable(self.store, source_job, continuation)
        ):
            raise ValueError("continue_from_job_id source is not recoverable")
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
                try:
                    self._read_plan(reused_job.job_id, reused_job)
                except Exception:
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

        job = self.store.create_job(
            query=query,
            request_fingerprint=request_fingerprint,
            status="draft",
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
        planning_updated_at = utc_now_iso()
        if continuation.mode == "continue":
            section_graph, evidence_ledger, section_banks = _bootstrap_internal_state(
                plan,
                unit_results=dict(continuation.carry_forward_unit_results),
                evidence_items=list(continuation.carry_forward_evidence),
                sections=list(continuation.carry_forward_sections),
                source_registry=list(continuation.carry_forward_sources),
                updated_at=planning_updated_at,
            )
        else:
            section_graph = initialize_section_graph(plan, updated_at=planning_updated_at)
            section_banks = initialize_section_banks(plan, updated_at=planning_updated_at)
            evidence_ledger = []
        outline_versions = list(plan.model_dump().get("outline_versions", []) or [])
        self.write_artifact(job.job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
        _write_internal_state_artifacts(
            self,
            job.job_id,
            source_policy=dict(plan.source_policy),
            lineage=_lineage_payload(job=job, continuation=continuation),
            outline_versions=outline_versions,
            section_graph=section_graph,
            evidence_ledger=evidence_ledger,
            section_banks=section_banks,
        )
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
                outline_versions=outline_versions,
                section_graph=section_graph,
                evidence_ledger=evidence_ledger,
                section_banks=section_banks,
            ).model_dump(),
        )
        if continuation.mode == "continue":
            self.write_artifact(
                job.job_id,
                "continuation.json",
                _json_markdown_block(continuation.model_dump()),
                "application/json",
            )
            self.write_artifact(
                job.job_id,
                "continuation_capsule.json",
                _json_markdown_block(_continuation_capsule(continuation)),
                "application/json",
            )
        self.store.append_event(
            job.job_id,
            type="job_created",
            phase="planning",
            message="Deep research job created.",
            data={"plan_only": plan_only, "continuation_mode": continuation.mode},
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
        if not plan_only:
            job = self.store.update_job(
                job.job_id,
                status="queued",
                heartbeat_at=utc_now_iso(),
            )
        job = self.store.get_job(job.job_id)

        if not plan_only and schedule:
            await self._schedule(job.job_id)

        return self._job_payload(job, reused=False)

    async def status(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        payload = self._serialize_job(job)
        final_bundle, unresolved_batch_backed, visibility_reason = _artifact_surface_context(
            self.store,
            job_id,
            job=job,
        )
        artifacts = _artifact_payloads(
            self.store,
            job_id,
            final_bundle=final_bundle,
        )
        if unresolved_batch_backed:
            artifacts = [artifact for artifact in artifacts if artifact.get("kind") not in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS]
        payload["artifact_kinds"] = [artifact["kind"] for artifact in artifacts]
        payload["artifacts"] = artifacts
        payload["artifact_fallback_used"] = _artifact_bundle_differs_from_current(self.store, job_id, final_bundle)
        payload["resolved_artifact_batch_id"] = final_bundle["batch_id"] if final_bundle is not None else ""
        diagnostics = _job_runtime_diagnostics(self.store, job_id, final_bundle=final_bundle)
        payload["planner_fallback_used"] = diagnostics["planner_fallback_used"]
        payload["runtime_warnings"] = diagnostics["runtime_warnings"]
        payload["constraint_violations"] = diagnostics["constraint_violations"]
        payload["artifact_visibility_reason"] = visibility_reason
        operator_summary = _operator_summary_payload(
            job,
            diagnostics=diagnostics,
            final_bundle=final_bundle,
        )
        operator_summary["current_checkpoint_kind"] = payload["current_checkpoint_kind"]
        operator_summary["current_checkpoint_seq"] = payload["current_checkpoint_seq"]
        operator_summary["artifact_fallback_used"] = payload["artifact_fallback_used"]
        operator_summary["artifact_visibility_reason"] = payload["artifact_visibility_reason"]
        payload["operator_summary"] = operator_summary
        return payload

    async def events(self, job_id: str, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        normalized_after_seq = max(0, int(after_seq or 0))
        normalized_limit = max(0, int(limit or 0))
        events = self.store.list_events(job_id, after_seq=normalized_after_seq, limit=normalized_limit)
        job_terminal = self.store.get_job(job_id).status in {"completed", "failed", "canceled", "interrupted"}
        return {
            "job_id": job_id,
            "events": [event.model_dump() for event in events],
            "next_after_seq": events[-1].seq if events else normalized_after_seq,
            "returned_count": len(events),
            "last_event_type": events[-1].type if events else "",
            "window_has_terminal_event": _window_has_terminal_event(events, job_terminal=job_terminal),
            "job_terminal": job_terminal,
        }

    async def result(self, job_id: str, *, include_partial: bool = True) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        current_checkpoint = self.store.get_checkpoint(job_id, job.current_checkpoint) if job.current_checkpoint else None
        plan_text = self.store.read_artifact_text(job_id, "plan.json")
        partial_text = self.store.read_artifact_text(job_id, "partial_report.md") if include_partial else None
        final_bundle, unresolved_batch_backed, visibility_reason = _artifact_surface_context(
            self.store,
            job_id,
            job=job,
        )
        final_text = (
            _read_text_if_exists(final_bundle["paths"]["final_report.md"])
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, "final_report.md")
        )
        citations_text = (
            _read_text_if_exists(final_bundle["paths"]["citations.json"])
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, "citations.json")
        )
        evidence_items_text = (
            _read_batch_artifact_text(final_bundle, _EVIDENCE_ITEMS_ARTIFACT_KIND)
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, _EVIDENCE_ITEMS_ARTIFACT_KIND)
        )
        report_text = (
            _read_text_if_exists(final_bundle["paths"]["report.json"])
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, "report.json")
        )
        sources_text = (
            _read_text_if_exists(final_bundle["paths"]["sources.json"])
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, "sources.json")
        )
        selected_bank_text = (
            _read_batch_artifact_text(final_bundle, _SELECTED_BANK_ARTIFACT_KIND)
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, _SELECTED_BANK_ARTIFACT_KIND)
        )
        evidence_bank_text = (
            _read_batch_artifact_text(final_bundle, _EVIDENCE_BANK_ARTIFACT_KIND)
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, _EVIDENCE_BANK_ARTIFACT_KIND)
        )
        verification_text = (
            _read_batch_artifact_text(final_bundle, _VERIFICATION_ARTIFACT_KIND)
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, _VERIFICATION_ARTIFACT_KIND)
        )
        coverage_gaps_text = (
            _read_batch_artifact_text(final_bundle, _COVERAGE_GAPS_ARTIFACT_KIND)
            if final_bundle is not None
            else None
            if unresolved_batch_backed
            else self.store.read_artifact_text(job_id, _COVERAGE_GAPS_ARTIFACT_KIND)
        )
        artifact_errors: dict[str, str] = {}
        plan_value, plan_error = _safe_load_json_artifact(plan_text)
        report_value, report_error = _safe_load_json_artifact(report_text)
        sources_value, sources_error = _safe_load_json_artifact(sources_text)
        citations_value, citations_error = _safe_load_json_artifact(citations_text)
        evidence_items_value, evidence_items_error = _safe_load_json_artifact(evidence_items_text)
        selected_bank_value, selected_bank_error = _safe_load_json_artifact(selected_bank_text)
        evidence_bank_value, evidence_bank_error = _safe_load_json_artifact(evidence_bank_text)
        verification_value, verification_error = _safe_load_json_artifact(verification_text)
        coverage_gaps_value, coverage_gaps_error = _safe_load_json_artifact(coverage_gaps_text)
        if plan_error:
            artifact_errors["plan.json"] = plan_error
        if report_error:
            artifact_errors["report.json"] = report_error
        if sources_error:
            artifact_errors["sources.json"] = sources_error
        if citations_error:
            artifact_errors["citations.json"] = citations_error
        if evidence_items_error:
            artifact_errors[_EVIDENCE_ITEMS_ARTIFACT_KIND] = evidence_items_error
        if selected_bank_error:
            artifact_errors[_SELECTED_BANK_ARTIFACT_KIND] = selected_bank_error
        if evidence_bank_error:
            artifact_errors[_EVIDENCE_BANK_ARTIFACT_KIND] = evidence_bank_error
        if verification_error:
            artifact_errors[_VERIFICATION_ARTIFACT_KIND] = verification_error
        if coverage_gaps_error:
            artifact_errors[_COVERAGE_GAPS_ARTIFACT_KIND] = coverage_gaps_error
        citations = _normalize_citations_payload(citations_value)
        for kind, value in (
            ("report.json", report_value),
            ("sources.json", sources_value),
            ("citations.json", citations),
            (_EVIDENCE_ITEMS_ARTIFACT_KIND, evidence_items_value),
            (_SELECTED_BANK_ARTIFACT_KIND, selected_bank_value),
            (_EVIDENCE_BANK_ARTIFACT_KIND, evidence_bank_value),
            (_VERIFICATION_ARTIFACT_KIND, verification_value),
            (_COVERAGE_GAPS_ARTIFACT_KIND, coverage_gaps_value),
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
                elif kind == _EVIDENCE_ITEMS_ARTIFACT_KIND:
                    evidence_items_value = None
                elif kind == _SELECTED_BANK_ARTIFACT_KIND:
                    selected_bank_value = None
                elif kind == _EVIDENCE_BANK_ARTIFACT_KIND:
                    evidence_bank_value = None
                elif kind == _VERIFICATION_ARTIFACT_KIND:
                    verification_value = None
                elif kind == _COVERAGE_GAPS_ARTIFACT_KIND:
                    coverage_gaps_value = None
        artifact_errors.update(
            _validate_provenance_bundle(
                report_value=report_value,
                sources_value=sources_value,
                citations_value=citations,
                evidence_items_value=evidence_items_value,
            )
        )
        if final_text is None and not unresolved_batch_backed:
            final_text = _fallback_final_report_text(report_value)
        required = _report_artifact_contract_error(job)
        for kind, error_code in required.items():
            artifact_text = (
                _read_text_if_exists(final_bundle["paths"][kind])
                if final_bundle is not None and kind in final_bundle["paths"]
                else None
                if unresolved_batch_backed
                else self.store.read_artifact_text(job_id, kind)
            )
            if artifact_text is None:
                artifact_errors[kind] = _required_artifact_error_code(kind, visibility_reason)
        diagnostics = _job_runtime_diagnostics(self.store, job_id, final_bundle=final_bundle)
        payload = {
            "job_id": job_id,
            "status": job.status,
            "phase": job.phase,
            "current_checkpoint_kind": _checkpoint_kind(job.current_checkpoint),
            "current_checkpoint_seq": current_checkpoint.checkpoint_seq if current_checkpoint is not None else 0,
            "plan": plan_value,
            "partial_report": partial_text,
            "final_report": final_text,
            "sources": sources_value,
            "citations": citations,
            "evidence_items": evidence_items_value,
            "selected_bank": selected_bank_value,
            "evidence_bank": evidence_bank_value,
            "verification": verification_value,
            "coverage_gaps": coverage_gaps_value,
            "report": report_value,
            "artifact_errors": artifact_errors,
            "artifact_fallback_used": _artifact_bundle_differs_from_current(self.store, job_id, final_bundle),
            "resolved_artifact_batch_id": final_bundle["batch_id"] if final_bundle is not None else "",
            "planner_fallback_used": diagnostics["planner_fallback_used"],
            "runtime_warnings": diagnostics["runtime_warnings"],
            "constraint_violations": diagnostics["constraint_violations"],
            "artifact_visibility_reason": visibility_reason,
            "artifacts": [
                artifact
                for artifact in _artifact_payloads(
                    self.store,
                    job_id,
                    final_bundle=final_bundle,
                )
                if not (unresolved_batch_backed and artifact.get("kind") in _RESOLVED_FINAL_PUBLIC_ARTIFACT_KINDS)
            ],
        }
        payload["operator_summary"] = {
            **_operator_summary_payload(
                job,
                diagnostics=diagnostics,
                final_bundle=final_bundle,
            ),
            "current_checkpoint_kind": payload["current_checkpoint_kind"],
            "current_checkpoint_seq": payload["current_checkpoint_seq"],
            "artifact_fallback_used": payload["artifact_fallback_used"],
            "artifact_visibility_reason": payload["artifact_visibility_reason"],
        }
        return payload

    def read_artifact(self, job_id: str, kind: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        final_bundle, unresolved_batch_backed, visibility_reason = _artifact_surface_context(
            self.store,
            job_id,
            job=job,
        )
        if final_bundle is not None:
            if kind in final_bundle["paths"]:
                return {
                    "content": _read_text_if_exists(final_bundle["paths"][kind]),
                    "state": "available",
                    "artifact_visibility_reason": "",
                }
            batch_text = _read_batch_artifact_text(final_bundle, kind)
            if batch_text is not None:
                return {
                    "content": batch_text,
                    "state": "available",
                    "artifact_visibility_reason": "",
                }
        if unresolved_batch_backed and _artifact_hidden_by_visibility_reason(kind, visibility_reason):
            return {
                "content": None,
                "state": "hidden",
                "artifact_visibility_reason": visibility_reason,
            }
        content = self.store.read_artifact_text(job_id, kind)
        return {
            "content": content,
            "state": "available" if content is not None else "missing",
            "artifact_visibility_reason": "",
        }

    def read_artifact_text(self, job_id: str, kind: str) -> str | None:
        return self.read_artifact(job_id, kind)["content"]

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
            return await self.status(job_id)
        if job.status not in {"draft", "failed", "interrupted"}:
            return await self.status(job_id)
        if job.last_error == "worker_restarted":
            resume_source = "worker_restarted"
        elif job.status == "failed":
            resume_source = "failed_retry"
        elif job.status == "draft":
            resume_source = "draft_execution"
        else:
            resume_source = "interrupted_resume"
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
                return await self.status(job_id)
        checkpoint_state, _ = self._load_checkpoint_state(job)
        completed_units_count = len(checkpoint_state.completed_unit_ids) if checkpoint_state else 0
        next_attempt_count = max(1, job.attempt_count + 1)
        current_checkpoint = self.store.get_checkpoint(job_id, job.current_checkpoint) if job.current_checkpoint else None
        job = self.store.update_job(
            job_id,
            status="queued",
            progress_pct=0.0,
            started_at="",
            finished_at="",
            last_error="",
            cancel_requested=False,
            heartbeat_at=utc_now_iso(),
            attempt_count=next_attempt_count,
        )
        self.store.append_event(
            job_id,
            type="job_resumed",
            phase=job.phase,
            message="Deep research job resumed from checkpoint.",
            data={
                "checkpoint_key": job.current_checkpoint,
                "checkpoint_kind": _checkpoint_kind(job.current_checkpoint),
                "checkpoint_seq": current_checkpoint.checkpoint_seq if current_checkpoint is not None else 0,
                "resume_source": resume_source,
                "attempt_count": next_attempt_count,
                "attempt_id": _attempt_id(next_attempt_count),
                "completed_units_count": completed_units_count,
            },
        )
        if schedule:
            await self._schedule(job_id)
        return await self.status(job_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        job = self.store.get_job(job_id)
        if job.status in {"completed", "failed", "canceled", "interrupted"}:
            if job.cancel_requested:
                job = self.store.update_job(job_id, cancel_requested=False)
            payload = await self.status(job_id)
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
        payload = await self.status(job_id)
        payload["cancel_requested"] = True
        return payload

    async def list_jobs(self, *, status: str = "", limit: int = 50) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        jobs = self.store.list_jobs(status=status, limit=limit)
        return {
            "jobs": [self._serialize_job(job) for job in jobs],
        }

    async def run_job(self, job_id: str) -> dict[str, Any]:
        await self._ensure_startup_reconciled(exclude_job_ids={job_id})
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

    async def _ensure_startup_reconciled(self, *, exclude_job_ids: set[str] | None = None) -> None:
        if self._startup_reconciled:
            return
        recovered_jobs = self.store.reconcile_incomplete_jobs(
            stale_after_seconds=_RUNTIME_RECONCILE_STALE_SECONDS,
            exclude_job_ids=exclude_job_ids,
        )
        for job in recovered_jobs:
            refreshed = self.store.get_job(job.job_id)
            if not _job_prefers_resolved_final_bundle(refreshed):
                continue
            final_bundle = _resolve_final_artifact_bundle(self.store, refreshed.job_id)
            if not _artifact_bundle_is_usable(final_bundle):
                continue
            if refreshed.status == "interrupted" and refreshed.last_error == "worker_restarted":
                refreshed = self.store.update_job(
                    refreshed.job_id,
                    status="completed",
                    phase="finalizing",
                    progress_pct=100.0,
                    finished_at=refreshed.finished_at or utc_now_iso(),
                    heartbeat_at=utc_now_iso(),
                    last_error="",
                )
            elif refreshed.status == "canceled":
                refreshed = self.store.update_job(
                    refreshed.job_id,
                    phase="finalizing",
                    progress_pct=100.0,
                    current_checkpoint="finalizing",
                    finished_at=refreshed.finished_at or utc_now_iso(),
                    heartbeat_at=utc_now_iso(),
                )
            self.store.append_event(
                refreshed.job_id,
                type="job_resolved_from_final_batch",
                phase="finalizing",
                message="Deep research recovered a usable final artifact batch during startup recovery.",
                data={
                    "resolved_artifact_batch_id": final_bundle["batch_id"],
                    "resolved_status": refreshed.status,
                    "recovery_reason": job.last_error or ("cancel_requested_during_recovery" if job.cancel_requested else ""),
                },
            )
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
            baseline_attempt_count = job.attempt_count
            if job.cancel_requested:
                _mark_canceled(self, job_id, job.phase, data={"reason": "cancel_requested_before_start"})
                return
            await self._runner(self, job_id)
        except Exception as exc:
            current_job = self.store.get_job(job_id)
            if current_job.cancel_requested:
                _mark_canceled(self, job_id, current_job.phase, data={"error": str(exc), "reason": "cancel_requested"})
                return
            if (
                current_job.status in {"queued", "interrupted", "canceled", "completed", "failed"}
                or current_job.attempt_count > (baseline_attempt_count + 1)
            ):
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
        raw_plan: dict[str, Any] | None = None
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
            fallback_reason = {"stage": stage, "error": _trim_text(str(exc), limit=280)}
            if stage == "unsafe_plan":
                bounded_salvage = self._build_bounded_salvage_plan(
                    job,
                    continuation,
                    raw_plan=raw_plan,
                    fallback_reason=fallback_reason,
                    planner_trace=getattr(exc, "trace", None),
                )
                if bounded_salvage is not None:
                    return bounded_salvage
            return self._build_fallback_plan(
                job,
                continuation,
                fallback_reason=fallback_reason,
                planner_trace=getattr(exc, "trace", None),
            )

    async def _generate_plan_with_model(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuationState,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        provider, _ = await _build_runtime_grok_provider(effort=job.effort)
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
            "query_repair_details": [],
            "blocked_reasons": [],
            "fallback_used": False,
            "final_status": "generated",
        }
        planner_prompt = (
            "You are planning a deep research job.\n"
            "Return valid JSON only with keys: brief, sub_questions, search_strategy, report_outline, research_units, planner_metadata.\n"
            "Keep the plan lightweight and execution-ready.\n"
            "research_units must be an array of objects with unit_id, unit_type, title, goal, query/url/instructions, depends_on, status, notes.\n"
            "Every search unit must include a non-empty query. Every fetch/map unit must include a non-empty URL unless you intentionally convert it into a search unit.\n"
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
                "continuation_capsule": _continuation_capsule(continuation),
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
        provider, provider_meta = await _build_runtime_grok_provider(effort=job.effort)
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
                        "every search unit has a non-empty query; every fetch/map unit has a non-empty URL or is explicitly downgraded to search; "
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

    def _build_bounded_salvage_plan(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuationState,
        *,
        raw_plan: dict[str, Any] | None,
        fallback_reason: dict[str, Any],
        planner_trace: dict[str, Any] | None = None,
    ) -> DeepResearchPlan | None:
        trace = dict(planner_trace or {})
        if not isinstance(raw_plan, dict):
            return None
        allowed_validation_issues = {
            "missing_search_query",
            "missing_sub_question_unit_coverage",
            "unknown_dependency",
            "generic_outline_for_sub_questions",
        }
        validation_issues = {
            str(item).strip()
            for item in trace.get("validation_issues", []) or []
            if str(item).strip()
        }
        if validation_issues and not validation_issues.issubset(allowed_validation_issues):
            return None
        allowed_action_prefixes = (
            "filled_search_query:",
            "added_sub_question_search_unit:",
            "added_sub_question_search_query:",
            "filtered_low_signal_search_queries",
            "dropped_unknown_dependency:",
            "expanded_outline_from_sub_questions",
            "expanded_outline_from_follow_up_surface",
        )
        normalize_actions = [
            str(item).strip()
            for item in trace.get("normalize_actions", []) or []
            if str(item).strip()
        ]
        query_repair_details = [
            dict(item)
            for item in trace.get("query_repair_details", []) or []
            if isinstance(item, dict)
        ]
        if any(
            str(item.get("source_kind", "")).strip() not in _STRUCTURAL_SAFE_QUERY_REPAIR_SOURCES
            for item in query_repair_details
            if str(item.get("unit_type", "")).strip() == "search"
        ):
            return None
        if not normalize_actions or any(
            not action.startswith(allowed_action_prefixes) for action in normalize_actions
        ):
            return None
        research_units = raw_plan.get("research_units") or []
        if research_units and not all(
            isinstance(unit, dict) and str(unit.get("unit_type", "search")).strip() == "search"
            for unit in research_units
        ):
            return None
        salvage_queries = _bounded_salvage_queries(raw_plan, continuation=continuation)
        if not salvage_queries:
            return None
        trace["salvage_used"] = True
        trace["salvage_query_count"] = len(salvage_queries)
        trace["salvage_queries"] = list(salvage_queries)
        trace["salvage_source_trace"] = {
            "normalize_actions": list(trace.get("normalize_actions") or []),
            "validation_issues": list(trace.get("validation_issues") or []),
            "query_repair_details": list(trace.get("query_repair_details") or []),
            "blocked_reasons": list(trace.get("blocked_reasons") or []),
        }
        trace["normalize_actions"] = []
        trace["validation_issues"] = []
        trace["query_repair_details"] = []
        trace["blocked_reasons"] = []
        trace["final_status"] = "bounded_salvage"
        raw_sub_questions = raw_plan.get("sub_questions") if isinstance(raw_plan, dict) else []
        normalized_sub_questions: list[dict[str, Any]] = []
        salvage_query_by_key = {
            _stable_text_key(item): item
            for item in salvage_queries
            if _stable_text_key(item)
        }
        seen_question_ids: set[str] = set()
        if isinstance(raw_sub_questions, list):
            for index, item in enumerate(raw_sub_questions, start=1):
                if not isinstance(item, dict):
                    continue
                question = _normalize_whitespace(str(item.get("question", "")))
                question_key = _stable_text_key(question)
                if not question_key or question_key not in salvage_query_by_key:
                    continue
                question_id = _normalize_whitespace(str(item.get("id", ""))) or f"sq{index}"
                if question_id in seen_question_ids:
                    continue
                seen_question_ids.add(question_id)
                normalized_sub_questions.append(
                    {
                        "id": question_id,
                        "question": salvage_query_by_key[question_key],
                        "reason": str(item.get("reason") or "Preserve the bounded safe slice from the unsafe planner output.").strip(),
                    }
                )
        for index, item in enumerate(salvage_queries, start=1):
            if any(_stable_text_equivalent(question["question"], item) for question in normalized_sub_questions):
                continue
            question_id = f"sq{index}"
            while question_id in seen_question_ids:
                index += 1
                question_id = f"sq{index}"
            seen_question_ids.add(question_id)
            normalized_sub_questions.append(
                {
                    "id": question_id,
                    "question": item,
                    "reason": "Preserve the bounded safe slice from the unsafe planner output.",
                }
            )
        raw_outline = _normalize_outline_sections_payload(
            list((raw_plan or {}).get("report_outline") or [])
        )
        salvaged_outline = raw_outline or [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
            {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
        ]
        continuation_focus = _sanitize_follow_up_surface_items(
            [
                continuation.continuation_goal,
                *continuation.confirmed_claims,
                *continuation.open_questions,
                *continuation.trusted_source_headers,
            ],
            limit=6,
        )
        if not continuation_focus and _normalize_whitespace(continuation.previous_summary):
            continuation_focus = [continuation.previous_summary]
        raw_plan = {
            "brief": {
                "objective": salvage_queries[0],
                "deliverable": "A structured deep research report with citations.",
                "success_criteria": [
                    "Answer the remaining continuation-specific questions.",
                    "Ground each section in verifiable sources.",
                ],
                "must_cover": list(salvage_queries),
                "out_of_scope": [],
                "preferred_sources": list(job.include_domains),
                "stop_policy": {
                    "stop_on_sufficient_coverage": True,
                    "max_search_queries": max(1, min(len(salvage_queries), config.deep_research_max_concurrency)),
                    "max_urls_per_search": _effort_selective_fetch_limit(job.effort),
                },
                "continuation_focus": continuation_focus,
            },
            "sub_questions": normalized_sub_questions,
            "search_strategy": {
                "approach": "targeted",
                "search_queries": list(salvage_queries),
                "selective_fetch": {
                    "max_urls_per_search": _effort_selective_fetch_limit(job.effort),
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": salvaged_outline,
            "research_units": [
                {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": _trim_text(item, limit=96),
                    "goal": item,
                    "query": item,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
                for index, item in enumerate(
                    salvage_queries[: max(1, config.deep_research_max_concurrency)],
                    start=1,
                )
            ],
            "planner_metadata": {
                "planner": "salvage",
                "used_fallback": False,
                "fallback_reason": fallback_reason,
            },
        }
        return self._normalize_plan_payload(job, raw_plan, continuation, planner_trace=trace)

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
        if job.effort in {"deep", "ultra"}:
            search_queries.append(f"{query} tradeoffs")
        unique_queries: list[str] = []
        seen: set[str] = set()
        for item in search_queries:
            normalized = _normalize_whitespace(item)
            if normalized and normalized not in seen:
                unique_queries.append(normalized)
                seen.add(normalized)
        continuation_focus = _sanitize_follow_up_surface_items(
            [
                continuation.continuation_goal,
                *continuation.confirmed_claims,
                *continuation.open_questions,
                *continuation.trusted_source_headers,
            ],
            limit=6,
        )
        if not continuation_focus and _normalize_whitespace(continuation.previous_summary):
            continuation_focus = [continuation.previous_summary]

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
                    "max_urls_per_search": _effort_selective_fetch_limit(job.effort),
                },
                "continuation_focus": continuation_focus,
            },
            "sub_questions": [
                {"id": f"sq{index}", "question": item, "reason": "Cover the core research surface."}
                for index, item in enumerate(unique_queries[:3], start=1)
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": unique_queries[: max(1, config.deep_research_max_concurrency)],
                "selective_fetch": {
                    "max_urls_per_search": _effort_selective_fetch_limit(job.effort),
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
            "query_repair_details": [],
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
        requested_continuation_focus = _normalize_string_list(
            (raw_plan.get("brief") or {}).get("continuation_focus")
            if isinstance(raw_plan.get("brief"), dict)
            else None
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
                    "_raw_title": str(item.get("title", "")),
                    "_raw_goal": str(item.get("goal", "")),
                    "_raw_query": str(item.get("query", "")),
                    "_raw_notes": str(item.get("notes", "")),
                    "_raw_instructions": str(item.get("instructions", "")),
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
        preliminary_strategy = raw_plan.get("search_strategy") or {}
        preliminary_search_queries = []
        if isinstance(preliminary_strategy, dict):
            raw_search_queries = preliminary_strategy.get("search_queries")
            if isinstance(raw_search_queries, str):
                preliminary_search_queries = _normalize_string_list(raw_search_queries)
            elif isinstance(raw_search_queries, dict):
                preliminary_search_queries = _normalize_string_list(raw_search_queries.values())
            elif isinstance(raw_search_queries, list):
                preliminary_search_queries = _normalize_string_list(raw_search_queries)
            elif raw_search_queries is not None:
                preliminary_search_queries = _normalize_string_list(raw_search_queries)
        query_repair_details: list[dict[str, Any]] = []
        normalized_units = _repair_research_units(
            deduped_units or normalized_units,
            sub_questions=sub_questions,
            search_queries=preliminary_search_queries,
            job_query=job.query,
            continuation=continuation,
            normalize_actions=normalize_actions,
            validation_issues=validation_issues,
            blocked_reasons=planner_trace["blocked_reasons"],
            query_repair_details=query_repair_details,
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
                    "status": _normalize_whitespace(str(item.get("status", "") or "")),
                    "coverage_state": dict(item.get("coverage_state") or {})
                    if isinstance(item.get("coverage_state"), dict)
                    else {},
                    "rewrite_reason": _normalize_whitespace(str(item.get("rewrite_reason", "") or "")),
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
        strategy["search_queries"] = _ensure_sub_question_search_query_coverage(
            _dedupe_preserve_order(
                [
                    _rewrite_research_query(str(query), continuation)
                    for query in [
                        *(strategy.get("search_queries") or [job.query]),
                        *[unit["query"] for unit in normalized_units if unit["unit_type"] == "search" and unit["query"]],
                    ]
                ]
            ),
            sub_questions=sub_questions,
            continuation=continuation,
            normalize_actions=normalize_actions,
        )
        normalized_brief = _finalize_brief_payload(
            normalized_brief,
            job=job,
            continuation=continuation,
            sub_questions=sub_questions,
            strategy=strategy,
        )
        normalized_outline = _outline_from_follow_up_surface(
            normalized_outline,
            continuation_focus=requested_continuation_focus or list(normalized_brief.get("continuation_focus") or []),
            normalize_actions=normalize_actions,
        )
        outline_versions = _normalize_outline_versions(
            raw_plan.get("outline_versions"),
            latest_sections=normalized_outline,
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
        planner_trace["query_repair_details"] = list(
            query_repair_details or planner_trace.get("query_repair_details") or []
        )
        planner_trace["blocked_reasons"] = _dedupe_preserve_order(list(planner_trace.get("blocked_reasons") or []))
        planner_trace["fallback_used"] = bool(planner_metadata.get("used_fallback"))
        planner_trace["fallback_reason"] = planner_metadata.get("fallback_reason")
        unsafe_plan_reason = _unsafe_plan_reason(
            planner=str(planner_metadata.get("planner", "") or ""),
            continuation=continuation,
            normalize_actions=planner_trace["normalize_actions"],
            validation_issues=planner_trace["validation_issues"],
            blocked_reasons=planner_trace["blocked_reasons"],
            query_repair_details=planner_trace["query_repair_details"],
            include_domains=job.include_domains,
            research_units=normalized_units,
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
            "source_policy": _source_policy_from_job(job, continuation=continuation),
            "brief": normalized_brief,
            "sub_questions": sub_questions,
            "search_strategy": strategy,
            "report_outline": normalized_outline,
            "outline_versions": outline_versions,
            "research_units": normalized_units,
            "continuation": _compact_continuation(continuation).model_dump(),
            "planner_metadata": planner_metadata,
        }
        plan = DeepResearchPlan.model_validate(normalized)
        _validate_research_units(plan.research_units)
        return plan

    def _build_continuation_context(self, continue_from_job_id: str) -> DeepResearchContinuationState:
        if not continue_from_job_id:
            return DeepResearchContinuationState(
                mode="fresh",
                compaction_policy="focused_snapshot_v1",
                compaction_reason_codes=["fresh_query"],
                lineage_root_job_id="",
                parent_job_id="",
                resume_from_checkpoint_key="",
                replay_from_checkpoint_key="",
                skipped_unit_ids=[],
            )
        job = self.store.get_job(continue_from_job_id)

        final_bundle = _resolve_final_artifact_bundle(self.store, continue_from_job_id)
        use_final_bundle = _artifact_bundle_is_usable(final_bundle)
        bundle_candidate = (
            _latest_batch_bundle_candidate(self.store, continue_from_job_id)
            if final_bundle is None
            else final_bundle
        )
        bundle_candidate_used = bool(bundle_candidate is not None and not use_final_bundle)
        current_report_text = self.store.read_artifact_text(continue_from_job_id, "report.json") or ""
        current_final_report = self.store.read_artifact_text(continue_from_job_id, "final_report.md") or ""
        current_sources_text = self.store.read_artifact_text(continue_from_job_id, "sources.json") or "[]"
        current_citations_text = self.store.read_artifact_text(continue_from_job_id, "citations.json") or ""
        current_outline_versions_text = self.store.read_artifact_text(continue_from_job_id, _OUTLINE_VERSIONS_ARTIFACT_KIND) or ""
        plan_text = self.store.read_artifact_text(continue_from_job_id, "plan.json") or ""
        checkpoints = self.store.list_checkpoints(continue_from_job_id)
        checkpoint_state, checkpoint_meta = self._load_checkpoint_state(job)
        latest_state = checkpoints[-1].state or {} if checkpoints else {}
        artifact_origin_map: dict[str, str] = {}

        def _continuation_artifact_text(kind: str, current_text: str) -> str:
            if use_final_bundle:
                text = _read_text_if_exists(final_bundle["paths"][kind])
                artifact_origin_map[kind] = "resolved_final_bundle"
                return text or ""
            if bundle_candidate is not None:
                batch_text = _read_batch_artifact_text(bundle_candidate, kind)
                if batch_text:
                    artifact_origin_map[kind] = "latest_batch_candidate"
                    return batch_text
            artifact_origin_map[kind] = "current_artifact"
            return current_text or ""

        report_text = _continuation_artifact_text("report.json", current_report_text)
        final_report = _continuation_artifact_text("final_report.md", current_final_report)
        partial_report = self.store.read_artifact_text(continue_from_job_id, "partial_report.md") or ""
        sources_text = _continuation_artifact_text("sources.json", current_sources_text) or "[]"
        citations_text = _continuation_artifact_text("citations.json", current_citations_text) or ""
        current_evidence_items_text = self.store.read_artifact_text(
            continue_from_job_id, _EVIDENCE_ITEMS_ARTIFACT_KIND
        ) or ""
        evidence_items_text = _continuation_artifact_text(
            _EVIDENCE_ITEMS_ARTIFACT_KIND,
            current_evidence_items_text,
        ) or ""
        outline_versions_text = current_outline_versions_text
        if use_final_bundle and not evidence_items_text:
            evidence_items_text = self.store.read_artifact_text(
                continue_from_job_id,
                _EVIDENCE_ITEMS_ARTIFACT_KIND,
            ) or ""
            artifact_origin_map[_EVIDENCE_ITEMS_ARTIFACT_KIND] = "current_artifact"
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
        carry_forward_outline_versions = _carry_forward_outline_versions(
            plan_payload=plan_payload,
            carry_forward_sections=list(report.get("sections") or []) if isinstance(report.get("sections"), list) else [],
        )
        if outline_versions_text:
            outline_versions_value, outline_versions_error = _safe_load_json_artifact(outline_versions_text)
            if outline_versions_error is None and isinstance(outline_versions_value, list):
                latest_outline_sections = _normalize_outline_sections_payload(
                    list((outline_versions_value[-1] or {}).get("sections") or [])
                ) or _normalize_outline_sections_payload(list(plan_payload.get("report_outline") or []))
                carry_forward_outline_versions = _normalize_outline_versions(
                    outline_versions_value,
                    latest_sections=latest_outline_sections or [],
                )

        carry_forward_sources: list[dict[str, Any]] = []
        current_sources_used = False
        citations_registry_fallback_used = False
        checkpoint_sources_used = False
        latest_state_sources_used = False
        if sources_text:
            sources_value, _ = _safe_load_json_artifact(sources_text)
            if _validate_json_artifact_shape("sources.json", sources_value) is None and isinstance(sources_value, list):
                carry_forward_sources = list(sources_value)
                current_sources_used = True
        if not carry_forward_sources and citations_text:
            citations_value, _ = _safe_load_json_artifact(citations_text)
            normalized_citations = _normalize_citations_payload(citations_value)
            if (
                _validate_json_artifact_shape("citations.json", normalized_citations) is None
                and normalized_citations
                and isinstance(normalized_citations.get("source_registry"), dict)
            ):
                carry_forward_sources = list(normalized_citations["source_registry"].values())
                citations_registry_fallback_used = True

        if not carry_forward_sources and not use_final_bundle and checkpoint_state:
            carry_forward_sources = list(checkpoint_state.sources)
            checkpoint_sources_used = bool(carry_forward_sources)
        elif not carry_forward_sources and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_sources = list(latest_state.get("sources") or [])
            latest_state_sources_used = bool(carry_forward_sources)
        if checkpoint_sources_used:
            artifact_origin_map["sources.json"] = "checkpoint_state"
        elif latest_state_sources_used:
            artifact_origin_map["sources.json"] = "latest_checkpoint_snapshot"
        elif citations_registry_fallback_used:
            artifact_origin_map["sources.json"] = "citations_registry"
        source_count = len(carry_forward_sources)

        carry_forward_sections = list(report.get("sections") or []) if isinstance(report.get("sections"), list) else []
        checkpoint_sections_used = False
        latest_state_sections_used = False
        if not carry_forward_sections and not use_final_bundle and checkpoint_state:
            carry_forward_sections = list(checkpoint_state.sections)
            checkpoint_sections_used = bool(carry_forward_sections)
        elif not carry_forward_sections and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_sections = list(latest_state.get("sections") or [])
            latest_state_sections_used = bool(carry_forward_sections)
        if checkpoint_sections_used:
            artifact_origin_map["sections"] = "checkpoint_state"
        elif latest_state_sections_used:
            artifact_origin_map["sections"] = "latest_checkpoint_snapshot"
        elif carry_forward_sections:
            artifact_origin_map["sections"] = "report.json"

        report_unit_results = report.get("unit_results") if isinstance(report.get("unit_results"), dict) else {}
        carry_forward_unit_results = dict(report_unit_results or {})
        checkpoint_unit_results_used = False
        latest_state_unit_results_used = False
        if not carry_forward_unit_results and not use_final_bundle and checkpoint_state:
            carry_forward_unit_results = dict(checkpoint_state.unit_results)
            checkpoint_unit_results_used = bool(carry_forward_unit_results)
        elif not carry_forward_unit_results and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_unit_results = dict(latest_state.get("unit_results") or {})
            latest_state_unit_results_used = bool(carry_forward_unit_results)
        if checkpoint_unit_results_used:
            artifact_origin_map["unit_results"] = "checkpoint_state"
        elif latest_state_unit_results_used:
            artifact_origin_map["unit_results"] = "latest_checkpoint_snapshot"
        elif carry_forward_unit_results:
            artifact_origin_map["unit_results"] = "report.json"

        carry_forward_evidence = []
        checkpoint_evidence_used = False
        latest_state_evidence_used = False
        if evidence_items_text:
            evidence_value, evidence_error = _safe_load_json_artifact(evidence_items_text)
            if evidence_error is None and isinstance(evidence_value, list):
                carry_forward_evidence = list(evidence_value)
        if not carry_forward_evidence and not use_final_bundle and checkpoint_state:
            carry_forward_evidence = list(checkpoint_state.evidence_items)
            checkpoint_evidence_used = bool(carry_forward_evidence)
        elif not carry_forward_evidence and not use_final_bundle and isinstance(latest_state, dict):
            carry_forward_evidence = list(latest_state.get("evidence_items") or [])
            latest_state_evidence_used = bool(carry_forward_evidence)
        reconstructed_evidence_used = False
        if not carry_forward_evidence:
            carry_forward_evidence = _build_carry_forward_evidence(carry_forward_unit_results, carry_forward_sections)
            reconstructed_evidence_used = bool(carry_forward_evidence)
        if checkpoint_evidence_used:
            artifact_origin_map[_EVIDENCE_ITEMS_ARTIFACT_KIND] = "checkpoint_state"
        elif latest_state_evidence_used:
            artifact_origin_map[_EVIDENCE_ITEMS_ARTIFACT_KIND] = "latest_checkpoint_snapshot"
        elif reconstructed_evidence_used:
            artifact_origin_map[_EVIDENCE_ITEMS_ARTIFACT_KIND] = "reconstructed"
        carry_forward_unit_results = _sanitize_unit_results(carry_forward_unit_results, carry_forward_sources)
        carry_forward_sections = _sanitize_continuation_sections(carry_forward_sections, carry_forward_sources)
        carry_forward_evidence = _sanitize_evidence_items(carry_forward_evidence, carry_forward_sources)
        carry_forward_outline_versions = _append_outline_version_if_changed(
            carry_forward_outline_versions,
            sections=carry_forward_sections,
            kind="continuation_compaction",
            reason_codes=["carry_forward_sections"],
        )
        used_source_ids = _continuation_used_source_ids(
            carry_forward_unit_results,
            carry_forward_evidence,
            carry_forward_sections,
        )
        original_source_ids = {
            str(item.get("source_id", "")).strip()
            for item in carry_forward_sources
            if str(item.get("source_id", "")).strip()
        }
        carry_forward_sources = _focused_continuation_sources(
            carry_forward_sources,
            used_source_ids=used_source_ids,
        )
        focused_source_ids = {
            str(item.get("source_id", "")).strip()
            for item in carry_forward_sources
            if str(item.get("source_id", "")).strip()
        }
        filtered_to_focused_sources = focused_source_ids != original_source_ids
        carry_forward_unit_results = _sanitize_unit_results(carry_forward_unit_results, carry_forward_sources)
        carry_forward_sections = _sanitize_continuation_sections(carry_forward_sections, carry_forward_sources)
        carry_forward_evidence = _sanitize_evidence_items(carry_forward_evidence, carry_forward_sources)
        carry_forward_outline_versions = _append_outline_version_if_changed(
            carry_forward_outline_versions,
            sections=carry_forward_sections,
            kind="continuation_compaction",
            reason_codes=["focused_source_subset"],
        )
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
        skipped_unit_ids = (
            list(checkpoint_state.skipped_unit_ids)
            if checkpoint_state is not None
            else list(latest_state.get("skipped_unit_ids") or [])
            if isinstance(latest_state, dict)
            else []
        )
        if job.continued_from_job_id:
            parent_continuation = self._read_runtime_continuation(job)
            lineage_root_job_id = (
                parent_continuation.lineage_root_job_id
                or parent_continuation.source_job_id
                or continue_from_job_id
            )
        else:
            lineage_root_job_id = continue_from_job_id
        continuation_goal = plan_payload.get("query") or job.query
        source_count = len(carry_forward_sources)
        carry_forward_constraints = _carry_forward_constraints(job=job, plan_payload=plan_payload)
        confirmed_claims = _collect_confirmed_claims(carry_forward_sections)
        open_questions = _collect_continuation_open_questions(report)
        trusted_source_headers = _trusted_source_headers(carry_forward_sources)
        confirmed_claims_filtered = bool(carry_forward_sections) and not bool(confirmed_claims)
        open_questions_filtered = bool(
            isinstance(report.get("coverage"), dict)
            and (report["coverage"].get("uncovered_sub_questions") or report["coverage"].get("unanswered_sections"))
            and not open_questions
        )
        focused_snapshot = _focused_continuation_snapshot(
            source_job_id=continue_from_job_id,
            source_job_status=job.status,
            lineage_root_job_id=lineage_root_job_id,
            parent_job_id=continue_from_job_id,
            checkpoint_key=checkpoint_key,
            resume_from_checkpoint_key=checkpoint_key,
            replay_from_checkpoint_key=checkpoint_key,
            continuation_goal=_trim_text(continuation_goal, limit=200),
            previous_summary=_trim_text(previous_summary, limit=400),
            prior_plan_summary=_trim_text(prior_plan_summary, limit=400),
            confirmed_claims=confirmed_claims,
            open_questions=open_questions,
            trusted_source_headers=trusted_source_headers,
            carry_forward_constraints=carry_forward_constraints,
            skipped_unit_ids=skipped_unit_ids,
            carry_forward_sources=carry_forward_sources,
            carry_forward_outline_versions=carry_forward_outline_versions,
            carry_forward_sections=carry_forward_sections,
            carry_forward_unit_results=carry_forward_unit_results,
        )
        focused_snapshot["artifact_origin_map"] = dict(artifact_origin_map)
        continuation_identity = _continuation_identity_for_source_job(
            focused_snapshot,
        )
        compaction_reason_codes = _continuation_compaction_reason_codes(
            source_job=job,
            use_final_bundle=use_final_bundle,
            bundle_candidate_used=bundle_candidate_used,
            current_sources_used=current_sources_used,
            citations_registry_fallback_used=citations_registry_fallback_used,
            checkpoint_sources_used=checkpoint_sources_used,
            latest_state_sources_used=latest_state_sources_used,
            checkpoint_sections_used=checkpoint_sections_used,
            latest_state_sections_used=latest_state_sections_used,
            checkpoint_unit_results_used=checkpoint_unit_results_used,
            latest_state_unit_results_used=latest_state_unit_results_used,
            checkpoint_evidence_used=checkpoint_evidence_used,
            latest_state_evidence_used=latest_state_evidence_used,
            reconstructed_evidence_used=reconstructed_evidence_used,
            filtered_to_focused_sources=filtered_to_focused_sources,
            confirmed_claims_filtered=confirmed_claims_filtered,
            open_questions_filtered=open_questions_filtered,
        )

        return _hydrate_continuation_snapshot(
            DeepResearchContinuationState(
            mode="continue",
            source_job_id=continue_from_job_id,
            source_job_status=job.status,
            lineage_root_job_id=lineage_root_job_id,
            parent_job_id=continue_from_job_id,
            continuation_identity=continuation_identity,
            compaction_policy="focused_snapshot_v1",
            compaction_reason_codes=compaction_reason_codes,
            focused_snapshot=focused_snapshot,
            previous_summary=_trim_text(previous_summary, limit=400),
            prior_plan_summary=_trim_text(prior_plan_summary, limit=400),
            continuation_goal=_trim_text(continuation_goal, limit=200),
            source_count=source_count,
            checkpoint_key=checkpoint_key,
            resume_from_checkpoint_key=checkpoint_key,
            replay_from_checkpoint_key=checkpoint_key,
            state_version=2,
            confirmed_claims=confirmed_claims,
            open_questions=open_questions,
            trusted_source_headers=trusted_source_headers,
            carry_forward_constraints=carry_forward_constraints,
            skipped_unit_ids=skipped_unit_ids,
            carry_forward_sources=carry_forward_sources,
            carry_forward_outline_versions=carry_forward_outline_versions,
            carry_forward_evidence=carry_forward_evidence,
            carry_forward_sections=carry_forward_sections,
            carry_forward_unit_results=carry_forward_unit_results,
        ))

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
            "include_domains": _stable_string_list(include_domains),
            "exclude_domains": _stable_string_list(exclude_domains),
            "continue_from_job_id": continue_from_job_id.strip(),
            "plan_only": bool(plan_only),
            "continuation_identity": continuation_identity.strip(),
        }
        return hashlib.sha256(_json_markdown_block(payload).encode("utf-8")).hexdigest()

    def _resolve_budget_seconds(self, requested_budget_seconds: int | None, effort: str) -> int:
        if requested_budget_seconds and requested_budget_seconds > 0:
            return min(requested_budget_seconds, config.deep_research_hard_timeout_seconds)
        normalized_effort = (effort or "").strip().lower()
        if normalized_effort == "ultra":
            candidate = int(config.deep_research_default_budget_seconds * 2.5)
        elif normalized_effort == "deep":
            candidate = int(config.deep_research_default_budget_seconds * 1.5)
        else:
            candidate = config.deep_research_default_budget_seconds
        return min(candidate, config.deep_research_hard_timeout_seconds)

    def _job_payload(self, job: DeepResearchJob, *, reused: bool) -> dict[str, Any]:
        payload = self._serialize_job(job)
        final_bundle, _, visibility_reason = _artifact_surface_context(
            self.store,
            job.job_id,
            job=job,
        )
        payload["reused"] = reused
        plan_text = self.store.read_artifact_text(job.job_id, "plan.json")
        plan_value, _ = _safe_load_json_artifact(plan_text)
        payload["plan"] = plan_value
        payload["artifact_fallback_used"] = _artifact_bundle_differs_from_current(self.store, job.job_id, final_bundle)
        payload["resolved_artifact_batch_id"] = final_bundle["batch_id"] if final_bundle is not None else ""
        diagnostics = _job_runtime_diagnostics(self.store, job.job_id, final_bundle=final_bundle)
        payload["planner_fallback_used"] = diagnostics["planner_fallback_used"]
        payload["runtime_warnings"] = diagnostics["runtime_warnings"]
        payload["constraint_violations"] = diagnostics["constraint_violations"]
        payload["artifact_visibility_reason"] = visibility_reason
        return payload

    def _serialize_job(self, job: DeepResearchJob) -> dict[str, Any]:
        payload = job.model_dump()
        current_checkpoint = (
            self.store.get_checkpoint(job.job_id, job.current_checkpoint)
            if job.current_checkpoint
            else None
        )
        payload["attempt_id"] = _attempt_id(job.attempt_count)
        payload["current_checkpoint_kind"] = _checkpoint_kind(job.current_checkpoint)
        payload["current_checkpoint_seq"] = current_checkpoint.checkpoint_seq if current_checkpoint is not None else 0
        return payload

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
                "carry_forward_outline_versions": raw_state.get("outline_versions") or [],
                "carry_forward_evidence": raw_state.get("evidence_items") or [],
                "carry_forward_sections": raw_state.get("sections") or [],
                "carry_forward_unit_results": raw_state.get("unit_results") or {},
            }
        )
        return _hydrate_continuation_snapshot(base)

    def _read_runtime_continuation(self, job: DeepResearchJob) -> DeepResearchContinuationState:
        if not job.continued_from_job_id:
            return DeepResearchContinuationState(mode="fresh")
        continuation_text = self.store.read_artifact_text(job.job_id, "continuation.json")
        if continuation_text:
            try:
                return _hydrate_continuation_snapshot(DeepResearchContinuationState.model_validate(json.loads(continuation_text)))
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
        invalid_checkpoints: list[dict[str, Any]] = []
        if job.current_checkpoint:
            candidates.extend(
                checkpoint
                for checkpoint in checkpoints
                if checkpoint.checkpoint_key == job.current_checkpoint
            )
            candidates = sorted(
                candidates,
                key=lambda checkpoint: (checkpoint.checkpoint_seq, checkpoint.created_at),
                reverse=True,
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
                invalid_checkpoints.append(
                    {
                        "checkpoint_key": checkpoint.checkpoint_key,
                        "checkpoint_seq": checkpoint.checkpoint_seq,
                        "reason": "missing_plan",
                    }
                )
                continue
            try:
                if checkpoint.phase != "planning" and not any(
                    key in raw_state for key in ("completed_unit_ids", "unit_results", "sources", "evidence_items", "sections")
                ):
                    raise ValueError("checkpoint missing runtime state")
                if isinstance(raw_state.get("plan"), dict):
                    frozen_continuation = self._read_runtime_continuation(job)
                    raw_state = {
                        **raw_state,
                        "plan": self._normalize_plan_payload(
                            job,
                            raw_state["plan"],
                            frozen_continuation,
                        ).model_dump(),
                    }
                state = DeepResearchCheckpointState.model_validate(raw_state)
                if invalid_checkpoints or (job.current_checkpoint and checkpoint.checkpoint_key != job.current_checkpoint):
                    return (
                        state,
                        {
                            "fallback_from": job.current_checkpoint or checkpoint.checkpoint_key,
                            "fallback_to": checkpoint.checkpoint_key,
                            "fallback_to_seq": checkpoint.checkpoint_seq,
                            "invalid_checkpoint_keys": invalid_checkpoint_keys,
                            "invalid_checkpoints": invalid_checkpoints,
                        },
                    )
                return state, None
            except Exception as exc:
                reason = "invalid_checkpoint_state"
                if "runtime state" in str(exc):
                    reason = "missing_runtime_state"
                elif "plan" in str(exc):
                    reason = "invalid_plan_state"
                invalid_checkpoint_keys.append(checkpoint.checkpoint_key)
                invalid_checkpoints.append(
                    {
                        "checkpoint_key": checkpoint.checkpoint_key,
                        "checkpoint_seq": checkpoint.checkpoint_seq,
                        "reason": reason,
                    }
                )
                continue
        return None, None


async def _default_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    job = runtime.store.get_job(job_id)
    now_iso = utc_now_iso()
    started_at = job.started_at or now_iso
    next_attempt_count = job.attempt_count if job.attempt_count > 0 else 1
    job = runtime.store.update_job(
        job_id,
        status="running",
        started_at=started_at,
        heartbeat_at=now_iso,
        attempt_count=next_attempt_count,
        last_error="",
    )
    worker_attempt_count = job.attempt_count
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
    current_checkpoint = runtime.store.get_checkpoint(job_id, runtime.store.get_job(job_id).current_checkpoint)
    restored_from_checkpoint = current_checkpoint is not None and current_checkpoint.phase != "planning"
    if restored_from_checkpoint:
        runtime.store.append_event(
            job_id,
            type="checkpoint_restored",
            phase=current_checkpoint.phase,
            message="Deep research restored execution from the latest durable checkpoint.",
            data={
                "checkpoint_key": current_checkpoint.checkpoint_key,
                "checkpoint_kind": _checkpoint_kind(current_checkpoint.checkpoint_key),
                "checkpoint_seq": current_checkpoint.checkpoint_seq,
                "attempt_count": worker_attempt_count,
                "attempt_id": _attempt_id(worker_attempt_count),
                "completed_units_count": len(checkpoint_state.completed_unit_ids) if checkpoint_state else 0,
            },
        )

    if not restored_from_checkpoint:
        runtime.store.append_event(
            job_id,
            type="phase_started",
            phase="planning",
            message="Planning started.",
            data={"unit_count": len(plan.research_units)},
        )
        runtime.store.update_job(job_id, phase="planning", progress_pct=10.0, heartbeat_at=utc_now_iso())

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, current_checkpoint.phase if restored_from_checkpoint and current_checkpoint is not None else "planning")
        return

    continuation = runtime._read_runtime_continuation(job)
    completed_unit_ids = list(checkpoint_state.completed_unit_ids) if checkpoint_state else []
    failed_unit_ids = list(checkpoint_state.failed_unit_ids) if checkpoint_state else []
    failed_units = [dict(item) for item in checkpoint_state.failed_units] if checkpoint_state else []
    skipped_unit_ids = list(checkpoint_state.skipped_unit_ids) if checkpoint_state else []
    skipped_units = [dict(item) for item in checkpoint_state.skipped_units] if checkpoint_state else []
    constraint_violations = (
        [dict(item) for item in checkpoint_state.constraint_violations] if checkpoint_state else []
    )
    coverage_state = dict(checkpoint_state.coverage_state) if checkpoint_state else {}
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
    current_updated_at = utc_now_iso()
    outline_versions = (
        list(checkpoint_state.outline_versions)
        if checkpoint_state and checkpoint_state.outline_versions
        else list(plan.model_dump().get("outline_versions", []) or continuation.carry_forward_outline_versions)
    )
    if checkpoint_state and checkpoint_state.section_graph and checkpoint_state.section_banks:
        section_graph = dict(checkpoint_state.section_graph)
        evidence_ledger = list(checkpoint_state.evidence_ledger)
        section_banks = list(checkpoint_state.section_banks)
    else:
        section_graph, evidence_ledger, section_banks = _bootstrap_internal_state(
            plan,
            unit_results=unit_results,
            evidence_items=evidence_items,
            sections=sections,
            source_registry=source_registry,
            updated_at=current_updated_at,
        )
    _write_internal_state_artifacts(
        runtime,
        job_id,
        source_policy=dict(plan.source_policy),
        lineage=_lineage_payload(job=job, continuation=continuation),
        outline_versions=outline_versions,
        section_graph=section_graph,
        evidence_ledger=evidence_ledger,
        section_banks=section_banks,
    )
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
    allow_one_post_resume_batch = bool(restored_from_checkpoint and completed_unit_ids)
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
        if elapsed_seconds >= job.resolved_budget_seconds and not allow_one_post_resume_batch:
            _write_partial_outputs(runtime, job_id, plan, completed_unit_ids, unit_results, sections)
            latest_checkpoint_key = (
                f"researching-{completed_unit_ids[-1]}"
                if completed_unit_ids
                else runtime.store.get_job(job_id).current_checkpoint
            )
            latest_checkpoint = (
                runtime.store.get_checkpoint(job_id, latest_checkpoint_key)
                if latest_checkpoint_key
                else None
            )
            runtime.store.update_job(
                job_id,
                status="interrupted",
                phase="researching",
                last_error="time_budget_exceeded",
                current_checkpoint=latest_checkpoint_key,
                finished_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
            )
            runtime.store.append_event(
                job_id,
                type="job_interrupted",
                phase="researching",
                message="Deep research paused after reaching the time budget.",
                data={
                    "reason": "time_budget_exceeded",
                    "checkpoint_key": latest_checkpoint_key,
                    "checkpoint_kind": _checkpoint_kind(latest_checkpoint_key),
                    "checkpoint_seq": latest_checkpoint.checkpoint_seq if latest_checkpoint is not None else 0,
                    "attempt_count": worker_attempt_count,
                    "attempt_id": _attempt_id(worker_attempt_count),
                    "completed_units_count": len(completed_unit_ids),
                },
            )
            return

        batch_units = ready_units[: max(1, config.deep_research_max_concurrency)]
        dispatch_checkpoint_key = f"researching-dispatch-a{worker_attempt_count}-{batch_units[0].unit_id}"
        dispatch_state = _checkpoint_state_payload(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            failed_unit_ids=failed_unit_ids,
            failed_units=failed_units,
            skipped_unit_ids=skipped_unit_ids,
            skipped_units=skipped_units,
            constraint_violations=constraint_violations,
            coverage_state=coverage_state,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
            outline_versions=outline_versions,
            section_graph=section_graph,
            evidence_ledger=evidence_ledger,
            section_banks=section_banks,
        ).model_dump()
        dispatch_state["dispatched_unit_ids"] = [unit.unit_id for unit in batch_units]
        runtime.store.save_checkpoint(
            job_id,
            phase="researching",
            checkpoint_key=dispatch_checkpoint_key,
            state=dispatch_state,
        )
        runtime.store.update_job(job_id, current_checkpoint=dispatch_checkpoint_key, heartbeat_at=utc_now_iso())
        batch_results = await asyncio.gather(
            *[_execute_research_unit(runtime, plan, unit) for unit in batch_units],
            return_exceptions=True,
        )
        allow_one_post_resume_batch = False
        if _job_execution_is_stale(runtime, job_id, attempt_count=worker_attempt_count):
            return
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
            removed_source_count = max(0, len(new_sources) - len(constrained_sources))
            if removed_source_count > 0:
                constraint_violations.append(
                    {
                        "unit_id": unit.unit_id,
                        "removed_source_count": removed_source_count,
                        "reason": "domain_constraints_applied",
                    }
                )
            if removed_source_count > 0 and not constrained_sources:
                unit_result = {
                    **unit_result,
                    "summary": "",
                    "detail": "",
                }
                if unit.unit_type == "search":
                    new_evidence = []
            if unit.unit_type in {"fetch", "map", "search"} and not unit_result.get("summary") and not constrained_sources and not new_evidence:
                error_code = f"empty_{unit.unit_type}_result_after_constraints" if unit.unit_type == "search" else f"empty_{unit.unit_type}_result"
                failed_unit_ids.append(unit.unit_id)
                failed_units.append(
                    {
                        "unit_id": unit.unit_id,
                        "unit_type": unit.unit_type,
                        "reason": error_code,
                    }
                )
                runtime.store.append_event(
                    job_id,
                    type="research_unit_failed",
                    phase="researching",
                    message=f"Failed {unit.unit_id}.",
                    data={"unit_type": unit.unit_type, "error": error_code},
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
            ledger_entries = build_evidence_ledger_entries(
                plan,
                unit_id=unit.unit_id,
                origin_query=unit.query or unit.goal,
                evidence_items=new_evidence,
                updated_at=utc_now_iso(),
            )
            evidence_ledger = merge_evidence_ledger(evidence_ledger, ledger_entries)
            section_banks = update_section_banks(
                section_banks,
                ledger_entries=ledger_entries,
                updated_at=utc_now_iso(),
            )
            section_graph = update_section_graph(
                section_graph,
                plan=plan,
                source_registry=source_registry,
                selected_evidence_ids_by_section=selected_evidence_ids_by_section(section_banks),
                candidate_evidence_ids_by_section=candidate_evidence_ids_by_section(section_banks),
                rejected_evidence_ids_by_section=rejected_evidence_ids_by_section(section_banks),
                matched_unit_ids_by_section=matched_unit_ids_by_section(evidence_ledger),
                evidence_source_ids=evidence_source_ids(evidence_ledger),
                updated_at=utc_now_iso(),
            )
            unit_results = _sanitize_unit_results(unit_results, source_registry)
            evidence_items = _sanitize_evidence_items(evidence_items, source_registry)
            _write_internal_state_artifacts(
                runtime,
                job_id,
                source_policy=dict(plan.source_policy),
                lineage=_lineage_payload(job=job, continuation=continuation),
                outline_versions=outline_versions,
                section_graph=section_graph,
                evidence_ledger=evidence_ledger,
                section_banks=section_banks,
            )
            coverage_state = _runtime_coverage_state(
                plan,
                unit_results,
                completed_unit_ids=completed_unit_ids,
                failed_unit_ids=failed_unit_ids,
                skipped_unit_ids=skipped_unit_ids,
                section_banks=section_banks,
            )
            checkpoint_state = _checkpoint_state_payload(
                plan=plan,
                completed_unit_ids=completed_unit_ids,
                failed_unit_ids=failed_unit_ids,
                failed_units=failed_units,
                skipped_unit_ids=skipped_unit_ids,
                skipped_units=skipped_units,
                constraint_violations=constraint_violations,
                coverage_state=coverage_state,
                unit_results=unit_results,
                sources=source_registry,
                evidence_items=evidence_items,
                sections=sections,
                outline_versions=outline_versions,
                section_graph=section_graph,
                evidence_ledger=evidence_ledger,
                section_banks=section_banks,
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
            and _has_sufficient_runtime_coverage(plan, unit_results, coverage_state=coverage_state)
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

    if _job_execution_is_stale(runtime, job_id, attempt_count=worker_attempt_count):
        return
    active_outline = build_synthesis_outline(
        plan,
        section_graph=section_graph,
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
    )
    section_banks = seed_section_banks_from_question_bindings(
        section_banks,
        planned_outline=active_outline,
        ledger_entries=evidence_ledger,
        updated_at=utc_now_iso(),
    )
    outline_versions = _append_outline_version_if_changed(
        outline_versions,
        sections=active_outline,
        kind="evidence_rewrite",
        reason_codes=["active_outline"],
        created_at=utc_now_iso(),
    )
    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
        section_graph=section_graph,
        planned_outline=active_outline,
    )
    section_graph = _reconcile_section_graph_with_materialized_sections(
        section_graph,
        planned_outline=active_outline,
        sections=sections,
        updated_at=utc_now_iso(),
    )
    source_registry = _annotate_source_usage(
        source_registry,
        sections,
        reference_texts=[
            plan.query,
            *(
                f"{str(section.get('title', '')).strip()} {str(section.get('goal', '')).strip()}"
                for section in active_outline
                if isinstance(section, dict)
            ),
        ],
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
    coverage_state = _runtime_coverage_state(
        plan,
        unit_results,
        completed_unit_ids=completed_unit_ids,
        failed_unit_ids=failed_unit_ids,
        skipped_unit_ids=skipped_unit_ids,
        section_banks=section_banks,
    )
    runtime.store.save_checkpoint(
        job_id,
        phase="synthesizing",
        checkpoint_key="synthesizing",
        state=_checkpoint_state_payload(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            failed_unit_ids=failed_unit_ids,
            failed_units=failed_units,
            skipped_unit_ids=skipped_unit_ids,
            skipped_units=skipped_units,
            constraint_violations=constraint_violations,
            coverage_state=coverage_state,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
            outline_versions=outline_versions,
            section_graph=section_graph,
            evidence_ledger=evidence_ledger,
            section_banks=section_banks,
        ).model_dump(),
    )

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, "synthesizing")
        return
    if _job_execution_is_stale(runtime, job_id, attempt_count=worker_attempt_count):
        return

    citations = {
        "source_registry": {item["source_id"]: item for item in source_registry},
        "sections": _sanitize_sections(sections, source_registry),
    }
    evidence_ledger = _attach_materialized_claim_ids_to_evidence_ledger(
        evidence_ledger,
        citations["sections"],
    )
    section_banks = sync_section_banks_to_outline(
        section_banks,
        planned_outline=active_outline,
        updated_at=utc_now_iso(),
    )
    section_banks = update_section_banks(
        section_banks,
        ledger_entries=evidence_ledger,
        updated_at=utc_now_iso(),
    )
    section_banks = reconcile_section_banks_with_materialized_sections(
        section_banks,
        sections=citations["sections"],
        updated_at=utc_now_iso(),
    )
    citations["sections"] = _reconcile_section_bindings_with_section_banks(
        citations["sections"],
        section_banks=section_banks,
    )
    runtime_warnings = sorted({warning for result in unit_results.values() for warning in result.get("warnings", [])})
    report_coverage = _coverage_for_report(
        plan,
        citations["sections"],
        coverage_state=coverage_state,
        planned_outline=active_outline,
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
    )
    coverage_diagnostics = {
        "query": plan.query,
        "must_cover": list(plan.brief.must_cover),
        "coverage_checklist": list(plan.brief.coverage_checklist),
        "coverage_state": coverage_state,
        **report_coverage,
    }
    grounding_diagnostics = _build_grounding_diagnostics(citations["sections"], citations["source_registry"])
    verifier_diagnostics = _build_verifier_diagnostics(
        coverage=report_coverage,
        grounding=grounding_diagnostics,
        sections=citations["sections"],
        source_registry=citations["source_registry"],
        evidence_items=evidence_items,
        section_banks=section_banks,
    )
    citations["sections"] = _rebuild_verified_rollup_sections(
        citations["sections"],
        verifier_diagnostics,
    )
    grounding_diagnostics = _build_grounding_diagnostics(citations["sections"], citations["source_registry"])
    verifier_diagnostics = _build_verifier_diagnostics(
        coverage=report_coverage,
        grounding=grounding_diagnostics,
        sections=citations["sections"],
        source_registry=citations["source_registry"],
        evidence_items=evidence_items,
        section_banks=section_banks,
    )
    release_gate = _build_release_gate(
        report_coverage,
        grounding_diagnostics,
        verifier_diagnostics,
        source_policy=plan.source_policy,
    )
    runtime_warnings = sorted(
        {
            *runtime_warnings,
            *_coverage_warning_codes(report_coverage),
            *release_gate["reason_codes"],
            *release_gate.get("soft_reason_codes", []),
        }
    )
    if plan.planner_metadata.get("used_fallback"):
        runtime_warnings = sorted({*runtime_warnings, "planner_fallback_used"})
    if constraint_violations:
        runtime_warnings = sorted({*runtime_warnings, "domain_constraints_applied"})
    report_status = "failed" if not release_gate["passed"] else ("degraded" if runtime_warnings or not citations["sections"] or failed_units else "completed")
    report_confidence = _cluster_confidence(
        source_count=len({citation for section in citations["sections"] for citation in section.get("citations", [])}),
        evidence_count=sum(len(section.get("claims", [])) for section in citations["sections"]),
    )
    report_summary = _build_report_summary(plan, citations["sections"], confidence=report_confidence)
    selected_bank = _selected_bank_payload(
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
        sections=citations["sections"],
    )
    evidence_bank = _evidence_bank_payload(
        evidence_items=evidence_items,
        sections=citations["sections"],
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
            "source_policy": dict(plan.source_policy),
            "coverage": {
                "must_cover_count": len(plan.brief.must_cover),
                "uncovered_sub_question_count": len(report_coverage.get("uncovered_sub_questions", [])),
                "unanswered_section_count": len(report_coverage.get("unanswered_sections", [])),
                "hard_uncovered_target_count": len(report_coverage.get("hard_uncovered_targets", [])),
            },
            "grounding": {
                "total_claims": grounding_diagnostics["total_claims"],
                "ungrounded_claims": grounding_diagnostics["ungrounded_claims"],
                "single_source_claims": grounding_diagnostics["single_source_claims"],
                "missing_evidence_binding_claims": grounding_diagnostics["missing_evidence_binding_claims"],
            },
            "verifier": verifier_diagnostics,
            "release_gate": release_gate,
            "constraint_violations": constraint_violations,
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
    verification = _verification_payload(
        sections=citations["sections"],
        coverage=report_coverage,
        verifier=verifier_diagnostics,
        selected_bank=selected_bank,
        final_report=final_report,
    )
    coverage_gaps = _coverage_gaps_payload(
        query=plan.query,
        coverage=report_coverage,
    )
    report["runtime"]["verification"] = verification
    section_graph = _reconcile_section_graph_with_materialized_sections(
        section_graph,
        planned_outline=active_outline,
        sections=citations["sections"],
        updated_at=utc_now_iso(),
    )
    outline_versions = _append_outline_version_if_changed(
        outline_versions,
        sections=active_outline,
        kind="materialized_outline",
        reason_codes=["synthesized_sections"],
        created_at=utc_now_iso(),
    )
    _write_internal_state_artifacts(
        runtime,
        job_id,
        source_policy=dict(plan.source_policy),
        lineage=_lineage_payload(job=job, continuation=continuation),
        outline_versions=outline_versions,
        section_graph=section_graph,
        evidence_ledger=evidence_ledger,
        section_banks=section_banks,
    )
    runtime.write_artifact(job_id, "coverage.json", _json_markdown_block(coverage_diagnostics), "application/json")
    runtime.write_artifact(job_id, "grounding.json", _json_markdown_block(grounding_diagnostics), "application/json")
    runtime.write_artifact(job_id, "verifier.json", _json_markdown_block(verifier_diagnostics), "application/json")
    runtime.write_artifact(job_id, _SELECTED_BANK_ARTIFACT_KIND, _json_markdown_block(selected_bank), "application/json")
    runtime.write_artifact(job_id, _EVIDENCE_BANK_ARTIFACT_KIND, _json_markdown_block(evidence_bank), "application/json")
    runtime.write_artifact(job_id, _VERIFICATION_ARTIFACT_KIND, _json_markdown_block(verification), "application/json")
    runtime.write_artifact(job_id, _COVERAGE_GAPS_ARTIFACT_KIND, _json_markdown_block(coverage_gaps), "application/json")
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
            {
                "kind": _EVIDENCE_ITEMS_ARTIFACT_KIND,
                "content": _json_markdown_block(evidence_items),
                "content_type": "application/json",
            },
            {
                "kind": "coverage.json",
                "content": _json_markdown_block(coverage_diagnostics),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": _json_markdown_block(grounding_diagnostics),
                "content_type": "application/json",
            },
            {
                "kind": "verifier.json",
                "content": _json_markdown_block(verifier_diagnostics),
                "content_type": "application/json",
            },
            {
                "kind": _EVIDENCE_BANK_ARTIFACT_KIND,
                "content": _json_markdown_block(evidence_bank),
                "content_type": "application/json",
            },
            {
                "kind": _SELECTED_BANK_ARTIFACT_KIND,
                "content": _json_markdown_block(selected_bank),
                "content_type": "application/json",
            },
            {
                "kind": _VERIFICATION_ARTIFACT_KIND,
                "content": _json_markdown_block(verification),
                "content_type": "application/json",
            },
            {
                "kind": _COVERAGE_GAPS_ARTIFACT_KIND,
                "content": _json_markdown_block(coverage_gaps),
                "content_type": "application/json",
            },
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
        state=_checkpoint_state_payload(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            failed_unit_ids=failed_unit_ids,
            failed_units=failed_units,
            skipped_unit_ids=skipped_unit_ids,
            skipped_units=skipped_units,
            constraint_violations=constraint_violations,
            coverage_state=coverage_state,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
            outline_versions=outline_versions,
            section_graph=section_graph,
            evidence_ledger=evidence_ledger,
            section_banks=section_banks,
        ).model_dump(),
    )
    if _job_execution_is_stale(runtime, job_id, attempt_count=worker_attempt_count):
        return
    terminal_status = "completed" if release_gate["passed"] else "failed"
    runtime.store.update_job(
        job_id,
        status=terminal_status,
        phase="finalizing",
        progress_pct=100.0,
        finished_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
        last_error="" if release_gate["passed"] else ",".join(release_gate["reason_codes"]),
    )
    runtime.store.append_event(
        job_id,
        type="job_completed" if release_gate["passed"] else "job_failed",
        phase="finalizing",
        message="Deep research completed." if release_gate["passed"] else "Deep research failed release gate.",
        data={
            "sources_count": len(source_registry),
            "artifact_batch_id": artifact_batch_id,
            "release_gate": release_gate,
        },
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


def _job_execution_is_stale(runtime: DeepResearchRuntime, job_id: str, *, attempt_count: int) -> bool:
    current_job = runtime.store.get_job(job_id)
    if current_job.attempt_count != attempt_count:
        return True
    return current_job.status in {"interrupted", "canceled", "completed", "failed"}


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
        summary_text, summary_line_start, summary_line_end = _extract_relevant_excerpt_with_span(
            fetched,
            reference_texts=reference_texts,
            line_limit=4,
            char_limit=_MAX_CLAIM_LENGTH,
        )
        detail, detail_line_start, detail_line_end = _extract_relevant_excerpt_with_span(
            fetched,
            reference_texts=reference_texts,
            line_limit=8,
            char_limit=1200,
            multiline=True,
        )
        summary = _trim_text(summary_text, limit=180)
        line_start = summary_line_start or detail_line_start
        line_end = detail_line_end or summary_line_end
        source = {"url": unit.url, "title": unit.title}
        return (
            {"summary": summary, "detail": detail},
            [source],
            [
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}",
                    unit_id=unit.unit_id,
                    source_urls=[unit.url] if unit.url else [],
                    summary=summary_text,
                    detail=detail,
                    evidence_kind="fetch",
                    weight=1.0,
                    derived_from_source_url=unit.url,
                    line_start=line_start,
                    line_end=line_end,
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
        summary_text, summary_line_start, summary_line_end = _extract_relevant_excerpt_with_span(
            mapped,
            reference_texts=reference_texts,
            line_limit=4,
            char_limit=_MAX_CLAIM_LENGTH,
        )
        detail, detail_line_start, detail_line_end = _extract_relevant_excerpt_with_span(
            mapped,
            reference_texts=reference_texts,
            line_limit=8,
            char_limit=1200,
            multiline=True,
        )
        line_start = summary_line_start or detail_line_start
        line_end = detail_line_end or summary_line_end
        sources = [{"url": unit.url, "title": unit.title}]
        evidence_items = [
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-map",
                unit_id=unit.unit_id,
                source_urls=[unit.url] if unit.url else [],
                summary=summary_text,
                detail=detail,
                evidence_kind="map",
                weight=0.6,
                derived_from_source_url=unit.url,
                line_start=line_start,
                line_end=line_end,
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
            candidate_summary, candidate_line_start, candidate_line_end = _extract_relevant_excerpt_with_span(
                fetched,
                reference_texts=reference_texts + [candidate_url, fetched_source.get("title", "")],
                line_limit=4,
                char_limit=_MAX_CLAIM_LENGTH,
            )
            candidate_detail, candidate_detail_start, candidate_detail_end = _extract_relevant_excerpt_with_span(
                fetched,
                reference_texts=reference_texts + [candidate_url, fetched_source.get("title", "")],
                line_limit=8,
                char_limit=1200,
                multiline=True,
            )
            evidence_items.append(
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}-fetch-{len(evidence_items)}",
                    unit_id=unit.unit_id,
                    source_urls=[candidate_url],
                    summary=candidate_summary,
                    detail=candidate_detail,
                    evidence_kind="fetch",
                    weight=1.0,
                    derived_from_source_url=candidate_url,
                    line_start=candidate_line_start or candidate_detail_start,
                    line_end=candidate_detail_end or candidate_line_end,
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
        search_result = await _search_query_with_details(unit.query or unit.goal, effort=plan.effort)
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
    answer_summary, search_line_start, search_line_end = _extract_relevant_excerpt_with_span(
        search_result["answer"],
        reference_texts=reference_texts,
        line_limit=4,
        char_limit=_MAX_CLAIM_LENGTH,
    )
    answer_detail, search_detail_start, search_detail_end = _extract_relevant_excerpt_with_span(
        search_result["answer"],
        reference_texts=reference_texts,
        line_limit=8,
        char_limit=1200,
        multiline=True,
    )
    selective_fetch = plan.search_strategy.selective_fetch
    fetch_limit = max(0, selective_fetch.max_urls_per_search)
    constrained_sources_for_grounding = _apply_domain_constraints(
        sources,
        include_domains=plan.include_domains,
        exclude_domains=plan.exclude_domains,
    )
    selected_sources_for_grounding = _select_fetch_sources(
        constrained_sources_for_grounding or sources,
        plan,
        unit,
    )
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
                line_start=search_line_start or search_detail_start,
                line_end=search_detail_end or search_line_end,
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
        fetched_summary, fetched_line_start, fetched_line_end = _extract_relevant_excerpt_with_span(
            fetched,
            reference_texts=reference_texts + [source["url"], enriched_source.get("title", "")],
            line_limit=4,
            char_limit=_MAX_CLAIM_LENGTH,
        )
        fetched_detail, fetched_detail_start, fetched_detail_end = _extract_relevant_excerpt_with_span(
            fetched,
            reference_texts=reference_texts + [source["url"], enriched_source.get("title", "")],
            line_limit=8,
            char_limit=1200,
            multiline=True,
        )
        fetched_evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-fetch-{len(evidence_items) + len(fetched_evidence_items)}",
                unit_id=unit.unit_id,
                source_urls=[source["url"]],
                summary=fetched_summary,
                detail=fetched_detail,
                evidence_kind="fetch",
                weight=1.0,
                derived_from_source_url=source["url"],
                line_start=fetched_line_start or fetched_detail_start,
                line_end=fetched_detail_end or fetched_line_end,
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
    query_keywords = _tokenize_keywords(f"{unit.title} {unit.goal} {unit.query}")
    query_identifier_terms = _query_identifier_terms(f"{unit.title} {unit.goal} {unit.query}")

    query_intent_text = f"{unit.title} {unit.goal} {unit.query}".lower()
    troubleshooting_intent = any(
        keyword in query_intent_text
        for keyword in ("troubleshooting", "troubleshoot", "support", "error", "issue", "failure")
    )
    generic_query_terms = {
        "aws",
        "dms",
        "checkpoint",
        "checkpoints",
        "resume",
        "restart",
        "semantics",
        "behavior",
        "task",
        "tasks",
        "replication",
        "processing",
        "service",
        "services",
        "database",
    }
    candidate_texts = [
        f"{source.get('title', '')} {source.get('description', '')} {source.get('url', '')}"
        for source in sources
    ]
    distinctive_query_terms = [
        term
        for term in query_keywords
        if term not in generic_query_terms
        and sum(1 for text in candidate_texts if term in set(_tokenize_keywords(text))) == 1
    ]

    def score(source: dict[str, Any]) -> tuple[int, int, int, int, int, int, int, int, int, str]:
        title = f"{source.get('title', '')} {source.get('description', '')} {source.get('url', '')}"
        quality_bias = _source_quality_bias(source)
        lowered_title = title.lower()
        traits = _source_doc_traits(source)
        namespace_priority = _docs_aws_namespace_priority(
            source,
            [plan.query, unit.title, unit.goal, unit.query, *[section.goal for section in plan.report_outline]],
        )
        trait_priority = 0
        if "api_reference" in traits or "reference" in traits:
            trait_priority = 2
        elif "user_guide" in traits:
            trait_priority = 1
        elif "prescriptive_guidance" in traits:
            trait_priority = -1
        identifier_match_count = sum(1 for term in query_identifier_terms if term and term in lowered_title)
        non_shell_distinctive_match_count = 0
        if "prescriptive_guidance" not in traits and "troubleshooting" not in traits:
            non_shell_distinctive_match_count = _count_keyword_overlap(title, distinctive_query_terms)
        substring_query_overlap = sum(1 for keyword in query_keywords if keyword and keyword in lowered_title)
        shell_penalty = 0
        if not troubleshooting_intent and "troubleshooting" in lowered_title:
            shell_penalty -= 2
        if not troubleshooting_intent and "support" in lowered_title:
            shell_penalty -= 1
        if "background" in lowered_title:
            shell_penalty -= 2
        if "prescriptive_guidance" in traits:
            shell_penalty -= 3
        if _is_low_signal_title(str(source.get("title") or "")):
            shell_penalty -= 2
        return (
            namespace_priority,
            non_shell_distinctive_match_count,
            trait_priority,
            identifier_match_count,
            quality_bias,
            _count_keyword_overlap(title, outline_keywords) if prefer_outline else 0,
            substring_query_overlap,
            _count_keyword_overlap(title, query_keywords),
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
    *,
    coverage_state: dict[str, Any] | None = None,
    planned_outline: list[dict[str, Any]] | None = None,
    section_banks: list[dict[str, Any]] | None = None,
    evidence_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def _section_question_ids(section: dict[str, Any]) -> list[str]:
        explicit_ids = [
            str(question_id).strip()
            for question_id in (
                list(section.get("question_ids", []) or [])
                + ([section.get("question_id", "")] if section.get("question_id") else [])
            )
            if str(question_id).strip()
        ]
        mapped_id = question_id_by_section_id.get(str(section.get("section_id", "")).strip(), "")
        if mapped_id:
            explicit_ids.append(mapped_id)
        return _dedupe_preserve_order(explicit_ids)

    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks or []
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    explicit_question_id_by_section_id = {
        str(section.get("section_id", "")).strip(): str(section.get("question_id", "")).strip()
        for section in (planned_outline or [])
        if isinstance(section, dict)
        and str(section.get("section_id", "")).strip()
        and str(section.get("question_id", "")).strip()
    }
    question_id_by_section_id = {
        _slugify(_trim_text(_normalize_whitespace(item.question.rstrip(" ?")), limit=96)): item.id
        for item in plan.sub_questions
    }
    question_id_by_section_id.update(explicit_question_id_by_section_id)
    ledger_by_question_id: dict[str, list[dict[str, Any]]] = {}
    for entry in evidence_ledger or []:
        if not isinstance(entry, dict):
            continue
        for question_id in _entry_question_ids(entry):
            ledger_by_question_id.setdefault(question_id, []).append(entry)
    answered_section_ids: list[str] = []
    covered_sub_question_ids: list[str] = []
    uncovered_sub_questions: list[str] = []
    sub_question_coverage: list[dict[str, Any]] = []
    grounded_sections: list[dict[str, Any]] = []
    section_coverage: list[dict[str, Any]] = []
    hard_target_coverage: list[dict[str, Any]] = []
    hard_uncovered_targets: list[str] = []
    for section in sections:
        section_id = str(section.get("section_id", "")).strip()
        bank = banks_by_section.get(section_id, {})
        grounded_claims = [
            claim
            for claim in section.get("claims", [])
            if [citation for citation in claim.get("citations", []) if str(citation).strip()]
        ]
        grounded_citations = sorted(
            {
                str(citation).strip()
                for claim in grounded_claims
                for citation in claim.get("citations", [])
                if str(citation).strip()
            }
        )
        if grounded_claims and section_id:
            answered_section_ids.append(section_id)
        section_support = [_claim_support_details(claim) for claim in grounded_claims if isinstance(claim, dict)]
        section_claim_ids = _dedupe_preserve_order(
            [item["claim_id"] for item in section_support if item.get("claim_id")]
        )
        section_evidence_ids = _dedupe_preserve_order(
            [
                evidence_id
                for item in section_support
                for evidence_id in item.get("evidence_ids", [])
                if evidence_id
            ]
        )
        section_source_ids = _dedupe_preserve_order(
            [
                source_id
                for item in section_support
                for source_id in item.get("source_ids", [])
                if source_id
            ]
        )
        section_question_ids = _dedupe_preserve_order(
            [
                question_id
                for item in section_support
                for question_id in item.get("question_ids", [])
                if question_id
            ]
        )
        grounded_sections.append(
            {
                **dict(section),
                "claims": grounded_claims,
                "citations": grounded_citations,
            }
        )
        section_question_id_refs = _section_question_ids(section)
        question_entries = [
            entry
            for question_id in section_question_id_refs
            for entry in ledger_by_question_id.get(question_id, [])
        ]
        question_entries = list({
            str(entry.get("ledger_id", "")).strip() or str(index): entry
            for index, entry in enumerate(question_entries)
        }.values())
        section_coverage.append(
            {
                "section_id": section_id,
                "title": str(section.get("title", "")),
                "answered": bool(grounded_claims),
                "grounded_claim_count": len(grounded_claims),
                "citation_count": len(grounded_citations),
                "candidate_evidence_count": len(
                    [item for item in bank.get("candidate_evidence_ids", []) if str(item).strip()]
                ) or len(
                    {
                        str(entry.get("evidence_id", "")).strip()
                        for entry in question_entries
                        if str(entry.get("evidence_id", "")).strip()
                    }
                ),
                "selected_evidence_count": len(
                    [item for item in bank.get("selected_evidence_ids", []) if str(item).strip()]
                ) or len(
                    {
                        str(entry.get("evidence_id", "")).strip()
                        for entry in question_entries
                        if str(entry.get("evidence_id", "")).strip()
                    }
                ),
                "rejected_evidence_count": len(
                    [item for item in bank.get("rejected_evidence_ids", []) if str(item).strip()]
                ) or len(
                    {
                        str(entry.get("evidence_id", "")).strip()
                        for entry in question_entries
                        if str(entry.get("evidence_id", "")).strip()
                        and (
                            any(str(section_ref).strip() for section_ref in entry.get("rejected_section_ids", []) or [])
                            or str(entry.get("disposition", "")).strip() == "rejected"
                        )
                    }
                ),
                "supporting_claim_ids": section_claim_ids,
                "supporting_evidence_ids": section_evidence_ids,
                "supporting_source_ids": section_source_ids,
                "question_id": section_question_id_refs[0] if section_question_id_refs else "",
                "explain_via": "section_packets" if section_evidence_ids else ("claim_bindings" if section_claim_ids else ""),
                "pool_mode": str(section.get("pool_mode", "") or ""),
                "question_ids": section_question_ids,
            }
        )
    for item in plan.sub_questions:
        question = item.question.strip()
        if not question:
            continue
        explicit_matching_section_ids = [
            str(section.get("section_id", "")).strip()
            for section in grounded_sections
            if str(section.get("section_id", "")).strip()
            and item.id in _section_question_ids(section)
            and any(
                [
                    citation
                    for claim in section.get("claims", [])
                    for citation in claim.get("citations", [])
                    if str(citation).strip()
                ]
            )
        ]
        question_tokens = _tokenize_keywords(question)
        coverage_threshold = max(2, min(4, max(1, len(question_tokens) // 2)))
        matching_section_ids: list[str] = list(explicit_matching_section_ids)
        matching_claim_ids: list[str] = []
        for section in grounded_sections:
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
                if section.get("citations") and _count_keyword_overlap(section_text, question_tokens) >= max(coverage_threshold, 3):
                    if section_id and section_id not in matching_section_ids:
                        matching_section_ids.append(section_id)
        matching_section_id_set = set(matching_section_ids)
        filtered_question_entries = [
            entry
            for entry in ledger_by_question_id.get(item.id, [])
            if not matching_section_id_set
            or str(entry.get("selected_section_id", "")).strip() in matching_section_id_set
            or any(
                str(section_id).strip() in matching_section_id_set
                for section_id in entry.get("candidate_section_ids", []) or []
            )
        ]
        if not filtered_question_entries:
            filtered_question_entries = list(ledger_by_question_id.get(item.id, []))
        supporting_evidence_ids = _dedupe_preserve_order(
            [
                str(entry.get("evidence_id", "")).strip()
                for entry in filtered_question_entries
                if str(entry.get("evidence_id", "")).strip()
            ]
        )
        supporting_source_ids = _dedupe_preserve_order(
            [
                str(source_id).strip()
                for entry in filtered_question_entries
                for source_id in entry.get("source_ids", []) or []
                if str(source_id).strip()
            ]
        )
        packet_backed = bool(supporting_evidence_ids) or bool(supporting_source_ids)
        covered = (bool(explicit_matching_section_ids) and packet_backed) or (
            bool(matching_claim_ids) and packet_backed
        )
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
                "supporting_evidence_ids": supporting_evidence_ids,
                "supporting_source_ids": supporting_source_ids,
                "explain_via": "explicit_question_binding"
                if packet_backed
                else ("claim_text_overlap" if matching_claim_ids else ""),
            }
        )
    normalized_outline = [
        {
            "section_id": str(section.get("section_id", "")).strip(),
            "title": str(section.get("title", "")).strip(),
        }
        for section in (planned_outline or [])
        if isinstance(section, dict)
    ] or [
        {"section_id": section.section_id, "title": section.title}
        for section in plan.report_outline
    ]
    unanswered_sections = [
        section["title"]
        for section in normalized_outline
        if section["section_id"] not in answered_section_ids
    ]
    coverage_items_by_target = {
        _normalize_whitespace(str(item.get("target", ""))): item
        for item in (coverage_state or {}).get("items", [])
        if isinstance(item, dict) and _normalize_whitespace(str(item.get("target", "")))
    }
    for target in _stop_policy_targets(plan):
        target_tokens = _tokenize_keywords(target)
        coverage_threshold = max(2, min(4, max(1, len(target_tokens) // 2)))
        target_question_ids = {
            item.id
            for item in plan.sub_questions
            if _normalize_whitespace(item.question) == _normalize_whitespace(target)
            or _count_keyword_overlap(item.question, target_tokens) >= coverage_threshold
        }
        matching_section_ids: list[str] = []
        matching_claim_ids: list[str] = []
        for section in grounded_sections:
            section_id = str(section.get("section_id", "")).strip()
            section_matched = False
            for claim in section.get("claims", []):
                claim_text = str(claim.get("text", ""))
                if _count_keyword_overlap(claim_text, target_tokens) >= coverage_threshold:
                    if section_id and section_id not in matching_section_ids:
                        matching_section_ids.append(section_id)
                    claim_id = str(claim.get("claim_id", "")).strip()
                    if claim_id and claim_id not in matching_claim_ids:
                        matching_claim_ids.append(claim_id)
                    section_matched = True
            if not section_matched:
                section_text = " ".join([str(section.get("title", "")), str(section.get("summary", ""))])
                if section.get("citations") and _count_keyword_overlap(section_text, target_tokens) >= max(coverage_threshold, 3):
                    if section_id and section_id not in matching_section_ids:
                        matching_section_ids.append(section_id)
        coverage_item = coverage_items_by_target.get(_normalize_whitespace(target), {})
        matched_unit_ids = [
            str(unit_id).strip()
            for unit_id in coverage_item.get("matched_unit_ids", [])
            if str(unit_id).strip()
        ]
        grounded_source_ids = {
            str(source_id).strip()
            for source_id in coverage_item.get("grounded_source_ids", [])
            if str(source_id).strip()
        }
        packet_backed_section_ids = [
            section_id
            for section_id in matching_section_ids
            if any(
                str(evidence_id).strip()
                for evidence_id in (banks_by_section.get(section_id, {}) or {}).get("selected_evidence_ids", []) or []
            )
        ]
        binding_backed_claim_ids = []
        for section in grounded_sections:
            section_id = str(section.get("section_id", "")).strip()
            for claim in section.get("claims", []):
                claim_id = str(claim.get("claim_id", "")).strip()
                if claim_id not in matching_claim_ids:
                    continue
                if any(str(evidence_id).strip() for evidence_id in claim.get("evidence_ids", []) or []):
                    binding_backed_claim_ids.append(claim_id)
                    continue
                if any(
                    isinstance(binding, dict)
                    and (
                        str(binding.get("evidence_id", "")).strip()
                        or str(binding.get("source_id", "")).strip()
                    )
                    for binding in claim.get("evidence_bindings", []) or []
                ):
                    binding_backed_claim_ids.append(claim_id)
        binding_backed_claim_ids = _dedupe_preserve_order(binding_backed_claim_ids)
        grounded_source_backed_section_ids: list[str] = []
        if not matching_claim_ids and grounded_source_ids:
            for section in grounded_sections:
                section_id = str(section.get("section_id", "")).strip()
                section_citations = {
                    str(citation).strip()
                    for citation in section.get("citations", [])
                    if str(citation).strip()
                }
                section_bank = banks_by_section.get(section_id, {}) or {}
                has_packet_support = any(
                    str(evidence_id).strip()
                    for evidence_id in section_bank.get("selected_evidence_ids", []) or []
                )
                has_binding_support = any(
                    any(str(evidence_id).strip() for evidence_id in claim.get("evidence_ids", []) or [])
                    or any(
                        isinstance(binding, dict)
                        and (
                            str(binding.get("evidence_id", "")).strip()
                            or str(binding.get("source_id", "")).strip()
                        )
                        for binding in claim.get("evidence_bindings", []) or []
                    )
                    for claim in section.get("claims", []) or []
                )
                section_text = " ".join(
                    [
                        str(section.get("title", "") or ""),
                        str(section.get("summary", "") or ""),
                    ]
                )
                section_text_aligned = _count_keyword_overlap(section_text, target_tokens) >= coverage_threshold
                section_question_ids = set(_section_question_ids(section))
                packet_question_ids = {
                    str(question_id).strip()
                    for packet in section_bank.get("selected_packets", []) or []
                    if isinstance(packet, dict)
                    for question_id in packet.get("question_ids", []) or []
                    if str(question_id).strip()
                }
                question_aligned = bool(target_question_ids & (section_question_ids | packet_question_ids))
                if (
                    section_id
                    and section_citations & grounded_source_ids
                    and (has_packet_support or has_binding_support)
                    and (section_text_aligned or question_aligned)
                    and section_id not in matching_section_ids
                ):
                    matching_section_ids.append(section_id)
                    grounded_source_backed_section_ids.append(section_id)
        covered = bool(packet_backed_section_ids or binding_backed_claim_ids or grounded_source_backed_section_ids)
        if not covered:
            hard_uncovered_targets.append(target)
        hard_target_coverage.append(
            {
                "target": target,
                "covered": covered,
                "section_ids": matching_section_ids,
                "claim_ids": matching_claim_ids,
                "matched_unit_ids": matched_unit_ids,
                "supporting_source_ids": sorted(grounded_source_ids),
                "explain_via": "section_packets"
                if packet_backed_section_ids
                else (
                    "claim_bindings"
                    if binding_backed_claim_ids
                    else ("coverage_state_grounded_sources" if grounded_source_backed_section_ids else "")
                ),
            }
        )
    coverage_gate_passed = not unanswered_sections and not uncovered_sub_questions
    hard_coverage_gate_passed = not hard_uncovered_targets
    return {
        "planned_section_ids": [section["section_id"] for section in normalized_outline],
        "answered_section_ids": answered_section_ids,
        "unanswered_sections": unanswered_sections,
        "planned_sub_question_ids": [item.id for item in plan.sub_questions],
        "covered_sub_question_ids": covered_sub_question_ids,
        "uncovered_sub_questions": uncovered_sub_questions,
        "sub_questions": sub_question_coverage,
        "section_coverage": section_coverage,
        "hard_coverage_targets": hard_target_coverage,
        "hard_uncovered_targets": hard_uncovered_targets,
        "hard_coverage_gate_passed": hard_coverage_gate_passed,
        "coverage_gate_passed": coverage_gate_passed,
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
    source_claim_ids: dict[str, list[str]] = {}
    source_section_ids: dict[str, list[str]] = {}
    total_claims = 0
    grounded_claims = 0
    single_source_claims = 0
    low_confidence_claims = 0
    missing_evidence_binding_claims = 0
    total_evidence_bindings = 0
    source_backed_binding_count = 0
    search_only_binding_count = 0
    null_span_binding_count = 0
    grounded_claims_without_source_backed_binding = 0
    for section in sections:
        claims = section.get("claims", [])
        claim_diagnostics: list[dict[str, Any]] = []
        for claim in claims:
            total_claims += 1
            citation_ids = [citation for citation in claim.get("citations", []) if citation in source_registry]
            grounded = bool(citation_ids)
            evidence_bindings = claim.get("evidence_bindings", []) or []
            valid_evidence_bindings = [
                binding
                for binding in evidence_bindings
                if isinstance(binding, dict)
                and str(binding.get("source_id", "")).strip() in set(citation_ids)
                and str(binding.get("source_id", "")).strip() in source_registry
            ]
            source_backed_bindings = [
                binding for binding in valid_evidence_bindings if bool(binding.get("source_backed"))
            ]
            search_only_bindings = [
                binding for binding in valid_evidence_bindings if not bool(binding.get("source_backed"))
            ]
            null_span_bindings = [
                binding
                for binding in source_backed_bindings
                if binding.get("line_start") is None or binding.get("line_end") is None
            ]
            if grounded:
                grounded_claims += 1
            if len(set(citation_ids)) <= 1:
                single_source_claims += 1
            if str(claim.get("confidence", "")).lower() == "low":
                low_confidence_claims += 1
            if grounded and not valid_evidence_bindings:
                missing_evidence_binding_claims += 1
            if grounded and valid_evidence_bindings and not source_backed_bindings:
                grounded_claims_without_source_backed_binding += 1
            total_evidence_bindings += len(valid_evidence_bindings)
            source_backed_binding_count += len(source_backed_bindings)
            search_only_binding_count += len(search_only_bindings)
            null_span_binding_count += len(null_span_bindings)
            claim_id = str(claim.get("claim_id", "")).strip()
            section_id = str(section.get("section_id", "")).strip()
            for citation_id in citation_ids:
                if claim_id and claim_id not in source_claim_ids.setdefault(citation_id, []):
                    source_claim_ids[citation_id].append(claim_id)
                if section_id and section_id not in source_section_ids.setdefault(citation_id, []):
                    source_section_ids[citation_id].append(section_id)
            claim_diagnostics.append(
                {
                    "claim_id": claim_id,
                    "citation_ids": citation_ids,
                    "grounded": grounded,
                    "supporting_source_count": len(set(citation_ids)),
                    "supporting_domain_count": _supporting_domain_count(citation_ids, source_registry),
                    "confidence": claim.get("confidence", ""),
                    "evidence_binding_count": len(valid_evidence_bindings),
                    "source_backed_binding_count": len(source_backed_bindings),
                    "search_only_binding_count": len(search_only_bindings),
                    "null_span_binding_count": len(null_span_bindings),
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
        "missing_evidence_binding_claims": missing_evidence_binding_claims,
        "total_evidence_bindings": total_evidence_bindings,
        "source_backed_binding_count": source_backed_binding_count,
        "search_only_binding_count": search_only_binding_count,
        "null_span_binding_count": null_span_binding_count,
        "grounded_claims_without_source_backed_binding": grounded_claims_without_source_backed_binding,
        "sections": section_diagnostics,
        "sources": [
            {
                "source_id": source_id,
                "citation_count": len(source_claim_ids.get(source_id, [])),
                "section_count": len(source_section_ids.get(source_id, [])),
                "supporting_claim_ids": list(source_claim_ids.get(source_id, [])),
                "supporting_section_ids": list(source_section_ids.get(source_id, [])),
            }
            for source_id in sorted(source_registry)
            if source_claim_ids.get(source_id) or source_section_ids.get(source_id)
        ],
    }


def _build_release_gate(
    coverage: dict[str, Any],
    grounding: dict[str, Any],
    verifier: dict[str, Any] | None = None,
    *,
    source_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_reason_codes: list[str] = []
    blocking_reason_codes: list[str] = []
    hard_coverage_gate_passed = bool(
        coverage.get("hard_coverage_gate_passed", coverage.get("coverage_gate_passed", False))
    )
    official_docs_only = str((source_policy or {}).get("mode", "")).strip() == "official_docs_only"
    coverage_incomplete = bool(
        not hard_coverage_gate_passed
        or coverage.get("unanswered_sections")
        or coverage.get("uncovered_sub_questions")
        or coverage.get("hard_uncovered_targets")
    )
    if not hard_coverage_gate_passed:
        all_reason_codes.append("coverage_incomplete")
        blocking_reason_codes.append("coverage_incomplete")
    if int(grounding.get("ungrounded_claims", 0) or 0) > 0:
        all_reason_codes.append("ungrounded_claims")
        blocking_reason_codes.append("ungrounded_claims")
    if int(grounding.get("missing_evidence_binding_claims", 0) or 0) > 0:
        all_reason_codes.append("missing_evidence_bindings")
        blocking_reason_codes.append("missing_evidence_bindings")
    if isinstance(verifier, dict):
        for code in (str(code).strip() for code in verifier.get("reason_codes", [])):
            if not code:
                continue
            all_reason_codes.append(code)
            if (
                official_docs_only
                and coverage_incomplete
                and code == "medium_single_source_search_only"
            ):
                blocking_reason_codes.append(code)
                continue
            if code not in _NON_BLOCKING_VERIFIER_REASON_CODES:
                blocking_reason_codes.append(code)
    all_reason_codes = _dedupe_preserve_order(all_reason_codes)
    blocking_reason_codes = _dedupe_preserve_order(blocking_reason_codes)
    return {
        "passed": not blocking_reason_codes,
        "reason_codes": blocking_reason_codes,
        "all_reason_codes": all_reason_codes,
        "soft_reason_codes": [code for code in all_reason_codes if code not in set(blocking_reason_codes)],
    }


def _claim_provenance_details(
    claim: dict[str, Any],
    *,
    source_registry: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    citations = _dedupe_preserve_order(
        [str(citation).strip() for citation in claim.get("citations", []) if str(citation).strip()]
    )
    claim_evidence_ids = _dedupe_preserve_order(
        [str(evidence_id).strip() for evidence_id in claim.get("evidence_ids", []) if str(evidence_id).strip()]
    )
    bindings = [binding for binding in claim.get("evidence_bindings", []) if isinstance(binding, dict)]
    valid_bindings: list[dict[str, Any]] = []
    source_backed_bindings: list[dict[str, Any]] = []
    search_only_bindings: list[dict[str, Any]] = []
    null_span_bindings: list[dict[str, Any]] = []
    mismatched_binding_source_count = 0
    mismatched_binding_evidence_count = 0

    for binding in bindings:
        binding_source_id = str(binding.get("source_id", "")).strip()
        binding_evidence_id = str(binding.get("evidence_id", "")).strip()
        source_ok = not source_registry or (binding_source_id in citations and binding_source_id in source_registry)
        if not source_ok:
            mismatched_binding_source_count += 1
        evidence_ok = True
        evidence_item = evidence_by_id.get(binding_evidence_id)
        if claim_evidence_ids:
            evidence_ok = (
                binding_evidence_id in claim_evidence_ids
                and evidence_item is not None
                and binding_source_id in {
                    str(source_id).strip()
                    for source_id in evidence_item.get("source_ids", [])
                    if str(source_id).strip()
                }
            )
            if not evidence_ok:
                mismatched_binding_evidence_count += 1
        if not (source_ok and evidence_ok):
            continue
        valid_bindings.append(binding)
        if bool(binding.get("source_backed")):
            source_backed_bindings.append(binding)
            line_start = binding.get("line_start")
            line_end = binding.get("line_end")
            if (
                line_start is None
                or line_end is None
                or not isinstance(line_start, int)
                or not isinstance(line_end, int)
                or line_end < line_start
            ):
                null_span_bindings.append(binding)
        else:
            search_only_bindings.append(binding)

    bound_citation_ids = _dedupe_preserve_order(
        [str(binding.get("source_id", "")).strip() for binding in valid_bindings if str(binding.get("source_id", "")).strip()]
    )
    bound_evidence_ids = _dedupe_preserve_order(
        [
            str(binding.get("evidence_id", "")).strip()
            for binding in valid_bindings
            if str(binding.get("evidence_id", "")).strip() in claim_evidence_ids
        ]
    )
    return {
        "citations": citations,
        "claim_evidence_ids": claim_evidence_ids,
        "valid_bindings": valid_bindings,
        "source_backed_bindings": source_backed_bindings,
        "search_only_bindings": search_only_bindings,
        "null_span_bindings": null_span_bindings,
        "derived_supporting_source_count": len(set(citations)),
        "derived_supporting_domain_count": _supporting_domain_count(citations, source_registry),
        "unbound_citation_ids": [citation for citation in citations if citation not in set(bound_citation_ids)],
        "unbound_evidence_ids": [evidence_id for evidence_id in claim_evidence_ids if evidence_id not in set(bound_evidence_ids)],
        "mismatched_binding_source_count": mismatched_binding_source_count,
        "mismatched_binding_evidence_count": mismatched_binding_evidence_count,
    }


def _build_verifier_diagnostics(
    *,
    coverage: dict[str, Any],
    grounding: dict[str, Any],
    sections: list[dict[str, Any]],
    source_registry: dict[str, dict[str, Any]] | None = None,
    evidence_items: list[dict[str, Any]] | None = None,
    section_banks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reason_codes: list[str] = []
    flagged_claim_ids: list[str] = []
    source_registry = source_registry or {}
    evidence_items = evidence_items or []
    evidence_by_id = {
        str(item.get("evidence_id", "")).strip(): dict(item)
        for item in evidence_items
        if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
    }
    seen_claim_keys: dict[str, dict[str, Any]] = {}
    section_bank_by_id = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks or []
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }

    def _section_bank_is_clean_official_docs(section_id: str) -> bool:
        normalized_section_id = str(section_id).strip()
        if not normalized_section_id:
            return False
        bank = section_bank_by_id.get(normalized_section_id, {})
        selected_ids = [
            str(evidence_id).strip()
            for evidence_id in bank.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip() in evidence_by_id
        ]
        if not selected_ids:
            return False
        rejected_ids = [
            str(evidence_id).strip()
            for evidence_id in bank.get("rejected_evidence_ids", []) or []
            if str(evidence_id).strip()
        ]
        if rejected_ids:
            return False
        selected_source_ids = {
            str(source_id).strip()
            for evidence_id in selected_ids
            for source_id in evidence_by_id.get(evidence_id, {}).get("source_ids", [])
            if str(source_id).strip()
        }
        return bool(selected_source_ids) and all(
            source_id in source_registry and _source_looks_like_official_docs(source_registry[source_id])
            for source_id in selected_source_ids
        )

    integrity_counts = {
        "missing_evidence_items": 0,
        "mismatched_binding_source": 0,
        "mismatched_binding_evidence": 0,
        "invalid_source_backed_span": 0,
        "duplicate_claims": 0,
        "conflicted_claims": 0,
        "low_value_claims": 0,
        "medium_single_source_search_only": 0,
        "same_domain_off_topic_dominance": 0,
        "unbound_citation_sources": 0,
        "unbound_evidence_ids": 0,
        "claim_outside_selected_bank": 0,
        "selected_evidence_unused": 0,
        "section_packet_mismatch": 0,
    }
    hard_coverage_gate_passed = bool(
        coverage.get("hard_coverage_gate_passed", coverage.get("coverage_gate_passed", False))
    )
    coverage_incomplete = bool(
        not hard_coverage_gate_passed
        or coverage.get("unanswered_sections")
        or coverage.get("uncovered_sub_questions")
        or coverage.get("hard_uncovered_targets")
    )
    if not hard_coverage_gate_passed:
        reason_codes.append("coverage_incomplete")
    if int(grounding.get("missing_evidence_binding_claims", 0) or 0) > 0:
        reason_codes.append("missing_evidence_bindings")
    if int(grounding.get("ungrounded_claims", 0) or 0) > 0:
        reason_codes.append("ungrounded_claims")
    source_backed_binding_count = int(grounding.get("source_backed_binding_count", 0) or 0)
    null_span_binding_count = int(grounding.get("null_span_binding_count", 0) or 0)
    if source_backed_binding_count > 0 and (null_span_binding_count / source_backed_binding_count) > 0.5:
        reason_codes.append("high_null_span_ratio")
    conflicted_claims: list[dict[str, Any]] = []

    for section in sections:
        section_title = str(section.get("title", "") or "").strip()
        section_id = str(section.get("section_id", "") or "").strip()
        section_is_rollup = is_summary_section_title(section_title) or is_key_findings_section_title(section_title)
        section_is_gap = is_open_questions_section_title(section_title) or _is_gap_section(section)
        section_bank = section_bank_by_id.get(section_id, {})
        selected_evidence_ids = [
            str(evidence_id).strip()
            for evidence_id in section_bank.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip() in evidence_by_id
        ]
        selected_packets = [
            dict(packet)
            for packet in section_bank.get("selected_packets", []) or []
            if isinstance(packet, dict) and str(packet.get("evidence_id", "")).strip()
        ]
        rejected_evidence_ids = [
            str(evidence_id).strip()
            for evidence_id in section_bank.get("rejected_evidence_ids", []) or []
            if str(evidence_id).strip()
        ]
        selected_evidence_source_ids = {
            str(source_id).strip()
            for evidence_id in selected_evidence_ids
            for source_id in evidence_by_id.get(evidence_id, {}).get("source_ids", [])
            if str(source_id).strip()
        }
        selected_evidence_is_official_docs = bool(selected_evidence_source_ids) and all(
            source_id in source_registry and _source_looks_like_official_docs(source_registry[source_id])
            for source_id in selected_evidence_source_ids
        )
        used_selected_evidence_ids: set[str] = set()
        section_claim_snapshots: list[dict[str, Any]] = []
        for claim in section.get("claims", []):
            claim_id = str(claim.get("claim_id", "")).strip()
            raw_claim_text = str(claim.get("text", "") or "")
            claim_text = _summarize_evidence_text(raw_claim_text, limit=_MAX_CLAIM_LENGTH)
            confidence = str(claim.get("confidence", "") or "").strip().lower()
            provenance = _claim_provenance_details(
                claim,
                source_registry=source_registry,
                evidence_by_id=evidence_by_id,
            )
            citations = provenance["citations"]
            claim_evidence_ids = provenance["claim_evidence_ids"]
            bindings = provenance["valid_bindings"]
            source_backed_bindings = provenance["source_backed_bindings"]
            null_span_bindings = provenance["null_span_bindings"]
            supporting_source_count = provenance["derived_supporting_source_count"]
            if selected_evidence_ids:
                matching_selected = set(claim_evidence_ids) & set(selected_evidence_ids)
                selected_binding_ids = {
                    str(binding.get("evidence_id", "")).strip()
                    for binding in bindings
                    if isinstance(binding, dict)
                    and (
                        bool(binding.get("selected_for_section"))
                        or str(binding.get("pool_mode", "")).strip() == "selected"
                    )
                    and str(binding.get("evidence_id", "")).strip()
                }
                out_of_bank_binding_ids = {
                    str(binding.get("evidence_id", "")).strip()
                    for binding in bindings
                    if isinstance(binding, dict)
                    and str(binding.get("evidence_id", "")).strip()
                    and (
                        str(binding.get("evidence_id", "")).strip() not in set(selected_evidence_ids)
                        or (
                            "selected_for_section" in binding
                            and not bool(binding.get("selected_for_section"))
                        )
                        or str(binding.get("pool_mode", "")).strip() not in {"", "selected"}
                    )
                }
                used_selected_evidence_ids.update(matching_selected | selected_binding_ids)
                claim_spillover = bool(set(claim_evidence_ids) - set(selected_evidence_ids))
                binding_spillover = bool(out_of_bank_binding_ids)
                has_selected_support = bool(matching_selected or selected_binding_ids)
                if claim_spillover or binding_spillover or ((claim_evidence_ids or bindings) and not has_selected_support):
                    if claim_id:
                        flagged_claim_ids.append(claim_id)
                    integrity_counts["claim_outside_selected_bank"] += 1
                    reason_codes.append("claim_outside_selected_bank")
            if claim_text:
                claim_key = _stable_text_key(claim_text)
                previous_claim = seen_claim_keys.get(claim_key)
                if previous_claim is not None:
                    previous_is_rollup = bool(previous_claim.get("is_rollup"))
                    previous_section_id = str(previous_claim.get("section_id", "")).strip()
                    if not section_is_rollup and previous_is_rollup:
                        seen_claim_keys[claim_key] = {
                            "claim_id": claim_id,
                            "is_rollup": False,
                            "section_id": section_id,
                        }
                    elif (
                        not section_is_rollup
                        and not previous_is_rollup
                        and previous_section_id == section_id
                    ):
                        if claim_id:
                            flagged_claim_ids.append(claim_id)
                        integrity_counts["duplicate_claims"] += 1
                        reason_codes.append("duplicate_claims")
                else:
                    seen_claim_keys[claim_key] = {
                        "claim_id": claim_id,
                        "is_rollup": section_is_rollup,
                        "section_id": section_id,
                    }
                if _is_noisy_text(raw_claim_text) or _is_noisy_text(claim_text):
                    if claim_id:
                        flagged_claim_ids.append(claim_id)
                    integrity_counts["low_value_claims"] += 1
                    reason_codes.append("low_value_claims")
            elif raw_claim_text and _is_noisy_text(raw_claim_text):
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["low_value_claims"] += 1
                reason_codes.append("low_value_claims")
            missing_claim_evidence_ids = [evidence_id for evidence_id in claim_evidence_ids if evidence_id not in evidence_by_id]
            if missing_claim_evidence_ids:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["missing_evidence_items"] += len(missing_claim_evidence_ids)
                reason_codes.append("missing_evidence_items")
            if provenance["unbound_citation_ids"]:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["unbound_citation_sources"] += 1
                reason_codes.append("unbound_citation_sources")
            if provenance["unbound_evidence_ids"]:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["unbound_evidence_ids"] += 1
                reason_codes.append("unbound_evidence_ids")
            cited_sources = [source_registry[citation] for citation in citations if citation in source_registry]
            if cited_sources and all(
                "same_domain_off_topic" in set(source.get("ranking_penalties") or []) for source in cited_sources
            ):
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["same_domain_off_topic_dominance"] += 1
                reason_codes.append("same_domain_off_topic_dominance")
            if (
                hard_coverage_gate_passed
                and confidence == "medium"
                and supporting_source_count <= 1
                and bindings
                and not any(bool(binding.get("source_backed")) for binding in bindings)
            ):
                binding_section_ids = {
                    str(binding.get("section_id", "")).strip()
                    for binding in bindings
                    if isinstance(binding, dict) and str(binding.get("section_id", "")).strip()
                }
                acceptable_official_section = (
                    not section_is_gap
                    and not coverage_incomplete
                    and (
                        (
                            not section_is_rollup
                            and bool(selected_evidence_ids)
                            and not rejected_evidence_ids
                            and selected_evidence_is_official_docs
                        )
                        or (
                            section_is_rollup
                            and bool(binding_section_ids)
                            and all(
                                _section_bank_is_clean_official_docs(bound_section_id)
                                for bound_section_id in binding_section_ids
                            )
                        )
                    )
                )
                if not acceptable_official_section:
                    if claim_id:
                        flagged_claim_ids.append(claim_id)
                    integrity_counts["medium_single_source_search_only"] += 1
                    reason_codes.append("medium_single_source_search_only")
            if (
                confidence == "low"
                and supporting_source_count <= 1
                and bindings
                and not source_backed_bindings
            ):
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                reason_codes.append("single_source_low_confidence")
            if provenance["mismatched_binding_source_count"] > 0:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["mismatched_binding_source"] += provenance["mismatched_binding_source_count"]
                reason_codes.append("mismatched_binding_source")
            if provenance["mismatched_binding_evidence_count"] > 0:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["mismatched_binding_evidence"] += provenance["mismatched_binding_evidence_count"]
                reason_codes.append("mismatched_binding_evidence")
            if null_span_bindings:
                if claim_id:
                    flagged_claim_ids.append(claim_id)
                integrity_counts["invalid_source_backed_span"] += len(null_span_bindings)
                reason_codes.append("invalid_source_backed_span")
            section_claim_snapshots.append(
                {
                    "claim_id": claim_id,
                    "section_id": section_id,
                    "text": claim_text or raw_claim_text,
                    "confidence": str(claim.get("confidence", "") or ""),
                    "source_ids": list(claim.get("source_ids", []) or claim.get("citations", []) or []),
                    "evidence_ids": list(claim.get("evidence_ids", []) or []),
                }
            )
        for index, left_claim in enumerate(section_claim_snapshots):
            left_text = str(left_claim.get("text", "") or "")
            if not left_text:
                continue
            for right_claim in section_claim_snapshots[index + 1 :]:
                right_text = str(right_claim.get("text", "") or "")
                conflict_reason = _claim_conflict_reason(left_text, right_text)
                if not conflict_reason:
                    continue
                for conflicted in (left_claim, right_claim):
                    claim_id = str(conflicted.get("claim_id", "")).strip()
                    if claim_id:
                        flagged_claim_ids.append(claim_id)
                    conflicted_claims.append(
                        {
                            "claim_id": claim_id,
                            "section_id": section_id,
                            "text": str(conflicted.get("text", "") or ""),
                            "confidence": str(conflicted.get("confidence", "") or ""),
                            "reason": conflict_reason,
                        }
                    )
                integrity_counts["conflicted_claims"] += 2
                reason_codes.append("conflict")
        unused_selected_evidence_ids = [
            evidence_id
            for evidence_id in selected_evidence_ids
            if evidence_id not in used_selected_evidence_ids
        ]
        if unused_selected_evidence_ids:
            integrity_counts["selected_evidence_unused"] += len(unused_selected_evidence_ids)
            reason_codes.append("selected_evidence_unused")
        if selected_packets and section.get("claims"):
            packet_claim_ids = {
                str(claim_id).strip()
                for packet in selected_packets
                for claim_id in packet.get("claim_ids", []) or []
                if str(claim_id).strip()
            }
            actual_claim_ids = {
                str(claim.get("claim_id", "")).strip()
                for claim in section.get("claims", []) or []
                if isinstance(claim, dict) and str(claim.get("claim_id", "")).strip()
            }
            if actual_claim_ids and packet_claim_ids != actual_claim_ids:
                integrity_counts["section_packet_mismatch"] += 1
                reason_codes.append("section_packet_mismatch")

    return {
        "passed": not reason_codes,
        "reason_codes": _dedupe_preserve_order(reason_codes),
        "flagged_claim_ids": _dedupe_preserve_order(flagged_claim_ids),
        "conflicted_claims": conflicted_claims,
        "summary": {
            "section_count": len(sections),
            "total_claims": int(grounding.get("total_claims", 0) or 0),
            "low_confidence_claims": int(grounding.get("low_confidence_claims", 0) or 0),
            "single_source_claims": int(grounding.get("single_source_claims", 0) or 0),
            "source_backed_binding_count": source_backed_binding_count,
            "search_only_binding_count": int(grounding.get("search_only_binding_count", 0) or 0),
            "null_span_binding_count": null_span_binding_count,
            **integrity_counts,
        },
    }


def _validate_provenance_bundle(
    *,
    report_value: dict[str, Any] | None,
    sources_value: list[dict[str, Any]] | None,
    citations_value: dict[str, Any] | None,
    evidence_items_value: list[dict[str, Any]] | None,
) -> dict[str, str]:
    if (
        not isinstance(report_value, dict)
        or not isinstance(sources_value, list)
        or not isinstance(citations_value, dict)
        or not isinstance(evidence_items_value, list)
    ):
        return {}
    errors: dict[str, str] = {}
    source_ids = {
        str(item.get("source_id", "")).strip()
        for item in sources_value
        if isinstance(item, dict) and str(item.get("source_id", "")).strip()
    }
    source_registry = citations_value.get("source_registry")
    sections = citations_value.get("sections")
    report_sections = report_value.get("sections")
    if not isinstance(source_registry, dict) or not isinstance(sections, list) or not isinstance(report_sections, list):
        return errors
    registry_ids = {str(source_id).strip() for source_id in source_registry if str(source_id).strip()}
    if source_ids != registry_ids:
        errors["sources.json"] = "invalid_provenance_bundle"
        errors["citations.json"] = "invalid_provenance_bundle"
    if report_sections != sections:
        errors["report.json"] = "invalid_provenance_bundle"
        errors["citations.json"] = "invalid_provenance_bundle"
    grounding = _build_grounding_diagnostics(sections, source_registry)
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding=grounding,
        sections=sections,
        source_registry=source_registry,
        evidence_items=evidence_items_value,
        section_banks=None,
    )
    structural_reason_codes = {
        "missing_evidence_items",
        "mismatched_binding_source",
        "mismatched_binding_evidence",
        "unbound_citation_sources",
        "unbound_evidence_ids",
    }
    if structural_reason_codes & set(verifier.get("reason_codes", [])):
        errors["report.json"] = "invalid_provenance_bundle"
    return errors


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


def _build_claim_evidence_bindings(
    items: list[DeepResearchEvidenceItem],
    source_registry: dict[str, dict[str, Any]],
    *,
    section_id: str = "",
    pool_mode: str = "",
    selected_evidence_ids: list[str] | None = None,
    question_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    selected_evidence_ids = [
        str(evidence_id).strip() for evidence_id in selected_evidence_ids or [] if str(evidence_id).strip()
    ]
    question_ids = [str(question_id).strip() for question_id in question_ids or [] if str(question_id).strip()]
    for item in items:
        excerpt = _summarize_evidence_text(item.summary or item.detail, limit=_MAX_CLAIM_LENGTH)
        if not excerpt:
            continue
        excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        for source_id in item.source_ids:
            normalized_source_id = str(source_id).strip()
            if not normalized_source_id:
                continue
            identity = (item.evidence_id, normalized_source_id)
            if identity in seen:
                continue
            seen.add(identity)
            source = source_registry.get(normalized_source_id, {})
            source_url = str(source.get("url", "") or "").strip()
            if not source_url:
                source_url = next((str(url).strip() for url in item.source_urls if str(url).strip()), "")
            source_backed = item.evidence_kind != "search"
            bindings.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_id": normalized_source_id,
                    "source_url": source_url,
                    "winner_provider": str(source.get("winner_provider") or source.get("provider") or "").strip(),
                    "evidence_kind": item.evidence_kind,
                    "excerpt": excerpt,
                    "excerpt_hash": excerpt_hash,
                    "excerpt_origin": "search_answer" if item.evidence_kind == "search" else "source_text",
                    "source_backed": source_backed,
                    "line_start": item.line_start if source_backed else None,
                    "line_end": item.line_end if source_backed else None,
                    "section_id": section_id,
                    "pool_mode": pool_mode,
                    "selected_for_section": item.evidence_id in set(selected_evidence_ids),
                    "question_ids": list(question_ids),
                }
            )
    return bindings


def _best_cluster_claim_text(items: list[DeepResearchEvidenceItem]) -> str:
    def candidate_text(item: DeepResearchEvidenceItem) -> str:
        summary_text = _summarize_evidence_text(item.summary, limit=_MAX_CLAIM_LENGTH)
        detail_text = _summarize_evidence_text(item.detail, limit=_MAX_CLAIM_LENGTH)
        summary_keywords = _tokenize_keywords(summary_text)
        detail_keywords = _tokenize_keywords(detail_text)
        if detail_text and (
            not summary_text
            or len(summary_keywords) < 3
            or (len(detail_keywords) >= max(3, len(summary_keywords) + 2))
        ):
            return detail_text
        return summary_text or detail_text

    ranked = sorted(
        items,
        key=lambda item: (
            1 if item.evidence_kind != "search" else 0,
            item.weight,
            len(item.source_ids),
            len(candidate_text(item)),
        ),
        reverse=True,
    )
    for item in ranked:
        text = candidate_text(item)
        if text and not _is_noisy_text(text):
            sentences = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", text) if chunk.strip()]
            if sentences:
                return _trim_text(" ".join(sentences[:2]), limit=min(220, _MAX_CLAIM_LENGTH))
            return text
    return ""


def _claim_support_details(claim: dict[str, Any]) -> dict[str, Any]:
    claim_id = str(claim.get("claim_id", "")).strip()
    evidence_ids = _dedupe_preserve_order(
        [str(evidence_id).strip() for evidence_id in claim.get("evidence_ids", []) if str(evidence_id).strip()]
    )
    source_ids = _dedupe_preserve_order(
        [
            str(source_id).strip()
            for source_id in [*(claim.get("source_ids", []) or []), *(claim.get("citations", []) or [])]
            if str(source_id).strip()
        ]
    )
    question_ids = _dedupe_preserve_order(
        [
            str(question_id).strip()
            for binding in claim.get("evidence_bindings", []) or []
            if isinstance(binding, dict)
            for question_id in binding.get("question_ids", []) or []
            if str(question_id).strip()
        ]
    )
    return {
        "claim_id": claim_id,
        "evidence_ids": evidence_ids,
        "source_ids": source_ids,
        "question_ids": question_ids,
    }


def _section_question_ids(section: dict[str, Any]) -> list[str]:
    return [
        str(question_id).strip()
        for question_id in (
            (section.get("question_ids") or [])
            if isinstance(section.get("question_ids"), list)
            else [section.get("question_id", "")]
        )
        if str(question_id).strip()
    ]


def _attach_materialized_claim_ids_to_evidence_ledger(
    ledger_entries: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claim_ids_by_evidence_id: dict[str, list[str]] = {}
    section_ids_by_evidence_id: dict[str, list[str]] = {}
    question_ids_by_evidence_id: dict[str, list[str]] = {}
    generic_section_ids: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        section_title = str(section.get("title", "")).strip()
        section_question_ids = _section_question_ids(section)
        if (
            section_id
            and (
                is_summary_section_title(section_title)
                or is_key_findings_section_title(section_title)
                or is_open_questions_section_title(section_title)
            )
        ):
            generic_section_ids.add(section_id)
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", "")).strip()
            if not claim_id:
                continue
            for evidence_id in claim.get("evidence_ids", []) or []:
                normalized = str(evidence_id).strip()
                if not normalized:
                    continue
                claim_ids_by_evidence_id.setdefault(normalized, [])
                if claim_id not in claim_ids_by_evidence_id[normalized]:
                    claim_ids_by_evidence_id[normalized].append(claim_id)
                if section_id:
                    section_ids_by_evidence_id.setdefault(normalized, [])
                    if section_id not in section_ids_by_evidence_id[normalized]:
                        section_ids_by_evidence_id[normalized].append(section_id)
                if section_question_ids:
                    question_ids_by_evidence_id.setdefault(normalized, [])
                    for question_id in section_question_ids:
                        if question_id not in question_ids_by_evidence_id[normalized]:
                            question_ids_by_evidence_id[normalized].append(question_id)
    hydrated: list[dict[str, Any]] = []
    for entry in ledger_entries:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        evidence_id = str(normalized.get("evidence_id", "")).strip()
        normalized["materialized_claim_ids"] = claim_ids_by_evidence_id.get(evidence_id, [])
        materialized_section_ids = section_ids_by_evidence_id.get(evidence_id, [])
        candidate_section_ids = _dedupe_preserve_order(
            [
                str(section_id).strip()
                for section_id in [
                    *(normalized.get("candidate_section_ids", []) or []),
                    *materialized_section_ids,
                ]
                if str(section_id).strip()
            ]
        )
        normalized["candidate_section_ids"] = candidate_section_ids
        normalized["question_ids"] = _dedupe_preserve_order(
            [
                str(question_id).strip()
                for question_id in [
                    *(normalized.get("question_ids", []) or []),
                    normalized.get("question_id", ""),
                    *(question_ids_by_evidence_id.get(evidence_id, []) or []),
                ]
                if str(question_id).strip()
            ]
        )
        preferred_selected_section_id = str(normalized.get("selected_section_id", "")).strip()
        if materialized_section_ids:
            if preferred_selected_section_id not in materialized_section_ids:
                concrete_materialized_section_ids = [
                    section_id for section_id in materialized_section_ids if section_id not in generic_section_ids
                ]
                preferred_selected_section_id = (
                    concrete_materialized_section_ids[0]
                    if concrete_materialized_section_ids
                    else materialized_section_ids[0]
                )
            normalized["selected_section_id"] = preferred_selected_section_id
            normalized["decision_state"] = "selected"
            normalized["disposition"] = "selected"
            if not str(normalized.get("disposition_reason", "")).strip():
                normalized["disposition_reason"] = "materialized_claim_binding"
        normalized["rejected_section_ids"] = [
            section_id
            for section_id in normalized.get("rejected_section_ids", []) or []
            if str(section_id).strip() and str(section_id).strip() != str(normalized.get("selected_section_id", "")).strip()
        ]
        hydrated.append(normalized)
    return hydrated


def _reconcile_section_bindings_with_section_banks(
    sections: list[dict[str, Any]],
    *,
    section_banks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    reconciled_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        normalized_section = dict(section)
        section_id = str(normalized_section.get("section_id", "")).strip()
        bank = banks_by_section.get(section_id, {})
        selected_evidence_ids = {
            str(evidence_id).strip()
            for evidence_id in bank.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip()
        }
        candidate_evidence_ids = {
            str(evidence_id).strip()
            for evidence_id in bank.get("candidate_evidence_ids", []) or []
            if str(evidence_id).strip()
        }
        claims: list[dict[str, Any]] = []
        for claim in normalized_section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            normalized_claim = dict(claim)
            reconciled_bindings: list[dict[str, Any]] = []
            for binding in normalized_claim.get("evidence_bindings", []) or []:
                if not isinstance(binding, dict):
                    continue
                normalized_binding = dict(binding)
                evidence_id = str(normalized_binding.get("evidence_id", "")).strip()
                normalized_binding["section_id"] = section_id
                if evidence_id and evidence_id in selected_evidence_ids:
                    normalized_binding["selected_for_section"] = True
                    normalized_binding["pool_mode"] = "selected"
                elif evidence_id and evidence_id in candidate_evidence_ids:
                    normalized_binding["selected_for_section"] = False
                    normalized_binding["pool_mode"] = "candidate"
                reconciled_bindings.append(normalized_binding)
            normalized_claim["evidence_bindings"] = reconciled_bindings
            claims.append(normalized_claim)
        normalized_section["claims"] = claims
        reconciled_sections.append(normalized_section)
    return reconciled_sections


def _build_section_citations(
    plan: DeepResearchPlan,
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
    *,
    section_banks: list[dict[str, Any]] | None = None,
    evidence_ledger: list[dict[str, Any]] | None = None,
    section_graph: dict[str, Any] | None = None,
    planned_outline: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    outline = planned_outline or build_synthesis_outline(
        plan,
        section_graph=section_graph,
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
    )
    outline_position = {
        str(section.get("section_id", "")).strip(): index
        for index, section in enumerate(outline)
        if isinstance(section, dict)
    }
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks or []
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
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
    claim_counter = {"value": 1}
    query_keywords = _tokenize_keywords(plan.query)

    def _section_value(section: dict[str, Any], key: str) -> str:
        return str(section.get(key, "") or "").strip()

    def build_section_from_pool(
        section: dict[str, Any],
        evidence_pool: list[DeepResearchEvidenceItem],
        *,
        enforce_overlap: bool,
    ) -> dict[str, Any] | None:
        section_title = _section_value(section, "title")
        section_id = _section_value(section, "section_id")
        section_goal = _section_value(section, "goal")
        if not section_id or not section_title:
            return None
        if _is_gap_section(section) and not any(
            _has_gap_signal(item.summary) or _has_gap_signal(item.detail) for item in evidence_pool
        ):
            return None
        section_keywords = _tokenize_keywords(f"{section_title} {section_goal}") or query_keywords
        section_bank = banks_by_section.get(section_id, {})
        question_ids = [
            str(question_id).strip()
            for question_id in (
                (section.get("question_ids") or [])
                if isinstance(section.get("question_ids"), list)
                else [section.get("question_id", "")]
            )
            if str(question_id).strip()
        ]
        selected_evidence_ids = [
            str(evidence_id).strip()
            for evidence_id in section_bank.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip()
        ]
        candidate_packet_evidence_ids = [
            str(packet.get("evidence_id", "")).strip()
            for packet in section_bank.get("candidate_packets", []) or []
            if isinstance(packet, dict) and str(packet.get("evidence_id", "")).strip()
        ]
        selected_packet_evidence_ids = [
            str(packet.get("evidence_id", "")).strip()
            for packet in section_bank.get("selected_packets", []) or []
            if isinstance(packet, dict) and str(packet.get("evidence_id", "")).strip()
        ]
        selected_bank_evidence_ids = _dedupe_preserve_order(
            [*selected_packet_evidence_ids, *selected_evidence_ids]
        )
        candidate_bank_evidence_ids = _dedupe_preserve_order(
            [*candidate_packet_evidence_ids, *[
                str(evidence_id).strip()
                for evidence_id in section_bank.get("candidate_evidence_ids", []) or []
                if str(evidence_id).strip()
            ]]
        )
        active_packet_evidence_ids = selected_packet_evidence_ids or candidate_packet_evidence_ids
        active_bank_evidence_ids = selected_bank_evidence_ids or candidate_bank_evidence_ids
        bank_filtered_pool = (
            [evidence for evidence in evidence_pool if evidence.evidence_id in set(active_bank_evidence_ids)]
            if active_bank_evidence_ids
            else list(evidence_pool)
        )
        ranking_pool = bank_filtered_pool or list(evidence_pool)
        ranked_pool = sorted(
            ranking_pool,
            key=lambda evidence: (
                1 if evidence.evidence_id in set(active_packet_evidence_ids) else 0,
                _count_keyword_overlap(f"{evidence.summary} {evidence.detail}", section_keywords),
                evidence.weight,
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
            if enforce_overlap and overlap_score <= 0 and section_keywords:
                continue
            relevant_evidence.append(evidence)
        if (
            not relevant_evidence
            and not enforce_overlap
            and (
                is_summary_section_title(section_title)
                or is_key_findings_section_title(section_title)
                or is_open_questions_section_title(section_title)
            )
        ):
            relevant_evidence = [evidence for evidence in ranked_pool if evidence.source_ids]
        if not relevant_evidence:
            return None
        section_claims: list[dict[str, Any]] = []
        section_claim_keys: set[str] = set()
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
                max(1 if item.evidence_kind != "search" else 0 for item in cluster),
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
            if claim_key in section_claim_keys:
                continue
            cluster_source_ids = _dedupe_preserve_order([source_id for item in cluster for source_id in item.source_ids])
            supporting_domain_count = _supporting_domain_count(cluster_source_ids, registry_by_id)
            cluster_type = _cluster_type_for_items(cluster)
            claim = DeepResearchClaim(
                claim_id=f"{section_id}-claim-{claim_counter['value']}",
                text=claim_text,
                citations=_preferred_citation_ids(cluster_source_ids, source_registry, limit=3),
                source_ids=cluster_source_ids,
                unit_id=cluster[0].unit_id,
                evidence_ids=_dedupe_preserve_order([item.evidence_id for item in cluster]),
                evidence_bindings=_build_claim_evidence_bindings(
                    cluster,
                    registry_by_id,
                    section_id=section_id,
                    pool_mode="selected" if selected_bank_evidence_ids else ("global" if enforce_overlap else "candidate"),
                    selected_evidence_ids=list(selected_bank_evidence_ids),
                    question_ids=question_ids,
                ),
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
            section_claim_keys.add(claim_key)
            claim_counter["value"] += 1
            if len(section_claims) >= 2:
                break
        if not section_claims:
            return None
        section_summary = _build_section_summary(section_claims)
        section_source_count = len({citation for claim in section_claims for citation in claim.get("citations", [])})
        section_domain_count = _supporting_domain_count(
            [citation for claim in section_claims for citation in claim.get("citations", [])],
            registry_by_id,
        )
        section_model = DeepResearchSectionCitations(
            section_id=section_id,
            title=section_title,
            summary=section_summary,
            prose="",
            claims=section_claims,
            citations=sorted({citation for claim in section_claims for citation in claim.get("citations", [])}),
            source_ids=_dedupe_preserve_order(
                [
                    source_id
                    for claim in section_claims
                    for source_id in claim.get("source_ids", [])
                    if str(source_id).strip()
                ]
            ),
            evidence_ids=_dedupe_preserve_order(
                [
                    evidence_id
                    for claim in section_claims
                    for evidence_id in claim.get("evidence_ids", [])
                    if str(evidence_id).strip()
                ]
            ),
            confidence=_cluster_confidence(
                source_count=section_source_count,
                evidence_count=sum(len(claim.get("evidence_ids", [])) for claim in section_claims),
                domain_count=section_domain_count,
            ),
            claim_cluster_count=len(section_claims),
            supporting_source_count=section_source_count,
            supporting_domain_count=section_domain_count,
            pool_mode="selected" if selected_bank_evidence_ids else ("global" if enforce_overlap else "candidate"),
            question_ids=question_ids,
        ).model_dump()
        section_model["prose"] = _build_section_prose(section_model)
        return section_model

    concrete_sections: list[dict[str, Any]] = []
    concrete_section_ids: set[str] = set()
    for section in outline:
        if not isinstance(section, dict):
            continue
        title = _section_value(section, "title")
        if (
            is_summary_section_title(title)
            or is_key_findings_section_title(title)
            or is_open_questions_section_title(title)
        ):
            continue
        pool_items, pool_mode = evidence_pool_for_section(
            _section_value(section, "section_id"),
            evidence_items=[item.model_dump() for item in claims_pool],
            section_banks=section_banks,
        )
        materialized = build_section_from_pool(
            section,
            [DeepResearchEvidenceItem.model_validate(item) for item in pool_items],
            enforce_overlap=pool_mode == "global",
        )
        if materialized is None:
            continue
        concrete_sections.append(materialized)
        concrete_section_ids.add(_section_value(materialized, "section_id"))

    def build_generic_section(section: dict[str, Any]) -> dict[str, Any] | None:
        if not concrete_sections:
            return build_section_from_pool(section, claims_pool, enforce_overlap=False)
        derived_claims: list[dict[str, Any]] = []
        seen_claim_keys: set[str] = set()
        for concrete in concrete_sections:
            concrete_claims = [dict(claim) for claim in concrete.get("claims", []) if isinstance(claim, dict)]
            if not concrete_claims:
                continue
            synthesized_text = _synthesize_rollup_claim_text(
                _section_value(section, "title"),
                str(concrete.get("summary", "") or concrete_claims[0].get("text", "")),
            )
            claim_key = _stable_text_key(synthesized_text)
            if not synthesized_text or claim_key in seen_claim_keys:
                continue
            base_claim = concrete_claims[0]
            derived_claims.append(
                DeepResearchClaim(
                    claim_id=f"{_section_value(section, 'section_id')}-claim-{len(derived_claims) + 1}",
                    text=synthesized_text,
                    citations=_preferred_citation_ids(
                        [
                            str(source_id).strip()
                            for source_id in base_claim.get("source_ids", []) or concrete.get("source_ids", []) or []
                            if str(source_id).strip()
                        ],
                        source_registry,
                        limit=3,
                    ),
                    source_ids=_dedupe_preserve_order(
                        [
                            str(source_id).strip()
                            for source_id in base_claim.get("source_ids", []) or concrete.get("source_ids", []) or []
                            if str(source_id).strip()
                        ]
                    ),
                    unit_id=str(base_claim.get("unit_id", "") or concrete.get("section_id", "")).strip(),
                    evidence_ids=_dedupe_preserve_order(
                        [
                            str(evidence_id).strip()
                            for evidence_id in base_claim.get("evidence_ids", []) or concrete.get("evidence_ids", []) or []
                            if str(evidence_id).strip()
                        ]
                    ),
                    evidence_bindings=[
                        {
                            **dict(binding),
                            "section_id": _section_value(section, "section_id"),
                            "pool_mode": "selected",
                            "selected_for_section": True,
                        }
                        for binding in base_claim.get("evidence_bindings", []) or []
                        if isinstance(binding, dict)
                    ],
                    cluster_type="rollup",
                    supporting_source_count=len(
                        {
                            str(source_id).strip()
                            for source_id in base_claim.get("source_ids", []) or concrete.get("source_ids", []) or []
                            if str(source_id).strip()
                        }
                    ),
                    supporting_domain_count=_supporting_domain_count(
                        [
                            str(source_id).strip()
                            for source_id in base_claim.get("source_ids", []) or concrete.get("source_ids", []) or []
                            if str(source_id).strip()
                        ],
                        registry_by_id,
                    ),
                    confidence=str(concrete.get("confidence", "") or base_claim.get("confidence", "") or "medium"),
                ).model_dump()
            )
            seen_claim_keys.add(claim_key)
            if len(derived_claims) >= 2:
                break
            if len(derived_claims) >= 2:
                break
        if not derived_claims:
            return None
        section_source_count = len({citation for claim in derived_claims for citation in claim.get("citations", [])})
        section_domain_count = _supporting_domain_count(
            [citation for claim in derived_claims for citation in claim.get("citations", [])],
            registry_by_id,
        )
        materialized = DeepResearchSectionCitations(
            section_id=_section_value(section, "section_id"),
            title=_section_value(section, "title"),
            summary=_build_section_summary(derived_claims),
            prose="",
            claims=derived_claims,
            citations=sorted({citation for claim in derived_claims for citation in claim.get("citations", [])}),
            source_ids=_dedupe_preserve_order(
                [
                    source_id
                    for claim in derived_claims
                    for source_id in claim.get("source_ids", [])
                    if str(source_id).strip()
                ]
            ),
            evidence_ids=_dedupe_preserve_order(
                [
                    evidence_id
                    for claim in derived_claims
                    for evidence_id in claim.get("evidence_ids", [])
                    if str(evidence_id).strip()
                ]
            ),
            confidence=_cluster_confidence(
                source_count=section_source_count,
                evidence_count=sum(len(claim.get("evidence_ids", [])) for claim in derived_claims),
                domain_count=section_domain_count,
            ),
            claim_cluster_count=len(derived_claims),
            supporting_source_count=section_source_count,
            supporting_domain_count=section_domain_count,
        ).model_dump()
        materialized["prose"] = _build_section_prose(materialized)
        return materialized

    def build_open_question_section(section: dict[str, Any]) -> dict[str, Any] | None:
        evidence_by_id = {
            item.evidence_id: item
            for item in claims_pool
            if item.evidence_id
        }
        rejected_ids = [
            str(evidence_id).strip()
            for bank in section_banks or []
            if isinstance(bank, dict)
            for evidence_id in bank.get("rejected_evidence_ids", []) or []
            if str(evidence_id).strip() in evidence_by_id
        ]
        if not rejected_ids:
            rejected_ids = [
                str(entry.get("evidence_id", "")).strip()
                for entry in evidence_ledger or []
                if isinstance(entry, dict)
                and str(entry.get("evidence_id", "")).strip() in evidence_by_id
                and (
                    str(entry.get("disposition", "")).strip() == "rejected"
                    or any(str(section_id).strip() for section_id in entry.get("rejected_section_ids", []) or [])
                )
            ]
        rejected_pool = [
            evidence_by_id[evidence_id]
            for evidence_id in _dedupe_preserve_order(rejected_ids)
            if evidence_id in evidence_by_id
            and _has_gap_signal(
                f"{evidence_by_id[evidence_id].summary} {evidence_by_id[evidence_id].detail}"
            )
        ]
        if not rejected_pool:
            return None
        return build_section_from_pool(section, rejected_pool, enforce_overlap=False)

    for section in outline:
        if not isinstance(section, dict):
            continue
        section_id = _section_value(section, "section_id")
        title = _section_value(section, "title")
        if section_id in concrete_section_ids:
            continue
        if is_summary_section_title(title) or is_key_findings_section_title(title):
            materialized = build_generic_section(section)
            if materialized is not None:
                sections.append(materialized)
            continue
        if is_open_questions_section_title(title):
            materialized = build_open_question_section(section)
            if materialized is not None:
                sections.append(materialized)
            continue
        pool_items, pool_mode = evidence_pool_for_section(
            section_id,
            evidence_items=[item.model_dump() for item in claims_pool],
            section_banks=section_banks,
        )
        materialized = build_section_from_pool(
            section,
            [DeepResearchEvidenceItem.model_validate(item) for item in pool_items],
            enforce_overlap=pool_mode == "global",
        )
        if materialized is not None:
            sections.append(materialized)

    sections.extend(concrete_sections)
    return sorted(sections, key=lambda section: outline_position.get(str(section.get("section_id", "")), 10_000))


def _build_section_summary(section_claims: list[dict[str, Any]]) -> str:
    summary_parts: list[str] = []
    for claim in section_claims:
        text = _strip_summary_scaffolding(
            _summarize_evidence_text(str(claim.get("text", "")), limit=220)
        )
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
        section_summary = str(section.get("summary", "") or "").strip()
        section_prose = str(section.get("prose", "") or "").strip()
        if section_summary and (
            not section_prose
            or not _stable_text_equivalent(section_summary, section_prose)
        ):
            lines.append(section_summary)
            lines.append("")
        if section_prose:
            lines.append(section_prose)
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


def _fallback_final_report_text(report_value: dict[str, Any] | None) -> str | None:
    if not isinstance(report_value, dict):
        return None
    query = _normalize_whitespace(str(report_value.get("query", "") or ""))
    summary = _normalize_whitespace(str(report_value.get("summary", "") or ""))
    sections = report_value.get("sections")
    if not isinstance(sections, list):
        sections = []
    lines = [f"# {query or 'Final Report'}", ""]
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    for section in sections:
        if not isinstance(section, dict):
            continue
        title = _normalize_whitespace(str(section.get("title", "") or ""))
        if not title:
            continue
        lines.extend([f"## {title}", ""])
        section_summary = _normalize_whitespace(str(section.get("summary", "") or ""))
        if section_summary:
            lines.extend([section_summary, ""])
        prose = _normalize_whitespace(str(section.get("prose", "") or ""))
        if prose:
            lines.extend([prose, ""])
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            text = _normalize_whitespace(str(claim.get("text", "") or ""))
            if text:
                lines.extend([f"- {text}", ""])
    rendered = "\n".join(lines).strip()
    return rendered + "\n" if rendered else None


async def _search_query(query: str, *, effort: str = "standard") -> tuple[str, list[dict]]:
    result = await _search_query_with_details(query, effort=effort)
    return result["answer"], result["sources"]


async def _search_query_with_details(query: str, *, effort: str = "standard") -> dict[str, Any]:
    from . import server as server_module

    provider, provider_meta = await _build_runtime_grok_provider(effort=effort)
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
