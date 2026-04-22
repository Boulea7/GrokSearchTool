import hashlib
import re
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

_GENERIC_SECTION_TITLES = {
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


def _normalize_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def _is_generic_section_title(title: str) -> bool:
    return _normalize_whitespace(title).lower() in _GENERIC_SECTION_TITLES


def _tokenize_keywords(value: str) -> list[str]:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", _normalize_whitespace(value))
    return [
        token
        for token in re.findall(r"[A-Za-z0-9]+", normalized.lower().replace("-", " ").replace("_", " "))
        if token and token not in _STOPWORDS
    ]


def _count_keyword_overlap(text: str, keywords: list[str]) -> int:
    if not text or not keywords:
        return 0
    haystack_tokens = set(_tokenize_keywords(text))
    return sum(1 for keyword in keywords if keyword in haystack_tokens)


def _evidence_surface_text(
    evidence: dict[str, Any],
    *,
    origin_query: str = "",
    include_origin_query: bool = True,
) -> str:
    return " ".join(
        _normalize_whitespace(str(item))
        for item in [
            evidence.get("summary", ""),
            evidence.get("detail", ""),
            evidence.get("derived_from_source_url", ""),
            *list(evidence.get("source_urls", []) or []),
            origin_query if include_origin_query else "",
        ]
        if _normalize_whitespace(str(item))
    )


def initialize_section_banks(plan: DeepResearchPlan, *, updated_at: str = "") -> list[dict[str, Any]]:
    return [
        DeepResearchSectionEvidenceBank(
            section_id=section.section_id,
            last_updated_at=updated_at,
        ).model_dump()
        for section in plan.report_outline
    ]


def initialize_section_banks_for_outline(
    outline_sections: list[dict[str, Any]],
    *,
    updated_at: str = "",
) -> list[dict[str, Any]]:
    return [
        DeepResearchSectionEvidenceBank(
            section_id=str(section.get("section_id", "")).strip(),
            last_updated_at=updated_at,
        ).model_dump()
        for section in outline_sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    ]


def _entry_question_ids(entry: dict[str, Any]) -> list[str]:
    return [
        str(question_id).strip()
        for question_id in (
            list(entry.get("question_ids", []) or [])
            + ([entry.get("question_id", "")] if entry.get("question_id") else [])
        )
        if str(question_id).strip()
    ]


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


def _materialized_section_ids(entry: dict[str, Any]) -> list[str]:
    section_ids: list[str] = []
    for claim_id in entry.get("materialized_claim_ids", []) or []:
        normalized_claim_id = str(claim_id).strip()
        if not normalized_claim_id or "-claim-" not in normalized_claim_id:
            continue
        section_id = normalized_claim_id.rsplit("-claim-", 1)[0].strip()
        if section_id and section_id not in section_ids:
            section_ids.append(section_id)
    return section_ids


def sync_section_banks_to_outline(
    section_banks: list[dict[str, Any]],
    *,
    planned_outline: list[dict[str, Any]],
    updated_at: str = "",
) -> list[dict[str, Any]]:
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    ordered_bank_ids: list[str] = []
    for section in planned_outline:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        if not section_id:
            continue
        if section_id not in banks_by_section:
            banks_by_section[section_id] = DeepResearchSectionEvidenceBank(
                section_id=section_id,
                last_updated_at=updated_at,
            ).model_dump()
        ordered_bank_ids.append(section_id)
    for bank in banks_by_section.values():
        bank.setdefault("candidate_evidence_ids", [])
        bank.setdefault("selected_evidence_ids", [])
        bank.setdefault("rejected_evidence_ids", [])
        bank.setdefault("candidate_packets", [])
        bank.setdefault("selected_packets", [])
        bank.setdefault("rejected_packets", [])
        bank["last_updated_at"] = updated_at or bank.get("last_updated_at", "")
    for section_id in sorted(banks_by_section):
        if section_id not in ordered_bank_ids:
            ordered_bank_ids.append(section_id)
    return [
        DeepResearchSectionEvidenceBank.model_validate(banks_by_section[section_id]).model_dump()
        for section_id in ordered_bank_ids
    ]


def seed_section_banks_from_question_bindings(
    section_banks: list[dict[str, Any]],
    *,
    planned_outline: list[dict[str, Any]],
    ledger_entries: list[dict[str, Any]],
    updated_at: str = "",
) -> list[dict[str, Any]]:
    seeded = sync_section_banks_to_outline(
        section_banks,
        planned_outline=planned_outline,
        updated_at=updated_at,
    )
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in seeded
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }

    def _upsert_packet(packet_list: list[dict[str, Any]], packet: dict[str, Any]) -> None:
        evidence_id = str(packet.get("evidence_id", "")).strip()
        for index, existing in enumerate(packet_list):
            if str(existing.get("evidence_id", "")).strip() == evidence_id:
                packet_list[index] = packet
                return
        packet_list.append(packet)

    for section in planned_outline:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        if not section_id or section_id not in banks_by_section:
            continue
        bank = banks_by_section[section_id]
        section_question_ids = set(_section_question_ids(section))
        if not section_question_ids or bank.get("selected_evidence_ids") or bank.get("candidate_evidence_ids"):
            continue
        for entry in ledger_entries:
            if not isinstance(entry, dict):
                continue
            evidence_id = str(entry.get("evidence_id", "")).strip()
            if not evidence_id:
                continue
            if str(entry.get("disposition", "")).strip() == "rejected":
                continue
            entry_question_ids = set(_entry_question_ids(entry))
            if not (section_question_ids & entry_question_ids):
                continue
            if evidence_id not in bank["candidate_evidence_ids"]:
                bank["candidate_evidence_ids"].append(evidence_id)
            packet = _packet_for_section(entry, section_id=section_id)
            _upsert_packet(bank["candidate_packets"], packet)
            if (
                str(entry.get("selected_section_id", "")).strip() == section_id
                or section_id in _materialized_section_ids(entry)
            ):
                if evidence_id not in bank["selected_evidence_ids"]:
                    bank["selected_evidence_ids"].append(evidence_id)
                _upsert_packet(bank["selected_packets"], packet)
        bank["last_updated_at"] = updated_at or bank.get("last_updated_at", "")

    ordered_ids = [
        str(section.get("section_id", "")).strip()
        for section in planned_outline
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    ]
    for section_id in sorted(banks_by_section):
        if section_id not in ordered_ids:
            ordered_ids.append(section_id)
    return [
        DeepResearchSectionEvidenceBank.model_validate(banks_by_section[section_id]).model_dump()
        for section_id in ordered_ids
    ]


