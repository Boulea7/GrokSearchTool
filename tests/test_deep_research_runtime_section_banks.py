import json

import pytest

from grok_search.deep_research_evidence import (
    build_evidence_ledger_entries,
    reconcile_section_banks_with_materialized_sections,
    seed_section_banks_from_question_bindings,
    update_section_banks,
)
from grok_search.deep_research_types import DeepResearchPlan
from test_deep_research_runtime import build_runtime, structured_plan_payload


def test_update_section_banks_backfills_selected_packets_for_materialized_sections():
    section_banks = [
        {
            "section_id": "section-a",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "candidate_packets": [],
            "selected_packets": [],
            "rejected_packets": [],
            "last_updated_at": "",
        },
        {
            "section_id": "section-b",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "candidate_packets": [],
            "selected_packets": [],
            "rejected_packets": [],
            "last_updated_at": "",
        },
    ]
    ledger_entries = [
        {
            "ledger_id": "ledger-e1",
            "evidence_id": "e1",
            "candidate_section_ids": ["section-a", "section-b"],
            "selected_section_id": "section-a",
            "rejected_section_ids": ["section-b"],
            "question_ids": ["sq1"],
            "source_ids": ["R1"],
            "unit_id": "unit-search-1",
            "line_start": 10,
            "line_end": 11,
            "materialized_claim_ids": ["section-a-claim-1", "section-b-claim-2"],
        }
    ]

    updated = update_section_banks(section_banks, ledger_entries=ledger_entries, updated_at="2026-04-19T00:00:00Z")
    bank_by_id = {bank["section_id"]: bank for bank in updated}

    assert bank_by_id["section-a"]["selected_packets"][0]["claim_ids"] == ["section-a-claim-1"]
    assert bank_by_id["section-b"]["selected_packets"][0]["claim_ids"] == ["section-b-claim-2"]


def test_update_section_banks_honors_selected_section_ids_list():
    section_banks = [
        {
            "section_id": "task-visibility",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "candidate_packets": [],
            "selected_packets": [],
            "rejected_packets": [],
            "last_updated_at": "",
        },
        {
            "section_id": "task-start",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "candidate_packets": [],
            "selected_packets": [],
            "rejected_packets": [],
            "last_updated_at": "",
        },
    ]
    ledger_entries = [
        {
            "ledger_id": "ledger-e1",
            "evidence_id": "e1",
            "candidate_section_ids": ["task-visibility", "task-start"],
            "selected_section_ids": ["task-visibility", "task-start"],
            "rejected_section_ids": [],
            "question_ids": ["sq1", "sq2"],
            "source_ids": ["R1"],
            "unit_id": "unit-search-1",
            "line_start": 10,
            "line_end": 11,
            "materialized_claim_ids": [],
        }
    ]

    updated = update_section_banks(section_banks, ledger_entries=ledger_entries, updated_at="2026-04-27T00:00:00Z")
    bank_by_id = {bank["section_id"]: bank for bank in updated}

    assert bank_by_id["task-visibility"]["selected_evidence_ids"] == ["e1"]
    assert bank_by_id["task-start"]["selected_evidence_ids"] == ["e1"]
    assert bank_by_id["task-visibility"]["selected_packets"][0]["question_ids"] == ["sq1", "sq2"]


