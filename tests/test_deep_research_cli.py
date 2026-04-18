import json
import asyncio
import sys
from pathlib import Path

from grok_search import deep_research_cli
from grok_search.deep_research_runtime import DeepResearchRuntime
from grok_search.deep_research_types import utc_now_iso


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_research"
_SEEDED_BATCH_ID_PLACEHOLDER = "$seeded_batch_id"


def build_runtime(tmp_path):
    runtime = DeepResearchRuntime(tmp_path / "deep-research")
    runtime._generate_plan_with_model = lambda job, continuation: asyncio.sleep(0, result=structured_plan_payload(job, continuation))
    return runtime


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


def with_minimal_provenance_artifacts(
    artifacts: list[dict],
    *,
    query: str,
    evidence_items: list[dict] | None = None,
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
            "unbound_citation_sources": 0,
            "unbound_evidence_ids": 0,
        },
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
                for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims")
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


def summary_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("summary:")]


def assert_no_traceback(text: str) -> None:
    assert "Traceback (most recent call last)" not in text


def load_deep_research_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


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


def seed_round11_interrupted_finalizing_job(runtime: DeepResearchRuntime):
    continuation_snapshot = load_deep_research_fixture("probe_round11_interrupted_continue_snapshot.json")
    report_snapshot = load_deep_research_fixture("probe_round11_main_snapshot.json")
    source = {
        **report_snapshot["sources"][0],
        "url": "https://docs.aws.amazon.com/dms/latest/userguide/CHAP_Tasks.CustomizingTasks.TaskSettings.ChangeProcessingTuning.html",
    }
    plan_payload = {
        "query": continuation_snapshot["query"],
        "include_domains": continuation_snapshot["include_domains"],
        "exclude_domains": continuation_snapshot["exclude_domains"],
        "continuation": continuation_snapshot["continuation"],
        "brief": {
            "continuation_focus": continuation_snapshot["continuation"]["continuation_focus"],
        },
        "search_strategy": continuation_snapshot["plan"]["search_strategy"],
        "planner_metadata": continuation_snapshot["planner"],
    }
    report_payload = {
        "summary": "Checkpoint resume summary.",
        "status": report_snapshot["report"]["status"],
        "runtime": dict(report_snapshot["report"].get("runtime") or {}),
        "sections": [],
        "unit_results": {},
    }
    citations_payload = {
        "source_registry": {
            source["source_id"]: source,
        },
        "sections": [],
    }
    job = runtime.store.create_job(
        query=continuation_snapshot["query"],
        request_fingerprint="fp-cli-round11-interrupted-parity",
        status="interrupted",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=continuation_snapshot["include_domains"],
        exclude_domains=continuation_snapshot["exclude_domains"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id=continuation_snapshot["continued_from_job_id"],
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=2,
        current_checkpoint="finalizing",
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact(job.job_id, "plan.json", json.dumps(plan_payload), "application/json")
    persisted = runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": json.dumps([source]),
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": json.dumps(citations_payload),
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": json.dumps(report_payload),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": f"# Final Report\n\n{report_payload['summary']}\n",
                "content_type": "text/markdown",
            },
            ],
            query=continuation_snapshot["query"],
        ),
    )
    runtime.store.append_event(
        job.job_id,
        type="phase_started",
        phase="finalizing",
        message="Finalizing interrupted round11 snapshot.",
        data={"resolved_artifact_batch_id": persisted[0]["metadata"]["batch_id"]},
    )
    runtime.store.append_event(
        job.job_id,
        type="job_interrupted",
        phase="finalizing",
        message="Interrupted after resolved final batch was written.",
        data={"checkpoint": "finalizing"},
    )
    return {
        "job": job,
        "batch_id": persisted[0]["metadata"]["batch_id"],
        "source": source,
        "plan_payload": plan_payload,
        "report_payload": report_payload,
    }


def seed_round21_stale_worker_reconnect_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round21_stale_worker_reconnect.json")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-cli-round21-stale-worker-reconnect",
        status=snapshot["resume_run"]["status"],
        phase=snapshot["resume_run"]["phase"],
        effort="deep",
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
        attempt_count=snapshot["resume_run"]["attempt_count"],
        current_checkpoint=snapshot["resume_run"]["current_checkpoint"],
        finished_at=utc_now_iso(),
        last_error="time_budget_exceeded",
    )
    for event in snapshot["initial_events"] + snapshot["resume_events_after_seq_7"]:
        runtime.store.append_event(
            job.job_id,
            type=event["type"],
            phase=event["phase"],
            message=event.get("message") or event["type"],
            data=event.get("data") or {},
        )
    return {"job": runtime.store.get_job(job.job_id), "snapshot": snapshot}


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
            "grounding": grounding_payload,
            "release_gate": snapshot["resume_events_after_seq_16"][-1]["data"]["release_gate"],
            "verifier": verifier_payload,
        },
    }
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-cli-round24-worker-restart-live",
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
            {"kind": "sources.json", "content": json.dumps([source]), "content_type": "application/json"},
            {"kind": "citations.json", "content": json.dumps({"source_registry": {"R1": source}, "sections": []}), "content_type": "application/json"},
            {"kind": "report.json", "content": json.dumps(report_payload), "content_type": "application/json"},
            {"kind": "final_report.md", "content": "# Final Report\n\nRound24 worker restart lifecycle summary.\n", "content_type": "text/markdown"},
            {"kind": "evidence_items.json", "content": "[]", "content_type": "application/json"},
            {"kind": "coverage.json", "content": json.dumps(coverage_payload), "content_type": "application/json"},
            {"kind": "grounding.json", "content": json.dumps(grounding_payload), "content_type": "application/json"},
            {"kind": "verifier.json", "content": json.dumps(verifier_payload), "content_type": "application/json"}
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
    return {"job": runtime.store.get_job(job.job_id), "snapshot": snapshot, "batch_id": persisted[0]["metadata"]["batch_id"]}


def seed_round26_worker_restart_dispatch_runtime_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round26_worker_restart_dispatch_runtime.json")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-cli-round26-worker-restart-dispatch-runtime",
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


