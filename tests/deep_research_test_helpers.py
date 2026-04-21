import json
import shlex
from pathlib import Path

from grok_search.deep_research_types import utc_now_iso


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_research"
SEEDED_BATCH_ID_PLACEHOLDER = "$seeded_batch_id"
RECENT_DEEP_RESEARCH_LIVE_PROBE_FIXTURES = (
    "probe_round33_aws_dms_official_doc.json",
    "probe_round33_lifecycle_public_surface.json",
    "probe_round34_aws_dms_official_doc.json",
    "probe_round34_lifecycle_public_surface.json",
    "probe_round35_aws_dms_official_doc.json",
    "probe_round35_lifecycle_public_surface.json",
)


def summary_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("summary:")]


def parse_summary_line(line: str) -> dict[str, str]:
    assert line.startswith("summary:")
    payload = line[len("summary:") :].strip()
    parsed: dict[str, str] = {}
    for token in shlex.split(payload):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parsed[key] = value
    return parsed


def assert_summary_fields(text: str, expected: dict[str, str], *, index: int = 0) -> None:
    lines = summary_lines(text)
    assert len(lines) > index, f"expected summary line at index {index}, got {lines!r}"
    parsed = parse_summary_line(lines[index])
    for key, expected_value in expected.items():
        assert parsed.get(key) == expected_value, (
            f"summary field mismatch for {key!r}: expected {expected_value!r}, got {parsed.get(key)!r}. "
            f"parsed={parsed!r}"
        )


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
    if value == SEEDED_BATCH_ID_PLACEHOLDER:
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


def _default_citations_payload(sources: list[dict], report: dict, evidence_items: list[dict]) -> dict:
    source_registry = {
        source["source_id"]: dict(source)
        for source in sources
    }
    section_claims = []
    for evidence in evidence_items:
        source_ids = list(evidence.get("source_ids") or [])
        claim_id = str(evidence.get("evidence_id") or f"claim-{len(section_claims) + 1}")
        section_claims.append(
            {
                "claim_id": claim_id,
                "text": str(evidence.get("summary") or evidence.get("detail") or ""),
                "citations": source_ids,
            }
        )
    section_title = "Findings"
    sections = report.get("sections") or []
    if sections:
        section_title = str(sections[0].get("title") or section_title)
    return {
        "source_registry": source_registry,
        "sections": [
            {
                "section_id": "findings",
                "title": section_title,
                "claims": section_claims,
            }
        ],
    }