def test_seed_section_banks_from_question_bindings_prefers_candidate_until_section_is_explicitly_selected():
    section_banks = [
        {
            "section_id": "task-visibility",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "candidate_packets": [],
            "selected_packets": [],
            "rejected_packets": [],
            "last_updated_at": "",
        }
    ]
    planned_outline = [
        {
            "section_id": "task-visibility",
            "title": "Task Visibility",
            "goal": "Explain DescribeReplicationTasks recovery visibility.",
            "question_ids": ["sq3"],
        }
    ]
    ledger_entries = [
        {
            "ledger_id": "ledger-e1",
            "evidence_id": "e1",
            "candidate_section_ids": ["resume-semantics"],
            "selected_section_id": "resume-semantics",
            "rejected_section_ids": [],
            "question_ids": ["sq3"],
            "source_ids": ["R1"],
            "unit_id": "unit-search-1",
            "line_start": 10,
            "line_end": 11,
            "materialized_claim_ids": [],
        }
    ]

    updated = seed_section_banks_from_question_bindings(
        section_banks,
        planned_outline=planned_outline,
        ledger_entries=ledger_entries,
        updated_at="2026-04-20T00:00:00Z",
    )

    assert updated[0]["candidate_evidence_ids"] == ["e1"]
    assert updated[0]["selected_evidence_ids"] == []
    assert updated[0]["candidate_packets"]
    assert updated[0]["selected_packets"] == []


def test_build_evidence_ledger_entries_uses_source_url_tokens_for_question_binding():
    payload = structured_plan_payload(
        type(
            "Job",
            (),
            {
                "query": "AWS DMS checkpoint semantics",
                "context": "",
                "effort": "deep",
                "resolved_budget_seconds": 240,
            },
        )(),
        {"mode": "fresh"},
    )
    payload["sub_questions"] = [
        {
            "id": "sq3",
            "question": "Explain DescribeReplicationTasks RecoveryCheckpoint visibility.",
            "reason": "Primary axis.",
        }
    ]
    payload["report_outline"] = [
        {
            "section_id": "task-visibility",
            "title": "Task Visibility",
            "goal": "Explain DescribeReplicationTasks RecoveryCheckpoint visibility.",
        }
    ]
    plan = DeepResearchPlan.model_validate(payload)
    entries = build_evidence_ledger_entries(
        plan,
        unit_id="unit-search-1",
        origin_query="DescribeReplicationTasks RecoveryCheckpoint",
        evidence_items=[
            {
                "evidence_id": "e1",
                "summary": "The API returns recovery checkpoint details for replication tasks.",
                "detail": "Checkpoint metadata is exposed through DescribeReplicationTasks.",
                "source_urls": [
                    "https://docs.aws.amazon.com/dms/latest/APIReference/API_DescribeReplicationTasks.html"
                ],
                "source_ids": ["R1"],
                "evidence_kind": "search",
            }
        ],
        updated_at="2026-04-20T00:00:00Z",
    )

    assert entries[0]["selected_section_id"] == "task-visibility"
    assert entries[0]["question_ids"] == ["sq3"]


def test_build_evidence_ledger_entries_does_not_bind_section_from_origin_query_alone():
    payload = structured_plan_payload(
        type(
            "Job",
            (),
            {
                "query": "checkpoint resume semantics",
                "context": "",
                "effort": "deep",
                "resolved_budget_seconds": 240,
            },
        )(),
        {"mode": "fresh"},
    )
    payload["sub_questions"] = [
        {
            "id": "sq2",
            "question": "Explain restart trade-offs.",
            "reason": "Secondary axis.",
        }
    ]
    payload["report_outline"] = [
        {
            "section_id": "restart-tradeoffs",
            "title": "Restart Trade-offs",
            "goal": "Explain restart trade-offs.",
            "question_ids": ["sq2"],
        }
    ]
    plan = DeepResearchPlan.model_validate(payload)

    entries = build_evidence_ledger_entries(
        plan,
        unit_id="unit-search-1",
        origin_query="restart trade-offs",
        evidence_items=[
            {
                "evidence_id": "e1",
                "summary": "General operational guidance for on-call handling.",
                "detail": "This page covers general operational guidance and escalation paths.",
                "source_urls": [
                    "https://docs.example.com/runtime/operations"
                ],
                "source_ids": ["R1"],
                "evidence_kind": "search",
            }
        ],
        updated_at="2026-04-21T00:00:00Z",
    )

    assert entries[0]["selected_section_id"] == ""
    assert entries[0]["question_ids"] == []


