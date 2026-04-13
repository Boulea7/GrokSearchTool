import asyncio

import pytest

from grok_search import server
from grok_search import deep_research_runtime
from grok_search.deep_research_runtime import DeepResearchRuntime


def build_runtime(tmp_path, runner):
    return DeepResearchRuntime(tmp_path / "deep-research", runner=runner)


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
    assert status["artifact_kinds"] == ["citations.json", "final_report.md", "partial_report.md", "plan.json"]
    assert [event["seq"] for event in events["events"]] == [1, 2, 3, 4]
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
async def test_deep_research_resume_from_draft_uses_existing_plan(tmp_path):
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

    assert resumed["job_id"] == response["job_id"]
    assert status["status"] == "completed"


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
