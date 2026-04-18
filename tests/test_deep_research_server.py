import asyncio
import json
from pathlib import Path

import pytest

from grok_search import server
from grok_search import deep_research_runtime
from grok_search.deep_research_runtime import DeepResearchRuntime
from grok_search.deep_research_types import utc_now_iso


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_research"


def build_runtime(tmp_path, runner):
    return DeepResearchRuntime(tmp_path / "deep-research", runner=runner)


def load_deep_research_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


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
        request_fingerprint="fp-server-round11-interrupted-parity",
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
        request_fingerprint="fp-server-round21-stale-worker-reconnect",
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
        request_fingerprint="fp-server-canceled-finalizing-visibility",
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
    }


def seed_round13_failed_continuation_job(runtime: DeepResearchRuntime):
    snapshot = load_deep_research_fixture("probe_round13_continue_resume_snapshot.json")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint="fp-server-round13-failed-continuation-parity",
        status="failed",
        phase="finalizing",
        effort="standard",
        context="",
        include_domains=snapshot["plan"]["include_domains"],
        exclude_domains=snapshot["plan"]["exclude_domains"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id=snapshot["continuation"]["source_job_id"],
    )
    runtime.store.update_job(
        job.job_id,
        current_checkpoint=snapshot["continuation"]["checkpoint_key"],
        finished_at=utc_now_iso(),
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(
            {
                "query": snapshot["query"],
                "include_domains": snapshot["plan"]["include_domains"],
                "exclude_domains": snapshot["plan"]["exclude_domains"],
                "continuation": snapshot["continuation"],
                "brief": snapshot["plan"]["brief"],
                "planner_metadata": snapshot["planner"],
            }
        ),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "report.json",
        json.dumps(snapshot["report"]),
        "application/json",
    )
    runtime.write_artifact(
        job.job_id,
        "continuation.json",
        json.dumps(snapshot["continuation"]),
        "application/json",
    )
    return {"job": job, "snapshot": snapshot}


async def complete_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    runtime.store.update_job(job_id, status="running", phase="planning", started_at="2026-04-12T14:00:00Z")
    runtime.store.append_event(job_id, type="phase_started", phase="planning", message="Planning started.", data={})
    runtime.store.save_checkpoint(job_id, phase="planning", checkpoint_key="planning", state={"ready": True})
    runtime.write_artifact(job_id, kind="partial_report.md", content="# Partial Report\n\nWorking...", content_type="text/markdown")
    runtime.store.update_job(job_id, phase="finalizing", progress_pct=85.0)
    runtime.store.append_event(job_id, type="phase_started", phase="finalizing", message="Finalizing report.", data={})
    runtime.write_artifact(
        job_id,
        kind="final_report.md",
        content="# Final Report\n\nDone.",
        content_type="text/markdown",
    )
    runtime.write_artifact(
        job_id,
        kind="citations.json",
        content='{"R1": {"url": "https://example.com"}}',
        content_type="application/json",
    )
    runtime.store.update_job(
        job_id,
        status="completed",
        phase="finalizing",
        progress_pct=100.0,
        finished_at="2026-04-12T14:01:00Z",
        heartbeat_at="2026-04-12T14:01:00Z",
    )
    runtime.store.append_event(job_id, type="job_completed", phase="finalizing", message="Deep research completed.", data={})


async def waiting_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    runtime.store.update_job(job_id, status="running", phase="researching", started_at="2026-04-12T15:00:00Z")
    runtime.store.append_event(job_id, type="phase_started", phase="researching", message="Researching.", data={})
    await asyncio.sleep(0.05)
    if runtime.store.get_job(job_id).cancel_requested:
        runtime.store.update_job(
            job_id,
            status="canceled",
            phase="researching",
            finished_at="2026-04-12T15:00:05Z",
            heartbeat_at="2026-04-12T15:00:05Z",
        )
        runtime.store.append_event(job_id, type="job_canceled", phase="researching", message="Canceled.", data={})
        return
    runtime.store.update_job(job_id, status="completed", phase="finalizing", progress_pct=100.0)


