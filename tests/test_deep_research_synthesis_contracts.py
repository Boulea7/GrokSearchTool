from grok_search.deep_research_synthesis import build_synthesis_outline, evidence_pool_for_section
from grok_search.deep_research_types import DeepResearchPlan


def test_build_synthesis_outline_respects_section_graph_root_order_for_specific_sections():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare checkpoint resume and restart semantics",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Compare checkpoint resume and restart semantics",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary axis."},
                {"id": "sq2", "question": "Restart trade-offs", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "resume-semantics", "title": "Resume Semantics", "goal": "Explain checkpoint resume semantics."},
                {"section_id": "restart-trade-offs", "title": "Restart Trade-offs", "goal": "Explain restart trade-offs."},
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            ],
            "research_units": [],
        }
    )

    outline = build_synthesis_outline(
        plan,
        section_graph={
            "root_section_ids": ["executive-summary", "restart-trade-offs", "resume-semantics"],
            "nodes": [],
        },
    )

    assert [section["section_id"] for section in outline] == [
        "executive-summary",
        "restart-trade-offs",
        "resume-semantics",
    ]


def test_build_synthesis_outline_does_not_materialize_open_questions_without_rejected_signal():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare checkpoint resume and restart semantics",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Compare checkpoint resume and restart semantics",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
            ],
            "research_units": [],
        }
    )

    outline = build_synthesis_outline(
        plan,
        evidence_ledger=[
            {
                "ledger_id": "ledger-evidence-resume",
                "evidence_id": "evidence-resume",
                "unit_id": "unit-search-1",
                "question_id": "sq1",
                "origin_query": "checkpoint resume semantics",
                "candidate_section_ids": ["key-findings"],
                "selected_section_id": "key-findings",
                "rejected_section_ids": [],
                "disposition": "selected",
                "disposition_reason": "keyword_overlap",
                "source_ids": ["R1"],
                "source_urls": ["https://docs.example.com/runtime/resume"],
                "summary": "Resume continues from the last durable checkpoint after interruption.",
                "evidence_kind": "fetch",
                "recorded_at": "2026-04-18T00:00:00Z",
            }
        ],
        section_banks=[
            {
                "section_id": "executive-summary",
                "candidate_evidence_ids": [],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            },
            {
                "section_id": "key-findings",
                "candidate_evidence_ids": ["evidence-resume"],
                "selected_evidence_ids": ["evidence-resume"],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            },
            {
                "section_id": "open-questions",
                "candidate_evidence_ids": [],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            },
        ],
    )

    assert [section["section_id"] for section in outline] == [
        "executive-summary",
        "key-findings",
        "checkpoint-resume-semantics",
    ]


def test_evidence_pool_for_section_prefers_selected_then_candidate_then_global():
    evidence_items = [
        {"evidence_id": "selected-e1", "summary": "Selected 1", "detail": "Selected 1"},
        {"evidence_id": "selected-e2", "summary": "Selected 2", "detail": "Selected 2"},
        {"evidence_id": "candidate-e1", "summary": "Candidate", "detail": "Candidate"},
        {"evidence_id": "candidate-e2", "summary": "Candidate 2", "detail": "Candidate 2"},
        {"evidence_id": "global-e1", "summary": "Global", "detail": "Global"},
    ]

    selected_pool, selected_mode = evidence_pool_for_section(
        "resume-semantics",
        evidence_items=evidence_items,
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["candidate-e1", "selected-e1", "selected-e2"],
                "selected_evidence_ids": ["selected-e1", "selected-e2"],
                "rejected_evidence_ids": [],
                "selected_packets": [{"evidence_id": "selected-e2"}],
            }
        ],
    )
    candidate_pool, candidate_mode = evidence_pool_for_section(
        "restart-trade-offs",
        evidence_items=evidence_items,
        section_banks=[
            {
                "section_id": "restart-trade-offs",
                "candidate_evidence_ids": ["candidate-e1", "candidate-e2"],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": [],
                "candidate_packets": [{"evidence_id": "candidate-e2"}],
            }
        ],
    )
    global_pool, global_mode = evidence_pool_for_section(
        "open-questions",
        evidence_items=evidence_items,
        section_banks=[
            {
                "section_id": "open-questions",
                "candidate_evidence_ids": ["candidate-e1"],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": ["candidate-e1"],
            }
        ],
    )

    assert selected_mode == "selected"
    assert [item["evidence_id"] for item in selected_pool] == ["selected-e2"]
    assert candidate_mode == "candidate"
    assert [item["evidence_id"] for item in candidate_pool] == ["candidate-e2"]
    assert global_mode == "global"
    assert [item["evidence_id"] for item in global_pool] == [
        "selected-e1",
        "selected-e2",
        "candidate-e1",
        "candidate-e2",
        "global-e1",
    ]
