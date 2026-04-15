import asyncio
import json
from pathlib import Path

import pytest

from grok_search import server
from grok_search.deep_research_runtime import (
    DeepResearchRuntime,
    _build_report_summary,
    _coverage_for_report,
    _search_query,
)
from grok_search.providers.grok import GrokSearchProvider
from grok_search.deep_research_types import DeepResearchPlan, utc_now_iso


def build_runtime(tmp_path):
    return DeepResearchRuntime(tmp_path / "deep-research")


def load_deep_research_fixture(name: str) -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "deep_research" / name
    return json.loads(fixture_path.read_text())


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
    runtime.write_artifact_batch(
        job.job_id,
        [
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
        ],
    )
    runtime.store.save_checkpoint(
        job.job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state={
            "plan": plan_payload,
            "completed_unit_ids": list(unit_results),
            "unit_results": unit_results,
            "sources": source_registry,
            "evidence_items": [
                {
                    "evidence_id": "evidence-unit-search-1-search",
                    "unit_id": "unit-search-1",
                    "summary": "Resume continues from the last checkpoint.",
                    "detail": "Resume continues from the last checkpoint.",
                    "source_ids": ["R1"],
                    "source_urls": ["https://docs.example.com/runtime/checkpoints"],
                }
            ],
            "sections": sections,
        },
    )
    return runtime.store.get_job(job.job_id)


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
    assert status["artifact_kinds"] == ["plan.json", "planner_trace.json"]


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

    status = await runtime.status(stale_job.job_id)
    events = await runtime.events(stale_job.job_id)

    assert status["status"] == "interrupted"
    assert any(event["type"] == "job_interrupted" for event in events["events"])


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
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "_execute_completion_with_retry_result", fake_execute)

    await runtime.start(query="Deep effort fallback", effort="deep", plan_only=True, force_new=True, schedule=False)

    assert observed_models == ["grok-4.20-0309-reasoning"]


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

    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert plan["research_units"][0]["unit_type"] == "search"
    assert plan["research_units"][0]["status"] == "pending"
    assert "continuation workflow" not in plan_json
    assert plan["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert "generic_continuation_outline" in plan["planner_metadata"]["validation"]["issues"]


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

    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(server, "_get_available_models_cached", fake_models)
    monkeypatch.setattr(GrokSearchProvider, "search_with_sources", fake_search_with_sources)

    from grok_search.deep_research_runtime import _search_query_with_details

    result = await _search_query_with_details("Search preselection")

    assert result["warning_code"] == "body_missing_sources_only"
    assert result["requested_model"] == "grok-4.20-0309"
    assert result["effective_model"] == "grok-4.20-0309-non-reasoning"
    assert result["provider_name"] == "fallback_1"
    assert result["provider_model"] == "grok-4.20-0309-non-reasoning"
    assert result["provider_api_url"] == "https://secondary.example.com/v1"
    assert result["sources"][0]["url"] == "https://docs.example.com/a"


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

    assert result["status"] == "completed"
    assert result["report"]["status"] == "degraded"
    assert "body_missing_sources_only" in result["report"]["runtime"]["warnings"]


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
    assert result["report"]["runtime"]["skipped_units"] == [
        {
            "unit_id": "unit-search-2",
            "unit_type": "search",
            "reason": "max_search_queries_reached",
        }
    ]
    assert result["report"]["runtime"]["constraint_violations"] == [
        {
            "unit_id": "unit-search-1",
            "removed_source_count": 2,
            "reason": "domain_constraints_applied",
        }
    ]


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

    assert {
        "mode",
        "source_job_id",
        "source_job_status",
        "continuation_identity",
        "previous_summary",
        "prior_plan_summary",
        "continuation_goal",
        "source_count",
        "checkpoint_key",
    }.issubset(plan_continuation)
    assert plan_continuation["state_version"] >= 2
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

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert len(sub_questions) >= 1
    assert outline_titles == ["Executive Summary", "Follow-up Findings", "Remaining Gaps"]
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert validation["repaired"] is True
    assert "duplicate_sub_questions" in validation["issues"]
    assert "generic_continuation_outline" in validation["issues"]


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

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert outline_titles == ["Executive Summary", "Follow-up Findings", "Remaining Gaps"]
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert validation["repaired"] is True
    assert "string_report_outline_items" in validation["issues"]
    assert "generic_continuation_outline" in validation["issues"]


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

    assert "No content fetched." not in result["final_report"]
    assert "No site map returned." not in result["final_report"]


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

    assert result["status"] == "completed"
    assert result["report"]["status"] == "degraded"
    assert {event["type"] for event in events["events"]} >= {"research_unit_failed", "job_completed"}
    assert not any(event["type"] == "research_unit_completed" for event in events["events"])
    assert result["report"]["runtime"]["failed_units"] == [
        {"reason": "empty_fetch_result", "unit_id": "unit-fetch-1", "unit_type": "fetch"},
        {"reason": "empty_map_result", "unit_id": "unit-map-1", "unit_type": "map"},
    ]


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

    assert result["status"] == "completed"
    assert result["report"]["status"] == "degraded"
    assert any(event["type"] == "research_unit_failed" and event["message"] == "Failed unit-fetch-1." for event in events["events"])
    assert any(event["type"] == "research_unit_skipped" and event["message"] == "Skipped unit-search-2." for event in events["events"])
    assert result["report"]["runtime"]["skipped_units"] == [
        {"reason": "dependency_failed", "unit_id": "unit-search-2", "unit_type": "search"}
    ]


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
"""

    async def fake_fetch(url):
        fetched_urls.append(url)
        if url.endswith("/resume"):
            return "# Resume docs\n\nResume-processing continues from the last checkpoint."
        return "# Restart docs\n\nRestart replays the task from a fresh starting point."

    monkeypatch.setattr(runtime, "_generate_plan_with_model", fake_planner)
    monkeypatch.setattr("grok_search.deep_research_runtime._map_url", fake_map)
    monkeypatch.setattr("grok_search.deep_research_runtime._fetch_url", fake_fetch)

    response = await runtime.start(query="Map then fetch docs", force_new=True, schedule=False)
    result = await runtime.run_job(response["job_id"])

    assert fetched_urls == [
        "https://docs.example.com/runtime/resume",
        "https://docs.example.com/runtime/restart",
    ]
    assert "Resume-processing continues from the last checkpoint" in result["final_report"]
    assert "Restart replays the task from a fresh starting point" in result["final_report"]


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
    assert "Resume-processing continues from the last checkpoint" in result["final_report"]
    assert "Flink restart restores a job graph from a savepoint" not in result["final_report"]


@pytest.mark.asyncio
async def test_continuation_start_reuses_recent_completed_follow_up_job(tmp_path):
    runtime = build_runtime(tmp_path)
    original = create_completed_source_job(runtime, query="Original research")
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
                "content": json.dumps({"summary": "Reusable summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nReusable summary.",
                "content_type": "text/markdown",
            },
        ],
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
                "content": json.dumps({"summary": "Reusable follow-up summary.", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nReusable follow-up summary.",
                "content_type": "text/markdown",
            },
        ],
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
    )
    runtime.write_artifact_batch(
        job.job_id,
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
        [
            {"kind": "sources.json", "content": json.dumps([{"source_id": "R1", "url": "https://good.example.com"}]), "content_type": "application/json"},
            {"kind": "citations.json", "content": json.dumps({"source_registry": {"R1": {"source_id": "R1", "url": "https://good.example.com"}}, "sections": []}), "content_type": "application/json"},
            {"kind": "report.json", "content": json.dumps({"summary": "Good batch", "sections": [], "unit_results": {}}), "content_type": "application/json"},
            {"kind": "final_report.md", "content": "# Final Report\n\nGood batch.", "content_type": "text/markdown"},
        ],
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
    runtime.store.update_job(job.job_id, current_checkpoint="finalizing", finished_at=utc_now_iso())
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Reuse interrupted final batch"}), "application/json")
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
                "content": json.dumps({"summary": "Recovered report", "sections": [], "unit_results": {}}),
                "content_type": "application/json",
            },
            {
                "kind": "final_report.md",
                "content": "# Final Report\n\nRecovered report.\n",
                "content_type": "text/markdown",
            },
        ],
    )

    response = await runtime.start(query="Reuse interrupted final batch", force_new=False, schedule=False)

    assert response["reused"] is True
    assert response["job_id"] == job.job_id
    assert response["status"] == "completed"


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
    )

    resumed = await runtime.resume(job.job_id, schedule=False)

    assert resumed["status"] == "queued"
    assert runtime.store.get_job(job.job_id).status == "queued"


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
    )

    resumed = await runtime.resume(job.job_id, schedule=False)
    status = await runtime.status(job.job_id)
    events = await runtime.events(job.job_id)

    assert resumed["status"] == "completed"
    assert status["status"] == "completed"
    assert any(event["type"] == "job_resolved_from_final_batch" for event in events["events"])
    assert not any(event["type"] == "job_completed" for event in events["events"])
    assert runtime.read_artifact_text(job.job_id, "final_report.md") == "# Final Report\n\nRecovered final report.\n"


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
    assert elapsed < 0.14


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
async def test_invalid_depends_on_uses_focused_fallback_plan(tmp_path):
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

    assert plan["planner_metadata"]["planner"] == "fallback"
    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert plan["research_units"][0]["depends_on"] == []
    assert trace["unsafe_plan"] is True
    assert "dropped_unknown_dependency:unit-search-1->unknown-unit" in trace["normalize_actions"]
    assert "unknown_dependency:unit-search-1->unknown-unit" in trace["blocked_reasons"]


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
async def test_missing_sub_question_coverage_uses_focused_fallback_plan(tmp_path):
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

    assert plan["planner_metadata"]["planner"] == "fallback"
    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["unsafe_plan"] is True
    assert "added_sub_question_search_unit:sq2" in trace["normalize_actions"]
    assert "added_sub_question_search_unit:sq3" in trace["normalize_actions"]
    assert "missing_sub_question_unit_coverage" in trace["validation_issues"]


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

    assert response["plan"]["planner_metadata"]["used_fallback"] is True
    assert response["plan"]["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert outline_titles == ["Executive Summary", "Key Findings", "Open Questions"]
    assert response["plan"]["planner_metadata"]["trace"]["unsafe_plan"] is True
    assert "expanded_outline_from_sub_questions" in trace["normalize_actions"]
    assert "generic_outline_for_sub_questions" in validation["issues"]


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

    async def slow_search(query):
        executed.append(query)
        await asyncio.sleep(1.2)
        return (
            f"Evidence for {query}",
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

    assert interrupted["status"] == "interrupted"
    assert status["status"] == "interrupted"
    assert status["last_error"] == "time_budget_exceeded"
    assert runtime.store.read_artifact_text(response["job_id"], "partial_report.md")
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md") is None
    assert runtime.store.get_job(response["job_id"]).current_checkpoint == "researching-unit-search-1"
    assert any(event["type"] == "job_interrupted" for event in events["events"])
    assert executed == ["first checkpoint"]

    resumed = await runtime.resume(response["job_id"], schedule=False)
    completed = await runtime.run_job(response["job_id"])

    assert resumed["status"] == "queued"
    assert completed["status"] == "completed"
    assert executed == ["first checkpoint", "second checkpoint"]
    assert runtime.store.get_job(response["job_id"]).finished_at
    assert runtime.store.read_artifact_text(response["job_id"], "final_report.md")


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
    )
    runtime.write_artifact_batch(
        original.job_id,
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

    assert claim["cluster_type"] == "single_source"
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
                "depends_on": [],
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

    assert plan["planner_metadata"]["used_fallback"] is True
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
                "depends_on": [],
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

    assert plan["planner_metadata"]["used_fallback"] is True
    assert plan["planner_metadata"]["fallback_reason"]["stage"] == "unsafe_plan"
    assert trace["unsafe_plan"] is True
    assert trace["salvage_used"] is True
    assert plan["sub_questions"] == [
        {
            "id": "sq1",
            "question": "Investigate checkpoint replay safety after interruption",
            "reason": "Preserve the bounded safe slice from the unsafe planner output.",
        },
        {
            "id": "sq2",
            "question": "Compare restart trade-offs after interruption",
            "reason": "Preserve the bounded safe slice from the unsafe planner output.",
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
    assert first.focused_snapshot["source_ids"] == ["R1"]
    assert first.focused_snapshot["confirmed_claims"] == ["Resume continues from the last checkpoint."]


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

    section_titles = [section["title"] for section in result["report"]["sections"]]
    report_claims = [
        claim["text"]
        for section in result["report"]["sections"]
        for claim in section["claims"]
    ]
    citation_claims = [
        claim["text"]
        for section in result["citations"]["sections"]
        for claim in section["claims"]
    ]

    assert "Remaining Gaps" not in section_titles
    assert "## Remaining Gaps" not in result["final_report"]
    assert report_claims
    assert report_claims == citation_claims
    assert len(report_claims) == len(set(report_claims))


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

    assert result["report"]["coverage"]["unanswered_sections"] == ["Restart Trade-offs"]
    assert result["report"]["coverage"]["uncovered_sub_questions"] == ["Explain restart trade-offs"]
    assert result["report"]["status"] == "degraded"
    assert "coverage_incomplete" in result["report"]["runtime"]["warnings"]


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

    coverage = result["report"]["coverage"]

    assert coverage["planned_section_ids"] == ["resume-semantics", "restart-tradeoffs"]
    assert coverage["answered_section_ids"] == ["resume-semantics"]
    assert coverage["unanswered_sections"] == ["Restart Trade-offs"]
    assert coverage["planned_sub_question_ids"] == ["sq1", "sq2"]
    assert coverage["covered_sub_question_ids"] == ["sq1"]
    assert coverage["uncovered_sub_questions"] == ["Explain restart trade-offs"]


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

    coverage = result["report"]["coverage"]

    assert coverage["sub_questions"] == [
        {
            "sub_question_id": "sq1",
            "question": "Explain checkpoint resume semantics",
            "covered": True,
            "section_ids": ["resume-semantics"],
            "claim_ids": ["resume-semantics-claim-1"],
        },
        {
            "sub_question_id": "sq2",
            "question": "Explain restart trade-offs",
            "covered": False,
            "section_ids": [],
            "claim_ids": [],
        },
    ]


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

    unit_result = result["report"]["unit_results"]["unit-search-1"]

    assert unit_result["source_ids"] == []
    assert "stackoverflow.com" not in unit_result["detail"].lower()
    assert "repost.aws" not in unit_result["detail"].lower()
    assert "domain_constraints_applied" in result["report"]["runtime"]["warnings"]


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

    assert "Resume-processing continues from the last checkpoint" in result["final_report"]
    assert "Flink restart restores a job graph from a savepoint" not in result["final_report"]
    assert "jobruns-flink-restart.html" not in result["final_report"]


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
    assert "docs.datadoghq.com" in source_lines[-1]


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
async def test_reused_job_payload_tolerates_invalid_plan_json(tmp_path):
    runtime = build_runtime(tmp_path)
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

    response = await runtime.start(query="Reuse invalid plan", force_new=False, schedule=False)

    assert response["reused"] is True
    assert response["job_id"] == job.job_id
    assert response["plan"] is None


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
