from typing import Any

from .deep_research_types import DeepResearchPlan, DeepResearchSectionGraphState, DeepResearchSectionNode


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


def _section_question_ids(plan: DeepResearchPlan, section_title: str, section_goal: str) -> list[str]:
    haystack = f"{section_title} {section_goal}"
    question_ids: list[str] = []
    for item in plan.sub_questions:
        question = str(item.question or "").strip()
        if not question:
            continue
        tokens = _tokenize_keywords(question)
        if not tokens:
            continue
        if _count_keyword_overlap(haystack, tokens) >= max(1, min(2, len(tokens))):
            question_ids.append(item.id)
    return question_ids


def initialize_section_graph(plan: DeepResearchPlan, *, updated_at: str = "") -> dict[str, Any]:
    nodes = [
        DeepResearchSectionNode(
            section_id=section.section_id,
            title=section.title,
            goal=section.goal,
            question_ids=_section_question_ids(plan, section.title, section.goal),
            status=section.status or "planned",
            coverage_state=dict(section.coverage_state or {}),
            rewrite_reason=section.rewrite_reason,
            last_updated_at=updated_at,
        ).model_dump()
        for section in plan.report_outline
    ]
    return DeepResearchSectionGraphState(
        root_section_ids=[section.section_id for section in plan.report_outline],
        nodes=nodes,
    ).model_dump()


def update_section_graph(
    section_graph: dict[str, Any] | None,
    *,
    plan: DeepResearchPlan,
    source_registry: list[dict[str, Any]],
    selected_evidence_ids_by_section: dict[str, list[str]],
    candidate_evidence_ids_by_section: dict[str, list[str]],
    rejected_evidence_ids_by_section: dict[str, list[str]],
    matched_unit_ids_by_section: dict[str, list[str]],
    evidence_source_ids: dict[str, list[str]],
    updated_at: str = "",
) -> dict[str, Any]:
    base = dict(section_graph or {}) if isinstance(section_graph, dict) else {}
    nodes = base.get("nodes") if isinstance(base.get("nodes"), list) else []
    if not nodes:
        return initialize_section_graph(plan, updated_at=updated_at)

    source_ids_by_section: dict[str, list[str]] = {}
    for section_id, evidence_ids in selected_evidence_ids_by_section.items():
        merged: list[str] = []
        for evidence_id in evidence_ids:
            for source_id in evidence_source_ids.get(evidence_id, []):
                if source_id and source_id not in merged:
                    merged.append(source_id)
        source_ids_by_section[section_id] = merged

    refreshed_nodes: list[dict[str, Any]] = []
    for raw_node in nodes:
        node = dict(raw_node)
        section_id = str(node.get("section_id", "")).strip()
        if not section_id:
            continue
        node["candidate_evidence_ids"] = list(candidate_evidence_ids_by_section.get(section_id, []))
        node["selected_evidence_ids"] = list(selected_evidence_ids_by_section.get(section_id, []))
        node["rejected_evidence_ids"] = list(rejected_evidence_ids_by_section.get(section_id, []))
        node["evidence_ids"] = list(selected_evidence_ids_by_section.get(section_id, []))
        node["matched_unit_ids"] = list(matched_unit_ids_by_section.get(section_id, []))
        node["source_ids"] = list(source_ids_by_section.get(section_id, []))
        node["status"] = "grounded" if node["selected_evidence_ids"] else node.get("status") or "planned"
        node["last_updated_at"] = updated_at or node.get("last_updated_at", "")
        refreshed_nodes.append(node)

    return DeepResearchSectionGraphState(
        version=int(base.get("version", 1) or 1),
        root_section_ids=list(base.get("root_section_ids") or [section.section_id for section in plan.report_outline]),
        nodes=[DeepResearchSectionNode.model_validate(node) for node in refreshed_nodes],
    ).model_dump()