def test_build_evidence_ledger_entries_uses_stable_tiebreak_for_multi_hit_official_doc_candidates():
    payload = structured_plan_payload(
        type(
            "Job",
            (),
            {
                "query": "AWS DMS checkpoint semantics",
                "context": "",
                "effort": "deep",
                "resolved_budget_seconds": 240,
            },
        )(),
        {"mode": "fresh"},
    )
    payload["sub_questions"] = [
        {
            "id": "sq1",
            "question": "Explain RecoveryCheckpoint and CdcStartPosition usage.",
            "reason": "Primary axis.",
        },
        {
            "id": "sq2",
            "question": "Explain DescribeReplicationTasks recovery visibility.",
            "reason": "Primary axis.",
        },
    ]
    payload["report_outline"] = [
        {
            "section_id": "checkpoint-positioning",
            "title": "Checkpoint Positioning",
            "goal": "Explain RecoveryCheckpoint and CdcStartPosition usage.",
        },
        {
            "section_id": "task-visibility",
            "title": "Task Visibility",
            "goal": "Explain DescribeReplicationTasks recovery visibility.",
        },
    ]
    plan = DeepResearchPlan.model_validate(payload)

    entries = build_evidence_ledger_entries(
        plan,
        unit_id="unit-search-1",
        origin_query="RecoveryCheckpoint DescribeReplicationTasks official docs",
        evidence_items=[
            {
                "evidence_id": "e1",
                "summary": "DescribeReplicationTasks exposes RecoveryCheckpoint metadata for replication tasks.",
                "detail": "RecoveryCheckpoint can be reused with CdcStartPosition and is exposed through DescribeReplicationTasks.",
                "source_urls": [
                    "https://docs.aws.amazon.com/dms/latest/APIReference/API_DescribeReplicationTasks.html"
                ],
                "source_ids": ["R1"],
                "evidence_kind": "search",
            }
        ],
        updated_at="2026-04-21T00:00:00Z",
    )

    assert set(entries[0]["candidate_section_ids"]) == {"task-visibility", "checkpoint-positioning"}
    assert entries[0]["selected_section_id"] == "task-visibility"
    assert entries[0]["question_ids"] == ["sq2"]
    assert entries[0]["disposition"] == "selected"


def test_reconcile_section_banks_with_materialized_sections_keeps_only_claim_backed_selected_packets():
    reconciled = reconcile_section_banks_with_materialized_sections(
        [
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1", "e2"],
                "selected_evidence_ids": ["e1", "e2"],
                "rejected_evidence_ids": [],
                "candidate_packets": [
                    {
                        "evidence_id": "e1",
                        "question_ids": ["sq1"],
                        "claim_ids": [],
                    },
                    {
                        "evidence_id": "e2",
                        "question_ids": ["sq1"],
                        "claim_ids": [],
                    },
                ],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "question_ids": ["sq1"],
                        "claim_ids": ["stale-claim-id"],
                    },
                    {
                        "evidence_id": "e2",
                        "question_ids": ["sq1"],
                        "claim_ids": [],
                    },
                ],
                "rejected_packets": [],
                "last_updated_at": "",
            }
        ],
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "question_ids": ["sq1"],
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        ],
        updated_at="2026-04-21T00:00:00Z",
    )

    assert reconciled[0]["selected_evidence_ids"] == ["e1"]
    assert reconciled[0]["selected_packets"] == [
        {
            "evidence_id": "e1",
            "question_ids": ["sq1"],
            "claim_ids": ["resume-semantics-claim-1"],
        }
    ]


