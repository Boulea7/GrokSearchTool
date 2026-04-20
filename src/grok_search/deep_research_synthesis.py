from __future__ import annotations

import re
from typing import Any

from .deep_research_types import DeepResearchPlan


_SUMMARY_TITLES = {"executive summary", "summary"}
_KEY_FINDINGS_TITLES = {"key findings", "findings", "main findings"}
_OPEN_QUESTION_TITLES = {"open questions", "remaining gaps", "next steps", "recommendations"}
_GENERIC_OUTLINE_TITLES = _SUMMARY_TITLES | _KEY_FINDINGS_TITLES | _OPEN_QUESTION_TITLES


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def _trim_text(value: str, *, limit: int = 96) -> str:
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


def is_summary_section_title(title: str) -> bool:
    return _normalize_whitespace(title).lower() in _SUMMARY_TITLES


def is_key_findings_section_title(title: str) -> bool:
    return _normalize_whitespace(title).lower() in _KEY_FINDINGS_TITLES


def is_open_questions_section_title(title: str) -> bool:
    return _normalize_whitespace(title).lower() in _OPEN_QUESTION_TITLES


def is_generic_section_title(title: str) -> bool:
    return _normalize_whitespace(title).lower() in _GENERIC_OUTLINE_TITLES


def _outline_section_dicts(plan: DeepResearchPlan) -> list[dict[str, Any]]:
    return [
        {
            "section_id": section.section_id,
            "title": section.title,
            "goal": section.goal,
            "status": section.status or "",
            "coverage_state": dict(section.coverage_state or {}),
            "rewrite_reason": section.rewrite_reason or "",
        }
        for section in plan.report_outline
    ]


def _selected_question_ids(evidence_ledger: list[dict[str, Any]] | None) -> list[str]:
    question_ids: list[str] = []
    fallback_question_ids: list[str] = []
    for entry in evidence_ledger or []:
        if not isinstance(entry, dict):
            continue
        entry_question_ids = _dedupe_preserve_order(
            [
                str(question_id).strip()
                for question_id in (
                    list(entry.get("question_ids", []) or [])
                    + ([entry.get("question_id", "")] if entry.get("question_id") else [])
                )
                if str(question_id).strip()
            ]
        )
        if not entry_question_ids:
            continue
        disposition = str(entry.get("disposition", "")).strip()
        selected_section_id = str(entry.get("selected_section_id", "")).strip()
        if disposition == "selected" or selected_section_id:
            for question_id in entry_question_ids:
                if question_id not in question_ids:
                    question_ids.append(question_id)
            continue
        if disposition != "rejected":
            for question_id in entry_question_ids:
                if question_id not in fallback_question_ids:
                    fallback_question_ids.append(question_id)
    return question_ids or fallback_question_ids


def _has_open_question_signal(
    *,
    section_banks: list[dict[str, Any]] | None,
    evidence_ledger: list[dict[str, Any]] | None,
) -> bool:
    for bank in section_banks or []:
        if not isinstance(bank, dict):
            continue
        if any(str(evidence_id).strip() for evidence_id in bank.get("rejected_evidence_ids", []) or []):
            return True
    for entry in evidence_ledger or []:
        if not isinstance(entry, dict):
            continue
        if any(str(section_id).strip() for section_id in entry.get("rejected_section_ids", []) or []):
            return True
        if str(entry.get("disposition", "")).strip() == "rejected":
            return True
    return False