def seed_canceled_finalizing_job(runtime: DeepResearchRuntime):
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
    job = runtime.store.create_job(
        query="Canceled finalizing visibility",
        request_fingerprint="fp-cli-canceled-finalizing-visibility",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Canceled finalizing visibility"}), "application/json")
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
    return {
        "job": job,
        "batch_id": persisted[0]["metadata"]["batch_id"],
        "evidence_items": evidence_items,
        "evidence_text": json.dumps(evidence_items),
    }


def seed_round12_continue_resume_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round12_continue_resume_snapshot.json")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-cli-round12-continue-resume-parity",
        status=snapshot["continue_status"]["status"],
        phase=snapshot["continue_status"]["phase"],
        effort="standard",
        context="",
        include_domains=snapshot["include_domains"],
        exclude_domains=snapshot["exclude_domains"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id=snapshot["source_job_id"],
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=snapshot["continue_status"]["attempt_count"],
        current_checkpoint=snapshot["continue_status"]["checkpoint"],
        last_error=snapshot["continue_status"]["last_error"],
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(
            {
                "query": snapshot["query"],
                "include_domains": snapshot["include_domains"],
                "exclude_domains": snapshot["exclude_domains"],
                "continuation": snapshot["continuation"],
                "brief": {"must_cover": snapshot["plan"]["must_cover"]},
                "search_strategy": {"search_queries": snapshot["plan"]["search_queries"]},
                "planner_metadata": snapshot["planner"],
            }
        ),
        "application/json",
    )
    for event_type in snapshot["events"]:
        runtime.store.append_event(
            job.job_id,
            type=event_type,
            phase=snapshot["continue_status"]["phase"],
            message=event_type,
            data={},
        )
    return {"job": job, "snapshot": snapshot}


def test_cli_plan_only_start_prints_draft_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    exit_code = deep_research_cli.main(
        ["start", "Plan a deep research run", "--context", "Need the plan first.", "--plan-only"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "draft"
    assert payload["plan"]["context"] == "Need the plan first."


def test_cli_result_artifact_prints_artifact_content(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    response = runtime.store.create_job(
        query="Artifact job",
        request_fingerprint="fp-artifact",
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
    runtime.write_artifact(response.job_id, "final_report.md", "# Final Report\n\nArtifact body.", "text/markdown")

    exit_code = deep_research_cli.main(["result", response.job_id, "--artifact", "final_report.md"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "# Final Report\n\nArtifact body.\n"
    assert summary_lines(captured.err) == [
        f"summary: job={response.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false artifact=final_report.md bytes=30"
    ]


def test_cli_result_artifact_missing_file_returns_nonzero(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Missing artifact job",
        request_fingerprint="fp-missing-artifact",
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

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "missing.md"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "artifact_not_found: missing.md" in captured.err


def test_cli_result_artifact_prefers_resolved_final_batch(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Artifact batch job",
        request_fingerprint="fp-artifact-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Artifact batch job"}), "application/json")
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
                "content": json.dumps({"summary": "Good report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nGood report.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Artifact batch job",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://stale.example.com"}]),
        "application/json",
    )

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "sources.json"])
    captured = capsys.readouterr()
    batch_id = persisted[0]["metadata"]["batch_id"]
    artifact_content = '[{"source_id": "R1", "url": "https://good.example.com"}]'

    assert exit_code == 0
    assert captured.out == artifact_content + "\n"
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch={batch_id} artifact_fallback=true artifact=sources.json bytes={len(artifact_content.encode('utf-8'))}"
    ]


def test_cli_result_artifact_prefers_resolved_final_batch_for_citations(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Artifact batch citations job",
        request_fingerprint="fp-artifact-batch-citations",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Artifact batch citations job"}), "application/json")
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
                    "content": json.dumps(
                        {
                            "summary": "Good report",
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
                                        "unbound_citation_sources": 0,
                                        "unbound_evidence_ids": 0,
                                    },
                                },
                            },
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nGood report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Artifact batch citations job",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "citations.json",
        json.dumps({"source_registry": {"R9": {"source_id": "R9", "url": "https://stale.example.com"}}, "sections": []}),
        "application/json",
    )

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "citations.json"])
    captured = capsys.readouterr()
    batch_id = persisted[0]["metadata"]["batch_id"]
    artifact_content = '{"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}'

    assert exit_code == 0
    assert captured.out == artifact_content + "\n"
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch={batch_id} artifact_fallback=true artifact=citations.json bytes={len(artifact_content.encode('utf-8'))}"
    ]


def test_cli_result_artifact_prefers_resolved_final_batch_for_verifier(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Artifact batch verifier job",
        request_fingerprint="fp-artifact-batch-verifier",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Artifact batch verifier job"}), "application/json")
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
                    "content": json.dumps(
                        {
                            "summary": "Good report",
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
                                        "unbound_citation_sources": 0,
                                        "unbound_evidence_ids": 0,
                                    },
                                },
                            },
                        }
                    ),
                    "content_type": "application/json",
                },
                {
                    "kind": "final_report.md",
                    "content": "# Final Report\n\nGood report.\n",
                    "content_type": "text/markdown",
                },
            ],
            query="Artifact batch verifier job",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "verifier.json",
        json.dumps({"passed": False, "reason_codes": ["stale_current_only"]}),
        "application/json",
    )

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "verifier.json"])
    captured = capsys.readouterr()
    batch_id = persisted[0]["metadata"]["batch_id"]
    artifact_content = (
        '{"passed": true, "reason_codes": [], "flagged_claim_ids": [], "summary": '
        '{"section_count": 0, "total_claims": 0, "low_confidence_claims": 0, '
        '"single_source_claims": 0, "source_backed_binding_count": 0, '
        '"search_only_binding_count": 0, "null_span_binding_count": 0, '
        '"missing_evidence_items": 0, "mismatched_binding_source": 0, '
        '"mismatched_binding_evidence": 0, "invalid_source_backed_span": 0, '
        '"duplicate_claims": 0, "low_value_claims": 0, '
        '"medium_single_source_search_only": 0, '
        '"same_domain_off_topic_dominance": 0, '
        '"unbound_citation_sources": 0, "unbound_evidence_ids": 0}}'
    )

    assert exit_code == 0
    assert captured.out == artifact_content + "\n"
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch={batch_id} artifact_fallback=true artifact=verifier.json bytes={len(artifact_content.encode('utf-8'))}"
    ]


def test_cli_result_artifact_reads_resolved_final_batch_for_interrupted_finalizing_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Interrupted artifact batch job",
        request_fingerprint="fp-interrupted-artifact-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted artifact batch job"}), "application/json")
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
            query="Interrupted artifact batch job",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "final_report.md",
        "# Final Report\n\nStale current report.\n",
        "text/markdown",
    )

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "final_report.md"])

    assert exit_code == 0
    assert capsys.readouterr().out == "# Final Report\n\nRecovered report.\n\n"