def _candidate_section_ids(
    plan: DeepResearchPlan,
    evidence: dict[str, Any],
    *,
    origin_query: str = "",
) -> list[str]:
    evidence_surface = _evidence_surface_text(
        evidence,
        origin_query=origin_query,
        include_origin_query=False,
    )
    scored_matches: list[tuple[int, int, str]] = []
    for section in plan.report_outline:
        keywords = _tokenize_keywords(f"{section.title} {section.goal}")
        overlap = _count_keyword_overlap(evidence_surface, keywords)
        if overlap > 0:
            scored_matches.append((overlap, len(keywords), section.section_id))
    matches = [
        section_id
        for _, _, section_id in sorted(
            scored_matches,
            key=lambda item: (item[0], item[1], item[2]),
            reverse=True,
        )
    ]
    if (
        not matches
        and len(plan.report_outline) == 1
        and _is_generic_section_title(plan.report_outline[0].title)
    ):
        return [plan.report_outline[0].section_id]
    return matches


def _selected_section_id_for_evidence(
    plan: DeepResearchPlan,
    *,
    candidate_section_ids: list[str],
    evidence_text: str,
    question_ids: list[str] | None = None,
    source_hint_text: str = "",
) -> str:
    normalized_candidates = [
        str(section_id).strip()
        for section_id in candidate_section_ids
        if str(section_id).strip()
    ]
    if len(normalized_candidates) <= 1:
        return normalized_candidates[0] if normalized_candidates else ""
    question_text_by_id = {
        item.id: _normalize_whitespace(item.question)
        for item in plan.sub_questions
        if _normalize_whitespace(item.question)
    }
    outline_index_by_section_id = {
        section.section_id: index
        for index, section in enumerate(plan.report_outline)
    }
    scored_candidates: list[tuple[int, int, int, int, str]] = []
    for section_id in normalized_candidates:
        selection_score, selection_basis_tokens = _selection_score(
            plan,
            evidence_text=evidence_text,
            selected_section_id=section_id,
        )
        source_hint_score = _count_keyword_overlap(source_hint_text, selection_basis_tokens)
        question_alignment_score = sum(
            _count_keyword_overlap(
                f"{question_text_by_id.get(question_id, '')} {evidence_text}",
                selection_basis_tokens,
            )
            for question_id in question_ids or []
            if question_id in question_text_by_id
        )
        specific_section_bonus = int(
            not _is_generic_section_title(
                next(
                    (
                        section.title
                        for section in plan.report_outline
                        if section.section_id == section_id
                    ),
                    "",
                )
            )
        )
        scored_candidates.append(
            (
                source_hint_score,
                question_alignment_score,
                selection_score,
                specific_section_bonus,
                -outline_index_by_section_id.get(section_id, 10_000),
                section_id,
            )
        )
    if not scored_candidates:
        return ""
    ranked = sorted(
        scored_candidates,
        key=lambda item: (item[0], item[1], item[2], item[3], item[4], item[5]),
        reverse=True,
    )
    if len(ranked) == 1:
        return ranked[0][5]
    _, _, first_score, _, _, first_section_id = ranked[0]
    _, _, second_score, _, _, _ = ranked[1]
    if first_score <= 0:
        return ""
    return first_section_id