def _minimal_provenance_payloads(
    *,
    query: str,
    report: dict,
    sources: list[dict],
    evidence_items: list[dict],
) -> tuple[dict, dict, dict]:
    section_ids = [section["section_id"] for section in report.get("sections") or [] if section.get("section_id")]
    sub_question_ids = [item["id"] for item in report.get("sub_questions") or [] if item.get("id")]
    total_claims = sum(len(section.get("claims") or []) for section in report.get("sections") or [])
    section_count = len(report.get("sections") or [])
    coverage_payload = {
        "query": query,
        "planned_section_ids": section_ids,
        "answered_section_ids": section_ids,
        "unanswered_sections": [],
        "planned_sub_question_ids": sub_question_ids,
        "covered_sub_question_ids": sub_question_ids,
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
        "total_evidence_bindings": len(evidence_items),
        "source_backed_binding_count": len(evidence_items),
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
            "source_backed_binding_count": len(evidence_items),
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
    return coverage_payload, grounding_payload, verifier_payload


def _artifact_batch_from_snapshot(snapshot: dict) -> list[dict]:
    artifact_payload = snapshot.get("artifact_payload") or {}
    query = snapshot["query"]
    sources = list(artifact_payload.get("sources") or [])
    report = json.loads(json.dumps(artifact_payload.get("report") or {}))
    if not snapshot.get("preserve_claim_sections", False):
        report["sections"] = []
    evidence_items = list(artifact_payload.get("evidence_items") or [])
    citations = artifact_payload.get("citations") or _default_citations_payload(sources, report, evidence_items)
    if isinstance(citations, dict):
        citations.setdefault("source_registry", {})
        citations["source_registry"] = {
            source["source_id"]: dict(source)
            for source in sources
        }
        citations["sections"] = json.loads(json.dumps(report.get("sections") or []))
    runtime_payload = report.setdefault("runtime", {})
    coverage = artifact_payload.get("coverage")
    if coverage is None:
        coverage = report.get("coverage")
    grounding = artifact_payload.get("grounding")
    if grounding is None:
        grounding = runtime_payload.get("grounding")
    verifier = artifact_payload.get("verifier")
    if verifier is None:
        verifier = runtime_payload.get("verifier")
    default_coverage, default_grounding, default_verifier = _minimal_provenance_payloads(
        query=query,
        report=report,
        sources=sources,
        evidence_items=evidence_items,
    )
    coverage = default_coverage if coverage is None else coverage
    grounding = default_grounding if grounding is None else grounding
    verifier = default_verifier if verifier is None else verifier
    if isinstance(grounding, dict):
        normalized_grounding = json.loads(json.dumps(default_grounding))
        normalized_grounding.update(grounding)
        grounding = normalized_grounding
    else:
        grounding = json.loads(json.dumps(default_grounding))
    if isinstance(verifier, dict):
        normalized_verifier = json.loads(json.dumps(default_verifier))
        normalized_verifier.update({key: value for key, value in verifier.items() if key != "summary"})
        summary_payload = normalized_verifier.setdefault("summary", {})
        incoming_summary = verifier.get("summary")
        if isinstance(summary_payload, dict) and isinstance(incoming_summary, dict):
            summary_payload.update(incoming_summary)
        verifier = normalized_verifier
    else:
        verifier = json.loads(json.dumps(default_verifier))
    report.setdefault("sections", [])
    report.setdefault("unit_results", {})
    report_coverage = report.setdefault("coverage", {})
    if isinstance(report_coverage, dict):
        for key, value in coverage.items():
            report_coverage.setdefault(key, value)
    else:
        report["coverage"] = json.loads(json.dumps(coverage))
    runtime_payload.setdefault("warnings", [])
    runtime_payload.setdefault("constraint_violations", [])
    report_grounding = runtime_payload.setdefault(
        "grounding",
        {
            key: grounding[key]
            for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims")
        },
    )
    if isinstance(report_grounding, dict):
        for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims"):
            report_grounding.setdefault(key, grounding[key])
    else:
        runtime_payload["grounding"] = {
            key: grounding[key]
            for key in ("total_claims", "ungrounded_claims", "single_source_claims", "missing_evidence_binding_claims")
        }
    report_verifier = runtime_payload.setdefault("verifier", {})
    if isinstance(report_verifier, dict):
        report_verifier.setdefault("passed", verifier["passed"])
        report_verifier.setdefault("reason_codes", list(verifier["reason_codes"]))
        report_verifier.setdefault("flagged_claim_ids", list(verifier["flagged_claim_ids"]))
        verifier_summary = report_verifier.setdefault("summary", {})
        if isinstance(verifier_summary, dict):
            for key, value in verifier["summary"].items():
                verifier_summary.setdefault(key, value)
        else:
            report_verifier["summary"] = json.loads(json.dumps(verifier["summary"]))
    else:
        runtime_payload["verifier"] = json.loads(json.dumps(verifier))
    return [
        {
            "kind": "sources.json",
            "content": json.dumps(sources),
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
            "content": str(artifact_payload.get("final_report") or "# Final Report\n"),
            "content_type": "text/markdown",
        },
        {
            "kind": "evidence_items.json",
            "content": json.dumps(evidence_items),
            "content_type": "application/json",
        },
        {
            "kind": "coverage.json",
            "content": json.dumps(coverage),
            "content_type": "application/json",
        },
        {
            "kind": "grounding.json",
            "content": json.dumps(grounding),
            "content_type": "application/json",
        },
        {
            "kind": "verifier.json",
            "content": json.dumps(verifier),
            "content_type": "application/json",
        },
    ]


def seed_live_probe_fixture_job(runtime, fixture_name: str, *, request_fingerprint: str) -> dict:
    snapshot = load_deep_research_fixture(fixture_name)
    job_payload = snapshot["job"]
    current_checkpoint = str(job_payload.get("current_checkpoint") or "")
    include_domains = list(snapshot.get("include_domains") or snapshot.get("plan", {}).get("include_domains") or [])
    exclude_domains = list(snapshot.get("exclude_domains") or snapshot.get("plan", {}).get("exclude_domains") or [])
    continued_from_job_id = str(job_payload.get("continued_from_job_id") or "")
    job = runtime.store.create_job(
        query=snapshot["query"],
        request_fingerprint=request_fingerprint,
        status=job_payload["status"],
        phase=job_payload["phase"],
        effort=str(job_payload.get("effort") or "deep"),
        context=str(snapshot.get("context") or ""),
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=int(job_payload.get("resolved_budget_seconds") or 180),
        continued_from_job_id=continued_from_job_id,
    )
    runtime.store.update_job(
        job.job_id,
        attempt_count=int(job_payload.get("attempt_count") or 0),
        current_checkpoint=current_checkpoint,
        progress_pct=float(job_payload.get("progress_pct") or 100.0),
        cancel_requested=bool(job_payload.get("cancel_requested") or False),
        finished_at=utc_now_iso() if job_payload["status"] in {"completed", "failed", "canceled", "interrupted"} else "",
        last_error=str(job_payload.get("last_error") or ""),
    )
    runtime.write_artifact(
        job.job_id,
        "plan.json",
        json.dumps(snapshot.get("plan") or {"query": snapshot["query"]}),
        "application/json",
    )
    planner_trace = (
        (snapshot.get("plan") or {}).get("planner_metadata", {}).get("trace")
        if isinstance((snapshot.get("plan") or {}).get("planner_metadata"), dict)
        else None
    )
    if planner_trace:
        runtime.write_artifact(
            job.job_id,
            "planner_trace.json",
            json.dumps(planner_trace),
            "application/json",
        )
    for kind, payload in (
        ("source_policy.json", snapshot.get("source_policy")),
        ("lineage.json", snapshot.get("lineage")),
        ("selected_bank.json", snapshot.get("selected_bank")),
    ):
        if payload is None:
            continue
        runtime.write_artifact(
            job.job_id,
            kind,
            json.dumps(payload),
            "application/json",
        )
    for artifact in snapshot.get("non_batch_artifacts") or []:
        content = artifact.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        runtime.write_artifact(
            job.job_id,
            artifact["kind"],
            content,
            artifact.get("content_type") or "application/json",
        )
    persisted = []
    if snapshot.get("artifact_payload") is not None:
        persisted = runtime.write_artifact_batch(job.job_id, _artifact_batch_from_snapshot(snapshot))
    batch_id = persisted[0]["metadata"]["batch_id"] if persisted else ""
    for event in list(snapshot.get("initial_events") or []) + list(snapshot.get("events_after_seq") or []):
        runtime.store.append_event(
            job.job_id,
            type=event["type"],
            phase=event["phase"],
            message=event.get("message") or event["type"],
            data=event.get("data") or {},
        )
    materialized_snapshot = _materialize_fixture_surface(snapshot, seeded_batch_id=batch_id)
    return {
        "job": runtime.store.get_job(job.job_id),
        "snapshot": materialized_snapshot,
        "batch_id": batch_id,
    }
