from typing import Any

from .deep_research_types import (
    DeepResearchEvidenceLedgerEntry,
    DeepResearchPlan,
    DeepResearchSectionEvidenceBank,
)


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
    "of",
    "on",
    "or",
    "the",
    "to",
    "up",
    "with",
}


def _normalize_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def _tokenize_keywords(value: str) -> list[str]:
    return [
        token
        for token in _normalize_whitespace(value).lower().replace("-", " ").replace("_", " ").split()
        if token and token not in _STOPWORDS
    ]


def _count_keyword_overlap(text: str, keywords: list[str]) -> int:
    if not text or not keywords:
        return 0
    haystack = " " + _normalize_whitespace(text).lower() + " "
    return sum(1 for keyword in keywords if f" {keyword} " in haystack)


def initialize_section_banks(plan: DeepResearchPlan, *, updated_at: str = "") -> list[dict[str, Any]]:
    return [
        DeepResearchSectionEvidenceBank(
            section_id=section.section_id,
            last_updated_at=updated_at,
        ).model_dump()
        for section in plan.report_outline
    ]


def _candidate_section_ids(plan: DeepResearchPlan, evidence: dict[str, Any]) -> list[str]:
    summary = _normalize_whitespace(str(evidence.get("summary") or evidence.get("detail") or ""))
    matches: list[str] = []
    for section in plan.report_outline:
        keywords = _tokenize_keywords(f"{section.title} {section.goal}")
        overlap = _count_keyword_overlap(summary, keywords)
        if overlap > 0:
            matches.append(section.section_id)
    if not matches and len(plan.report_outline) == 1:
        matches.append(plan.report_outline[0].section_id)
    return matches


def build_evidence_ledger_entries(
    plan: DeepResearchPlan,
    *,
    unit_id: str,
    origin_query: str,
    evidence_items: list[dict[str, Any]],
    updated_at: str = "",
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for evidence in evidence_items:
        evidence_id = str(evidence.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        candidate_section_ids = _candidate_section_ids(plan, evidence)
        selected_section_id = candidate_section_ids[0] if candidate_section_ids else ""
        selected_section = next(
            (section for section in plan.report_outline if section.section_id == selected_section_id),
            None,
        )
        question_ids = [
            item.id
            for item in plan.sub_questions
            if selected_section is not None
            and _count_keyword_overlap(
                item.question,
                _tokenize_keywords(f"{selected_section.title} {selected_section.goal}"),
            )
            > 0
        ]
        entries.append(
            DeepResearchEvidenceLedgerEntry(
                ledger_id=f"ledger-{evidence_id}",
                evidence_id=evidence_id,
                unit_id=unit_id,
                question_id=question_ids[0] if question_ids else "",
                origin_query=origin_query,
                candidate_section_ids=candidate_section_ids,
                selected_section_id=selected_section_id,
                rejected_section_ids=[
                    section_id for section_id in candidate_section_ids if section_id != selected_section_id
                ],
                disposition="selected" if selected_section_id else "unbound",
                disposition_reason="keyword_overlap" if selected_section_id else "no_matching_section",
                source_ids=list(evidence.get("source_ids") or []),
                source_urls=list(evidence.get("source_urls") or []),
                summary=str(evidence.get("summary") or evidence.get("detail") or ""),
                evidence_kind=str(evidence.get("evidence_kind") or ""),
                recorded_at=updated_at,
            ).model_dump()
        )
    return entries


def merge_evidence_ledger(
    existing_entries: list[dict[str, Any]],
    new_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entry in existing_entries + new_entries:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        merged[evidence_id] = dict(entry)
    return [merged[evidence_id] for evidence_id in sorted(merged)]


def update_section_banks(
    section_banks: list[dict[str, Any]],
    *,
    ledger_entries: list[dict[str, Any]],
    updated_at: str = "",
) -> list[dict[str, Any]]:
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    for bank in banks_by_section.values():
        bank.setdefault("candidate_evidence_ids", [])
        bank.setdefault("selected_evidence_ids", [])
        bank.setdefault("rejected_evidence_ids", [])
        bank["last_updated_at"] = updated_at or bank.get("last_updated_at", "")

    for entry in ledger_entries:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        for section_id in entry.get("candidate_section_ids", []):
            bank = banks_by_section.get(section_id)
            if bank is None:
                continue
            if evidence_id not in bank["candidate_evidence_ids"]:
                bank["candidate_evidence_ids"].append(evidence_id)
        selected_section_id = str(entry.get("selected_section_id", "")).strip()
        if selected_section_id and selected_section_id in banks_by_section:
            bank = banks_by_section[selected_section_id]
            if evidence_id not in bank["selected_evidence_ids"]:
                bank["selected_evidence_ids"].append(evidence_id)
        for rejected_section_id in entry.get("rejected_section_ids", []):
            bank = banks_by_section.get(rejected_section_id)
            if bank is None:
                continue
            if evidence_id not in bank["rejected_evidence_ids"]:
                bank["rejected_evidence_ids"].append(evidence_id)

    return [
        DeepResearchSectionEvidenceBank.model_validate(bank).model_dump()
        for _, bank in sorted(banks_by_section.items())
    ]


def selected_evidence_ids_by_section(section_banks: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(bank.get("section_id", "")).strip(): list(bank.get("selected_evidence_ids") or [])
        for bank in section_banks
        if str(bank.get("section_id", "")).strip()
    }


def candidate_evidence_ids_by_section(section_banks: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(bank.get("section_id", "")).strip(): list(bank.get("candidate_evidence_ids") or [])
        for bank in section_banks
        if str(bank.get("section_id", "")).strip()
    }


def rejected_evidence_ids_by_section(section_banks: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        str(bank.get("section_id", "")).strip(): list(bank.get("rejected_evidence_ids") or [])
        for bank in section_banks
        if str(bank.get("section_id", "")).strip()
    }


def matched_unit_ids_by_section(ledger_entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    matched: dict[str, list[str]] = {}
    for entry in ledger_entries:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("selected_section_id", "")).strip()
        unit_id = str(entry.get("unit_id", "")).strip()
        if not section_id or not unit_id:
            continue
        matched.setdefault(section_id, [])
        if unit_id not in matched[section_id]:
            matched[section_id].append(unit_id)
    return matched


def evidence_source_ids(ledger_entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for entry in ledger_entries:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        mapping[evidence_id] = [
            str(source_id).strip()
            for source_id in entry.get("source_ids", [])
            if str(source_id).strip()
        ]
    return mapping