def test_cli_status_surfaces_resolved_artifact_batch_id(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="CLI status resolved batch",
        request_fingerprint="fp-cli-status-resolved-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "CLI status resolved batch"}), "application/json")
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
            query="CLI status resolved batch",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        json.dumps([{"source_id": "R9", "url": "https://stale.example.com"}]),
        "application/json",
    )

    exit_code = deep_research_cli.main(["status", job.job_id])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["resolved_artifact_batch_id"]
    assert payload["artifact_fallback_used"] is True
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch={payload['resolved_artifact_batch_id']} artifact_fallback=true"
    ]


def test_cli_status_summary_surfaces_planner_fallback_and_warning_counts(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="CLI diagnostics job",
        request_fingerprint="fp-cli-diagnostics",
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
        "plan.json",
        json.dumps(
            {
                "query": "CLI diagnostics job",
                "planner_metadata": {
                    "planner": "fallback",
                    "used_fallback": True,
                    "fallback_reason": {"stage": "unsafe_plan", "error": "filled_search_query:u1"},
                },
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps(
            {
                "summary": "Checkpoint resume summary.",
                "sections": [],
                "unit_results": {},
                "runtime": {
                    "warnings": ["planner_fallback_used", "domain_constraints_applied"],
                    "constraint_violations": [{"unit_id": "unit-search-1", "removed_source_count": 1}],
                },
            }
        ),
        "application/json",
    )

    exit_code = deep_research_cli.main(["status", job.job_id])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false planner_fallback=true warnings=2 warning_codes=planner_fallback_used,domain_constraints_applied constraint_violations=1 constraint_codes=unit-search-1"
    ]


def test_cli_start_spawns_worker_for_background_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    exit_code = deep_research_cli.main(["start", "Run in background"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert spawned == [payload["job_id"]]


def test_cli_continue_creates_follow_up_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    original = runtime.store.create_job(
        query="Original job",
        request_fingerprint="fp-original",
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

    exit_code = deep_research_cli.main(["continue", original.job_id, "Continue from previous findings"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["continued_from_job_id"] == original.job_id
    assert payload["include_domains"] == ["docs.aws.amazon.com"]
    assert payload["exclude_domains"] == ["repost.aws"]
    assert spawned == [payload["job_id"]]


def test_cli_start_does_not_spawn_worker_for_reused_completed_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    fingerprint = runtime._request_fingerprint(
        query="Reuse me",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    job = runtime.store.create_job(
        query="Reuse me",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Reuse me"}), "application/json")
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
            query="Reuse me",
        ),
    )

    exit_code = deep_research_cli.main(["start", "Reuse me"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["reused"] is True
    assert payload["status"] == "completed"
    assert spawned == []


def test_cli_list_filters_jobs_by_status(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    completed = runtime.store.create_job(
        query="Completed job",
        request_fingerprint="fp-cli-list-completed",
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
    runtime.store.create_job(
        query="Failed job",
        request_fingerprint="fp-cli-list-failed",
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

    exit_code = deep_research_cli.main(["list", "--status", "completed"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert [job["job_id"] for job in payload["jobs"]] == [completed.job_id]
    assert summary_lines(captured.err) == [
        "summary: jobs=1 status_filter=completed limit=50",
        f'summary: job={completed.job_id} status=completed phase=finalizing progress=0.0% checkpoint=- attempts=0 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=- query="Completed job"',
    ]


def test_cli_watch_prints_events_until_terminal_status(monkeypatch, tmp_path, capsys):
    class FakeRuntime:
        def __init__(self):
            self.status_calls = 0

        async def status(self, job_id):
            self.status_calls += 1
            if self.status_calls == 1:
                return {
                    "job_id": job_id,
                    "status": "running",
                    "phase": "researching",
                    "progress_pct": 35.0,
                    "attempt_count": 1,
                    "current_checkpoint": "researching",
                    "cancel_requested": False,
                    "continued_from_job_id": "",
                    "resolved_artifact_batch_id": "",
                }
            return {
                "job_id": job_id,
                "status": "completed",
                "phase": "finalizing",
                "progress_pct": 100.0,
                "attempt_count": 1,
                "current_checkpoint": "finalizing",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "batch-1",
                "artifact_fallback_used": True,
            }

        async def events(self, job_id, after_seq=0, limit=100):
            if after_seq == 0:
                return {
                    "events": [{"seq": 1, "phase": "researching", "type": "phase_started", "message": "Researching."}],
                    "next_after_seq": 1,
                }
            return {"events": [], "next_after_seq": after_seq}

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(deep_research_cli.asyncio, "sleep", fake_sleep)

    exit_code = deep_research_cli.main(["watch", "job-123", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()
    output = captured.err

    assert exit_code == 0
    assert "[1] researching phase_started: Researching." in output
    assert "summary: job=job-123 status=running phase=researching progress=35.0% checkpoint=researching attempts=1 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=-" in output
    assert "summary: job=job-123 status=completed phase=finalizing progress=100.0% checkpoint=finalizing attempts=1 cancel_requested=false continued_from=- resolved_batch=batch-1 artifact_fallback=true" in output


def test_cli_start_watch_preserves_initial_json_response(monkeypatch, capsys):
    class FakeRuntime:
        async def start(
            self,
            *,
            query,
            context,
            effort,
            time_budget_seconds,
            include_domains,
            exclude_domains,
            continue_from_job_id,
            plan_only,
            force_new,
            schedule,
        ):
            return {
                "job_id": "job-watch-1",
                "status": "queued",
                "phase": "planning",
                "progress_pct": 0.0,
                "attempt_count": 1,
                "current_checkpoint": "planning",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "",
                "reused": False,
            }

    async def fake_watch(runtime, job_id, *, interval_seconds=1.0, after_seq=0):
        print(f"watch-stream: {job_id}", file=sys.stderr)

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: None)
    monkeypatch.setattr(deep_research_cli, "_watch_job", fake_watch)

    exit_code = deep_research_cli.main(["start", "Watch me", "--watch"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["job_id"] == "job-watch-1"
    assert "watch-stream: job-watch-1" in captured.err


def test_cli_watch_explains_reconnected_job_context(monkeypatch, capsys):
    class FakeRuntime:
        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "interrupted",
                "phase": "finalizing",
                "progress_pct": 90.0,
                "attempt_count": 3,
                "current_checkpoint": "finalizing",
                "cancel_requested": False,
                "continued_from_job_id": "seed-job",
                "resolved_artifact_batch_id": "batch-7",
            }

        async def events(self, job_id, after_seq=0, limit=100):
            return {"events": [], "next_after_seq": after_seq}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())

    exit_code = deep_research_cli.main(["watch", "job-123", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        "watch: attached_to_existing_state job=job-123 attempts=3 checkpoint=finalizing continued_from=seed-job"
        in captured.err
    )


def test_cli_watch_existing_attempt_skips_full_history_replay(monkeypatch, capsys):
    snapshot = load_deep_research_fixture("probe_round21_stale_worker_reconnect.json")
    all_events = snapshot["initial_events"] + snapshot["resume_events_after_seq_7"]

    class FakeRuntime:
        def __init__(self):
            self.event_calls = []

        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "interrupted",
                "phase": snapshot["resume_run"]["phase"],
                "progress_pct": 20.0,
                "attempt_count": snapshot["resume_run"]["attempt_count"],
                "current_checkpoint": snapshot["resume_run"]["current_checkpoint"],
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "",
                "last_error": snapshot["expected"]["interrupted_reason"],
            }

        async def events(self, job_id, after_seq=0, limit=100):
            self.event_calls.append((after_seq, limit))
            if limit == 1000:
                raise AssertionError("watch attach should not depend on limit=1000 history scans")
            if after_seq >= snapshot["resume_window"]["after_seq"]:
                return {
                    "events": [
                        {
                            "seq": event["seq"],
                            "phase": event["phase"],
                            "type": event["type"],
                            "message": event.get("message") or event["type"],
                            "data": event.get("data") or {},
                        }
                        for event in snapshot["resume_events_after_seq_7"]
                    ],
                    "next_after_seq": snapshot["resume_window"]["next_after_seq"],
                }
            page = []
            next_after_seq = after_seq
            for event in all_events:
                if event["seq"] <= after_seq:
                    continue
                page.append(
                    {
                        "seq": event["seq"],
                        "phase": event["phase"],
                        "type": event["type"],
                        "message": event.get("message") or event["type"],
                        "data": event.get("data") or {},
                    }
                )
                next_after_seq = event["seq"]
                if len(page) >= limit:
                    break
            return {
                "events": page,
                "next_after_seq": next_after_seq,
            }

    runtime = FakeRuntime()
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    exit_code = deep_research_cli.main(["watch", "job-123", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[8] researching job_resumed: Deep research job resumed from checkpoint." in captured.err
    assert "[7] researching job_interrupted: Deep research interrupted during worker recovery." not in captured.err
    assert "[1] planning job_created:" not in captured.err
    assert all(limit != 1000 for _, limit in runtime.event_calls)


def test_resolve_watch_attach_after_seq_falls_back_to_previous_terminal_anchor():
    class FakeRuntime:
        async def events(self, job_id, after_seq=0, limit=100):
            if after_seq == 0:
                return {
                    "events": [
                        {"seq": 1, "type": "job_created", "phase": "planning", "message": "created", "data": {}},
                        {"seq": 2, "type": "phase_started", "phase": "planning", "message": "planning", "data": {}},
                        {"seq": 3, "type": "phase_started", "phase": "researching", "message": "researching", "data": {}},
                        {
                            "seq": 4,
                            "type": "job_interrupted",
                            "phase": "researching",
                            "message": "worker restarted",
                            "data": {"attempt_count": 1, "reason": "worker_restarted"},
                        },
                    ],
                    "next_after_seq": 4,
                }
            return {
                "events": [
                    {
                        "seq": 5,
                        "type": "research_unit_completed",
                        "phase": "researching",
                        "message": "Completed u1.",
                        "data": {"sources_count": 3, "unit_type": "search"},
                    },
                    {
                        "seq": 6,
                        "type": "job_interrupted",
                        "phase": "researching",
                        "message": "time budget",
                        "data": {"attempt_count": 2, "reason": "time_budget_exceeded"},
                    },
                ],
                "next_after_seq": 6,
            }

    payload = {
        "job_id": "job-123",
        "attempt_count": 2,
        "current_checkpoint": "researching-u1",
        "continued_from_job_id": "",
    }

    attach_after_seq = asyncio.run(
        deep_research_cli._resolve_watch_attach_after_seq(FakeRuntime(), "job-123", payload, page_limit=4)
    )

    assert attach_after_seq == 4


def test_cli_resume_watch_passes_resume_response_as_initial_status(monkeypatch, capsys):
    response = {
        "job_id": "job-123",
        "status": "queued",
        "phase": "researching",
        "progress_pct": 0.0,
        "attempt_count": 2,
        "current_checkpoint": "researching-dispatch-a1-u1",
        "cancel_requested": False,
        "continued_from_job_id": "",
        "resolved_artifact_batch_id": "",
        "artifact_fallback_used": False,
        "planner_fallback_used": False,
        "runtime_warnings": [],
        "constraint_violations": [],
    }

    class FakeRuntime:
        async def resume(self, job_id, schedule=False):
            assert schedule is False
            return dict(response)

    seen = {}

    async def fake_watch(
        runtime,
        job_id,
        *,
        interval_seconds=1.0,
        initial_status=None,
        suppress_initial_status_line=False,
        after_seq=0,
    ):
        seen["job_id"] = job_id
        seen["initial_status"] = initial_status
        seen["suppress_initial_status_line"] = suppress_initial_status_line

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: None)
    monkeypatch.setattr(deep_research_cli, "_watch_job", fake_watch)

    exit_code = deep_research_cli.main(["resume", "job-123", "--watch"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["job_id"] == "job-123"
    assert seen == {
        "job_id": "job-123",
        "initial_status": response,
        "suppress_initial_status_line": True,
    }


def test_cli_result_includes_partial_report_by_default(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Partial by default",
        request_fingerprint="fp-cli-result-partial-default",
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

    exit_code = deep_research_cli.main(["result", job.job_id])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["partial_report"] == "# Partial Report\n\nStill working.\n"


def test_cli_result_can_disable_partial_report_explicitly(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="No partial",
        request_fingerprint="fp-cli-result-no-partial",
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

    exit_code = deep_research_cli.main(["result", job.job_id, "--no-include-partial"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["partial_report"] is None


def test_cli_watch_uses_additive_after_seq_when_reconnecting(monkeypatch, capsys):
    event_calls = []

    class FakeRuntime:
        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "interrupted",
                "phase": "researching",
                "progress_pct": 80.0,
                "attempt_count": 2,
                "current_checkpoint": "researching-u2",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "",
            }

        async def events(self, job_id, after_seq=0, limit=100):
            event_calls.append((after_seq, limit))
            return {"events": [], "next_after_seq": after_seq}

    async def fake_attach(*args, **kwargs):
        return 7

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(deep_research_cli, "_resolve_watch_attach_after_seq", fake_attach)

    exit_code = deep_research_cli.main(["watch", "job-123", "--after-seq", "11", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert event_calls == [(11, 100)]
    assert "watch: attached_to_existing_state job=job-123 attempts=2 checkpoint=researching-u2" in captured.err


def test_cli_resume_watch_passes_after_seq_to_watch_job(monkeypatch, capsys):
    response = {
        "job_id": "job-123",
        "status": "queued",
        "phase": "researching",
        "progress_pct": 0.0,
        "attempt_count": 2,
        "current_checkpoint": "researching-dispatch-a1-u1",
        "cancel_requested": False,
        "continued_from_job_id": "",
        "resolved_artifact_batch_id": "",
        "artifact_fallback_used": False,
        "planner_fallback_used": False,
        "runtime_warnings": [],
        "constraint_violations": [],
    }

    class FakeRuntime:
        async def resume(self, job_id, schedule=False):
            assert schedule is False
            return dict(response)

    seen = {}

    async def fake_watch(
        runtime,
        job_id,
        *,
        interval_seconds=1.0,
        initial_status=None,
        suppress_initial_status_line=False,
        after_seq=0,
    ):
        seen["job_id"] = job_id
        seen["initial_status"] = initial_status
        seen["suppress_initial_status_line"] = suppress_initial_status_line
        seen["after_seq"] = after_seq

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: None)
    monkeypatch.setattr(deep_research_cli, "_watch_job", fake_watch)

    exit_code = deep_research_cli.main(["resume", "job-123", "--watch", "--after-seq", "13"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert json.loads(captured.out)["job_id"] == "job-123"
    assert seen == {
        "job_id": "job-123",
        "initial_status": response,
        "suppress_initial_status_line": True,
        "after_seq": 13,
    }


def test_watch_job_refreshes_status_before_printing_summary_after_resume_event(monkeypatch, capsys):
    queued_status = {
        "job_id": "job-123",
        "status": "queued",
        "phase": "finalizing",
        "progress_pct": 0.0,
        "attempt_count": 2,
        "current_checkpoint": "finalizing",
        "cancel_requested": False,
        "continued_from_job_id": "",
        "resolved_artifact_batch_id": "",
        "artifact_fallback_used": False,
        "planner_fallback_used": False,
        "runtime_warnings": [],
        "constraint_violations": [],
    }
    running_status = {
        **queued_status,
        "status": "running",
        "phase": "researching",
        "current_checkpoint": "researching-u1",
    }
    completed_status = {
        **running_status,
        "status": "completed",
        "phase": "finalizing",
        "progress_pct": 100.0,
        "resolved_artifact_batch_id": "batch-1",
    }

    class FakeRuntime:
        def __init__(self):
            self.status_calls = 0
            self.event_calls = 0

        async def status(self, job_id):
            self.status_calls += 1
            if self.status_calls == 1:
                return running_status
            return completed_status

        async def events(self, job_id, after_seq=0, limit=100):
            self.event_calls += 1
            if self.event_calls == 1:
                return {
                    "events": [
                        {
                            "seq": 8,
                            "phase": "finalizing",
                            "type": "job_resumed",
                            "message": "Deep research job resumed from checkpoint.",
                            "data": {"resume_source": "failed_retry"},
                        }
                    ],
                    "next_after_seq": 8,
                }
            return {"events": [], "next_after_seq": after_seq}

    monkeypatch.setattr(
        deep_research_cli,
        "_watch_existing_state_message",
        lambda payload, fallback_job_id="": f"watch: attached_to_existing_state job={fallback_job_id} attempts={payload['attempt_count']} checkpoint={payload['current_checkpoint']}",
    )
    async def fake_attach(*args, **kwargs):
        return 7

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(deep_research_cli, "_resolve_watch_attach_after_seq", fake_attach)
    monkeypatch.setattr(deep_research_cli.asyncio, "sleep", fake_sleep)

    asyncio.run(
        deep_research_cli._watch_job(
            FakeRuntime(),
            "job-123",
            interval_seconds=0.01,
            initial_status=queued_status,
            suppress_initial_status_line=True,
        )
    )
    captured = capsys.readouterr()

    summaries = summary_lines(captured.err)
    assert summaries == [
        "summary: job=job-123 status=running phase=researching progress=0.0% checkpoint=researching-u1 attempts=2 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false",
        "summary: job=job-123 status=completed phase=finalizing progress=100.0% checkpoint=researching-u1 attempts=2 cancel_requested=false continued_from=- resolved_batch=batch-1 artifact_fallback=false",
    ]
    assert "[8] finalizing job_resumed: Deep research job resumed from checkpoint." in captured.err


def test_cli_round21_stale_worker_status_and_result_match_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round21_stale_worker_reconnect_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == snapshot["resume_run"]["status"]
    assert status_payload["phase"] == snapshot["resume_run"]["phase"]
    assert status_payload["current_checkpoint"] == snapshot["resume_run"]["current_checkpoint"]
    assert status_payload["current_checkpoint_kind"] == snapshot["resume_run"]["current_checkpoint_kind"]
    assert status_payload["attempt_count"] == snapshot["resume_run"]["attempt_count"]
    assert status_payload["last_error"] == snapshot["resume_run"]["last_error"]
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status={snapshot['resume_run']['status']} phase={snapshot['resume_run']['phase']} progress=0.0% checkpoint={snapshot['resume_run']['current_checkpoint']} attempts={snapshot['resume_run']['attempt_count']} cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false last_error={snapshot['resume_run']['last_error']}"
    ]

    exit_code = deep_research_cli.main(["result", job.job_id, "--include-partial"])
    result_captured = capsys.readouterr()
    result_payload = json.loads(result_captured.out)

    assert exit_code == 0
    assert result_payload["status"] == snapshot["resume_run"]["status"]
    assert result_payload["phase"] == snapshot["resume_run"]["phase"]
    assert_fixture_public_surface(snapshot, status=status_payload, result=result_payload, seeded_batch_id="")


def test_cli_round24_worker_restart_status_and_result_match_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round24_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == snapshot["final_status"]["status"]
    assert status_payload["phase"] == snapshot["final_status"]["phase"]
    assert status_payload["current_checkpoint"] == snapshot["final_status"]["current_checkpoint"]
    assert status_payload["current_checkpoint_kind"] == snapshot["final_status"]["current_checkpoint_kind"]
    assert status_payload["attempt_count"] == snapshot["final_status"]["attempt_count"]
    assert status_payload["last_error"] == snapshot["final_status"]["last_error"]
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status={snapshot['final_status']['status']} phase={snapshot['final_status']['phase']} progress=0.0% checkpoint={snapshot['final_status']['current_checkpoint']} attempts={snapshot['final_status']['attempt_count']} cancel_requested=false continued_from=- resolved_batch={seeded['batch_id']} artifact_fallback=false warnings=3 warning_codes=coverage_incomplete,domain_constraints_applied,medium_single_source_search_only constraint_violations=3 constraint_codes=restate_durable,langgraph_interrupts,comparison_synthesis"
    ]

    exit_code = deep_research_cli.main(["result", job.job_id, "--include-partial"])
    result_captured = capsys.readouterr()
    result_payload = json.loads(result_captured.out)

    assert exit_code == 0
    assert result_payload["status"] == snapshot["final_status"]["status"]
    assert result_payload["phase"] == snapshot["final_status"]["phase"]
    assert result_payload["report"]["status"] == snapshot["final_status"]["report_status"]
    assert_fixture_public_surface(
        snapshot,
        status=status_payload,
        result=result_payload,
        seeded_batch_id=seeded["batch_id"],
    )


def test_cli_round26_worker_restart_dispatch_runtime_status_and_result_match_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round26_worker_restart_dispatch_runtime_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == snapshot["resume_run"]["status"]
    assert status_payload["phase"] == snapshot["resume_run"]["phase"]
    assert status_payload["current_checkpoint"] == snapshot["resume_run"]["current_checkpoint"]
    assert status_payload["current_checkpoint_kind"] == snapshot["resume_run"]["current_checkpoint_kind"]
    assert status_payload["attempt_count"] == snapshot["resume_run"]["attempt_count"]
    assert status_payload["last_error"] == snapshot["resume_run"]["last_error"]
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status={snapshot['resume_run']['status']} phase={snapshot['resume_run']['phase']} progress=0.0% checkpoint={snapshot['resume_run']['current_checkpoint']} attempts={snapshot['resume_run']['attempt_count']} cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false last_error={snapshot['resume_run']['last_error']}"
    ]

    exit_code = deep_research_cli.main(["result", job.job_id, "--include-partial"])
    result_captured = capsys.readouterr()
    result_payload = json.loads(result_captured.out)

    assert exit_code == 0
    assert result_payload["status"] == snapshot["resume_run"]["status"]
    assert result_payload["phase"] == snapshot["resume_run"]["phase"]
    assert result_payload["partial_report"].startswith("# Partial Report")
    assert result_payload["final_report"] is None
    assert result_payload["sources"] is None
    assert result_payload["report"] is None
    assert_fixture_public_surface(snapshot, status=status_payload, result=result_payload, seeded_batch_id="")


def test_cli_watch_summary_surfaces_last_error_for_existing_state(monkeypatch, capsys):
    class FakeRuntime:
        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "interrupted",
                "phase": "researching",
                "progress_pct": 20.0,
                "attempt_count": 2,
                "current_checkpoint": "researching-u4",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "",
                "last_error": "worker_restarted",
            }

        async def events(self, job_id, after_seq=0, limit=100):
            return {"events": [], "next_after_seq": after_seq}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())

    exit_code = deep_research_cli.main(["watch", "job-123", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "last_error=worker_restarted" in captured.err


def test_cli_watch_returns_130_on_keyboard_interrupt(monkeypatch, capsys):
    async def raising_watch_job(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(deep_research_cli, "_watch_job", raising_watch_job)

    exit_code = deep_research_cli.main(["watch", "job-interrupted", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert_no_traceback(captured.err)


def test_cli_resume_watch_returns_130_on_keyboard_interrupt_without_canceling(monkeypatch, capsys):
    class FakeRuntime:
        def __init__(self):
            self.cancel_calls = 0

        async def resume(self, job_id, schedule=False):
            return {
                "job_id": job_id,
                "status": "queued",
                "phase": "researching",
                "progress_pct": 0.0,
                "attempt_count": 3,
                "current_checkpoint": "researching",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "",
                "artifact_fallback_used": False,
            }

        async def cancel(self, job_id):
            self.cancel_calls += 1
            return {"job_id": job_id, "status": "canceled", "cancel_requested": True}

    runtime = FakeRuntime()

    async def raising_watch_job(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_watch_job", raising_watch_job)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: None)

    exit_code = deep_research_cli.main(["resume", "job-resume", "--watch", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert runtime.cancel_calls == 0
    assert_no_traceback(captured.err)
    assert summary_lines(captured.err) == [
        "summary: job=job-resume status=queued phase=researching progress=0.0% checkpoint=researching attempts=3 cancel_requested=false continued_from=- resolved_batch=- artifact_fallback=false"
    ]


def test_cli_events_follow_returns_130_on_keyboard_interrupt_without_canceling(monkeypatch, capsys):
    class FakeRuntime:
        def __init__(self):
            self.cancel_calls = 0

        async def events(self, job_id, after_seq=0, limit=100):
            raise KeyboardInterrupt

        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "running",
                "phase": "researching",
            }

        async def cancel(self, job_id):
            self.cancel_calls += 1
            return {"job_id": job_id, "status": "canceled", "cancel_requested": True}

    runtime = FakeRuntime()
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    exit_code = deep_research_cli.main(["events", "job-events", "--follow", "--interval-seconds", "0.01"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert runtime.cancel_calls == 0
    assert captured.out == ""
    assert_no_traceback(captured.err)


def test_cli_resume_and_cancel_emit_consistent_operator_summaries(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    job = runtime.store.create_job(
        query="Resume me",
        request_fingerprint="fp-cli-resume",
        status="interrupted",
        phase="researching",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="seed-job",
    )
    runtime.store.update_job(
        job.job_id,
        progress_pct=45.0,
        attempt_count=2,
        current_checkpoint="researching",
        cancel_requested=True,
        finished_at=utc_now_iso(),
    )

    exit_code = deep_research_cli.main(["resume", job.job_id])
    resume_captured = capsys.readouterr()
    resume_payload = json.loads(resume_captured.out)

    assert exit_code == 0
    assert resume_payload["status"] == "queued"
    assert summary_lines(resume_captured.err) == [
        f"summary: job={job.job_id} status=queued phase=researching progress=0.0% checkpoint=researching attempts=3 cancel_requested=false continued_from=seed-job resolved_batch=- artifact_fallback=false"
    ]

    exit_code = deep_research_cli.main(["cancel", job.job_id])
    cancel_captured = capsys.readouterr()
    cancel_payload = json.loads(cancel_captured.out)

    assert exit_code == 0
    assert cancel_payload == {
        "job_id": job.job_id,
        "cancel_requested": True,
        "status": "canceled",
    }
    assert summary_lines(cancel_captured.err) == [
        f"summary: job={job.job_id} status=canceled phase=researching progress=0.0% checkpoint=researching attempts=3 cancel_requested=true continued_from=seed-job resolved_batch=- artifact_fallback=false"
    ]


def test_cli_events_prints_operator_batch_summary(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    runtime._startup_reconciled = True
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    job = runtime.store.create_job(
        query="Eventful job",
        request_fingerprint="fp-cli-events",
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
    runtime.store.append_event(job.job_id, type="phase_started", phase="researching", message="Researching.", data={})
    runtime.store.append_event(job.job_id, type="job_completed", phase="finalizing", message="Done.", data={})

    exit_code = deep_research_cli.main(["events", job.job_id, "--after-seq", "0", "--limit", "10"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert [event["type"] for event in payload["events"]] == ["phase_started", "job_completed"]
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} events=2 after_seq=0 next_after_seq=2 last_event=job_completed terminal=false"
    ]


def test_cli_round11_interrupted_status_events_and_result_remain_consistent(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round11_interrupted_finalizing_job(runtime)
    job = seeded["job"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == "interrupted"
    assert status_payload["phase"] == "finalizing"
    assert status_payload["current_checkpoint"] == "finalizing"
    assert status_payload["attempt_count"] == 2
    assert status_payload["continued_from_job_id"] == job.continued_from_job_id
    assert status_payload["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert status_payload["artifact_fallback_used"] is False
    assert status_payload["planner_fallback_used"] is True
    assert status_payload["runtime_warnings"] == ["coverage_incomplete", "planner_fallback_used"]
    assert status_payload["constraint_violations"] == []
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status=interrupted phase=finalizing progress=0.0% checkpoint=finalizing attempts=2 cancel_requested=false continued_from={job.continued_from_job_id} resolved_batch={seeded['batch_id']} artifact_fallback=false planner_fallback=true warnings=2 warning_codes=coverage_incomplete,planner_fallback_used"
    ]

    exit_code = deep_research_cli.main(["events", job.job_id, "--after-seq", "0", "--limit", "10"])
    events_captured = capsys.readouterr()
    events_payload = json.loads(events_captured.out)

    assert exit_code == 0
    assert [event["type"] for event in events_payload["events"]] == ["phase_started", "job_interrupted"]
    assert summary_lines(events_captured.err) == [
        f"summary: job={job.job_id} events=2 after_seq=0 next_after_seq=2 last_event=job_interrupted terminal=true"
    ]

    exit_code = deep_research_cli.main(["result", job.job_id])
    result_captured = capsys.readouterr()
    result_payload = json.loads(result_captured.out)

    assert exit_code == 0
    assert result_payload["status"] == "interrupted"
    assert result_payload["phase"] == "finalizing"
    assert result_payload["artifact_fallback_used"] is False
    assert result_payload["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert result_payload["plan"]["continuation"]["mode"] == "continue"
    assert result_payload["plan"]["continuation"]["checkpoint_key"] == "finalizing"
    assert result_payload["plan"]["planner_metadata"]["used_fallback"] is True
    assert result_payload["report"]["status"] == "degraded"
    assert result_payload["report"]["summary"] == seeded["report_payload"]["summary"]
    assert result_payload["sources"] == [seeded["source"]]
    assert result_payload["final_report"] == f"# Final Report\n\n{seeded['report_payload']['summary']}\n"


def test_cli_canceled_finalizing_status_and_evidence_artifact_use_resolved_final_batch(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_canceled_finalizing_job(runtime)
    job = seeded["job"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == "canceled"
    assert status_payload["phase"] == "finalizing"
    assert status_payload["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert status_payload["artifact_fallback_used"] is True
    assert "evidence_items.json" in status_payload["artifact_kinds"]
    assert any(artifact["kind"] == "evidence_items.json" for artifact in status_payload["artifacts"])
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status=canceled phase=finalizing progress=0.0% checkpoint=finalizing attempts=0 cancel_requested=true continued_from=- resolved_batch={seeded['batch_id']} artifact_fallback=true"
    ]

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "evidence_items.json"])
    result_captured = capsys.readouterr()

    assert exit_code == 0
    assert result_captured.out == seeded["evidence_text"] + "\n"
    assert summary_lines(result_captured.err) == [
        f"summary: job={job.job_id} status=canceled phase=finalizing progress=0.0% checkpoint=finalizing attempts=0 cancel_requested=true continued_from=- resolved_batch={seeded['batch_id']} artifact_fallback=true artifact=evidence_items.json bytes={len(seeded['evidence_text'].encode('utf-8'))}"
    ]


def test_cli_round12_continue_resume_status_and_events_match_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round12_continue_resume_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(["status", job.job_id])
    status_captured = capsys.readouterr()
    status_payload = json.loads(status_captured.out)

    assert exit_code == 0
    assert status_payload["status"] == snapshot["continue_status"]["status"]
    assert status_payload["phase"] == snapshot["continue_status"]["phase"]
    assert status_payload["attempt_count"] == snapshot["continue_status"]["attempt_count"]
    assert status_payload["current_checkpoint"] == snapshot["continue_status"]["checkpoint"]
    assert status_payload["continued_from_job_id"] == snapshot["source_job_id"]
    assert status_payload["planner_fallback_used"] is True
    assert summary_lines(status_captured.err) == [
        f"summary: job={job.job_id} status={snapshot['continue_status']['status']} phase={snapshot['continue_status']['phase']} progress=0.0% checkpoint={snapshot['continue_status']['checkpoint']} attempts={snapshot['continue_status']['attempt_count']} cancel_requested=false continued_from={snapshot['source_job_id']} resolved_batch=- artifact_fallback=false last_error=worker_restarted planner_fallback=true"
    ]

    exit_code = deep_research_cli.main(["events", job.job_id, "--after-seq", "0", "--limit", "20"])
    events_captured = capsys.readouterr()
    events_payload = json.loads(events_captured.out)

    assert exit_code == 0
    assert [event["type"] for event in events_payload["events"]] == snapshot["events"]


def test_cli_events_after_seq_replay_matches_round21_stale_worker_reconnect_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round21_stale_worker_reconnect_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(
        ["events", job.job_id, "--after-seq", str(snapshot["resume_window"]["after_seq"]), "--limit", "20"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in payload["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_7"]
    ]
    assert payload["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert payload["events"][0]["data"]["checkpoint_kind"] == snapshot["expected"]["checkpoint_kind"]
    assert payload["events"][1]["type"] == "checkpoint_restored"
    assert payload["events"][1]["data"]["checkpoint_kind"] == snapshot["expected"]["checkpoint_kind"]
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} events=5 after_seq={snapshot['resume_window']['after_seq']} next_after_seq={snapshot['resume_window']['next_after_seq']} last_event=job_interrupted terminal=true"
    ]


def test_cli_events_after_seq_replay_matches_round24_worker_restart_fixture(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round24_worker_restart_live_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(
        ["events", job.job_id, "--after-seq", str(snapshot["resume_window"]["after_seq"]), "--limit", "20"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in payload["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_16"]
    ]
    assert payload["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert payload["events"][1]["type"] == "checkpoint_restored"
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} events=7 after_seq={snapshot['resume_window']['after_seq']} next_after_seq={snapshot['resume_window']['next_after_seq']} last_event=job_completed terminal=true"
    ]


def test_cli_events_after_seq_replay_matches_round26_worker_restart_dispatch_runtime_fixture(
    monkeypatch, tmp_path, capsys
):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    seeded = seed_round26_worker_restart_dispatch_runtime_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    exit_code = deep_research_cli.main(
        ["events", job.job_id, "--after-seq", str(snapshot["resume_window"]["after_seq"]), "--limit", "20"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert [
        (event["seq"], event["type"], event["phase"])
        for event in payload["events"]
    ] == [
        (event["seq"], event["type"], event["phase"])
        for event in snapshot["resume_events_after_seq_4"]
    ]
    assert payload["events"][0]["data"]["resume_source"] == snapshot["expected"]["resume_source"]
    assert payload["events"][1]["type"] == "checkpoint_restored"
    assert summary_lines(captured.err) == [
        f"summary: job={job.job_id} events=7 after_seq={snapshot['resume_window']['after_seq']} next_after_seq={snapshot['resume_window']['next_after_seq']} last_event=job_interrupted terminal=true"
    ]


def test_cli_status_summary_prefers_unit_ids_when_constraint_reasons_repeat(monkeypatch, capsys):
    class FakeRuntime:
        async def status(self, job_id):
            return {
                "job_id": job_id,
                "status": "completed",
                "phase": "finalizing",
                "progress_pct": 100.0,
                "attempt_count": 4,
                "current_checkpoint": "finalizing",
                "cancel_requested": False,
                "continued_from_job_id": "",
                "resolved_artifact_batch_id": "batch-24",
                "artifact_fallback_used": False,
                "runtime_warnings": [
                    "coverage_incomplete",
                    "domain_constraints_applied",
                    "medium_single_source_search_only",
                ],
                "constraint_violations": [
                    {"reason": "domain_constraints_applied", "unit_id": "restate_durable"},
                    {"reason": "domain_constraints_applied", "unit_id": "langgraph_interrupts"},
                    {"reason": "domain_constraints_applied", "unit_id": "comparison_synthesis"},
                ],
                "planner_fallback_used": False,
            }

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FakeRuntime())

    exit_code = deep_research_cli.main(["status", "job-24"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert summary_lines(captured.err) == [
        "summary: job=job-24 status=completed phase=finalizing progress=100.0% checkpoint=finalizing attempts=4 cancel_requested=false continued_from=- resolved_batch=batch-24 artifact_fallback=false warnings=3 warning_codes=coverage_incomplete,domain_constraints_applied,medium_single_source_search_only constraint_violations=3 constraint_codes=restate_durable,langgraph_interrupts,comparison_synthesis"
    ]


def test_spawn_worker_writes_logs_to_worker_log_dir(monkeypatch, tmp_path):
    captured = {}
    runtime_dir = tmp_path / "deep-research"
    monkeypatch.setenv("GROK_DEEP_RESEARCH_DIR", str(runtime_dir))

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
        captured["cmd"] = cmd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["start_new_session"] = start_new_session
        return object()

    monkeypatch.setattr(deep_research_cli.subprocess, "Popen", fake_popen)

    deep_research_cli._spawn_worker("job-123")

    assert captured["cmd"][-2:] == ["_worker", "job-123"]
    assert captured["start_new_session"] is True
    assert Path(captured["stdout"].name).name == "job-123.stdout.log"
    assert Path(captured["stderr"].name).name == "job-123.stderr.log"
    assert Path(captured["stdout"].name).parent == runtime_dir / "worker-logs"
    captured["stdout"].close()
    captured["stderr"].close()


def test_cli_worker_returns_nonzero_when_job_fails(monkeypatch, tmp_path):
    class FailingRuntime:
        async def run_job(self, job_id):
            return {"status": "failed"}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FailingRuntime())

    exit_code = deep_research_cli.main(["_worker", "job-123"])

    assert exit_code == 1


def test_cli_worker_returns_nonzero_when_job_is_interrupted(monkeypatch, tmp_path):
    class InterruptedRuntime:
        async def run_job(self, job_id):
            return {"status": "interrupted"}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: InterruptedRuntime())

    exit_code = deep_research_cli.main(["_worker", "job-123"])

    assert exit_code == 1


def test_cli_worker_does_not_interrupt_fresh_target_job_during_startup_reconcile(monkeypatch, tmp_path):
    seed_runtime = build_runtime(tmp_path)
    job = seed_runtime.store.create_job(
        query="CLI worker fresh target",
        request_fingerprint="fp-cli-worker-fresh-target",
        status="queued",
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
    seed_runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps({"query": "CLI worker fresh target"}),
        "application/json",
    )

    async def fake_runner(current_runtime, job_id):
        current_runtime.store.append_event(
            job_id,
            type="phase_started",
            phase="planning",
            message="Planning started.",
            data={},
        )
        current_runtime.store.update_job(
            job_id,
            status="completed",
            phase="finalizing",
            progress_pct=100.0,
            finished_at=utc_now_iso(),
            heartbeat_at=utc_now_iso(),
            last_error="",
        )
        current_runtime.store.append_event(
            job_id,
            type="job_completed",
            phase="finalizing",
            message="Done.",
            data={},
        )

    worker_runtime = DeepResearchRuntime(seed_runtime.store.root_dir, runner=fake_runner)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: worker_runtime)

    exit_code = deep_research_cli.main(["_worker", job.job_id])
    events = worker_runtime.store.list_events(job.job_id, limit=20)
    event_types = [event.type for event in events]
    refreshed = worker_runtime.store.get_job(job.job_id)

    assert exit_code == 0
    assert "job_interrupted" not in event_types
    assert event_types == ["phase_started", "job_completed"]
    assert refreshed.status == "completed"