def _selection_score(
    plan: DeepResearchPlan,
    *,
    evidence_text: str,
    selected_section_id: str,
) -> tuple[int, list[str]]:
    selected_section = next(
        (section for section in plan.report_outline if section.section_id == selected_section_id),
        None,
    )
    if selected_section is None:
        return 0, []
    section_keywords = _tokenize_keywords(f"{selected_section.title} {selected_section.goal}")
    return _count_keyword_overlap(evidence_text, section_keywords), section_keywords


def _packet_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    excerpt = _normalize_whitespace(str(entry.get("summary", "")))
    excerpt_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest() if excerpt else ""
    line_start = entry.get("line_start")
    line_end = entry.get("line_end")
    source_backed = bool(entry.get("derived_from_source_url")) or (
        line_start is not None and line_end is not None
    )
    return {
        "evidence_id": str(entry.get("evidence_id", "")).strip(),
        "source_ids": [str(source_id).strip() for source_id in entry.get("source_ids", []) if str(source_id).strip()],
        "question_ids": [
            str(question_id).strip() for question_id in entry.get("question_ids", []) if str(question_id).strip()
        ]
        or ([str(entry.get("question_id", "")).strip()] if str(entry.get("question_id", "")).strip() else []),
        "unit_id": str(entry.get("unit_id", "")).strip(),
        "source_backed": source_backed,
        "line_span_complete": isinstance(line_start, int) and isinstance(line_end, int) and line_end >= line_start,
        "excerpt_hash": excerpt_hash,
        "claim_ids": [
            str(claim_id).strip()
            for claim_id in entry.get("materialized_claim_ids", []) or []
            if str(claim_id).strip()
        ],
    }


def _packet_for_section(entry: dict[str, Any], *, section_id: str) -> dict[str, Any]:
    packet = _packet_from_entry(entry)
    normalized_section_id = str(section_id).strip()
    if not normalized_section_id:
        return packet
    packet["claim_ids"] = [
        claim_id
        for claim_id in packet.get("claim_ids", [])
        if str(claim_id).strip().startswith(f"{normalized_section_id}-")
    ]
    return packet


def _section_decisions(
    *,
    candidate_section_ids: list[str],
    selected_section_id: str,
    rejected_section_ids: list[str],
) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = []
    seen: set[str] = set()
    for section_id in candidate_section_ids:
        normalized = str(section_id).strip()
        if not normalized or normalized in seen:
            continue
        state = "pending"
        if normalized == selected_section_id:
            state = "selected"
        elif normalized in rejected_section_ids:
            state = "rejected"
        decisions.append({"section_id": normalized, "state": state})
        seen.add(normalized)
    return decisions


def _question_ids_for_evidence(
    plan: DeepResearchPlan,
    *,
    evidence_text: str,
    evidence_context_text: str = "",
    selected_section_id: str,
) -> list[str]:
    selected_section = next(
        (section for section in plan.report_outline if section.section_id == selected_section_id),
        None,
    )
    if selected_section is not None:
        section_keywords = _tokenize_keywords(f"{selected_section.title} {selected_section.goal}")
        matched_by_section = [
            item.id
            for item in plan.sub_questions
            if _count_keyword_overlap(item.question, section_keywords) > 0
        ]
        if matched_by_section:
            return matched_by_section

    evidence_keywords = _tokenize_keywords(f"{evidence_text} {evidence_context_text}")
    return [
        item.id
        for item in plan.sub_questions
        if _count_keyword_overlap(item.question, evidence_keywords) > 0
    ]


