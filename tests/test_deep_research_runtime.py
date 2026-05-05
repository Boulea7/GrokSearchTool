import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from grok_search import server
from grok_search.deep_research_evidence import (
    candidate_evidence_ids_by_section,
    rejected_evidence_ids_by_section,
    selected_evidence_ids_by_section,
    update_section_banks,
)
from grok_search.deep_research_runtime import (
    DeepResearchRuntime,
    _build_section_citations,
    _build_grounding_diagnostics,
    _build_release_gate,
    _build_report_summary,
    _build_verifier_diagnostics,
    _coverage_for_report,
    _execute_research_unit,
    _extract_relevant_excerpt_with_span,
    _search_query,
)
from grok_search.deep_research_synthesis import build_synthesis_outline
from grok_search.deep_research_section_graph import initialize_section_graph, update_section_graph
import grok_search.deep_research_runtime as deep_research_runtime_module
from grok_search.providers.grok import GrokSearchProvider
from grok_search.deep_research_types import (
    DeepResearchContinuationState,
    DeepResearchEvidenceItem,
    DeepResearchPlan,
    DeepResearchResearchUnit,
    utc_now_iso,
)
from deep_research_test_helpers import (
    RECENT_DEEP_RESEARCH_LIVE_PROBE_FIXTURES,
    assert_fixture_public_surface as assert_recent_probe_fixture_public_surface,
    seed_live_probe_fixture_job,
)


_SEEDED_BATCH_ID_PLACEHOLDER = "$seeded_batch_id"


def build_runtime(tmp_path):
    return DeepResearchRuntime(tmp_path / "deep-research")


def load_deep_research_fixture(name: str) -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "deep_research" / name
    return json.loads(fixture_path.read_text())


def _materialize_fixture_surface(value, *, seeded_batch_id: str):
    if isinstance(value, dict):
        return {
            key: _materialize_fixture_surface(item, seeded_batch_id=seeded_batch_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_materialize_fixture_surface(item, seeded_batch_id=seeded_batch_id) for item in value]
    if value == _SEEDED_BATCH_ID_PLACEHOLDER:
        return seeded_batch_id
    return value


def assert_public_surface_subset(actual: dict, expected: dict, *, seeded_batch_id: str) -> None:
    for key, expected_value in expected.items():
        assert key in actual
        materialized = _materialize_fixture_surface(expected_value, seeded_batch_id=seeded_batch_id)
        actual_value = actual[key]
        if isinstance(materialized, dict):
            assert isinstance(actual_value, dict)
            assert_public_surface_subset(actual_value, materialized, seeded_batch_id=seeded_batch_id)
            continue
        assert actual_value == materialized


def assert_fixture_public_surface(snapshot: dict, *, status: dict, result: dict, seeded_batch_id: str) -> None:
    public_surface = snapshot.get("public_surface") or {}
    status_surface = public_surface.get("status") or {}
    result_surface = public_surface.get("result") or {}
    if status_surface:
        assert_public_surface_subset(status, status_surface, seeded_batch_id=seeded_batch_id)
    if result_surface:
        assert_public_surface_subset(result, result_surface, seeded_batch_id=seeded_batch_id)


def structured_plan_payload(job, continuation):
    return {
        "query": job.query,
        "context": job.context,
        "effort": job.effort,
        "time_budget_seconds": job.resolved_budget_seconds,
        "include_domains": [],
        "exclude_domains": [],
        "brief": {
            "objective": job.query,
            "deliverable": "A cited report.",
            "success_criteria": ["Produce a structured report."],
        },
        "sub_questions": [
            {"id": "sq1", "question": job.query, "reason": "Cover the primary question."},
        ],
        "search_strategy": {
            "approach": "targeted",
            "search_queries": [job.query],
            "selective_fetch": {
                "max_urls_per_search": 1,
                "prefer_titles_matching_outline": True,
            },
        },
        "report_outline": [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
        ],
        "research_units": [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Primary search",
                "goal": job.query,
                "query": job.query,
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ],
        "continuation": continuation,
        "planner_metadata": {"planner": "test", "used_fallback": False},
    }


class _CapturedPlannerProvider:
    def __init__(self, captured):
        self.model = "grok-4.20-expert"
        self._captured = captured
        self._last_success_provider_name = "test-provider"
        self._last_success_provider_model = self.model
        self._last_success_provider_api_url = "https://api.example.com/v1"

    def _build_api_headers(self):
        return {"Authorization": "Bearer test"}

    async def _execute_completion_with_retry_result(self, headers, payload, render_sources=False):
        self._captured["headers"] = dict(headers)
        self._captured["payload"] = json.loads(json.dumps(payload))
        return json.dumps(
            {
                "brief": {
                    "objective": "Follow up checkpoint resume semantics",
                    "deliverable": "A cited report.",
                    "success_criteria": ["Produce a structured report."],
                },
                "sub_questions": [
                    {
                        "id": "sq1",
                        "question": "Follow up checkpoint resume semantics",
                        "reason": "Cover the primary question.",
                    }
                ],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Follow up checkpoint resume semantics"],
                    "selective_fetch": {
                        "max_urls_per_search": 1,
                        "prefer_titles_matching_outline": True,
                    },
                },
                "report_outline": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "goal": "Summarize the answer.",
                    }
                ],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Follow up checkpoint resume semantics",
                        "query": "Follow up checkpoint resume semantics",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "model", "used_fallback": False},
            }
        ), []


def with_minimal_provenance_artifacts(
    artifacts: list[dict],
    *,
    query: str,
    evidence_items: list[dict] | None = None,
    total_claims: int = 0,
    section_count: int = 0,
):
    payload = list(artifacts)
    normalized_evidence_items = [] if evidence_items is None else evidence_items
    coverage_payload = {
        "query": query,
        "planned_section_ids": [],
        "answered_section_ids": [],
        "unanswered_sections": [],
        "planned_sub_question_ids": [],
        "covered_sub_question_ids": [],
        "uncovered_sub_questions": [],
        "coverage_gate_passed": True,
        "hard_coverage_gate_passed": True,
    }
    grounding_payload = {
        "total_claims": total_claims,
        "grounded_claims": total_claims,
        "ungrounded_claims": 0,
        "single_source_claims": total_claims,
        "low_confidence_claims": 0,
        "missing_evidence_binding_claims": 0,
        "total_evidence_bindings": 0,
        "source_backed_binding_count": 0,
        "search_only_binding_count": 0,
        "null_span_binding_count": 0,
        "grounded_claims_without_source_backed_binding": 0,
        "sections": [],
        "sources": [],
    }
    verifier_payload = {
        "passed": True,
        "reason_codes": [],
        "flagged_claim_ids": [],
        "summary": {
            "section_count": section_count,
            "total_claims": total_claims,
            "low_confidence_claims": 0,
            "single_source_claims": total_claims,
            "source_backed_binding_count": 0,
            "search_only_binding_count": 0,
            "null_span_binding_count": 0,
            "missing_evidence_items": 0,
            "mismatched_binding_source": 0,
            "mismatched_binding_evidence": 0,
            "invalid_source_backed_span": 0,
            "duplicate_claims": 0,
            "low_value_claims": 0,
            "medium_single_source_search_only": 0,
            "same_domain_off_topic_dominance": 0,
            "unbound_citation_sources": 0,
            "unbound_evidence_ids": 0,
        },
    }
    verification_payload = {
        "supported_claims": [],
        "single_source_claims": [],
        "conflicted_claims": [],
        "unmapped_evidence_ids": [],
        "confidence_by_section": {},
        "unresolved_sections": [],
        "packet_to_prose_fidelity": {
            "passed": True,
            "checked_packet_count": 0,
            "missing_selected_packet_ids": [],
            "missing_selected_row_ids": [],
            "reason_codes": [],
        },
    }
    coverage_gaps_payload = {
        "query": query,
        "unanswered_sections": [],
        "uncovered_sub_questions": [],
        "hard_uncovered_targets": [],
        "coverage_gate_passed": True,
        "hard_coverage_gate_passed": True,
        "blocking_gap_count": 0,
        "hard_gap_count": 0,
        "total_gap_count": 0,
        "gaps": [],
    }
    for item in payload:
        if item.get("kind") != "report.json":
            continue
        try:
            report_payload = json.loads(item.get("content") or "")
        except Exception:
            continue
        if not isinstance(report_payload, dict):
            continue
        report_payload.setdefault("sections", [])
        report_payload.setdefault("unit_results", {})
        coverage_report = report_payload.setdefault("coverage", {})
        if isinstance(coverage_report, dict):
            for key, value in coverage_payload.items():
                coverage_report.setdefault(key, value)
        else:
            report_payload["coverage"] = dict(coverage_payload)
        runtime_payload = report_payload.setdefault("runtime", {})
        runtime_payload.setdefault("warnings", [])
        grounding_report = runtime_payload.setdefault(
            "grounding",
            {
                key: grounding_payload[key]
                for key in (
                    "total_claims",
                    "ungrounded_claims",
                    "single_source_claims",
                    "missing_evidence_binding_claims",
                )
            },
        )
        if isinstance(grounding_report, dict):
            for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims"):
                grounding_report.setdefault(key, grounding_payload[key])
        else:
            runtime_payload["grounding"] = {
                key: grounding_payload[key]
                for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims")
            }
        verifier_report = runtime_payload.setdefault("verifier", {})
        if isinstance(verifier_report, dict):
            verifier_report.setdefault("passed", verifier_payload["passed"])
            verifier_report.setdefault("reason_codes", list(verifier_payload["reason_codes"]))
            verifier_report.setdefault("flagged_claim_ids", list(verifier_payload["flagged_claim_ids"]))
            verifier_summary = verifier_report.setdefault("summary", {})
            if isinstance(verifier_summary, dict):
                for key, value in verifier_payload["summary"].items():
                    verifier_summary.setdefault(key, value)
            else:
                verifier_report["summary"] = dict(verifier_payload["summary"])
        else:
            runtime_payload["verifier"] = json.loads(json.dumps(verifier_payload))
        runtime_verification = runtime_payload.setdefault("verification", {})
        if isinstance(runtime_verification, dict):
            for key, value in verification_payload.items():
                runtime_verification.setdefault(key, value)
        else:
            runtime_payload["verification"] = json.loads(json.dumps(verification_payload))
        item["content"] = json.dumps(report_payload)
    payload.extend(
        [
            {
                "kind": "coverage.json",
                "content": json.dumps(coverage_payload),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": json.dumps(grounding_payload),
                "content_type": "application/json",
            },
            {
                "kind": "verifier.json",
                "content": json.dumps(verifier_payload),
                "content_type": "application/json",
            },
            {
                "kind": "selected_bank.json",
                "content": json.dumps([]),
                "content_type": "application/json",
            },
            {
                "kind": "evidence_bank.json",
                "content": json.dumps([]),
                "content_type": "application/json",
            },
            {
                "kind": "verification.json",
                "content": json.dumps(verification_payload),
                "content_type": "application/json",
            },
            {
                "kind": "coverage_gaps.json",
                "content": json.dumps(coverage_gaps_payload),
                "content_type": "application/json",
            },
        ]
    )
    if not any(item.get("kind") == "evidence_items.json" for item in payload):
        payload.append(
            {
                "kind": "evidence_items.json",
                "content": json.dumps(normalized_evidence_items),
                "content_type": "application/json",
            }
        )
    return payload


@pytest.mark.asyncio
async def test_plan_brief_includes_focused_scope_fields(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Compare checkpoint resume and restart semantics in AWS DMS",
        context="Use official docs only and focus on recovery trade-offs.",
        include_domains=["docs.aws.amazon.com"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    brief = response["plan"]["brief"]

    assert brief["must_cover"] == ["Compare checkpoint resume and restart semantics in AWS DMS"]
    assert brief["out_of_scope"] == []
    assert brief["preferred_sources"] == ["docs.aws.amazon.com"]
    assert brief["stop_policy"]["stop_on_sufficient_coverage"] is True
    assert brief["stop_policy"]["max_search_queries"] >= 1
    assert brief["continuation_focus"] == []


def create_completed_source_job(
    runtime: DeepResearchRuntime,
    *,
    query: str = "Checkpoint resume semantics",
    sources_content: str | None = None,
    citations_payload: dict | None = None,
    report_payload: dict | None = None,
    checkpoint_sections: list[dict] | None = None,
    checkpoint_sources: list[dict] | None = None,
    checkpoint_unit_results: dict | None = None,
    include_provenance_bundle: bool = True,
):
    job = runtime.store.create_job(
        query=query,
        request_fingerprint=f"fp-{query}",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())

    plan_payload = structured_plan_payload(job, {"mode": "fresh"})
    runtime.write_artifact(job.job_id, "plan.json", json.dumps(plan_payload), "application/json")

    source_registry = checkpoint_sources or [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
            "title": "Runtime checkpoints",
            "domain": "docs.example.com",
            "source_type": "official_docs",
            "ranking_reasons": ["official_docs"],
        }
    ]
    sections = checkpoint_sections or [
        {
            "section_id": "executive-summary",
            "title": "Executive Summary",
            "summary": "Resume continues from the last checkpoint.",
            "claims": [
                {
                    "claim_id": "executive-summary-claim-1",
                    "text": "Resume continues from the last checkpoint.",
                    "citations": ["R1"],
                    "unit_id": "unit-search-1",
                    "evidence_ids": ["evidence-unit-search-1-search"],
                    "confidence": "medium",
                }
            ],
            "citations": ["R1"],
            "confidence": "medium",
        }
    ]
    unit_results = checkpoint_unit_results or {
        "unit-search-1": {
            "summary": "Resume continues from the last checkpoint.",
            "detail": "Resume continues from the last checkpoint.",
            "source_ids": ["R1"],
            "citations": ["R1"],
        }
    }
    citations = citations_payload or {
        "source_registry": {item["source_id"]: item for item in source_registry},
        "sections": sections,
    }
    report = report_payload or {
        "query": query,
        "summary": "Resume continues from the last checkpoint.",
        "sections": sections,
        "unit_results": unit_results,
    }
    evidence_items = [
        {
            "evidence_id": "evidence-unit-search-1-search",
            "unit_id": "unit-search-1",
            "summary": "Resume continues from the last checkpoint.",
            "detail": "Resume continues from the last checkpoint.",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
        }
    ]
    artifacts = [
        {
            "kind": "sources.json",
            "content": sources_content if sources_content is not None else json.dumps(source_registry),
            "content_type": "application/json",
        },
        {
            "kind": "citations.json",
            "content": json.dumps(citations),
            "content_type": "application/json",
        },
        {
            "kind": "report.json",
            "content": json.dumps(report),
            "content_type": "application/json",
        },
        {
            "kind": "final_report.md",
            "content": "# Final Report\n\nResume continues from the last checkpoint.",
            "content_type": "text/markdown",
        },
    ]
    if include_provenance_bundle:
        artifacts.extend(
            [
                {
                    "kind": "evidence_items.json",
                    "content": json.dumps(evidence_items),
                    "content_type": "application/json",
                },
                {
                    "kind": "coverage.json",
                    "content": json.dumps(
                        {
                            "query": query,
                            "planned_section_ids": [section["section_id"] for section in sections],
                            "answered_section_ids": [section["section_id"] for section in sections],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": ["sq1"],
                            "covered_sub_question_ids": ["sq1"],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "grounding.json",
                    "content": json.dumps(
                        {
                            "total_claims": sum(len(section.get("claims", [])) for section in sections),
                            "grounded_claims": sum(len(section.get("claims", [])) for section in sections),
                            "ungrounded_claims": 0,
                            "single_source_claims": sum(len(section.get("claims", [])) for section in sections),
                            "low_confidence_claims": 0,
                            "missing_evidence_binding_claims": 0,
                            "total_evidence_bindings": 0,
                            "source_backed_binding_count": 0,
                            "search_only_binding_count": 0,
                            "null_span_binding_count": 0,
                            "grounded_claims_without_source_backed_binding": 0,
                            "sections": [],
                            "sources": [],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "verifier.json",
                    "content": json.dumps(
                        {
                            "passed": True,
                            "reason_codes": [],
                            "flagged_claim_ids": [],
                            "summary": {
                                "section_count": len(sections),
                                "total_claims": sum(len(section.get("claims", [])) for section in sections),
                                "low_confidence_claims": 0,
                                "single_source_claims": sum(len(section.get("claims", [])) for section in sections),
                                "source_backed_binding_count": 0,
                                "search_only_binding_count": 0,
                                "null_span_binding_count": 0,
                                "missing_evidence_items": 0,
                                "mismatched_binding_source": 0,
                                "mismatched_binding_evidence": 0,
                                "invalid_source_backed_span": 0,
                                "duplicate_claims": 0,
                                "low_value_claims": 0,
                                "medium_single_source_search_only": 0,
                                "same_domain_off_topic_dominance": 0,
                            },
                        }
                    ),
                    "content_type": "application/json",
                },
            ]
        )
    runtime.write_artifact_batch(job.job_id, artifacts)
    runtime.store.save_checkpoint(
        job.job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state={
            "plan": plan_payload,
            "completed_unit_ids": list(unit_results),
            "unit_results": unit_results,
            "sources": source_registry,
            "evidence_items": evidence_items,
            "sections": sections,
        },
    )
    return runtime.store.get_job(job.job_id)


def seed_round24_worker_restart_live_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round24_worker_restart_live.json")
    final_status = snapshot["final_status"]
    coverage_payload = {
        "query": snapshot["query"],
        "planned_section_ids": [],
        "answered_section_ids": [],
        "unanswered_sections": ["Open Questions"],
        "planned_sub_question_ids": [],
        "covered_sub_question_ids": [],
        "uncovered_sub_questions": ["Coverage incomplete"],
        "hard_coverage_targets": [],
        "hard_uncovered_targets": [],
        "coverage_gate_passed": False,
        "hard_coverage_gate_passed": True,
    }
    grounding_payload = {
        "total_claims": 0,
        "grounded_claims": 0,
        "ungrounded_claims": 0,
        "single_source_claims": 0,
        "low_confidence_claims": 0,
        "missing_evidence_binding_claims": 0,
        "total_evidence_bindings": 0,
        "source_backed_binding_count": 0,
        "search_only_binding_count": 0,
        "null_span_binding_count": 0,
        "grounded_claims_without_source_backed_binding": 0,
        "sections": [],
        "sources": [],
    }
    verifier_payload = {
        "passed": False,
        "reason_codes": ["medium_single_source_search_only"],
        "flagged_claim_ids": [],
        "summary": {
            "section_count": 0,
            "total_claims": 0,
            "low_confidence_claims": 0,
            "single_source_claims": 0,
            "source_backed_binding_count": 0,
            "search_only_binding_count": 0,
            "null_span_binding_count": 0,
            "missing_evidence_items": 0,
            "mismatched_binding_source": 0,
            "mismatched_binding_evidence": 0,
            "invalid_source_backed_span": 0,
            "duplicate_claims": 0,
            "low_value_claims": 0,
            "medium_single_source_search_only": 1,
            "same_domain_off_topic_dominance": 0,
            "unbound_citation_sources": 0,
            "unbound_evidence_ids": 0,
        },
    }
    source = {
        "source_id": "R1",
        "url": "https://docs.temporal.io/workflow-execution/continue-as-new",
        "title": "Continue-As-New",
        "domain": "docs.temporal.io",
        "source_type": "official_docs",
    }
    report_payload = {
        "query": snapshot["query"],
        "summary": "Round24 worker restart lifecycle summary.",
        "status": final_status["report_status"],
        "coverage": coverage_payload,
        "sections": [],
        "unit_results": {},
        "runtime": {
            "warnings": list(final_status["runtime_warnings"]),
            "constraint_violations": list(final_status["constraint_violations"]),
            "release_gate": snapshot["resume_events_after_seq_16"][-1]["data"]["release_gate"],
            "verifier": verifier_payload,
            "grounding": grounding_payload,
        },
    }
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-round24-worker-restart-live",
        status=final_status["status"],
        phase=final_status["phase"],
        effort="deep",
        context="",
        include_domains=["docs.langchain.com", "docs.temporal.io", "docs.restate.dev"],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=1,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=final_status["attempt_count"],
        current_checkpoint=final_status["current_checkpoint"],
        finished_at=utc_now_iso(),
        last_error=final_status["last_error"],
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps({"query": snapshot["query"], "planner_metadata": {"planner": "model", "used_fallback": False}}),
        "application/json",
    )
    persisted = runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([source]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": source}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(report_payload),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRound24 worker restart lifecycle summary.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": "[]",
                "content_type": "application/json",
            },
            {
                "kind": "coverage.json",
                "content": json.dumps(coverage_payload),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": json.dumps(grounding_payload),
                "content_type": "application/json",
            },
            {
                "kind": "verifier.json",
                "content": json.dumps(verifier_payload),
                "content_type": "application/json",
            },
        ],
    )
    for event in snapshot["initial_events"] + snapshot["resume_events_after_seq_16"]:
        runtime.store.append_event(
            job.job_id,
            type=event["type"],
            phase=event["phase"],
            message=event.get("message") or event["type"],
            data=event.get("data") or {},
        )
    return {
        "job": runtime.store.get_job(job.job_id),
        "snapshot": snapshot,
        "batch_id": persisted[0]["metadata"]["batch_id"],
        "report_payload": report_payload,
    }


def seed_round26_worker_restart_dispatch_runtime_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round26_worker_restart_dispatch_runtime.json")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-round26-worker-restart-dispatch-runtime",
        status=snapshot["resume_run"]["status"],
        phase=snapshot["resume_run"]["phase"],
        effort="deep",
        context="",
        include_domains=["docs.langchain.com", "docs.temporal.io", "docs.restate.dev"],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=1,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=snapshot["resume_run"]["attempt_count"],
        current_checkpoint=snapshot["resume_run"]["current_checkpoint"],
        finished_at=utc_now_iso(),
        last_error=snapshot["resume_run"]["last_error"],
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(
            {
                "query": snapshot["query"],
                "include_domains": ["docs.langchain.com", "docs.temporal.io", "docs.restate.dev"],
                "exclude_domains": [],
                "continuation": {"mode": "fresh"},
                "brief": {"objective": snapshot["query"]},
                "planner_metadata": {"planner": "probe", "used_fallback": False},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "planner_trace.json",
        json.dumps({"provider_name": "probe", "fallback_used": False}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "outline_state.json",
        json.dumps({"version": 1, "root_section_ids": [], "nodes": []}),
        "application/json",
    )
    runtime.write_artifact(job.job_id, "evidence_ledger.json", "[]", "application/json")
    runtime.write_artifact(job.job_id, "section_banks.json", "[]", "application/json")
    runtime.write_artifact(
        job.job_id,
        "partial_report.md",
        "# Partial Report\n\nResume attempt in progress.\n",
        "text/markdown",
    )
    for event in snapshot["initial_events"] + snapshot["resume_events_after_seq_4"]:
        runtime.store.append_event(
            job.job_id,
            type=event["type"],
            phase=event["phase"],
            message=event.get("message") or event["type"],
            data=event.get("data") or {},
        )
    return {"job": runtime.store.get_job(job.job_id), "snapshot": snapshot}


def seed_round30_worker_restart_live_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round30_worker_restart_live.json")
    final_status = snapshot["final_status"]
    source = {
        "source_id": "R11",
        "url": "https://docs.aws.amazon.com/sagemaker/latest/dg/model-checkpoints-resume.html",
        "title": "Resume training from a checkpoint",
        "domain": "docs.aws.amazon.com",
        "source_type": "official_docs",
    }
    report_payload = {
        "query": snapshot["query"],
        "summary": "Round30 worker restart replay summary.",
        "status": final_status["report_status"],
        "sections": [],
        "unit_results": {},
        "runtime": {
            "warnings": list(final_status["runtime_warnings"]),
            "verifier": {"passed": False, "reason_codes": ["coverage_incomplete"]},
            "release_gate": {
                "passed": False,
                "reason_codes": ["coverage_incomplete"],
                "all_reason_codes": ["coverage_incomplete"],
                "soft_reason_codes": [],
            },
        },
    }
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-round30-worker-restart-live",
        status=final_status["status"],
        phase=final_status["phase"],
        effort="ultra",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=600,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=final_status["attempt_count"],
        current_checkpoint=final_status["current_checkpoint"],
        finished_at=utc_now_iso(),
        last_error=final_status["last_error"],
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps({"query": snapshot["query"], "planner_metadata": {"planner": "fallback", "used_fallback": True}}),
        "application/json",
    )
    persisted = runtime.write_artifact_batch(
        job.job_id,
        [
            {"kind": "sources.json", "content": json.dumps([source]), "content_type": "application/json"},
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {source["source_id"]: source}, "sections": []}),
                "content_type": "application/json",
            },
            {"kind": "report.json", "content": json.dumps(report_payload), "content_type": "application/json"},
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRound30 worker restart replay summary.\n",
                "content_type": "text/markdown",
            },
            {"kind": "evidence_items.json", "content": "[]", "content_type": "application/json"},
            {
                "kind": "coverage.json",
                "content": json.dumps(
                    {
                        "query": snapshot["query"],
                        "planned_section_ids": [],
                        "answered_section_ids": [],
                        "unanswered_sections": ["Open Questions"],
                        "planned_sub_question_ids": [],
                        "covered_sub_question_ids": [],
                        "uncovered_sub_questions": ["Coverage incomplete"],
                        "coverage_gate_passed": True,
                        "hard_coverage_gate_passed": False,
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": json.dumps(
                    {
                        "total_claims": 0,
                        "ungrounded_claims": 0,
                        "single_source_claims": 0,
                        "missing_evidence_binding_claims": 0,
                    }
                ),
                "content_type": "application/json",
            },
            {"kind": "verifier.json", "content": json.dumps(report_payload["runtime"]["verifier"]), "content_type": "application/json"},
        ],
    )
    for event in snapshot["initial_events"] + snapshot["resume_events_after_seq_5"]:
        runtime.store.append_event(
            job.job_id,
            type=event["type"],
            phase=event["phase"],
            message=event.get("message") or event["type"],
            data=event.get("data") or {},
        )
    return {"job": runtime.store.get_job(job.job_id), "snapshot": snapshot, "batch_id": persisted[0]["metadata"]["batch_id"]}


@pytest.mark.asyncio
async def test_plan_start_persists_outline_state_and_empty_evidence_artifacts(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain checkpoint resume semantics.",
                "reason": "Primary question.",
            },
            {
                "id": "sq2",
                "question": "Explain restart trade-offs.",
                "reason": "Secondary question.",
            },
        ]
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Checkpoint resume semantics",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline_state = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_state.json") or "{}")
    evidence_ledger = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_ledger.json") or "[]")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    checkpoint = runtime.store.get_checkpoint(response["job_id"], "planning")
    expected_section_ids = [section["section_id"] for section in response["plan"]["report_outline"]]

    assert outline_state["root_section_ids"] == expected_section_ids
    assert [node["section_id"] for node in outline_state["nodes"]] == expected_section_ids
    assert evidence_ledger == []
    assert [bank["section_id"] for bank in section_banks] == expected_section_ids
    assert checkpoint is not None
    assert checkpoint.state["section_graph"]["root_section_ids"] == expected_section_ids
    assert checkpoint.state["evidence_ledger"] == []
    assert [bank["section_id"] for bank in checkpoint.state["section_banks"]] == expected_section_ids


def test_section_graph_uses_selected_evidence_only_for_section_sources():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Checkpoint resume semantics",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "brief": {
                "objective": "Checkpoint resume semantics",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary question."}
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "resume", "title": "Resume", "goal": "Explain checkpoint resume semantics."},
                {"section_id": "restart", "title": "Restart", "goal": "Explain restart trade-offs."},
            ],
            "research_units": [],
            "continuation": {"mode": "fresh"},
            "planner_metadata": {},
        }
    )
    section_banks = [
        {
            "section_id": "resume",
            "candidate_evidence_ids": ["e1"],
            "selected_evidence_ids": ["e1"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "restart",
            "candidate_evidence_ids": ["e1"],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": ["e1"],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
    ]

    graph = update_section_graph(
        initialize_section_graph(plan, updated_at="2026-04-18T00:00:00Z"),
        plan=plan,
        source_registry=[{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}],
        selected_evidence_ids_by_section=selected_evidence_ids_by_section(section_banks),
        candidate_evidence_ids_by_section=candidate_evidence_ids_by_section(section_banks),
        rejected_evidence_ids_by_section=rejected_evidence_ids_by_section(section_banks),
        matched_unit_ids_by_section={"resume": ["unit-search-1"]},
        evidence_source_ids={"e1": ["R1"]},
        updated_at="2026-04-18T00:00:00Z",
    )
    nodes_by_id = {node["section_id"]: node for node in graph["nodes"]}

    assert nodes_by_id["resume"]["source_ids"] == ["R1"]
    assert nodes_by_id["resume"]["evidence_ids"] == ["e1"]
    assert nodes_by_id["restart"]["source_ids"] == []
    assert nodes_by_id["restart"]["evidence_ids"] == []


@pytest.mark.asyncio
async def test_runtime_materializes_section_banks_and_evidence_ledger_from_completed_unit(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain checkpoint resume semantics.",
                "reason": "Primary question.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="Checkpoint resume semantics",
        force_new=True,
        schedule=False,
    )
    await runtime.run_job(response["job_id"])

    outline_state = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_state.json") or "{}")
    outline_versions = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_versions.json") or "[]")
    evidence_ledger = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_ledger.json") or "[]")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    checkpoint = runtime.store.get_checkpoint(response["job_id"], "finalizing")

    assert outline_state["nodes"][0]["section_id"] == "resume-semantics"
    assert outline_versions
    assert outline_versions[0]["sections"][0]["section_id"] == "resume-semantics"
    assert outline_state["nodes"][0]["source_ids"] == ["R1"]
    assert outline_state["nodes"][0]["selected_evidence_ids"]
    assert outline_state["nodes"][0]["coverage_state"]["grounded_claim_ids"]
    assert set(outline_state["nodes"][0]["coverage_state"]["grounded_evidence_ids"]) == {
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    }
    assert outline_state["nodes"][0]["coverage_state"]["pool_mode"] == "selected"
    assert outline_state["nodes"][0]["coverage_state"]["explainable_by"] == [
        "claim_evidence_bindings",
        "section_packets",
    ]
    assert {entry["evidence_id"] for entry in evidence_ledger} >= {
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    }
    assert {entry["selected_section_id"] for entry in evidence_ledger} == {"resume-semantics"}
    search_entry = next(entry for entry in evidence_ledger if entry["evidence_id"] == "evidence-unit-search-1-search")
    fetch_entry = next(entry for entry in evidence_ledger if entry["evidence_id"] == "evidence-unit-search-1-fetch-1")
    assert search_entry["decision_state"] == "selected"
    assert search_entry["section_decisions"] == [{"section_id": "resume-semantics", "state": "selected"}]
    assert search_entry["question_ids"] == ["sq1"]
    assert search_entry["selection_score"] >= 1
    assert search_entry["selection_basis_tokens"]
    assert fetch_entry["line_start"] == 3
    assert fetch_entry["line_end"] == 3
    assert fetch_entry["materialized_claim_ids"]
    assert len(section_banks) == 1
    assert section_banks[0]["section_id"] == "resume-semantics"
    assert section_banks[0]["candidate_evidence_ids"] == [
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    ]
    assert section_banks[0]["selected_evidence_ids"] == [
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    ]
    assert section_banks[0]["rejected_evidence_ids"] == []
    assert {packet["evidence_id"] for packet in section_banks[0]["selected_packets"]} == {
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    }
    assert any(packet["claim_ids"] for packet in section_banks[0]["selected_packets"])
    assert checkpoint is not None
    assert set(checkpoint.state["section_graph"]["nodes"][0]["selected_evidence_ids"]) == {
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    }
    assert set(checkpoint.state["section_banks"][0]["selected_evidence_ids"]) == {
        "evidence-unit-search-1-search",
        "evidence-unit-search-1-fetch-1",
    }
    assert checkpoint.state["section_banks"][0]["selected_packets"]


@pytest.mark.asyncio
async def test_continuation_planning_bootstraps_internal_state_from_carry_forward_evidence(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Continuation internal state bootstrap source")
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Follow up continuation internal state bootstrap",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline_state = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_state.json") or "{}")
    outline_versions = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_versions.json") or "[]")
    evidence_ledger = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_ledger.json") or "[]")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json") or "{}")

    assert outline_state["nodes"]
    assert outline_versions
    assert evidence_ledger
    assert any(entry["selected_section_id"] for entry in evidence_ledger)
    assert any(bank["selected_evidence_ids"] for bank in section_banks)
    assert all("question_ids" in entry for entry in evidence_ledger)
    assert any(bank["selected_packets"] for bank in section_banks)
    assert continuation_payload["carry_forward_outline_versions"]


def test_update_section_banks_scopes_packet_claim_ids_to_the_current_section():
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
    assert bank_by_id["section-b"]["rejected_packets"][0]["claim_ids"] == ["section-b-claim-2"]


@pytest.mark.asyncio
async def test_plan_only_builds_structured_plan(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Compare open-source deep research runtimes",
        context="Focus on checkpoint resume and report citations.",
        plan_only=True,
        schedule=False,
    )

    plan = response["plan"]
    outline_versions = json.loads(runtime.store.read_artifact_text(response["job_id"], "outline_versions.json") or "[]")

    assert plan["query"] == "Compare open-source deep research runtimes"
    assert plan["brief"]["objective"]
    assert plan["sub_questions"]
    assert plan["search_strategy"]["search_queries"]
    assert plan["report_outline"]
    assert plan["outline_versions"]
    assert plan["outline_versions"][0]["sections"] == plan["report_outline"]
    assert plan["research_units"]
    assert plan["continuation"]["mode"] == "fresh"
    assert plan["planner_metadata"]["used_fallback"] is False
    assert "carry_forward_sources" not in plan["continuation"]
    assert outline_versions == plan["outline_versions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_string_shaped_strategy_and_writes_planner_trace(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"] = "Need a repaired brief"
        payload["search_strategy"] = "runtime resume checkpoints"
        payload["planner_metadata"] = "planner metadata as text"
        payload["report_outline"] = "Executive Summary"
        payload["research_units"] = "runtime resume checkpoints"
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair strategy shape",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = json.loads(runtime.store.read_artifact_text(response["job_id"], "planner_trace.json"))

    assert plan["brief"]["objective"] == "Repair strategy shape"
    assert plan["search_strategy"]["search_queries"][0] == "runtime resume checkpoints"
    assert plan["planner_metadata"]["planner"] == "model"
    assert plan["planner_metadata"]["trace"]["repair_attempted"] is False
    assert "non_dict_search_strategy" in plan["planner_metadata"]["trace"]["normalize_actions"]
    assert "non_dict_brief" in plan["planner_metadata"]["trace"]["normalize_actions"]
    assert "non_dict_planner_metadata" in plan["planner_metadata"]["trace"]["normalize_actions"]
    assert trace["normalize_actions"] == plan["planner_metadata"]["trace"]["normalize_actions"]
    assert trace["final_status"] == "normalized"


@pytest.mark.asyncio
async def test_plan_normalization_repairs_browse_page_unit_type_alias(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-browse-1",
                "unit_type": "browse_page",
                "title": "Browse docs",
                "goal": "Read the primary docs page",
                "url": "https://docs.example.com/runtime/checkpoints",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair browse page alias",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["research_units"][0]["unit_type"] == "fetch"
    assert "aliased_unit_type:browse_page->fetch" in plan["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_browse_unit_type_alias(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-browse-1",
                "unit_type": "browse",
                "title": "Browse docs",
                "goal": "Read the primary docs page",
                "url": "https://docs.example.com/runtime/checkpoints",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair browse alias",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["research_units"][0]["unit_type"] == "fetch"
    assert "aliased_unit_type:browse->fetch" in plan["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_ready_status_alias(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["status"] = "ready"
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair ready status alias",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["research_units"][0]["status"] == "pending"
    assert "aliased_unit_status:ready->pending" in plan["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_string_success_criteria_and_unknown_unit_type(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["success_criteria"] = "Ground each finding in sources."
        payload["research_units"] = [
            {
                "unit_id": "unit-browser-1",
                "unit_type": "browser",
                "title": "Read docs",
                "goal": "Read the docs page.",
                "url": "https://docs.example.com/runtime/checkpoints",
                "depends_on": [],
                "status": "waiting",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair extra dirty planner shape",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["brief"]["success_criteria"] == ["Ground each finding in sources."]
    assert plan["research_units"][0]["unit_type"] == "fetch"
    assert plan["research_units"][0]["status"] == "pending"
    assert "string_success_criteria" in trace["normalize_actions"]
    assert "inferred_unknown_unit_type:browser->fetch" in trace["normalize_actions"]
    assert "defaulted_unknown_unit_status:waiting->pending" in trace["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_invalid_approach_and_selective_fetch_bounds(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"] = {
            "approach": "wide_open",
            "search_queries": [job.query],
            "selective_fetch": {
                "max_urls_per_search": -3,
                "prefer_titles_matching_outline": "yes",
            },
        }
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair invalid planner strategy bounds",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    strategy = response["plan"]["search_strategy"]
    validation = response["plan"]["planner_metadata"]["validation"]
    trace = response["plan"]["planner_metadata"]["trace"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert strategy["approach"] == "targeted"
    assert strategy["selective_fetch"]["max_urls_per_search"] == 1
    assert strategy["selective_fetch"]["prefer_titles_matching_outline"] is True
    assert validation["repaired"] is True
    assert "invalid_search_strategy_approach" in validation["issues"]
    assert "invalid_selective_fetch_max_urls" in validation["issues"]
    assert "invalid_selective_fetch_prefer_titles_matching_outline" in validation["issues"]
    assert "defaulted_invalid_search_strategy_approach:wide_open->targeted" in trace["normalize_actions"]
    assert "clamped_selective_fetch_max_urls:-3->1" in trace["normalize_actions"]
    assert "defaulted_invalid_selective_fetch_prefer_titles_matching_outline:yes->True" in trace["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_duplicate_report_outline_section_ids(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "shared-section",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "shared-section",
                "title": "Operational Impact",
                "goal": "Explain the operational impact.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Repair duplicate report outline ids",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline = response["plan"]["report_outline"]
    validation = response["plan"]["planner_metadata"]["validation"]
    trace = response["plan"]["planner_metadata"]["trace"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert [section["title"] for section in outline] == ["Executive Summary", "Operational Impact"]
    assert len({section["section_id"] for section in outline}) == len(outline)
    assert validation["repaired"] is True
    assert "duplicate_report_outline_section_id" in validation["issues"]
    assert any(action.startswith("renamed_duplicate_report_outline_section_id:shared-section->") for action in trace["normalize_actions"])


@pytest.mark.asyncio
async def test_fallback_plan_omits_control_only_context_from_search_queries(tmp_path):
    runtime = build_runtime(tmp_path)

    async def broken_planner(job, continuation):
        raise RuntimeError("planner exploded")

    runtime._generate_plan_with_model = broken_planner

    response = await runtime.start(
        query="Plan only probe for deep research contract",
        context="Only create the plan artifact.",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    queries = response["plan"]["search_strategy"]["search_queries"]
    unit_queries = [unit.get("query", "") for unit in response["plan"]["research_units"]]

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert not any("only create the plan artifact" in query.lower() for query in queries)
    assert not any("only create the plan artifact" in query.lower() for query in unit_queries)


@pytest.mark.asyncio
async def test_planner_repair_recovers_invalid_json_and_records_trace(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []
    call_count = {"value": 0}

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        call_count["value"] += 1
        if call_count["value"] == 1:
            return "```json\n{\"brief\": \"broken\"\n```", []
        return (
            json.dumps(
                {
                    "brief": {"objective": "Repair from retry", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                    "sub_questions": [{"id": "sq1", "question": "Repair from retry", "reason": "Cover the primary question."}],
                    "search_strategy": {
                        "approach": "targeted",
                        "search_queries": ["Repair from retry"],
                        "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                    },
                    "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                    "research_units": [
                        {
                            "unit_id": "unit-search-1",
                            "unit_type": "search",
                            "title": "Primary search",
                            "goal": "Repair from retry",
                            "query": "Repair from retry",
                            "depends_on": [],
                            "status": "pending",
                            "notes": "",
                        }
                    ],
                    "planner_metadata": {"planner": "model", "used_fallback": False},
                }
            ),
            [],
        )

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    response = await runtime.start(
        query="Repair planner json",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    trace = json.loads(runtime.store.read_artifact_text(response["job_id"], "planner_trace.json"))

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert response["plan"]["brief"]["objective"] == "Repair from retry"
    assert call_count["value"] == 2
    assert observed_models == ["grok-4.20-0309", "grok-4.20-0309"]
    assert trace["repair_attempted"] is True
    assert trace["repair_succeeded"] is True
    assert trace["initial_parse_error"]
    assert trace["repair_parse_path"] == "direct_json"
    assert trace["final_status"] == "normalized"


def test_parse_json_object_with_trace_salvages_embedded_json_object():
    parsed, trace = deep_research_runtime_module._parse_json_object_with_trace(
        "Planner notes before JSON.\n"
        "{\"brief\": {\"objective\": \"Embedded plan\"}, \"planner_metadata\": {\"planner\": \"embedded\"}}\n"
        "Trailing commentary after JSON."
    )

    assert parsed["brief"]["objective"] == "Embedded plan"
    assert parsed["planner_metadata"]["planner"] == "embedded"
    assert trace["parse_path"] == "embedded_json"
    assert trace["parsed_type"] == "dict"


def test_parse_json_object_with_trace_prefers_plan_like_embedded_object_over_debug_blob():
    parsed, trace = deep_research_runtime_module._parse_json_object_with_trace(
        "Planner debug:\n"
        "{\"debug\": true, \"message\": \"ignore this blob\"}\n"
        "Recovered plan:\n"
        "{\"brief\": {\"objective\": \"Recovered embedded plan\"}, "
        "\"sub_questions\": [{\"id\": \"sq1\", \"question\": \"Recovered embedded plan\", \"reason\": \"Primary axis.\"}], "
        "\"search_strategy\": {\"approach\": \"targeted\", \"search_queries\": [\"Recovered embedded plan\"]}, "
        "\"report_outline\": [{\"section_id\": \"summary\", \"title\": \"Summary\", \"goal\": \"Summarize the answer.\"}]}\n"
        "Trailing commentary."
    )

    assert parsed["brief"]["objective"] == "Recovered embedded plan"
    assert parsed["sub_questions"][0]["id"] == "sq1"
    assert trace["parse_path"] == "embedded_json"
    assert "Recovered embedded plan" in trace["embedded_json_preview"]


def test_attempt_window_anchor_seq_paginates_past_first_ten_thousand_events(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Paginate watch anchor lookup",
        request_fingerprint="fp-paginate-watch-anchor-lookup",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    job = runtime.store.update_job(job.job_id, attempt_count=2, current_checkpoint="researching-u6")

    class FakeStore:
        def __init__(self):
            self.calls = []

        def list_events(self, job_id, *, after_seq=0, limit=100):
            self.calls.append((job_id, after_seq, limit))
            all_events = [
                SimpleNamespace(seq=seq, type="phase_started", data={})
                for seq in range(1, 10_001)
            ]
            all_events.extend(
                [
                    SimpleNamespace(
                        seq=10_001,
                        type="job_interrupted",
                        data={"attempt_id": "attempt-1", "attempt_count": 1, "reason": "worker_restarted"},
                    ),
                    SimpleNamespace(
                        seq=10_002,
                        type="job_resumed",
                        data={"attempt_id": "attempt-2", "attempt_count": 2},
                    ),
                ]
            )
            page = []
            for event in all_events:
                if event.seq <= after_seq:
                    continue
                page.append(event)
                if len(page) >= limit:
                    break
            return page

    store = FakeStore()

    anchor_seq = deep_research_runtime_module._attempt_window_anchor_seq(store, job)

    assert anchor_seq == 10_001
    assert len(store.calls) >= 2
    assert store.calls[0][1] == 0
    assert any(after_seq >= 10_000 for _, after_seq, _ in store.calls[1:])


@pytest.mark.asyncio
async def test_planner_attempts_targeted_repair_before_fallback_on_normalize_failure(tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "broken-section",
                "title": {"broken": "shape"},
                "goal": "Break normalization first.",
            }
        ]
        return payload

    async def repair(job, continuation, raw_plan, planner_trace, error):
        repaired = structured_plan_payload(job, continuation)
        repaired["planner_metadata"] = {"planner": "repair", "used_fallback": False}
        return repaired

    runtime._generate_plan_with_model = planner
    runtime._repair_plan_after_normalize_failure = repair

    response = await runtime.start(
        query="Normalize failure repair loop",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    trace = response["plan"]["planner_metadata"]["trace"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert trace["repair_attempted"] is True
    assert trace["repair_succeeded"] is True
    assert trace["repair_stage"] == "normalize"
    assert trace["final_status"] == "normalized"


@pytest.mark.asyncio
async def test_planner_repair_failure_falls_back_with_explicit_repair_stage(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []
    call_count = {"value": 0}

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        call_count["value"] += 1
        return "```json\n{\"brief\": \"broken\"\n```", []

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    response = await runtime.start(
        query="Repair planner failure stage",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    trace = json.loads(runtime.store.read_artifact_text(response["job_id"], "planner_trace.json"))

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "repair"
    assert call_count["value"] == 2
    assert observed_models == ["grok-4.20-0309", "grok-4.20-0309"]
    assert trace["repair_attempted"] is True
    assert trace["repair_succeeded"] is False
    assert trace["final_status"] == "repair_failed"
    assert trace["repair_error"]


@pytest.mark.asyncio
async def test_plan_only_does_not_schedule_execution(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    executed = []

    async def should_not_run(runtime, job_id):
        executed.append(job_id)

    runtime._runner = should_not_run

    response = await runtime.start(
        query="Plan only should stay draft",
        context="Only create the plan artifact.",
        plan_only=True,
    )
    await asyncio.sleep(0.05)

    status = await runtime.status(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert executed == []
    assert status["status"] == "draft"
    assert [event["type"] for event in events["events"]] == ["job_created"]
    assert "plan.json" in status["artifact_kinds"]
    assert "planner_trace.json" in status["artifact_kinds"]
    assert "outline_state.json" in status["artifact_kinds"]
    assert "evidence_ledger.json" in status["artifact_kinds"]
    assert "section_banks.json" in status["artifact_kinds"]


@pytest.mark.asyncio
async def test_plan_only_job_cannot_be_run_or_resumed(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Plan only guardrail",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    run_result = await runtime.run_job(response["job_id"])
    resume_result = await runtime.resume(response["job_id"], schedule=False)
    events = await runtime.events(response["job_id"])

    assert run_result["status"] == "draft"
    assert resume_result["status"] == "draft"
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md") is None
    assert any(event["type"] == "plan_only_execution_blocked" for event in events["events"])


@pytest.mark.asyncio
async def test_status_reconciles_stale_running_job_before_read(tmp_path):
    runtime = build_runtime(tmp_path)
    stale_job = runtime.store.create_job(
        query="Stale running job",
        request_fingerprint="fp-stale-running-read",
        status="running",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        stale_job.job_id,
        heartbeat_at="2000-01-01T00:00:00Z",
        started_at="2000-01-01T00:00:00Z",
    )
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", stale_job.job_id),
        )

    other_runtime = DeepResearchRuntime(runtime.store.root_dir)
    status = await other_runtime.status(stale_job.job_id)
    events = await other_runtime.events(stale_job.job_id)

    assert status["status"] == "interrupted"
    assert any(event["type"] == "job_interrupted" for event in events["events"])


@pytest.mark.asyncio
async def test_status_does_not_reconcile_fresh_running_job_from_another_runtime(tmp_path):
    runtime = build_runtime(tmp_path)
    fresh_job = runtime.store.create_job(
        query="Fresh running job",
        request_fingerprint="fp-fresh-running-read",
        status="running",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        fresh_job.job_id,
        current_checkpoint="researching-unit-search-1",
        heartbeat_at=utc_now_iso(),
        started_at=utc_now_iso(),
    )
    other_runtime = DeepResearchRuntime(runtime.store.root_dir)

    status = await other_runtime.status(fresh_job.job_id)
    events = await other_runtime.events(fresh_job.job_id)

    assert status["status"] == "running"
    assert not any(event["type"] == "job_interrupted" for event in events["events"])
    assert runtime.store.get_job(fresh_job.job_id).status == "running"


@pytest.mark.asyncio
async def test_start_keeps_job_draft_until_planning_artifacts_are_persisted(tmp_path):
    runtime = build_runtime(tmp_path)
    planner_entered = asyncio.Event()
    allow_finish = asyncio.Event()

    async def blocked_planner(job, continuation):
        planner_entered.set()
        await allow_finish.wait()
        return structured_plan_payload(job, continuation)

    runtime._generate_plan_with_model = blocked_planner

    start_task = asyncio.create_task(runtime.start(query="Draft planning window", force_new=True, schedule=False))
    await planner_entered.wait()

    jobs = runtime.store.list_jobs(limit=10)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == "draft"

    other_runtime = DeepResearchRuntime(runtime.store.root_dir)
    status = await other_runtime.status(job.job_id)
    events = await other_runtime.events(job.job_id)

    assert status["status"] == "draft"
    assert not any(event["type"] == "job_interrupted" for event in events["events"])

    allow_finish.set()
    response = await start_task

    assert response["status"] == "queued"
    assert runtime.store.get_job(job.job_id).status == "queued"


@pytest.mark.asyncio
async def test_planner_preselects_available_grok_model_for_deep_research(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-non-reasoning"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        return json.dumps(
            {
                "brief": {"objective": "Planner preselection", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                "sub_questions": [{"id": "sq1", "question": "Planner preselection", "reason": "Cover the primary question."}],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Planner preselection"],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
                "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Planner preselection",
                        "query": "Planner preselection",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "test", "used_fallback": False},
            }
        ), []

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Planner preselection", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-0309-non-reasoning"]


@pytest.mark.asyncio
async def test_standard_effort_prefers_expert_before_fallback(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-expert", "grok-4.20-reasoning", "grok-4.20-auto"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        return json.dumps(
            {
                "brief": {"objective": "Standard effort", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                "sub_questions": [{"id": "sq1", "question": "Standard effort", "reason": "Cover the primary question."}],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Standard effort"],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
                "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Standard effort",
                        "query": "Standard effort",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "test", "used_fallback": False},
            }
        ), []

    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("GROK_DEEP_RESEARCH_STANDARD_MODEL", raising=False)
    monkeypatch.setattr(server.config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(server.config, "_load_config_file", lambda: {})
    server.config.reset_runtime_state()
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Standard effort", effort="standard", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-expert"]


@pytest.mark.asyncio
async def test_deep_effort_prefers_multi_agent_default_and_preserves_single_agent_fallback(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-reasoning"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        return json.dumps(
            {
                "brief": {"objective": "Deep effort fallback", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                "sub_questions": [{"id": "sq1", "question": "Deep effort fallback", "reason": "Cover the primary question."}],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Deep effort fallback"],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
                "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Deep effort fallback",
                        "query": "Deep effort fallback",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "test", "used_fallback": False},
            }
        ), []

    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("GROK_DEEP_RESEARCH_ULTRA_PROFILE", raising=False)
    monkeypatch.setattr(server.config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(server.config, "_load_config_file", lambda: {})
    server.config.reset_runtime_state()
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Deep effort fallback", effort="deep", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-0309-reasoning"]


@pytest.mark.asyncio
async def test_ultra_effort_prefers_heavy_16_agent_before_fallback(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-heavy-16-agent", "grok-4.20-multi-agent"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        return json.dumps(
            {
                "brief": {"objective": "Ultra effort", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                "sub_questions": [{"id": "sq1", "question": "Ultra effort", "reason": "Cover the primary question."}],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Ultra effort"],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
                "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Ultra effort",
                        "query": "Ultra effort",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "test", "used_fallback": False},
            }
        ), []

    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("GROK_DEEP_RESEARCH_ULTRA_PROFILE", raising=False)
    monkeypatch.setattr(server.config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(server.config, "_load_config_file", lambda: {})
    server.config.reset_runtime_state()
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Ultra effort", effort="ultra", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-heavy-16-agent"]


@pytest.mark.asyncio
async def test_ultra_effort_falls_back_to_heavy_single_agent_before_lower_tiers(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-heavy", "grok-4.20-expert-4-agent", "grok-4.20-reasoning"]

    async def fake_execute(self, headers, payload, ctx=None, render_sources=False):
        observed_models.append(payload["model"])
        return json.dumps(
            {
                "brief": {"objective": "Ultra effort fallback", "deliverable": "A cited report.", "success_criteria": ["Produce a structured report."]},
                "sub_questions": [{"id": "sq1", "question": "Ultra effort fallback", "reason": "Cover the primary question."}],
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": ["Ultra effort fallback"],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
                "report_outline": [{"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."}],
                "research_units": [
                    {
                        "unit_id": "unit-search-1",
                        "unit_type": "search",
                        "title": "Primary search",
                        "goal": "Ultra effort fallback",
                        "query": "Ultra effort fallback",
                        "depends_on": [],
                        "status": "pending",
                        "notes": "",
                    }
                ],
                "planner_metadata": {"planner": "test", "used_fallback": False},
            }
        ), []

    monkeypatch.setenv("GROK_API_URL", "https://grok2api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("GROK_DEEP_RESEARCH_ULTRA_PROFILE", raising=False)
    monkeypatch.setattr(server.config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(server.config, "_load_config_file", lambda: {})
    server.config.reset_runtime_state()
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Ultra effort fallback", effort="ultra", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-heavy"]


@pytest.mark.asyncio
async def test_plan_normalization_replays_round7_probe_shape_with_focused_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        request_fingerprint="fp-round7-replay",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume semantics and restart trade-offs were already covered once.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    fixture = load_deep_research_fixture("round7_planner_replay.json")

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = fixture["sub_questions"]
        payload["report_outline"] = fixture["report_outline"]
        payload["research_units"] = fixture["research_units"]
        payload["search_strategy"]["search_queries"] = [fixture["query"]]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query=fixture["query"],
        context=fixture["context"],
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    plan_json = json.dumps(plan).lower()

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["research_units"][0]["status"] == "pending"
    assert "continuation workflow" not in plan_json
    assert plan["planner_metadata"]["trace"]["unsafe_plan"] is False
    assert "generic_continuation_outline" in plan["planner_metadata"]["validation"]["issues"]
    assert "expanded_outline_from_follow_up_surface" in plan["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_search_query_preselects_available_grok_model_for_deep_research(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-non-reasoning"]

    async def fake_search_with_sources(self, query, **kwargs):
        observed_models.append(self.model)
        return ("Answer", [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}])

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)

    async def planner(job, continuation):
        return structured_plan_payload(job, continuation)

    runtime._generate_plan_with_model = planner

    response = await runtime.start(query="Search preselection", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    assert observed_models == ["grok-4.20-0309-non-reasoning"]


@pytest.mark.asyncio
async def test_search_query_details_uses_deep_effort_profile_before_single_agent_fallback(monkeypatch, tmp_path):
    observed_models = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-reasoning"]

    async def fake_search_with_sources(self, query, **kwargs):
        observed_models.append(self.model)
        return ("Answer", [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}])

    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)

    answer, sources = await _search_query("Search preselection", effort="deep")

    assert answer == "Answer"
    assert sources
    assert observed_models == ["grok-4.20-0309-reasoning"]


@pytest.mark.asyncio
async def test_search_query_details_surfaces_body_quality_warning_and_runtime_metadata(monkeypatch, tmp_path):
    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-non-reasoning"]

    async def fake_search_with_sources(self, query, **kwargs):
        self._last_success_provider_name = "fallback_1"
        self._last_success_provider_model = "grok-4.20-0309-non-reasoning"
        self._last_success_provider_api_url = "https://secondary.example.com/v1"
        return (
            "",
            [{"url": "https://docs.example.com/a", "title": "Doc A"}],
        )

    async def fake_tavily_search(query, max_results=6, **kwargs):
        return [
            {
                "url": "https://docs.example.com/b",
                "title": "Doc B",
                "content": "Supplemental source from Tavily.",
                "provider": "tavily",
            }
        ]

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)
    monkeypatch.setattr(server, "_call_tavily_search", fake_tavily_search)

    from grok_search.deep_research_runtime import _search_query_with_details

    result = await _search_query_with_details("Search preselection")

    assert result["warning_code"] == "body_missing_sources_only"
    assert result["requested_model"] == "grok-4.20-0309"
    assert result["effective_model"] == "grok-4.20-0309-non-reasoning"
    assert result["provider_name"] == "fallback_1"
    assert result["provider_model"] == "grok-4.20-0309-non-reasoning"
    assert result["provider_api_url"] == "https://secondary.example.com/v1"
    assert {source["url"] for source in result["sources"]} == {
        "https://docs.example.com/a",
        "https://docs.example.com/b",
    }


@pytest.mark.asyncio
async def test_completed_report_with_only_runtime_warning_is_marked_degraded(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        return structured_plan_payload(job, continuation)

    async def warned_search(query, *, effort="standard"):
        return {
            "answer": "",
            "sources": [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}],
            "warning_code": "body_missing_sources_only",
            "requested_model": "grok-4.20-0309",
            "effective_model": "grok-4.20-0309",
            "provider_name": "primary",
            "provider_model": "grok-4.20-0309",
            "provider_api_url": "https://primary.example.com/v1",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", warned_search)

    response = await runtime.start(query="Degraded runtime warning", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert result["status"] == "failed"
    if isinstance(result.get("report"), dict):
        assert result["report"]["status"] == "failed"
        assert "body_missing_sources_only" in result["report"]["runtime"]["warnings"]
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_provider_error_text_is_warning_not_evidence(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        return structured_plan_payload(job, continuation)

    async def overloaded_search(query, *, effort="standard"):
        return {
            "answer": "This model is overloaded right now. Please try again shortly or pick a different model.",
            "sources": [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-fast",
            "provider_name": "primary",
            "provider_model": "grok-4.20-fast",
            "provider_api_url": "https://primary.example.com/v1",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", overloaded_search)

    response = await runtime.start(
        query="Provider overload handling",
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    plan = DeepResearchPlan.model_validate(response["plan"])
    unit_result, sources, evidence_items = await _execute_research_unit(
        runtime,
        plan,
        plan.research_units[0],
    )

    assert sources == [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}]
    assert "provider_response_model_overloaded" in unit_result.get("warnings", [])
    assert unit_result.get("summary", "") == ""
    assert evidence_items == []


@pytest.mark.asyncio
async def test_search_unit_adds_verified_aws_dms_api_reference_hint_for_recoverycheckpoint(
    monkeypatch,
    tmp_path,
):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "u1",
                "unit_type": "search",
                "title": "ReplicationTask Data Type",
                "goal": "Detail the RecoveryCheckpoint field definition",
                "query": "ReplicationTask RecoveryCheckpoint site:docs.aws.amazon.com",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search_with_details(query, *, effort="standard"):
        return {
            "answer": "RecoveryCheckpoint is described for AWS DMS replication tasks.",
            "sources": [
                {
                    "url": "https://docs.aws.amazon.com/goto/boto3/dms-2016-01-01/DescribeReplicationTasks",
                    "title": "DescribeReplicationTasks paginator",
                    "description": "Boto3 paginator reference for DMS DescribeReplicationTasks.",
                    "domain": "docs.aws.amazon.com",
                    "provider": "primary",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-fast",
            "provider_name": "primary",
            "provider_model": "grok-4.20-fast",
            "provider_api_url": "https://primary.example.com/v1",
        }

    async def fetch_with_details(url):
        fetched_urls.append(url)
        return {
            "content": (
                "# ReplicationTask\n\n"
                "RecoveryCheckpoint is a string field on ReplicationTask that records the last checkpoint "
                "from a Change Data Capture operation."
            ),
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url_with_details", fetch_with_details)

    response = await runtime.start(
        query="AWS DMS DescribeReplicationTasks RecoveryCheckpoint official docs",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    plan = DeepResearchPlan.model_validate(response["plan"])
    unit_result, sources, evidence_items = await _execute_research_unit(runtime, plan, plan.research_units[0])

    assert fetched_urls == ["https://docs.aws.amazon.com/dms/latest/APIReference/API_ReplicationTask.html"]
    assert any(source["url"] == fetched_urls[0] for source in sources)
    assert unit_result["supporting_provider_attempts"][0]["attempt_role"] == "selective_fetch"
    assert any("RecoveryCheckpoint is a string field" in item.get("detail", "") for item in evidence_items)


@pytest.mark.asyncio
async def test_search_query_propagates_preselected_model_to_fallback_provider(monkeypatch, tmp_path):
    observed = []

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-non-reasoning"]

    async def fake_search_with_sources(self, query, **kwargs):
        observed.append(
            {
                "primary_model": self.model,
                "fallback_model": self._provider_chain[1]["model"],
            }
        )
        return ("Answer", [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}])

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_API_URL_2", "https://secondary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY_2", "secondary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.delenv("GROK_MODEL_2", raising=False)
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)

    answer, sources = await runtime_module_search_query()

    assert answer == "Answer"
    assert sources
    assert observed == [
        {
            "primary_model": "grok-4.20-0309-non-reasoning",
            "fallback_model": "grok-4.20-0309-non-reasoning",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_provider_preselection_propagates_to_openrouter_fallback(monkeypatch, tmp_path):
    from grok_search import deep_research_runtime as runtime_module

    observed = []
    original_get_env_value = runtime_module.config._get_env_value

    async def fake_models(api_url, api_key):
        return ["grok-4.20-0309-non-reasoning"]

    async def fake_search_with_sources(self, query, **kwargs):
        observed.append(
            {
                "primary_model": self.model,
                "fallback_model": self._provider_chain[1]["model"],
            }
        )
        return ("Answer", [{"url": "https://docs.example.com/runtime", "title": "Runtime docs"}])

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_API_URL_2", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROK_API_KEY_2", "secondary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.delenv("GROK_MODEL_2", raising=False)
    monkeypatch.setattr(
        runtime_module.config,
        "_get_env_value",
        lambda key, default=None: None if key == "GROK_MODEL_2" else original_get_env_value(key, default),
    )
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)

    answer, sources = await runtime_module_search_query()

    assert answer == "Answer"
    assert sources
    assert observed == [
        {
            "primary_model": "grok-4.20-0309-non-reasoning",
            "fallback_model": "grok-4.20-0309-non-reasoning:online",
        }
    ]


async def runtime_module_search_query():
    from grok_search.deep_research_runtime import _search_query

    return await _search_query("Search preselection")


@pytest.mark.asyncio
async def test_continue_plan_uses_previous_artifacts(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Original research",
        request_fingerprint="fp-original-runtime",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "final_report.md",
        "# Final Report\n\nPrior findings about resume behavior.",
        "text/markdown",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Prior findings about resume behavior.",
                "sections": [{"title": "Resume", "claims": [{"text": "Resume should continue from checkpoints."}]}],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://example.com/resume"}]),
        "application/json",
    )

    response = await runtime.start(
        query="Follow up on recovery behavior",
        continue_from_job_id=original.job_id,
        plan_only=True,
        schedule=False,
        force_new=True,
    )

    continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation["mode"] == "continue"
    assert continuation["source_job_id"] == original.job_id
    assert "resume should continue from checkpoints" in continuation["previous_summary"].lower()
    assert continuation["source_count"] == 1
    assert continuation["source_job_status"] == "completed"
    assert "carry_forward_sources" not in continuation
    assert continuation_payload["carry_forward_sources"][0]["source_id"] == "R1"
    assert continuation_payload["carry_forward_sections"][0]["title"] == "Resume"
    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert "checkpoint checkpoint" not in json.dumps(response["plan"]).lower()


@pytest.mark.asyncio
async def test_continue_plan_uses_partial_report_when_final_artifacts_missing(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Interrupted research",
        request_fingerprint="fp-interrupted-runtime",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "partial_report.md",
        "# Partial Report\n\nCheckpoint-aware resume is the main finding so far.",
        "text/markdown",
    )
    runtime.store.save_checkpoint(
        original.job_id,
        phase="researching",
        checkpoint_key="researching-unit-search-1",
        state={
            "plan": structured_plan_payload(original, {"mode": "fresh", "source_job_id": "", "previous_summary": "", "prior_plan_summary": "", "source_count": 0}),
            "sources": [{"source_id": "R1", "url": "https://example.com/checkpoint"}],
        },
    )

    response = await runtime.start(
        query="Continue interrupted work",
        continue_from_job_id=original.job_id,
        plan_only=True,
        schedule=False,
        force_new=True,
    )

    continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert "checkpoint-aware resume" in continuation["previous_summary"].lower()
    assert continuation["source_count"] == 1
    assert continuation["checkpoint_key"] == "researching-unit-search-1"
    assert "carry_forward_sources" not in continuation
    assert continuation_payload["carry_forward_sources"][0]["source_id"] == "R1"


@pytest.mark.asyncio
async def test_run_job_outputs_consistent_sources_citations_and_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"].append(
            {
                "section_id": "resume-behavior",
                "title": "Resume Behavior",
                "goal": "Explain runtime resume semantics.",
            }
        )
        payload["research_units"][0]["goal"] = "Find the primary evidence."
        payload["research_units"][0]["query"] = "deep research resume semantics"
        payload["search_strategy"]["search_queries"] = ["deep research resume semantics"]
        return payload

    async def fake_search(query):
        return (
            "Resume should continue from the last completed research unit.",
            [
                {
                    "url": "https://example.com/resume",
                    "title": "Resume Guide",
                    "description": "Checkpoint-aware resume behavior.",
                }
            ],
        )

    async def fake_fetch(url):
        return "# Resume Guide\n\nCheckpoint-aware resume continues from the last completed unit."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="How should deep research resume work?", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    result = await runtime.result(response["job_id"])
    sources = json.loads(runtime.store.read_artifact_text(response["job_id"], "sources.json"))
    citations = result["citations"]
    report = result["report"]

    assert result["status"] == "completed"
    assert sources[0]["source_id"] == "R1"
    assert citations["source_registry"]["R1"]["url"] == "https://example.com/resume"
    assert citations["sections"][0]["claims"]
    assert citations["sections"][0]["claims"][0]["citations"] == ["R1"]
    assert report["sections"][0]["claims"][0]["citations"] == ["R1"]
    assert "[R1]" in result["final_report"]


@pytest.mark.asyncio
async def test_completed_artifacts_share_same_batch_id(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        return structured_plan_payload(job, continuation)

    async def fake_search(query):
        return ("Batch-safe answer.", [{"url": "https://example.com/batch", "title": "Batch"}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Batch finalization", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    status = await runtime.status(response["job_id"])
    final_artifacts = {
        artifact["kind"]: artifact
        for artifact in status["artifacts"]
        if artifact["kind"] in {"sources.json", "citations.json", "report.json", "final_report.md"}
    }
    batch_ids = {artifact["metadata"]["batch_id"] for artifact in final_artifacts.values()}

    assert batch_ids == {next(iter(batch_ids))}
    assert next(iter(batch_ids))


@pytest.mark.asyncio
async def test_continue_runtime_reuses_prior_sources_and_evidence(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Original research",
        request_fingerprint="fp-original-reuse",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://example.com/old", "title": "Old source"}]),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Old evidence matters.",
                "sections": [
                    {
                        "section_id": "previous",
                        "title": "Previous",
                        "claims": [{"claim_id": "c1", "text": "Old evidence matters.", "citations": ["R1"]}],
                        "citations": ["R1"],
                    }
                ],
                "unit_results": {
                    "unit-old": {
                        "unit_id": "unit-old",
                        "unit_type": "search",
                        "summary": "Old evidence matters.",
                        "detail": "Old evidence matters in detail.",
                        "source_ids": ["R1"],
                        "citations": ["R1"],
                    }
                },
            }
        ),
        "application/json",
    )

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["query"] = "new evidence"
        payload["search_strategy"]["search_queries"] = ["new evidence"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def fake_search(query):
        return ("New evidence arrives.", [{"url": "https://example.com/new", "title": "New source"}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query="Continue with new evidence",
        continue_from_job_id=original.job_id,
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    source_registry = result["citations"]["source_registry"]

    assert "https://example.com/old" in {item["url"] for item in source_registry.values()}
    assert "https://example.com/new" in {item["url"] for item in source_registry.values()}


@pytest.mark.asyncio
async def test_result_returns_citations_artifact_shape_without_flattening(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Citations shape",
        request_fingerprint="fp-citations-shape",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        job.job_id,
        "citations.json",
        json.dumps(
            {
                "source_registry": {"R1": {"source_id": "R1", "url": "https://example.com/source"}},
                "sections": [{"section_id": "s1", "title": "Section", "claims": [], "citations": ["R1"]}],
            }
        ),
        "application/json",
    )

    result = await runtime.result(job.job_id)

    assert result["citations"] == {
        "source_registry": {"R1": {"source_id": "R1", "url": "https://example.com/source"}},
        "sections": [{"section_id": "s1", "title": "Section", "claims": [], "citations": ["R1"]}],
    }


@pytest.mark.asyncio
async def test_result_normalizes_legacy_flat_citations_shape(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Legacy citations shape",
        request_fingerprint="fp-legacy-citations-shape",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        job.job_id,
        "citations.json",
        json.dumps({"R1": {"source_id": "R1", "url": "https://example.com/source"}}),
        "application/json",
    )

    result = await runtime.result(job.job_id)

    assert result["citations"] == {
        "source_registry": {"R1": {"source_id": "R1", "url": "https://example.com/source"}},
        "sections": [],
    }


@pytest.mark.asyncio
async def test_resume_continues_from_completed_unit_checkpoint(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    executed_units = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "One?", "reason": "first"},
            {"id": "sq2", "question": "Two?", "reason": "second"},
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["one", "two"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": False},
        }
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "First",
                "query": "one",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Second",
                "query": "two",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        return payload

    async def fake_search(query):
        executed_units.append(query)
        return (
            f"Answer for {query}",
            [{"url": f"https://example.com/{query}", "title": f"Source {query}"}],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    async def no_fetch(url):
        return None

    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Resume runtime", force_new=True, schedule=False)
    plan = response["plan"]
    runtime.store.update_job(
        response["job_id"],
        status="interrupted",
        phase="researching",
        current_checkpoint="researching-unit-search-1",
    )
    runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-unit-search-1",
        state={
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "First unit already completed.",
                    "source_ids": ["R1"],
                    "citations": ["R1"],
                }
            },
            "sources": [{"source_id": "R1", "url": "https://example.com/one", "title": "Source one"}],
            "evidence_items": [],
            "sections": [],
            "plan": plan,
        },
    )

    resumed = await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])

    assert resumed["status"] == "queued"
    assert executed_units == ["two"]


@pytest.mark.asyncio
async def test_resume_preserves_skipped_and_constraint_state_from_checkpoint(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    executed_units = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "One?", "reason": "first"},
            {"id": "sq2", "question": "Two?", "reason": "second"},
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["one", "two"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": False},
        }
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "First",
                "query": "one",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Second",
                "query": "two",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        return payload

    async def fake_search(query):
        executed_units.append(query)
        return (
            f"Answer for {query}",
            [{"url": f"https://example.com/{query}", "title": f"Source {query}"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Resume runtime with skipped state", force_new=True, schedule=False)
    plan = response["plan"]
    runtime.store.update_job(
        response["job_id"],
        status="interrupted",
        phase="researching",
        current_checkpoint="researching-unit-search-1",
    )
    runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-unit-search-1",
        state={
            "completed_unit_ids": ["unit-search-1"],
            "failed_unit_ids": [],
            "failed_units": [],
            "skipped_unit_ids": ["unit-search-2"],
            "skipped_units": [
                {
                    "unit_id": "unit-search-2",
                    "unit_type": "search",
                    "reason": "max_search_queries_reached",
                }
            ],
            "constraint_violations": [
                {
                    "unit_id": "unit-search-1",
                    "removed_source_count": 2,
                    "reason": "domain_constraints_applied",
                }
            ],
            "coverage_state": {
                "items": [
                    {
                        "target": "One?",
                        "matched_unit_ids": ["unit-search-1"],
                        "grounded_source_ids": ["R1"],
                        "candidate_section_ids": ["executive-summary"],
                        "satisfied": True,
                    },
                    {
                        "target": "Two?",
                        "matched_unit_ids": [],
                        "grounded_source_ids": [],
                        "candidate_section_ids": [],
                        "satisfied": False,
                    },
                ]
            },
            "unit_results": {
                "unit-search-1": {
                    "summary": "First unit already completed.",
                    "source_ids": ["R1"],
                    "citations": ["R1"],
                }
            },
            "sources": [{"source_id": "R1", "url": "https://example.com/one", "title": "Source one"}],
            "evidence_items": [],
            "sections": [],
            "plan": plan,
        },
    )

    resumed = await runtime.resume(response["job_id"], schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert resumed["status"] == "queued"
    assert executed_units == []
    expected_skipped_units = [
        {
            "unit_id": "unit-search-2",
            "unit_type": "search",
            "reason": "max_search_queries_reached",
        }
    ]
    expected_constraint_violations = [
        {
            "unit_id": "unit-search-1",
            "removed_source_count": 2,
            "reason": "domain_constraints_applied",
        }
    ]
    if isinstance(result.get("report"), dict):
        assert result["report"]["runtime"]["skipped_units"] == expected_skipped_units
        assert result["report"]["runtime"]["constraint_violations"] == expected_constraint_violations
    else:
        finished_job = runtime.store.get_job(response["job_id"])
        final_checkpoint = runtime.store.get_checkpoint(response["job_id"], finished_job.current_checkpoint)
        assert final_checkpoint is not None
        assert final_checkpoint.state["skipped_units"] == expected_skipped_units
        assert final_checkpoint.state["constraint_violations"] == expected_constraint_violations


@pytest.mark.asyncio
async def test_resume_returns_full_status_shaped_payload(tmp_path):
    runtime = build_runtime(tmp_path)
    response = await runtime.start(query="Resume payload shape", force_new=True, schedule=False)
    runtime.store.update_job(
        response["job_id"],
        status="interrupted",
        phase="researching",
        current_checkpoint="researching-unit-search-1",
    )

    resumed = await runtime.resume(response["job_id"], schedule=False)

    assert resumed["status"] == "queued"
    assert "artifact_kinds" in resumed
    assert "artifacts" in resumed
    assert "plan.json" in resumed["artifact_kinds"]


@pytest.mark.asyncio
async def test_status_returns_artifact_summaries(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Artifact summary",
        request_fingerprint="fp-artifact-summary",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "final_report.md", "# Final Report\n\nBody.", "text/markdown")

    status = await runtime.status(job.job_id)

    assert status["artifact_kinds"] == ["final_report.md"]
    assert status["artifacts"][0]["kind"] == "final_report.md"
    assert status["artifacts"][0]["content_type"] == "text/markdown"
    assert status["artifacts"][0]["metadata"]["bytes"] > 0


@pytest.mark.asyncio
async def test_resume_migrates_legacy_plan_and_checkpoint(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Legacy resume",
        request_fingerprint="fp-legacy-resume",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    legacy_plan = {
        "query": "Legacy resume",
        "context": "",
        "effort": "standard",
        "time_budget_seconds": 240,
        "include_domains": [],
        "exclude_domains": [],
        "search_queries": ["legacy search"],
        "report_sections": ["Executive Summary", "Detailed Report"],
    }
    runtime.write_artifact(job.job_id, "plan.json", json.dumps(legacy_plan), "application/json")
    runtime.store.save_checkpoint(
        job.job_id,
        phase="planning",
        checkpoint_key="planning",
        state=legacy_plan,
    )

    async def fake_search(query):
        return ("Legacy search completed.", [{"url": "https://example.com/legacy", "title": "Legacy"}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    resumed = await runtime.resume(job.job_id, schedule=False)
    result = await runtime.run_job(job.job_id)

    assert resumed["status"] == "queued"
    assert result["status"] == "failed"
    assert result["plan"]["brief"]["objective"] == "Legacy resume"


@pytest.mark.asyncio
async def test_checkpoint_fallback_records_explicit_event(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_search(query):
        return ("Recovered from older checkpoint.", [{"url": "https://example.com/recovered", "title": "Recovered"}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Fallback checkpoint", force_new=True, schedule=False)
    plan = response["plan"]
    runtime.store.update_job(
        response["job_id"],
        status="interrupted",
        phase="researching",
        current_checkpoint="researching-bad",
    )
    runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-good",
        state={
            "completed_unit_ids": [],
            "unit_results": {},
            "sources": [],
            "evidence_items": [],
            "sections": [],
            "plan": plan,
        },
    )
    runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-bad",
        state={"plan": {"query": "broken"}},
    )

    await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert any(event["type"] == "checkpoint_fallback" for event in events["events"])
    fallback_event = next(event for event in events["events"] if event["type"] == "checkpoint_fallback")
    assert fallback_event["data"]["invalid_checkpoint_keys"] == ["researching-bad"]
    assert fallback_event["data"]["invalid_checkpoints"] == [
        {
            "checkpoint_key": "researching-bad",
            "checkpoint_seq": 3,
            "reason": "missing_runtime_state",
        }
    ]


@pytest.mark.asyncio
async def test_checkpoint_fallback_uses_previous_version_of_same_checkpoint_key(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_search(query):
        return ("Recovered from previous checkpoint version.", [{"url": "https://example.com/recovered", "title": "Recovered"}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Fallback same checkpoint key", force_new=True, schedule=False)
    plan = response["plan"]
    runtime.store.update_job(
        response["job_id"],
        status="interrupted",
        phase="researching",
        current_checkpoint="researching-u1",
    )
    first = runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-u1",
        state={
            "completed_unit_ids": [],
            "unit_results": {},
            "sources": [],
            "evidence_items": [],
            "sections": [],
            "plan": plan,
        },
    )
    second = runtime.store.save_checkpoint(
        response["job_id"],
        phase="researching",
        checkpoint_key="researching-u1",
        state={"plan": {"query": "broken"}},
    )

    await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    fallback_event = next(event for event in events["events"] if event["type"] == "checkpoint_fallback")

    assert first.checkpoint_seq < second.checkpoint_seq
    assert fallback_event["data"]["fallback_from"] == "researching-u1"
    assert fallback_event["data"]["fallback_to"] == "researching-u1"
    assert fallback_event["data"]["invalid_checkpoint_keys"] == ["researching-u1"]
    assert fallback_event["data"]["invalid_checkpoints"] == [
        {
            "checkpoint_key": "researching-u1",
            "checkpoint_seq": second.checkpoint_seq,
            "reason": "missing_runtime_state",
        }
    ]


@pytest.mark.asyncio
async def test_continue_uses_latest_usable_checkpoint_when_latest_is_broken(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Interrupted source job",
        request_fingerprint="fp-interrupted-source",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(original.job_id, current_checkpoint="researching-bad")
    runtime.store.save_checkpoint(
        original.job_id,
        phase="researching",
        checkpoint_key="researching-good",
        state={
            "plan": structured_plan_payload(original, {"mode": "fresh", "source_job_id": "", "previous_summary": "", "prior_plan_summary": "", "source_count": 0}),
            "sources": [{"source_id": "R1", "url": "https://example.com/checkpoint"}],
            "sections": [],
            "unit_results": {},
            "evidence_items": [],
        },
    )
    runtime.store.save_checkpoint(
        original.job_id,
        phase="researching",
        checkpoint_key="researching-bad",
        state={"plan": {"query": "broken"}},
    )

    response = await runtime.start(
        query="Continue from usable checkpoint",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation["checkpoint_key"] == "researching-good"
    assert continuation["source_count"] == 1
    assert "carry_forward_sources" not in continuation
    assert continuation_payload["carry_forward_sources"][0]["source_id"] == "R1"


@pytest.mark.asyncio
async def test_continuation_plan_stays_compact_while_artifact_keeps_full_state(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Original compact continuation source",
        request_fingerprint="fp-compact-continuation",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Previous summary.",
                "sections": [{"section_id": "s1", "title": "Section", "claims": [{"text": "Claim", "citations": ["R1"]}]}],
                "unit_results": {"unit-1": {"summary": "Summary", "detail": "Detail", "source_ids": ["R1"], "citations": ["R1"]}},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://example.com/source", "title": "Example"}]),
        "application/json",
    )

    response = await runtime.start(
        query="Continue the same line of research",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan_continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert {
        "mode",
        "source_job_id",
        "source_job_status",
        "continuation_identity",
        "compaction_policy",
        "compaction_reason_codes",
        "previous_summary",
        "prior_plan_summary",
        "continuation_goal",
        "source_count",
        "checkpoint_key",
    }.issubset(plan_continuation)
    assert plan_continuation["state_version"] >= 2
    assert plan_continuation["compaction_policy"] == "focused_snapshot_v1"
    assert "current_artifacts" in plan_continuation["compaction_reason_codes"]
    assert plan_continuation["confirmed_claims"] == ["Claim"]
    assert plan_continuation["carry_forward_constraints"] == {
        "include_domains": [],
        "exclude_domains": [],
        "preferred_sources": [],
        "allowed_sources": [],
    }
    assert continuation_payload["carry_forward_sources"][0]["source_id"] == "R1"
    assert continuation_payload["carry_forward_sections"][0]["title"] == "Section"
    assert continuation_payload["carry_forward_unit_results"]["unit-1"]["source_ids"] == ["R1"]


@pytest.mark.asyncio
async def test_continuation_query_rewrite_does_not_repeat_technical_terms(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume semantics in migration runtimes",
        request_fingerprint="fp-rewrite-safety",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume should stay in runtime semantics.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )

    async def planner_with_repeated_resume(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Follow up on migration and resume tradeoffs",
                "reason": "Continue the same technical topic.",
            }
        ]
        payload["search_strategy"]["search_queries"] = [
            "Follow up on migration and resume tradeoffs",
            "Follow up on migration and resume tradeoffs",
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Follow-up search",
                "goal": "Follow up on migration and resume tradeoffs",
                "query": "Follow up on migration and resume tradeoffs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = planner_with_repeated_resume

    response = await runtime.start(
        query="Follow up on migration and resume tradeoffs",
        context="Stay in runtime recovery semantics.",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan_json = json.dumps(response["plan"]).lower()
    assert "checkpoint checkpoint" not in plan_json
    assert "checkpoint checkpoint checkpoint" not in plan_json


@pytest.mark.asyncio
async def test_continuation_query_rewrite_avoids_generic_workflow_language(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        request_fingerprint="fp-continuation-language",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume semantics and restart trade-offs were already covered once.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Continue from previous findings with focus on operational trade-offs and restart risk",
                "reason": "Extend the prior research.",
            }
        ]
        payload["search_strategy"]["search_queries"] = [
            "Continue from previous findings with focus on operational trade-offs and restart risk"
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Continue from previous findings",
                "goal": "Continue from previous findings with focus on operational trade-offs and restart risk",
                "query": "Continue from previous findings with focus on operational trade-offs and restart risk",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = planner

    response = await runtime.start(
        query="Continue from previous findings with focus on operational trade-offs and restart risk",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan_json = json.dumps(response["plan"]).lower()

    assert "continuation workflow" not in plan_json
    assert "checkpoint resume" in plan_json


@pytest.mark.asyncio
async def test_continuation_plan_repairs_duplicate_sub_questions_and_generic_outline(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume semantics in migration runtimes",
        request_fingerprint="fp-plan-repair",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume has already been covered once.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )

    async def low_quality_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Follow up on migration and resume tradeoffs",
                "reason": "Same question twice.",
            },
            {
                "id": "sq2",
                "question": "Follow up on migration and resume tradeoffs",
                "reason": "Same question twice.",
            },
        ]
        payload["report_outline"] = [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
            {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
        ]
        return payload

    runtime._generate_plan_with_model = low_quality_planner

    response = await runtime.start(
        query="Follow up on migration and resume tradeoffs",
        context="Stay in migration runtime semantics.",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    sub_questions = response["plan"]["sub_questions"]
    outline_titles = [item["title"] for item in response["plan"]["report_outline"]]
    validation = response["plan"]["planner_metadata"]["validation"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert len(sub_questions) >= 1
    assert outline_titles[0] == "Executive Summary"
    assert "Key Findings" not in outline_titles
    assert "Open Questions" not in outline_titles
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is False
    assert validation["repaired"] is True
    assert "duplicate_sub_questions" in validation["issues"]
    assert "generic_continuation_outline" in validation["issues"]
    assert "expanded_outline_from_follow_up_surface" in response["plan"]["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_continuation_plan_repairs_string_outline_before_generic_outline_detection(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume semantics in migration runtimes",
        request_fingerprint="fp-string-outline-repair",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume has already been covered once.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )

    async def string_outline_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            "Executive Summary",
            "Key Findings",
            "Open Questions",
        ]
        return payload

    runtime._generate_plan_with_model = string_outline_planner

    response = await runtime.start(
        query="Follow up on migration and resume tradeoffs",
        context="Stay in migration runtime semantics.",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline_titles = [item["title"] for item in response["plan"]["report_outline"]]
    validation = response["plan"]["planner_metadata"]["validation"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert outline_titles[0] == "Executive Summary"
    assert "Key Findings" not in outline_titles
    assert "Open Questions" not in outline_titles
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is False
    assert validation["repaired"] is True
    assert "string_report_outline_items" in validation["issues"]
    assert "generic_continuation_outline" in validation["issues"]
    assert "expanded_outline_from_follow_up_surface" in response["plan"]["planner_metadata"]["trace"]["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_string_shaped_sub_questions_and_outline(tmp_path):
    runtime = build_runtime(tmp_path)

    async def stringy_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            "Compare resume behavior",
            "Compare restart behavior",
        ]
        payload["report_outline"] = [
            "Executive Summary",
            "Operational Impact",
        ]
        payload["research_units"] = []
        return payload

    runtime._generate_plan_with_model = stringy_planner

    response = await runtime.start(
        query="Repair string plan fields",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    validation = response["plan"]["planner_metadata"]["validation"]

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert response["plan"]["sub_questions"][0]["question"] == "Repair string plan fields"
    assert response["plan"]["report_outline"][0]["title"] == "Executive Summary"
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert validation["repaired"] is True
    assert "string_sub_question_items" in validation["issues"]
    assert "string_report_outline_items" in validation["issues"]


@pytest.mark.asyncio
async def test_selective_fetch_prefers_official_docs_over_community_pages(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "checkpoint-docs",
                "title": "Checkpoint Docs",
                "goal": "Use official runtime checkpoint documentation.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Checkpoint search",
                "goal": "Find checkpoint runtime documentation.",
                "query": "checkpoint runtime docs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint runtime docs"],
            "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
        }
        return payload

    async def fake_search(query):
        return (
            "Checkpoint runtime docs comparison.",
            [
                {
                    "url": "https://stackoverflow.com/questions/123/checkpoint-runtime",
                    "title": "Official checkpoint runtime docs guide",
                    "description": "A community discussion.",
                },
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime guide",
                    "description": "Official documentation.",
                },
            ],
        )

    async def fake_fetch(url):
        fetched_urls.append(url)
        return "# Fetched\n\nUseful fetched content."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Prefer official docs", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    assert fetched_urls == ["https://docs.example.com/runtime/checkpoints"]


@pytest.mark.asyncio
async def test_selective_fetch_prefers_official_docs_when_community_title_matches_query_better(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "checkpoint-docs",
                "title": "Checkpoint Resume Docs",
                "goal": "Use official runtime checkpoint documentation.",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume runtime exact semantics"],
            "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
        }
        return payload

    async def fake_search(query):
        return (
            "Checkpoint resume should rely on primary runtime docs.",
            [
                {
                    "url": "https://community.example.com/checkpoint-resume-runtime-guide",
                    "title": "Checkpoint resume runtime guide and exact restart semantics",
                    "description": "Community post with very query-heavy title.",
                },
                {
                    "url": "https://docs.example.com/runtime/recovery",
                    "title": "Runtime recovery",
                    "description": "Official runtime documentation.",
                },
            ],
        )

    async def fake_fetch(url):
        fetched_urls.append(url)
        if "docs.example.com" in url:
            return "# Runtime recovery\n\nCheckpoint resume continues from the last durable checkpoint."
        return "# Community guide\n\nCommunity speculation."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Prefer official docs over catchy community titles", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == ["https://docs.example.com/runtime/recovery"]
    assert "Checkpoint resume continues from the last durable checkpoint" in result["final_report"]
    assert "Community speculation" not in result["final_report"]


@pytest.mark.asyncio
async def test_noisy_fetched_shell_text_does_not_enter_final_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def fake_search(query):
        return (
            "Checkpoint resume is the main finding.",
            [
                {
                    "url": "https://stackoverflow.com/questions/123/checkpoint-runtime",
                    "title": "Checkpoint runtime docs question",
                    "description": "Community discussion.",
                }
            ],
        )

    async def noisy_fetch(url):
        return """### current community

### your communities

Communities for your favorite technologies.
Stack Overflow for Teams is now called Stack Internal.
Sign up or log in.

# AWS DMS difference between resume and restart
Resume continues from the last checkpoint.
"""

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", noisy_fetch)

    response = await runtime.start(query="Filter noisy fetch shell", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert "Communities for your favorite technologies" not in result["final_report"]
    assert "Stack Overflow for Teams is now called" not in result["final_report"]
    assert "Resume continues from the last checkpoint" in result["final_report"]


@pytest.mark.asyncio
async def test_search_unit_preserves_search_evidence_alongside_fetched_evidence(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "checkpoint-analysis",
                "title": "Checkpoint Analysis",
                "goal": "Compare abstract search findings with fetched document details.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def fake_search(query):
        return (
            "Search synthesis: checkpoint resume preserves prior progress while restart replays work from scratch.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoint guide",
                    "description": "Official checkpoint documentation.",
                }
            ],
        )

    async def fake_fetch(url):
        return "# Runtime checkpoint guide\n\nFetched details: RecoveryCheckpoint is reused when resume-processing continues from the last durable point."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Preserve search evidence", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_claim = result["report"]["sections"][0]["claims"][0]

    assert "preserves prior progress" in result["report"]["unit_results"]["unit-search-1"]["summary"]
    assert "RecoveryCheckpoint is reused" in first_claim["text"]
    assert "evidence-unit-search-1-fetch-1" in first_claim["evidence_ids"]


@pytest.mark.asyncio
async def test_fetch_and_map_units_skip_placeholder_claims_when_empty(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "runtime-evidence",
                "title": "Runtime Evidence",
                "goal": "Collect concrete runtime evidence.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Fetch docs page",
                "goal": "Fetch a docs page.",
                "url": "https://docs.example.com/runtime",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-map-1",
                "unit_type": "map",
                "title": "Map docs section",
                "goal": "Map docs pages.",
                "url": "https://docs.example.com",
                "instructions": "Only docs pages.",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": [],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def empty_fetch(url):
        return None

    async def empty_map(url, instructions=""):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", empty_fetch)
    monkeypatch.setattr("grok_search.deep_research_runtime._map_url", empty_map)

    response = await runtime.start(query="Empty fetch/map handling", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    final_report_text = result.get("final_report") or ""
    assert "No content fetched." not in final_report_text
    assert "No site map returned." not in final_report_text
    if not final_report_text:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_empty_fetch_and_map_units_emit_failed_events_and_degrade_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "runtime-evidence",
                "title": "Runtime Evidence",
                "goal": "Collect concrete runtime evidence.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Fetch docs page",
                "goal": "Fetch a docs page.",
                "url": "https://docs.example.com/runtime",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-map-1",
                "unit_type": "map",
                "title": "Map docs section",
                "goal": "Map docs pages.",
                "url": "https://docs.example.com",
                "instructions": "Only docs pages.",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": [],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def empty_fetch(url):
        return None

    async def empty_map(url, instructions=""):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", empty_fetch)
    monkeypatch.setattr("grok_search.deep_research_runtime._map_url", empty_map)

    response = await runtime.start(query="Empty fetch/map failure visibility", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert result["status"] == "failed"
    assert {event["type"] for event in events["events"]} >= {"research_unit_failed", "job_failed"}
    assert not any(event["type"] == "research_unit_completed" for event in events["events"])
    expected_failed_units = [
        {"reason": "empty_fetch_result", "unit_id": "unit-fetch-1", "unit_type": "fetch"},
        {"reason": "empty_map_result", "unit_id": "unit-map-1", "unit_type": "map"},
    ]
    if isinstance(result.get("report"), dict):
        assert result["report"]["status"] == "failed"
        assert result["report"]["runtime"]["failed_units"] == expected_failed_units
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_failed_fetch_dependency_skips_downstream_units_instead_of_blocking_job(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "runtime-evidence",
                "title": "Runtime Evidence",
                "goal": "Collect concrete runtime evidence.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Fetch docs page",
                "goal": "Fetch a docs page.",
                "url": "https://docs.example.com/runtime",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Dependent search",
                "goal": "Analyze the fetched page.",
                "query": "dependent runtime query",
                "depends_on": ["unit-fetch-1"],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["dependent runtime query"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def empty_fetch(url):
        return None

    async def unexpected_search(query):
        raise AssertionError("dependent search should be skipped when upstream fetch fails")

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", empty_fetch)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", unexpected_search)

    response = await runtime.start(query="Failed fetch dependency handling", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert result["status"] == "failed"
    assert any(event["type"] == "research_unit_failed" and event["message"] == "Failed unit-fetch-1." for event in events["events"])
    assert any(event["type"] == "research_unit_skipped" and event["message"] == "Skipped unit-search-2." for event in events["events"])
    expected_skipped_units = [
        {"reason": "dependency_failed", "unit_id": "unit-search-2", "unit_type": "search"}
    ]
    if isinstance(result.get("report"), dict):
        assert result["report"]["status"] == "failed"
        assert result["report"]["runtime"]["skipped_units"] == expected_skipped_units
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_map_unit_fetches_discovered_urls_into_final_evidence(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "mapped-docs",
                "title": "Mapped Docs",
                "goal": "Use mapped runtime docs pages as concrete evidence for resume and restart semantics.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-map-1",
                "unit_type": "map",
                "title": "Map runtime docs",
                "goal": "Discover runtime docs.",
                "url": "https://docs.example.com/runtime",
                "instructions": "Find docs about resume and restart semantics.",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def fake_map(url, instructions=""):
        return """
- https://docs.example.com/runtime/resume
- https://docs.example.com/runtime/restart
- https://blog.example.com/runtime/summary
"""

    async def fake_fetch(url):
        fetched_urls.append(url)
        if "blog.example.com" in url:
            raise AssertionError("off-domain map candidates must not be fetched")
        if url.endswith("/resume"):
            return "# Resume docs\n\nResume-processing continues from the last checkpoint."
        return "# Restart docs\n\nRestart replays the task from a fresh starting point."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._map_url", fake_map)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Map then fetch docs", force_new=True, schedule=False)
    runtime.store.update_job(response["job_id"], include_domains=["docs.example.com"])
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == [
        "https://docs.example.com/runtime/resume",
        "https://docs.example.com/runtime/restart",
    ]
    assert result["status"] in {"completed", "failed"}


@pytest.mark.asyncio
async def test_map_then_fetch_prefers_ranked_topic_match_over_first_url(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "mapped-docs",
                "title": "DMS Recovery Semantics",
                "goal": "Use mapped runtime docs pages as concrete evidence for AWS DMS restart and resume semantics.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-map-1",
                "unit_type": "map",
                "title": "Map runtime docs",
                "goal": "Discover runtime docs.",
                "url": "https://docs.example.com/runtime",
                "instructions": "Find docs about resume and restart semantics.",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def fake_map(url, instructions=""):
        return """
- https://docs.example.com/runtime/flink-restart
- https://docs.example.com/runtime/dms-restart
"""

    async def fake_fetch(url):
        fetched_urls.append(url)
        if url.endswith("/flink-restart"):
            return "# Restart a Flink job\n\nFlink restart restores a job graph from a savepoint."
        return "# AWS DMS restart\n\nResume-processing continues from the last checkpoint while reload-target restarts from a fresh load."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._map_url", fake_map)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Map then fetch ranked docs", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == ["https://docs.example.com/runtime/dms-restart"]
    assert result["status"] in {"completed", "failed"}


@pytest.mark.asyncio
async def test_continuation_start_reuses_recent_completed_follow_up_job(tmp_path):
    runtime = build_runtime(tmp_path)
    original = create_completed_source_job(runtime, query="Original research")
    runtime.write_artifact_batch(
        original.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Checkpoint resume summary.",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nCheckpoint resume summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Original research",
        ),
    )
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    follow_up = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        continue_from_job_id=original.job_id,
        force_new=True,
        schedule=False,
    )
    runtime.store.update_job(follow_up["job_id"], status="completed", phase="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact_batch(
        follow_up["job_id"],
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Checkpoint resume summary.",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nCheckpoint resume summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Follow up query",
        ),
    )

    response = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        continue_from_job_id=original.job_id,
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is True
    assert response["job_id"] == follow_up["job_id"]


@pytest.mark.asyncio
async def test_continuation_reuse_is_invalidated_when_source_final_batch_changes(tmp_path):
    runtime = build_runtime(tmp_path)
    original = create_completed_source_job(runtime, query="Source batch identity")
    runtime.write_artifact_batch(
        original.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Initial batch summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nInitial batch summary.",
                "content_type": "text/markdown",
            },
        ],
    )
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    first = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        continue_from_job_id=original.job_id,
        force_new=True,
        schedule=False,
    )
    runtime.store.update_job(first["job_id"], status="completed", phase="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact_batch(
        first["job_id"],
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Follow-up summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nFollow-up summary.",
                "content_type": "text/markdown",
            },
        ],
    )
    runtime.write_artifact(
        first["job_id"],
        "continuation.json",
        json.dumps({"mode": "continue", "source_job_id": original.job_id, "continuation_identity": "stale-identity"}),
        "application/json",
    )

    runtime.write_artifact_batch(
        original.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R2", "url": "https://docs.example.com/runtime/restart"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R2": {"source_id": "R2", "url": "https://docs.example.com/runtime/restart"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Updated source batch summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nUpdated source batch summary.",
                "content_type": "text/markdown",
            },
        ],
    )

    second = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        continue_from_job_id=original.job_id,
        force_new=False,
        schedule=False,
    )

    assert second["reused"] is False
    assert second["job_id"] != first["job_id"]


@pytest.mark.asyncio
async def test_reused_start_payload_surfaces_resolved_artifact_diagnostics(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Reuse diagnostics source")
    runtime.write_artifact_batch(
        source.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Reusable summary.",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nReusable summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Reuse diagnostics source",
        ),
    )
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    first = await runtime.start(
        query="Reuse diagnostics follow-up",
        continue_from_job_id=source.job_id,
        force_new=True,
        schedule=False,
    )
    runtime.store.update_job(first["job_id"], status="completed", phase="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact_batch(
        first["job_id"],
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Reusable follow-up summary.",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nReusable follow-up summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Reuse diagnostics follow-up",
        ),
    )
    runtime.write_artifact(first["job_id"], "sources.json", json.dumps([{"source_id": "R9", "url": "https://stale.example.com"}]), "application/json")

    reused = await runtime.start(
        query="Reuse diagnostics follow-up",
        continue_from_job_id=source.job_id,
        force_new=False,
        schedule=False,
    )

    assert reused["reused"] is True
    assert reused["resolved_artifact_batch_id"]
    assert reused["artifact_fallback_used"] is True


@pytest.mark.asyncio
async def test_start_does_not_reuse_completed_job_with_incomplete_final_artifact_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    fingerprint = runtime._request_fingerprint(
        query="Follow up query",
        context="Stay technical.",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    completed = runtime.store.create_job(
        query="Follow up query",
        request_fingerprint=fingerprint,
        status="completed",
        phase="finalizing",
        effort="standard",
        context="Stay technical.",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(completed.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        completed.job_id,
        "final_report.md",
        "# Final Report\n\nStale final report.",
        "text/markdown",
    )

    response = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is False
    assert response["job_id"] != completed.job_id


@pytest.mark.asyncio
async def test_result_returns_artifact_errors_instead_of_raising_for_corrupt_json(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Corrupt artifact handling",
        request_fingerprint="fp-corrupt-artifacts",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", "{bad-json", "application/json")
    runtime.write_artifact(job.job_id, "report.json", "{bad-json", "application/json")
    runtime.write_artifact(job.job_id, "sources.json", "{bad-json", "application/json")
    runtime.write_artifact(job.job_id, "citations.json", "{bad-json", "application/json")

    result = await runtime.result(job.job_id)

    assert result["plan"] is None
    assert result["report"] is None
    assert result["sources"] is None
    assert result["citations"] is None
    assert result["artifact_errors"] == {
        "plan.json": "invalid_json",
        "report.json": "invalid_json",
        "sources.json": "invalid_json",
        "citations.json": "invalid_json",
        "final_report.md": "missing_required_artifact",
        "evidence_items.json": "missing_required_artifact",
        "coverage.json": "missing_required_artifact",
        "grounding.json": "missing_required_artifact",
        "verifier.json": "missing_required_artifact",
    }


@pytest.mark.asyncio
async def test_result_prefers_resolved_final_batch_over_current_mixed_artifacts(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Mixed final artifacts",
        request_fingerprint="fp-mixed-final-artifacts",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Mixed final artifacts"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Good report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nGood report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Mixed final artifacts",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://stale.example.com"}]),
        "application/json",
    )

    result = await runtime.result(job.job_id)

    assert result["sources"][0]["url"] == "https://good.example.com"
    assert result["artifact_errors"] == {}
    assert result["resolved_artifact_batch_id"]
    assert any(
        artifact["kind"] == "sources.json" and "batches" in artifact["path"] and "good.example.com" not in artifact["path"]
        for artifact in result["artifacts"]
    )


@pytest.mark.asyncio
async def test_result_prefers_resolved_final_batch_from_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    fixture = load_deep_research_fixture("round13_resolved_final_batch.json")
    job = runtime.store.create_job(**fixture["job"])
    runtime.write_artifact(job.job_id, "plan.json", json.dumps(fixture["plan"]), "application/json")

    persisted_batches = []
    for batch in fixture["batches"]:
        artifacts = []
        for kind, content in batch.items():
            serialized = content if isinstance(content, str) else json.dumps(content)
            content_type = "text/markdown" if kind.endswith(".md") else "application/json"
            artifacts.append({"kind": kind, "content": serialized, "content_type": content_type})
        evidence_items = batch.get("evidence_items.json", [])
        artifacts = with_minimal_provenance_artifacts(
            artifacts,
            query=fixture["job"]["query"],
            evidence_items=evidence_items,
        )
        persisted_batches.append(runtime.write_artifact_batch(job.job_id, artifacts))

    for kind, content in fixture.get("current_artifacts", {}).items():
        runtime.write_artifact(job.job_id, kind, json.dumps(content), "application/json")

    result = await runtime.result(job.job_id)

    assert result["report"]["summary"] == fixture["expected"]["resolved_summary"]
    assert result["sources"][0]["url"] == fixture["expected"]["resolved_source_url"]
    assert result["artifact_fallback_used"] is fixture["expected"]["artifact_fallback_used"]
    assert result["resolved_artifact_batch_id"] == persisted_batches[0][0]["metadata"]["batch_id"]


def test_batch_bundle_candidates_prefer_manifest_rank_over_directory_mtime(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Manifest-ranked batches",
        request_fingerprint="fp-manifest-ranked-batches",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    batches_dir = runtime.store.artifacts_dir / job.job_id / "batches"
    older_dir = batches_dir / "batch-older"
    newer_dir = batches_dir / "batch-newer"
    older_dir.mkdir(parents=True, exist_ok=True)
    newer_dir.mkdir(parents=True, exist_ok=True)
    (older_dir / "manifest.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-older",
                "attempt_id": "attempt-3",
                "attempt_count": 3,
                "phase": "finalizing",
                "created_at": "2026-04-23T00:00:00Z",
                "artifact_kinds": ["report.json"],
                "completeness_ok": True,
            }
        ),
        encoding="utf-8",
    )
    (newer_dir / "manifest.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-newer",
                "attempt_id": "attempt-2",
                "attempt_count": 2,
                "phase": "finalizing",
                "created_at": "2026-04-23T00:05:00Z",
                "artifact_kinds": ["report.json"],
                "completeness_ok": False,
            }
        ),
        encoding="utf-8",
    )
    os.utime(older_dir, (1, 1))
    os.utime(newer_dir, (2, 2))

    candidates = deep_research_runtime_module._batch_bundle_candidates(runtime.store, job.job_id)

    assert [candidate["batch_id"] for candidate in candidates[:2]] == ["batch-older", "batch-newer"]


@pytest.mark.asyncio
async def test_result_falls_back_to_older_usable_final_batch_when_latest_complete_batch_is_invalid(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Older usable batch fallback",
        request_fingerprint="fp-older-usable-batch-fallback",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Older usable batch fallback"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Older good report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nOlder good report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Older usable batch fallback",
        ),
    )
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R2", "url": "https://bad.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R2": {"source_id": "R2", "url": "https://bad.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": "{bad-json",
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nBad report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Older usable batch fallback",
        ),
    )

    result = await runtime.result(job.job_id)

    assert result["report"]["summary"] == "Older good report"
    assert result["sources"][0]["url"] == "https://good.example.com"
    assert result["artifact_errors"] == {}


@pytest.mark.asyncio
async def test_result_surfaces_invalid_shape_errors_for_sources_and_report(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Invalid shape artifact handling",
        request_fingerprint="fp-invalid-shape-artifacts",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Invalid shape artifact handling"}), "application/json")
    runtime.write_artifact(job.job_id, "report.json", json.dumps(["not", "an", "object"]), "application/json")
    runtime.write_artifact(job.job_id, "sources.json", json.dumps({"url": "https://example.com"}), "application/json")
    runtime.write_artifact(job.job_id, "citations.json", json.dumps({"source_registry": {}, "sections": []}), "application/json")

    result = await runtime.result(job.job_id)

    assert result["report"] is None
    assert result["sources"] is None
    assert result["artifact_errors"]["report.json"] == "invalid_shape"
    assert result["artifact_errors"]["sources.json"] == "invalid_shape"


@pytest.mark.asyncio
async def test_result_surfaces_invalid_nested_claim_shape_errors(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Nested invalid shape",
        request_fingerprint="fp-nested-invalid-shape",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Nested invalid shape"}), "application/json")
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime"}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "citations.json",
        json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime"}}, "sections": [{"section_id": "s1", "claims": ["bad-claim"]}]}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps({"summary": "Bad nested report", "sections": [{"section_id": "s1", "claims": ["bad-claim"]}], "unit_results": {}}),
        "application/json",
    )
    runtime.write_artifact(job.job_id, "final_report.md", "# Final Report\n\nBad nested report.", "text/markdown")

    result = await runtime.result(job.job_id)

    assert result["artifact_errors"]["report.json"] == "invalid_shape"
    assert result["artifact_errors"]["citations.json"] == "invalid_shape"


@pytest.mark.asyncio
async def test_result_surfaces_invalid_provenance_bundle_for_partially_bound_claim_graph(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Invalid provenance bundle handling",
        request_fingerprint="fp-invalid-provenance-bundle",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Invalid provenance bundle handling"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps(
                        [
                            {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"},
                            {"source_id": "R2", "url": "https://docs.example.com/runtime/state"},
                        ]
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps(
                        {
                            "source_registry": {
                                "R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"},
                                "R2": {"source_id": "R2", "url": "https://docs.example.com/runtime/state"},
                            },
                            "sections": [
                                {
                                    "section_id": "resume-semantics",
                                    "title": "Resume Semantics",
                                    "summary": "Resume continues from the last checkpoint and preserves downstream state.",
                                    "claims": [
                                        {
                                            "claim_id": "resume-semantics-claim-1",
                                            "text": "Resume continues from the last checkpoint and preserves downstream state.",
                                            "citations": ["R1", "R2"],
                                            "evidence_ids": ["e1", "e2"],
                                            "evidence_bindings": [
                                                {
                                                    "evidence_id": "e1",
                                                    "source_id": "R1",
                                                    "source_backed": True,
                                                    "line_start": 3,
                                                    "line_end": 4,
                                                }
                                            ],
                                        }
                                    ],
                                    "citations": ["R1", "R2"],
                                }
                            ],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps(
                        {
                            "summary": "Resume continues from the last checkpoint and preserves downstream state.",
                            "sections": [
                                {
                                    "section_id": "resume-semantics",
                                    "title": "Resume Semantics",
                                    "summary": "Resume continues from the last checkpoint and preserves downstream state.",
                                    "claims": [
                                        {
                                            "claim_id": "resume-semantics-claim-1",
                                            "text": "Resume continues from the last checkpoint and preserves downstream state.",
                                            "citations": ["R1", "R2"],
                                            "evidence_ids": ["e1", "e2"],
                                            "evidence_bindings": [
                                                {
                                                    "evidence_id": "e1",
                                                    "source_id": "R1",
                                                    "source_backed": True,
                                                    "line_start": 3,
                                                    "line_end": 4,
                                                }
                                            ],
                                        }
                                    ],
                                    "citations": ["R1", "R2"],
                                }
                            ],
                            "unit_results": {},
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nResume continues from the last checkpoint and preserves downstream state.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Invalid provenance bundle handling",
            evidence_items=[
                {
                    "evidence_id": "e1",
                    "unit_id": "unit-search-1",
                    "summary": "Resume continues from the last checkpoint.",
                    "detail": "Resume continues from the last checkpoint.",
                    "source_ids": ["R1"],
                    "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                    "evidence_kind": "fetch",
                    "line_start": 3,
                    "line_end": 4,
                },
                {
                    "evidence_id": "e2",
                    "unit_id": "unit-search-1",
                    "summary": "Preserves downstream state.",
                    "detail": "Preserves downstream state.",
                    "source_ids": ["R2"],
                    "source_urls": ["https://docs.example.com/runtime/state"],
                    "evidence_kind": "fetch",
                    "line_start": 8,
                    "line_end": 9,
                },
            ],
            total_claims=1,
            section_count=1,
        ),
    )

    result = await runtime.result(job.job_id)

    assert result["resolved_artifact_batch_id"] == ""
    assert result["artifact_errors"]["report.json"] in {"missing_required_artifact", "invalid_provenance_bundle"}
    if result["report"] is None:
        assert result["sources"] is None
        assert result["final_report"] is None
    assert "report.json" in result["artifact_errors"]


@pytest.mark.asyncio
async def test_status_exposes_resolved_artifact_batch_when_current_artifacts_are_mixed(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    job = runtime.store.create_job(
        query="Artifact status fallback",
        request_fingerprint="fp-artifact-status-fallback",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {"kind": "sources.json", "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]), "content_type": "application/json"},
            {"kind": "citations.json", "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}), "content_type": "application/json"},
            {"kind": "report.json", "content": json.dumps({"summary": "Good batch summary", "sections": [], "unit_results": {}}), "content_type": "application/json"},
            {"kind": "final_report.md", "content": "# Final Report\n\nGood batch summary.", "content_type": "text/markdown"},
            ],
            query="Artifact status fallback",
        ),
    )
    runtime.write_artifact(job.job_id, "sources.json", json.dumps([{"source_id": "R999", "url": "https://stale.example.com"}]), "application/json")

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert status["artifact_fallback_used"] is True
    assert status["resolved_artifact_batch_id"]
    assert result["artifact_fallback_used"] is True
    assert result["sources"][0]["url"] == "https://good.example.com"


@pytest.mark.asyncio
async def test_interrupted_finalizing_job_reads_resolved_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Interrupted finalizing visibility",
        request_fingerprint="fp-interrupted-finalizing-visibility",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted finalizing visibility"}), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Interrupted finalizing visibility",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps({"summary": "stale current report", "sections": [], "unit_results": {}}),
        "application/json",
    )

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)
    report_text = runtime.read_artifact_text(job.job_id, "final_report.md")
    batch_id = next(artifact["metadata"]["batch_id"] for artifact in persisted if artifact["kind"] == "report.json")

    assert status["resolved_artifact_batch_id"] == batch_id
    assert result["report"]["summary"] == "Recovered final report"
    assert report_text == "# Final Report\n\nRecovered final report.\n"


@pytest.mark.asyncio
async def test_canceled_finalizing_job_prefers_resolved_final_batch_and_exposes_evidence_artifact(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Canceled finalizing visibility",
        request_fingerprint="fp-canceled-finalizing-visibility",
        status="canceled",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        cancel_requested=True,
        current_checkpoint="finalizing",
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps({"query": "Canceled finalizing visibility"}),
        "application/json",
    )
    evidence_items = [
        {
            "evidence_id": "evidence-unit-search-1-fetch",
            "unit_id": "unit-search-1",
            "summary": "Recovered final batch evidence.",
            "detail": "Recovered final batch evidence.",
            "source_ids": ["R1"],
            "source_urls": ["https://good.example.com/runtime/recovery"],
            "evidence_kind": "fetch",
            "derived_from_source_url": "https://good.example.com/runtime/recovery",
            "line_start": 3,
            "line_end": 4,
        }
    ]
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {
                                "source_id": "R1",
                                "url": "https://good.example.com/runtime/recovery",
                            }
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps(evidence_items),
                "content_type": "application/json",
            },
            ],
            query="Canceled finalizing visibility",
            evidence_items=evidence_items,
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://stale.example.com"}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps({"summary": "stale current report", "sections": [], "unit_results": {}}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "evidence_items.json",
        json.dumps(
            [
                {
                    "evidence_id": "evidence-stale-current",
                    "unit_id": "unit-search-stale",
                    "summary": "stale current evidence",
                    "detail": "stale current evidence",
                    "source_ids": ["R9"],
                    "source_urls": ["https://stale.example.com"],
                    "evidence_kind": "search",
                    "line_start": 99,
                    "line_end": 100,
                }
            ]
        ),
        "application/json",
    )

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)
    evidence_text = runtime.read_artifact_text(job.job_id, "evidence_items.json")
    batch_id = persisted[0]["metadata"]["batch_id"]

    assert status["status"] == "canceled"
    assert status["resolved_artifact_batch_id"] == batch_id
    assert status["artifact_fallback_used"] is True
    assert "evidence_items.json" in status["artifact_kinds"]
    assert any(artifact["kind"] == "evidence_items.json" for artifact in status["artifacts"])
    assert result["status"] == "canceled"
    assert result["resolved_artifact_batch_id"] == batch_id
    assert result["artifact_fallback_used"] is True
    assert result["report"]["summary"] == "Recovered final batch report"
    assert result["sources"][0]["url"] == "https://good.example.com/runtime/recovery"
    assert any(artifact["kind"] == "evidence_items.json" for artifact in result["artifacts"])
    assert json.loads(evidence_text or "[]") == evidence_items


@pytest.mark.asyncio
async def test_continue_from_canceled_job_with_resolved_final_batch_builds_continuation_state(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Canceled continuation source")
    runtime.store.update_job(
        source.job_id,
        status="canceled",
        cancel_requested=True,
        current_checkpoint="finalizing",
        finished_at=utc_now_iso(),
    )

    response = await runtime.start(
        query="Follow up canceled continuation source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    assert response["plan"]["continuation"]["mode"] == "continue"
    assert response["plan"]["continuation"]["source_job_id"] == source.job_id
    assert response["plan"]["continuation"]["source_job_status"] == "canceled"
    assert response["plan"]["continuation"]["source_count"] == 1


@pytest.mark.asyncio
async def test_continue_from_interrupted_finalizing_job_with_resolved_final_batch_builds_continuation_state(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Interrupted finalizing continuation source")
    runtime.store.update_job(
        source.job_id,
        status="interrupted",
        current_checkpoint="finalizing",
        phase="finalizing",
        last_error="worker_restarted",
        finished_at=utc_now_iso(),
    )

    response = await runtime.start(
        query="Follow up interrupted finalizing continuation source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    continuation = response["plan"]["continuation"]

    assert continuation["mode"] == "continue"
    assert continuation["source_job_id"] == source.job_id
    assert continuation["source_job_status"] == "interrupted"
    assert continuation["checkpoint_key"] == "finalizing"
    assert continuation["source_count"] == 1


@pytest.mark.asyncio
async def test_continue_from_canceled_job_without_recoverable_surfaces_is_rejected_without_orphan(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = runtime.store.create_job(
        query="Canceled continuation without artifacts",
        request_fingerprint="fp-canceled-continuation-without-artifacts",
        status="canceled",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(source.job_id, finished_at=utc_now_iso())

    with pytest.raises(ValueError, match="continue_from_job_id source is not recoverable"):
        await runtime.start(
            query="Follow up canceled continuation without artifacts",
            continue_from_job_id=source.job_id,
            plan_only=True,
            force_new=True,
            schedule=False,
        )

    assert [job.job_id for job in runtime.store.list_jobs(limit=20)] == [source.job_id]


def test_completed_continuation_summary_without_plan_or_checkpoint_is_not_recoverable(tmp_path):
    runtime = build_runtime(tmp_path)
    source = runtime.store.create_job(
        query="Completed summary-only source",
        request_fingerprint="fp-completed-summary-only-source",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(source.job_id, finished_at=utc_now_iso())

    continuation = DeepResearchContinuationState(
        mode="continue",
        source_job_id=source.job_id,
        source_job_status="completed",
        previous_summary="Resume continues from the last checkpoint.",
    )

    assert deep_research_runtime_module._continuation_source_is_recoverable(runtime.store, source, continuation) is False


def test_completed_continuation_summary_with_plan_artifact_is_recoverable(tmp_path):
    runtime = build_runtime(tmp_path)
    source = runtime.store.create_job(
        query="Completed summary-plan source",
        request_fingerprint="fp-completed-summary-plan-source",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(source.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(source.job_id, "plan.json", json.dumps({"query": source.query}), "application/json")

    continuation = DeepResearchContinuationState(
        mode="continue",
        source_job_id=source.job_id,
        source_job_status="completed",
        previous_summary="Resume continues from the last checkpoint.",
    )

    assert deep_research_runtime_module._continuation_source_is_recoverable(runtime.store, source, continuation) is True


@pytest.mark.asyncio
async def test_lineage_preserves_root_and_parent_across_multi_hop_continuations(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Multi hop lineage source")

    first_response = await runtime.start(
        query="First follow-up for multi hop lineage",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    first_job = runtime.store.update_job(
        first_response["job_id"],
        status="completed",
        phase="finalizing",
        current_checkpoint="planning",
        finished_at=utc_now_iso(),
    )

    second_response = await runtime.start(
        query="Second follow-up for multi hop lineage",
        continue_from_job_id=first_job.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    first_lineage = json.loads(runtime.store.read_artifact_text(first_job.job_id, "lineage.json") or "{}")
    second_lineage = json.loads(runtime.store.read_artifact_text(second_response["job_id"], "lineage.json") or "{}")
    second_continuation = json.loads(runtime.store.read_artifact_text(second_response["job_id"], "continuation.json") or "{}")

    assert first_lineage["root_job_id"] == source.job_id
    assert first_lineage["parent_job_id"] == source.job_id
    assert first_lineage["supersedes_job_id"] == source.job_id
    assert second_lineage["root_job_id"] == source.job_id
    assert second_lineage["parent_job_id"] == first_job.job_id
    assert second_lineage["supersedes_job_id"] == first_job.job_id
    assert second_continuation["lineage_root_job_id"] == source.job_id
    assert second_continuation["parent_job_id"] == first_job.job_id


@pytest.mark.asyncio
async def test_continue_rejects_dispatch_only_source_without_material_carry_forward_state(tmp_path):
    runtime = build_runtime(tmp_path)
    source = runtime.store.create_job(
        query="Dispatch-only continuation source",
        request_fingerprint="fp-dispatch-only-continuation",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        source.job_id,
        current_checkpoint="researching-dispatch-a1-unit-search-1",
        last_error="worker_restarted",
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="researching",
        checkpoint_key="researching-dispatch-a1-unit-search-1",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": [],
            "sources": [],
            "evidence_items": [],
            "sections": [],
            "unit_results": {},
        },
    )

    with pytest.raises(ValueError, match="continue_from_job_id source is not recoverable"):
        await runtime.start(
            query="Follow up dispatch-only continuation source",
            continue_from_job_id=source.job_id,
            plan_only=True,
            force_new=True,
            schedule=False,
        )


@pytest.mark.asyncio
async def test_startup_reconcile_resolves_worker_restarted_finalizing_job_with_usable_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Startup reconcile resolved final batch",
        request_fingerprint="fp-startup-reconcile-resolved-final-batch",
        status="running",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="finalizing",
        heartbeat_at="2000-01-01T00:00:00Z",
        started_at="2000-01-01T00:00:00Z",
    )
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", job.job_id),
        )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Startup reconcile resolved final batch"}), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Startup reconcile resolved final batch",
        ),
    )

    other_runtime = DeepResearchRuntime(runtime.store.root_dir)
    await other_runtime._ensure_startup_reconciled()

    reconciled = other_runtime.store.get_job(job.job_id)
    events = await other_runtime.events(job.job_id)
    batch_id = persisted[0]["metadata"]["batch_id"]

    assert reconciled.status == "completed"
    assert reconciled.phase == "finalizing"
    assert reconciled.last_error == ""
    assert any(
        event["type"] == "job_resolved_from_final_batch"
        and event["data"]["resolved_artifact_batch_id"] == batch_id
        for event in events["events"]
    )


@pytest.mark.asyncio
async def test_startup_reconcile_is_idempotent_across_reconnects_for_resolved_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Startup reconcile idempotent final batch",
        request_fingerprint="fp-startup-reconcile-idempotent-final-batch",
        status="running",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="finalizing",
        heartbeat_at="2000-01-01T00:00:00Z",
        started_at="2000-01-01T00:00:00Z",
    )
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", job.job_id),
        )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Startup reconcile idempotent final batch"}), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final batch report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Startup reconcile idempotent final batch",
        ),
    )
    batch_id = persisted[0]["metadata"]["batch_id"]

    first_runtime = DeepResearchRuntime(runtime.store.root_dir)
    await first_runtime._ensure_startup_reconciled()
    second_runtime = DeepResearchRuntime(runtime.store.root_dir)
    await second_runtime._ensure_startup_reconciled()

    events = await second_runtime.events(job.job_id)
    resolved_events = [
        event
        for event in events["events"]
        if event["type"] == "job_resolved_from_final_batch"
        and event["data"].get("resolved_artifact_batch_id") == batch_id
    ]

    assert len(resolved_events) == 1


@pytest.mark.asyncio
async def test_startup_reconcile_preserves_canceled_finalizing_job_with_usable_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Startup reconcile canceled final batch",
        request_fingerprint="fp-startup-reconcile-canceled-final-batch",
        status="running",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        cancel_requested=True,
        current_checkpoint="finalizing",
        heartbeat_at="2000-01-01T00:00:00Z",
        started_at="2000-01-01T00:00:00Z",
    )
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", job.job_id),
        )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Startup reconcile canceled final batch"}), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Startup reconcile canceled final batch",
        ),
    )

    other_runtime = DeepResearchRuntime(runtime.store.root_dir)
    await other_runtime._ensure_startup_reconciled()

    reconciled = other_runtime.store.get_job(job.job_id)
    status = await other_runtime.status(job.job_id)
    events = await other_runtime.events(job.job_id)
    batch_id = persisted[0]["metadata"]["batch_id"]

    assert reconciled.status == "canceled"
    assert reconciled.phase == "finalizing"
    assert status["resolved_artifact_batch_id"] == batch_id
    assert any(
        event["type"] == "job_canceled"
        and event["data"]["reason"] == "cancel_requested_during_recovery"
        for event in events["events"]
    )
    assert any(
        event["type"] == "job_resolved_from_final_batch"
        and event["data"]["resolved_artifact_batch_id"] == batch_id
        and event["data"]["resolved_status"] == "canceled"
        for event in events["events"]
    )


@pytest.mark.asyncio
async def test_start_reuses_interrupted_finalizing_job_with_usable_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    fingerprint = runtime._request_fingerprint(
        query="Reuse interrupted final batch",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    job = runtime.store.create_job(
        query="Reuse interrupted final batch",
        request_fingerprint=fingerprint,
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="finalizing",
        finished_at=utc_now_iso(),
        cancel_requested=True,
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Reuse interrupted final batch"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Reuse interrupted final batch",
        ),
    )

    response = await runtime.start(query="Reuse interrupted final batch", force_new=False, schedule=False)

    assert response["reused"] is True
    assert response["job_id"] == job.job_id
    assert response["status"] == "completed"
    assert response["cancel_requested"] is False
    assert runtime.store.get_job(job.job_id).cancel_requested is False


@pytest.mark.asyncio
async def test_resume_interrupted_finalizing_job_with_invalid_final_batch_does_not_short_circuit(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Interrupted finalizing invalid bundle",
        request_fingerprint="fp-interrupted-finalizing-invalid-bundle",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted finalizing invalid bundle"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": "{bad-json",
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nBroken report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Interrupted finalizing invalid bundle",
        ),
    )

    resumed = await runtime.resume(job.job_id, schedule=False)
    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)
    events = await runtime.events(job.job_id)

    assert resumed["status"] == "queued"
    assert runtime.store.get_job(job.job_id).status == "queued"
    assert status["status"] == "queued"
    assert result["status"] == "queued"
    assert resumed["resolved_artifact_batch_id"] == ""
    assert status["resolved_artifact_batch_id"] == ""
    assert result["resolved_artifact_batch_id"] == ""
    assert resumed["artifact_visibility_reason"] == ""
    assert status["artifact_visibility_reason"] == ""
    assert result["artifact_visibility_reason"] == ""
    assert any(event["type"] == "job_resumed" for event in events["events"])
    assert not any(event["type"] == "job_resolved_from_final_batch" for event in events["events"])


@pytest.mark.asyncio
async def test_resume_interrupted_finalizing_job_with_usable_final_batch_short_circuits(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Interrupted finalizing resume",
        request_fingerprint="fp-interrupted-finalizing-resume",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted finalizing resume"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Recovered final report", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Interrupted finalizing resume",
        ),
    )

    resumed = await runtime.resume(job.job_id, schedule=False)
    status = await runtime.status(job.job_id)
    events = await runtime.events(job.job_id)

    assert resumed["status"] == "completed"
    assert status["status"] == "completed"
    assert any(event["type"] == "job_resolved_from_final_batch" for event in events["events"])
    resolved_event = next(event for event in events["events"] if event["type"] == "job_resolved_from_final_batch")
    assert resolved_event["data"]["attempt_id"] == "attempt-1"
    assert not any(event["type"] == "job_completed" for event in events["events"])
    assert runtime.read_artifact_text(job.job_id, "final_report.md") == "# Final Report\n\nRecovered final report.\n"


@pytest.mark.asyncio
async def test_events_window_treats_resolved_final_batch_as_terminal_window(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Resolved final batch events window",
        request_fingerprint="fp-resolved-final-batch-events-window",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Recovered final report", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query=job.query,
        ),
    )

    before_resume = await runtime.events(job.job_id)
    resumed = await runtime.resume(job.job_id, schedule=False)
    replay = await runtime.events(job.job_id, after_seq=before_resume["next_after_seq"])

    assert resumed["status"] == "completed"
    assert [event["type"] for event in replay["events"]] == ["job_resolved_from_final_batch"]
    assert replay["job_terminal"] is True
    assert replay["window_has_terminal_event"] is True


@pytest.mark.asyncio
async def test_start_does_not_reuse_interrupted_finalizing_job_with_empty_final_report(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    fingerprint = runtime._request_fingerprint(
        query="Interrupted finalizing empty markdown",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    job = runtime.store.create_job(
        query="Interrupted finalizing empty markdown",
        request_fingerprint=fingerprint,
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted finalizing empty markdown"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "A meaningful report should exist before reuse.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\n",
                "content_type": "text/markdown",
            },
        ],
    )

    response = await runtime.start(query="Interrupted finalizing empty markdown", force_new=False, schedule=False)

    assert response["reused"] is False
    assert response["job_id"] != job.job_id


@pytest.mark.asyncio
async def test_run_job_executes_independent_units_concurrently(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    started = []
    both_started = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "First",
                "query": "one",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Second",
                "query": "two",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = ["one", "two"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def fake_search(query):
        nonlocal active, max_active
        started.append(query)
        active += 1
        max_active = max(max_active, active)
        if len(started) >= 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        await asyncio.sleep(0.01)
        active -= 1
        return (f"Answer for {query}", [{"url": f"https://example.com/{query}", "title": query}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)
    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "2")

    response = await runtime.start(query="Concurrent runtime", force_new=True, schedule=False)

    await runtime.run_job(response["job_id"])

    assert started == ["one", "two"]
    assert max_active >= 2


@pytest.mark.asyncio
async def test_concurrent_batch_persists_successful_units_before_failure(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    executed = []

    async def fake_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "First",
                "query": "one",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Second",
                "query": "two",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = ["one", "two"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def flaky_search(query):
        executed.append(query)
        if query == "two":
            raise RuntimeError("boom")
        return ("Answer for one", [{"url": "https://example.com/one", "title": "one"}])

    async def recovered_search(query):
        executed.append(f"retry:{query}")
        return (f"Answer for {query}", [{"url": f"https://example.com/{query}", "title": query}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", flaky_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)
    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "2")

    response = await runtime.start(query="Concurrent failure", force_new=True, schedule=False)
    failed = await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert failed["status"] == "failed"
    assert any(event["type"] == "research_unit_completed" and "unit-search-1" in event["message"] for event in events["events"])
    assert any(event["type"] == "research_unit_failed" and "unit-search-2" in event["message"] for event in events["events"])

    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", recovered_search)
    resumed = await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])

    assert resumed["status"] == "queued"
    assert executed.count("one") == 1
    assert "retry:two" in executed


@pytest.mark.asyncio
async def test_invalid_depends_on_uses_structural_salvage_plan(tmp_path):
    runtime = build_runtime(tmp_path)

    async def bad_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["depends_on"] = "unknown-unit"
        return payload

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runtime, "_generate_plan_with_model", bad_planner)
    try:
        response = await runtime.start(query="Bad depends_on", plan_only=True, force_new=True, schedule=False)
    finally:
        monkeypatch.undo()

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["planner"] == "salvage"
    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert plan["research_units"][0]["depends_on"] == []
    assert trace["unsafe_plan"] is True
    assert trace["salvage_used"] is True
    assert "dropped_unknown_dependency:unit-search-1->unknown-unit" in trace["salvage_source_trace"]["normalize_actions"]
    assert "unknown_dependency:unit-search-1->unknown-unit" in trace["salvage_source_trace"]["blocked_reasons"]


@pytest.mark.asyncio
async def test_plan_normalization_repairs_round6c_style_payload_without_fallback(tmp_path):
    runtime = build_runtime(tmp_path)

    async def round6c_style_planner(job, continuation):
        return {
            "brief": "Compare checkpoint, resume, and restart semantics in AWS DMS tasks.",
            "sub_questions": [
                "What are the definitions and mechanics of resume-processing and restart?",
                "How does recovery differ between full load and CDC tasks?",
            ],
            "search_strategy": "Targeted searches on docs.aws.amazon.com only",
            "report_outline": [
                "Executive Summary",
                "Behavior by Task Type",
            ],
            "research_units": [
                {
                    "unit_id": "u1",
                    "unit_type": "search",
                    "title": "Core Task Management and Start/Resume/Restart",
                    "goal": "Extract official definitions and mechanics.",
                    "query": "",
                    "depends_on": ["u6", "missing-unit"],
                    "status": "ready",
                    "notes": "",
                },
                {
                    "unit_id": "u1",
                    "unit_type": "browse_page",
                    "title": "Browse Key Task Page",
                    "goal": "Deep extract on task states and monitoring.",
                    "url": "",
                    "depends_on": ["u1"],
                    "status": "pending",
                    "notes": "Summarize all sections on starting, stopping, resuming, restarting tasks, recovery, and checkpoint mentions.",
                },
            ],
            "planner_metadata": {
                "planner": "model",
                "used_fallback": False,
            },
        }

    runtime._generate_plan_with_model = round6c_style_planner

    response = await runtime.start(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        context="Prefer official docs and focus on recovery behavior and restart trade-offs.",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]
    validation = plan["planner_metadata"]["validation"]

    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert plan["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert "aliased_unit_type:browse_page->fetch" in trace["normalize_actions"]
    assert "aliased_unit_status:ready->pending" in trace["normalize_actions"]
    assert "renamed_duplicate_unit_id:u1->u1-2" in trace["normalize_actions"]
    assert "filled_search_query:u1" in trace["normalize_actions"]
    assert "degraded_fetch_without_url_to_search:u1-2" in trace["normalize_actions"]
    assert "dropped_unknown_dependency:u1->missing-unit" in trace["normalize_actions"]
    assert "dropped_unknown_dependency:u1->u6" in trace["normalize_actions"]
    assert "unknown_dependency:u1->missing-unit" in trace["blocked_reasons"]
    assert "unknown_dependency:u1->u6" in trace["blocked_reasons"]
    assert validation["repaired"] is True
    assert "duplicate_unit_id" in validation["issues"]
    assert "missing_search_query" in validation["issues"]
    assert "missing_fetch_url" in validation["issues"]


@pytest.mark.asyncio
async def test_missing_sub_question_coverage_uses_structural_salvage_plan(tmp_path):
    runtime = build_runtime(tmp_path)

    async def sparse_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Compare checkpoint resume semantics", "reason": "Primary axis."},
            {"id": "sq2", "question": "Compare restart trade-offs", "reason": "Secondary axis."},
            {"id": "sq3", "question": "Explain operational recovery risks", "reason": "Operational axis."},
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Checkpoint resume search",
                "goal": "Compare checkpoint resume semantics",
                "query": "Compare checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = ["Compare checkpoint resume semantics"]
        return payload

    runtime._generate_plan_with_model = sparse_planner

    response = await runtime.start(
        query="Expand planner coverage",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["planner"] == "salvage"
    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["unsafe_plan"] is True
    assert trace["salvage_used"] is True
    assert "Compare checkpoint resume semantics" in trace["salvage_queries"]
    assert "Compare restart trade-offs" in trace["salvage_queries"]
    assert "Explain operational recovery risks" in trace["salvage_queries"]
    assert plan["search_strategy"]["search_queries"] == [
        "Compare checkpoint resume semantics",
        "Compare restart trade-offs",
        "Explain operational recovery risks",
    ]


@pytest.mark.asyncio
async def test_fresh_official_doc_query_safe_repairs_do_not_force_planner_fallback(tmp_path):
    runtime = build_runtime(tmp_path)

    async def sparse_official_docs_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain AWS DMS checkpoint resume semantics.",
                "reason": "Primary axis.",
            },
            {
                "id": "sq2",
                "question": "Explain DescribeReplicationTasks recovery visibility.",
                "reason": "Secondary axis.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "AWS DMS checkpoint semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
                "query": "",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
            },
            {
                "section_id": "task-visibility",
                "title": "Task Visibility",
                "goal": "Explain DescribeReplicationTasks recovery visibility.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = sparse_official_docs_planner

    response = await runtime.start(
        query="Follow up AWS DMS checkpoint resume semantics with official docs only",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["planner"] == "test"
    assert "Explain AWS DMS checkpoint resume semantics." in plan["research_units"][0]["query"]
    auto_unit = next(unit for unit in plan["research_units"] if unit["unit_id"] == "unit-search-auto-1")
    assert auto_unit["goal"] == "Explain DescribeReplicationTasks recovery visibility."
    assert "Explain DescribeReplicationTasks recovery visibility." in auto_unit["query"]
    assert "Follow up AWS DMS checkpoint resume semantics with official docs only" in auto_unit["query"]
    assert "site:docs.aws.amazon.com" in auto_unit["query"]
    assert any(
        "Explain DescribeReplicationTasks recovery visibility." in query
        and "Follow up AWS DMS checkpoint resume semantics with official docs only" in query
        and "site:docs.aws.amazon.com" in query
        for query in plan["search_strategy"]["search_queries"]
    )
    assert "filled_search_query:unit-search-1" in trace["normalize_actions"]
    assert "added_sub_question_search_unit:sq2" in trace["normalize_actions"]
    assert "missing_search_query" in trace["validation_issues"]
    assert "missing_sub_question_unit_coverage" in trace["validation_issues"]
    assert trace["unsafe_plan"] is False


@pytest.mark.asyncio
async def test_true_continuation_official_doc_safe_repairs_do_not_force_planner_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Prior AWS DMS checkpoint resume investigation",
        checkpoint_sources=[
            {
                "source_id": "R1",
                "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_ReplicationTask.html",
                "title": "API_ReplicationTask",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
                "ranking_reasons": ["official_docs", "api_reference"],
            }
        ],
        report_payload={
            "query": "Prior AWS DMS checkpoint resume investigation",
            "summary": "Checkpoint resume continues from the last durable checkpoint.",
            "sections": [
                {
                    "section_id": "executive-summary",
                    "title": "Executive Summary",
                    "summary": "Checkpoint resume continues from the last durable checkpoint.",
                    "claims": [
                        {
                            "claim_id": "executive-summary-claim-1",
                            "text": "Checkpoint resume continues from the last durable checkpoint.",
                            "citations": ["R1"],
                            "unit_id": "unit-search-1",
                            "evidence_ids": ["evidence-unit-search-1-search"],
                            "confidence": "medium",
                        }
                    ],
                    "citations": ["R1"],
                    "confidence": "medium",
                }
            ],
            "coverage": {
                "uncovered_sub_questions": [
                    "Investigate restart trade-offs after interruption",
                ],
                "unanswered_sections": ["Task Visibility"],
            },
            "unit_results": {},
        },
    )

    async def sparse_official_docs_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain AWS DMS checkpoint resume semantics.",
                "reason": "Primary axis.",
            },
            {
                "id": "sq2",
                "question": "Explain DescribeReplicationTasks recovery visibility.",
                "reason": "Secondary axis.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "AWS DMS checkpoint semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
                "query": "",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
            },
            {
                "section_id": "task-visibility",
                "title": "Task Visibility",
                "goal": "Explain DescribeReplicationTasks recovery visibility.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = sparse_official_docs_planner

    response = await runtime.start(
        query="Continue AWS DMS checkpoint resume follow-up with official docs only",
        continue_from_job_id=source.job_id,
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["planner"] == "test"
    assert plan["continuation"]["mode"] == "continue"
    assert "Explain AWS DMS checkpoint resume semantics." in plan["research_units"][0]["query"]
    assert any(unit["goal"] == "Explain DescribeReplicationTasks recovery visibility." for unit in plan["research_units"])
    assert "filled_search_query:unit-search-1" in trace["normalize_actions"]
    assert "added_sub_question_search_unit:sq2" in trace["normalize_actions"]
    assert "missing_search_query" in trace["validation_issues"]
    assert "missing_sub_question_unit_coverage" in trace["validation_issues"]
    assert trace["unsafe_plan"] is False
    assert plan["brief"]["scope"]["allowed_sources"] == ["docs.aws.amazon.com"]


@pytest.mark.asyncio
async def test_true_continuation_official_doc_unknown_dependency_safe_repair_does_not_force_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Prior AWS DMS checkpoint resume investigation",
        checkpoint_sources=[
            {
                "source_id": "R1",
                "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_ReplicationTask.html",
                "title": "API_ReplicationTask",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
                "ranking_reasons": ["official_docs", "api_reference"],
            }
        ],
        report_payload={
            "query": "Prior AWS DMS checkpoint resume investigation",
            "summary": "Checkpoint resume continues from the last durable checkpoint.",
            "sections": [
                {
                    "section_id": "resume-semantics",
                    "title": "Resume Semantics",
                    "summary": "Checkpoint resume continues from the last durable checkpoint.",
                    "claims": [
                        {
                            "claim_id": "resume-semantics-claim-1",
                            "text": "Checkpoint resume continues from the last durable checkpoint.",
                            "citations": ["R1"],
                            "unit_id": "unit-search-1",
                            "evidence_ids": ["evidence-unit-search-1-search"],
                            "confidence": "medium",
                        }
                    ],
                    "citations": ["R1"],
                    "confidence": "medium",
                }
            ],
            "coverage": {
                "uncovered_sub_questions": [
                    "Explain DescribeReplicationTasks recovery visibility.",
                ],
                "unanswered_sections": ["Task Visibility"],
            },
            "unit_results": {},
        },
    )

    async def sparse_official_docs_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain AWS DMS checkpoint resume semantics.",
                "reason": "Primary axis.",
            },
            {
                "id": "sq2",
                "question": "Explain DescribeReplicationTasks recovery visibility.",
                "reason": "Secondary axis.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "AWS DMS checkpoint semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
                "query": "",
                "depends_on": ["missing-unit"],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
            },
            {
                "section_id": "task-visibility",
                "title": "Task Visibility",
                "goal": "Explain DescribeReplicationTasks recovery visibility.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = sparse_official_docs_planner

    response = await runtime.start(
        query="Continue AWS DMS checkpoint resume follow-up with official docs only",
        continue_from_job_id=source.job_id,
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["planner"] == "test"
    assert plan["continuation"]["mode"] == "continue"
    assert plan["research_units"][0]["depends_on"] == []
    assert "Explain AWS DMS checkpoint resume semantics." in plan["research_units"][0]["query"]
    assert "filled_search_query:unit-search-1" in trace["normalize_actions"]
    assert "dropped_unknown_dependency:unit-search-1->missing-unit" in trace["normalize_actions"]
    assert "unknown_dependency" in trace["validation_issues"]
    assert trace["unsafe_plan"] is False
    assert plan["brief"]["scope"]["allowed_sources"] == ["docs.aws.amazon.com"]


@pytest.mark.asyncio
async def test_fresh_official_doc_fetch_without_url_degrades_to_search_without_fallback(tmp_path):
    runtime = build_runtime(tmp_path)

    async def sparse_official_docs_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain checkpoint identity in durable workflows.",
                "reason": "Primary axis.",
            },
            {
                "id": "sq2",
                "question": "Explain resume behavior after worker restart.",
                "reason": "Secondary axis.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Checkpoint identity page",
                "goal": "Explain checkpoint identity in durable workflows.",
                "url": "",
                "depends_on": [],
                "status": "pending",
                "notes": "Use the official docs page that explains checkpoint identity.",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "checkpoint-identity",
                "title": "Checkpoint Identity",
                "goal": "Explain checkpoint identity in durable workflows.",
            },
            {
                "section_id": "resume-behavior",
                "title": "Resume Behavior",
                "goal": "Explain resume behavior after worker restart.",
            },
        ]
        return payload

    runtime._generate_plan_with_model = sparse_official_docs_planner

    response = await runtime.start(
        query="Follow up checkpoint identity and resume behavior with official docs only",
        include_domains=["learn.microsoft.com"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["planner"] == "test"
    assert plan["research_units"][0]["unit_type"] == "search"
    assert plan["research_units"][0]["query"] == "Explain checkpoint identity in durable workflows."
    assert any(unit["goal"] == "Explain resume behavior after worker restart." for unit in plan["research_units"])
    assert "degraded_fetch_without_url_to_search:unit-fetch-1" in trace["normalize_actions"]
    assert "missing_fetch_url" in trace["validation_issues"]
    assert trace["unsafe_plan"] is False


@pytest.mark.asyncio
async def test_official_doc_stop_policy_expands_search_budget_to_match_sub_questions(tmp_path):
    runtime = build_runtime(tmp_path)

    async def sparse_official_docs_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint identity.", "reason": "Primary axis."},
            {"id": "sq2", "question": "Explain RecoveryCheckpoint visibility.", "reason": "Secondary axis."},
            {"id": "sq3", "question": "Explain awsdms_txn_state persistence.", "reason": "Third axis."},
        ]
        payload["search_strategy"]["search_queries"] = ["checkpoint identity"]
        payload["brief"]["stop_policy"] = {"max_search_queries": 1}
        return payload

    runtime._generate_plan_with_model = sparse_official_docs_planner

    response = await runtime.start(
        query="Follow up AWS DMS checkpoint semantics with official docs only",
        include_domains=["docs.aws.amazon.com"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    assert response["plan"]["brief"]["stop_policy"]["max_search_queries"] >= 3


@pytest.mark.asyncio
async def test_non_official_allowlist_structural_safe_repairs_use_salvage(tmp_path):
    runtime = build_runtime(tmp_path)

    async def sparse_allowlisted_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics.", "reason": "Primary axis."},
            {"id": "sq2", "question": "Explain restart trade-offs.", "reason": "Secondary axis."},
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Generic docs search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        return payload

    runtime._generate_plan_with_model = sparse_allowlisted_planner

    response = await runtime.start(
        query="Checkpoint resume semantics",
        include_domains=["example.com"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["planner"] == "salvage"
    assert plan["planner_metadata"]["used_fallback"] is False
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["salvage_used"] is True
    assert trace["unsafe_plan"] is True
    assert plan["search_strategy"]["search_queries"] == [
        "Explain checkpoint resume semantics.",
        "Explain restart trade-offs.",
    ]


@pytest.mark.asyncio
async def test_non_structural_query_fill_from_notes_still_forces_fallback(tmp_path):
    runtime = build_runtime(tmp_path)

    async def notes_only_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics.", "reason": "Primary axis."},
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "",
                "goal": "",
                "query": "",
                "depends_on": [],
                "status": "pending",
                "notes": "Use a troubleshooting query about restart surprises after interruption.",
            }
        ]
        payload["search_strategy"]["search_queries"] = []
        return payload

    runtime._generate_plan_with_model = notes_only_planner

    response = await runtime.start(
        query="Checkpoint resume semantics",
        include_domains=["docs.langchain.com", "google.github.io"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["planner"] == "fallback"
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["unsafe_plan"] is True
    assert trace["query_repair_details"][0]["source_kind"] == "unit_notes"


@pytest.mark.asyncio
async def test_continuation_open_question_query_fill_still_forces_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Prior lifecycle investigation",
        report_payload={
            "query": "Prior lifecycle investigation",
            "summary": "Checkpoint replay is stable, but restart trade-offs remain open.",
            "sections": [],
            "coverage": {
                "uncovered_sub_questions": [
                    "Investigate restart trade-offs after worker interruption.",
                ],
                "unanswered_sections": ["Restart Trade-offs"],
            },
            "unit_results": {},
        },
    )

    async def sparse_continuation_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = []
        payload["search_strategy"]["search_queries"] = []
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "",
                "goal": "",
                "query": "",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    runtime._generate_plan_with_model = sparse_continuation_planner

    response = await runtime.start(
        query="Follow up lifecycle trade-offs",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["planner"] == "fallback"
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["unsafe_plan"] is True
    assert trace["query_repair_details"][0]["source_kind"] == "continuation_goal"


@pytest.mark.asyncio
async def test_plan_normalization_expands_generic_outline_from_sub_questions(tmp_path):
    runtime = build_runtime(tmp_path)

    async def generic_outline_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Compare checkpoint resume semantics", "reason": "Primary axis."},
            {"id": "sq2", "question": "Compare restart trade-offs", "reason": "Secondary axis."},
            {"id": "sq3", "question": "Explain operational recovery risks", "reason": "Operational axis."},
        ]
        payload["report_outline"] = [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Present the main evidence."},
        ]
        return payload

    runtime._generate_plan_with_model = generic_outline_planner

    response = await runtime.start(
        query="Expand outline coverage",
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline_titles = [section["title"] for section in response["plan"]["report_outline"]]
    trace = response["plan"]["planner_metadata"]["trace"]
    validation = response["plan"]["planner_metadata"]["validation"]

    assert response["plan"]["planner_metadata"]["planner"] == "salvage"
    assert response["plan"]["planner_metadata"]["used_fallback"] is False
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert outline_titles == [
        "Executive Summary",
        "Compare checkpoint resume semantics",
        "Compare restart trade-offs",
        "Explain operational recovery risks",
    ]
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert trace["salvage_used"] is True
    assert "expanded_outline_from_sub_questions" in trace["normalize_actions"]
    assert "generic_outline_for_sub_questions" in validation["issues"]


@pytest.mark.asyncio
async def test_plan_normalization_expands_generic_outline_for_continuation_focus(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Continuation outline source")

    async def generic_outline_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Investigate interrupted, continue, and resume semantics for deep research jobs", "reason": "Primary axis."},
        ]
        payload["brief"]["continuation_focus"] = [
            "checkpoint identity and worker fencing",
            "resume from finalizing semantics",
            "carry-forward evidence scope",
        ]
        payload["report_outline"] = [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Present the main evidence."},
            {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
        ]
        return payload

    runtime._generate_plan_with_model = generic_outline_planner

    response = await runtime.start(
        query="Follow up on lifecycle semantics",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline_titles = [section["title"] for section in response["plan"]["report_outline"]]
    trace = response["plan"]["planner_metadata"]["trace"]

    assert outline_titles == [
        "Executive Summary",
        "Checkpoint identity and worker fencing",
        "Resume from finalizing semantics",
        "Carry-forward evidence scope",
    ]
    assert "expanded_outline_from_follow_up_surface" in trace["normalize_actions"]


@pytest.mark.asyncio
async def test_plan_normalization_rewrites_only_generic_remainder_for_continuation_focus(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Mixed outline source")

    async def mixed_outline_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["continuation_focus"] = [
            "checkpoint replay boundary",
            "resume lineage visibility",
        ]
        payload["report_outline"] = [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {
                "section_id": "existing-specific",
                "title": "Recovered batch consistency",
                "goal": "Explain how resolved final batches are chosen.",
            },
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Present the main evidence."},
            {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
        ]
        return payload

    runtime._generate_plan_with_model = mixed_outline_planner

    response = await runtime.start(
        query="Follow up mixed outline lifecycle semantics",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    outline = response["plan"]["report_outline"]
    outline_titles = [section["title"] for section in outline]

    assert outline_titles == [
        "Executive Summary",
        "Recovered batch consistency",
        "Checkpoint replay boundary",
        "Resume lineage visibility",
    ]
    assert outline[1]["rewrite_reason"] == ""
    assert outline[2]["rewrite_reason"] == "follow_up_surface"
    assert outline[3]["rewrite_reason"] == "follow_up_surface"


@pytest.mark.asyncio
async def test_continue_requires_existing_source_job(tmp_path):
    runtime = build_runtime(tmp_path)

    with pytest.raises(KeyError):
        await runtime.start(
            query="Invalid continuation",
            continue_from_job_id="missing-job-id",
            plan_only=True,
            force_new=True,
            schedule=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_status", ["queued", "running"])
async def test_plan_only_does_not_reuse_active_execution_job(tmp_path, existing_status):
    runtime = build_runtime(tmp_path)
    existing = runtime.store.create_job(
        query="Reuse boundary",
        request_fingerprint=runtime._request_fingerprint(
            query="Reuse boundary",
            context="",
            effort="standard",
            include_domains=[],
            exclude_domains=[],
            continue_from_job_id="",
            plan_only=False,
        ),
        status=existing_status,
        phase="planning",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Reuse boundary",
        plan_only=True,
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is False
    assert response["job_id"] != existing.job_id
    assert response["status"] == "draft"
    assert response["plan_only"] is True


@pytest.mark.asyncio
async def test_plan_only_does_not_reuse_completed_execution_job(tmp_path):
    runtime = build_runtime(tmp_path)
    existing = create_completed_source_job(runtime, query="Reuse completed boundary")
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Reuse completed boundary",
        plan_only=True,
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is False
    assert response["job_id"] != existing.job_id
    assert response["status"] == "draft"
    assert response["plan_only"] is True


@pytest.mark.asyncio
async def test_continue_requires_existing_source_job_without_creating_orphan(tmp_path):
    runtime = build_runtime(tmp_path)

    with pytest.raises(KeyError):
        await runtime.start(
            query="Invalid continuation without orphan",
            continue_from_job_id="missing-job-id",
            plan_only=True,
            force_new=True,
            schedule=False,
        )

    assert runtime.store.list_jobs(limit=20) == []


@pytest.mark.asyncio
async def test_time_budget_interrupts_after_completed_checkpoint_and_resume_finishes_remaining_units(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    executed = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain interrupted resume behavior.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "Collect interrupted resume checkpoint evidence.",
                "query": "interrupted resume behavior first checkpoint",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Collect interrupted resume recovery evidence.",
                "query": "interrupted resume behavior second checkpoint",
                "depends_on": ["unit-search-1"],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": [
                "interrupted resume behavior first checkpoint",
                "interrupted resume behavior second checkpoint",
            ],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def slow_search(query):
        executed.append(query)
        await asyncio.sleep(1.2)
        return (
            f"Interrupted resume behavior evidence for {query}",
            [{"url": f"https://docs.example.com/{query.replace(' ', '-')}", "title": f"{query.title()} docs"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", slow_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query="Interrupt after first checkpoint",
        time_budget_seconds=1,
        force_new=True,
        schedule=False,
    )
    interrupted = await runtime.run_job(response["job_id"])
    status = await runtime.status(response["job_id"])
    events = await runtime.events(response["job_id"])
    interrupted_event = next(event for event in events["events"] if event["type"] == "job_interrupted")

    assert interrupted["status"] == "interrupted"
    assert status["status"] == "interrupted"
    assert status["last_error"] == "time_budget_exceeded"
    assert runtime.store.read_artifact_text(response["job_id"], "partial_report.md")
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md") is None
    assert runtime.store.get_job(response["job_id"]).current_checkpoint == "researching-unit-search-1"
    assert interrupted_event["data"]["reason"] == "time_budget_exceeded"
    assert interrupted_event["data"]["checkpoint_key"] == "researching-unit-search-1"
    assert interrupted_event["data"]["checkpoint_kind"] == "research_unit"
    assert interrupted_event["data"]["attempt_count"] == 1
    assert interrupted_event["data"]["completed_units_count"] == 1
    assert executed == ["interrupted resume behavior first checkpoint"]

    resumed = await runtime.resume(response["job_id"], schedule=False)
    completed = await runtime.run_job(response["job_id"])

    assert resumed["status"] == "queued"
    assert resumed["attempt_count"] == 2
    assert completed["status"] == "completed"
    assert executed == [
        "interrupted resume behavior first checkpoint",
        "interrupted resume behavior second checkpoint",
    ]
    assert runtime.store.get_job(response["job_id"]).finished_at
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md")


@pytest.mark.asyncio
async def test_resume_from_research_checkpoint_restores_without_replaying_planning_phase(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    call_count = 0

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain interrupted resume behavior.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "Collect first checkpointed evidence.",
                "query": "first checkpoint",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Collect second checkpointed evidence.",
                "query": "second checkpoint",
                "depends_on": ["unit-search-1"],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["first checkpoint", "second checkpoint"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def timed_search(query):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(1.2 if call_count == 1 else 0)
        return (
            f"Evidence for {query}",
            [{"url": f"https://docs.example.com/{query.replace(' ', '-')}", "title": f"{query.title()} docs"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", timed_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query="Interrupt before second unit",
        time_budget_seconds=1,
        force_new=True,
        schedule=False,
    )
    await runtime.run_job(response["job_id"])
    first_events = await runtime.events(response["job_id"])
    first_last_seq = first_events["next_after_seq"]

    await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])
    resumed_events = await runtime.events(response["job_id"], after_seq=first_last_seq)

    resumed_types = [event["type"] for event in resumed_events["events"]]
    resumed_phases = [(event["type"], event["phase"]) for event in resumed_events["events"]]

    assert resumed_types[0] == "job_resumed"
    assert resumed_types[1] == "checkpoint_restored"
    assert resumed_events["events"][0]["data"]["attempt_id"] == "attempt-2"
    assert resumed_events["events"][1]["data"]["attempt_id"] == "attempt-2"
    assert ("phase_started", "planning") not in resumed_phases
    assert ("phase_started", "researching") in resumed_phases


@pytest.mark.asyncio
async def test_events_after_seq_replays_only_new_events_after_research_resume(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    call_count = 0

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain interrupted resume behavior.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "Collect first checkpointed evidence.",
                "query": "first checkpoint",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Collect second checkpointed evidence.",
                "query": "second checkpoint",
                "depends_on": ["unit-search-1"],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["first checkpoint", "second checkpoint"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def timed_search(query):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(1.2 if call_count == 1 else 0)
        return (
            f"Evidence for {query}",
            [{"url": f"https://docs.example.com/{query.replace(' ', '-')}", "title": f"{query.title()} docs"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", timed_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query="Interrupt and replay only new events",
        time_budget_seconds=1,
        force_new=True,
        schedule=False,
    )
    await runtime.run_job(response["job_id"])
    first_events = await runtime.events(response["job_id"])
    first_last_seq = first_events["next_after_seq"]

    await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])
    resumed_events = await runtime.events(response["job_id"], after_seq=first_last_seq)

    assert resumed_events["events"]
    assert all(event["seq"] > first_last_seq for event in resumed_events["events"])
    assert not any(event["type"] == "job_created" for event in resumed_events["events"])
    assert not any(
        event["type"] == "research_unit_completed"
        and event["message"] == "Completed unit-search-1."
        for event in resumed_events["events"]
    )
    assert resumed_events["events"][0]["type"] == "job_resumed"
    assert resumed_events["events"][1]["type"] == "checkpoint_restored"
    assert resumed_events["events"][0]["data"]["attempt_id"] == "attempt-2"
    assert resumed_events["events"][1]["data"]["attempt_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_cancel_requested_wins_over_batch_failure(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    executed = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "First unit",
                "goal": "First",
                "query": "first failure race",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Second unit",
                "goal": "Second",
                "query": "second failure race",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = ["first failure race", "second failure race"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def racing_search(query):
        executed.append(query)
        await runtime.cancel(current_job_id)
        raise RuntimeError(f"boom:{query}")

    async def no_fetch(url):
        return None

    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "2")
    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", racing_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Cancel beats failure", force_new=True, schedule=False)
    current_job_id = response["job_id"]
    result = await runtime.run_job(current_job_id)
    events = await runtime.events(current_job_id)
    status = await runtime.status(current_job_id)

    assert result["status"] == "canceled"
    assert status["status"] == "canceled"
    assert any(event["type"] == "cancel_requested" for event in events["events"])
    assert any(event["type"] == "job_canceled" for event in events["events"])
    assert not any(event["type"] == "job_failed" for event in events["events"])
    assert runtime.store.read_artifact_text(current_job_id, "final_report.md") is None
    assert executed


@pytest.mark.asyncio
async def test_reconciled_interrupted_job_cannot_be_completed_by_stale_worker(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    did_reconcile = False

    async def reconciling_search(query):
        nonlocal did_reconcile
        if not did_reconcile:
            did_reconcile = True
            runtime.store.reconcile_incomplete_jobs()
        return (
            "Recovered answer",
            [{"url": "https://example.com/recovered", "title": "Recovered source"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", reconciling_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Fence stale worker after reconcile", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    status = await runtime.status(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert did_reconcile is True
    assert result["status"] == "interrupted"
    assert status["status"] == "interrupted"
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md") is None
    assert any(event["type"] == "job_interrupted" for event in events["events"])
    assert not any(event["type"] == "job_completed" for event in events["events"])


@pytest.mark.asyncio
async def test_stale_worker_exception_after_reconcile_does_not_overwrite_terminal_state(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    async def stale_runner(current_runtime, job_id):
        current_runtime.store.reconcile_incomplete_jobs()
        raise RuntimeError("stale boom")

    runtime._runner = stale_runner
    response = await runtime.start(query="Stale worker exception fencing", force_new=True, schedule=False)

    await runtime._run(response["job_id"])

    job = runtime.store.get_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert job.status == "interrupted"
    assert job.last_error == "worker_restarted"
    assert any(event["type"] == "job_interrupted" for event in events["events"])
    assert not any(event["type"] == "job_failed" for event in events["events"])


@pytest.mark.asyncio
async def test_stale_worker_reconnect_lifecycle_matches_round21_fixture(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fixture = load_deep_research_fixture("probe_round21_stale_worker_reconnect.json")
    search_calls = 0

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "stale-worker-reconnect",
                "title": "Stale Worker Reconnect",
                "goal": "Track resume behavior after reconnect-triggered worker recovery.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "u1",
                "unit_type": "search",
                "title": "Checkpoint one",
                "goal": "Collect the first checkpoint.",
                "query": "checkpoint one",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "u2",
                "unit_type": "search",
                "title": "Checkpoint two",
                "goal": "Collect the second checkpoint.",
                "query": "checkpoint two",
                "depends_on": ["u1"],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "u3",
                "unit_type": "search",
                "title": "Checkpoint three",
                "goal": "Collect the third checkpoint.",
                "query": "checkpoint three",
                "depends_on": ["u2"],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint one", "checkpoint two", "checkpoint three"],
            "selective_fetch": {
                "max_urls_per_search": 0,
                "prefer_titles_matching_outline": False,
            },
        }
        return payload

    async def timed_search(query):
        nonlocal search_calls
        search_calls += 1
        await asyncio.sleep(1.2)
        return (
            f"Evidence for {query}",
            [{"url": f"https://example.com/{query.replace(' ', '-')}", "title": f"{query.title()} source"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", timed_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query=fixture["query"],
        time_budget_seconds=1,
        force_new=True,
        schedule=False,
    )
    first_result = await runtime.run_job(response["job_id"])
    first_status = await runtime.status(response["job_id"])
    first_resume = await runtime.resume(response["job_id"], schedule=False)
    queued_status = await runtime.status(response["job_id"])

    runtime.store.update_job(response["job_id"], heartbeat_at="2000-01-01T00:00:00Z")
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z", response["job_id"]),
        )

    other_runtime = DeepResearchRuntime(runtime.store.root_dir)
    monkeypatch.setattr(other_runtime, "_generate_plan_with_model", planner)

    reconciled_status = await other_runtime.status(response["job_id"])
    resumed = await other_runtime.resume(response["job_id"], schedule=False)
    resumed_status = await other_runtime.status(response["job_id"])
    final_result = await other_runtime.run_job(response["job_id"])
    final_status = await other_runtime.status(response["job_id"])
    all_events = await other_runtime.events(response["job_id"])
    resumed_window = await other_runtime.events(
        response["job_id"],
        after_seq=fixture["resume_window"]["after_seq"],
    )

    assert search_calls == 2
    assert first_result["status"] == fixture["first_run"]["status"]
    assert first_status["current_checkpoint"] == fixture["first_run"]["current_checkpoint"]
    assert first_status["current_checkpoint_kind"] == fixture["first_run"]["current_checkpoint_kind"]
    assert first_status["attempt_count"] == fixture["first_run"]["attempt_count"]
    assert first_resume["status"] == "queued"
    assert queued_status["current_checkpoint_kind"] == fixture["expected"]["checkpoint_kind"]
    assert reconciled_status["status"] == fixture["reconnect_reconcile"]["status"]
    assert reconciled_status["last_error"] == fixture["reconnect_reconcile"]["last_error"]
    assert reconciled_status["current_checkpoint"] == fixture["reconnect_reconcile"]["current_checkpoint"]
    assert reconciled_status["current_checkpoint_kind"] == fixture["reconnect_reconcile"]["current_checkpoint_kind"]
    assert reconciled_status["attempt_count"] == fixture["reconnect_reconcile"]["attempt_count"]
    assert resumed["status"] == "queued"
    assert resumed_status["current_checkpoint"] == fixture["reconnect_reconcile"]["current_checkpoint"]
    assert resumed_status["current_checkpoint_kind"] == fixture["expected"]["checkpoint_kind"]
    assert final_result["status"] == fixture["resume_run"]["status"]
    assert final_status["current_checkpoint"] == fixture["resume_run"]["current_checkpoint"]
    assert final_status["current_checkpoint_kind"] == fixture["resume_run"]["current_checkpoint_kind"]
    assert final_status["attempt_count"] == fixture["resume_run"]["attempt_count"]
    assert final_status["status"] == fixture["resume_run"]["status"]
    assert final_status["current_checkpoint_kind"] == fixture["resume_run"]["current_checkpoint_kind"]
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in all_events["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in fixture["initial_events"] + fixture["resume_events_after_seq_7"]
    ]
    assert resumed_window["next_after_seq"] == fixture["resume_window"]["next_after_seq"]
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in resumed_window["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in fixture["resume_events_after_seq_7"]
    ]
    assert resumed_window["events"][0]["data"]["resume_source"] == fixture["expected"]["resume_source"]
    assert resumed_window["events"][0]["data"]["checkpoint_kind"] == fixture["expected"]["checkpoint_kind"]
    assert resumed_window["events"][1]["type"] == "checkpoint_restored"
    assert resumed_window["events"][1]["data"]["checkpoint_kind"] == fixture["expected"]["checkpoint_kind"]
    assert not any(
        event["type"] == "phase_started" and event["phase"] == "planning"
        for event in resumed_window["events"]
    )


@pytest.mark.asyncio
async def test_round24_worker_restart_events_after_seq_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round24_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    resumed_window = await runtime.events(
        job.job_id,
        after_seq=snapshot["resume_window"]["after_seq"],
        limit=20,
    )

    assert resumed_window["next_after_seq"] == snapshot["resume_window"]["next_after_seq"]
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in resumed_window["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_16"]
    ]
    assert resumed_window["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert resumed_window["events"][1]["type"] == "checkpoint_restored"


@pytest.mark.asyncio
async def test_round24_worker_restart_status_and_result_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round24_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert status["status"] == snapshot["final_status"]["status"]
    assert status["phase"] == snapshot["final_status"]["phase"]
    assert status["attempt_count"] == snapshot["final_status"]["attempt_count"]
    assert status["runtime_warnings"] == snapshot["final_status"]["runtime_warnings"]
    assert status["constraint_violations"] == snapshot["final_status"]["constraint_violations"]
    assert result["status"] == snapshot["final_status"]["status"]
    assert result["phase"] == snapshot["final_status"]["phase"]
    assert_fixture_public_surface(snapshot, status=status, result=result, seeded_batch_id=seeded["batch_id"])


@pytest.mark.asyncio
async def test_round26_worker_restart_dispatch_runtime_events_after_seq_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round26_worker_restart_dispatch_runtime_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    resumed_window = await runtime.events(
        job.job_id,
        after_seq=snapshot["resume_window"]["after_seq"],
        limit=20,
    )

    assert resumed_window["next_after_seq"] == snapshot["resume_window"]["next_after_seq"]
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in resumed_window["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_4"]
    ]
    assert resumed_window["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert resumed_window["events"][1]["type"] == "checkpoint_restored"


@pytest.mark.asyncio
async def test_round26_worker_restart_dispatch_runtime_status_and_result_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round26_worker_restart_dispatch_runtime_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert status["status"] == snapshot["resume_run"]["status"]
    assert status["phase"] == snapshot["resume_run"]["phase"]
    assert status["attempt_count"] == snapshot["resume_run"]["attempt_count"]
    assert status["current_checkpoint"] == snapshot["resume_run"]["current_checkpoint"]
    assert status["current_checkpoint_kind"] == snapshot["resume_run"]["current_checkpoint_kind"]
    assert status["last_error"] == snapshot["resume_run"]["last_error"]
    assert result["status"] == snapshot["resume_run"]["status"]
    assert result["phase"] == snapshot["resume_run"]["phase"]
    assert result["partial_report"].startswith("# Partial Report")
    assert result["final_report"] is None
    assert result["sources"] is None
    assert result["report"] is None
    assert_fixture_public_surface(snapshot, status=status, result=result, seeded_batch_id="")


@pytest.mark.asyncio
async def test_round30_worker_restart_events_after_seq_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round30_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    resumed_window = await runtime.events(
        job.job_id,
        after_seq=snapshot["resume_window"]["after_seq"],
        limit=20,
    )

    assert resumed_window["next_after_seq"] == snapshot["resume_window"]["next_after_seq"]
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in resumed_window["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_5"]
    ]
    assert resumed_window["returned_count"] == snapshot["events_public_surface"]["returned_count"]
    assert resumed_window["last_event_type"] == snapshot["events_public_surface"]["last_event_type"]
    assert resumed_window["window_has_terminal_event"] is snapshot["events_public_surface"]["window_has_terminal_event"]
    assert resumed_window["job_terminal"] is snapshot["events_public_surface"]["job_terminal"]
    assert resumed_window["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert resumed_window["events"][1]["type"] == "checkpoint_restored"


@pytest.mark.asyncio
async def test_round30_worker_restart_status_and_result_match_fixture(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_round30_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert status["status"] == snapshot["final_status"]["status"]
    assert status["phase"] == snapshot["final_status"]["phase"]
    assert status["attempt_count"] == snapshot["final_status"]["attempt_count"]
    assert status["runtime_warnings"] == snapshot["final_status"]["runtime_warnings"]
    assert status["constraint_violations"] == snapshot["final_status"]["constraint_violations"]
    assert result["status"] == snapshot["final_status"]["status"]
    assert result["phase"] == snapshot["final_status"]["phase"]
    assert result["report"] is None
    assert result["final_report"] is None
    assert result["sources"] is None
    assert_fixture_public_surface(snapshot, status=status, result=result, seeded_batch_id=seeded["batch_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", RECENT_DEEP_RESEARCH_LIVE_PROBE_FIXTURES)
async def test_recent_probe_status_and_result_match_fixture(tmp_path, fixture_name):
    runtime = build_runtime(tmp_path)
    seeded = seed_live_probe_fixture_job(
        runtime,
        fixture_name,
        request_fingerprint=f"fp-runtime-{fixture_name.removesuffix('.json')}",
    )
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert status["status"] == snapshot["job"]["status"]
    assert status["phase"] == snapshot["job"]["phase"]
    assert status["attempt_count"] == snapshot["job"]["attempt_count"]
    assert status["current_checkpoint"] == snapshot["job"]["current_checkpoint"]
    assert status["current_checkpoint_kind"] == snapshot["job"]["current_checkpoint_kind"]
    assert result["status"] == snapshot["job"]["status"]
    assert result["phase"] == snapshot["job"]["phase"]
    if snapshot.get("strict_public_surface"):
        assert_recent_probe_fixture_public_surface(
            snapshot,
            status=status,
            result=result,
            seeded_batch_id=seeded["batch_id"],
        )
    else:
        assert status["runtime_warnings"] == snapshot["public_surface"]["status"]["runtime_warnings"]
        result_surface = snapshot["public_surface"].get("result") or {}
        if result_surface:
            assert result["runtime_warnings"] == result_surface["runtime_warnings"]
            assert result["report"]["status"] == result_surface["report"]["status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", RECENT_DEEP_RESEARCH_LIVE_PROBE_FIXTURES)
async def test_recent_probe_events_after_seq_match_fixture(tmp_path, fixture_name):
    runtime = build_runtime(tmp_path)
    seeded = seed_live_probe_fixture_job(
        runtime,
        fixture_name,
        request_fingerprint=f"fp-runtime-events-{fixture_name.removesuffix('.json')}",
    )
    job = seeded["job"]
    snapshot = seeded["snapshot"]
    event_window = snapshot["event_window"]

    payload = await runtime.events(
        job.job_id,
        after_seq=event_window["after_seq"],
        limit=20,
    )

    assert payload["next_after_seq"] == event_window["next_after_seq"]
    assert payload["returned_count"] == event_window["returned_count"]
    assert payload["last_event_type"] == event_window["last_event_type"]
    assert payload["window_has_terminal_event"] is event_window["window_has_terminal_event"]
    assert payload["job_terminal"] is event_window["job_terminal"]
    if snapshot.get("strict_public_surface"):
        assert [
            (event["seq"], event["type"], event["phase"])
            for event in payload["events"]
        ] == [
            (event["seq"], event["type"], event["phase"])
            for event in snapshot["events_after_seq"]
        ]


@pytest.mark.asyncio
async def test_round38_aws_dms_partial_failure_fixture_preserves_partial_artifacts(tmp_path):
    runtime = build_runtime(tmp_path)
    seeded = seed_live_probe_fixture_job(
        runtime,
        "probe_round38_aws_dms_partial_failure.json",
        request_fingerprint="fp-runtime-round38-aws-dms-partial-failure",
    )
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    status_surface = snapshot["public_surface"]["status"]
    result_surface = snapshot["public_surface"]["result"]
    assert status["status"] == status_surface["status"]
    assert status["phase"] == status_surface["phase"]
    assert status["current_checkpoint"] == status_surface["current_checkpoint"]
    assert status["current_checkpoint_kind"] == status_surface["current_checkpoint_kind"]
    assert status["resolved_artifact_batch_id"] == status_surface["resolved_artifact_batch_id"]
    assert status["artifact_visibility_reason"] == status_surface["artifact_visibility_reason"]
    assert status["runtime_warnings"] == status_surface["runtime_warnings"]
    assert all(kind in status["artifact_kinds"] for kind in status_surface["artifact_kinds"])
    assert result["status"] == result_surface["status"]
    assert result["phase"] == result_surface["phase"]
    assert result["resolved_artifact_batch_id"] == result_surface["resolved_artifact_batch_id"]
    assert result["artifact_visibility_reason"] == result_surface["artifact_visibility_reason"]
    assert result["runtime_warnings"] == result_surface["runtime_warnings"]
    assert result["report"] is None
    assert result["final_report"] is None
    assert result["partial_payload"]["selected_bank"] == snapshot["selected_bank"]
    assert result["partial_payload"]["evidence_ledger"] is not None
    assert result["partial_payload"]["section_banks"] is not None
    assert result["operator_summary"]["partial_payload_available"] is True
    assert result["selected_bank"] == snapshot["selected_bank"]
    assert result["artifact_errors"] == result_surface["artifact_errors"]
    evidence_ledger = json.loads(runtime.store.read_artifact_text(job.job_id, "evidence_ledger.json") or "[]")
    source_urls_by_evidence_id = {
        entry.get("evidence_id"): list(entry.get("source_urls") or [])
        for entry in evidence_ledger
        if entry.get("evidence_id")
    }
    selected_section_ids = [
        bank["section_id"]
        for bank in result["selected_bank"]
        if bank.get("selected_rows")
    ]
    assert selected_section_ids == [
        "overview-of-aws-dms-cdc-checkpoint-and-resume-mechanics",
        "describereplicationtasks-and-related-apis-for-checkpoint-visibility",
    ]
    assert any(
        "flink-restart.html" in url
        for bank in result["selected_bank"]
        for row in bank.get("selected_rows", [])
        for url in source_urls_by_evidence_id.get(row.get("evidence_id"), [])
    )


@pytest.mark.asyncio
async def test_status_and_result_mirror_attempt_window_and_partial_payload_fields(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Mirror attempt window surface",
        request_fingerprint="fp-mirror-attempt-window-surface",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    job = runtime.store.update_job(job.job_id, attempt_count=2, current_checkpoint="researching-u2")
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(
            {
                "brief": {"objective": "Mirror attempt window surface"},
                "sub_questions": [{"id": "sq1", "question": "Mirror attempt window surface", "reason": "Primary axis."}],
                "search_strategy": {"approach": "targeted", "search_queries": ["Mirror attempt window surface"]},
                "report_outline": [{"section_id": "summary", "title": "Summary", "goal": "Summarize the answer."}],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "source_policy.json",
        json.dumps({"allow_domains": ["docs.example.com"], "web_mode": "restrict"}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "lineage.json",
        json.dumps({"continued_from_job_id": "job-prev", "resume_source": "worker_restarted"}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "outline_state.json",
        json.dumps({"status": "draft", "current_version": 2}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "outline_versions.json",
        json.dumps([{"version": 1}, {"version": 2}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "section_graph.json",
        json.dumps({"root_section_ids": ["summary"], "sections": [{"section_id": "summary"}]}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "evidence_ledger.json",
        json.dumps([{"ledger_id": "ledger-1", "evidence_id": "e1", "source_urls": ["https://docs.example.com/1"]}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "section_banks.json",
        json.dumps([{"section_id": "summary", "selected_evidence_ids": ["e1"]}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "selected_bank.json",
        json.dumps([{"section_id": "summary", "selected_rows": [{"evidence_id": "e1"}]}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "evidence_bank.json",
        json.dumps([{"evidence_id": "e1", "source_ids": ["R1"]}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "verification.json",
        json.dumps({"packet_to_prose_fidelity": {"passed": True, "checked_packet_count": 1}}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "coverage_gaps.json",
        json.dumps({"coverage_gate_passed": True, "total_gap_count": 0, "unanswered_sections": [], "uncovered_sub_questions": [], "hard_uncovered_targets": [], "gaps": []}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "partial_report.md",
        "# Partial Report\n\nStill working.\n",
        "text/markdown",
    )
    runtime.store.append_event(
        job.job_id,
        type="job_interrupted",
        phase="researching",
        message="attempt 1 stopped",
        data={"attempt_id": "attempt-1", "attempt_count": 1, "reason": "worker_restarted"},
    )
    runtime.store.append_event(
        job.job_id,
        type="job_resumed",
        phase="researching",
        message="attempt 2 resumed",
        data={"attempt_id": "attempt-2", "attempt_count": 2},
    )
    runtime.store.append_event(
        job.job_id,
        type="research_unit_completed",
        phase="researching",
        message="u2 done",
        data={"attempt_id": "attempt-2", "unit_id": "u2"},
    )

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)
    start_payload = runtime._job_payload(runtime.store.get_job(job.job_id), reused=False)

    assert status["watch_attach_after_seq"] == 1
    assert result["watch_attach_after_seq"] == 1
    assert start_payload["watch_attach_after_seq"] == 1
    assert status["attempt_window_start_seq"] == 2
    assert result["attempt_window_start_seq"] == 2
    assert start_payload["attempt_window_start_seq"] == 2
    assert status["operator_summary"]["watch_attach_after_seq"] == status["watch_attach_after_seq"]
    assert result["operator_summary"]["watch_attach_after_seq"] == result["watch_attach_after_seq"]
    assert start_payload["operator_summary"]["watch_attach_after_seq"] == start_payload["watch_attach_after_seq"]
    assert status["operator_summary"]["attempt_window_start_seq"] == status["attempt_window_start_seq"]
    assert result["operator_summary"]["attempt_window_start_seq"] == result["attempt_window_start_seq"]
    assert start_payload["operator_summary"]["attempt_window_start_seq"] == start_payload["attempt_window_start_seq"]
    assert status["operator_summary"]["partial_payload_available"] is True
    assert result["operator_summary"]["partial_payload_available"] is True
    assert start_payload["operator_summary"]["partial_payload_available"] is True
    assert status["partial_payload"]["plan"]["brief"]["objective"] == "Mirror attempt window surface"
    assert start_payload["partial_payload"]["plan"]["brief"]["objective"] == "Mirror attempt window surface"
    assert result["partial_payload"]["source_policy"]["web_mode"] == "restrict"
    assert result["partial_payload"]["lineage"]["continued_from_job_id"] == "job-prev"
    assert result["partial_payload"]["outline_state"]["current_version"] == 2
    assert result["partial_payload"]["outline_versions"] == [{"version": 1}, {"version": 2}]
    assert result["partial_payload"]["section_graph"]["root_section_ids"] == ["summary"]
    assert result["partial_payload"]["evidence_ledger"][0]["evidence_id"] == "e1"
    assert result["partial_payload"]["section_banks"][0]["section_id"] == "summary"
    assert result["partial_payload"]["selected_bank"][0]["section_id"] == "summary"
    assert result["partial_payload"]["evidence_bank"][0]["evidence_id"] == "e1"
    assert result["partial_payload"]["verification"]["packet_to_prose_fidelity"]["passed"] is True
    assert result["partial_payload"]["coverage_gaps"]["total_gap_count"] == 0
    assert result["partial_payload"]["partial_artifact_errors"] == {}
    assert status["partial_payload"]["evidence_bank"][0]["evidence_id"] == "e1"
    assert status["partial_payload"]["verification"]["packet_to_prose_fidelity"]["passed"] is True
    assert status["partial_payload"]["coverage_gaps"]["total_gap_count"] == 0


@pytest.mark.asyncio
async def test_result_without_include_partial_hides_partial_payload_bundle(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="No partial bundle",
        request_fingerprint="fp-no-partial-bundle",
        status="running",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "partial_report.md", "# Partial Report\n\nStill working.\n", "text/markdown")
    runtime.write_artifact(job.job_id, "verification.json", json.dumps({"packet_to_prose_fidelity": {"passed": True}}), "application/json")

    result = await runtime.result(job.job_id, include_partial=False)

    assert result["partial_report"] is None
    assert result["partial_payload"] is None
    assert result["operator_summary"]["partial_payload_available"] is False


@pytest.mark.asyncio
async def test_result_partial_payload_surfaces_partial_artifact_errors(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Broken partial sidecars",
        request_fingerprint="fp-broken-partial-sidecars",
        status="running",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"brief": {"objective": "Broken partial sidecars"}}), "application/json")
    runtime.write_artifact(job.job_id, "evidence_bank.json", "{", "application/json")
    runtime.write_artifact(job.job_id, "verification.json", json.dumps([]), "application/json")
    runtime.write_artifact(job.job_id, "coverage_gaps.json", json.dumps({"gaps": "bad"}), "application/json")

    result = await runtime.result(job.job_id)

    assert result["partial_payload"] is not None
    assert result["partial_payload"]["evidence_bank"] is None
    assert result["partial_payload"]["verification"] is None
    assert result["partial_payload"]["coverage_gaps"] is None
    assert result["partial_payload"]["partial_artifact_errors"] == {
        "evidence_bank.json": "invalid_json",
        "verification.json": "invalid_shape",
        "coverage_gaps.json": "invalid_shape",
    }


@pytest.mark.asyncio
async def test_resume_resolved_final_batch_clears_cancel_requested(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Resolved final batch clears cancel flag",
        request_fingerprint="fp-resolved-final-batch-clears-cancel",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="finalizing",
        attempt_count=2,
        cancel_requested=True,
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final batch report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Resolved final batch clears cancel flag",
        ),
    )

    resumed = await runtime.resume(job.job_id, schedule=False)

    assert resumed["status"] == "completed"
    assert resumed["cancel_requested"] is False
    resolved_event = next(event for event in (await runtime.events(job.job_id))["events"] if event["type"] == "job_resolved_from_final_batch")
    assert resolved_event["data"]["attempt_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_resume_keeps_distinct_dispatch_checkpoint_identities_across_attempts(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    did_reconcile = False

    async def reconciling_search(query):
        nonlocal did_reconcile
        if not did_reconcile:
            did_reconcile = True
            runtime.store.reconcile_incomplete_jobs()
        return (
            "Recovered answer",
            [{"url": "https://example.com/recovered", "title": "Recovered source"}],
        )

    async def stable_search(query):
        return (
            "Recovered answer",
            [{"url": "https://example.com/recovered", "title": "Recovered source"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", reconciling_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Distinct dispatch checkpoint identities", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", stable_search)
    await runtime.resume(response["job_id"], schedule=False)
    await runtime.run_job(response["job_id"])

    dispatch_keys = [
        checkpoint.checkpoint_key
        for checkpoint in runtime.store.list_checkpoints(response["job_id"])
        if checkpoint.checkpoint_key.startswith("researching-dispatch-")
    ]

    assert len(dispatch_keys) >= 2
    assert len(set(dispatch_keys)) == len(dispatch_keys)


@pytest.mark.asyncio
async def test_status_surfaces_checkpoint_kind_for_interrupted_dispatch_resume(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    did_reconcile = False

    async def reconciling_search(query):
        nonlocal did_reconcile
        if not did_reconcile:
            did_reconcile = True
            runtime.store.reconcile_incomplete_jobs()
        return (
            "Recovered answer",
            [{"url": "https://example.com/recovered", "title": "Recovered source"}],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", reconciling_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Dispatch checkpoint kind status", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])
    status = await runtime.status(response["job_id"])
    result = await runtime.result(response["job_id"])

    assert status["status"] == "interrupted"
    assert status["current_checkpoint_kind"] == "research_dispatch"
    assert result["status"] == "interrupted"
    assert result["current_checkpoint_kind"] == "research_dispatch"


@pytest.mark.asyncio
async def test_canceling_queued_job_emits_single_terminal_canceled_event(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    response = await runtime.start(
        query="Queued cancel race",
        force_new=True,
        schedule=False,
    )
    await runtime.cancel(response["job_id"])
    await runtime._run(response["job_id"])

    events = await runtime.events(response["job_id"])
    canceled_events = [event for event in events["events"] if event["type"] == "job_canceled"]

    assert len(canceled_events) == 1
    assert canceled_events[0]["phase"] == "planning"


@pytest.mark.asyncio
async def test_cancel_completed_job_is_noop_without_new_cancel_event(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Completed cancel noop",
        request_fingerprint="fp-completed-cancel-noop",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    before_events = await runtime.events(job.job_id)

    canceled = await runtime.cancel(job.job_id)
    after_events = await runtime.events(job.job_id)

    assert canceled["status"] == "completed"
    assert canceled["cancel_requested"] is False
    assert after_events["events"] == before_events["events"]


@pytest.mark.asyncio
async def test_cancel_running_job_returns_full_job_payload(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Running cancel payload",
        request_fingerprint="fp-running-cancel-payload",
        status="running",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="researching-u1",
        attempt_count=2,
        progress_pct=35.0,
    )

    canceled = await runtime.cancel(job.job_id)

    assert canceled["job_id"] == job.job_id
    assert canceled["status"] == "running"
    assert canceled["phase"] == "researching"
    assert canceled["current_checkpoint"] == "researching-u1"
    assert canceled["attempt_count"] == 2
    assert canceled["progress_pct"] == 35.0
    assert canceled["cancel_requested"] is True
    assert canceled["artifact_kinds"] == []
    assert canceled["planner_fallback_used"] is False
    assert canceled["runtime_warnings"] == []
    assert canceled["constraint_violations"] == []


@pytest.mark.asyncio
async def test_continuation_uses_citations_registry_when_sources_artifact_missing(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Citations fallback source job",
        request_fingerprint="fp-citations-fallback-source-job",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "citations.json",
        json.dumps(
            {
                "source_registry": {
                    "R7": {
                        "source_id": "R7",
                        "url": "https://docs.example.com/runtime/fallback",
                        "title": "Fallback docs",
                        "provider": "grok",
                    }
                },
                "sections": [],
            }
        ),
        "application/json",
    )

    response = await runtime.start(
        query="Continue from citations registry fallback",
        continue_from_job_id=original.job_id,
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert response["plan"]["continuation"]["source_count"] == 1
    assert continuation_payload["carry_forward_sources"] == [
        {
            "source_id": "R7",
            "url": "https://docs.example.com/runtime/fallback",
            "title": "Fallback docs",
            "provider": "grok",
        }
    ]


@pytest.mark.asyncio
async def test_continuation_falls_back_when_sources_and_report_shapes_are_invalid(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(
        runtime,
        query="Invalid continuation shapes",
        sources_content=json.dumps(
            {
                "source_registry": {
                    "R1": {
                        "source_id": "R1",
                        "url": "https://docs.example.com/runtime/checkpoints",
                        "title": "Runtime checkpoints",
                    }
                }
            }
        ),
        report_payload={
            "query": "Invalid continuation shapes",
            "summary": "Resume continues from the last checkpoint.",
            "sections": {"bad": "shape"},
            "unit_results": {"unit-search-1": {"summary": "Resume continues from the last checkpoint."}},
        },
        checkpoint_sections=[
            {
                "section_id": "checkpoint-semantics",
                "title": "Checkpoint Semantics",
                "summary": "Checkpoint sections should survive.",
                "claims": [
                    {
                        "claim_id": "checkpoint-semantics-claim-1",
                        "text": "Checkpoint sections should survive.",
                        "citations": ["R1"],
                        "unit_id": "unit-search-1",
                        "evidence_ids": ["evidence-unit-search-1-search"],
                    }
                ],
                "citations": ["R1"],
            }
        ],
    )

    response = await runtime.start(
        query="Follow up invalid continuation shapes",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"][0]["url"] == "https://docs.example.com/runtime/checkpoints"
    assert continuation_payload["carry_forward_sections"][0]["title"] == "Checkpoint Semantics"


@pytest.mark.asyncio
async def test_corrupt_runtime_continuation_rebuilds_from_source_job(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Corrupt continuation rebuild")

    response = await runtime.start(
        query="Follow up corrupt continuation rebuild",
        continue_from_job_id=source.job_id,
        plan_only=False,
        force_new=True,
        schedule=False,
    )
    runtime.write_artifact(response["job_id"], "continuation.json", "{bad-json", "application/json")

    rebuilt = runtime._read_runtime_continuation(runtime.store.get_job(response["job_id"]))

    assert rebuilt.mode == "continue"
    assert rebuilt.source_job_id == source.job_id
    assert rebuilt.carry_forward_sources[0]["url"] == "https://docs.example.com/runtime/checkpoints"
    assert rebuilt.carry_forward_sections[0]["title"] == "Executive Summary"


@pytest.mark.asyncio
async def test_corrupt_runtime_continuation_uses_frozen_planning_checkpoint_snapshot(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Frozen continuation source")

    response = await runtime.start(
        query="Follow up frozen continuation source",
        continue_from_job_id=source.job_id,
        plan_only=False,
        force_new=True,
        schedule=False,
    )
    job = runtime.store.get_job(response["job_id"])

    runtime.write_artifact(
        source.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Updated source summary after follow-up creation.",
                "sections": [
                    {
                        "section_id": "updated",
                        "title": "Updated",
                        "summary": "Updated source summary after follow-up creation.",
                        "claims": [{"claim_id": "c1", "text": "Updated source summary after follow-up creation.", "citations": ["R9"]}],
                        "citations": ["R9"],
                    }
                ],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://docs.example.com/runtime/updated"}]),
        "application/json",
    )
    runtime.write_artifact(job.job_id, "continuation.json", "{bad-json", "application/json")

    rebuilt = runtime._read_runtime_continuation(job)

    assert rebuilt.mode == "continue"
    assert rebuilt.source_job_id == source.job_id
    assert rebuilt.carry_forward_sources[0]["source_id"] == "R1"
    assert rebuilt.carry_forward_sections[0]["title"] == "Executive Summary"


@pytest.mark.asyncio
async def test_read_plan_uses_frozen_follow_up_continuation_instead_of_live_source_job(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Frozen continuation precedence source")

    response = await runtime.start(
        query="Follow up frozen continuation precedence source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    follow_up_job = runtime.store.get_job(response["job_id"])
    frozen_continuation = json.loads(runtime.store.read_artifact_text(follow_up_job.job_id, "continuation.json"))

    runtime.write_artifact(
        source.job_id,
        "report.json",
        json.dumps(
            {
                "query": "Frozen continuation precedence source",
                "summary": "Updated source summary after follow-up creation.",
                "sections": [
                    {
                        "section_id": "updated",
                        "title": "Updated",
                        "summary": "Updated source summary after follow-up creation.",
                        "claims": [
                            {
                                "claim_id": "updated-claim-1",
                                "text": "Updated source summary after follow-up creation.",
                                "citations": ["R9"],
                            }
                        ],
                        "citations": ["R9"],
                    }
                ],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://docs.example.com/runtime/updated"}]),
        "application/json",
    )

    rebuilt_plan = runtime._read_plan(follow_up_job.job_id, follow_up_job)

    assert rebuilt_plan.continuation.previous_summary == frozen_continuation["previous_summary"]
    assert rebuilt_plan.continuation.source_count == frozen_continuation["source_count"]


@pytest.mark.asyncio
async def test_read_plan_keeps_frozen_follow_up_continuation_when_source_job_gets_new_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Frozen follow-up should not drift")

    response = await runtime.start(
        query="Follow up frozen follow-up should not drift",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    follow_up_job = runtime.store.get_job(response["job_id"])
    frozen_continuation = json.loads(runtime.store.read_artifact_text(follow_up_job.job_id, "continuation.json"))

    runtime.write_artifact_batch(
        source.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R9", "url": "https://docs.example.com/runtime/newer"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {"R9": {"source_id": "R9", "url": "https://docs.example.com/runtime/newer"}},
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "A newer source batch should not rewrite the frozen follow-up continuation.",
                        "sections": [],
                        "unit_results": {},
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nA newer source batch should not rewrite the frozen follow-up continuation.\n",
                "content_type": "text/markdown",
            },
        ],
    )

    rebuilt_plan = runtime._read_plan(follow_up_job.job_id, follow_up_job)

    assert rebuilt_plan.continuation.previous_summary == frozen_continuation["previous_summary"]
    assert rebuilt_plan.continuation.source_count == frozen_continuation["source_count"]


@pytest.mark.asyncio
async def test_load_checkpoint_state_uses_frozen_follow_up_continuation_instead_of_live_source_job(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Frozen checkpoint continuation source")

    response = await runtime.start(
        query="Follow up frozen checkpoint continuation source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    follow_up_job = runtime.store.update_job(response["job_id"], current_checkpoint="planning")
    frozen_continuation = json.loads(runtime.store.read_artifact_text(follow_up_job.job_id, "continuation.json"))

    runtime.write_artifact(
        source.job_id,
        "report.json",
        json.dumps(
            {
                "query": "Frozen checkpoint continuation source",
                "summary": "Updated source summary after follow-up creation.",
                "sections": [
                    {
                        "section_id": "updated",
                        "title": "Updated",
                        "summary": "Updated source summary after follow-up creation.",
                        "claims": [
                            {
                                "claim_id": "updated-claim-1",
                                "text": "Updated source summary after follow-up creation.",
                                "citations": ["R9"],
                            }
                        ],
                        "citations": ["R9"],
                    }
                ],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://docs.example.com/runtime/updated"}]),
        "application/json",
    )

    checkpoint_state, checkpoint_meta = runtime._load_checkpoint_state(follow_up_job)

    assert checkpoint_meta is None
    assert checkpoint_state is not None
    assert checkpoint_state.plan.continuation.previous_summary == frozen_continuation["previous_summary"]
    assert checkpoint_state.plan.continuation.source_count == frozen_continuation["source_count"]


@pytest.mark.asyncio
async def test_load_checkpoint_state_keeps_frozen_follow_up_continuation_when_failed_source_job_drifts(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = create_completed_source_job(runtime, query="Frozen failed continuation source")
    runtime.store.update_job(
        source.job_id,
        status="failed",
        phase="researching",
        current_checkpoint="researching-u1",
        last_error="upstream provider failure",
        finished_at=utc_now_iso(),
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="researching",
        checkpoint_key="researching-u1",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "Older frozen failed-source summary.",
                    "detail": "Older frozen failed-source summary.",
                    "source_ids": ["R1"],
                }
            },
            "sources": [{"source_id": "R1", "url": "https://docs.example.com/runtime/older"}],
            "sections": [],
            "evidence_items": [],
        },
    )

    response = await runtime.start(
        query="Follow up frozen failed continuation source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    follow_up_job = runtime.store.update_job(response["job_id"], current_checkpoint="planning")
    frozen_continuation = json.loads(runtime.store.read_artifact_text(follow_up_job.job_id, "continuation.json"))

    runtime.write_artifact(
        source.job_id,
        "report.json",
        json.dumps(
            {
                "query": "Frozen failed continuation source",
                "summary": "Updated failed source summary after follow-up creation.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://docs.example.com/runtime/updated-failed"}]),
        "application/json",
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="researching",
        checkpoint_key="researching-u2",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "Updated failed source summary after follow-up creation.",
                    "detail": "Updated failed source summary after follow-up creation.",
                    "source_ids": ["R9"],
                }
            },
            "sources": [{"source_id": "R9", "url": "https://docs.example.com/runtime/updated-failed"}],
            "sections": [],
            "evidence_items": [],
        },
    )

    checkpoint_state, checkpoint_meta = runtime._load_checkpoint_state(follow_up_job)

    assert checkpoint_meta is None
    assert checkpoint_state is not None
    assert checkpoint_state.plan.continuation.previous_summary == frozen_continuation["previous_summary"]
    assert checkpoint_state.plan.continuation.source_count == frozen_continuation["source_count"]


@pytest.mark.asyncio
async def test_continuation_prefers_consistent_final_artifact_batch_over_mixed_current_artifacts(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Consistent batch continuation source",
        request_fingerprint="fp-consistent-batch-continuation",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact_batch(
        original.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Checkpoint resume summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nCheckpoint resume summary.",
                "content_type": "text/markdown",
            },
        ],
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps([{"source_id": "R999", "url": "https://stale.example.com/mixed"}]),
        "application/json",
    )

    response = await runtime.start(
        query="Continue from consistent batch source",
        continue_from_job_id=original.job_id,
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"] == [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
        }
    ]
    assert continuation_payload["previous_summary"] == "Checkpoint resume summary."


@pytest.mark.asyncio
async def test_continuation_does_not_mix_checkpoint_state_into_selected_final_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = runtime.store.create_job(
        query="Single tier continuation source",
        request_fingerprint="fp-single-tier-continuation",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(source.job_id, "plan.json", json.dumps({"query": "Single tier continuation source"}), "application/json")
    runtime.write_artifact_batch(
        source.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Selected final batch summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nSelected final batch summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Single tier continuation source",
        ),
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="synthesizing",
        checkpoint_key="synthesizing",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "Stale checkpoint summary.",
                    "detail": "Stale checkpoint detail.",
                    "source_ids": ["R9"],
                    "citations": ["R9"],
                }
            },
            "sources": [{"source_id": "R9", "url": "https://docs.example.com/runtime/stale"}],
            "evidence_items": [],
            "sections": [
                {
                    "section_id": "stale",
                    "title": "Stale checkpoint section",
                    "summary": "Stale checkpoint summary.",
                    "claims": [{"claim_id": "c1", "text": "Stale checkpoint summary.", "citations": ["R9"]}],
                    "citations": ["R9"],
                }
            ],
        },
    )

    response = await runtime.start(
        query="Follow up single tier continuation source",
        continue_from_job_id=source.job_id,
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"] == [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
        }
    ]
    assert continuation_payload["carry_forward_sections"] == []
    assert continuation_payload["carry_forward_unit_results"] == {}
    assert continuation_payload["previous_summary"] == "Selected final batch summary."


@pytest.mark.asyncio
async def test_continuation_uses_current_valid_sources_before_checkpoint_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = runtime.store.create_job(
        query="Per artifact fallback source",
        request_fingerprint="fp-per-artifact-fallback-source",
        status="failed",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(source.job_id, "plan.json", json.dumps({"query": "Per artifact fallback source"}), "application/json")
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/current"}]),
        "application/json",
    )
    runtime.write_artifact(source.job_id, "report.json", "{bad-json", "application/json")
    runtime.store.save_checkpoint(
        source.job_id,
        phase="researching",
        checkpoint_key="researching",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "Checkpoint fallback summary.",
                    "detail": "Checkpoint fallback detail.",
                    "source_ids": ["R9"],
                    "citations": ["R9"],
                }
            },
            "sources": [{"source_id": "R9", "url": "https://docs.example.com/runtime/checkpoint"}],
            "evidence_items": [],
            "sections": [
                {
                    "section_id": "checkpoint-only",
                    "title": "Checkpoint Only",
                    "summary": "Checkpoint fallback summary.",
                    "claims": [{"claim_id": "c1", "text": "Checkpoint fallback summary.", "citations": ["R9"]}],
                    "citations": ["R9"],
                }
            ],
        },
    )

    response = await runtime.start(
        query="Follow up per artifact fallback source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"] == [
        {"source_id": "R1", "url": "https://docs.example.com/runtime/current"}
    ]
    assert continuation_payload["carry_forward_sections"][0]["title"] == "Checkpoint Only"
    assert continuation_payload["focused_snapshot"]["artifact_origin_map"]["sources.json"] == "current_artifact"


@pytest.mark.asyncio
async def test_continuation_uses_current_valid_sources_when_latest_batch_candidate_is_partial(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    source = runtime.store.create_job(
        query="Per artifact partial batch source",
        request_fingerprint="fp-per-artifact-partial-batch-source",
        status="failed",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(source.job_id, "plan.json", json.dumps({"query": "Per artifact partial batch source"}), "application/json")
    runtime.write_artifact(
        source.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/current"}]),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "report.json",
        json.dumps({"summary": "Current artifact summary.", "sections": [], "unit_results": {}}),
        "application/json",
    )
    runtime.write_artifact_batch(
        source.job_id,
        [
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Batch report summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nBatch report summary.",
                "content_type": "text/markdown",
            },
        ],
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="researching",
        checkpoint_key="researching",
        state={
            "plan": structured_plan_payload(source, {"mode": "fresh"}),
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": {
                "unit-search-1": {
                    "summary": "Checkpoint fallback summary.",
                    "detail": "Checkpoint fallback detail.",
                    "source_ids": ["R9"],
                    "citations": ["R9"],
                }
            },
            "sources": [{"source_id": "R9", "url": "https://docs.example.com/runtime/checkpoint"}],
            "evidence_items": [],
            "sections": [],
        },
    )

    response = await runtime.start(
        query="Follow up per artifact partial batch source",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"] == [
        {"source_id": "R1", "url": "https://docs.example.com/runtime/current"}
    ]
    assert continuation_payload["focused_snapshot"]["artifact_origin_map"]["sources.json"] == "current_artifact"


@pytest.mark.asyncio
async def test_continuation_falls_back_to_older_usable_final_batch_when_latest_complete_batch_is_invalid(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Older usable continuation source",
        request_fingerprint="fp-older-usable-continuation",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(original.job_id, "plan.json", json.dumps({"query": "Older usable continuation source"}), "application/json")
    runtime.write_artifact_batch(
        original.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Good continuation report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nGood continuation report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Older usable continuation source",
        ),
    )
    runtime.write_artifact_batch(
        original.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R2", "url": "https://docs.example.com/runtime/bad"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R2": {"source_id": "R2", "url": "https://docs.example.com/runtime/bad"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": "{bad-json",
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nBad continuation report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Older usable continuation source",
        ),
    )

    response = await runtime.start(
        query="Continue from older usable batch",
        continue_from_job_id=original.job_id,
        force_new=True,
        plan_only=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation_payload["carry_forward_sources"] == [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
        }
    ]


@pytest.mark.asyncio
async def test_execute_start_does_not_reuse_plan_only_draft(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))

    draft = await runtime.start(
        query="Shared fingerprint query",
        context="Same input",
        plan_only=True,
        force_new=False,
        schedule=False,
    )

    execute = await runtime.start(
        query="Shared fingerprint query",
        context="Same input",
        plan_only=False,
        force_new=False,
        schedule=False,
    )

    assert draft["status"] == "draft"
    assert execute["reused"] is False
    assert execute["job_id"] != draft["job_id"]


@pytest.mark.asyncio
async def test_completed_job_with_corrupt_final_json_is_not_reused(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    completed = runtime.store.create_job(
        query="Corrupt completed reuse source",
        request_fingerprint=runtime._request_fingerprint(
            query="Corrupt completed reuse source",
            context="",
            effort="standard",
            include_domains=[],
            exclude_domains=[],
            continue_from_job_id="",
            plan_only=False,
        ),
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        completed.job_id,
        "plan.json",
        json.dumps({"query": "Corrupt completed reuse source"}),
        "application/json",
    )
    runtime.write_artifact_batch(
        completed.job_id,
        [
            {"kind": "sources.json", "content": "{bad-json", "content_type": "application/json"},
            {"kind": "citations.json", "content": "{bad-json", "content_type": "application/json"},
            {"kind": "report.json", "content": "{bad-json", "content_type": "application/json"},
            {"kind": "final_report.md", "content": "# Final Report\n\nBad batch.\n", "content_type": "text/markdown"},
        ],
    )

    response = await runtime.start(
        query="Corrupt completed reuse source",
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is False
    assert response["job_id"] != completed.job_id


@pytest.mark.asyncio
async def test_result_reports_invalid_json_and_missing_required_artifacts_together(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Mixed artifact errors",
        request_fingerprint="fp-mixed-artifact-errors",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", "{bad-json", "application/json")
    runtime.write_artifact(job.job_id, "sources.json", json.dumps([{"source_id": "R1", "url": "https://ok.example.com"}]), "application/json")

    result = await runtime.result(job.job_id)

    assert result["artifact_errors"]["plan.json"] == "invalid_json"
    assert result["artifact_errors"]["citations.json"] == "missing_required_artifact"
    assert result["artifact_errors"]["report.json"] == "missing_required_artifact"
    assert result["artifact_errors"]["final_report.md"] == "missing_required_artifact"


@pytest.mark.asyncio
async def test_source_registry_keeps_stable_ids_and_prefers_enriched_metadata(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Original source registry",
        request_fingerprint="fp-original-source-registry",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R4",
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official checkpoint docs.",
                    "snippet": "Official docs snippet.",
                    "provider": "grok",
                    "domain": "docs.example.com",
                    "rank": 1,
                }
            ]
        ),
        "application/json",
    )

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["query"] = "runtime checkpoint docs"
        payload["search_strategy"]["search_queries"] = ["runtime checkpoint docs"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint docs answer.",
            [
                {
                    "url": "https://stackoverflow.com/questions/1/runtime-checkpoints",
                    "title": "Runtime checkpoint docs",
                    "description": "Community page.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "",
                    "description": "",
                    "snippet": "",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nOfficial checkpoint docs explain resumable runtime recovery."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="Continue source enrichment work",
        continue_from_job_id=original.job_id,
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    registry = result["citations"]["source_registry"]
    docs_source = next(item for item in registry.values() if item["url"] == "https://docs.example.com/runtime/checkpoints")

    assert docs_source["source_id"] == "R4"
    assert docs_source["title"] == "Runtime checkpoints"
    assert "Official checkpoint docs" in (docs_source.get("description") or docs_source.get("snippet") or "")
    assert all("stackoverflow.com" not in item["url"] for item in registry.values())


@pytest.mark.asyncio
async def test_completed_claims_bind_only_to_supporting_source_ids(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from checkpoints without reloading completed work.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                },
                {
                    "url": "https://docs.example.com/runtime/restart",
                    "title": "Runtime restart",
                    "description": "Restart docs.",
                },
            ],
        )

    async def fetch(url):
        if "checkpoints" in url:
            return "# Runtime checkpoints\n\nResume continues from the last completed checkpoint."
        return "# Runtime restart\n\nRestart replays work from the beginning."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Checkpoint resume behavior", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_section = result["report"]["sections"][0]
    first_claim = first_section["claims"][0]
    citations = set(first_claim["citations"])
    registry = result["citations"]["source_registry"]
    cited_urls = {registry[source_id]["url"] for source_id in citations}

    assert cited_urls == {"https://docs.example.com/runtime/checkpoints"}


@pytest.mark.asyncio
async def test_search_grounded_fetch_uses_query_aware_excerpt_instead_of_page_lead(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS documentation covers checkpoint resume behavior.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference.",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return (
            "# StartReplicationTask\n\n"
            "Starts the replication task. For more information about AWS DMS tasks, see Working with Migration Tasks.\n"
            "The StartReplicationTaskType value `resume-processing` resumes from the last recovery checkpoint when checkpoint metadata is still available.\n"
            "Use `reload-target` to reload target tables instead of continuing from the prior checkpoint.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS checkpoint resume semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    summary = result["report"]["summary"]
    claim_text = result["report"]["sections"][0]["claims"][0]["text"]

    assert "resumes from the last recovery checkpoint" in summary
    assert "resumes from the last recovery checkpoint" in claim_text
    assert "Starts the replication task." not in summary
    assert "Starts the replication task." not in claim_text


@pytest.mark.asyncio
async def test_search_only_evidence_does_not_claim_multi_source_corroboration(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume-processing continues from the last durable checkpoint when recovery metadata is still available.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.example.com/runtime/restart",
                    "title": "Runtime restart",
                    "description": "Restart docs.",
                    "provider": "grok",
                },
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(query="checkpoint resume semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    claim = result["report"]["sections"][0]["claims"][0]

    assert claim["cluster_type"] == "single_source"
    assert claim["supporting_source_count"] == 1
    assert len(claim["citations"]) == 1
    assert claim["evidence_bindings"][0]["line_start"] is None
    assert claim["evidence_bindings"][0]["line_end"] is None
    assert claim["evidence_bindings"][0]["excerpt_origin"] == "search_answer"
    assert claim["evidence_bindings"][0]["source_backed"] is False


@pytest.mark.asyncio
async def test_same_domain_corroboration_does_not_escalate_claim_confidence_to_high(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "key-findings",
                "title": "Key Findings",
                "goal": "Compare resume and restart behavior.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "AWS DMS guide",
                "goal": "Use official docs only.",
                "query": "aws dms checkpoint resume restart",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "AWS DMS troubleshooting",
                "goal": "Use official docs only.",
                "query": "aws dms troubleshooting restart",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = [
            "aws dms checkpoint resume restart",
            "aws dms troubleshooting restart",
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        if "troubleshooting" in query:
            return (
                "Resume-processing continues from the last checkpoint, but troubleshooting guidance also discusses support cases.",
                [
                    {
                        "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html",
                        "title": "Troubleshooting migration tasks in AWS Database Migration Service",
                        "description": "Official troubleshooting docs.",
                    }
                ],
            )
        return (
            "Resume-processing continues from the last checkpoint while restart replays work from the beginning.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                    "title": "CHAP Task.CDC",
                    "description": "Official CDC docs.",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "API StartReplicationTask",
                    "description": "Official API docs.",
                },
            ],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="AWS DMS confidence probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    claim = result["report"]["sections"][0]["claims"][0]
    section = result["report"]["sections"][0]

    assert claim["confidence"] != "high"
    assert section["confidence"] != "high"


@pytest.mark.asyncio
async def test_unmatched_outline_section_is_omitted_when_evidence_overlap_is_below_threshold(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            },
            {
                "section_id": "billing-impact",
                "title": "Billing Impact",
                "goal": "Explain invoice reconciliation behavior.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last completed checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume continues from the last completed checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Checkpoint resume behavior", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    section_titles = [section["title"] for section in result["report"]["sections"]]

    assert "Resume Semantics" in section_titles
    assert "Billing Impact" not in section_titles
    assert "## Billing Impact" not in result["final_report"]


@pytest.mark.asyncio
async def test_continue_plan_sanitizes_previous_summary_before_persisting_continuation(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Original noisy summary source",
        request_fingerprint="fp-original-noisy-summary",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Topics can help you to resolve common issues using both AWS DMS and selected endpoint databases. Resume continues from the last completed checkpoint.",
                "sections": [],
                "unit_results": {},
            }
        ),
        "application/json",
    )

    response = await runtime.start(
        query="Follow-up on checkpoint resume",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert "topics can help you to resolve common issues" not in response["plan"]["continuation"]["previous_summary"].lower()
    assert "topics can help you to resolve common issues" not in continuation_payload["previous_summary"].lower()
    assert "resume continues from the last completed checkpoint" in continuation_payload["previous_summary"].lower()


@pytest.mark.asyncio
async def test_continue_plan_inherits_domain_constraints_and_keeps_summary_out_of_follow_up_surface(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        request_fingerprint="fp-continuation-constraint-inheritance",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(original.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        original.job_id,
        "plan.json",
        json.dumps(
            {
                "query": "Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
                "sub_questions": [
                    {
                        "id": "sq1",
                        "question": "Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
                        "reason": "Primary question.",
                    }
                ],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": (
                    "Medium confidence: Compare checkpoint resume and restart semantics in AWS DMS with official docs only: "
                    "operations. For more information, see the target settings page."
                ),
                "sections": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "summary": "Resume-processing continues from the last durable checkpoint.",
                        "claims": [
                            {
                                "claim_id": "executive-summary-claim-1",
                                "text": "Resume-processing continues from the last durable checkpoint.",
                                "citations": ["R1"],
                                "unit_id": "unit-search-1",
                                "evidence_ids": ["evidence-unit-search-1-search"],
                                "confidence": "medium",
                            }
                        ],
                        "citations": ["R1"],
                        "confidence": "medium",
                    }
                ],
                "unit_results": {
                    "unit-search-1": {
                        "summary": "Resume-processing continues from the last durable checkpoint.",
                        "detail": "Resume-processing continues from the last durable checkpoint.",
                        "source_ids": ["R1"],
                        "citations": ["R1"],
                    }
                },
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R1",
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.TargetMetadata.html",
                    "title": "Target metadata task settings",
                    "domain": "docs.aws.amazon.com",
                    "source_type": "official_docs",
                    "citation_count": 1,
                    "section_count": 1,
                }
            ]
        ),
        "application/json",
    )

    response = await runtime.start(
        query="Continue from previous findings with focus on restart risk and recovery timeout",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    brief = plan["brief"]
    continuation = plan["continuation"]

    assert plan["include_domains"] == ["docs.aws.amazon.com"]
    assert plan["exclude_domains"] == ["repost.aws"]
    assert brief["scope"]["allowed_sources"] == ["docs.aws.amazon.com"]
    assert brief["scope"]["include_domains"] == ["docs.aws.amazon.com"]
    assert brief["scope"]["exclude_domains"] == ["repost.aws"]
    assert all("for more information" not in item.lower() for item in brief["must_cover"])
    assert all("for more information" not in item.lower() for item in brief["coverage_checklist"])
    assert all("for more information" not in item.lower() for item in brief["continuation_focus"])
    assert all("for more information" not in item.lower() for item in plan["search_strategy"]["search_queries"])
    assert all("for more information" not in item["question"].lower() for item in plan["sub_questions"])
    assert "resume-processing continues from the last durable checkpoint" in continuation["previous_summary"].lower()


@pytest.mark.asyncio
async def test_continuation_open_questions_drop_generic_section_titles(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        request_fingerprint="fp-continuation-open-questions-filter",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(original.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Resume-processing continues from the last durable checkpoint.",
                "sections": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "summary": "Resume-processing continues from the last durable checkpoint.",
                        "claims": [
                            {
                                "claim_id": "executive-summary-claim-1",
                                "text": "Resume-processing continues from the last durable checkpoint.",
                                "citations": ["R1"],
                            }
                        ],
                        "citations": ["R1"],
                    }
                ],
                "coverage": {
                    "uncovered_sub_questions": ["Investigate restart trade-offs after interruption"],
                    "unanswered_sections": ["Key Findings", "Open Questions"],
                },
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R1",
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.TargetMetadata.html",
                    "title": "Target metadata task settings",
                    "domain": "docs.aws.amazon.com",
                    "source_type": "official_docs",
                }
            ]
        ),
        "application/json",
    )

    response = await runtime.start(
        query="Focus on restart risk and recovery timeout",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert continuation["open_questions"] == ["Investigate restart trade-offs after interruption"]
    assert continuation_payload["open_questions"] == ["Investigate restart trade-offs after interruption"]


@pytest.mark.asyncio
async def test_fallback_plan_uses_focused_continuation_surface_instead_of_only_previous_summary(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Compare checkpoint resume and restart semantics in AWS DMS with official docs only",
        request_fingerprint="fp-fallback-continuation-focus",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(original.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Medium confidence: Resume-processing continues from the last durable checkpoint.",
                "sections": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "summary": "Resume-processing continues from the last durable checkpoint.",
                        "claims": [
                            {
                                "claim_id": "executive-summary-claim-1",
                                "text": "Resume-processing continues from the last durable checkpoint.",
                                "citations": ["R1"],
                            }
                        ],
                        "citations": ["R1"],
                    }
                ],
                "coverage": {
                    "uncovered_sub_questions": ["Investigate restart trade-offs after interruption"],
                    "unanswered_sections": ["Key Findings", "Open Questions"],
                },
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R1",
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.TargetMetadata.html",
                    "title": "Target metadata task settings",
                    "domain": "docs.aws.amazon.com",
                    "source_type": "official_docs",
                    "citation_count": 1,
                    "section_count": 1,
                }
            ]
        ),
        "application/json",
    )

    async def unsafe_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"] = "not-a-brief-object"
        payload["research_units"] = [
            {
                "unit_id": "u1",
                "unit_type": "search",
                "title": "Follow-up search",
                "goal": "Investigate restart trade-offs after interruption",
                "query": "",
                "depends_on": ["missing-unit"],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["planner_metadata"] = {"planner": "model", "used_fallback": False}
        return payload

    runtime._generate_plan_with_model = unsafe_planner

    response = await runtime.start(
        query="Focus on restart risk and recovery timeout",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    brief = plan["brief"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert "Resume-processing continues from the last durable checkpoint." in brief["continuation_focus"]
    assert "Investigate restart trade-offs after interruption" in brief["continuation_focus"]
    assert "Target metadata task settings (docs.aws.amazon.com)" in brief["continuation_focus"]
    assert len(brief["continuation_focus"]) >= 3


@pytest.mark.asyncio
async def test_unsafe_plan_uses_bounded_salvage_surface_before_generic_fallback(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Prior checkpoint resume investigation",
        request_fingerprint="fp-bounded-salvage-source",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(original.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume continues from the last durable checkpoint.",
                "sections": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "summary": "Checkpoint resume continues from the last durable checkpoint.",
                        "claims": [
                            {
                                "claim_id": "executive-summary-claim-1",
                                "text": "Checkpoint resume continues from the last durable checkpoint.",
                                "citations": ["R1"],
                            }
                        ],
                        "citations": ["R1"],
                    }
                ],
                "unit_results": {},
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R1",
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.TargetMetadata.html",
                    "title": "Target metadata task settings",
                    "domain": "docs.aws.amazon.com",
                    "source_type": "official_docs",
                }
            ]
        ),
        "application/json",
    )

    async def unsafe_planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Investigate checkpoint replay safety after interruption",
                "reason": "Cover the remaining checkpoint-specific gap.",
            },
            {
                "id": "sq2",
                "question": "Compare restart trade-offs after interruption",
                "reason": "Cover the remaining recovery trade-off gap.",
            },
        ]
        payload["search_strategy"]["search_queries"] = [
            "Investigate checkpoint replay safety after interruption",
            "For more information about troubleshooting issues after restart",
        ]
        payload["research_units"] = [
            {
                "unit_id": "u1",
                "unit_type": "search",
                "title": "Follow-up search",
                "goal": "Investigate checkpoint replay safety after interruption",
                "query": "",
                "depends_on": ["missing-unit"],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["planner_metadata"] = {"planner": "model", "used_fallback": False}
        return payload

    runtime._generate_plan_with_model = unsafe_planner

    response = await runtime.start(
        query="Continue the previous findings",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    plan = response["plan"]
    trace = plan["planner_metadata"]["trace"]

    assert plan["planner_metadata"]["used_fallback"] is False
    assert trace["unsafe_plan"] is False
    assert plan["sub_questions"] == [
        {
            "id": "sq1",
            "question": "Investigate checkpoint replay safety after interruption",
            "reason": "Cover the remaining checkpoint-specific gap.",
        },
        {
            "id": "sq2",
            "question": "Compare restart trade-offs after interruption",
            "reason": "Cover the remaining recovery trade-off gap.",
        },
    ]
    assert plan["search_strategy"]["search_queries"] == [
        "Investigate checkpoint replay safety after interruption",
        "Compare restart trade-offs after interruption",
    ]
    assert all("for more information" not in item.lower() for item in plan["search_strategy"]["search_queries"])


@pytest.mark.asyncio
async def test_continue_from_failed_job_builds_focused_continuation_state(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    original = runtime.store.create_job(
        query="Failed research on checkpoint resume",
        request_fingerprint="fp-failed-continuation-focus",
        status="failed",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(
        original.job_id,
        "plan.json",
        json.dumps(
            {
                "query": "Failed research on checkpoint resume",
                "sub_questions": [
                    {"id": "sq1", "question": "How does checkpoint resume work?", "reason": "Primary question."},
                ],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "",
                "sections": [
                    {
                        "section_id": "executive-summary",
                        "title": "Executive Summary",
                        "summary": "Checkpoint resume continues from the last durable checkpoint.",
                        "claims": [
                            {
                                "claim_id": "executive-summary-claim-1",
                                "text": "Checkpoint resume continues from the last durable checkpoint.",
                                "citations": ["R1"],
                            }
                        ],
                    }
                ],
                "unit_results": {
                    "unit-search-1": {
                        "summary": "Checkpoint resume continues from the last durable checkpoint.",
                        "detail": "Checkpoint resume continues from the last durable checkpoint.",
                        "source_ids": ["R1"],
                        "citations": ["R1"],
                    }
                },
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        original.job_id,
        "sources.json",
        json.dumps(
            [
                {
                    "source_id": "R1",
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Checkpoint runtime docs",
                    "source_type": "official_docs",
                    "citation_count": 1,
                    "section_count": 1,
                },
                {
                    "source_id": "R2",
                    "url": "https://docs.example.com/runtime/troubleshooting",
                    "title": "Troubleshooting runtime docs",
                    "source_type": "official_docs",
                    "citation_count": 0,
                    "section_count": 0,
                },
            ]
        ),
        "application/json",
    )

    response = await runtime.start(
        query="Follow up on failed checkpoint resume research",
        continue_from_job_id=original.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    continuation = response["plan"]["continuation"]
    continuation_payload = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation.json"))

    assert "continuation from failed job" not in continuation["previous_summary"].lower()
    assert "checkpoint resume continues from the last durable checkpoint" in continuation["previous_summary"].lower()
    assert continuation["source_count"] == 1
    assert [source["source_id"] for source in continuation_payload["carry_forward_sources"]] == ["R1"]


@pytest.mark.asyncio
async def test_continuation_identity_is_stable_for_equivalent_focused_snapshot(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Stable continuation identity source")
    runtime.write_artifact(
        source.job_id,
        "plan.json",
        json.dumps(
            {
                "query": "Stable continuation identity source",
                "sub_questions": [
                    {
                        "id": "sq1",
                        "question": "How does checkpoint resume work?",
                        "reason": "Primary question.",
                    }
                ],
            }
        ),
        "application/json",
    )

    first = runtime._build_continuation_context(source.job_id)
    runtime.write_artifact(
        source.job_id,
        "plan.json",
        json.dumps(
            {
                "query": "Stable continuation identity source",
                "sub_questions": [
                    {
                        "id": "sq-rewritten",
                        "question": "How does checkpoint resume work?",
                        "reason": "Still the same focused snapshot.",
                    }
                ],
                "planner_metadata": {
                    "planner": "model",
                    "used_fallback": False,
                    "note": "This should not change continuation identity.",
                },
            }
        ),
        "application/json",
    )
    runtime.store.update_job(source.job_id, current_checkpoint="finalizing-duplicate")
    second = runtime._build_continuation_context(source.job_id)

    assert first.focused_snapshot == second.focused_snapshot
    assert first.continuation_identity == second.continuation_identity


def test_request_fingerprint_normalizes_domain_order_and_duplicates(tmp_path):
    runtime = build_runtime(tmp_path)

    first = runtime._request_fingerprint(
        query="Reuse boundary",
        context="",
        effort="standard",
        include_domains=["docs.aws.amazon.com", "docs.example.com", "docs.aws.amazon.com"],
        exclude_domains=["repost.aws", "example.invalid"],
        continue_from_job_id="seed-job",
        plan_only=False,
        continuation_identity="capsule-1",
    )
    second = runtime._request_fingerprint(
        query="Reuse boundary",
        context="",
        effort="standard",
        include_domains=["docs.example.com", "docs.aws.amazon.com"],
        exclude_domains=["example.invalid", "repost.aws", "repost.aws"],
        continue_from_job_id="seed-job",
        plan_only=False,
        continuation_identity="capsule-1",
    )

    assert first == second


@pytest.mark.asyncio
async def test_plan_build_writes_continuation_capsule_and_uses_compact_planner_surface(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Planner capsule source")
    captured = {}

    async def fake_build_runtime_grok_provider(current_model="", *, effort="standard"):
        provider = _CapturedPlannerProvider(captured)
        return provider, {
            "requested_model": provider.model,
            "effective_model": provider.model,
            "available_models": [provider.model],
            "resolution": None,
        }

    monkeypatch.setattr(deep_research_runtime_module, "_build_runtime_grok_provider", fake_build_runtime_grok_provider)

    response = await runtime.start(
        query="Follow up checkpoint resume semantics",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )

    continuation_capsule = json.loads(runtime.store.read_artifact_text(response["job_id"], "continuation_capsule.json"))
    planner_messages = captured["payload"]["messages"]
    planner_user_payload = json.loads(planner_messages[1]["content"])

    assert continuation_capsule["continuation_identity"] == response["plan"]["continuation"]["continuation_identity"]
    assert continuation_capsule["mode"] == "continue"
    assert "carry_forward_sources" not in continuation_capsule
    assert "previous_summary" not in planner_user_payload["continuation"]
    assert "prior_plan_summary" not in planner_user_payload["continuation"]
    assert planner_messages[0]["content"].startswith("You are planning a deep research job.")
    assert "continuation_capsule" in planner_user_payload


@pytest.mark.asyncio
async def test_status_surfaces_continuation_capsule_artifact(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Status capsule source")
    captured = {}

    async def fake_build_runtime_grok_provider(current_model="", *, effort="standard"):
        provider = _CapturedPlannerProvider(captured)
        return provider, {
            "requested_model": provider.model,
            "effective_model": provider.model,
            "available_models": [provider.model],
            "resolution": None,
        }

    monkeypatch.setattr(deep_research_runtime_module, "_build_runtime_grok_provider", fake_build_runtime_grok_provider)

    response = await runtime.start(
        query="Follow up status capsule semantics",
        continue_from_job_id=source.job_id,
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    status = await runtime.status(response["job_id"])

    assert "continuation_capsule.json" in status["artifact_kinds"]
    assert status["artifact_kinds"].count("continuation_capsule.json") == 1


@pytest.mark.asyncio
async def test_status_artifact_kinds_do_not_duplicate_provenance_sidecars(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Duplicate artifact kinds probe",
        request_fingerprint="fp-duplicate-artifact-kinds-probe",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Duplicate artifact kinds probe"}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Recovered report", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered report.\n",
                    "content_type": "text/markdown",
                }
            ],
            query="Duplicate artifact kinds probe",
        ),
    )

    status = await runtime.status(job.job_id)

    assert status["artifact_kinds"].count("evidence_items.json") == 1
    assert status["artifact_kinds"].count("coverage.json") == 1
    assert status["artifact_kinds"].count("grounding.json") == 1
    assert status["artifact_kinds"].count("verifier.json") == 1


@pytest.mark.asyncio
async def test_search_unit_with_no_allowed_sources_is_marked_failed(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        return payload

    async def search(query):
        return (
            "Resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://blog.example.net/runtime/checkpoints",
                    "title": "Off-domain runtime shell",
                    "description": "Off-domain shell page.",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(
        query="Search unit filtered by allowlist",
        include_domains=["docs.example.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert result["status"] == "failed"
    assert any(
        event["type"] == "research_unit_failed"
        and event["data"]["error"] == "empty_search_result_after_constraints"
        for event in events["events"]
    )
    assert not any(event["type"] == "research_unit_completed" for event in events["events"])


def test_coverage_for_report_requires_grounded_claims_to_mark_answered_or_covered():
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
                {
                    "id": "sq1",
                    "question": "Compare checkpoint resume and restart semantics",
                    "reason": "Primary comparison.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["Compare checkpoint resume and restart semantics"],
                "selective_fetch": {
                    "max_urls_per_search": 1,
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": [
                {
                    "section_id": "resume-vs-restart",
                    "title": "Resume vs Restart",
                    "goal": "Compare checkpoint resume and restart semantics.",
                }
            ],
            "research_units": [],
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "resume-vs-restart",
                "title": "Resume vs Restart",
                "summary": "Restart tasks from the console.",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "Restart tasks from the console.",
                        "citations": [],
                    }
                ],
                "citations": [],
            }
        ],
    )

    assert coverage["answered_section_ids"] == []
    assert coverage["covered_sub_question_ids"] == []
    assert coverage["unanswered_sections"] == ["Resume vs Restart"]
    assert coverage["uncovered_sub_questions"] == ["Compare checkpoint resume and restart semantics"]


def test_coverage_for_report_does_not_treat_scaffold_sections_as_unanswered_research_gaps():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Verify AWS DMS DescribeReplicationTasks RecoveryCheckpoint visibility",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 90,
            "include_domains": ["docs.aws.amazon.com"],
            "exclude_domains": [],
            "brief": {
                "objective": "Verify AWS DMS DescribeReplicationTasks RecoveryCheckpoint visibility",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "What is the purpose and type of RecoveryCheckpoint?",
                    "reason": "Primary field detail.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["ReplicationTask RecoveryCheckpoint site:docs.aws.amazon.com"],
                "selective_fetch": {
                    "max_urls_per_search": 1,
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": [
                {"section_id": "summary-finding", "title": "Summary Finding", "goal": "Summarize the finding."},
                {
                    "section_id": "field-details-and-usage",
                    "title": "Field Details and Usage",
                    "goal": "Explain RecoveryCheckpoint purpose and type.",
                },
                {"section_id": "provider-accounting", "title": "Provider Accounting", "goal": "Summarize provider usage."},
                {"section_id": "citations", "title": "Citations", "goal": "List cited sources."},
            ],
            "research_units": [],
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "field-details-and-usage",
                "title": "Field Details and Usage",
                "summary": "RecoveryCheckpoint purpose is CDC checkpoint recovery and its type is string.",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "RecoveryCheckpoint purpose is CDC checkpoint recovery and its type is string.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "question_ids": ["sq1"],
                    }
                ],
                "citations": ["R1"],
            }
        ],
        evidence_ledger=[
            {
                "ledger_id": "ledger-e1",
                "evidence_id": "e1",
                "source_ids": ["R1"],
                "question_ids": ["sq1"],
                "selected_section_id": "field-details-and-usage",
            }
        ],
    )

    assert coverage["unanswered_sections"] == []
    assert coverage["uncovered_sub_questions"] == []


def test_coverage_for_report_keeps_candidate_only_ledger_out_of_selected_count():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Explain AWS DMS checkpoint resume behavior",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Explain AWS DMS checkpoint resume behavior",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "How does AWS DMS resume CDC from checkpoints?",
                    "reason": "Primary official-doc behavior.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["AWS DMS resume CDC checkpoint"],
                "selective_fetch": {
                    "max_urls_per_search": 1,
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": [
                {
                    "section_id": "checkpoint-resume",
                    "title": "Checkpoint Resume",
                    "goal": "Explain checkpoint resume behavior.",
                    "question_id": "sq1",
                }
            ],
            "research_units": [],
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "checkpoint-resume",
                "title": "Checkpoint Resume",
                "summary": "",
                "claims": [],
                "citations": [],
            }
        ],
        planned_outline=plan.report_outline,
        section_banks=[
            {
                "section_id": "checkpoint-resume",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": [],
            }
        ],
        evidence_ledger=[
            {
                "ledger_id": "ledger-1",
                "evidence_id": "e1",
                "question_ids": ["sq1"],
                "source_urls": ["https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ChangeProcessingTuning.html"],
            }
        ],
    )

    section_coverage = coverage["section_coverage"][0]
    assert section_coverage["candidate_evidence_count"] == 1
    assert section_coverage["selected_evidence_count"] == 0


@pytest.mark.asyncio
async def test_runtime_honors_max_search_queries_stop_policy(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = ["Explain checkpoint resume semantics"]
        payload["brief"]["coverage_checklist"] = ["Explain checkpoint resume semantics"]
        payload["brief"]["stop_policy"] = {
            "stop_on_sufficient_coverage": False,
            "max_search_queries": 1,
            "max_urls_per_search": 0,
            "max_runtime_seconds": 240,
        }
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Primary search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Secondary search",
                "goal": "Explain restart trade-offs.",
                "query": "restart trade-offs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = ["checkpoint resume semantics", "restart trade-offs"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    search_calls: list[str] = []

    async def search(query):
        search_calls.append(query)
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", lambda url: asyncio.sleep(0, result=None))

    response = await runtime.start(query="Respect max_search_queries", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert search_calls == ["checkpoint resume semantics"]
    assert set(result["report"]["unit_results"]) == {"unit-search-1"}
    assert result["report"]["runtime"]["skipped_units"] == [
        {
            "unit_id": "unit-search-2",
            "unit_type": "search",
            "reason": "max_search_queries_reached",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_skips_remaining_units_once_research_stage_coverage_is_sufficient(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = ["Explain checkpoint resume semantics"]
        payload["brief"]["coverage_checklist"] = ["Explain checkpoint resume semantics"]
        payload["brief"]["stop_policy"] = {
            "stop_on_sufficient_coverage": True,
            "max_search_queries": 3,
            "max_urls_per_search": 1,
            "max_runtime_seconds": 240,
        }
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
                "question_ids": ["sq1"],
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Primary search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Follow-up search",
                "goal": "Explain checkpoint resume semantics more.",
                "query": "checkpoint resume semantics follow up",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"]["search_queries"] = [
            "checkpoint resume semantics",
            "checkpoint resume semantics follow up",
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    search_calls: list[str] = []

    async def search(query):
        search_calls.append(query)
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)
    monkeypatch.setattr(
        "grok_search.config.Config.deep_research_max_concurrency",
        property(lambda self: 1),
    )

    response = await runtime.start(query="Sufficient runtime coverage", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert search_calls == ["checkpoint resume semantics"]
    assert result["report"]["runtime"]["skipped_units"] == [
        {
            "unit_id": "unit-search-2",
            "unit_type": "search",
            "reason": "sufficient_coverage_reached",
        }
    ]


@pytest.mark.asyncio
async def test_continuation_artifact_omits_troubleshooting_shell_text_from_carry_forward_state(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["goal"] = "Explain checkpoint resume semantics."
        payload["research_units"][0]["query"] = "checkpoint resume semantics"
        payload["search_strategy"]["search_queries"] = ["checkpoint resume semantics"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume-processing continues from the last checkpoint for previously executed tasks.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html",
                    "title": "Troubleshooting migration tasks in AWS Database Migration Service",
                    "description": "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return (
            "# Troubleshooting migration tasks in AWS Database Migration Service\n\n"
            "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).\n"
            "These topics can help you to resolve common issues using both AWS DMS and selected endpoint databases.\n"
            "Resume-processing continues from the last checkpoint for previously executed tasks.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="continuation shell filter probe", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    continued = await runtime.start(
        query="Follow up on continuation shell filter probe",
        continue_from_job_id=response["job_id"],
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    continuation_payload = json.loads(runtime.store.read_artifact_text(continued["job_id"], "continuation.json"))
    continuation_json = json.dumps(continuation_payload).lower()

    assert "following, you can find topics about troubleshooting issues" not in continuation_json
    assert "these topics can help you to resolve common issues" not in continuation_json
    assert "resume-processing continues from the last checkpoint" in continuation_json


@pytest.mark.asyncio
async def test_final_report_omits_remaining_gaps_without_real_gap_and_claims_stay_consistent(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics.", "reason": "Primary axis."},
            {"id": "sq2", "question": "Explain restart trade-offs.", "reason": "Secondary axis."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "remaining-gaps",
                "title": "Remaining Gaps",
                "goal": "Call out remaining gaps.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last completed unit. Checkpoint resume continues from the last completed unit.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last completed unit."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="No fake gaps", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    report_value = result.get("report") or {}
    citations_value = result.get("citations") or {}
    final_report_text = result.get("final_report") or ""
    if isinstance(report_value, dict) and report_value.get("sections"):
        section_titles = [section["title"] for section in report_value["sections"]]
        report_claims = [
            claim["text"]
            for section in report_value["sections"]
            for claim in section["claims"]
        ]
        citation_claims = [
            claim["text"]
            for section in citations_value["sections"]
            for claim in section["claims"]
        ]
        assert "Remaining Gaps" not in section_titles
        assert report_claims
        assert report_claims == citation_claims
        assert len(report_claims) == len(set(report_claims))
    else:
        assert result["artifact_errors"]
    assert "## Remaining Gaps" not in final_report_text


@pytest.mark.asyncio
async def test_incomplete_coverage_degrades_report_status_and_runtime_warnings(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
            {"id": "sq2", "question": "Explain restart trade-offs", "reason": "Secondary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Restart search",
                "goal": "Explain restart trade-offs.",
                "query": "restart trade-offs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
            "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        if "restart" in query:
            return (
                "General operational guidance for on-call handling.",
                [
                    {
                        "url": "https://docs.example.com/runtime/operations",
                        "title": "Runtime operations overview",
                        "description": "General operational guidance.",
                    }
                ],
            )
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        if "operations" in url:
            return "# Runtime operations overview\n\nGeneral operational guidance for on-call handling."
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Coverage completeness regression", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    if isinstance(result.get("report"), dict):
        assert result["report"]["coverage"]["answered_section_ids"] == ["resume-semantics"]
        assert result["report"]["coverage"]["unanswered_sections"] == ["Restart Trade-offs"]
        assert result["report"]["coverage"]["uncovered_sub_questions"] == ["Explain restart trade-offs"]
        assert result["report"]["status"] == "failed"
        assert "coverage_incomplete" in result["report"]["runtime"]["warnings"]
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_report_exposes_coverage_for_unanswered_sections_and_sub_questions(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
            {"id": "sq2", "question": "Explain restart trade-offs", "reason": "Secondary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Restart trade-off search",
                "goal": "Explain restart trade-offs.",
                "query": "restart trade-offs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
            "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Coverage ledger probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    if isinstance(result.get("report"), dict):
        coverage = result["report"]["coverage"]
        assert coverage["planned_section_ids"] == ["resume-semantics", "restart-tradeoffs"]
        assert coverage["answered_section_ids"] == ["resume-semantics"]
        assert coverage["unanswered_sections"] == ["Restart Trade-offs"]
        assert coverage["planned_sub_question_ids"] == ["sq1", "sq2"]
        assert coverage["covered_sub_question_ids"] == ["sq1"]
        assert coverage["uncovered_sub_questions"] == ["Explain restart trade-offs"]
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_report_exposes_sub_question_to_claim_coverage_ledger(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
            {"id": "sq2", "question": "Explain restart trade-offs", "reason": "Secondary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Restart search",
                "goal": "Explain restart trade-offs.",
                "query": "restart trade-offs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
            "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        if "restart" in query:
            return (
                "General operational guidance for on-call handling.",
                [
                    {
                        "url": "https://docs.example.com/runtime/operations",
                        "title": "Runtime operations overview",
                        "description": "General operational guidance.",
                    }
                ],
            )
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        if "operations" in url:
            return "# Runtime operations overview\n\nGeneral operational guidance for on-call handling."
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Coverage ledger mapping probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    if isinstance(result.get("report"), dict):
        coverage = result["report"]["coverage"]

        assert coverage["sub_questions"][0]["sub_question_id"] == "sq1"
        assert coverage["sub_questions"][0]["question"] == "Explain checkpoint resume semantics"
        assert coverage["sub_questions"][0]["covered"] is True
        assert coverage["sub_questions"][0]["section_ids"] == ["resume-semantics"]
        assert coverage["sub_questions"][0]["claim_ids"] == ["resume-semantics-claim-1"]
        assert coverage["sub_questions"][0]["supporting_evidence_ids"] == [
            "evidence-unit-search-1-fetch-1",
            "evidence-unit-search-1-search",
        ]
        assert coverage["sub_questions"][0]["supporting_source_ids"] == ["R1"]
        assert coverage["sub_questions"][0]["explain_via"] == "explicit_question_binding"
        assert coverage["sub_questions"][1]["sub_question_id"] == "sq2"
        assert coverage["sub_questions"][1]["question"] == "Explain restart trade-offs"
        assert coverage["sub_questions"][1]["covered"] is False
        assert coverage["sub_questions"][1]["section_ids"] == []
        assert coverage["sub_questions"][1]["claim_ids"] == []
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_report_coverage_exposes_hard_gate_flag_when_only_executive_summary_is_answered(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = [
            "Explain checkpoint resume semantics",
            "Explain restart trade-offs",
        ]
        payload["brief"]["coverage_checklist"] = [
            "Explain checkpoint resume semantics",
            "Explain restart trade-offs",
        ]
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
            {"id": "sq2", "question": "Explain restart trade-offs", "reason": "Secondary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint, while restart replays work from a fresh starting point.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint docs.",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", lambda url: asyncio.sleep(0, result=None))

    response = await runtime.start(query="Coverage hard gate probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert result["report"]["coverage"]["coverage_gate_passed"] is False
    assert result["report"]["status"] == "failed"
    assert "coverage_incomplete" in result["report"]["runtime"]["warnings"]


@pytest.mark.asyncio
async def test_claims_include_evidence_bindings_in_final_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Evidence binding probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    claim = result["report"]["sections"][0]["claims"][0]

    assert claim["evidence_bindings"]
    assert claim["evidence_bindings"][0]["source_id"] == "R1"
    assert claim["evidence_bindings"][0]["source_url"] == "https://docs.example.com/runtime/checkpoints"
    assert claim["evidence_bindings"][0]["excerpt"]
    assert claim["evidence_bindings"][0]["excerpt_hash"]
    assert claim["evidence_bindings"][0]["line_start"] == 3
    assert claim["evidence_bindings"][0]["line_end"] == 3
    assert claim["evidence_bindings"][0]["evidence_kind"] == "fetch"
    assert claim["evidence_bindings"][0]["winner_provider"] == "grok"
    assert claim["evidence_bindings"][0]["excerpt_origin"] == "source_text"
    assert claim["evidence_bindings"][0]["source_backed"] is True


def test_release_gate_rejects_claim_with_stale_evidence_binding():
    sections = [
        {
            "section_id": "resume-semantics",
            "title": "Resume Semantics",
            "claims": [
                {
                    "claim_id": "resume-semantics-claim-1",
                    "text": "Resume continues from the last checkpoint.",
                    "citations": ["R1"],
                    "confidence": "medium",
                    "evidence_bindings": [
                        {
                            "evidence_id": "e1",
                            "source_id": "R2",
                            "source_url": "https://docs.example.com/runtime/stale",
                            "evidence_kind": "fetch",
                            "excerpt": "Resume continues from the last checkpoint.",
                        }
                    ],
                }
            ],
            "citations": ["R1"],
        }
    ]
    source_registry = {
        "R1": {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
            "domain": "docs.example.com",
        }
    }

    grounding = _build_grounding_diagnostics(sections, source_registry)
    release_gate = _build_release_gate({"coverage_gate_passed": True}, grounding)

    assert grounding["missing_evidence_binding_claims"] == 1
    assert release_gate["passed"] is False
    assert release_gate["reason_codes"] == ["missing_evidence_bindings"]


def test_release_gate_rejects_single_source_low_confidence_verifier_findings():
    grounding = {
        "total_claims": 1,
        "ungrounded_claims": 0,
        "single_source_claims": 1,
        "low_confidence_claims": 1,
        "missing_evidence_binding_claims": 0,
    }
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding=grounding,
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "confidence": "low",
                        "supporting_source_count": 1,
                        "evidence_bindings": [
                            {
                                "source_id": "R1",
                                "source_backed": False,
                                "excerpt_origin": "search_answer",
                            }
                        ],
                    }
                ],
            }
        ],
    )
    release_gate = _build_release_gate({"coverage_gate_passed": True}, grounding, verifier)

    assert verifier["reason_codes"] == ["single_source_low_confidence"]
    assert release_gate["passed"] is False
    assert release_gate["reason_codes"] == ["single_source_low_confidence"]


def test_release_gate_degrades_but_does_not_fail_medium_single_source_search_only():
    grounding = {
        "total_claims": 1,
        "ungrounded_claims": 0,
        "single_source_claims": 1,
        "low_confidence_claims": 0,
        "missing_evidence_binding_claims": 0,
    }
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding=grounding,
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "confidence": "medium",
                        "evidence_ids": ["e1"],
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[{"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "search"}],
    )
    release_gate = _build_release_gate({"coverage_gate_passed": True, "hard_coverage_gate_passed": True}, grounding, verifier)

    assert verifier["reason_codes"] == ["medium_single_source_search_only"]
    assert release_gate["passed"] is True
    assert release_gate["reason_codes"] == []
    assert release_gate["soft_reason_codes"] == ["medium_single_source_search_only"]


def test_release_gate_blocks_medium_single_source_search_only_for_official_docs_when_coverage_is_incomplete():
    grounding = {
        "total_claims": 1,
        "ungrounded_claims": 0,
        "single_source_claims": 1,
        "low_confidence_claims": 0,
        "missing_evidence_binding_claims": 0,
    }
    verifier = _build_verifier_diagnostics(
        coverage={
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": True,
            "unanswered_sections": ["Key Differences"],
        },
        grounding=grounding,
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "confidence": "medium",
                        "evidence_ids": ["e1"],
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_DescribeReplicationTasks.html",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
                "quality_tier": "official",
            }
        },
        evidence_items=[{"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "search"}],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )
    release_gate = _build_release_gate(
        {
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": True,
            "unanswered_sections": ["Key Differences"],
        },
        grounding,
        verifier,
        source_policy={"mode": "official_docs_only"},
    )

    assert verifier["reason_codes"] == ["medium_single_source_search_only"]
    assert release_gate["passed"] is False
    assert release_gate["reason_codes"] == ["medium_single_source_search_only"]


def test_verifier_flags_missing_evidence_items_and_mismatched_bindings():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "supporting_source_count": 1,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R2",
                                "source_backed": True,
                                "line_start": 4,
                                "line_end": 5,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[],
    )

    assert verifier["passed"] is False
    assert set(verifier["reason_codes"]) >= {
        "missing_evidence_items",
        "mismatched_binding_source",
        "mismatched_binding_evidence",
    }
    assert verifier["flagged_claim_ids"] == ["resume-semantics-claim-1"]


def test_verifier_flags_invalid_source_backed_span_duplicate_and_low_value_claims():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 2,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "supporting_source_count": 1,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": None,
                                "line_end": 5,
                            }
                        ],
                    },
                    {
                        "claim_id": "resume-semantics-claim-2",
                        "text": "Topics can help you to resolve common issues using both AWS DMS and selected endpoint databases.",
                        "citations": ["R1"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "supporting_source_count": 1,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    },
                    {
                        "claim_id": "resume-semantics-claim-3",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "evidence_ids": ["e3"],
                        "confidence": "medium",
                        "supporting_source_count": 1,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e3",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    },
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R1"], "evidence_kind": "search"},
            {"evidence_id": "e3", "source_ids": ["R1"], "evidence_kind": "search"},
        ],
    )

    assert verifier["passed"] is False
    assert set(verifier["reason_codes"]) >= {
        "invalid_source_backed_span",
        "duplicate_claims",
        "low_value_claims",
        "medium_single_source_search_only",
    }
    assert set(verifier["flagged_claim_ids"]) >= {
        "resume-semantics-claim-1",
        "resume-semantics-claim-2",
        "resume-semantics-claim-3",
    }


def test_verifier_derives_single_source_search_only_from_citations_not_claim_metadata():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 0,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "supporting_source_count": 4,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "search"},
        ],
    )

    assert "medium_single_source_search_only" in verifier["reason_codes"]
    assert verifier["summary"]["medium_single_source_search_only"] == 1


def test_verifier_ignores_intentional_rollup_duplicates_in_summary_sections():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 3,
            "ungrounded_claims": 0,
            "single_source_claims": 3,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 3,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "claims": [
                    {
                        "claim_id": "executive-summary-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "key-findings",
                "title": "Key Findings",
                "claims": [
                    {
                        "claim_id": "key-findings-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "checkpoint-resume-semantics",
                "title": "Checkpoint Resume Semantics",
                "claims": [
                    {
                        "claim_id": "checkpoint-resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                            }
                        ],
                    }
                ],
            },
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
    )

    assert "duplicate_claims" not in verifier["reason_codes"]
    assert verifier["summary"]["duplicate_claims"] == 0


def test_verifier_allows_duplicate_claim_text_across_different_specific_sections():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 2,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "operator-resume",
                "title": "Operator Resume",
                "claims": [
                    {
                        "claim_id": "operator-resume-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "checkpoint-resume",
                "title": "Checkpoint Resume",
                "claims": [
                    {
                        "claim_id": "checkpoint-resume-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R2"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R2",
                                "source_backed": True,
                                "line_start": 12,
                                "line_end": 13,
                            }
                        ],
                    }
                ],
            },
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            },
            "R2": {
                "source_id": "R2",
                "url": "https://docs.example.com/runtime/operators",
                "domain": "docs.example.com",
            },
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
    )

    assert "duplicate_claims" not in verifier["reason_codes"]
    assert verifier["summary"]["duplicate_claims"] == 0


def test_verifier_flags_cross_section_restatement_same_source_different_evidence():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 2,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "operator-resume",
                "title": "Operator Resume",
                "claims": [
                    {
                        "claim_id": "operator-resume-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                                "excerpt_hash": "same-excerpt",
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "checkpoint-resume",
                "title": "Checkpoint Resume",
                "claims": [
                    {
                        "claim_id": "checkpoint-resume-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 12,
                                "line_end": 13,
                                "excerpt_hash": "same-excerpt",
                            }
                        ],
                    }
                ],
            },
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
    )

    assert "duplicate_claims" in verifier["reason_codes"]
    assert verifier["summary"]["duplicate_claims"] == 1
    assert "checkpoint-resume-claim-1" in verifier["flagged_claim_ids"]


def test_verifier_flags_cross_section_conflict():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 2,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "restart-semantics",
                "title": "Restart Semantics",
                "claims": [
                    {
                        "claim_id": "claim-2",
                        "text": "Resume does not continue from the last durable checkpoint after interruption.",
                        "citations": ["R2"],
                        "source_ids": ["R2"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R2",
                                "source_backed": True,
                                "line_start": 12,
                                "line_end": 13,
                            }
                        ],
                    }
                ],
            },
        ],
        source_registry={
            "R1": {"source_id": "R1", "url": "https://docs.example.com/resume", "domain": "docs.example.com"},
            "R2": {"source_id": "R2", "url": "https://docs.example.com/restart", "domain": "docs.example.com"},
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
        section_banks=[],
    )

    assert "conflict" in verifier["reason_codes"]
    assert verifier["summary"]["conflicted_claims"] == 2
    assert {"claim-1", "claim-2"} <= set(verifier["flagged_claim_ids"])


def test_verifier_allows_medium_single_source_search_only_but_marks_single_page_official_doc_risk():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 0,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
                "source_type": "official_docs",
                "quality_tier": "official",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "search"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )

    release_gate = _build_release_gate(
        {"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        {
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
        },
        verifier,
        source_policy={"mode": "official_docs_only"},
    )

    assert "medium_single_source_search_only" not in verifier["reason_codes"]
    assert "official_doc_family_single_page_only" in verifier["reason_codes"]
    assert verifier["summary"]["medium_single_source_search_only"] == 0
    assert verifier["summary"]["official_doc_family_single_page_only"] == 1
    assert release_gate["passed"] is True
    assert release_gate["soft_reason_codes"] == ["official_doc_family_single_page_only"]


def test_release_gate_blocks_single_page_official_doc_risk_when_coverage_incomplete():
    coverage = {
        "coverage_gate_passed": False,
        "hard_coverage_gate_passed": True,
        "uncovered_sub_questions": ["DescribeReplicationTasks RecoveryCheckpoint visibility"],
    }
    grounding = {
        "total_claims": 1,
        "ungrounded_claims": 0,
        "single_source_claims": 1,
        "low_confidence_claims": 0,
        "missing_evidence_binding_claims": 0,
    }
    release_gate = _build_release_gate(
        coverage,
        grounding,
        {
            "passed": False,
            "reason_codes": ["official_doc_family_single_page_only"],
            "summary": {"official_doc_family_single_page_only": 1},
        },
        source_policy={"mode": "official_docs_only"},
    )

    assert release_gate["passed"] is False
    assert release_gate["reason_codes"] == ["official_doc_family_single_page_only"]
    assert release_gate["soft_reason_codes"] == []


def test_verifier_flags_claim_outside_selected_bank_as_soft_packet_reason():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R2"],
                        "source_ids": ["R2"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e2",
                                "source_id": "R2",
                                "source_backed": True,
                                "line_start": 8,
                                "line_end": 9,
                                "section_id": "resume-semantics",
                                "pool_mode": "candidate",
                                "selected_for_section": False,
                                "question_ids": ["sq1"],
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            },
            "R2": {
                "source_id": "R2",
                "url": "https://docs.example.com/runtime/restart",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            },
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1", "e2"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "candidate_packets": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "source_ids": ["R1"],
                        "question_ids": ["sq1"],
                        "unit_id": "unit-search-1",
                        "source_backed": True,
                        "line_span_complete": True,
                        "excerpt_hash": "hash-1",
                        "claim_ids": [],
                    }
                ],
                "rejected_packets": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )
    release_gate = _build_release_gate(
        {"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        {
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
        },
        verifier,
    )

    assert "claim_outside_selected_bank" in verifier["reason_codes"]
    assert "selected_evidence_unused" in verifier["reason_codes"]
    assert "section_packet_mismatch" in verifier["reason_codes"]
    assert verifier["summary"]["claim_outside_selected_bank"] == 1
    assert verifier["summary"]["selected_evidence_unused"] == 1
    assert verifier["summary"]["section_packet_mismatch"] == 1
    assert release_gate["passed"] is False
    assert "claim_outside_selected_bank" in release_gate["reason_codes"]
    assert "section_packet_mismatch" in release_gate["reason_codes"]
    assert "selected_evidence_unused" in release_gate["soft_reason_codes"]


def test_verifier_flags_claim_when_any_binding_spills_outside_selected_bank():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1", "R2"],
                        "source_ids": ["R1", "R2"],
                        "evidence_ids": ["e1", "e2"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 8,
                                "line_end": 9,
                                "section_id": "resume-semantics",
                                "pool_mode": "selected",
                                "selected_for_section": True,
                                "question_ids": ["sq1"],
                            },
                            {
                                "evidence_id": "e2",
                                "source_id": "R2",
                                "source_backed": True,
                                "line_start": 12,
                                "line_end": 13,
                                "section_id": "resume-semantics",
                                "pool_mode": "candidate",
                                "selected_for_section": False,
                                "question_ids": ["sq1"],
                            },
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            },
            "R2": {
                "source_id": "R2",
                "url": "https://docs.example.com/runtime/restart",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            },
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1", "e2"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "candidate_packets": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "source_ids": ["R1"],
                        "question_ids": ["sq1"],
                        "unit_id": "unit-search-1",
                        "source_backed": True,
                        "line_span_complete": True,
                        "excerpt_hash": "hash-1",
                        "claim_ids": ["resume-semantics-claim-1"],
                    }
                ],
                "rejected_packets": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )

    assert "claim_outside_selected_bank" in verifier["reason_codes"]
    assert verifier["summary"]["claim_outside_selected_bank"] == 1


def test_verifier_flags_partially_unused_selected_evidence():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 8,
                                "line_end": 9,
                                "section_id": "resume-semantics",
                                "pool_mode": "selected",
                                "selected_for_section": True,
                                "question_ids": ["sq1"],
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1", "e2"],
                "selected_evidence_ids": ["e1", "e2"],
                "rejected_evidence_ids": [],
                "candidate_packets": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "source_ids": ["R1"],
                        "question_ids": ["sq1"],
                        "unit_id": "unit-search-1",
                        "source_backed": True,
                        "line_span_complete": True,
                        "excerpt_hash": "hash-1",
                        "claim_ids": ["resume-semantics-claim-1"],
                    },
                    {
                        "evidence_id": "e2",
                        "source_ids": ["R1"],
                        "question_ids": ["sq1"],
                        "unit_id": "unit-search-1",
                        "source_backed": True,
                        "line_span_complete": True,
                        "excerpt_hash": "hash-2",
                        "claim_ids": [],
                    },
                ],
                "rejected_packets": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )

    assert "selected_evidence_unused" in verifier["reason_codes"]
    assert verifier["summary"]["selected_evidence_unused"] == 1


def test_verifier_flags_section_packet_mismatch_when_selected_packet_claim_ids_are_stale():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 10,
                                "line_end": 11,
                                "section_id": "resume-semantics",
                                "pool_mode": "selected",
                                "selected_for_section": True,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
                "source_type": "official_docs",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "candidate_packets": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "source_ids": ["R1"],
                        "question_ids": ["sq1"],
                        "unit_id": "unit-search-1",
                        "source_backed": True,
                        "line_span_complete": True,
                        "excerpt_hash": "hash-1",
                        "claim_ids": ["stale-claim-id"],
                    }
                ],
                "rejected_packets": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )

    assert "section_packet_mismatch" in verifier["reason_codes"]
    assert verifier["summary"]["section_packet_mismatch"] == 1


def test_verifier_keeps_medium_single_source_search_only_for_standard_sources():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 0,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": False,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://www.rfc-editor.org/rfc/rfc9000",
                "domain": "www.rfc-editor.org",
                "source_type": "standard",
                "quality_tier": "high_signal",
            }
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "search"},
        ],
        section_banks=[
            {
                "section_id": "resume-semantics",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
    )

    assert "medium_single_source_search_only" in verifier["reason_codes"]
    assert verifier["summary"]["medium_single_source_search_only"] == 1


def test_verifier_flags_unbound_citation_sources_and_evidence_ids():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 0,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Resume continues from the last checkpoint and preserves downstream state.",
                        "citations": ["R1", "R2"],
                        "evidence_ids": ["e1", "e2"],
                        "confidence": "medium",
                        "supporting_source_count": 2,
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 3,
                                "line_end": 4,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.example.com/runtime/checkpoints",
                "domain": "docs.example.com",
            },
            "R2": {
                "source_id": "R2",
                "url": "https://docs.example.com/runtime/state",
                "domain": "docs.example.com",
            },
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
    )

    assert "unbound_citation_sources" in verifier["reason_codes"]
    assert "unbound_evidence_ids" in verifier["reason_codes"]
    assert verifier["summary"]["unbound_citation_sources"] == 1
    assert verifier["summary"]["unbound_evidence_ids"] == 1


def test_extract_relevant_excerpt_with_span_tracks_original_line_range():
    excerpt, line_start, line_end = _extract_relevant_excerpt_with_span(
        "# Runtime checkpoints\n\n"
        "Unrelated overview.\n"
        "Checkpoint resume continues from the last durable checkpoint.\n"
        "Operational filler.\n"
        "Restart replays from a fresh starting point when checkpoint metadata is lost.\n",
        reference_texts=["checkpoint resume semantics", "restart trade-offs"],
        line_limit=2,
        char_limit=400,
        multiline=False,
    )

    assert "Checkpoint resume continues" in excerpt
    assert "Restart replays" in excerpt
    assert line_start == 4
    assert line_end == 6


def test_section_citations_prefer_source_backed_cluster_over_search_only_cluster():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Checkpoint resume semantics",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "brief": {
                "objective": "Checkpoint resume semantics",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [{"id": "sq1", "question": "Checkpoint resume semantics", "reason": "Primary question."}],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "resume-semantics",
                    "title": "Resume Semantics",
                    "goal": "Explain checkpoint resume semantics.",
                }
            ],
            "research_units": [],
            "continuation": {"mode": "fresh"},
            "planner_metadata": {},
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
            "title": "Runtime checkpoints",
            "domain": "docs.example.com",
            "source_type": "official_docs",
            "ranking_reasons": ["official_docs"],
        }
    ]
    sections = _build_section_citations(
        plan,
        [
            {
                "evidence_id": "e-search",
                "unit_id": "unit-search-1",
                "summary": "Checkpoint resume semantics checkpoint resume semantics checkpoint trade-offs.",
                "detail": "Checkpoint resume semantics checkpoint resume semantics checkpoint trade-offs.",
                "source_ids": ["R1"],
                "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                "evidence_kind": "search",
                "weight": 0.9,
            },
            {
                "evidence_id": "e-fetch",
                "unit_id": "unit-search-1",
                "summary": "Resume continues from the last durable checkpoint.",
                "detail": "Resume continues from the last durable checkpoint.",
                "source_ids": ["R1"],
                "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                "evidence_kind": "fetch",
                "weight": 1.0,
                "line_start": 10,
                "line_end": 11,
            },
        ],
        source_registry,
    )

    claim = sections[0]["claims"][0]

    assert claim["text"] == "Resume continues from the last durable checkpoint."
    assert any(binding["source_backed"] for binding in claim["evidence_bindings"])


@pytest.mark.asyncio
async def test_report_release_gate_fails_job_when_non_summary_sections_are_unanswered(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint, while restart replays work from a fresh starting point.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint docs.",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", lambda url: asyncio.sleep(0, result=None))

    response = await runtime.start(query="Coverage hard gate probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    events = await runtime.events(response["job_id"])

    assert result["status"] == "failed"
    assert result["report"]["coverage"]["coverage_gate_passed"] is False
    assert result["report"]["runtime"]["release_gate"]["passed"] is False
    assert result["report"]["runtime"]["release_gate"]["reason_codes"] == ["coverage_incomplete"]
    assert result["report"]["status"] == "failed"
    assert "coverage_incomplete" in result["report"]["runtime"]["warnings"]
    assert any(event["type"] == "job_failed" for event in events["events"])
    assert not any(event["type"] == "job_completed" for event in events["events"])


@pytest.mark.asyncio
async def test_hard_coverage_gate_respects_runtime_coverage_state_for_broad_targets(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = [
            "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
        ]
        payload["brief"]["coverage_checklist"] = list(payload["brief"]["must_cover"])
        payload["report_outline"] = [
            {
                "section_id": "durable-runtime-semantics",
                "title": "Durable Runtime Semantics",
                "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint identity uses stable thread_id and checkpoint_id boundaries, while safe interrupt resume restores durable state before replaying the interrupted node.",
            [
                {
                    "url": "https://docs.example.com/checkpoint-identity",
                    "title": "Checkpoint identity",
                    "description": "Checkpoint identity reference.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.example.com/interrupt-resume",
                    "title": "Interrupt resume",
                    "description": "Interrupt resume reference.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "checkpoint-identity" in url:
            return (
                "# Checkpoint identity\n\n"
                "Checkpoint identity is anchored by thread_id and checkpoint_id.\n"
            )
        return (
            "# Interrupt resume\n\n"
            "Interrupt resume restores durable state before replaying the interrupted node.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    assert result["status"] == "completed"
    assert result["report"]["runtime"]["release_gate"]["passed"] is True
    assert result["report"]["coverage"]["hard_coverage_gate_passed"] is True
    assert result["report"]["coverage"]["hard_uncovered_targets"] == []


@pytest.mark.asyncio
async def test_hard_coverage_gate_does_not_accept_runtime_coverage_without_grounded_report_section():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "brief": {
                "objective": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
                "must_cover": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "coverage_checklist": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "reason": "Primary question.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "durable-runtime-semantics",
                    "title": "Durable Runtime Semantics",
                    "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                }
            ],
            "research_units": [],
            "continuation": {"mode": "fresh"},
            "planner_metadata": {},
        }
    )

    coverage = _coverage_for_report(
        plan,
        [],
        coverage_state={
            "items": [
                {
                    "target": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "matched_unit_ids": ["unit-search-1"],
                    "grounded_source_ids": ["R1"],
                    "candidate_section_ids": [],
                    "satisfied": True,
                }
            ]
        },
    )

    assert coverage["hard_coverage_gate_passed"] is False
    assert coverage["hard_uncovered_targets"] == [
        "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
    ]


def test_hard_coverage_gate_does_not_accept_claim_text_overlap_without_packet_backed_support():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "brief": {
                "objective": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
                "must_cover": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "coverage_checklist": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "reason": "Primary question.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "durable-runtime-semantics",
                    "title": "Durable Runtime Semantics",
                    "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                }
            ],
            "research_units": [],
            "continuation": {"mode": "fresh"},
            "planner_metadata": {},
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "durable-runtime-semantics",
                "title": "Durable Runtime Semantics",
                "summary": "Checkpoint identity and durable resume both matter.",
                "citations": ["R1"],
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Checkpoint identity and safe interrupt resume semantics define how durable runtimes recover state.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": [],
                        "evidence_bindings": [],
                    }
                ],
            }
        ],
        coverage_state={
            "items": [
                {
                    "target": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "matched_unit_ids": ["unit-search-1"],
                    "grounded_source_ids": ["R1"],
                    "candidate_section_ids": ["durable-runtime-semantics"],
                    "satisfied": True,
                }
            ]
        },
        planned_outline=[
            {
                "section_id": "durable-runtime-semantics",
                "title": "Durable Runtime Semantics",
                "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                "question_id": "sq1",
            }
        ],
        section_banks=[
            {
                "section_id": "durable-runtime-semantics",
                "candidate_evidence_ids": [],
                "selected_evidence_ids": [],
                "rejected_evidence_ids": [],
            }
        ],
        evidence_ledger=[],
    )

    assert coverage["hard_coverage_gate_passed"] is False
    assert coverage["hard_uncovered_targets"] == [
        "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
    ]


def test_hard_coverage_gate_does_not_accept_unrelated_section_that_only_shares_grounded_source():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "brief": {
                "objective": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
                "must_cover": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "coverage_checklist": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "reason": "Primary question.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": [
                    "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
                ],
                "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "durable-runtime-semantics",
                    "title": "Durable Runtime Semantics",
                    "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                }
            ],
            "research_units": [],
            "continuation": {"mode": "fresh"},
            "planner_metadata": {},
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "billing-notes",
                "title": "Billing Notes",
                "summary": "Billing quotas are configured separately from workflow recovery.",
                "citations": ["R1"],
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Billing quotas are configured separately from workflow recovery semantics.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 4,
                                "line_end": 5,
                                "section_id": "billing-notes",
                                "pool_mode": "selected",
                                "selected_for_section": True,
                            }
                        ],
                    }
                ],
            }
        ],
        coverage_state={
            "items": [
                {
                    "target": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                    "matched_unit_ids": ["unit-search-1"],
                    "grounded_source_ids": ["R1"],
                    "candidate_section_ids": ["billing-notes"],
                    "satisfied": True,
                }
            ]
        },
        planned_outline=[
            {
                "section_id": "durable-runtime-semantics",
                "title": "Durable Runtime Semantics",
                "goal": "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes.",
                "question_id": "sq1",
            }
        ],
        section_banks=[
            {
                "section_id": "billing-notes",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
            }
        ],
        evidence_ledger=[
            {
                "question_id": "sq1",
                "section_id": "billing-notes",
                "evidence_id": "e1",
                "source_ids": ["R1"],
                "selected_for_section": True,
                "selected_section_ids": ["billing-notes"],
            }
        ],
    )

    assert coverage["hard_coverage_gate_passed"] is False
    assert coverage["hard_uncovered_targets"] == [
        "Compare checkpoint identity, persistence boundaries, and safe interrupt resume semantics in durable runtimes."
    ]
    assert coverage["hard_coverage_targets"][0]["explain_via"] == ""


def test_best_cluster_claim_text_prefers_grounded_detail_over_generic_fetch_heading():
    items = [
        DeepResearchEvidenceItem(
            evidence_id="e1",
            unit_id="unit-fetch-1",
            source_ids=["R1"],
            source_urls=["https://docs.example.com/runtime/checkpoints"],
            summary="Runtime checkpoints",
            detail="Checkpoint state is restored from the last durable checkpoint after interruption.",
            evidence_kind="fetch",
            weight=1.0,
            derived_from_source_url="https://docs.example.com/runtime/checkpoints",
        ),
        DeepResearchEvidenceItem(
            evidence_id="e2",
            unit_id="unit-search-1",
            source_ids=["R1"],
            summary="Use the following CLI snippet to inspect awsdms_txn_state and validate checkpoint metadata.",
            detail="Use the following CLI snippet to inspect awsdms_txn_state and validate checkpoint metadata.",
            evidence_kind="search",
            weight=0.9,
        ),
    ]

    claim_text = deep_research_runtime_module._best_cluster_claim_text(items)

    assert "restored from the last durable checkpoint" in claim_text
    assert "CLI snippet" not in claim_text
    assert claim_text != "Runtime checkpoints"


def test_build_section_citations_does_not_let_executive_summary_steal_specific_section_claims():
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
                {"id": "sq1", "question": "How does checkpoint resume work?", "reason": "Primary axis."},
                {"id": "sq2", "question": "How does restart differ?", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "resume-semantics", "title": "Resume Semantics", "goal": "Explain checkpoint resume semantics."},
                {"section_id": "restart-trade-offs", "title": "Restart Trade-offs", "goal": "Explain restart trade-offs."},
            ],
            "research_units": [
                {
                    "unit_id": "unit-search-1",
                    "unit_type": "search",
                    "title": "Primary search",
                    "goal": "Compare checkpoint resume and restart semantics",
                    "query": "checkpoint resume semantics",
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
            ],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/restart",
            "title": "Restart docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-restart",
            "unit_id": "unit-search-1",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Restart reloads work from a fresh starting point.",
            "detail": "Restart reloads work from a fresh starting point and may replay completed work.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]

    sections = _build_section_citations(plan, evidence_items, source_registry)
    sections_by_id = {section["section_id"]: section for section in sections}

    assert sections_by_id["resume-semantics"]["claims"]
    assert sections_by_id["restart-trade-offs"]["claims"]
    assert sections_by_id["resume-semantics"]["claims"][0]["text"].startswith(
        "Resume continues from the last durable checkpoint"
    )
    assert sections_by_id["restart-trade-offs"]["claims"][0]["text"].startswith(
        "Restart reloads work from a fresh starting point"
    )
    assert sections_by_id["resume-semantics"]["source_ids"] == ["R1"]
    assert sections_by_id["resume-semantics"]["evidence_ids"] == ["evidence-resume"]
    assert sections_by_id["resume-semantics"]["claims"][0]["source_ids"] == ["R1"]
    assert sections_by_id["restart-trade-offs"]["source_ids"] == ["R2"]
    assert sections_by_id["restart-trade-offs"]["evidence_ids"] == ["evidence-restart"]
    assert sections_by_id["restart-trade-offs"]["claims"][0]["source_ids"] == ["R2"]


def test_build_section_citations_prefers_selected_evidence_from_section_banks():
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
                {"id": "sq1", "question": "How does checkpoint resume work?", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "resume-semantics", "title": "Resume Semantics", "goal": "Explain checkpoint resume semantics."},
                {"section_id": "restart-trade-offs", "title": "Restart Trade-offs", "goal": "Explain restart trade-offs."},
            ],
            "research_units": [
                {
                    "unit_id": "unit-search-1",
                    "unit_type": "search",
                    "title": "Primary search",
                    "goal": "Compare checkpoint resume and restart semantics",
                    "query": "checkpoint resume semantics",
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
            ],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/restart",
            "title": "Restart docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume-selected",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-resume-rejected",
            "unit_id": "unit-search-1",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Resume semantics also mention restart wording and broad checkpoint trade-offs.",
            "detail": "Resume semantics also mention restart wording and broad checkpoint trade-offs.",
            "evidence_kind": "fetch",
            "weight": 2.0,
        },
    ]
    section_banks = [
        {
            "section_id": "executive-summary",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "resume-semantics",
            "candidate_evidence_ids": [
                "evidence-resume-selected",
                "evidence-resume-rejected",
            ],
            "selected_evidence_ids": ["evidence-resume-selected"],
            "rejected_evidence_ids": ["evidence-resume-rejected"],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "restart-trade-offs",
            "candidate_evidence_ids": ["evidence-resume-rejected"],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=[],
    )
    sections_by_id = {section["section_id"]: section for section in sections}

    assert sections_by_id["resume-semantics"]["evidence_ids"] == ["evidence-resume-selected"]
    assert sections_by_id["resume-semantics"]["source_ids"] == ["R1"]
    assert "evidence-resume-rejected" not in sections_by_id["resume-semantics"]["evidence_ids"]


def test_build_section_citations_derives_key_findings_from_grounded_sections():
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
                {"id": "sq1", "question": "How does checkpoint resume work?", "reason": "Primary axis."},
                {"id": "sq2", "question": "How does restart differ?", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics", "restart trade-offs"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "resume-semantics", "title": "Resume Semantics", "goal": "Explain checkpoint resume semantics."},
                {"section_id": "restart-trade-offs", "title": "Restart Trade-offs", "goal": "Explain restart trade-offs."},
            ],
            "research_units": [
                {
                    "unit_id": "unit-search-1",
                    "unit_type": "search",
                    "title": "Primary search",
                    "goal": "Compare checkpoint resume and restart semantics",
                    "query": "checkpoint resume semantics",
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
            ],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/restart",
            "title": "Restart docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-restart",
            "unit_id": "unit-search-1",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Restart replays work from a fresh starting point and may re-run completed work.",
            "detail": "Restart replays work from a fresh starting point and may re-run completed work.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    section_banks = [
        {
            "section_id": "executive-summary",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "key-findings",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "resume-semantics",
            "candidate_evidence_ids": ["evidence-resume"],
            "selected_evidence_ids": ["evidence-resume"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "restart-trade-offs",
            "candidate_evidence_ids": ["evidence-restart"],
            "selected_evidence_ids": ["evidence-restart"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=[],
    )
    sections_by_id = {section["section_id"]: section for section in sections}

    assert sections_by_id["key-findings"]["claims"]
    assert "Resume continues from the last durable checkpoint" in sections_by_id["key-findings"]["summary"]
    assert "Restart replays work from a fresh starting point" in sections_by_id["key-findings"]["summary"]


def test_build_section_citations_dedupes_claims_within_a_section_only():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Explain resume semantics across two scoped sections",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Explain resume semantics across two scoped sections",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {"id": "sq1", "question": "Resume semantics for operators", "reason": "Primary axis."},
                {"id": "sq2", "question": "Resume semantics for checkpoints", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["resume semantics operators", "resume semantics checkpoints"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "operator-resume", "title": "Operator Resume", "goal": "Explain operator-side resume semantics."},
                {"section_id": "checkpoint-resume", "title": "Checkpoint Resume", "goal": "Explain checkpoint-side resume semantics."},
            ],
            "research_units": [],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        }
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-operator",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-checkpoint",
            "unit_id": "unit-search-2",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    section_banks = [
        {
            "section_id": "operator-resume",
            "candidate_evidence_ids": ["evidence-operator"],
            "selected_evidence_ids": ["evidence-operator"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-22T00:00:00Z",
        },
        {
            "section_id": "checkpoint-resume",
            "candidate_evidence_ids": ["evidence-checkpoint"],
            "selected_evidence_ids": ["evidence-checkpoint"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-22T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=[],
    )
    sections_by_id = {section["section_id"]: section for section in sections}

    assert sections_by_id["operator-resume"]["claims"]
    assert sections_by_id["checkpoint-resume"]["claims"]
    assert sections_by_id["operator-resume"]["claims"][0]["text"] == sections_by_id["checkpoint-resume"]["claims"][0]["text"]


def test_build_section_citations_rewrites_generic_outline_from_evidence_ledger_question_ids():
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
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
            ],
            "research_units": [],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/restart",
            "title": "Restart docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-restart",
            "unit_id": "unit-search-2",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Restart replays work from a fresh starting point and may re-run completed work.",
            "detail": "Restart replays work from a fresh starting point and may re-run completed work.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    evidence_ledger = [
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
        },
        {
            "ledger_id": "ledger-evidence-restart",
            "evidence_id": "evidence-restart",
            "unit_id": "unit-search-2",
            "question_id": "sq2",
            "origin_query": "restart trade-offs",
            "candidate_section_ids": ["key-findings"],
            "selected_section_id": "key-findings",
            "rejected_section_ids": [],
            "disposition": "selected",
            "disposition_reason": "keyword_overlap",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Restart replays work from a fresh starting point and may re-run completed work.",
            "evidence_kind": "fetch",
            "recorded_at": "2026-04-18T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        evidence_ledger=evidence_ledger,
    )
    section_ids = [section["section_id"] for section in sections]

    assert section_ids == [
        "executive-summary",
        "key-findings",
        "checkpoint-resume-semantics",
        "restart-trade-offs",
    ]


def test_coverage_for_report_requires_packet_backed_support_for_multi_question_binding():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Explain checkpoint identity and resume behavior",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Explain checkpoint identity and resume behavior",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
                "must_cover": [
                    "Explain checkpoint identity in durable workflows.",
                    "Explain resume behavior after worker restart.",
                ],
                "coverage_checklist": [
                    "Explain checkpoint identity in durable workflows.",
                    "Explain resume behavior after worker restart.",
                ],
            },
            "sub_questions": [
                {"id": "sq1", "question": "Explain checkpoint identity in durable workflows.", "reason": "Primary axis."},
                {"id": "sq2", "question": "Explain resume behavior after worker restart.", "reason": "Secondary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint identity durable workflow", "resume behavior worker restart"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "checkpoint-identity", "title": "Checkpoint Identity", "goal": "Explain checkpoint identity in durable workflows."},
                {"section_id": "resume-behavior", "title": "Resume Behavior", "goal": "Explain resume behavior after worker restart."},
            ],
            "research_units": [],
        }
    )

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": "checkpoint-identity",
                "title": "Checkpoint Identity",
                "summary": "Checkpoint identity is preserved across resume boundaries.",
                "question_ids": ["sq1", "sq2"],
                "claims": [
                    {
                        "claim_id": "checkpoint-identity-claim-1",
                        "text": "Checkpoint identity is preserved across resume boundaries.",
                        "citations": ["R1"],
                        "evidence_ids": ["e1"],
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "question_ids": ["sq1", "sq2"],
                            }
                        ],
                    }
                ],
                "citations": ["R1"],
            }
        ],
        section_banks=[
            {
                "section_id": "checkpoint-identity",
                "candidate_evidence_ids": ["e1"],
                "selected_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "last_updated_at": "2026-04-18T00:00:00Z",
            }
        ],
        evidence_ledger=[
            {
                "ledger_id": "ledger-e1",
                "evidence_id": "e1",
                "question_id": "sq1",
                "question_ids": ["sq1", "sq2"],
                "selected_section_id": "checkpoint-identity",
                "candidate_section_ids": ["checkpoint-identity"],
                "rejected_section_ids": [],
                "disposition": "selected",
                "source_ids": ["R1"],
            }
        ],
    )

    sub_questions = {item["sub_question_id"]: item for item in coverage["sub_questions"]}

    assert coverage["covered_sub_question_ids"] == ["sq1", "sq2"]
    assert sub_questions["sq2"]["supporting_evidence_ids"] == ["e1"]
    assert sub_questions["sq2"]["explain_via"] == "explicit_question_binding"


def test_build_section_citations_materializes_open_questions_from_rejected_evidence():
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
                {"id": "sq2", "question": "Postgres restart behavior", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint resume semantics", "postgres restart behavior"],
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
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/postgres",
            "title": "Postgres restart docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-gap",
            "unit_id": "unit-search-2",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/postgres"],
            "summary": "Postgres restart behavior remains unclear and needs confirmation from deeper task-state evidence.",
            "detail": "Postgres restart behavior remains unclear and needs confirmation from deeper task-state evidence.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    evidence_ledger = [
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
        },
        {
            "ledger_id": "ledger-evidence-gap",
            "evidence_id": "evidence-gap",
            "unit_id": "unit-search-2",
            "question_id": "sq2",
            "origin_query": "postgres restart behavior",
            "candidate_section_ids": ["key-findings", "open-questions"],
            "selected_section_id": "",
            "rejected_section_ids": ["key-findings", "open-questions"],
            "disposition": "rejected",
            "disposition_reason": "needs_confirmation",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/postgres"],
            "summary": "Postgres restart behavior remains unclear and needs confirmation from deeper task-state evidence.",
            "evidence_kind": "fetch",
            "recorded_at": "2026-04-18T00:00:00Z",
        },
    ]
    section_banks = [
        {
            "section_id": "executive-summary",
            "candidate_evidence_ids": [],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "key-findings",
            "candidate_evidence_ids": ["evidence-resume", "evidence-gap"],
            "selected_evidence_ids": ["evidence-resume"],
            "rejected_evidence_ids": ["evidence-gap"],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "open-questions",
            "candidate_evidence_ids": ["evidence-gap"],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": ["evidence-gap"],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=evidence_ledger,
    )
    sections_by_id = {section["section_id"]: section for section in sections}

    assert "open-questions" in sections_by_id
    assert sections_by_id["open-questions"]["claims"]
    assert "needs confirmation" in sections_by_id["open-questions"]["summary"].lower()
    assert sections_by_id["open-questions"]["claims"][0]["citations"] == ["R2"]
    assert sections_by_id["open-questions"]["evidence_ids"] == ["evidence-gap"]


def test_build_section_citations_open_questions_only_use_gap_rejected_evidence():
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
                {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
            ],
            "research_units": [],
        }
    )
    source_registry = [
        {
            "source_id": "R0",
            "url": "https://docs.example.com/runtime/summary",
            "title": "Summary docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/gap",
            "title": "Gap docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/off-topic",
            "title": "Off topic docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-summary",
            "unit_id": "unit-search-1",
            "source_ids": ["R0"],
            "source_urls": ["https://docs.example.com/runtime/summary"],
            "summary": "Resume usually continues from the last durable checkpoint.",
            "detail": "Resume usually continues from the last durable checkpoint.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-gap",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/gap"],
            "summary": "Resume edge-case handling remains unclear and needs confirmation.",
            "detail": "Resume edge-case handling remains unclear and needs confirmation.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-off-topic",
            "unit_id": "unit-search-1",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/off-topic"],
            "summary": "Resume duplicates a previously accepted explanation and should be ignored here.",
            "detail": "Resume duplicates a previously accepted explanation and should be ignored here.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    section_banks = [
        {
            "section_id": "executive-summary",
            "candidate_evidence_ids": ["evidence-summary"],
            "selected_evidence_ids": ["evidence-summary"],
            "rejected_evidence_ids": [],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
        {
            "section_id": "open-questions",
            "candidate_evidence_ids": ["evidence-gap", "evidence-off-topic"],
            "selected_evidence_ids": [],
            "rejected_evidence_ids": ["evidence-gap", "evidence-off-topic"],
            "last_updated_at": "2026-04-18T00:00:00Z",
        },
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=[],
    )
    open_questions = next(section for section in sections if section["section_id"] == "open-questions")

    assert open_questions["evidence_ids"] == ["evidence-gap"]
    assert all("ignored here" not in claim["text"].lower() for claim in open_questions["claims"])


def test_coverage_uses_active_outline_question_binding_for_derived_sections():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Investigate checkpoint identity and worker fencing after reconnect",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Investigate checkpoint identity and worker fencing after reconnect",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "Checkpoint identity and worker fencing after reconnect",
                    "reason": "Primary axis.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["checkpoint identity worker fencing reconnect"],
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

    evidence_ledger = [
        {
            "ledger_id": "ledger-evidence-reconnect",
            "evidence_id": "evidence-reconnect",
            "unit_id": "unit-search-1",
            "question_id": "sq1",
            "origin_query": "checkpoint identity worker fencing reconnect",
            "candidate_section_ids": ["key-findings"],
            "selected_section_id": "key-findings",
            "rejected_section_ids": [],
            "disposition": "selected",
            "disposition_reason": "keyword_overlap",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
            "summary": "Recovered reconnect flow keeps the durable checkpoint lineage intact.",
            "evidence_kind": "fetch",
            "recorded_at": "2026-04-18T00:00:00Z",
        }
    ]

    active_outline = build_synthesis_outline(
        plan,
        evidence_ledger=evidence_ledger,
    )
    derived_section = next(section for section in active_outline if str(section.get("question_id", "")) == "sq1")

    coverage = _coverage_for_report(
        plan,
        [
            {
                "section_id": derived_section["section_id"],
                "title": derived_section["title"],
                "summary": "The runtime restores state from the durable checkpoint before continuing execution.",
                "claims": [
                    {
                        "claim_id": f"{derived_section['section_id']}-claim-1",
                        "text": "The runtime restores state from the durable checkpoint before continuing execution.",
                        "citations": ["R1"],
                    }
                ],
                "citations": ["R1"],
            }
        ],
        planned_outline=active_outline,
        evidence_ledger=evidence_ledger,
    )

    assert coverage["answered_section_ids"] == [derived_section["section_id"]]
    assert coverage["covered_sub_question_ids"] == ["sq1"]
    assert coverage["uncovered_sub_questions"] == []
    assert coverage["sub_questions"][0]["supporting_evidence_ids"] == ["evidence-reconnect"]
    assert coverage["sub_questions"][0]["supporting_source_ids"] == ["R1"]
    assert coverage["sub_questions"][0]["explain_via"] == "explicit_question_binding"
    assert coverage["section_coverage"][0]["supporting_claim_ids"] == [f"{derived_section['section_id']}-claim-1"]
    assert coverage["section_coverage"][0]["supporting_source_ids"] == ["R1"]
    assert coverage["section_coverage"][0]["question_id"] == "sq1"


@pytest.mark.asyncio
async def test_runtime_persists_verifier_artifact_and_blocks_single_source_low_confidence_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain restart trade-offs that still need confirmation.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "The exact restart trade-off is unclear and needs confirmation from deeper source material.",
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
        query="Explain restart trade-offs that still need confirmation.",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    verifier = json.loads(runtime.store.read_artifact_text(response["job_id"], "verifier.json") or "{}")

    assert result["status"] == "failed"
    assert "single_source_low_confidence" in verifier["reason_codes"]
    assert "single_source_low_confidence" in result["report"]["runtime"]["verifier"]["reason_codes"]
    assert "single_source_low_confidence" in result["report"]["runtime"]["release_gate"]["reason_codes"]


@pytest.mark.asyncio
async def test_runtime_uses_active_outline_for_coverage_and_key_findings(monkeypatch, tmp_path):
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
    if isinstance(result.get("report"), dict):
        section_ids = [section["section_id"] for section in result["report"]["sections"]]
        coverage = result["report"]["coverage"]
        key_findings = next(section for section in result["report"]["sections"] if section["section_id"] == "key-findings")
        coverage_by_id = {item["section_id"]: item for item in coverage["section_coverage"]}
        assert section_ids == [
            "executive-summary",
            "key-findings",
            "checkpoint-resume-semantics",
            "restart-trade-offs",
        ]
        assert coverage["planned_section_ids"] == section_ids
        assert coverage["unanswered_sections"] == []
        assert key_findings["claims"]
        assert coverage_by_id["checkpoint-resume-semantics"]["selected_evidence_count"] >= 1
        assert coverage_by_id["restart-trade-offs"]["selected_evidence_count"] >= 1
        assert outline_state["root_section_ids"] == section_ids
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_runtime_allows_medium_single_source_search_only_for_clean_official_docs_section(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    query = "Checkpoint resume semantics"

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics using only search-answer support.",
            }
        ]
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
        query=query,
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    verifier = json.loads(runtime.store.read_artifact_text(response["job_id"], "verifier.json") or "{}")

    assert result["status"] == "completed"
    assert result["report"]["status"] == "completed"
    assert "medium_single_source_search_only" not in verifier["reason_codes"]
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["verifier"]["reason_codes"]
    assert result["report"]["runtime"]["release_gate"]["reason_codes"] == []
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["release_gate"]["soft_reason_codes"]
    assert "medium_single_source_search_only" not in result["report"]["runtime"]["warnings"]
@pytest.mark.asyncio
async def test_runtime_flags_high_null_span_ratio_in_release_gate(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume-processing continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume-processing continues from the last durable checkpoint."

    def fake_extract(text, reference_texts, *, line_limit=2, char_limit=400, multiline=False):
        return (
            "Resume-processing continues from the last durable checkpoint.",
            None,
            None,
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)
    monkeypatch.setattr("grok_search.deep_research_runtime._extract_relevant_excerpt_with_span", fake_extract)

    response = await runtime.start(
        query="checkpoint resume semantics",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    verifier = json.loads(runtime.store.read_artifact_text(response["job_id"], "verifier.json") or "{}")

    assert result["status"] == "failed"
    assert "high_null_span_ratio" in result["report"]["runtime"]["warnings"]
    assert "high_null_span_ratio" in result["report"]["runtime"]["release_gate"]["reason_codes"]
    assert "invalid_source_backed_span" in verifier["reason_codes"]


@pytest.mark.asyncio
async def test_runtime_report_exposes_provider_winners(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def search_with_details(query, *, effort="standard"):
        return {
            "answer": "Checkpoint resume semantics: resume-processing continues from the last durable checkpoint.",
            "sources": [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint docs.",
                    "provider": "provider_2",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_name": "provider_2",
            "provider_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)

    response = await runtime.start(query="Checkpoint resume semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    winners = result["report"]["runtime"]["provider_winners"]

    assert winners == [
        {
            "unit_id": "unit-search-1",
            "provider_name": "provider_2",
            "provider_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_unit_provider_path_is_exposed_in_runtime_winners(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"] = [
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Fetch provider docs",
                "goal": "Fetch provider-backed runtime docs.",
                "url": "https://docs.example.com/runtime/fetch",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    async def fetch_with_details(url):
        return {
            "content": "# Runtime docs\n\nFetch provider provenance is preserved.",
            "provider_name": "firecrawl",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.firecrawl.dev",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr(
        "grok_search.deep_research_runtime._fetch_url_with_details",
        fetch_with_details,
        raising=False,
    )

    response = await runtime.start(query="Fetch provider provenance", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert {
        "unit_id": "unit-fetch-1",
        "provider_name": "firecrawl",
        "provider_model": "",
        "effective_model": "",
        "provider_api_url": "https://api.firecrawl.dev",
    } in result["report"]["runtime"]["provider_winners"]


@pytest.mark.asyncio
async def test_generate_plan_with_model_exposes_requested_and_effective_model_trace(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    class FakePlannerProvider:
        def __init__(self):
            self.model = "grok-4.20-expert"
            self._last_success_provider_name = "provider_2"
            self._last_success_provider_model = "grok-4.20-expert"
            self._last_success_provider_api_url = "https://api.example.com/v1"

        def _build_api_headers(self):
            return {}

        async def _execute_completion_with_retry_result(self, headers, payload, render_sources=False):
            return (
                json.dumps(structured_plan_payload(payload_job, payload_continuation.model_dump())),
                {},
            )

    payload_job = None
    payload_continuation = None

    async def fake_build_provider(*, effort="standard"):
        return (
            FakePlannerProvider(),
            {
                "requested_model": "grok-4.20-auto",
                "effective_model": "grok-4.20-expert",
                "resolution": "preferred_profile_match",
                "available_models": ["grok-4.20-expert"],
            },
        )

    monkeypatch.setattr("grok_search.deep_research_runtime._build_runtime_grok_provider", fake_build_provider)
    job = runtime.store.create_job(
        query="Planner trace model selection",
        request_fingerprint="fp-planner-trace-model-selection",
        status="draft",
        phase="planning",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=True,
        force_new=True,
        resolved_budget_seconds=120,
        continued_from_job_id="",
    )
    payload_job = job
    payload_continuation = DeepResearchContinuationState(mode="fresh")

    _, trace = await runtime._generate_plan_with_model(job, payload_continuation)

    assert trace["requested_model"] == "grok-4.20-auto"
    assert trace["effective_model"] == "grok-4.20-expert"
    assert trace["model_resolution"] == "preferred_profile_match"
    assert trace["provider_name"] == "provider_2"
    assert trace["provider_model"] == "grok-4.20-expert"


@pytest.mark.asyncio
async def test_continuation_confirmed_claims_filter_low_value_boilerplate(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Continuation hygiene source",
        checkpoint_sections=[
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "summary": "Checkpoint resume continues from the last durable checkpoint.",
                "claims": [
                    {
                        "claim_id": "executive-summary-claim-1",
                        "text": "Checkpoint resume continues from the last durable checkpoint.",
                        "citations": ["R1"],
                    },
                    {
                        "claim_id": "executive-summary-claim-2",
                        "text": "For more information, see the checkpoint recovery appendix.",
                        "citations": ["R1"],
                    },
                ],
                "citations": ["R1"],
                "confidence": "medium",
            }
        ],
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.confirmed_claims == ["Checkpoint resume continues from the last durable checkpoint."]


@pytest.mark.asyncio
async def test_continuation_confirmed_claims_require_grounded_claims(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Continuation grounded claims source",
        checkpoint_sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "summary": "Checkpoint resume continues from the last durable checkpoint.",
                "claims": [
                    {
                        "claim_id": "resume-semantics-claim-1",
                        "text": "Checkpoint resume continues from the last durable checkpoint.",
                        "citations": ["R1"],
                        "confidence": "medium",
                    },
                    {
                        "claim_id": "resume-semantics-claim-2",
                        "text": "Restart always preserves target state without any replay cost.",
                        "citations": [],
                        "confidence": "high",
                    },
                ],
                "citations": ["R1"],
                "confidence": "medium",
            }
        ],
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.confirmed_claims == ["Checkpoint resume continues from the last durable checkpoint."]


@pytest.mark.asyncio
async def test_continuation_uses_verification_and_coverage_gap_artifacts_for_focus(tmp_path):
    runtime = build_runtime(tmp_path)
    sections = [
        {
            "section_id": "task-visibility",
            "title": "Task Visibility",
            "summary": "DescribeReplicationTasks exposes task status.",
            "claims": [
                {
                    "claim_id": "claim-supported",
                    "text": "DescribeReplicationTasks exposes RecoveryCheckpoint for replication task visibility.",
                    "citations": ["R1"],
                    "source_ids": ["R1"],
                    "evidence_ids": ["e1"],
                    "confidence": "medium",
                },
                {
                    "claim_id": "claim-flagged",
                    "text": "StartReplicationTask always restarts every task from the beginning.",
                    "citations": ["R1"],
                    "source_ids": ["R1"],
                    "evidence_ids": ["e-stale"],
                    "confidence": "medium",
                },
            ],
            "citations": ["R1"],
            "confidence": "medium",
        }
    ]
    source = create_completed_source_job(
        runtime,
        query="AWS DMS continuation source",
        checkpoint_sections=sections,
    )
    runtime.write_artifact(
        source.job_id,
        "verification.json",
        json.dumps(
            {
                "supported_claims": [
                    {
                        "claim_id": "claim-supported",
                        "section_id": "task-visibility",
                        "text": "DescribeReplicationTasks exposes RecoveryCheckpoint for replication task visibility.",
                        "confidence": "medium",
                    }
                ],
                "single_source_claims": [],
                "conflicted_claims": [],
                "unmapped_evidence_ids": [],
                "confidence_by_section": {"task-visibility": "medium"},
                "unresolved_sections": ["StartReplicationTask resume semantics"],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        source.job_id,
        "coverage_gaps.json",
        json.dumps(
            {
                "query": "AWS DMS continuation source",
                "uncovered_sub_questions": ["StartReplicationTask resume-processing semantics"],
                "hard_uncovered_targets": [],
                "follow_up_hints": [
                    {
                        "target": "StartReplicationTask resume-processing semantics",
                        "search_query": "AWS DMS StartReplicationTask resume-processing",
                        "reason": "uncovered_sub_question",
                    }
                ],
            }
        ),
        "application/json",
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.confirmed_claims == [
        "DescribeReplicationTasks exposes RecoveryCheckpoint for replication task visibility."
    ]
    assert "StartReplicationTask resume-processing semantics" in continuation.open_questions
    assert all("always restarts every task" not in claim for claim in continuation.confirmed_claims)


@pytest.mark.asyncio
async def test_continuation_prefers_evidence_artifact_over_reconstructed_evidence(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Evidence artifact continuation source")
    plan_payload = structured_plan_payload(source, {"mode": "fresh"})
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/checkpoints",
            "title": "Runtime checkpoints",
            "domain": "docs.example.com",
            "source_type": "official_docs",
            "ranking_reasons": ["official_docs"],
        }
    ]
    sections = [
        {
            "section_id": "resume-semantics",
            "title": "Resume Semantics",
            "summary": "Resume continues from the last checkpoint.",
            "claims": [
                {
                    "claim_id": "resume-semantics-claim-1",
                    "text": "Resume continues from the last checkpoint.",
                    "citations": ["R1"],
                    "unit_id": "unit-search-1",
                    "evidence_ids": ["evidence-unit-search-1-fetch"],
                    "confidence": "medium",
                }
            ],
            "citations": ["R1"],
            "confidence": "medium",
        }
    ]
    unit_results = {
        "unit-search-1": {
            "summary": "Resume continues from the last checkpoint.",
            "detail": "Resume continues from the last checkpoint.",
            "source_ids": ["R1"],
            "citations": ["R1"],
        }
    }
    runtime.write_artifact_batch(
        source.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps(source_registry),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {item["source_id"]: item for item in source_registry},
                        "sections": sections,
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "query": source.query,
                        "summary": "Resume continues from the last checkpoint.",
                        "sections": sections,
                        "unit_results": unit_results,
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nResume continues from the last checkpoint.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps(
                    [
                        {
                            "evidence_id": "evidence-unit-search-1-fetch",
                            "unit_id": "unit-search-1",
                            "summary": "Resume continues from the last checkpoint.",
                            "detail": "Resume continues from the last checkpoint.",
                            "source_ids": ["R1"],
                            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                            "evidence_kind": "fetch",
                            "derived_from_source_url": "https://docs.example.com/runtime/checkpoints",
                            "line_start": 7,
                            "line_end": 9,
                        }
                    ]
                ),
                "content_type": "application/json",
            },
            ],
            query="Evidence artifact continuation source",
            evidence_items=[
                {
                    "evidence_id": "evidence-unit-search-1-fetch",
                    "unit_id": "unit-search-1",
                    "summary": "Resume continues from the last checkpoint.",
                    "detail": "Resume continues from the last checkpoint.",
                    "source_ids": ["R1"],
                    "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                    "evidence_kind": "fetch",
                    "derived_from_source_url": "https://docs.example.com/runtime/checkpoints",
                    "line_start": 7,
                    "line_end": 9,
                }
            ],
            total_claims=1,
            section_count=1,
        ),
    )
    runtime.store.save_checkpoint(
        source.job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state={
            "plan": plan_payload,
            "completed_unit_ids": ["unit-search-1"],
            "unit_results": unit_results,
            "sources": source_registry,
            "evidence_items": [],
            "sections": sections,
        },
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.carry_forward_evidence == [
        {
            "evidence_id": "evidence-unit-search-1-fetch",
            "unit_id": "unit-search-1",
            "summary": "Resume continues from the last checkpoint.",
            "detail": "Resume continues from the last checkpoint.",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
            "evidence_kind": "fetch",
            "derived_from_source_url": "https://docs.example.com/runtime/checkpoints",
            "line_start": 7,
            "line_end": 9,
        }
    ]


@pytest.mark.asyncio
async def test_continuation_prefers_resolved_batch_evidence_over_current_artifact(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="Continuation current evidence artifact fallback")
    current_evidence = [
        {
            "evidence_id": "evidence-unit-search-1-current",
            "unit_id": "unit-search-1",
            "summary": "Current artifact evidence should outrank checkpoint fallback.",
            "detail": "Current artifact evidence should outrank checkpoint fallback.",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
            "evidence_kind": "fetch",
            "derived_from_source_url": "https://docs.example.com/runtime/checkpoints",
            "line_start": 41,
            "line_end": 44,
        }
    ]
    runtime.write_artifact(
        source.job_id,
        "evidence_items.json",
        json.dumps(current_evidence),
        "application/json",
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.carry_forward_evidence == [
        {
            "evidence_id": "evidence-unit-search-1-search",
            "unit_id": "unit-search-1",
            "summary": "Resume continues from the last checkpoint.",
            "detail": "Resume continues from the last checkpoint.",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/checkpoints"],
        }
    ]


@pytest.mark.asyncio
async def test_runtime_persists_evidence_items_artifact_in_final_batch(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Persist evidence artifact probe", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    persisted = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_items.json") or "[]")

    assert persisted
    fetch_items = [item for item in persisted if item.get("evidence_kind") == "fetch"]

    assert fetch_items
    assert fetch_items[0]["line_start"] == 3
    assert fetch_items[0]["line_end"] == 3


@pytest.mark.asyncio
async def test_completed_result_requires_full_provenance_bundle_for_resolved_batch(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(
        runtime,
        query="Completed bundle provenance contract",
        include_provenance_bundle=False,
    )

    status = await runtime.status(source.job_id)
    result = await runtime.result(source.job_id)

    assert status["resolved_artifact_batch_id"] == ""
    assert result["resolved_artifact_batch_id"] == ""
    assert result["artifact_errors"]["evidence_items.json"] == "missing_required_artifact"
    assert result["artifact_errors"]["coverage.json"] == "missing_required_artifact"
    assert result["artifact_errors"]["grounding.json"] == "missing_required_artifact"
    assert result["artifact_errors"]["verifier.json"] == "missing_required_artifact"


@pytest.mark.asyncio
async def test_resolved_final_batch_rejects_latest_batch_with_mismatched_provenance_sidecars(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Resolved batch provenance sidecar parity",
        request_fingerprint="fp-resolved-batch-provenance-sidecar-parity",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")

    good_batch = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps(
                        {
                            "source_registry": {
                                "R1": {
                                    "source_id": "R1",
                                    "url": "https://good.example.com/runtime/recovery",
                                }
                            },
                            "sections": [],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps(
                        {
                            "summary": "Recovered final batch report",
                            "sections": [],
                            "unit_results": {},
                            "coverage": {
                                "planned_section_ids": [],
                                "answered_section_ids": [],
                                "unanswered_sections": [],
                                "planned_sub_question_ids": [],
                                "covered_sub_question_ids": [],
                                "uncovered_sub_questions": [],
                                "coverage_gate_passed": True,
                                "hard_coverage_gate_passed": True,
                            },
                            "runtime": {
                                "warnings": [],
                                "grounding": {
                                    "total_claims": 0,
                                    "ungrounded_claims": 0,
                                    "single_source_claims": 0,
                                    "missing_evidence_binding_claims": 0,
                                },
                                "verifier": {
                                    "passed": True,
                                    "reason_codes": [],
                                    "flagged_claim_ids": [],
                                    "summary": {
                                        "section_count": 0,
                                        "total_claims": 0,
                                        "low_confidence_claims": 0,
                                        "single_source_claims": 0,
                                        "source_backed_binding_count": 0,
                                        "search_only_binding_count": 0,
                                        "null_span_binding_count": 0,
                                        "missing_evidence_items": 0,
                                        "mismatched_binding_source": 0,
                                        "mismatched_binding_evidence": 0,
                                        "invalid_source_backed_span": 0,
                                        "duplicate_claims": 0,
                                        "low_value_claims": 0,
                                        "medium_single_source_search_only": 0,
                                        "same_domain_off_topic_dominance": 0,
                                    },
                                },
                            },
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final batch report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query=job.query,
        ),
    )

    bad_batch = runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {
                                "source_id": "R1",
                                "url": "https://good.example.com/runtime/recovery",
                            }
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Recovered final batch report",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps([]),
                "content_type": "application/json",
            },
            {
                "kind": "coverage.json",
                "content": json.dumps(
                    {
                        "query": job.query,
                        "planned_section_ids": ["stale-section"],
                        "answered_section_ids": [],
                        "unanswered_sections": ["Stale Section"],
                        "planned_sub_question_ids": [],
                        "covered_sub_question_ids": [],
                        "uncovered_sub_questions": [],
                        "coverage_gate_passed": False,
                        "hard_coverage_gate_passed": False,
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": json.dumps(
                    {
                        "total_claims": 9,
                        "grounded_claims": 0,
                        "ungrounded_claims": 9,
                        "single_source_claims": 9,
                        "low_confidence_claims": 9,
                        "missing_evidence_binding_claims": 9,
                        "total_evidence_bindings": 0,
                        "source_backed_binding_count": 0,
                        "search_only_binding_count": 0,
                        "null_span_binding_count": 0,
                        "grounded_claims_without_source_backed_binding": 0,
                        "sections": [],
                        "sources": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "verifier.json",
                "content": json.dumps(
                    {
                        "passed": False,
                        "reason_codes": ["stale_current_only"],
                        "flagged_claim_ids": [],
                        "summary": {
                            "section_count": 9,
                            "total_claims": 9,
                            "low_confidence_claims": 9,
                            "single_source_claims": 9,
                            "source_backed_binding_count": 0,
                            "search_only_binding_count": 0,
                            "null_span_binding_count": 0,
                            "missing_evidence_items": 9,
                            "mismatched_binding_source": 9,
                            "mismatched_binding_evidence": 9,
                            "invalid_source_backed_span": 9,
                            "duplicate_claims": 9,
                            "low_value_claims": 9,
                            "medium_single_source_search_only": 9,
                            "same_domain_off_topic_dominance": 9,
                            "unbound_citation_sources": 9,
                            "unbound_evidence_ids": 9,
                        },
                    }
                ),
                "content_type": "application/json",
            },
        ],
    )

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)
    coverage_text = runtime.read_artifact_text(job.job_id, "coverage.json")
    verifier_text = runtime.read_artifact_text(job.job_id, "verifier.json")

    assert status["resolved_artifact_batch_id"] == good_batch[0]["metadata"]["batch_id"]
    assert status["artifact_fallback_used"] is True
    assert result["resolved_artifact_batch_id"] == good_batch[0]["metadata"]["batch_id"]
    assert result["artifact_fallback_used"] is True
    assert json.loads(coverage_text or "{}")["coverage_gate_passed"] is True
    assert json.loads(verifier_text or "{}")["reason_codes"] == []
    assert result["report"]["runtime"]["verifier"]["reason_codes"] == []
    assert bad_batch[0]["metadata"]["batch_id"] != good_batch[0]["metadata"]["batch_id"]


@pytest.mark.asyncio
async def test_resolved_final_batch_rejects_latest_batch_with_unknown_provenance_sidecar_keys(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Resolved batch provenance unknown key parity",
        request_fingerprint="fp-resolved-batch-provenance-unknown-key-parity",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")

    good_batch = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps(
                        {
                            "source_registry": {
                                "R1": {
                                    "source_id": "R1",
                                    "url": "https://good.example.com/runtime/recovery",
                                }
                            },
                            "sections": [],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps(
                        {
                            "summary": "Recovered final batch report",
                            "sections": [],
                            "unit_results": {},
                            "coverage": {
                                "planned_section_ids": [],
                                "answered_section_ids": [],
                                "unanswered_sections": [],
                                "planned_sub_question_ids": [],
                                "covered_sub_question_ids": [],
                                "uncovered_sub_questions": [],
                                "coverage_gate_passed": True,
                                "hard_coverage_gate_passed": True,
                            },
                            "runtime": {
                                "warnings": [],
                                "grounding": {
                                    "total_claims": 0,
                                    "ungrounded_claims": 0,
                                    "single_source_claims": 0,
                                    "missing_evidence_binding_claims": 0,
                                },
                                "verifier": {
                                    "passed": True,
                                    "reason_codes": [],
                                    "flagged_claim_ids": [],
                                    "summary": {
                                        "section_count": 0,
                                        "total_claims": 0,
                                        "low_confidence_claims": 0,
                                        "single_source_claims": 0,
                                        "source_backed_binding_count": 0,
                                        "search_only_binding_count": 0,
                                        "null_span_binding_count": 0,
                                        "missing_evidence_items": 0,
                                        "mismatched_binding_source": 0,
                                        "mismatched_binding_evidence": 0,
                                        "invalid_source_backed_span": 0,
                                        "duplicate_claims": 0,
                                        "low_value_claims": 0,
                                        "medium_single_source_search_only": 0,
                                        "same_domain_off_topic_dominance": 0,
                                    },
                                },
                            },
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nRecovered final batch report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query=job.query,
        ),
    )

    runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {
                                "source_id": "R1",
                                "url": "https://good.example.com/runtime/recovery",
                            }
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(
                    {
                        "summary": "Recovered final batch report",
                        "sections": [],
                        "unit_results": {},
                        "coverage": {
                            "planned_section_ids": [],
                            "answered_section_ids": [],
                            "unanswered_sections": [],
                            "planned_sub_question_ids": [],
                            "covered_sub_question_ids": [],
                            "uncovered_sub_questions": [],
                            "coverage_gate_passed": True,
                            "hard_coverage_gate_passed": True,
                        },
                        "runtime": {
                            "warnings": [],
                            "grounding": {
                                "total_claims": 0,
                                "ungrounded_claims": 0,
                                "single_source_claims": 0,
                                "missing_evidence_binding_claims": 0,
                            },
                            "verifier": {
                                "passed": True,
                                "reason_codes": [],
                                "flagged_claim_ids": [],
                                "summary": {
                                    "section_count": 0,
                                    "total_claims": 0,
                                    "low_confidence_claims": 0,
                                    "single_source_claims": 0,
                                    "source_backed_binding_count": 0,
                                    "search_only_binding_count": 0,
                                    "null_span_binding_count": 0,
                                    "missing_evidence_items": 0,
                                    "mismatched_binding_source": 0,
                                    "mismatched_binding_evidence": 0,
                                    "invalid_source_backed_span": 0,
                                    "duplicate_claims": 0,
                                    "low_value_claims": 0,
                                    "medium_single_source_search_only": 0,
                                    "same_domain_off_topic_dominance": 0,
                                },
                            },
                        },
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps([]),
                "content_type": "application/json",
            },
            {
                "kind": "coverage.json",
                "content": json.dumps(
                    {
                        "query": job.query,
                        "planned_section_ids": [],
                        "answered_section_ids": [],
                        "unanswered_sections": [],
                        "planned_sub_question_ids": [],
                        "covered_sub_question_ids": [],
                        "uncovered_sub_questions": [],
                        "coverage_gate_passed": True,
                        "hard_coverage_gate_passed": True,
                        "stale_extra_key": "should invalidate batch",
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "grounding.json",
                "content": json.dumps(
                    {
                        "total_claims": 0,
                        "grounded_claims": 0,
                        "ungrounded_claims": 0,
                        "single_source_claims": 0,
                        "low_confidence_claims": 0,
                        "missing_evidence_binding_claims": 0,
                        "total_evidence_bindings": 0,
                        "source_backed_binding_count": 0,
                        "search_only_binding_count": 0,
                        "null_span_binding_count": 0,
                        "grounded_claims_without_source_backed_binding": 0,
                        "sections": [],
                        "sources": [],
                        "legacy_drift": {"stale": True},
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "verifier.json",
                "content": json.dumps(
                    {
                        "passed": True,
                        "reason_codes": [],
                        "flagged_claim_ids": [],
                        "summary": {
                            "section_count": 0,
                            "total_claims": 0,
                            "low_confidence_claims": 0,
                            "single_source_claims": 0,
                            "source_backed_binding_count": 0,
                            "search_only_binding_count": 0,
                            "null_span_binding_count": 0,
                            "missing_evidence_items": 0,
                            "mismatched_binding_source": 0,
                            "mismatched_binding_evidence": 0,
                            "invalid_source_backed_span": 0,
                            "duplicate_claims": 0,
                            "low_value_claims": 0,
                            "medium_single_source_search_only": 0,
                            "same_domain_off_topic_dominance": 0,
                        },
                    }
                ),
                "content_type": "application/json",
            },
        ],
    )

    status = await runtime.status(job.job_id)

    assert status["resolved_artifact_batch_id"] == good_batch[0]["metadata"]["batch_id"]
    assert status["artifact_fallback_used"] is True


@pytest.mark.asyncio
async def test_resolved_final_batch_rejects_latest_batch_with_invalid_provenance_bundle(tmp_path):
    runtime = build_runtime(tmp_path)
    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume continues from the last durable checkpoint."

    runtime._generate_plan_with_model = planner
    response = await runtime.start(query="Resolved batch invalid provenance bundle parity", force_new=True, schedule=False)
    with patch("grok_search.deep_research_runtime._search_query", search), patch(
        "grok_search.deep_research_runtime._fetch_url", fetch
    ):
        await runtime.run_job(response["job_id"])
    job = runtime.store.get_job(response["job_id"])
    good_batch_id = next(
        artifact.metadata.get("batch_id", "")
        for artifact in runtime.store.list_artifacts(job.job_id)
        if artifact.kind == "report.json"
    )

    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps(
                        {
                            "source_registry": {
                                "R1": {
                                    "source_id": "R1",
                                    "url": "https://docs.example.com/runtime/checkpoints",
                                }
                            },
                            "sections": [
                                {
                                    "section_id": "executive-summary",
                                    "title": "Executive Summary",
                                    "claims": [
                                        {
                                            "claim_id": "executive-summary-claim-1",
                                            "text": "Resume continues from the last checkpoint.",
                                            "citations": ["R9"],
                                            "evidence_ids": ["e404"],
                                            "evidence_bindings": [
                                                {
                                                    "evidence_id": "e404",
                                                    "source_id": "R9",
                                                    "source_backed": True,
                                                    "line_start": 3,
                                                    "line_end": 4,
                                                }
                                            ],
                                        }
                                    ],
                                    "citations": ["R9"],
                                }
                            ],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps(
                        {
                            "summary": "Resume continues from the last checkpoint.",
                            "sections": [
                                {
                                    "section_id": "executive-summary",
                                    "title": "Executive Summary",
                                    "summary": "Resume continues from the last checkpoint.",
                                    "claims": [
                                        {
                                            "claim_id": "executive-summary-claim-1",
                                            "text": "Resume continues from the last checkpoint.",
                                            "citations": ["R9"],
                                            "evidence_ids": ["e404"],
                                            "evidence_bindings": [
                                                {
                                                    "evidence_id": "e404",
                                                    "source_id": "R9",
                                                    "source_backed": True,
                                                    "line_start": 3,
                                                    "line_end": 4,
                                                }
                                            ],
                                        }
                                    ],
                                    "citations": ["R9"],
                                }
                            ],
                            "unit_results": {},
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nResume continues from the last checkpoint.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Resolved batch invalid provenance bundle parity",
            evidence_items=[],
        ),
    )

    status = await runtime.status(job.job_id)

    assert status["resolved_artifact_batch_id"] == good_batch_id
    assert status["artifact_fallback_used"] is True


@pytest.mark.asyncio
async def test_finalizing_result_does_not_resolve_partial_batch_with_current_provenance(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Interrupted partial bundle provenance contract",
        request_fingerprint="fp-interrupted-partial-bundle-provenance",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint="finalizing",
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {
                                "source_id": "R1",
                                "url": "https://good.example.com/runtime/recovery",
                            }
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps(
                    [
                        {
                            "evidence_id": "evidence-unit-search-1-fetch",
                            "unit_id": "unit-search-1",
                            "summary": "Recovered final batch evidence.",
                            "detail": "Recovered final batch evidence.",
                            "source_ids": ["R1"],
                            "source_urls": ["https://good.example.com/runtime/recovery"],
                            "evidence_kind": "fetch",
                            "derived_from_source_url": "https://good.example.com/runtime/recovery",
                            "line_start": 3,
                            "line_end": 4,
                        }
                    ]
                ),
                "content_type": "application/json",
            },
        ],
    )
    runtime.write_artifact(
        job.job_id,
        "coverage.json",
        json.dumps({"query": job.query, "planned_section_ids": ["s1"], "answered_section_ids": []}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "grounding.json",
        json.dumps({"total_claims": 99, "ungrounded_claims": 99}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "verifier.json",
        json.dumps({"passed": False, "reason_codes": ["stale_current_only"]}),
        "application/json",
    )

    status = await runtime.status(job.job_id)
    result = await runtime.result(job.job_id)

    assert persisted[0]["metadata"]["batch_id"]
    assert status["artifact_kinds"] == ["plan.json"]
    assert [artifact["kind"] for artifact in status["artifacts"]] == ["plan.json"]
    assert status["artifact_visibility_reason"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert status["resolved_artifact_batch_id"] == ""
    assert result["artifact_visibility_reason"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["resolved_artifact_batch_id"] == ""
    assert result["final_report"] is None
    assert result["report"] is None
    assert result["sources"] is None
    assert result["citations"] is None
    assert result["evidence_items"] is None
    assert status["operator_summary"]["artifact_visibility_reason"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["operator_summary"]["artifact_visibility_reason"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert status["operator_summary"]["current_checkpoint_kind"] == status["current_checkpoint_kind"]
    assert result["operator_summary"]["current_checkpoint_kind"] == result["current_checkpoint_kind"]
    assert result["artifact_errors"]["coverage.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["grounding.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["verifier.json"] == "unresolved_batch_backed_final_artifacts_hidden"


@pytest.mark.asyncio
async def test_finalizing_result_marks_hidden_final_artifacts_without_claiming_they_are_missing(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Interrupted partial bundle hidden vs missing",
        request_fingerprint="fp-interrupted-partial-bundle-hidden-vs-missing",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com/runtime/recovery"}},
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Recovered final batch report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered final batch report.\n",
                "content_type": "text/markdown",
            },
            {
                "kind": "evidence_items.json",
                "content": json.dumps([]),
                "content_type": "application/json",
            },
        ],
    )
    runtime.write_artifact(
        job.job_id,
        "coverage.json",
        json.dumps({"query": job.query, "planned_section_ids": ["s1"], "answered_section_ids": []}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "grounding.json",
        json.dumps({"total_claims": 99, "ungrounded_claims": 99}),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "verifier.json",
        json.dumps({"passed": False, "reason_codes": ["stale_current_only"]}),
        "application/json",
    )

    result = await runtime.result(job.job_id)

    assert result["artifact_visibility_reason"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["sources.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["citations.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["report.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["final_report.md"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["evidence_items.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["coverage.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["grounding.json"] == "unresolved_batch_backed_final_artifacts_hidden"
    assert result["artifact_errors"]["verifier.json"] == "unresolved_batch_backed_final_artifacts_hidden"


@pytest.mark.asyncio
async def test_stop_policy_does_not_skip_pending_unit_when_only_one_completed_unit_mentions_all_targets(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    search_calls: list[str] = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = [
            "Explain checkpoint resume semantics",
            "Explain restart trade-offs",
        ]
        payload["brief"]["coverage_checklist"] = [
            "Explain checkpoint resume semantics",
            "Explain restart trade-offs",
        ]
        payload["brief"]["stop_policy"] = {
            "stop_on_sufficient_coverage": True,
            "max_search_queries": 2,
            "max_urls_per_search": 0,
            "max_runtime_seconds": 240,
        }
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics", "reason": "Primary question."},
            {"id": "sq2", "question": "Explain restart trade-offs", "reason": "Secondary question."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-tradeoffs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Broad overview",
                "goal": "Explain checkpoint resume semantics and restart trade-offs.",
                "query": "checkpoint resume and restart overview",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Restart details",
                "goal": "Explain restart trade-offs.",
                "query": "restart trade-offs details",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume and restart overview", "restart trade-offs details"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": False},
        }
        return payload

    async def search(query):
        search_calls.append(query)
        if "overview" in query:
            return (
                "Checkpoint resume semantics and restart trade-offs are both important operational concerns.",
                [
                    {
                        "url": "https://docs.example.com/runtime/overview",
                        "title": "Runtime overview",
                        "description": "Broad overview.",
                    }
                ],
            )
        return (
            "Restart trade-offs include replay delay after interruption and extra validation steps.",
            [
                {
                    "url": "https://docs.example.com/runtime/restart",
                    "title": "Restart trade-offs",
                    "description": "Restart-specific guidance.",
                }
            ],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="Coverage stop gate probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert search_calls == [
        "checkpoint resume and restart overview",
        "restart trade-offs details",
    ]
    assert result["report"]["runtime"]["skipped_units"] == []


@pytest.mark.asyncio
async def test_domain_constraints_strip_off_domain_detail_from_unit_results(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["include_domains"] = ["docs.aws.amazon.com"]
        payload["brief"]["scope"]["include_domains"] = ["docs.aws.amazon.com"]
        payload["brief"]["scope"]["allowed_sources"] = ["docs.aws.amazon.com"]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume semantics"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": False},
        }
        return payload

    async def search(query):
        return (
            "Stack Overflow says restart from scratch. See https://stackoverflow.com/questions/123/checkpoint-runtime "
            "and https://repost.aws/questions/example for troubleshooting.",
            [
                {
                    "url": "https://stackoverflow.com/questions/123/checkpoint-runtime",
                    "title": "Checkpoint runtime discussion",
                    "description": "Community discussion.",
                },
                {
                    "url": "https://repost.aws/questions/example",
                    "title": "AWS re:Post runtime troubleshooting",
                    "description": "Troubleshooting page.",
                },
            ],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(
        query="Constraint hygiene probe",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    expected_failed_units = [
        {
            "unit_id": "unit-search-1",
            "unit_type": "search",
            "reason": "empty_search_result_after_constraints",
        }
    ]
    if isinstance(result.get("report"), dict):
        assert "unit-search-1" not in result["report"]["unit_results"]
        assert result["report"]["runtime"]["failed_units"] == expected_failed_units
        assert "domain_constraints_applied" in result["report"]["runtime"]["warnings"]
    else:
        assert result["artifact_errors"]


@pytest.mark.asyncio
async def test_official_doc_constraints_preserve_search_evidence_packets_when_allowlisted_source_survives(
    monkeypatch, tmp_path
):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["include_domains"] = ["docs.aws.amazon.com"]
        payload["brief"]["scope"]["include_domains"] = ["docs.aws.amazon.com"]
        payload["brief"]["scope"]["allowed_sources"] = ["docs.aws.amazon.com"]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics from official docs.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Resume search",
                "goal": "Explain AWS DMS checkpoint resume semantics from official docs.",
                "query": "aws dms checkpoint resume semantics official docs",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["aws dms checkpoint resume semantics official docs"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        return (
            "AWS DMS resume-processing continues from the last recovery checkpoint when checkpoint metadata remains available.",
            [
                {
                    "url": "https://example.com/community/aws-dms-checkpoint-resume",
                    "title": "AWS DMS checkpoint resume semantics deep dive",
                    "description": "Third-party summary with matching query words.",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "Official AWS DMS API reference for resume-processing.",
                },
            ],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)
    monkeypatch.setattr(
        "grok_search.deep_research_runtime._select_fetch_sources",
        lambda sources, plan, unit: list(sources),
    )

    response = await runtime.start(
        query="AWS DMS checkpoint resume semantics official docs",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    evidence_ledger = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_ledger.json") or "[]")
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")
    claims = [
        claim
        for section in result["report"]["sections"]
        for claim in section.get("claims", [])
    ]

    assert evidence_ledger
    assert any(entry.get("source_ids") == ["R1"] for entry in evidence_ledger)
    assert any(bank.get("selected_packets") for bank in section_banks)
    assert claims
    assert {
        source["url"]
        for source in result["citations"]["source_registry"].values()
    } == {"https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html"}
    assert all(binding.get("source_id") == "R1" for claim in claims for binding in claim.get("evidence_bindings", []))


@pytest.mark.asyncio
async def test_runtime_stop_gate_can_skip_followup_units_with_research_stage_packet_backing(
    monkeypatch, tmp_path
):
    runtime = build_runtime(tmp_path)
    search_calls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["coverage_checklist"] = ["AWS DMS runtime semantics"]
        payload["sub_questions"] = [
            {"id": "sq1", "question": "AWS DMS runtime semantics", "reason": "Primary axis."},
        ]
        payload["report_outline"] = [
            {
                "section_id": "txn-state-persistence",
                "title": "Transaction State Persistence",
                "goal": "Explain awsdms_txn_state persistence and recovery behavior.",
            },
            {
                "section_id": "recovery-timeout",
                "title": "Recovery Timeout",
                "goal": "Explain RecoveryTimeout semantics and failure conditions.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Broad runtime overview",
                "goal": "Summarize AWS DMS runtime semantics.",
                "query": "aws dms runtime semantics overview",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-search-2",
                "unit_type": "search",
                "title": "Transaction state details",
                "goal": "Explain awsdms_txn_state persistence and recovery behavior.",
                "query": "awsdms_txn_state persistence recovery behavior",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": [
                "aws dms runtime semantics overview",
                "awsdms_txn_state persistence recovery behavior",
            ],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        search_calls.append(query)
        if "overview" in query:
            return (
                "AWS DMS runtime semantics include resume-processing and restart behavior for replication tasks.",
                [
                    {
                        "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                        "title": "Creating tasks for ongoing replication using AWS DMS",
                        "description": "Overview of runtime semantics.",
                    }
                ],
            )
        return (
            "TaskRecoveryTableEnabled creates the awsdms_txn_state table so AWS DMS can persist transaction state for recovery.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.TargetMetadata.html",
                    "title": "Target metadata task settings",
                    "description": "Documents TaskRecoveryTableEnabled and awsdms_txn_state.",
                }
            ],
        )

    async def no_fetch(url):
        return None

    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "1")
    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)

    response = await runtime.start(query="AWS DMS runtime semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    section_banks = json.loads(runtime.store.read_artifact_text(response["job_id"], "section_banks.json") or "[]")

    assert search_calls == ["aws dms runtime semantics overview"]
    assert result["report"]["runtime"]["skipped_units"] == [
        {
            "unit_id": "unit-search-2",
            "unit_type": "search",
            "reason": "sufficient_coverage_reached",
        }
    ]
    assert any(bank.get("selected_packets") for bank in section_banks)


@pytest.mark.asyncio
async def test_unused_sources_are_pruned_from_final_report_registry(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["research_units"][0]["goal"] = "Explain checkpoint resume semantics."
        payload["research_units"][0]["query"] = "checkpoint resume semantics"
        payload["search_strategy"]["search_queries"] = ["checkpoint resume semantics"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                },
                {
                    "url": "https://docs.example.com/runtime/background",
                    "title": "Background runtime notes",
                    "description": "Related but unused docs.",
                },
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Unused source pruning probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    source_urls = {item["url"] for item in result["citations"]["source_registry"].values()}

    assert source_urls == {"https://docs.example.com/runtime/checkpoints"}
    assert "runtime/background" not in result["final_report"]


@pytest.mark.asyncio
async def test_executive_summary_omits_gap_claims_when_supported_claim_exists(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize checkpoint resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint for previously executed tasks.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Primary docs.",
                },
                {
                    "url": "https://docs.example.com/runtime/troubleshooting",
                    "title": "Troubleshooting runtime checkpoints",
                    "description": "Following, you can find topics about troubleshooting runtime issues.",
                },
            ],
        )

    async def fetch(url):
        if "troubleshooting" in url:
            return (
                "# Troubleshooting runtime checkpoints\n\n"
                "Following, you can find topics about troubleshooting runtime issues.\n"
                "These topics can help you to resolve common issues.\n"
                "If you opened a support case, your engineer might ask you to run a support script.\n"
            )
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint for previously executed tasks."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Executive summary gap suppression", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    first_section = result["report"]["sections"][0]

    assert len(first_section["claims"]) == 1
    assert first_section["claims"][0]["cluster_type"] != "gap"
    assert "support script" not in first_section["summary"].lower()
    assert "support script" not in result["final_report"].lower()


@pytest.mark.asyncio
async def test_claims_suppress_for_more_information_boilerplate_when_real_claim_exists(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Use resume-processing to continue from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        return (
            "# Runtime checkpoints\n\n"
            "Use resume-processing to continue from the last durable checkpoint.\n\n"
            "For more information, see the checkpoint recovery appendix.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Boilerplate claim suppression probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    claim_texts = [
        claim["text"].lower()
        for section in result["report"]["sections"]
        for claim in section["claims"]
    ]

    assert any("resume-processing" in claim for claim in claim_texts)
    assert not any("for more information" in claim for claim in claim_texts)


@pytest.mark.asyncio
async def test_report_summary_is_synthesized_instead_of_reusing_first_claim(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the operational tradeoff.",
            },
            {
                "section_id": "key-findings",
                "title": "Key Findings",
                "goal": "Compare resume and restart behavior.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from checkpoints without replaying completed work. Restart replays the task from a fresh starting point.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume continues from checkpoints without replaying completed work. Restart replays the task from a fresh starting point."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Summarize restart tradeoffs", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_claim = result["report"]["sections"][0]["claims"][0]["text"]

    assert result["report"]["summary"] != first_claim
    assert "Resume continues" in result["report"]["summary"]
    assert "Restart replays" in result["report"]["summary"]


@pytest.mark.asyncio
async def test_section_clustering_merges_reinforcing_claims_and_sets_confidence(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "checkpoint-resume",
                "title": "Checkpoint Resume",
                "goal": "Explain how resume-processing continues from checkpoints.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume-processing continues from the last completed checkpoint when recovery metadata is still available.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                    "provider": "grok",
                },
                {
                    "url": "https://standards.example.org/runtime/recovery",
                    "title": "Runtime recovery standard",
                    "description": "Standards guidance.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "standards.example.org" in url:
            return "# Runtime recovery standard\n\nResume-processing continues from the last completed checkpoint when recovery metadata is still available."
        return "# Runtime checkpoints\n\nResume-processing continues from the last completed checkpoint when recovery metadata is still available."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Cluster reinforcing resume evidence", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_section = result["report"]["sections"][0]

    assert len(first_section["claims"]) == 1
    assert first_section["confidence"] in {"high", "medium"}
    assert first_section["claim_cluster_count"] == 1
    assert first_section["claims"][0]["cluster_type"] == "consensus"
    assert first_section["claims"][0]["supporting_source_count"] >= 2
    assert len(first_section["claims"][0]["citations"]) >= 2
    assert result["report"]["confidence"] in {"high", "medium"}


@pytest.mark.asyncio
async def test_section_summary_flows_into_report_and_final_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the recovery tradeoff.",
            },
            {
                "section_id": "operational-impact",
                "title": "Operational Impact",
                "goal": "Explain runtime recovery behavior.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last completed checkpoint. Restart replays the task from a fresh starting point and increases recovery time.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nResume continues from the last completed checkpoint. Restart replays the task from a fresh starting point and increases recovery time."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="section summary probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_section = result["report"]["sections"][0]
    first_claim = first_section["claims"][0]["text"]

    assert first_section["summary"]
    assert first_section["summary"] != first_claim
    assert "Resume continues" in first_section["summary"]
    assert "Restart replays" in first_section["summary"]
    assert f"## {first_section['title']}\n\n{first_section['summary']}\n" in result["final_report"]


@pytest.mark.asyncio
async def test_report_summary_combines_multiple_answered_sections(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the key operational answer.",
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "recovery-timeout",
                "title": "Recovery Timeout",
                "goal": "Explain RecoveryTimeout behavior.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last durable checkpoint. RecoveryTimeout defaults to waiting indefinitely unless a bound is configured.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "Resume-processing semantics.",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ChangeProcessingTuning.html",
                    "title": "Change processing tuning settings",
                    "description": "RecoveryTimeout behavior.",
                },
            ],
        )

    async def fetch(url):
        if "ChangeProcessingTuning" in url:
            return "# Change processing tuning settings\n\nRecoveryTimeout defaults to waiting indefinitely unless a bound is configured."
        return "# StartReplicationTask\n\nResume-processing continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS resume and RecoveryTimeout", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert (
        "Resume continues from the last durable checkpoint" in result["report"]["summary"]
        or "Resume-processing continues from the last durable checkpoint" in result["report"]["summary"]
    )
    assert "RecoveryTimeout defaults to waiting indefinitely" in result["report"]["summary"]
    assert result["report"]["summary"].count("RecoveryTimeout defaults to waiting indefinitely") == 1
    assert "Medium confidence: Medium confidence:" not in result["report"]["summary"]


@pytest.mark.asyncio
async def test_key_findings_claims_do_not_clone_concrete_claim_text(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["sub_questions"] = [
            {"id": "sq1", "question": "Explain checkpoint resume semantics.", "reason": "Primary axis."},
            {"id": "sq2", "question": "Explain restart trade-offs.", "reason": "Secondary axis."},
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
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            },
            {
                "section_id": "restart-trade-offs",
                "title": "Restart Trade-offs",
                "goal": "Explain restart trade-offs.",
            },
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume continues from the last durable checkpoint without replaying completed work. Restart replays the task from a fresh starting point and increases recovery time.",
            [
                {
                    "url": "https://docs.example.com/runtime/resume",
                    "title": "Resume docs",
                    "description": "Resume semantics.",
                },
                {
                    "url": "https://docs.example.com/runtime/restart",
                    "title": "Restart docs",
                    "description": "Restart trade-offs.",
                },
            ],
        )

    async def fetch(url):
        if "restart" in url:
            return "# Restart docs\n\nRestart replays the task from a fresh starting point and increases recovery time."
        return "# Resume docs\n\nResume continues from the last durable checkpoint without replaying completed work."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Checkpoint resume and restart trade-offs", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    sections_by_id = {section["section_id"]: section for section in result["report"]["sections"]}
    concrete_sections = [
        section
        for section_id, section in sections_by_id.items()
        if section_id not in {"executive-summary", "key-findings", "open-questions"}
    ]
    concrete_claim_texts = {
        claim["text"]
        for section in concrete_sections
        for claim in section["claims"]
    }
    key_findings_claim_texts = {claim["text"] for claim in sections_by_id["key-findings"]["claims"]}
    key_findings_bindings = [
        binding
        for claim in sections_by_id["key-findings"]["claims"]
        for binding in claim.get("evidence_bindings", [])
    ]

    assert concrete_sections
    assert key_findings_claim_texts
    assert not (key_findings_claim_texts & concrete_claim_texts)
    assert key_findings_bindings
    assert all(binding.get("selected_for_section") for binding in key_findings_bindings)


@pytest.mark.asyncio
async def test_completed_claims_include_provenance_fields_and_final_sources_follow_rank(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Runtime resume relies on checkpointed progress.",
            [
                {
                    "url": "https://stackoverflow.com/questions/1/runtime-checkpoints",
                    "title": "Runtime checkpoint thread",
                    "description": "Community discussion.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official docs.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nOfficial checkpoint docs explain resumable runtime recovery."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Claim provenance and source ordering", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    first_claim = result["report"]["sections"][0]["claims"][0]
    source_lines = [line for line in result["final_report"].splitlines() if line.startswith("- [R")]

    assert first_claim["unit_id"] == "unit-search-1"
    assert first_claim["evidence_ids"]
    assert "docs.example.com/runtime/checkpoints" in source_lines[0]
    assert "official_docs" in source_lines[0]
    assert "reasons:" in source_lines[0]
    assert all("stackoverflow.com" not in line for line in source_lines)


@pytest.mark.asyncio
async def test_domain_constraints_filter_deep_research_sources_and_expose_ranking_reason(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS resume relies on checkpoints documented in official docs.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                    "title": "Creating tasks for ongoing replication using AWS DMS",
                    "description": "Official user guide.",
                    "provider": "grok",
                },
                {
                    "url": "https://repost.aws/questions/example",
                    "title": "AWS re:Post answer",
                    "description": "Community answer.",
                    "provider": "grok",
                },
                {
                    "url": "https://example.com/aws-dms-blog",
                    "title": "AWS DMS blog summary",
                    "description": "Third-party write-up.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "docs.aws.amazon.com" in url:
            return "# AWS DMS user guide\n\nResume continues from the last recovery checkpoint when source logs remain available."
        if "repost.aws" in url:
            return "# AWS re:Post\n\nCommunity answer."
        return "# Blog\n\nThird-party write-up."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="checkpoint resume semantics in aws dms",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    source_registry = result["citations"]["source_registry"]
    source_urls = {item["url"] for item in source_registry.values()}
    first_source = next(iter(source_registry.values()))

    assert source_urls == {"https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html"}
    assert first_source["source_type"] == "official_docs"
    assert "allowlisted_domain" in first_source["ranking_reasons"]
    assert "official_docs" in first_source["ranking_reasons"]
    assert "repost.aws" not in result["final_report"]
    assert "example.com/aws-dms-blog" not in result["final_report"]


@pytest.mark.asyncio
async def test_source_ranking_prefers_standards_and_papers_over_generic_blog(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 3,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume has strong support in standards and papers.",
            [
                {
                    "url": "https://blog.example.com/runtime-checkpoint-post",
                    "title": "Runtime checkpoint blog",
                    "description": "Generic blog summary.",
                    "provider": "grok",
                },
                {
                    "url": "https://standards.example.org/runtime/recovery",
                    "title": "Runtime recovery standard",
                    "description": "Normative runtime recovery guidance.",
                    "provider": "grok",
                },
                {
                    "url": "https://arxiv.org/abs/2404.12345",
                    "title": "Checkpoint Recovery for Distributed Runtimes",
                    "description": "Research paper.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "standards.example.org" in url:
            return "# Runtime recovery standard\n\nResume-processing continues from the last durable checkpoint."
        if "arxiv.org" in url:
            return "# Checkpoint Recovery for Distributed Runtimes\n\nThe paper shows resumable checkpoint recovery reduces replay cost."
        return "# Blog\n\nA generic blog summary."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Rank standards and papers", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    sources = list(result["citations"]["source_registry"].values())
    urls = [item["url"] for item in sources]

    assert urls[0] == "https://standards.example.org/runtime/recovery"
    assert "https://blog.example.com/runtime-checkpoint-post" not in urls
    assert all("blog.example.com" not in url for url in urls)
    assert sources[0]["winner_provider"] == "grok"
    assert sources[0]["citation_count"] >= 1
    assert "standard" in sources[0]["ranking_reasons"]
    if len(sources) > 1:
        assert "paper" in sources[1]["ranking_reasons"]


@pytest.mark.asyncio
async def test_selective_fetch_avoids_same_domain_off_topic_page(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        payload["report_outline"] = [
            {
                "section_id": "dms-semantics",
                "title": "DMS Semantics",
                "goal": "Explain AWS DMS checkpoint resume and restart semantics.",
            }
        ]
        return payload

    async def search(query):
        return (
            "AWS DMS resume relies on checkpoints while restart replays work from a new starting point.",
            [
                {
                    "url": "https://docs.aws.amazon.com/emr/latest/EMR-on-EKS-DevelopmentGuide/jobruns-flink-restart.html",
                    "title": "Restart a Flink job run on Amazon EMR on EKS",
                    "description": "Restart behavior for Flink jobs.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "API StartReplicationTask",
                    "description": "AWS DMS start and resume semantics.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "jobruns-flink-restart" in url:
            return "# Restart a Flink job run on Amazon EMR on EKS\n\nFlink restart restores a job graph from a savepoint."
        return "# API StartReplicationTask\n\nResume-processing continues from the last checkpoint while reload-target restarts from a fresh load."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS checkpoint resume restart semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    source_registry = result["citations"]["source_registry"]

    assert "Resume-processing continues from the last checkpoint" in result["final_report"]
    assert "Flink restart restores a job graph from a savepoint" not in result["final_report"]
    assert "jobruns-flink-restart.html" not in result["final_report"]
    assert all(
        "same_domain_off_topic" not in set(source.get("ranking_penalties") or [])
        for source in source_registry.values()
    )


@pytest.mark.asyncio
async def test_search_backing_source_prefers_dms_namespace_over_same_domain_off_topic_page(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        payload["report_outline"] = [
            {
                "section_id": "dms-semantics",
                "title": "AWS DMS Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume and restart semantics.",
            }
        ]
        return payload

    async def search(query):
        return (
            "AWS DMS resume-processing continues from the last checkpoint while restart uses a fresh starting point.",
            [
                {
                    "url": "https://docs.aws.amazon.com/emr/latest/ReleaseGuide/flink-restart.html",
                    "title": "Restart a Flink job",
                    "description": "Restart behavior for Amazon EMR Flink jobs.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for resume-processing and reload-target behavior.",
                    "provider": "grok",
                },
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(query="AWS DMS checkpoint resume restart semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    evidence_ledger = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_ledger.json") or "[]")

    assert "resume-processing continues from the last checkpoint" in result["final_report"]
    assert any(
        entry.get("source_urls") == ["https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html"]
        for entry in evidence_ledger
        if entry.get("evidence_id") == "evidence-unit-search-1-search"
    )
    assert not any(
        "flink-restart.html" in url
        for entry in evidence_ledger
        for url in entry.get("source_urls", []) or []
    )


@pytest.mark.asyncio
async def test_selective_fetch_prefers_api_reference_over_prescriptive_guidance(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "dms-semantics",
                "title": "AWS DMS Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume, restart, and API-visible task state semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS resume and restart behavior is documented in official pages.",
            [
                {
                    "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/migrate-data-with-aws-dms.html",
                    "title": "Migrate data with AWS DMS",
                    "description": "Prescriptive guidance pattern with migration shell content.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "API StartReplicationTask",
                    "description": "AWS DMS API reference for resume-processing, reload-target, and start-replication.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        fetched_urls.append(url)
        if "API_StartReplicationTask" in url:
            return "# API StartReplicationTask\n\nUse resume-processing to continue from the last checkpoint and reload-target to restart from a fresh load."
        return "# Migrate data with AWS DMS\n\nThis migration pattern describes a broader project shell and cross-service workflow."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS checkpoint resume restart semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == ["https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html"]
    assert "resume-processing" in result["final_report"]
    assert "migration pattern" not in result["final_report"].lower()


@pytest.mark.asyncio
async def test_selective_fetch_avoids_same_domain_prescriptive_guidance_shell_with_low_signal_title(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "dms-semantics",
                "title": "AWS DMS Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume, recovery timeout, awsdms_txn_state, and restart behavior.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS checkpoint resume semantics are described across official AWS documentation.",
            [
                {
                    "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/aws-dms-checkpoint-resume-restart-recovery-timeout.html",
                    "title": "AWS DMS checkpoint resume restart recovery timeout pattern",
                    "description": "AWS DMS checkpoint resume, recovery timeout, awsdms_txn_state, and restart workflow guidance.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for resume-processing and reload-target start behavior.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        fetched_urls.append(url)
        if "prescriptive-guidance" in url:
            return (
                "# 4\n\n"
                "Validate checkpoint information.\n"
                "Follow the migration pattern shell before confirming database-specific task state.\n"
                "For more information about migration shell setup, see the pattern prerequisites.\n"
            )
        return (
            "# StartReplicationTask\n\n"
            "The `resume-processing` start type resumes from the last recovery checkpoint when checkpoint metadata is still available.\n"
            "The `reload-target` start type reloads the target and restarts task execution.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="AWS DMS checkpoint resume recovery timeout awsdms_txn_state restart semantics",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    source_registry = list(result["citations"]["source_registry"].values())

    assert fetched_urls == ["https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html"]
    assert "Validate checkpoint information" not in result["final_report"]
    assert source_registry[0]["url"] == "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html"
    assert source_registry[0]["title"] != "4"


@pytest.mark.asyncio
async def test_selective_fetch_prefers_deeper_official_doc_when_it_uniquely_answers_target_detail(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "dms-recovery-details",
                "title": "AWS DMS Recovery Details",
                "goal": "Explain RecoveryTimeout, awsdms_txn_state, and Postgres restart nuance.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS recovery details require the deeper task settings documentation.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for resume-processing and reload-target start behavior.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ChangeProcessingTuning.html",
                    "title": "Change processing tuning settings",
                    "description": "AWS DMS user guide for RecoveryTimeout, TaskRecoveryTableEnabled, awsdms_txn_state, and PostgreSQL task recovery.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/aws-dms-checkpoint-resume-restart-recovery-timeout.html",
                    "title": "AWS DMS checkpoint resume restart recovery timeout pattern",
                    "description": "Prescriptive guidance shell for a migration pattern.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        fetched_urls.append(url)
        if "ChangeProcessingTuning" in url:
            return (
                "# Change processing tuning settings\n\n"
                "RecoveryTimeout controls how long AWS DMS waits for a checkpoint before failing recovery.\n"
                "TaskRecoveryTableEnabled creates the awsdms_txn_state table for transaction state recovery.\n"
                "PostgreSQL restart behavior depends on retained source logs and task recovery state.\n"
            )
        if "prescriptive-guidance" in url:
            return "# 4\n\nValidate checkpoint information.\n"
        return "# StartReplicationTask\n\nUse resume-processing to continue from the last checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="AWS DMS RecoveryTimeout awsdms_txn_state Postgres restart nuance",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == [
        "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ChangeProcessingTuning.html"
    ]
    assert "TaskRecoveryTableEnabled" in result["final_report"]
    assert "awsdms_txn_state" in result["final_report"]
    assert "Validate checkpoint information" not in result["final_report"]


@pytest.mark.asyncio
async def test_selective_fetch_prefers_exact_operation_identifier_for_replication_visibility(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "task-visibility",
                "title": "Task Visibility",
                "goal": "Explain DescribeReplicationTasks RecoveryCheckpoint visibility.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "DescribeReplicationTasks exposes replication task checkpoint visibility fields in the API response.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for resume-processing and reload-target behavior.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_DescribeReplicationTasks.html",
                    "title": "DescribeReplicationTasks",
                    "description": "AWS DMS API reference for task visibility, RecoveryCheckpoint, and replication task status fields.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        fetched_urls.append(url)
        if "DescribeReplicationTasks" in url:
            return (
                "# DescribeReplicationTasks\n\n"
                "DescribeReplicationTasks returns RecoveryCheckpoint and replication task status metadata."
            )
        return "# StartReplicationTask\n\nUse resume-processing to continue from the last checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="AWS DMS DescribeReplicationTasks RecoveryCheckpoint visibility",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == [
        "https://docs.aws.amazon.com/dms/latest/APIReference/API_DescribeReplicationTasks.html"
    ]
    assert "DescribeReplicationTasks returns RecoveryCheckpoint" in result["final_report"]
    assert "resume-processing" not in result["final_report"]


@pytest.mark.asyncio
async def test_selective_fetch_recognizes_aws_dms_cli_namespace_without_camelcase_identifier(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    fetched_urls = []

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "cli-task-start",
                "title": "AWS DMS CLI task start",
                "goal": "Explain the AWS CLI start-replication-task command for AWS DMS.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS CLI start-replication-task supports AWS DMS start types.",
            [
                {
                    "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/aws-dms-start-replication-task.html",
                    "title": "AWS DMS start replication task pattern",
                    "description": "A broader migration pattern shell.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/cli/latest/reference/dms/start-replication-task.html",
                    "title": "start-replication-task",
                    "description": "AWS CLI reference for the AWS DMS start-replication-task command.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        fetched_urls.append(url)
        if "/cli/latest/reference/dms/" in url:
            return "# start-replication-task\n\nThe AWS CLI command exposes start types for AWS DMS replication tasks."
        return "# Pattern\n\nThis broader migration pattern shell omits the command reference details."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="AWS DMS CLI start replication task official docs",
        include_domains=["docs.aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == ["https://docs.aws.amazon.com/cli/latest/reference/dms/start-replication-task.html"]
    assert "The AWS CLI command exposes start types" in result["final_report"]
    assert "migration pattern shell" not in result["final_report"]


def test_extract_markdown_title_ignores_numeric_headings():
    title = deep_research_runtime_module._extract_markdown_title(
        "# 4\n\nValidate checkpoint information.\n\nResume-processing continues from the last checkpoint."
    )

    assert title == ""


def test_summarize_evidence_text_skips_shell_like_validation_step():
    summary = deep_research_runtime_module._summarize_evidence_text(
        "# 4\n\n"
        "Validate checkpoint information.\n"
        "The `resume-processing` start type resumes from the last recovery checkpoint when checkpoint metadata is still available.\n"
    )

    assert "Validate checkpoint information" not in summary
    assert "resume-processing" in summary


def test_select_fetch_sources_prefers_matching_dms_namespace_over_same_domain_non_dms_page():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Follow up AWS DMS checkpoint resume semantics with official docs only; focus on RecoveryCheckpoint and restart behavior",
            "context": "",
            "effort": "deep",
            "time_budget_seconds": 180,
            "include_domains": ["docs.aws.amazon.com"],
            "exclude_domains": ["repost.aws"],
            "brief": {
                "objective": "Explain AWS DMS RecoveryCheckpoint behavior.",
                "deliverable": "A cited report.",
                "success_criteria": ["Prefer official AWS DMS docs."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "How does AWS DMS expose RecoveryCheckpoint and restart behavior?",
                    "reason": "Primary axis.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["RecoveryCheckpoint site:docs.aws.amazon.com"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "recoverycheckpoint",
                    "title": "RecoveryCheckpoint",
                    "goal": "Explain AWS DMS RecoveryCheckpoint API and restart behavior.",
                }
            ],
            "research_units": [],
        }
    )
    unit = DeepResearchResearchUnit.model_validate(
        {
            "unit_id": "u1",
            "unit_type": "search",
            "title": "RecoveryCheckpoint docs",
            "goal": "Explain AWS DMS RecoveryCheckpoint API and restart behavior.",
            "query": "RecoveryCheckpoint site:docs.aws.amazon.com",
            "depends_on": [],
            "status": "pending",
            "notes": "",
        }
    )

    ranked = deep_research_runtime_module._select_fetch_sources(
        [
            {
                "url": "https://docs.aws.amazon.com/emr/latest/ReleaseGuide/flink-restart.html",
                "title": "RecoveryCheckpoint restart guide for Flink",
                "description": "Checkpoint recovery and restart steps for Amazon EMR Flink jobs.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
            {
                "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                "title": "Creating tasks for ongoing replication using AWS DMS",
                "description": "User guide for AWS DMS CDC task behavior.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
        ],
        plan,
        unit,
    )

    assert ranked[0]["url"] == "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html"


def test_select_fetch_sources_keeps_exact_identifier_aws_doc_even_outside_dms_namespace():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Follow up AWS DMS checkpoint resume semantics with official docs only; focus on RecoveryCheckpoint API visibility",
            "context": "",
            "effort": "deep",
            "time_budget_seconds": 180,
            "include_domains": ["docs.aws.amazon.com"],
            "exclude_domains": ["repost.aws"],
            "brief": {
                "objective": "Explain RecoveryCheckpoint API visibility.",
                "deliverable": "A cited report.",
                "success_criteria": ["Prefer the most identifier-aligned official AWS docs."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "How is RecoveryCheckpoint exposed in AWS DMS APIs?",
                    "reason": "Primary axis.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["RecoveryCheckpoint site:docs.aws.amazon.com"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "recoverycheckpoint",
                    "title": "RecoveryCheckpoint",
                    "goal": "Explain RecoveryCheckpoint API visibility.",
                }
            ],
            "research_units": [],
        }
    )
    unit = DeepResearchResearchUnit.model_validate(
        {
            "unit_id": "u1",
            "unit_type": "search",
            "title": "RecoveryCheckpoint docs",
            "goal": "Explain RecoveryCheckpoint API visibility.",
            "query": "RecoveryCheckpoint site:docs.aws.amazon.com",
            "depends_on": [],
            "status": "pending",
            "notes": "",
        }
    )

    ranked = deep_research_runtime_module._select_fetch_sources(
        [
            {
                "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                "title": "Creating tasks for ongoing replication using AWS DMS",
                "description": "User guide for AWS DMS CDC task behavior.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
            {
                "url": "https://docs.aws.amazon.com/AWSJavaSDK/latest/javadoc/com/amazonaws/services/databasemigrationservice/model/RecoveryCheckpoint.html",
                "title": "RecoveryCheckpoint (AWS SDK for Java)",
                "description": "Typed API model exposing the RecoveryCheckpoint field.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
        ],
        plan,
        unit,
    )

    assert ranked[0]["url"] == "https://docs.aws.amazon.com/AWSJavaSDK/latest/javadoc/com/amazonaws/services/databasemigrationservice/model/RecoveryCheckpoint.html"


def test_select_fetch_sources_prefers_modify_replication_task_for_modify_intent():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Follow up AWS DMS ModifyReplicationTask CdcStartPosition RecoveryCheckpoint semantics with official docs only",
            "context": "",
            "effort": "deep",
            "time_budget_seconds": 180,
            "include_domains": ["docs.aws.amazon.com"],
            "exclude_domains": ["repost.aws"],
            "brief": {
                "objective": "Explain ModifyReplicationTask checkpoint semantics.",
                "deliverable": "A cited report.",
                "success_criteria": ["Prefer operation-specific official AWS docs."],
            },
            "sub_questions": [
                {
                    "id": "sq1",
                    "question": "How does ModifyReplicationTask interact with CdcStartPosition and RecoveryCheckpoint?",
                    "reason": "Primary axis.",
                }
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["ModifyReplicationTask CdcStartPosition RecoveryCheckpoint site:docs.aws.amazon.com"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {
                    "section_id": "modify-replication-task",
                    "title": "ModifyReplicationTask",
                    "goal": "Explain ModifyReplicationTask checkpoint semantics.",
                }
            ],
            "research_units": [],
        }
    )
    unit = DeepResearchResearchUnit.model_validate(
        {
            "unit_id": "u1",
            "unit_type": "search",
            "title": "ModifyReplicationTask docs",
            "goal": "Explain ModifyReplicationTask checkpoint semantics.",
            "query": "ModifyReplicationTask CdcStartPosition RecoveryCheckpoint site:docs.aws.amazon.com",
            "depends_on": [],
            "status": "pending",
            "notes": "",
        }
    )

    ranked = deep_research_runtime_module._select_fetch_sources(
        [
            {
                "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                "title": "StartReplicationTask",
                "description": "AWS DMS API reference for resume-processing and reload-target behavior.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
            {
                "url": "https://docs.aws.amazon.com/cli/latest/reference/dms/modify-replication-task.html",
                "title": "modify-replication-task",
                "description": "AWS CLI reference for ModifyReplicationTask, CdcStartPosition, and task settings updates.",
                "domain": "docs.aws.amazon.com",
                "provider": "grok",
            },
        ],
        plan,
        unit,
    )

    assert ranked[0]["url"] == "https://docs.aws.amazon.com/cli/latest/reference/dms/modify-replication-task.html"


@pytest.mark.asyncio
async def test_selective_fetch_without_outline_preference_still_returns_ranked_sources(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": False,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS checkpoint resume semantics are documented in the API reference.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html",
                    "title": "AWS DMS checkpoint resume troubleshooting",
                    "description": "Checkpoint and resume troubleshooting topics for AWS DMS tasks.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for start, resume-processing, and reload-target semantics.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "Troubleshooting" in url:
            return (
                "# Troubleshooting migration tasks in AWS Database Migration Service\n\n"
                "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).\n"
                "These topics can help you to resolve common issues using both AWS DMS and selected endpoint databases.\n"
            )
        return (
            "# StartReplicationTask\n\n"
            "The `resume-processing` start type resumes from the last recovery checkpoint when checkpoint metadata is still available.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS checkpoint resume semantics without outline preference", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert "resume-processing" in result["final_report"]
    assert "CHAP_Troubleshooting.html" not in result["final_report"]


@pytest.mark.asyncio
async def test_selective_fetch_penalizes_troubleshooting_shell_even_when_keywords_match(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain AWS DMS checkpoint resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS checkpoint resume semantics are documented in the API reference.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html",
                    "title": "AWS DMS checkpoint resume troubleshooting",
                    "description": "Checkpoint and resume troubleshooting topics for AWS DMS tasks.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "AWS DMS API reference for start, resume-processing, and reload-target semantics.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "Troubleshooting" in url:
            return (
                "# Troubleshooting migration tasks in AWS Database Migration Service\n\n"
                "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).\n"
                "These topics can help you to resolve common issues using both AWS DMS and selected endpoint databases.\n"
            )
        return (
            "# StartReplicationTask\n\n"
            "The `resume-processing` start type resumes from the last recovery checkpoint when checkpoint metadata is still available.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="AWS DMS checkpoint resume semantics", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    source_lines = [line for line in result["final_report"].splitlines() if line.startswith("- [R")]

    assert "resume-processing" in result["final_report"]
    assert "CHAP_Troubleshooting.html" not in source_lines[0]
    assert "API_StartReplicationTask.html" in source_lines[0]


@pytest.mark.asyncio
async def test_noisy_marketing_fetch_is_filtered_and_official_docs_rank_first(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "AWS DMS resume relies on checkpoints.",
            [
                {
                    "url": "https://docs.datadoghq.com/infrastructure/resource_catalog/aws_dms_replication_task/",
                    "title": "Aws dms replication task",
                    "description": "The integrated platform for monitoring & security Observability End-to-end, simplified visibility into your stack’s health & performance Infrastructure",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                    "title": "CHAP Task.CDC",
                    "description": "",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "datadoghq.com" in url:
            return (
                "# Aws dms replication task\n\n"
                "The integrated platform for monitoring & security Observability End-to-end, simplified visibility into your stack’s health & performance Infrastructure\n"
                "Sign up or log in.\n"
            )
        return "# CHAP Task.CDC\n\nResume continues from the last recovery checkpoint when source logs remain available."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="checkpoint resume semantics in aws dms", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    source_lines = [line for line in result["final_report"].splitlines() if line.startswith("- [R")]

    assert "The integrated platform for monitoring" not in result["final_report"]
    assert "Sign up or log in." not in result["final_report"]
    assert "Resume continues from the last recovery checkpoint" in result["final_report"]
    assert "docs.aws.amazon.com" in source_lines[0]
    assert all("docs.datadoghq.com" not in line for line in source_lines)


@pytest.mark.asyncio
async def test_multi_fetch_search_unit_uses_unique_evidence_ids_and_keeps_provenance_consistent(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["brief"]["must_cover"] = ["Explain checkpoint identity and interrupt resume semantics."]
        payload["brief"]["coverage_checklist"] = ["Explain checkpoint identity and interrupt resume semantics."]
        payload["report_outline"] = [
            {
                "section_id": "checkpoint-semantics",
                "title": "Checkpoint Semantics",
                "goal": "Explain checkpoint identity and interrupt resume semantics.",
            }
        ]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 2,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Checkpoint identity and interrupt resume semantics are documented in official LangGraph references.",
            [
                {
                    "url": "https://docs.example.com/checkpoint-identity",
                    "title": "Checkpoint identity",
                    "description": "Checkpoint identity reference.",
                    "provider": "grok",
                },
                {
                    "url": "https://docs.example.com/interrupt-resume",
                    "title": "Interrupt resume",
                    "description": "Interrupt resume reference.",
                    "provider": "grok",
                },
            ],
        )

    async def fetch(url):
        if "checkpoint-identity" in url:
            return (
                "# Checkpoint identity\n\n"
                "Checkpoint identity is defined by thread_id and checkpoint_id in the durable runtime.\n"
            )
        return (
            "# Interrupt resume\n\n"
            "Interrupt resume replays the interrupted node after restoring state from the durable checkpoint.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="Explain checkpoint identity and interrupt resume semantics.",
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    evidence_items = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_items.json") or "[]")
    verifier = result["report"]["runtime"]["verifier"]

    fetch_evidence_ids = [
        item["evidence_id"]
        for item in evidence_items
        if item.get("unit_id") == "unit-search-1" and item.get("evidence_kind") == "fetch"
    ]

    assert len(fetch_evidence_ids) == 2
    assert len(fetch_evidence_ids) == len(set(fetch_evidence_ids))
    assert "mismatched_binding_evidence" not in verifier["reason_codes"]
    assert "unbound_citation_sources" not in verifier["reason_codes"]
    assert "unbound_evidence_ids" not in verifier["reason_codes"]


@pytest.mark.asyncio
async def test_cookie_banner_text_is_filtered_from_summary_and_final_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume and restart in AWS DMS have distinct semantics.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                    "title": "Creating tasks for ongoing replication using AWS DMS",
                    "description": "We use essential cookies and similar tools that are necessary to provide our site and services.",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return (
            "# Creating tasks for ongoing replication using AWS DMS\n\n"
            "We use essential cookies and similar tools that are necessary to provide our site and services.\n"
            "We use performance cookies to collect anonymous statistics.\n"
            "Resume continues from the last recovery checkpoint when source logs remain available.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="cookie banner filter probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert "We use essential cookies" not in result["report"]["summary"]
    assert "performance cookies" not in result["report"]["summary"]
    assert "We use essential cookies" not in result["final_report"]
    assert "performance cookies" not in result["final_report"]
    assert "Resume continues from the last recovery checkpoint" in result["final_report"]


@pytest.mark.asyncio
async def test_troubleshooting_shell_text_is_filtered_from_summary_and_final_report(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Resume and restart in AWS DMS have distinct semantics.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Troubleshooting.html",
                    "title": "Troubleshooting migration tasks in AWS Database Migration Service",
                    "description": "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).",
                    "provider": "grok",
                }
            ],
        )

    async def fetch(url):
        return (
            "# Troubleshooting migration tasks in AWS Database Migration Service\n\n"
            "Following, you can find topics about troubleshooting issues with AWS Database Migration Service (AWS DMS).\n"
            "These topics can help you to resolve common issues using both AWS DMS and selected endpoint databases.\n"
            "Resume-processing continues from the last checkpoint for previously executed tasks.\n"
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="troubleshooting shell filter probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert "Following, you can find topics about troubleshooting issues" not in result["report"]["summary"]
    assert "These topics can help you to resolve common issues" not in result["report"]["summary"]
    assert "Following, you can find topics about troubleshooting issues" not in result["final_report"]
    assert "These topics can help you to resolve common issues" not in result["final_report"]
    assert "Resume-processing continues from the last checkpoint" in result["final_report"]


@pytest.mark.asyncio
async def test_start_does_not_reuse_completed_job_with_invalid_plan_json(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(
        0, result=structured_plan_payload(job, continuation)
    )
    fingerprint = runtime._request_fingerprint(
        query="Reuse invalid plan",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    job = runtime.store.create_job(
        query="Reuse invalid plan",
        request_fingerprint=fingerprint,
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", "{bad-json", "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"}}, "sections": []}),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Checkpoint resume summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nCheckpoint resume summary.",
                "content_type": "text/markdown",
            },
            ],
            query="Reuse invalid plan",
        ),
    )

    response = await runtime.start(query="Reuse invalid plan", force_new=False, schedule=False)

    assert response["reused"] is False
    assert response["job_id"] != job.job_id
    assert response["plan"]["query"] == "Reuse invalid plan"


@pytest.mark.asyncio
async def test_start_reuses_older_usable_completed_job_when_latest_match_is_unusable(tmp_path):
    runtime = build_runtime(tmp_path)
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(
        0, result=structured_plan_payload(job, continuation)
    )
    fingerprint = runtime._request_fingerprint(
        query="Reuse older valid job",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    older = runtime.store.create_job(
        query="Reuse older valid job",
        request_fingerprint=fingerprint,
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(older.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(older.job_id, "plan.json", json.dumps(structured_plan_payload(older, {"mode": "fresh"})), "application/json")
    runtime.write_artifact_batch(
        older.job_id,
        with_minimal_provenance_artifacts(
            [
                {
                    "kind": "sources.json",
                    "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/reuse/older"}]),
                    "content_type": "application/json",
                },
                {
                    "kind": "citations.json",
                    "content": json.dumps(
                        {
                            "source_registry": {
                                "R1": {"source_id": "R1", "url": "https://docs.example.com/reuse/older"}
                            },
                            "sections": [],
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "report.json",
                    "content": json.dumps({"summary": "Older usable job.", "sections": [], "unit_results": {}}),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nOlder usable job.",
                    "content_type": "text/markdown",
                },
            ],
            query="Reuse older valid job",
        ),
    )
    newer = runtime.store.create_job(
        query="Reuse older valid job",
        request_fingerprint=fingerprint,
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(newer.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(newer.job_id, "plan.json", "{bad-json", "application/json")
    with runtime.store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
            ("2099-01-01T00:00:00Z", newer.job_id),
        )

    response = await runtime.start(query="Reuse older valid job", force_new=False, schedule=False)

    assert response["reused"] is True
    assert response["job_id"] == older.job_id


@pytest.mark.asyncio
async def test_fallback_plan_records_generation_failure_in_metadata_and_events(tmp_path):
    runtime = build_runtime(tmp_path)

    async def broken_planner(job, continuation):
        raise RuntimeError("planner exploded")

    runtime._generate_plan_with_model = broken_planner

    response = await runtime.start(
        query="Observe planner fallback",
        plan_only=True,
        force_new=True,
        schedule=False,
    )
    events = await runtime.events(response["job_id"])

    assert response["plan"]["planner_metadata"]["planner"] == "fallback"
    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "generation"
    assert "planner exploded" in response["plan"]["planner_metadata"]["fallback_reason"]["error"]
    assert [event["type"] for event in events["events"][:2]] == ["job_created", "planner_fallback"]
    assert any(
        event["type"] == "planner_fallback" and event["data"]["stage"] == "generation"
        for event in events["events"]
    )


@pytest.mark.asyncio
async def test_report_runtime_surfaces_planner_fallback_and_constraint_violations(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["planner_metadata"] = {
            "planner": "fallback",
            "used_fallback": True,
            "fallback_reason": {"stage": "unsafe_plan", "error": "filled_search_query:u1"},
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Allowed docs page.",
                },
                {
                    "url": "https://blog.example.net/runtime/checkpoints",
                    "title": "Checkpoint troubleshooting shell",
                    "description": "Off-domain shell page.",
                },
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(
        query="Planner fallback diagnostics probe",
        include_domains=["docs.example.com"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])

    warnings = result["report"]["runtime"]["warnings"]
    violations = result["report"]["runtime"]["constraint_violations"]

    assert result["planner_fallback_used"] is True
    assert "planner_fallback_used" in result["runtime_warnings"]
    assert result["constraint_violations"] == violations
    assert "planner_fallback_used" in warnings
    assert "domain_constraints_applied" in warnings
    assert violations == [
        {
            "unit_id": "unit-search-1",
            "removed_source_count": 1,
            "reason": "domain_constraints_applied",
        }
    ]


def test_report_summary_uses_explicit_report_confidence_prefix():
    plan = DeepResearchPlan.model_validate(
        structured_plan_payload(
            type(
                "Job",
                (),
                {
                    "query": "Checkpoint resume semantics",
                    "context": "",
                    "effort": "standard",
                    "resolved_budget_seconds": 240,
                },
            )(),
            {"mode": "fresh"},
        )
    )
    sections = [
        {
            "section_id": "executive-summary",
            "title": "Executive Summary",
            "summary": "High confidence: Resume continues from the last durable checkpoint.",
            "confidence": "high",
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Resume continues from the last durable checkpoint.",
                    "citations": ["R1", "R2"],
                }
            ],
        }
    ]

    summary = _build_report_summary(plan, sections, confidence="medium")

    assert summary.startswith("Medium confidence:")


def test_report_rollup_helpers_strip_nested_summary_scaffolding():
    normalized = deep_research_runtime_module._synthesize_rollup_claim_text(
        "Executive Summary",
        "Medium confidence: Key point: Overall, Resume continues from the last durable checkpoint.",
    )
    summary = deep_research_runtime_module._build_section_summary(
        [
            {
                "claim_id": "c1",
                "text": "Key finding: Medium confidence: Resume continues from the last durable checkpoint.",
                "confidence": "medium",
            }
        ]
    )

    assert "Overall, Overall," not in normalized
    assert "Medium confidence: Key point: Key finding:" not in summary
    assert normalized == "Overall, Resume continues from the last durable checkpoint."
    assert summary == "Medium confidence: Key point: Resume continues from the last durable checkpoint."


def test_build_section_prose_dedupes_summary_and_claim_repetition():
    prose = deep_research_runtime_module._build_section_prose(
        {
            "section_id": "resume-semantics",
            "title": "Resume Semantics",
            "summary": "Medium confidence: Resume continues from the last durable checkpoint.",
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Resume continues from the last durable checkpoint.",
                },
                {
                    "claim_id": "c2",
                    "text": "Resume continues from the last durable checkpoint when recovery metadata is still available.",
                },
            ],
        }
    )

    assert prose.count("Resume continues from the last durable checkpoint.") == 0
    assert "Medium confidence:" not in prose
    assert "recovery metadata is still available" in prose


def test_build_section_prose_turns_selected_claims_into_compact_paragraph():
    prose = deep_research_runtime_module._build_section_prose(
        {
            "section_id": "task-recovery",
            "title": "Task Recovery",
            "summary": "Medium confidence: Key point: AWS DMS recovery depends on checkpoint metadata.",
            "claims": [
                {
                    "claim_id": "claim-1",
                    "text": "Excerpt: AWS DMS recovery depends on checkpoint metadata.",
                    "evidence_ids": ["e1"],
                    "citations": ["R1"],
                },
                {
                    "claim_id": "claim-2",
                    "text": "The task settings documentation adds that RecoveryTimeout controls how long recovery waits.",
                    "evidence_ids": ["e2"],
                    "citations": ["R2"],
                },
                {
                    "claim_id": "claim-3",
                    "text": "Selected packet: AWS DMS recovery depends on checkpoint metadata.",
                    "evidence_ids": ["e1"],
                    "citations": ["R1"],
                },
            ],
        }
    )

    assert prose.count("AWS DMS recovery depends on checkpoint metadata") == 1
    assert "Medium confidence:" not in prose
    assert "Key point:" not in prose
    assert "Excerpt:" not in prose
    assert "Selected packet:" not in prose
    assert "RecoveryTimeout controls how long recovery waits" in prose


def test_build_final_report_omits_duplicate_section_summary_when_prose_is_equivalent():
    report = deep_research_runtime_module._build_final_report(
        DeepResearchPlan.model_validate(
            structured_plan_payload(
                type(
                    "Job",
                    (),
                    {
                        "query": "Checkpoint resume semantics",
                        "context": "",
                        "effort": "standard",
                        "resolved_budget_seconds": 240,
                    },
                )(),
                {"mode": "fresh"},
            )
        ),
        [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "summary": "Resume continues from the last durable checkpoint.",
                "prose": "Resume continues from the last durable checkpoint.",
                "claims": [],
            }
        ],
        {},
        "Medium confidence: Resume continues from the last durable checkpoint.",
    )

    assert report.count("Resume continues from the last durable checkpoint.") == 2
    assert "## Resume Semantics\n\nResume continues from the last durable checkpoint.\n\nResume continues" not in report


def test_selected_bank_payload_drops_claim_ids_missing_from_final_sections():
    payload = deep_research_runtime_module._selected_bank_payload(
        section_banks=[
            {
                "section_id": "executive-summary",
                "selected_evidence_ids": ["e1"],
                "candidate_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "claim_ids": ["executive-summary-claim-1", "stale-claim"],
                        "question_ids": [],
                    }
                ],
            }
        ],
        evidence_ledger=[
            {
                "section_id": "executive-summary",
                "evidence_id": "e1",
                "selection_reason": "keyword_overlap",
                "coverage_tags": [],
                "rejected_reason": "",
            }
        ],
        sections=[
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "claims": [
                    {
                        "claim_id": "executive-summary-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                    }
                ],
            }
        ],
    )

    assert payload[0]["selected_rows"][0]["claim_ids"] == ["executive-summary-claim-1"]


def test_selected_bank_payload_clears_claim_ids_for_non_materialized_section():
    payload = deep_research_runtime_module._selected_bank_payload(
        section_banks=[
            {
                "section_id": "stale-section",
                "selected_evidence_ids": ["e1"],
                "candidate_evidence_ids": ["e1"],
                "rejected_evidence_ids": [],
                "selected_packets": [
                    {
                        "evidence_id": "e1",
                        "claim_ids": ["stale-claim-1"],
                        "question_ids": ["sq1"],
                    }
                ],
            }
        ],
        evidence_ledger=[
            {
                "section_id": "stale-section",
                "evidence_id": "e1",
                "selection_reason": "keyword_overlap",
                "coverage_tags": ["sq1"],
                "rejected_reason": "",
            }
        ],
        sections=[
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "claims": [
                    {
                        "claim_id": "executive-summary-claim-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                    }
                ],
            }
        ],
    )

    assert payload[0]["selected_rows"][0]["claim_ids"] == []


def test_rebuild_verified_rollup_sections_filters_rollups_to_supported_claim_inventory():
    rebuilt = deep_research_runtime_module._rebuild_verified_rollup_sections(
        [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "summary": "Overall, supported and unsupported claims appear together.",
                "prose": "",
                "claims": [
                    {
                        "claim_id": "summary-1",
                        "text": "RecoveryTimeout governs recovery wait behavior.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                    },
                    {
                        "claim_id": "summary-2",
                        "text": "Unsupported restart claim.",
                        "citations": ["R2"],
                        "source_ids": ["R2"],
                        "evidence_ids": ["e2"],
                    },
                ],
            },
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "summary": "RecoveryTimeout governs recovery wait behavior.",
                "prose": "",
                "claims": [
                    {
                        "claim_id": "resume-1",
                        "text": "RecoveryTimeout governs recovery wait behavior.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                    }
                ],
            },
        ],
        {
            "supported_claims": [{"claim_id": "resume-1"}],
            "flagged_claim_ids": ["resume-2"],
        },
    )

    summary_section = rebuilt[0]
    assert [claim["claim_id"] for claim in summary_section["claims"]] == ["summary-1"]
    assert "Unsupported restart claim" not in summary_section["summary"]


def test_rebuild_verified_rollup_sections_preserves_generic_claims_when_no_concrete_inventory_exists():
    rebuilt = deep_research_runtime_module._rebuild_verified_rollup_sections(
        [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "summary": "Resume continues from the last checkpoint.",
                "prose": "",
                "claims": [
                    {
                        "claim_id": "summary-1",
                        "text": "Resume continues from the last checkpoint.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        ],
        {
            "supported_claims": [],
            "flagged_claim_ids": [],
        },
    )

    assert [claim["claim_id"] for claim in rebuilt[0]["claims"]] == ["summary-1"]
    assert "Resume continues from the last checkpoint." in rebuilt[0]["summary"]


def test_coverage_gaps_payload_marks_hard_uncovered_targets_as_blocking():
    payload = deep_research_runtime_module._coverage_gaps_payload(
        query="Checkpoint resume semantics",
        coverage={
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": False,
            "unanswered_sections": [],
            "uncovered_sub_questions": [],
            "hard_uncovered_targets": ["Explain RecoveryTimeout behavior."],
        },
    )

    assert payload["hard_gap_count"] == 1
    assert payload["blocking_gap_count"] == 1
    assert payload["gaps"] == [
        {
            "gap_type": "hard_uncovered_target",
            "target": "Explain RecoveryTimeout behavior.",
            "blocking": True,
            "blocking_scope": "hard",
        }
    ]


def test_coverage_gaps_payload_distinguishes_soft_gaps_and_follow_up_hints():
    payload = deep_research_runtime_module._coverage_gaps_payload(
        query="AWS DMS DescribeReplicationTasks RecoveryCheckpoint official docs",
        coverage={
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": True,
            "unanswered_sections": ["Task settings"],
            "uncovered_sub_questions": ["DescribeReplicationTasks RecoveryCheckpoint visibility"],
            "hard_uncovered_targets": [],
        },
    )

    assert payload["blocking_gap_count"] == 0
    assert payload["total_gap_count"] == 2
    assert {gap["blocking_scope"] for gap in payload["gaps"]} == {"soft"}
    assert all(gap["blocking"] is False for gap in payload["gaps"])
    hint_targets = " ".join(hint["target"] for hint in payload["follow_up_hints"])
    assert "DescribeReplicationTasks" in hint_targets
    assert payload["suggested_research_units"][0]["source_policy"]["include_domains"] == ["docs.aws.amazon.com"]


def test_coverage_gaps_matches_report_rejects_partial_modern_sidecar():
    report = {
        "query": "Checkpoint resume semantics",
        "coverage": {
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": True,
            "unanswered_sections": ["Task settings"],
            "uncovered_sub_questions": [],
            "hard_uncovered_targets": [],
        },
    }
    partial_sidecar = {
        "query": "Checkpoint resume semantics",
        "coverage_gate_passed": False,
        "total_gap_count": 1,
    }

    assert (
        deep_research_runtime_module._coverage_gaps_matches_report(
            coverage_gaps_value=partial_sidecar,
            report_value=report,
        )
        is False
    )


def test_coverage_gaps_artifact_shape_rejects_invalid_modern_gap_rows():
    error = deep_research_runtime_module._validate_json_artifact_shape(
        "coverage_gaps.json",
        {
            "query": "Checkpoint resume semantics",
            "unanswered_sections": [],
            "uncovered_sub_questions": [],
            "hard_uncovered_targets": [],
            "coverage_gate_passed": False,
            "hard_coverage_gate_passed": True,
            "blocking_gap_count": 1,
            "hard_gap_count": 0,
            "total_gap_count": 1,
            "gaps": [
                {
                    "gap_type": "unanswered_section",
                    "target": "",
                    "blocking": "false",
                    "blocking_scope": "soft",
                }
            ],
            "follow_up_hints": [],
            "suggested_research_units": [],
        },
    )

    assert error == "invalid_shape"


def test_provider_attempts_payload_preserves_distinct_attempt_roles_and_error_layers():
    attempts = deep_research_runtime_module._provider_attempts_payload(
        unit_results={
            "unit-search-1": {
                "unit_type": "search",
                "provider_name": "primary-grok",
                "provider_api_url": "https://provider.example.invalid/v1",
                "source_ids": ["R1"],
                "supporting_provider_attempts": [
                    {
                        "operation": "fetch",
                        "status": "completed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/extract",
                        "source_count": 1,
                        "evidence_count": 1,
                        "attempt_role": "selective_fetch",
                    },
                    {
                        "operation": "fetch",
                        "status": "completed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/extract",
                        "source_count": 1,
                        "evidence_count": 1,
                        "attempt_role": "selective_fetch",
                    },
                    {
                        "operation": "fetch",
                        "status": "failed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/extract",
                        "source_count": 0,
                        "evidence_count": 0,
                        "error_code": "fetch_failed",
                        "failure_reason": "empty_fetch_result",
                        "attempt_role": "selective_fetch",
                    },
                ],
                "supplemental_attempts": [
                    {
                        "operation": "search",
                        "status": "completed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/search",
                        "source_count": 1,
                        "evidence_count": 0,
                    },
                    {
                        "operation": "search",
                        "status": "completed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/search",
                        "source_count": 1,
                        "evidence_count": 0,
                    },
                ],
            }
        },
        failed_units=[],
        evidence_items=[{"unit_id": "unit-search-1", "evidence_id": "e1"}],
    )

    fetch_attempts = [attempt for attempt in attempts if attempt["operation"] == "fetch"]
    supplemental_attempts = [
        attempt for attempt in attempts if attempt.get("attempt_role") == "supplemental"
    ]
    assert len(fetch_attempts) == 2
    assert {attempt["status"] for attempt in fetch_attempts} == {"completed", "failed"}
    assert next(attempt for attempt in fetch_attempts if attempt["status"] == "failed")["failure_reason"] == "empty_fetch_result"
    assert len(supplemental_attempts) == 1


def test_provider_attempts_payload_keeps_distinct_failed_attempt_contexts():
    attempts = deep_research_runtime_module._provider_attempts_payload(
        unit_results={
            "unit-search-1": {
                "unit_type": "search",
                "provider_name": "primary-grok",
                "provider_api_url": "https://provider.example.invalid/v1",
                "source_ids": ["R1"],
                "supporting_provider_attempts": [
                    {
                        "operation": "fetch",
                        "status": "failed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/extract",
                        "source_count": 0,
                        "evidence_count": 0,
                        "error_code": "fetch_failed",
                        "failure_reason": "empty_fetch_result",
                        "warnings": ["empty_body"],
                        "attempt_role": "selective_fetch",
                    },
                    {
                        "operation": "fetch",
                        "status": "failed",
                        "provider_name": "tavily",
                        "provider_api_url": "https://api.tavily.com/extract",
                        "source_count": 0,
                        "evidence_count": 0,
                        "error_code": "fetch_failed",
                        "failure_reason": "provider_timeout",
                        "warnings": ["timeout"],
                        "attempt_role": "selective_fetch",
                    },
                ],
            }
        },
        failed_units=[],
        evidence_items=[{"unit_id": "unit-search-1", "evidence_id": "e1"}],
    )

    failed_fetch_attempts = [
        attempt for attempt in attempts if attempt["operation"] == "fetch" and attempt["status"] == "failed"
    ]
    assert len(failed_fetch_attempts) == 2
    assert {attempt["failure_reason"] for attempt in failed_fetch_attempts} == {
        "empty_fetch_result",
        "provider_timeout",
    }
    assert {tuple(attempt["warnings"]) for attempt in failed_fetch_attempts} == {
        ("empty_body",),
        ("timeout",),
    }


def test_selected_bank_bundle_gate_rejects_selected_row_outside_selected_evidence_ids():
    matches = deep_research_runtime_module._selected_bank_matches_bundle(
        selected_bank_value=[
            {
                "section_id": "task-visibility",
                "selected_evidence_ids": ["e1"],
                "candidate_evidence_ids": ["e1", "e2"],
                "rejected_evidence_ids": [],
                "selected_rows": [{"evidence_id": "e2", "claim_ids": ["claim-1"]}],
            }
        ],
        report_value={
            "sections": [
                {
                    "section_id": "task-visibility",
                    "claims": [{"claim_id": "claim-1"}],
                }
            ]
        },
        evidence_items_value=[
            {"evidence_id": "e1"},
            {"evidence_id": "e2"},
        ],
    )

    assert matches is False


@pytest.mark.asyncio
async def test_continuation_carries_structured_coverage_gap_units(tmp_path):
    runtime = build_runtime(tmp_path)
    source = create_completed_source_job(runtime, query="AWS DMS continuation source")
    runtime.write_artifact(
        source.job_id,
        "coverage_gaps.json",
        json.dumps(
            {
                "query": "AWS DMS continuation source",
                "gaps": [
                    {
                        "gap_type": "hard_uncovered_target",
                        "target": "CDC guide checkpoint restart behavior",
                        "blocking": True,
                        "blocking_scope": "hard",
                    }
                ],
                "follow_up_hints": [
                    {
                        "target": "CDC guide checkpoint restart behavior",
                        "gap_type": "hard_uncovered_target",
                        "blocking_scope": "hard",
                        "search_query": "AWS DMS CDC checkpoint restart official docs",
                        "source_policy": {"include_domains": ["docs.aws.amazon.com"], "exclude_domains": []},
                    }
                ],
                "suggested_research_units": [
                    {
                        "title": "CDC checkpoint restart behavior",
                        "goal": "Resolve CDC guide coverage gap.",
                        "query": "AWS DMS CDC checkpoint restart official docs",
                        "source_policy": {"include_domains": ["docs.aws.amazon.com"], "exclude_domains": []},
                        "reason": "hard_uncovered_target",
                        "blocking_scope": "hard",
                    }
                ],
            }
        ),
        "application/json",
    )

    continuation = runtime._build_continuation_context(source.job_id)

    assert continuation.coverage_gap_scopes == {"CDC guide checkpoint restart behavior": "hard"}
    assert continuation.follow_up_hints[0]["blocking_scope"] == "hard"
    assert continuation.suggested_research_units[0]["query"] == "AWS DMS CDC checkpoint restart official docs"
    assert continuation.focused_snapshot["suggested_research_units"][0]["blocking_scope"] == "hard"


@pytest.mark.asyncio
async def test_continuation_ignores_incomplete_latest_batch_candidate_when_current_artifacts_exist(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Continuation incomplete batch candidate",
        request_fingerprint="fp-continuation-incomplete-batch",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(structured_plan_payload(job, {"mode": "fresh"})),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R1", "url": "https://docs.example.com/current"}]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "citations.json",
        json.dumps(
            {
                "source_registry": {"R1": {"source_id": "R1", "url": "https://docs.example.com/current"}},
                "sections": [],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps({"query": job.query, "summary": "Current report.", "sections": [], "unit_results": {}}),
        "application/json",
    )
    runtime.write_artifact(job.job_id, "final_report.md", "# Final Report\n\nCurrent final report.", "text/markdown")
    runtime.store.save_checkpoint(
        job.job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state={"completed_unit_ids": [], "sources": [], "sections": [], "unit_results": {}},
    )
    batch_dir = runtime.store.artifacts_dir / job.job_id / "batches" / "20990101T000000-incomplete"
    batch_dir.mkdir(parents=True)
    (batch_dir / "manifest.json").write_text(
        json.dumps(
            {
                "batch_id": "20990101T000000-incomplete",
                "created_at": "2099-01-01T00:00:00+00:00",
                "completeness_ok": False,
            }
        ),
        encoding="utf-8",
    )
    (batch_dir / "final_report.md").write_text(
        "# Final Report\n\nIncomplete latest batch should not be mixed.",
        encoding="utf-8",
    )

    continuation = runtime._build_continuation_context(job.job_id)

    assert continuation.focused_snapshot["artifact_origin_map"]["final_report.md"] == "current_artifact"
    assert "latest_batch_candidate" not in set(continuation.focused_snapshot["artifact_origin_map"].values())
    assert "Incomplete latest batch" not in continuation.previous_summary


@pytest.mark.asyncio
async def test_continuation_does_not_read_stale_additive_sidecars_for_core_batch_candidate(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Continuation core batch candidate",
        request_fingerprint="fp-continuation-core-batch",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps(structured_plan_payload(job, {"mode": "fresh"})), "application/json")
    runtime.write_artifact(
        job.job_id,
        "selected_bank.json",
        json.dumps(
            [
                {
                    "section_id": "stale-section",
                    "selected_evidence_ids": ["stale-evidence"],
                    "selected_rows": [{"evidence_id": "stale-evidence"}],
                }
            ]
        ),
        "application/json",
    )
    runtime.write_artifact_batch(
        job.job_id,
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/core-batch"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {"source_id": "R1", "url": "https://docs.example.com/core-batch"}
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Core batch summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nCore batch summary.",
                "content_type": "text/markdown",
            },
        ],
    )

    continuation = runtime._build_continuation_context(job.job_id)
    origin_map = continuation.focused_snapshot["artifact_origin_map"]

    assert origin_map["report.json"] == "latest_batch_candidate"
    assert origin_map.get("selected_bank.json") != "current_artifact"
    assert "stale-evidence" not in continuation.focused_snapshot.get("carry_forward_evidence_ids", [])


def test_verifier_flags_conflicting_claims_with_negation_mismatch():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 2,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Resume continues from the last durable checkpoint after interruption.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [{"evidence_id": "e1", "source_id": "R1", "source_backed": True}],
                    },
                    {
                        "claim_id": "claim-2",
                        "text": "Resume does not continue from the last durable checkpoint after interruption.",
                        "citations": ["R2"],
                        "source_ids": ["R2"],
                        "evidence_ids": ["e2"],
                        "confidence": "medium",
                        "evidence_bindings": [{"evidence_id": "e2", "source_id": "R2", "source_backed": True}],
                    },
                ],
            }
        ],
        source_registry={
            "R1": {"source_id": "R1", "url": "https://docs.example.com/resume", "domain": "docs.example.com"},
            "R2": {"source_id": "R2", "url": "https://docs.example.com/restart", "domain": "docs.example.com"},
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
            {"evidence_id": "e2", "source_ids": ["R2"], "evidence_kind": "fetch"},
        ],
        section_banks=[],
    )

    assert "conflict" in verifier["reason_codes"]
    assert verifier["summary"]["conflicted_claims"] == 2
    assert {item["claim_id"] for item in verifier["conflicted_claims"]} == {"claim-1", "claim-2"}


def test_verifier_flags_cross_section_duplicate_claims_when_same_evidence_is_reused():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 2,
            "ungrounded_claims": 0,
            "single_source_claims": 0,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 2,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Resume continues from the last durable checkpoint.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "high",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 4,
                                "line_end": 5,
                            }
                        ],
                    }
                ],
            },
            {
                "section_id": "checkpoint-overview",
                "title": "Checkpoint Overview",
                "claims": [
                    {
                        "claim_id": "claim-2",
                        "text": "Resume continues from the last durable checkpoint.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "high",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 4,
                                "line_end": 5,
                            }
                        ],
                    }
                ],
            },
        ],
        source_registry={
            "R1": {"source_id": "R1", "url": "https://docs.example.com/resume", "domain": "docs.example.com"},
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
        section_banks=[],
    )

    assert "duplicate_claims" in verifier["reason_codes"]
    assert verifier["summary"]["duplicate_claims"] == 1
    assert set(verifier["flagged_claim_ids"]) >= {"claim-2"}


def test_verifier_flags_same_domain_off_topic_dominance():
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "This section is dominated by same-domain off-topic shell content.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                        "evidence_bindings": [
                            {
                                "evidence_id": "e1",
                                "source_id": "R1",
                                "source_backed": True,
                                "line_start": 4,
                                "line_end": 5,
                            }
                        ],
                    }
                ],
            }
        ],
        source_registry={
            "R1": {
                "source_id": "R1",
                "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/aws-dms-checkpoint-resume-restart-recovery-timeout.html",
                "domain": "docs.aws.amazon.com",
                "ranking_penalties": ["same_domain_off_topic"],
            },
        },
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
        section_banks=[],
    )

    assert "same_domain_off_topic_dominance" in verifier["reason_codes"]
    assert verifier["summary"]["same_domain_off_topic_dominance"] == 1
    assert verifier["flagged_claim_ids"] == ["claim-1"]


def test_annotate_source_usage_marks_cited_same_domain_off_topic_source_for_verifier():
    sections = [
        {
            "section_id": "off-topic",
            "title": "Off-topic cited source",
            "citations": ["R1"],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "text": "The cited page is weakly related to AWS DMS RecoveryCheckpoint behavior.",
                    "citations": ["R1"],
                    "source_ids": ["R1"],
                    "evidence_ids": ["e1"],
                    "confidence": "medium",
                    "evidence_bindings": [
                        {
                            "evidence_id": "e1",
                            "source_id": "R1",
                            "source_backed": True,
                            "line_start": 1,
                            "line_end": 2,
                        }
                    ],
                }
            ],
        },
        {
            "section_id": "strong-topic",
            "title": "Strong topic source",
            "citations": ["R2"],
            "claims": [
                {
                    "claim_id": "claim-2",
                    "text": "RecoveryCheckpoint appears in AWS DMS task visibility docs.",
                    "citations": ["R2"],
                    "source_ids": ["R2"],
                    "evidence_ids": ["e2"],
                    "confidence": "high",
                    "evidence_bindings": [
                        {
                            "evidence_id": "e2",
                            "source_id": "R2",
                            "source_backed": True,
                            "line_start": 3,
                            "line_end": 4,
                        }
                    ],
                }
            ],
        },
    ]
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/background.html",
            "domain": "docs.aws.amazon.com",
            "title": "Migration overview",
            "description": "General migration background.",
        },
        {
            "source_id": "R2",
            "url": (
                "https://docs.aws.amazon.com/dms/latest/userguide/"
                "CHAP_Tasks.CustomizingTasks.TaskSettings.html"
            ),
            "domain": "docs.aws.amazon.com",
            "title": "AWS DMS RecoveryCheckpoint task settings",
            "description": "RecoveryCheckpoint task visibility and resume behavior.",
        },
    ]

    annotated_sources = deep_research_runtime_module._annotate_source_usage(
        source_registry,
        sections,
        reference_texts=["AWS DMS RecoveryCheckpoint task visibility resume behavior"],
    )
    annotated_registry = {source["source_id"]: source for source in annotated_sources}

    assert "same_domain_off_topic" in annotated_registry["R1"]["ranking_penalties"]
    verifier = _build_verifier_diagnostics(
        coverage={"coverage_gate_passed": True, "hard_coverage_gate_passed": True},
        grounding={
            "total_claims": 1,
            "ungrounded_claims": 0,
            "single_source_claims": 1,
            "low_confidence_claims": 0,
            "missing_evidence_binding_claims": 0,
            "source_backed_binding_count": 1,
            "null_span_binding_count": 0,
        },
        sections=[sections[0]],
        source_registry=annotated_registry,
        evidence_items=[
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
        section_banks=[],
    )

    assert "same_domain_off_topic_dominance" in verifier["reason_codes"]


def test_claim_conflict_reason_detects_numeric_mismatch():
    reason = deep_research_runtime_module._claim_conflict_reason(
        "RecoveryTimeout waits 5 minutes before failing resume.",
        "RecoveryTimeout waits 10 minutes before failing resume.",
    )

    assert reason == "numeric_mismatch"


def test_verification_payload_falls_back_to_verifier_conflicted_claims():
    payload = deep_research_runtime_module._verification_payload(
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "Resume continues from the last durable checkpoint.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                    }
                ],
            }
        ],
        coverage={"unanswered_sections": [], "hard_uncovered_targets": []},
        verifier={
            "reason_codes": ["conflict"],
            "flagged_claim_ids": ["other-claim"],
            "conflicted_claims": [
                {
                    "claim_id": "fallback-claim",
                    "section_id": "resume-semantics",
                    "text": "Fallback conflict payload.",
                    "confidence": "low",
                }
            ],
        },
        selected_bank=[],
        final_report="",
    )

    assert payload["conflicted_claims"] == [
        {
            "claim_id": "fallback-claim",
            "section_id": "resume-semantics",
            "text": "Fallback conflict payload.",
            "confidence": "low",
        }
    ]


def test_verification_payload_preserves_all_verifier_conflicted_claims():
    payload = deep_research_runtime_module._verification_payload(
        sections=[
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "AWS DMS resumes CDC from a checkpoint.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                        "confidence": "medium",
                    }
                ],
            }
        ],
        coverage={"unanswered_sections": [], "hard_uncovered_targets": []},
        verifier={
            "reason_codes": ["conflict"],
            "flagged_claim_ids": ["claim-1", "claim-2"],
            "conflicted_claims": [
                {
                    "claim_id": "claim-2",
                    "section_id": "resume-semantics",
                    "text": "AWS DMS always restarts from zero.",
                    "confidence": "low",
                }
            ],
        },
        selected_bank=[],
        final_report="",
    )

    assert [claim["claim_id"] for claim in payload["conflicted_claims"]] == ["claim-1", "claim-2"]


def test_packet_to_prose_fidelity_fails_when_selected_row_is_missing():
    payload = deep_research_runtime_module._packet_to_prose_fidelity_payload(
        selected_bank=[
            {
                "section_id": "resume-semantics",
                "selected_evidence_ids": ["e1"],
                "selected_rows": [],
            }
        ],
        sections=[
            {
                "section_id": "resume-semantics",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "AWS DMS resumes CDC from a checkpoint.",
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        ],
        final_report="AWS DMS resumes CDC from a checkpoint.",
    )

    assert payload["passed"] is False
    assert payload["checked_packet_count"] == 0
    assert payload["missing_selected_row_ids"] == ["e1"]
    assert payload["reason_codes"] == ["selected_packet_row_missing"]


def test_packet_to_prose_fidelity_fails_when_selected_claim_missing_from_surface():
    payload = deep_research_runtime_module._packet_to_prose_fidelity_payload(
        selected_bank=[
            {
                "section_id": "resume-semantics",
                "selected_evidence_ids": ["e1"],
                "selected_rows": [
                    {
                        "evidence_id": "e1",
                        "claim_ids": ["claim-missing"],
                    }
                ],
            }
        ],
        sections=[
            {
                "section_id": "resume-semantics",
                "claims": [
                    {
                        "claim_id": "claim-present",
                        "text": "AWS DMS resumes CDC from a checkpoint.",
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        ],
        final_report="AWS DMS resumes CDC from a checkpoint.",
    )

    assert payload["passed"] is False
    assert payload["missing_selected_claim_ids"] == ["claim-missing"]
    assert "selected_packet_claim_missing_from_surface" in payload["reason_codes"]


def test_packet_to_prose_fidelity_fails_when_claim_text_missing_from_report_text():
    payload = deep_research_runtime_module._packet_to_prose_fidelity_payload(
        selected_bank=[
            {
                "section_id": "task-visibility",
                "selected_evidence_ids": ["e1"],
                "selected_rows": [{"evidence_id": "e1", "claim_ids": ["claim-1"]}],
            }
        ],
        sections=[
            {
                "section_id": "task-visibility",
                "title": "Task Visibility",
                "summary": "DescribeReplicationTasks has task state fields.",
                "prose": "DescribeReplicationTasks has task state fields.",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "RecoveryCheckpoint is returned by DescribeReplicationTasks for CDC progress.",
                        "evidence_ids": ["e1"],
                    }
                ],
            }
        ],
        final_report="# Task Visibility\n\nDescribeReplicationTasks has task state fields.",
    )

    assert payload["passed"] is False
    assert payload["missing_selected_claim_text_ids"] == ["claim-1"]
    assert "selected_packet_claim_text_missing_from_prose" in payload["reason_codes"]


def test_packet_to_prose_fidelity_flags_selected_row_outside_selected_evidence_ids():
    payload = deep_research_runtime_module._packet_to_prose_fidelity_payload(
        selected_bank=[
            {
                "section_id": "task-visibility",
                "selected_evidence_ids": ["e1"],
                "selected_rows": [{"evidence_id": "e2", "claim_ids": ["claim-1"]}],
            }
        ],
        sections=[
            {
                "section_id": "task-visibility",
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "text": "RecoveryCheckpoint is returned by DescribeReplicationTasks.",
                        "evidence_ids": ["e2"],
                    }
                ],
            }
        ],
        final_report="RecoveryCheckpoint is returned by DescribeReplicationTasks.",
    )

    assert payload["passed"] is False
    assert payload["selected_row_outside_selected_evidence_ids"] == ["e2"]
    assert "selected_row_outside_selected_evidence" in payload["reason_codes"]


def test_official_doc_family_key_distinguishes_aws_dms_user_guide_families():
    families = {
        deep_research_runtime_module._official_doc_family_key(
            {
                "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Task.CDC.html",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
            }
        ),
        deep_research_runtime_module._official_doc_family_key(
            {
                "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.html",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
            }
        ),
        deep_research_runtime_module._official_doc_family_key(
            {
                "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Introduction.html",
                "domain": "docs.aws.amazon.com",
                "source_type": "official_docs",
            }
        ),
    }

    assert families == {
        "aws:dms:cdc_guide",
        "aws:dms:task_settings",
        "aws:dms:user_guide",
    }


def test_rebuild_verified_rollup_sections_uses_non_generic_inventory_when_supported_ids_absent():
    rebuilt = deep_research_runtime_module._rebuild_verified_rollup_sections(
        [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "summary": "Mixed generic rollup.",
                "prose": "",
                "claims": [
                    {
                        "claim_id": "summary-match",
                        "text": "RecoveryTimeout controls checkpoint wait behavior.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                    },
                    {
                        "claim_id": "summary-noise",
                        "text": "A generic migration shell describes background setup.",
                        "citations": ["R2"],
                        "source_ids": ["R2"],
                        "evidence_ids": ["e2"],
                    },
                ],
            },
            {
                "section_id": "recoverytimeout",
                "title": "Recovery Timeout",
                "summary": "RecoveryTimeout controls checkpoint wait behavior.",
                "prose": "",
                "claims": [
                    {
                        "claim_id": "detail-1",
                        "text": "RecoveryTimeout controls checkpoint wait behavior.",
                        "citations": ["R1"],
                        "source_ids": ["R1"],
                        "evidence_ids": ["e1"],
                    }
                ],
            },
        ],
        {
            "supported_claims": [],
            "flagged_claim_ids": [],
        },
    )

    summary_section = rebuilt[0]
    assert [claim["claim_id"] for claim in summary_section["claims"]] == ["summary-match"]
    assert "background setup" not in summary_section["summary"]


def test_build_section_citations_key_findings_keep_three_grounded_sections():
    plan = DeepResearchPlan.model_validate(
        {
            "query": "Compare resume, restart, and console checkpoint behavior",
            "context": "",
            "effort": "standard",
            "time_budget_seconds": 240,
            "include_domains": [],
            "exclude_domains": [],
            "brief": {
                "objective": "Compare resume, restart, and console checkpoint behavior",
                "deliverable": "A cited report.",
                "success_criteria": ["Produce a structured report."],
            },
            "sub_questions": [
                {"id": "sq1", "question": "How does resume work?", "reason": "Primary axis."},
                {"id": "sq2", "question": "How does restart differ?", "reason": "Primary axis."},
                {"id": "sq3", "question": "How does the console expose checkpoints?", "reason": "Primary axis."},
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": ["resume semantics", "restart semantics", "console checkpoint behavior"],
                "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "resume-semantics", "title": "Resume Semantics", "goal": "Explain resume semantics."},
                {"section_id": "restart-semantics", "title": "Restart Semantics", "goal": "Explain restart semantics."},
                {"section_id": "console-checkpoints", "title": "Console Checkpoints", "goal": "Explain console checkpoint behavior."},
            ],
            "research_units": [],
        }
    )
    source_registry = [
        {
            "source_id": "R1",
            "url": "https://docs.example.com/runtime/resume",
            "title": "Resume docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R2",
            "url": "https://docs.example.com/runtime/restart",
            "title": "Restart docs",
            "source_type": "official_docs",
        },
        {
            "source_id": "R3",
            "url": "https://docs.example.com/runtime/console",
            "title": "Console docs",
            "source_type": "official_docs",
        },
    ]
    evidence_items = [
        {
            "evidence_id": "evidence-resume",
            "unit_id": "unit-search-1",
            "source_ids": ["R1"],
            "source_urls": ["https://docs.example.com/runtime/resume"],
            "summary": "Resume continues from the last durable checkpoint after interruption.",
            "detail": "Resume continues from the last durable checkpoint after interruption.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-restart",
            "unit_id": "unit-search-2",
            "source_ids": ["R2"],
            "source_urls": ["https://docs.example.com/runtime/restart"],
            "summary": "Restart replays work from a fresh starting point and may re-run completed work.",
            "detail": "Restart replays work from a fresh starting point and may re-run completed work.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
        {
            "evidence_id": "evidence-console",
            "unit_id": "unit-search-3",
            "source_ids": ["R3"],
            "source_urls": ["https://docs.example.com/runtime/console"],
            "summary": "The console exposes the last checkpoint and lets operators set a restart point.",
            "detail": "The console exposes the last checkpoint and lets operators set a restart point.",
            "evidence_kind": "fetch",
            "weight": 1.0,
        },
    ]
    section_banks = [
        {"section_id": "executive-summary", "candidate_evidence_ids": [], "selected_evidence_ids": [], "rejected_evidence_ids": []},
        {"section_id": "key-findings", "candidate_evidence_ids": [], "selected_evidence_ids": [], "rejected_evidence_ids": []},
        {"section_id": "resume-semantics", "candidate_evidence_ids": ["evidence-resume"], "selected_evidence_ids": ["evidence-resume"], "rejected_evidence_ids": []},
        {"section_id": "restart-semantics", "candidate_evidence_ids": ["evidence-restart"], "selected_evidence_ids": ["evidence-restart"], "rejected_evidence_ids": []},
        {"section_id": "console-checkpoints", "candidate_evidence_ids": ["evidence-console"], "selected_evidence_ids": ["evidence-console"], "rejected_evidence_ids": []},
    ]

    sections = _build_section_citations(
        plan,
        evidence_items,
        source_registry,
        section_banks=section_banks,
        evidence_ledger=[],
    )
    key_findings = next(section for section in sections if section["section_id"] == "key-findings")

    assert len(key_findings["claims"]) == 3
    assert "Resume continues from the last durable checkpoint" in key_findings["summary"]
    assert "Restart replays work from a fresh starting point" in key_findings["summary"]
    assert "console exposes the last checkpoint" in key_findings["summary"].lower()


@pytest.mark.asyncio
async def test_grounding_diagnostics_include_source_level_usage(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain checkpoint resume semantics.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Primary search",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Checkpoint resume docs.",
                }
            ],
        )

    async def fetch(url):
        return "# Runtime checkpoints\n\nCheckpoint resume continues from the last durable checkpoint."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fetch)

    response = await runtime.start(query="Grounding diagnostics probe", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])

    grounding = json.loads(runtime.read_artifact_text(response["job_id"], "grounding.json"))

    assert grounding["sources"] == [
        {
            "source_id": "R1",
            "citation_count": 1,
            "section_count": 1,
            "supporting_claim_ids": ["resume-semantics-claim-1"],
            "supporting_section_ids": ["resume-semantics"],
        }
    ]


@pytest.mark.asyncio
async def test_runtime_persists_source_policy_lineage_and_selected_bank_artifacts(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["include_domains"] = ["docs.aws.amazon.com"]
        payload["exclude_domains"] = ["repost.aws"]
        payload["brief"]["scope"]["include_domains"] = ["docs.aws.amazon.com"]
        payload["brief"]["scope"]["exclude_domains"] = ["repost.aws"]
        payload["brief"]["scope"]["allowed_sources"] = ["docs.aws.amazon.com"]
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain RecoveryCheckpoint and resume-processing behavior.",
                "reason": "Primary axis.",
            }
        ]
        payload["report_outline"] = [
            {
                "section_id": "resume-semantics",
                "title": "Resume Semantics",
                "goal": "Explain RecoveryCheckpoint and resume-processing behavior.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "AWS DMS official docs",
                "goal": "Explain RecoveryCheckpoint and resume-processing behavior.",
                "query": "aws dms RecoveryCheckpoint resume-processing",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"]["search_queries"] = ["aws dms RecoveryCheckpoint resume-processing"]
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "RecoveryCheckpoint can be reused when resume-processing continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/APIReference/API_StartReplicationTask.html",
                    "title": "StartReplicationTask",
                    "description": "Official AWS DMS API reference for resume-processing and RecoveryCheckpoint behavior.",
                    "provider": "grok",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(
        query="Follow up AWS DMS RecoveryCheckpoint semantics with official docs only",
        include_domains=["docs.aws.amazon.com"],
        exclude_domains=["repost.aws"],
        force_new=True,
        schedule=False,
    )
    result = await runtime.run_job(response["job_id"])
    status = await runtime.status(response["job_id"])
    public_result = await runtime.result(response["job_id"])

    source_policy = json.loads(runtime.store.read_artifact_text(response["job_id"], "source_policy.json") or "{}")
    lineage = json.loads(runtime.store.read_artifact_text(response["job_id"], "lineage.json") or "{}")
    selected_bank = json.loads(runtime.store.read_artifact_text(response["job_id"], "selected_bank.json") or "[]")
    evidence_bank = json.loads(runtime.store.read_artifact_text(response["job_id"], "evidence_bank.json") or "[]")
    verification = json.loads(runtime.store.read_artifact_text(response["job_id"], "verification.json") or "{}")
    coverage_gaps = json.loads(runtime.store.read_artifact_text(response["job_id"], "coverage_gaps.json") or "{}")

    assert source_policy["mode"] == "official_docs_only"
    assert source_policy["allowed_domains"] == ["docs.aws.amazon.com"]
    assert source_policy["blocked_domains"] == ["repost.aws"]
    assert source_policy["must_cite_per_section"] is True
    assert lineage["root_job_id"] == response["job_id"]
    assert lineage["fork_type"] == "fresh"
    assert result["report"]["runtime"]["source_policy"]["mode"] == "official_docs_only"
    materialized_row = next(
        row
        for row in selected_bank
        if row["selected_rows"] and row["section_id"] not in {"executive-summary", "key-findings", "open-questions"}
    )
    assert materialized_row["selected_evidence_ids"]
    assert materialized_row["selected_rows"][0]["claim_ids"]
    assert evidence_bank
    assert evidence_bank[0]["used_by_section_ids"]
    assert verification["packet_to_prose_fidelity"]["passed"] is True
    assert coverage_gaps["coverage_gate_passed"] is True
    assert coverage_gaps["total_gap_count"] == 0
    assert "selected_bank.json" in status["artifact_kinds"]
    assert "evidence_bank.json" in status["artifact_kinds"]
    assert "verification.json" in status["artifact_kinds"]
    assert "coverage_gaps.json" in status["artifact_kinds"]
    assert public_result["selected_bank"] == selected_bank
    assert public_result["evidence_bank"] == evidence_bank
    assert public_result["verification"] == verification
    assert public_result["coverage_gaps"] == coverage_gaps


@pytest.mark.asyncio
async def test_runtime_source_policy_treats_mixed_allowlist_as_mixed_web(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search(query):
        return (
            "Provider docs and release notes should remain separated when the allowlist mixes official and non-official domains.",
            [
                {
                    "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Introduction.html",
                    "title": "AWS DMS User Guide",
                    "description": "Official AWS DMS docs.",
                    "provider": "grok",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(
        query="Mixed source policy should not claim official docs only",
        include_domains=["docs.aws.amazon.com", "aws.amazon.com"],
        force_new=True,
        schedule=False,
    )
    await runtime.run_job(response["job_id"])

    source_policy = json.loads(runtime.store.read_artifact_text(response["job_id"], "source_policy.json") or "{}")
    public_result = await runtime.result(response["job_id"])

    assert source_policy["mode"] == "mixed_web"
    assert source_policy["allowed_domains"] == ["docs.aws.amazon.com", "aws.amazon.com"]
    assert public_result["report"]["runtime"]["source_policy"]["mode"] == "mixed_web"


@pytest.mark.asyncio
async def test_runtime_provider_winners_include_provider_api_url(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search_with_details(query, *, effort="standard", include_domains=None, exclude_domains=None):
        return {
            "answer": "Provider routing can expose the API URL used for the winning research unit.",
            "sources": [
                {
                    "url": "https://docs.example.com/provider-routing",
                    "title": "Provider routing",
                    "description": "Official provider routing docs.",
                    "provider": "grok",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_name": "test-provider",
            "provider_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)

    response = await runtime.start(query="Provider winner API URL surface", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])
    result = await runtime.result(response["job_id"])

    provider_winner = result["report"]["runtime"]["provider_winners"][0]
    assert provider_winner["unit_id"] == "unit-search-1"
    assert provider_winner["provider_name"] == "test-provider"
    assert provider_winner["provider_api_url"] == "https://provider.example.invalid/v1/chat/completions"
    runtime_payload = result["report"]["runtime"]
    assert runtime_payload["budget"]["effort"] == "standard"
    assert runtime_payload["budget"]["resolved_budget_seconds"] == 240
    assert runtime_payload["budget"]["usage"]["completed_units"] == 1
    assert runtime_payload["budget"]["usage"]["search_calls"] == 1
    assert runtime_payload["budget"]["usage"]["source_count"] == 1
    assert runtime_payload["provider_capabilities"]["search"]["providers"] == ["test-provider"]
    assert runtime_payload["provider_attempts"] == [
        {
            "unit_id": "unit-search-1",
            "operation": "search",
            "status": "completed",
            "provider_name": "test-provider",
            "provider_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
            "source_count": 1,
            "evidence_count": 1,
            "error_code": "",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_provider_attempts_include_supplemental_search_provider(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search_with_details(query, *, effort="standard", include_domains=None, exclude_domains=None):
        return {
            "answer": "Supplemental search sources can repair a weak primary search body.",
            "sources": [
                {
                    "url": "https://docs.example.com/supplemental-provider",
                    "title": "Supplemental provider",
                    "description": "Supplemental provider docs.",
                    "provider": "tavily",
                }
            ],
            "supplemental_attempts": [
                {
                    "operation": "search",
                    "status": "completed",
                    "provider_name": "tavily",
                    "provider_model": "",
                    "effective_model": "",
                    "provider_api_url": "https://api.tavily.com/search",
                    "source_count": 1,
                    "evidence_count": 0,
                    "error_code": "",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_name": "primary-grok",
            "provider_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
            "provider_source_count": 0,
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)

    response = await runtime.start(query="Supplemental provider budget surface", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])
    result = await runtime.result(response["job_id"])
    runtime_payload = result["report"]["runtime"]

    search_attempts = [
        attempt for attempt in runtime_payload["provider_attempts"] if attempt["operation"] == "search"
    ]
    assert search_attempts == [
        {
            "unit_id": "unit-search-1",
            "operation": "search",
            "status": "completed",
            "provider_name": "primary-grok",
            "provider_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
            "source_count": 0,
            "evidence_count": 1,
            "error_code": "",
        },
        {
            "unit_id": "unit-search-1",
            "operation": "search",
            "status": "completed",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com/search",
            "source_count": 1,
            "evidence_count": 0,
            "error_code": "",
            "attempt_role": "supplemental",
        },
    ]
    assert runtime_payload["provider_capabilities"]["search"]["providers"] == ["primary-grok"]
    assert runtime_payload["budget"]["usage"]["search_calls"] == 2
    assert runtime_payload["budget"]["usage"]["provider_attempts"] == 2


@pytest.mark.asyncio
async def test_runtime_selective_fetch_records_fetch_attempt_and_budget_usage(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 1,
            "prefer_titles_matching_outline": True,
        }
        return payload

    async def search_with_details(query, *, effort="standard", include_domains=None, exclude_domains=None):
        return {
            "answer": "Selective fetch can ground provider accounting against the exact source page.",
            "sources": [
                {
                    "url": "https://docs.example.com/provider-budget",
                    "title": "Provider budget",
                    "description": "Provider budget docs.",
                    "provider": "grok",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_name": "primary-grok",
            "provider_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
            "provider_source_count": 1,
        }

    async def fetch_with_details(url):
        return {
            "content": "Provider budget docs explain fetch attempt accounting and exact source evidence.",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com/extract",
            "error_code": "",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url_with_details", fetch_with_details)

    response = await runtime.start(query="Selective fetch budget usage", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])
    result = await runtime.result(response["job_id"])
    runtime_payload = result["report"]["runtime"]

    fetch_attempts = [
        attempt for attempt in runtime_payload["provider_attempts"] if attempt["operation"] == "fetch"
    ]
    search_attempt = next(
        attempt
        for attempt in runtime_payload["provider_attempts"]
        if attempt["operation"] == "search" and not attempt.get("attempt_role")
    )
    assert search_attempt["evidence_count"] == 1
    assert fetch_attempts == [
        {
            "unit_id": "unit-search-1",
            "operation": "fetch",
            "status": "completed",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com/extract",
            "source_count": 1,
            "evidence_count": 1,
            "error_code": "",
            "attempt_role": "selective_fetch",
        }
    ]
    assert runtime_payload["provider_capabilities"]["fetch"]["providers"] == ["tavily"]
    assert runtime_payload["budget"]["usage"]["fetch_calls"] == 1
    assert runtime_payload["budget"]["usage"]["provider_attempts"] == 2


@pytest.mark.asyncio
async def test_runtime_failed_fetch_unit_records_provider_attempt(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["search_strategy"]["selective_fetch"] = {
            "max_urls_per_search": 0,
            "prefer_titles_matching_outline": True,
        }
        payload["report_outline"] = [
            {
                "section_id": "provider-success",
                "title": "Provider Success",
                "goal": "Summarize provider success metadata.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Provider success",
                "goal": "Produce one successful unit so final artifacts are available.",
                "query": "provider success metadata",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            },
            {
                "unit_id": "unit-fetch-1",
                "unit_type": "fetch",
                "title": "Fetch missing page",
                "goal": "Fetch provider failure metadata.",
                "query": "",
                "url": "https://docs.example.com/missing",
                "depends_on": ["unit-search-1"],
                "status": "pending",
                "notes": "",
            }
        ]
        return payload

    async def search_with_details(query, *, effort="standard", include_domains=None, exclude_domains=None):
        return {
            "answer": "Provider success metadata is available for final report synthesis.",
            "sources": [
                {
                    "url": "https://docs.example.com/provider-success",
                    "title": "Provider success",
                    "description": "Provider success docs.",
                    "provider": "grok",
                }
            ],
            "warning_code": None,
            "requested_model": "grok-4.20-expert",
            "effective_model": "grok-4.20-expert",
            "provider_name": "test-provider",
            "provider_model": "grok-4.20-expert",
            "provider_api_url": "https://provider.example.invalid/v1/chat/completions",
        }

    async def fetch_with_details(url):
        return {
            "content": "",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com/extract",
            "error_code": "fetch_failed",
        }

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query_with_details", search_with_details)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url_with_details", fetch_with_details)

    response = await runtime.start(query="Fetch provider failure visibility", force_new=True, schedule=False)
    await runtime.run_job(response["job_id"])
    result = await runtime.result(response["job_id"])
    runtime_payload = result["report"]["runtime"]

    assert runtime_payload["failed_units"] == [
        {
            "unit_id": "unit-fetch-1",
            "unit_type": "fetch",
            "reason": "empty_fetch_result",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": "https://api.tavily.com/extract",
            "error_code": "fetch_failed",
        }
    ]
    assert runtime_payload["provider_capabilities"]["fetch"]["providers"] == ["tavily"]
    fetch_attempt = next(
        attempt for attempt in runtime_payload["provider_attempts"] if attempt["unit_id"] == "unit-fetch-1"
    )
    assert fetch_attempt == {
        "unit_id": "unit-fetch-1",
        "operation": "fetch",
        "status": "failed",
        "provider_name": "tavily",
        "provider_model": "",
        "effective_model": "",
        "provider_api_url": "https://api.tavily.com/extract",
        "source_count": 0,
        "evidence_count": 0,
        "error_code": "fetch_failed",
        "failure_reason": "empty_fetch_result",
    }
    assert runtime_payload["budget"]["usage"]["failed_units"] == 1
    assert runtime_payload["budget"]["usage"]["completed_units"] == 1
    assert runtime_payload["budget"]["usage"]["fetch_calls"] == 1


@pytest.mark.asyncio
async def test_search_query_with_details_passes_domain_constraints_to_supplemental_search(monkeypatch):
    captured = {}

    class EmptyProvider:
        _last_success_provider_name = "grok"
        _last_success_provider_model = "grok-4.20-expert"
        _last_success_provider_api_url = "https://provider.example.invalid/v1/chat/completions"

    async def fake_provider_builder(*, effort="standard"):
        return EmptyProvider(), {"requested_model": "grok-4.20-expert", "effective_model": "grok-4.20-expert"}

    async def fake_provider_search(provider, query, *, platform="", min_results=3, max_results=10):
        return "Answer without usable body sources.", []

    async def fake_supplemental(query, *, answer, existing_sources, warning_code, include_domains=None, exclude_domains=None):
        captured["include_domains"] = include_domains
        captured["exclude_domains"] = exclude_domains
        return [{"url": "https://docs.example.com/provider-routing", "title": "Provider routing"}]

    monkeypatch.setattr("grok_search.deep_research_runtime._build_runtime_grok_provider", fake_provider_builder)
    monkeypatch.setattr("grok_search.deep_research_runtime._provider_search_with_sources", fake_provider_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._supplemental_sources_for_deep_research", fake_supplemental)

    result = await deep_research_runtime_module._search_query_with_details(
        "provider routing",
        include_domains=["docs.example.com"],
        exclude_domains=["blog.example.com"],
    )

    assert result["sources"][0]["url"] == "https://docs.example.com/provider-routing"
    assert captured == {
        "include_domains": ["docs.example.com"],
        "exclude_domains": ["blog.example.com"],
    }


@pytest.mark.asyncio
async def test_supplemental_sources_records_empty_tavily_before_firecrawl_success(monkeypatch):
    async def tavily_search(query, max_results, *, include_domains=None, exclude_domains=None):
        return []

    async def firecrawl_search(query, limit):
        return [
            {
                "url": "https://docs.example.com/firecrawl-result",
                "title": "Firecrawl result",
                "description": "Firecrawl supplemental result.",
            }
        ]

    monkeypatch.setattr(server, "_call_tavily_search", tavily_search)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl_search)

    result = await deep_research_runtime_module._supplemental_sources_for_deep_research(
        "supplemental provider fallback",
        answer="",
        existing_sources=[],
        warning_code="body_missing_sources_only",
    )

    assert result["sources"] == [
        {
            "url": "https://docs.example.com/firecrawl-result",
            "title": "Firecrawl result",
            "description": "Firecrawl supplemental result.",
            "provider": "firecrawl",
        }
    ]
    tavily_search_url = f"{deep_research_runtime_module.config.tavily_api_url.rstrip('/')}/search"
    firecrawl_search_url = f"{deep_research_runtime_module.config.firecrawl_api_url.rstrip('/')}/search"
    assert result["provider_attempts"] == [
        {
            "unit_id": "",
            "operation": "search",
            "status": "failed",
            "provider_name": "tavily",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": tavily_search_url,
            "source_count": 0,
            "evidence_count": 0,
            "error_code": "empty_search_result",
            "attempt_role": "supplemental",
        },
        {
            "unit_id": "",
            "operation": "search",
            "status": "completed",
            "provider_name": "firecrawl",
            "provider_model": "",
            "effective_model": "",
            "provider_api_url": firecrawl_search_url,
            "source_count": 1,
            "evidence_count": 0,
            "error_code": "",
            "attempt_role": "supplemental",
        },
    ]


@pytest.mark.asyncio
async def test_supplemental_sources_records_unavailable_tavily_before_firecrawl_success(monkeypatch):
    async def tavily_search(query, max_results, *, include_domains=None, exclude_domains=None):
        return None

    async def firecrawl_search(query, limit):
        return [
            {
                "url": "https://docs.example.com/firecrawl-result",
                "title": "Firecrawl result",
                "description": "Firecrawl supplemental result.",
            }
        ]

    monkeypatch.setattr(server, "_call_tavily_search", tavily_search)
    monkeypatch.setattr(server, "_call_firecrawl_search", firecrawl_search)

    result = await deep_research_runtime_module._supplemental_sources_for_deep_research(
        "supplemental provider fallback",
        answer="",
        existing_sources=[],
        warning_code="body_missing_sources_only",
    )

    assert [attempt["provider_name"] for attempt in result["provider_attempts"]] == ["tavily", "firecrawl"]
    assert result["provider_attempts"][0]["status"] == "failed"
    assert result["provider_attempts"][0]["error_code"] == "search_unavailable"
    assert result["provider_attempts"][0]["attempt_role"] == "supplemental"
    assert result["provider_attempts"][1]["status"] == "completed"
    assert result["provider_attempts"][1]["source_count"] == 1
    assert result["provider_attempts"][1]["attempt_role"] == "supplemental"


@pytest.mark.asyncio
async def test_runtime_generic_rollup_only_artifacts_remain_usable(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)

    async def planner(job, continuation):
        payload = structured_plan_payload(job, continuation)
        payload["report_outline"] = [
            {
                "section_id": "executive-summary",
                "title": "Executive Summary",
                "goal": "Summarize the answer.",
            },
            {
                "section_id": "key-findings",
                "title": "Key Findings",
                "goal": "List the strongest findings.",
            },
        ]
        payload["sub_questions"] = [
            {
                "id": "sq1",
                "question": "Explain checkpoint resume semantics.",
                "reason": "Primary axis.",
            }
        ]
        payload["research_units"] = [
            {
                "unit_id": "unit-search-1",
                "unit_type": "search",
                "title": "Checkpoint resume",
                "goal": "Explain checkpoint resume semantics.",
                "query": "checkpoint resume semantics",
                "depends_on": [],
                "status": "pending",
                "notes": "",
            }
        ]
        payload["search_strategy"] = {
            "approach": "targeted",
            "search_queries": ["checkpoint resume semantics"],
            "selective_fetch": {"max_urls_per_search": 0, "prefer_titles_matching_outline": True},
        }
        return payload

    async def search(query):
        return (
            "Checkpoint resume continues from the last durable checkpoint.",
            [
                {
                    "url": "https://docs.example.com/runtime/checkpoints",
                    "title": "Runtime checkpoints",
                    "description": "Official checkpoint resume docs.",
                    "provider": "grok",
                }
            ],
        )

    monkeypatch.setattr(runtime, "_generate_plan_with_model", planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", search)

    response = await runtime.start(query="Generic rollup only runtime probe", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])
    status = await runtime.status(response["job_id"])
    public_result = await runtime.result(response["job_id"])
    selected_bank = json.loads(runtime.store.read_artifact_text(response["job_id"], "selected_bank.json") or "[]")

    assert result["artifact_errors"] == {}
    assert status["resolved_artifact_batch_id"]
    assert public_result["selected_bank"] == selected_bank
    assert any(bank.get("selected_rows") for bank in selected_bank)
    assert all(
        claim_id
        for bank in selected_bank
        for row in bank.get("selected_rows", [])
        for claim_id in row.get("claim_ids", [])
    )


@pytest.mark.asyncio
async def test_resolved_final_batch_missing_additive_sidecar_does_not_read_stale_current_artifact(tmp_path):
    runtime = build_runtime(tmp_path)
    job = runtime.store.create_job(
        query="Resolved batch missing additive sidecar",
        request_fingerprint="fp-resolved-batch-missing-additive-sidecar",
        status="completed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )
    runtime.store.update_job(job.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": job.query}), "application/json")
    complete_artifacts = with_minimal_provenance_artifacts(
        [
            {
                "kind": "sources.json",
                "content": json.dumps([{"source_id": "R1", "url": "https://docs.example.com/final"}]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(
                    {
                        "source_registry": {
                            "R1": {"source_id": "R1", "url": "https://docs.example.com/final"}
                        },
                        "sections": [],
                    }
                ),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps({"summary": "Final batch.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nFinal batch.",
                "content_type": "text/markdown",
            },
        ],
        query=job.query,
    )
    persisted = runtime.write_artifact_batch(
        job.job_id,
        [artifact for artifact in complete_artifacts if artifact.get("kind") != "coverage_gaps.json"],
    )
    runtime.write_artifact(
        job.job_id,
        "coverage_gaps.json",
        json.dumps({"coverage_gate_passed": False, "gaps": [{"section_id": "stale"}]}),
        "application/json",
    )

    result = await runtime.result(job.job_id)
    artifact = runtime.read_artifact(job.job_id, "coverage_gaps.json")

    assert result["resolved_artifact_batch_id"] == persisted[0]["metadata"]["batch_id"]
    assert result["coverage_gaps"] is None
    assert result["artifact_errors"]["coverage_gaps.json"] == "missing_required_artifact"
    assert artifact == {
        "content": None,
        "state": "missing",
        "artifact_visibility_reason": "",
    }
