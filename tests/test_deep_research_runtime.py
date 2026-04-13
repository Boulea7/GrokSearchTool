import asyncio
import json

import pytest

from grok_search.deep_research_runtime import DeepResearchRuntime
from grok_search.deep_research_types import utc_now_iso


def build_runtime(tmp_path):
    return DeepResearchRuntime(tmp_path / "deep-research")


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

    assert plan["query"] == "Compare open-source deep research runtimes"
    assert plan["brief"]["objective"]
    assert plan["sub_questions"]
    assert plan["search_strategy"]["search_queries"]
    assert plan["report_outline"]
    assert plan["research_units"]
    assert plan["continuation"]["mode"] == "fresh"
    assert plan["planner_metadata"]["used_fallback"] is False
    assert "carry_forward_sources" not in plan["continuation"]


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
    assert status["artifact_kinds"] == ["plan.json"]


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
    assert "resume behavior" in continuation["previous_summary"].lower()
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
    assert result["status"] == "completed"
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

    assert set(plan_continuation) == {
        "mode",
        "source_job_id",
        "source_job_status",
        "previous_summary",
        "prior_plan_summary",
        "continuation_goal",
        "source_count",
        "checkpoint_key",
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

    assert len(sub_questions) == 1
    assert sub_questions[0]["question"] == "Follow up on migration and checkpoint resume tradeoffs runtime"
    assert outline_titles == ["Executive Summary", "Follow-up Findings", "Remaining Gaps"]
    assert validation["repaired"] is True
    assert "duplicate_sub_questions" in validation["issues"]
    assert "generic_continuation_outline" in validation["issues"]


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

    assert "No content fetched." not in result["final_report"]
    assert "No site map returned." not in result["final_report"]


@pytest.mark.asyncio
async def test_continuation_start_reuses_recent_completed_follow_up_job(tmp_path):
    runtime = build_runtime(tmp_path)
    original = runtime.store.create_job(
        query="Original research",
        request_fingerprint="fp-original-source",
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
    fingerprint = runtime._request_fingerprint(
        query="Follow up query",
        context="Stay technical.",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id=original.job_id,
    )
    follow_up = runtime.store.create_job(
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
        continued_from_job_id=original.job_id,
    )
    runtime.store.update_job(follow_up.job_id, finished_at=utc_now_iso())
    runtime.write_artifact(
        follow_up.job_id,
        "plan.json",
        json.dumps(structured_plan_payload(follow_up, {"mode": "continue"})),
        "application/json",
    )

    response = await runtime.start(
        query="Follow up query",
        context="Stay technical.",
        continue_from_job_id=original.job_id,
        force_new=False,
        schedule=False,
    )

    assert response["reused"] is True
    assert response["job_id"] == follow_up.job_id


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
    }


@pytest.mark.asyncio
async def test_run_job_executes_independent_units_concurrently(monkeypatch, tmp_path):
    runtime = build_runtime(tmp_path)
    started = []

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
        started.append(query)
        await asyncio.sleep(0.05)
        return (f"Answer for {query}", [{"url": f"https://example.com/{query}", "title": query}])

    async def no_fetch(url):
        return None

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._search_query", fake_search)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", no_fetch)
    monkeypatch.setenv("GROK_DEEP_RESEARCH_MAX_CONCURRENCY", "2")

    response = await runtime.start(query="Concurrent runtime", force_new=True, schedule=False)

    start = asyncio.get_running_loop().time()
    await runtime.run_job(response["job_id"])
    elapsed = asyncio.get_running_loop().time() - start

    assert started == ["one", "two"]
    assert elapsed < 0.09


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
async def test_invalid_depends_on_falls_back_to_valid_plan(tmp_path):
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

    assert response["plan"]["planner_metadata"]["planner"] == "fallback"
    assert response["plan"]["planner_metadata"]["used_fallback"] is True


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