def _prioritize_question_ids_for_section(
    plan: DeepResearchPlan,
    *,
    section_id: str,
    question_ids: list[str],
) -> list[str]:
    normalized_section_id = str(section_id).strip()
    if not normalized_section_id:
        return [str(question_id).strip() for question_id in question_ids if str(question_id).strip()]
    selected_section = next(
        (section for section in plan.report_outline if section.section_id == normalized_section_id),
        None,
    )
    if selected_section is None:
        return [str(question_id).strip() for question_id in question_ids if str(question_id).strip()]
    section_keywords = _tokenize_keywords(f"{selected_section.title} {selected_section.goal}")
    scored_question_ids: list[tuple[int, str]] = []
    for item in plan.sub_questions:
        if item.id not in question_ids:
            continue
        score = _count_keyword_overlap(item.question, section_keywords)
        if score <= 0:
            continue
        scored_question_ids.append((score, item.id))
    if scored_question_ids:
        top_score = max(score for score, _ in scored_question_ids)
        return [
            question_id
            for score, question_id in sorted(scored_question_ids, key=lambda item: (item[0], item[1]), reverse=True)
            if score == top_score
        ]
    return [str(question_id).strip() for question_id in question_ids if str(question_id).strip()]


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
        evidence_text = str(evidence.get("summary") or evidence.get("detail") or "")
        evidence_context_text = _evidence_surface_text(
            evidence,
            origin_query=origin_query,
            include_origin_query=False,
        )
        candidate_section_ids = _candidate_section_ids(plan, evidence, origin_query=origin_query)
        broad_question_ids = _question_ids_for_evidence(
            plan,
            evidence_text=evidence_text,
            evidence_context_text=evidence_context_text,
            selected_section_id="",
        )
        selected_section_id = _selected_section_id_for_evidence(
            plan,
            candidate_section_ids=candidate_section_ids,
            evidence_text=evidence_context_text,
            question_ids=broad_question_ids,
            source_hint_text=" ".join(str(url).strip() for url in evidence.get("source_urls", []) or []),
        )
        question_ids = _question_ids_for_evidence(
            plan,
            evidence_text=evidence_text,
            evidence_context_text=evidence_context_text,
            selected_section_id=selected_section_id,
        )
        if not question_ids:
            question_ids = broad_question_ids
        question_ids = _prioritize_question_ids_for_section(
            plan,
            section_id=selected_section_id,
            question_ids=question_ids,
        )
        selection_score, selection_basis_tokens = _selection_score(
            plan,
            evidence_text=evidence_context_text,
            selected_section_id=selected_section_id,
        )
        entries.append(
            DeepResearchEvidenceLedgerEntry(
                ledger_id=f"ledger-{evidence_id}",
                evidence_id=evidence_id,
                unit_id=unit_id,
                question_id=question_ids[0] if question_ids else "",
                question_ids=question_ids,
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
                summary=evidence_text,
                evidence_kind=str(evidence.get("evidence_kind") or ""),
                derived_from_source_url=str(evidence.get("derived_from_source_url") or ""),
                line_start=evidence.get("line_start"),
                line_end=evidence.get("line_end"),
                selection_score=selection_score,
                selection_basis_tokens=selection_basis_tokens,
                decision_state="selected" if selected_section_id else "pending",
                section_decisions=_section_decisions(
                    candidate_section_ids=candidate_section_ids,
                    selected_section_id=selected_section_id,
                    rejected_section_ids=[
                        section_id for section_id in candidate_section_ids if section_id != selected_section_id
                    ],
                ),
                materialized_claim_ids=[],
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
        bank.setdefault("candidate_packets", [])
        bank.setdefault("selected_packets", [])
        bank.setdefault("rejected_packets", [])
        bank["last_updated_at"] = updated_at or bank.get("last_updated_at", "")

    def _upsert_packet(packet_list: list[dict[str, Any]], packet: dict[str, Any]) -> None:
        evidence_id = str(packet.get("evidence_id", "")).strip()
        for index, existing in enumerate(packet_list):
            if str(existing.get("evidence_id", "")).strip() == evidence_id:
                packet_list[index] = packet
                return
        packet_list.append(packet)

    for entry in ledger_entries:
        if not isinstance(entry, dict):
            continue
        evidence_id = str(entry.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        materialized_section_ids = _materialized_section_ids(entry)
        for section_id in entry.get("candidate_section_ids", []):
            bank = banks_by_section.get(section_id)
            if bank is None:
                continue
            packet = _packet_for_section(entry, section_id=section_id)
            if evidence_id not in bank["candidate_evidence_ids"]:
                bank["candidate_evidence_ids"].append(evidence_id)
            _upsert_packet(bank["candidate_packets"], packet)
        selected_section_ids = []
        selected_section_id = str(entry.get("selected_section_id", "")).strip()
        if selected_section_id:
            selected_section_ids.append(selected_section_id)
        for section_id in materialized_section_ids:
            if section_id not in selected_section_ids:
                selected_section_ids.append(section_id)
        for section_id in selected_section_ids:
            bank = banks_by_section.get(section_id)
            if bank is None:
                continue
            packet = _packet_for_section(entry, section_id=section_id)
            if evidence_id not in bank["selected_evidence_ids"]:
                bank["selected_evidence_ids"].append(evidence_id)
            _upsert_packet(bank["selected_packets"], packet)
        for rejected_section_id in entry.get("rejected_section_ids", []):
            bank = banks_by_section.get(rejected_section_id)
            if bank is None:
                continue
            packet = _packet_for_section(entry, section_id=rejected_section_id)
            if evidence_id not in bank["rejected_evidence_ids"]:
                bank["rejected_evidence_ids"].append(evidence_id)
            _upsert_packet(bank["rejected_packets"], packet)

    return [
        DeepResearchSectionEvidenceBank.model_validate(bank).model_dump()
        for _, bank in sorted(banks_by_section.items())
    ]


def reconcile_section_banks_with_materialized_sections(
    section_banks: list[dict[str, Any]],
    *,
    sections: list[dict[str, Any]],
    updated_at: str = "",
) -> list[dict[str, Any]]:
    banks_by_section = {
        str(bank.get("section_id", "")).strip(): dict(bank)
        for bank in section_banks
        if isinstance(bank, dict) and str(bank.get("section_id", "")).strip()
    }
    claim_ids_by_section_evidence: dict[str, dict[str, list[str]]] = {}
    question_ids_by_section_evidence: dict[str, dict[str, list[str]]] = {}

    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", "")).strip()
        if not section_id:
            continue
        section_question_ids = _section_question_ids(section)
        for claim in section.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("claim_id", "")).strip()
            if not claim_id:
                continue
            for evidence_id in claim.get("evidence_ids", []) or []:
                normalized_evidence_id = str(evidence_id).strip()
                if not normalized_evidence_id:
                    continue
                claim_ids_by_section_evidence.setdefault(section_id, {}).setdefault(normalized_evidence_id, [])
                if claim_id not in claim_ids_by_section_evidence[section_id][normalized_evidence_id]:
                    claim_ids_by_section_evidence[section_id][normalized_evidence_id].append(claim_id)
                if section_question_ids:
                    question_ids_by_section_evidence.setdefault(section_id, {}).setdefault(normalized_evidence_id, [])
                    for question_id in section_question_ids:
                        if question_id not in question_ids_by_section_evidence[section_id][normalized_evidence_id]:
                            question_ids_by_section_evidence[section_id][normalized_evidence_id].append(question_id)

    def _packet_map(bank: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
        return {
            str(packet.get("evidence_id", "")).strip(): dict(packet)
            for packet in bank.get(key, []) or []
            if isinstance(packet, dict) and str(packet.get("evidence_id", "")).strip()
        }

    reconciled: list[dict[str, Any]] = []
    for section_id, bank in sorted(banks_by_section.items()):
        updated_bank = dict(bank)
        selected_packet_map = _packet_map(updated_bank, "selected_packets")
        candidate_packet_map = _packet_map(updated_bank, "candidate_packets")
        claim_ids_by_evidence = claim_ids_by_section_evidence.get(section_id, {})
        selected_evidence_ids = [
            evidence_id
            for evidence_id in updated_bank.get("selected_evidence_ids", []) or []
            if str(evidence_id).strip() in claim_ids_by_evidence
        ]
        for evidence_id in claim_ids_by_evidence:
            if evidence_id not in selected_evidence_ids:
                selected_evidence_ids.append(evidence_id)
        updated_bank["selected_evidence_ids"] = [str(evidence_id).strip() for evidence_id in selected_evidence_ids if str(evidence_id).strip()]
        updated_packets: list[dict[str, Any]] = []
        for evidence_id in updated_bank["selected_evidence_ids"]:
            packet = (
                dict(selected_packet_map.get(evidence_id) or {})
                or dict(candidate_packet_map.get(evidence_id) or {})
                or {"evidence_id": evidence_id}
            )
            packet["evidence_id"] = evidence_id
            packet["claim_ids"] = list(claim_ids_by_evidence.get(evidence_id, []))
            packet["question_ids"] = list(
                question_ids_by_section_evidence.get(section_id, {}).get(
                    evidence_id,
                    packet.get("question_ids", []) or [],
                )
            )
            updated_packets.append(packet)
        updated_bank["selected_packets"] = updated_packets
        updated_bank["last_updated_at"] = updated_at or updated_bank.get("last_updated_at", "")
        reconciled.append(DeepResearchSectionEvidenceBank.model_validate(updated_bank).model_dump())
    return reconciled


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