def build_synthesis_outline(
    plan: DeepResearchPlan,
    *,
    section_graph: dict[str, Any] | None = None,
    section_banks: list[dict[str, Any]] | None = None,
    evidence_ledger: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    outline = _outline_section_dicts(plan)
    specific_sections = [
        section
        for section in outline
        if not is_generic_section_title(str(section.get("title", "")))
    ]
    if specific_sections:
        if not isinstance(section_graph, dict):
            return outline
        rooted_ids = [
            str(section_id).strip()
            for section_id in section_graph.get("root_section_ids", []) or []
            if str(section_id).strip()
        ]
        if not rooted_ids:
            return outline
        order = {section_id: index for index, section_id in enumerate(rooted_ids)}
        return sorted(
            outline,
            key=lambda section: (
                order.get(str(section.get("section_id", "")).strip(), 10_000),
                0 if is_summary_section_title(str(section.get("title", ""))) else 1,
            ),
        )

    selected_question_ids = _selected_question_ids(evidence_ledger)
    if not selected_question_ids:
        return outline

    summary_sections = [section for section in outline if is_summary_section_title(str(section.get("title", "")))]
    key_findings_sections = [section for section in outline if is_key_findings_section_title(str(section.get("title", "")))]
    open_question_sections = [section for section in outline if is_open_questions_section_title(str(section.get("title", "")))]
    derived_sections: list[dict[str, Any]] = []
    used_section_ids = {
        str(section.get("section_id", "")).strip()
        for section in summary_sections + key_findings_sections + open_question_sections
        if str(section.get("section_id", "")).strip()
    }
    for item in plan.sub_questions:
        if item.id not in selected_question_ids:
            continue
        title = _trim_text(_normalize_whitespace(item.question.rstrip(" ?")))
        if not title:
            continue
        section_id = _slugify(title)
        suffix = 2
        while section_id in used_section_ids:
            section_id = f"{_slugify(title)}-{suffix}"
            suffix += 1
        used_section_ids.add(section_id)
        derived_sections.append(
            {
                "section_id": section_id,
                "title": title,
                "goal": title,
                "question_id": item.id,
                "status": "grounded",
                "coverage_state": {},
                "rewrite_reason": "evidence_ledger",
            }
        )

    if not derived_sections:
        return outline

    active_outline = [
        *summary_sections,
        *key_findings_sections,
        *derived_sections,
    ]
    if _has_open_question_signal(section_banks=section_banks, evidence_ledger=evidence_ledger):
        active_outline.extend(open_question_sections)
    return active_outline


def evidence_pool_for_section(
    section_id: str,
    *,
    evidence_items: list[dict[str, Any]],
    section_banks: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    evidence_by_id = {
        str(item.get("evidence_id", "")).strip(): item
        for item in evidence_items
        if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
    }
    bank = next(
        (
            item
            for item in section_banks or []
            if isinstance(item, dict) and str(item.get("section_id", "")).strip() == section_id
        ),
        None,
    )
    if not isinstance(bank, dict):
        return list(evidence_items), "global"

    selected_packet_ids = [
        str(packet.get("evidence_id", "")).strip()
        for packet in bank.get("selected_packets", []) or []
        if isinstance(packet, dict) and str(packet.get("evidence_id", "")).strip() in evidence_by_id
    ]
    if selected_packet_ids:
        deduped_selected_packet_ids = _dedupe_preserve_order(selected_packet_ids)
        return [evidence_by_id[evidence_id] for evidence_id in deduped_selected_packet_ids], "selected"

    selected_ids = [
        str(evidence_id).strip()
        for evidence_id in bank.get("selected_evidence_ids", []) or []
        if str(evidence_id).strip() in evidence_by_id
    ]
    if selected_ids:
        return [evidence_by_id[evidence_id] for evidence_id in selected_ids], "selected"

    rejected_ids = {
        str(evidence_id).strip()
        for evidence_id in bank.get("rejected_evidence_ids", []) or []
        if str(evidence_id).strip()
    }
    candidate_packet_ids = [
        str(packet.get("evidence_id", "")).strip()
        for packet in bank.get("candidate_packets", []) or []
        if isinstance(packet, dict)
        and str(packet.get("evidence_id", "")).strip() in evidence_by_id
        and str(packet.get("evidence_id", "")).strip() not in rejected_ids
    ]
    if candidate_packet_ids:
        deduped_candidate_packet_ids = _dedupe_preserve_order(candidate_packet_ids)
        return [evidence_by_id[evidence_id] for evidence_id in deduped_candidate_packet_ids], "candidate"

    candidate_ids = [
        str(evidence_id).strip()
        for evidence_id in bank.get("candidate_evidence_ids", []) or []
        if str(evidence_id).strip() in evidence_by_id and str(evidence_id).strip() not in rejected_ids
    ]
    if candidate_ids:
        return [evidence_by_id[evidence_id] for evidence_id in candidate_ids], "candidate"

    return list(evidence_items), "global"
