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
    report_payload = report_snapshot["report"]
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
        json.dumps({"summary": "stale current report", "runtime": {"warnings": [], "constraint_violations": []}}),
        "application/json",
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
    assert status["artifact_kinds"] == ["plan.json", "planner_trace.json", "partial_report.md", "citations.json", "final_report.md"]
    assert [event["seq"] for event in events["events"]] == list(range(1, len(events["events"]) + 1))
    assert events["events"][0]["type"] in {"planner_fallback", "job_created"}
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
                "content": '{"summary":"Good report","sections":[],"unit_results":{}}',
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nGood report.\n",
                "content_type": "text/markdown",
            },
        ],
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
    assert status["artifact_fallback_used"] is True
    assert status["planner_fallback_used"] is True
    assert status["runtime_warnings"] == ["coverage_incomplete", "planner_fallback_used"]
    assert status["constraint_violations"] == []

    assert [event["type"] for event in events["events"]] == ["phase_started", "job_interrupted"]
    assert events["next_after_seq"] == 2

    assert result["status"] == "interrupted"
    assert result["phase"] == "finalizing"
    assert result["artifact_fallback_used"] is True
    assert result["resolved_artifact_batch_id"] == seeded["batch_id"]
    assert result["plan"]["continuation"]["mode"] == "continue"
    assert result["plan"]["continuation"]["checkpoint_key"] == "finalizing"
    assert result["plan"]["planner_metadata"]["used_fallback"] is True
    assert result["report"]["status"] == "degraded"
    assert result["report"]["summary"] == seeded["report_payload"]["summary"]
    assert result["sources"] == [seeded["source"]]
    assert result["final_report"] == f"# Final Report\n\n{seeded['report_payload']['summary']}\n"