@pytest.mark.asyncio
async def test_runtime_backfills_derived_section_banks_from_active_outline(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary axis."},
            {"id": "sq2", "question": "Restart trade-offs", "reason": "Primary axis."},
        ]
        payload["report_outline"] = [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
            {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume docs",
                "goal": "Checkpoint resume semantics",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Restart docs",
                "goal": "Restart trade-offs",
                "query": "restart trade-offs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = [
            "checkpoint resume semantics",
            "restart trade-offs",
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        if "restart" in query:
            return (
                "Restart replays work from a fresh starting point and may re-run completed work.",
                [
                    {
                        "url": "https://docs.example.com/runtime/restart",
                        "title": "Restart docs",
                        "description": "Official restart docs.",
                    }
                ],
            )
        return (
            "Resume continues from the last durable checkpoint after interruption.",
            [
                {
                    "url": "https://docs.example.com/runtime/resume",
                    "title": "Resume docs",
                    "description": "Official resume docs.",
                }
            ],
        )

    async def fetch(url):
        if "restart" in url:
            return "# Restart docs\n\nRestart replays work from a fresh starting point and may re-run completed work."
        return "# Resume docs\n\nResume continues from the last durable checkpoint after interruption."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Checkpoint resume versus restart", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    outline_state = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_state.json") or "{}")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    bank_by_id = {bank["section_id"]: bank for bank in section_banks}
    expected_section_ids = [
        "executive-summary",
        "key-findings",
        "checkpoint-resume-semantics",
        "restart-trade-offs",
    ]
    if isinstance(result.get("report"), dict):
        section_ids = [section["section_id"] for section in result["report"]["sections"]]
        coverage = result["report"]["coverage"]
        coverage_by_id = {item["section_id"]: item for item in coverage["section_coverage"]}
        assert section_ids == expected_section_ids
        assert coverage["planned_section_ids"] == section_ids
        assert coverage_by_id["checkpoint-resume-semantics"]["selected_evidence_count"] >= 1
        assert coverage_by_id["restart-trade-offs"]["selected_evidence_count"] >= 1
    else:
        assert result["artifact_errors"]
        section_ids = expected_section_ids
    assert outline_state["root_section_ids"] == section_ids
    assert "checkpoint-resume-semantics" in bank_by_id
    assert "restart-trade-offs" in bank_by_id
    assert bank_by_id["checkpoint-resume-semantics"]["selected_evidence_ids"]
    assert bank_by_id["restart-trade-offs"]["selected_evidence_ids"]
    assert bank_by_id["checkpoint-resume-semantics"]["selected_packets"]
    assert bank_by_id["restart-trade-offs"]["selected_packets"]
    assert all(
        claim_id.startswith("checkpoint-resume-semantics-")
        for packet in bank_by_id["checkpoint-resume-semantics"]["selected_packets"]
        for claim_id in packet.get("claim_ids", [])
    )
    assert all(
        claim_id.startswith("restart-trade-offs-")
        for packet in bank_by_id["restart-trade-offs"]["selected_packets"]
        for claim_id in packet.get("claim_ids", [])
    )


@pytest.mark.asyncio
async def test_runtime_allows_medium_single_source_search_only_for_clean_official_docs_derived_section(
    monkeypatch, tmp_path
):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary axis."}
        ]
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "key-findings",
                "title": "Key Findings",
                "goal": "Cover the strongest findings.",
            },
            {
                "section_id": "open-questions",
                "title": "Open Questions",
                "goal": "Call out remaining gaps.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume docs",
                "goal": "Checkpoint resume semantics",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = ["checkpoint resume semantics"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume semantics: resume-processing continues from the last durable checkpoint when recovery metadata remains available.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                    "provider": "grok",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(
        query="Checkpoint resume semantics",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    verifier = json.loads(runtime.store.read_artifact_text(response["job_id"], "verifier.json") or "{}")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    bank_by_id = {bank["section_id"]: bank for bank in section_banks}

    assert result["status"] == "completed"
    assert result["report"]["status"] == "completed"
    assert "checkpoint-resume-semantics" in bank_by_id
    assert bank_by_id["checkpoint-resume-semantics"]["selected_evidence_ids"]
    assert bank_by_id["checkpoint-resume-semantics"]["selected_packets"]
    assert "medium_single_source_search_only" not in verifier["reason_codes"]
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["verifier"]["reason_codes"]
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["release_gate"]["soft_reason_codes"]
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["warnings"]