@pytest.fixture(autouse=True)
def reset_server_runtime(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    yield


@pytest.mark.asyncio
async def test_deep_research_start_status_events_result_and_list(tmp_path):
    response = await server.deep_research_start(
        query="Compare open-source deep research frameworks",
        context="Focus on checkpoint and resume behavior.",
        effort="standard",
    )

    assert response["job_id"]
    assert response["reused"] is False
    assert response["status"] in {"queued", "running"}
    assert response["plan"]["query"] == "Compare open-source deep research frameworks"

    await asyncio.sleep(0.01)

    status = await server.deep_research_status(response["job_id"])
    events = await server.deep_research_events(response["job_id"])
    result = await server.deep_research_result(response["job_id"])
    listing = await server.deep_research_list()

    assert status["status"] == "completed"
    assert "plan.json" in status["artifact_kinds"]
    assert "planner_trace.json" in status["artifact_kinds"]
    assert "outline_state.json" in status["artifact_kinds"]
    assert "evidence_ledger.json" in status["artifact_kinds"]
    assert "section_banks.json" in status["artifact_kinds"]
    assert "partial_report.md" in status["artifact_kinds"]
    assert "citations.json" in status["artifact_kinds"]
    assert "final_report.md" in status["artifact_kinds"]
    assert [event["seq"] for event in events["events"]] == list(range(1, len(events["events"]) + 1))
    assert events["events"][0]["type"] == "job_created"
    assert events["events"][-1]["type"] == "job_completed"
    assert result["final_report"].startswith("# Final Report")
    assert result["partial_report"].startswith("# Partial Report")
    assert result["citations"]["source_registry"]["R1"]["url"] == "https://example.com"
    assert listing["jobs"][0]["job_id"] == response["job_id"]


@pytest.mark.asyncio
async def test_deep_research_cancel_updates_job_state(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, waiting_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)

    response = await server.deep_research_start(query="Long-running research", effort="deep")
    canceled = await server.deep_research_cancel(response["job_id"])

    await asyncio.sleep(0.08)
    status = await server.deep_research_status(response["job_id"])
    events = await server.deep_research_events(response["job_id"])

    assert canceled["cancel_requested"] is True
    assert status["status"] == "canceled"
    assert events["events"][-1]["type"] == "job_canceled"


@pytest.mark.asyncio
async def test_deep_research_events_after_seq_matches_round21_stale_worker_reconnect_fixture(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    seeded = seed_round21_stale_worker_reconnect_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    payload = await server.deep_research_events(
        job.job_id,
        after_seq=snapshot["resume_window"]["after_seq"],
        limit=20,
    )

    assert payload["next_after_seq"] == snapshot["resume_window"]["next_after_seq"]
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
    assert not any(
        event["type"] == "phase_started" and event["phase"] == "planning"
        for event in payload["events"]
    )


@pytest.mark.asyncio
async def test_deep_research_round21_stale_worker_status_and_result_match_fixture(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    seeded = seed_round21_stale_worker_reconnect_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await server.deep_research_status(job.job_id)
    result = await server.deep_research_result(job.job_id)

    assert status["status"] == snapshot["resume_run"]["status"]
    assert status["phase"] == snapshot["resume_run"]["phase"]
    assert status["current_checkpoint"] == snapshot["resume_run"]["current_checkpoint"]
    assert status["current_checkpoint_kind"] == snapshot["resume_run"]["current_checkpoint_kind"]
    assert status["attempt_count"] == snapshot["resume_run"]["attempt_count"]
    assert status["last_error"] == snapshot["resume_run"]["last_error"]
    assert result["status"] == snapshot["resume_run"]["status"]
    assert result["phase"] == snapshot["resume_run"]["phase"]
    assert result["resolved_artifact_batch_id"] == ""
    assert result["partial_report"] is None


@pytest.mark.asyncio
async def test_deep_research_resume_from_draft_preserves_plan_only_job(tmp_path):
    response = await server.deep_research_start(
        query="Plan first, run later",
        context="Need a visible plan before execution.",
        plan_only=True,
    )

    draft_result = await server.deep_research_result(response["job_id"])
    assert draft_result["status"] == "draft"
    assert draft_result["plan"]["context"] == "Need a visible plan before execution."
    assert draft_result["partial_report"] is None

    resumed = await server.deep_research_resume(response["job_id"])
    await asyncio.sleep(0.01)
    status = await server.deep_research_status(response["job_id"])
    events = await server.deep_research_events(response["job_id"])

    assert resumed["job_id"] == response["job_id"]
    assert resumed["status"] == "draft"
    assert status["status"] == "draft"
    assert any(event["type"] == "plan_only_execution_blocked" for event in events["events"])


@pytest.mark.asyncio
async def test_search_query_falls_back_to_extracting_inline_urls(monkeypatch):
    async def fake_provider_search(provider, query, **kwargs):
        return (
            "SQLite is simple for local tooling. Source: https://example.com/sqlite",
            [],
        )

    monkeypatch.setattr(deep_research_runtime, "_provider_search_with_sources", fake_provider_search)
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")

    answer, sources = await deep_research_runtime._search_query("SQLite vs PostgreSQL")

    assert "SQLite is simple" in answer
    assert sources[0]["url"] == "https://example.com/sqlite"


@pytest.mark.asyncio
async def test_deep_research_result_surfaces_artifact_errors(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    job = runtime.store.create_job(
        query="Corrupt artifact result",
        request_fingerprint="fp-server-corrupt-artifact",
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

    result = await server.deep_research_result(job.job_id)

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
async def test_deep_research_result_prefers_resolved_final_batch_over_current_mixed_artifacts(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    job = runtime.store.create_job(
        query="Mixed artifact server result",
        request_fingerprint="fp-server-mixed-artifact",
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
    runtime.write_artifact(job.job_id, "plan.json", '{"query": "Mixed artifact server result"}', "application/json")
    runtime.write_artifact_batch(
        job.job_id,
        with_minimal_provenance_artifacts(
            [
            {
                "kind": "sources.json",
                "content": '[{"source_id":"R1","url":"https://good.example.com"}]',
                "content_type": "application/json",
            },
            {
                "kind": "citations.json",
                "content": '{"source_registry":{"R1":{"source_id":"R1","url":"https://good.example.com"}},"sections":[]}',
                "content_type": "application/json",
            },
            {
                "kind": "report.json",
                "content": '{"summary":"Good report summary","sections":[],"unit_results":{}}',
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nGood report summary.\n",
                "content_type": "text/markdown",
            },
            ],
            query="Mixed artifact server result",
        ),
    )
    runtime.write_artifact(
        job.job_id,
        "sources.json",
        '[{"source_id":"R9","url":"https://stale.example.com"}]',
        "application/json",
    )

    result = await server.deep_research_result(job.job_id)

    assert result["sources"][0]["url"] == "https://good.example.com"
    assert result["resolved_artifact_batch_id"]


@pytest.mark.asyncio
async def test_deep_research_round11_interrupted_status_events_and_result_remain_consistent(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    seeded = seed_round11_interrupted_finalizing_job(runtime)
    job = seeded["job"]

    status = await server.deep_research_status(job.job_id)
    events = await server.deep_research_events(job.job_id)
    result = await server.deep_research_result(job.job_id)

    assert status["status"] == "interrupted"
    assert status["phase"] == "finalizing"
    assert status["current_checkpoint"] == "finalizing"
    assert status["attempt_count"] == 2
    assert status["continued_from_job_id"] == job.continued_from_job_id
    assert status["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert status["artifact_fallback_used"] is False
    assert status["planner_fallback_used"] is True
    assert status["runtime_warnings"] == ["coverage_incomplete", "planner_fallback_used"]
    assert status["constraint_violations"] == []

    assert [event["type"] for event in events["events"]] == ["phase_started", "job_interrupted"]
    assert events["next_after_seq"] == 2

    assert result["status"] == "interrupted"
    assert result["phase"] == "finalizing"
    assert result["artifact_fallback_used"] is False
    assert result["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert result["plan"]["continuation"]["mode"] == "continue"
    assert result["plan"]["continuation"]["checkpoint_key"] == "finalizing"
    assert result["plan"]["planner_metadata"]["used_fallback"] is True
    assert result["report"]["status"] == "degraded"
    assert result["report"]["summary"] == seeded["report_payload"]["summary"]
    assert result["sources"] == [seeded["source"]]
    assert result["final_report"] == f"# Final Report\n\n{seeded['report_payload']['summary']}\n"


@pytest.mark.asyncio
async def test_deep_research_canceled_finalizing_status_and_result_expose_resolved_evidence_artifact(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    seeded = seed_canceled_finalizing_job(runtime)
    job = seeded["job"]

    status = await server.deep_research_status(job.job_id)
    result = await server.deep_research_result(job.job_id)

    assert status["status"] == "canceled"
    assert status["phase"] == "finalizing"
    assert status["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert status["artifact_fallback_used"] is True
    assert "evidence_items.json" in status["artifact_kinds"]
    assert any(artifact["kind"] == "evidence_items.json" for artifact in status["artifacts"])
    assert result["status"] == "canceled"
    assert result["phase"] == "finalizing"
    assert result["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert result["artifact_fallback_used"] is True
    assert result["report"]["summary"] == "Recovered final batch report"
    assert result["sources"][0]["url"] == "https://good.example.com/runtime/recovery"
    assert any(artifact["kind"] == "evidence_items.json" for artifact in result["artifacts"])


@pytest.mark.asyncio
async def test_deep_research_result_forwards_include_partial_flag(monkeypatch, tmp_path):
    class FakeRuntime:
        async def result(self, job_id, *, include_partial=True):
            return {"job_id": job_id, "include_partial": include_partial}

    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", FakeRuntime())

    result = await server.deep_research_result("job-include-partial", include_partial=False)

    assert result == {"job_id": "job-include-partial", "include_partial": False}


@pytest.mark.asyncio
async def test_deep_research_round13_failed_source_continuation_result_matches_fixture(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path, complete_runner)
    monkeypatch.setattr(server, "_DEEP_RESEARCH_RUNTIME", runtime)
    seeded = seed_round13_failed_continuation_job(runtime)
    job = seeded["job"]
    snapshot = seeded["snapshot"]

    status = await server.deep_research_status(job.job_id)
    result = await server.deep_research_result(job.job_id)

    assert status["status"] == "failed"
    assert status["phase"] == "finalizing"
    assert status["continued_from_job_id"] == snapshot["continuation"]["source_job_id"]
    assert result["plan"]["continuation"]["mode"] == "continue"
    assert result["plan"]["continuation"]["source_job_status"] == "failed"
    assert result["plan"]["continuation"]["carry_forward_sources"] == snapshot["continuation"]["carry_forward_sources"]
    assert result["report"]["status"] == "failed"
    assert result["report"]["runtime"]["warnings"] == snapshot["report"]["runtime"]["warnings"]
