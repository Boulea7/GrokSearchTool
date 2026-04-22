import json
from pathlib import Path
import re

import pytest
from deep_research_test_helpers import load_deep_research_fixture


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_research"
PROBE_PUBLIC_SURFACE_FIXTURES = (
    "eval_probe_round24_worker_restart_public_surface.json",
    "eval_probe_round26_aws_dms_public_surface.json",
    "eval_probe_round30_aws_dms_official_doc.json",
    "eval_probe_round30_lifecycle_public_surface.json",
    "eval_probe_round33_aws_dms_official_doc.json",
    "eval_probe_round33_lifecycle_public_surface.json",
    "eval_probe_round34_aws_dms_official_doc.json",
    "eval_probe_round34_lifecycle_public_surface.json",
    "eval_probe_round35_aws_dms_official_doc.json",
    "eval_probe_round35_lifecycle_public_surface.json",
    "eval_probe_round36_aws_dms_official_doc.json",
    "eval_probe_round36_lifecycle_public_surface.json",
    "eval_probe_round37_aws_dms_official_doc.json",
    "eval_probe_round37_lifecycle_public_surface.json",
    "eval_probe_round38_aws_dms_partial_failure.json",
)
PROBE_PUBLIC_SURFACE_METRICS = (
    "release_gate_consistency",
    "resolved_batch_parity",
)
RECENT_PROBE_EVAL_PARITY_FIXTURES = (
    ("eval_probe_round33_aws_dms_official_doc.json", "probe_round33_aws_dms_official_doc.json"),
    ("eval_probe_round33_lifecycle_public_surface.json", "probe_round33_lifecycle_public_surface.json"),
    ("eval_probe_round34_aws_dms_official_doc.json", "probe_round34_aws_dms_official_doc.json"),
    ("eval_probe_round34_lifecycle_public_surface.json", "probe_round34_lifecycle_public_surface.json"),
    ("eval_probe_round35_aws_dms_official_doc.json", "probe_round35_aws_dms_official_doc.json"),
    ("eval_probe_round35_lifecycle_public_surface.json", "probe_round35_lifecycle_public_surface.json"),
    ("eval_probe_round36_aws_dms_official_doc.json", "probe_round36_aws_dms_official_doc.json"),
    ("eval_probe_round36_lifecycle_public_surface.json", "probe_round36_lifecycle_public_surface.json"),
    ("eval_probe_round37_aws_dms_official_doc.json", "probe_round37_aws_dms_official_doc.json"),
    ("eval_probe_round37_lifecycle_public_surface.json", "probe_round37_lifecycle_public_surface.json"),
    ("eval_probe_round38_aws_dms_partial_failure.json", "probe_round38_aws_dms_partial_failure.json"),
)
UNGROUNDED_ANALOGY_MARKERS = ("real-world analogy", "think of ")
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "from",
    "into",
    "last",
    "only",
    "primarily",
    "related",
    "restart",
    "semantics",
    "task",
    "tasks",
    "that",
    "the",
    "their",
    "through",
    "with",
}
NOISE_MARKERS = (
    "for more information",
    "topics can help you to resolve common issues",
    "troubleshooting issues",
    "support engineer",
    "real-world analogy",
)
TROUBLESHOOTING_MARKERS = ("troubleshooting", "support")


def _is_low_signal_title(title: str) -> bool:
    normalized = " ".join(str(title or "").split()).lower()
    if not normalized:
        return True
    if re.fullmatch(r"(?:section|chapter|step|part)?\s*\d+(?:\.\d+)*", normalized):
        return True
    if re.fullmatch(r"[ivxlcdm]+", normalized):
        return True
    if len(normalized) <= 2 and not re.search(r"[a-z]{2}", normalized):
        return True
    return False


def load_eval_case(name: str) -> dict:
    fixture_path = FIXTURE_DIR / name
    return json.loads(fixture_path.read_text())


def assert_metric_matches_golden(result: dict, golden: dict) -> None:
    assert result["verdict"] == golden["verdict"]
    assert result["reason_tags"] == sorted(golden.get("reason_tags", []))
    if "score" in golden:
        assert result["score"] == pytest.approx(golden["score"], abs=1e-3)


def evaluate_case_metric(case: dict, metric: str) -> dict:
    if metric == "citation_faithfulness":
        return evaluate_citation_faithfulness(case)
    if metric == "coverage_completeness":
        return evaluate_coverage_completeness(case)
    if metric == "resume_continue_semantics":
        return evaluate_resume_continue_semantics(case)
    if metric == "planner_boundary":
        return evaluate_planner_boundary(case)
    if metric == "ranking_noise_suppression":
        return evaluate_ranking_noise_suppression(case)
    if metric == "diagnostics_consistency":
        return evaluate_diagnostics_consistency(case)
    if metric == "provenance_bundle_consistency":
        return evaluate_provenance_bundle_consistency(case)
    if metric == "release_gate_consistency":
        return evaluate_release_gate_consistency(case)
    if metric == "resolved_batch_parity":
        return evaluate_resolved_batch_parity(case)
    if metric == "packet_to_prose_fidelity":
        return evaluate_packet_to_prose_fidelity(case)
    raise ValueError(f"Unsupported metric: {metric}")


def evaluate_citation_faithfulness(case: dict) -> dict:
    registry = _source_registry(case)
    claims = list(_iter_claims(case))
    reason_tags: list[str] = []
    score = 1.0

    if not claims:
        return {"metric": "citation_faithfulness", "verdict": "fail", "score": 0.0, "reason_tags": ["missing_claims"]}

    for claim in claims:
        citations = claim.get("citations") or []
        if not citations:
            reason_tags.append("missing_citation")
            score -= 0.6
            continue

        claim_text = claim.get("text", "")
        claim_keywords = _keywords(claim_text)
        cited_keywords = set()

        for citation_id in citations:
            source = registry.get(citation_id)
            if not source:
                reason_tags.append("missing_cited_source")
                score -= 0.6
                continue
            cited_keywords.update(_keywords(_source_text(source)))

        if any(marker in claim_text.lower() for marker in UNGROUNDED_ANALOGY_MARKERS):
            reason_tags.append("ungrounded_analogy")
            score -= 0.7

        if claim_keywords and cited_keywords and not (claim_keywords & cited_keywords):
            reason_tags.append("weak_source_alignment")
            score -= 0.4

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.5 and "ungrounded_analogy" not in reason_tags else "fail"
    return {
        "metric": "citation_faithfulness",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_coverage_completeness(case: dict) -> dict:
    coverage = (case.get("report") or {}).get("coverage") or case.get("coverage") or {}
    reason_tags: list[str] = []

    planned_sections = coverage.get("planned_section_ids") or []
    answered_sections = coverage.get("answered_section_ids") or []
    planned_sub_questions = coverage.get("planned_sub_question_ids") or []
    covered_sub_questions = coverage.get("covered_sub_question_ids") or []
    unanswered_sections = coverage.get("unanswered_sections") or []
    uncovered_sub_questions = coverage.get("uncovered_sub_questions") or []

    if unanswered_sections:
        reason_tags.append("unanswered_sections")
    if uncovered_sub_questions:
        reason_tags.append("uncovered_sub_questions")

    section_score = len(answered_sections) / len(planned_sections) if planned_sections else 1.0
    sub_question_score = len(covered_sub_questions) / len(planned_sub_questions) if planned_sub_questions else 1.0
    score = round((section_score + sub_question_score) / 2, 3)
    verdict = "pass" if score >= 0.95 and not reason_tags else "fail"

    return {
        "metric": "coverage_completeness",
        "verdict": verdict,
        "score": score,
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_resume_continue_semantics(case: dict) -> dict:
    continuation = case.get("continuation") or {}
    reason_tags: list[str] = []
    score = 1.0

    if continuation.get("mode") != "continue":
        reason_tags.append("not_continue_mode")
        score -= 1.0
    if not continuation.get("source_job_id"):
        reason_tags.append("missing_source_job_id")
        score -= 0.8
    if continuation.get("source_job_status") == "failed":
        reason_tags.append("failed_source_job")
        score -= 0.4
    if not continuation.get("checkpoint_key"):
        reason_tags.append("missing_checkpoint_key")
        score -= 0.3
    if continuation.get("source_count", 0) <= 0 or not continuation.get("carry_forward_sources"):
        reason_tags.append("missing_carry_forward_sources")
        score -= 0.5

    previous_summary = (continuation.get("previous_summary") or "").lower()
    if not previous_summary:
        reason_tags.append("missing_previous_summary")
        score -= 0.4
    elif "continuation from failed job" in previous_summary:
        reason_tags.append("generic_previous_summary")
        score -= 0.5

    if continuation.get("source_job_status") == "completed" and not continuation.get("continuation_identity"):
        reason_tags.append("missing_continuation_identity")
        score -= 0.2

    continuation_focus = (((case.get("plan") or {}).get("brief") or {}).get("continuation_focus") or [])
    if continuation.get("source_job_status") == "completed" and not continuation_focus:
        reason_tags.append("missing_continuation_focus")
        score -= 0.2

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.6 and "generic_previous_summary" not in reason_tags else "fail"
    return {
        "metric": "resume_continue_semantics",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_planner_boundary(case: dict) -> dict:
    planner = case.get("planner") or ((case.get("plan") or {}).get("planner_metadata")) or {}
    reason_tags: list[str] = []
    score = 1.0

    planner_used_fallback = bool(planner.get("used_fallback"))
    if planner_used_fallback:
        reason_tags.append("planner_fallback_used")
        score -= 0.2

    fallback_reason = planner.get("fallback_reason") or {}
    if isinstance(fallback_reason, dict) and fallback_reason.get("stage") == "unsafe_plan":
        reason_tags.append("unsafe_plan_fallback")
        score -= 0.2

    if _surface_contains_noise(case):
        reason_tags.append("contaminated_follow_up_surface")
        score -= 0.5

    constraints = _constraint_surface(case)
    if case.get("continuation") and not constraints.get("include_domains") and not constraints.get("allowed_sources"):
        reason_tags.append("missing_constraint_inheritance")
        score -= 0.4

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and "contaminated_follow_up_surface" not in reason_tags else "fail"
    return {
        "metric": "planner_boundary",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_ranking_noise_suppression(case: dict) -> dict:
    reason_tags: list[str] = []
    score = 1.0
    allowed_domains = set(_constraint_domains(case))
    query = str(case.get("query", "")).lower()
    report_status = str(((case.get("report") or {}).get("status") or "")).lower()
    domains = {str(source.get("domain") or "").lower() for source in case.get("sources") or [] if str(source.get("domain") or "").strip()}
    mixed_docs_and_external = any(domain.startswith("docs.") for domain in domains) and any(
        domain and not domain.startswith("docs.") for domain in domains
    )

    for source in case.get("sources") or []:
        domain = str(source.get("domain") or "").lower()
        title = str(source.get("title") or "").lower()
        url = str(source.get("url") or "").lower()
        if any(marker in title for marker in TROUBLESHOOTING_MARKERS):
            reason_tags.append("troubleshooting_shell_source")
            score -= 0.5
        if report_status == "completed" and _is_low_signal_title(title):
            reason_tags.append("low_signal_title")
            score -= 0.5
        if (
            report_status == "completed"
            and
            allowed_domains
            and domain in allowed_domains
            and domain.startswith("docs.")
            and ("/prescriptive-guidance/" in url or "/patterns/" in url)
        ):
            reason_tags.append("same_domain_prescriptive_guidance")
            score -= 0.6
        if _is_off_domain_for_official_docs_query(query, domain, allowed_domains) or (
            mixed_docs_and_external and domain and not domain.startswith("docs.")
        ):
            reason_tags.append("off_domain_source")
            score -= 0.6

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and not reason_tags else "fail"
    return {
        "metric": "ranking_noise_suppression",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_diagnostics_consistency(case: dict) -> dict:
    report = case.get("report") or {}
    planner = case.get("planner") or {}
    runtime = report.get("runtime") or {}
    warnings = runtime.get("warnings") or []
    reason_tags: list[str] = []
    score = 1.0
    allowed_domains = _constraint_domains(case)
    domains = {str(source.get("domain") or "").lower() for source in case.get("sources") or [] if str(source.get("domain") or "").strip()}
    mixed_docs_and_external = any(domain.startswith("docs.") for domain in domains) and any(
        domain and not domain.startswith("docs.") for domain in domains
    )

    if report.get("status") == "degraded" and not warnings:
        reason_tags.append("missing_degradation_warning")
        score -= 0.5
    if planner.get("used_fallback") and "planner_fallback_used" not in warnings:
        reason_tags.append("planner_fallback_not_exposed")
        score -= 0.4
    if any(
        _is_off_domain_for_official_docs_query(
            str(case.get("query", "")).lower(),
            str(source.get("domain") or "").lower(),
            allowed_domains,
        )
        or (mixed_docs_and_external and str(source.get("domain") or "").lower() and not str(source.get("domain") or "").lower().startswith("docs."))
        for source in case.get("sources") or []
    ):
        if "domain_constraints_applied" not in warnings:
            reason_tags.append("constraint_violation_not_exposed")
            score -= 0.4

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and not reason_tags else "fail"
    return {
        "metric": "diagnostics_consistency",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_provenance_bundle_consistency(case: dict) -> dict:
    registry = _source_registry(case)
    report = case.get("report") or {}
    evidence_items = case.get("evidence_items") or []
    evidence_by_id = {
        str(item.get("evidence_id", "")).strip(): item
        for item in evidence_items
        if str(item.get("evidence_id", "")).strip()
    }
    reason_tags: list[str] = []
    score = 1.0

    report_sections = report.get("sections") or []
    citation_sections = (case.get("citations") or {}).get("sections") or []
    if report_sections and citation_sections and report_sections != citation_sections:
        reason_tags.append("report_citation_section_mismatch")
        score -= 0.5

    for claim in _iter_claims(case):
        citations = [str(citation).strip() for citation in claim.get("citations") or [] if str(citation).strip()]
        evidence_ids = [str(evidence_id).strip() for evidence_id in claim.get("evidence_ids") or [] if str(evidence_id).strip()]
        bindings = [binding for binding in claim.get("evidence_bindings") or [] if isinstance(binding, dict)]

        if any(citation not in registry for citation in citations):
            reason_tags.append("missing_cited_source")
            score -= 0.4
        if any(evidence_id not in evidence_by_id for evidence_id in evidence_ids):
            reason_tags.append("missing_evidence_item")
            score -= 0.4

        for binding in bindings:
            binding_source_id = str(binding.get("source_id", "")).strip()
            binding_evidence_id = str(binding.get("evidence_id", "")).strip()
            if binding_source_id and binding_source_id not in citations:
                reason_tags.append("unbound_citation_source")
                score -= 0.3
            if binding_evidence_id and binding_evidence_id not in evidence_ids:
                reason_tags.append("unbound_evidence_id")
                score -= 0.3
            if bool(binding.get("source_backed")):
                line_start = binding.get("line_start")
                line_end = binding.get("line_end")
                if not isinstance(line_start, int) or not isinstance(line_end, int) or line_end < line_start:
                    reason_tags.append("invalid_source_backed_span")
                    score -= 0.3

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and not reason_tags else "fail"
    return {
        "metric": "provenance_bundle_consistency",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_release_gate_consistency(case: dict) -> dict:
    report = case.get("report") or {}
    runtime = report.get("runtime") or {}
    release_gate = runtime.get("release_gate") or {}
    verifier = runtime.get("verifier") or case.get("verifier") or {}
    artifact_errors = case.get("artifact_errors") or report.get("artifact_errors") or {}
    warnings = set(runtime.get("warnings") or [])
    reason_tags: list[str] = []
    score = 1.0

    release_reason_codes = [str(code) for code in release_gate.get("reason_codes") or [] if str(code).strip()]
    surfaced_release_reason_codes = sorted(
        {
            *release_reason_codes,
            *(str(code) for code in release_gate.get("all_reason_codes") or [] if str(code).strip()),
            *(str(code) for code in release_gate.get("soft_reason_codes") or [] if str(code).strip()),
        }
    )
    verifier_reason_codes = [str(code) for code in verifier.get("reason_codes") or [] if str(code).strip()]
    passed = release_gate.get("passed")
    status = str(report.get("status") or "")

    if passed is False and status != "failed":
        reason_tags.append("release_gate_status_mismatch")
        score -= 0.5
    if passed is True and status == "failed":
        reason_tags.append("failed_status_without_release_gate_failure")
        score -= 0.5
    if any(code not in warnings for code in surfaced_release_reason_codes):
        reason_tags.append("release_gate_warning_gap")
        score -= 0.3
    if verifier_reason_codes and not set(verifier_reason_codes).issubset(set(surfaced_release_reason_codes)):
        reason_tags.append("release_gate_missing_verifier_reason")
        score -= 0.4
    if artifact_errors and status == "completed":
        reason_tags.append("artifact_error_terminal_mismatch")
        score -= 0.4

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and not reason_tags else "fail"
    return {
        "metric": "release_gate_consistency",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_resolved_batch_parity(case: dict) -> dict:
    resolved_batch_id = str(case.get("resolved_artifact_batch_id") or "").strip()
    artifacts = case.get("artifacts") or []
    reason_tags: list[str] = []
    score = 1.0

    if resolved_batch_id:
        relevant = [
            artifact
            for artifact in artifacts
            if str(artifact.get("kind") or "") in {
                "sources.json",
                "citations.json",
                "report.json",
                "final_report.md",
                "evidence_items.json",
                "coverage.json",
                "grounding.json",
                "verifier.json",
                "selected_bank.json",
                "evidence_bank.json",
                "verification.json",
                "coverage_gaps.json",
            }
        ]
        mismatched = [
            str(artifact.get("kind") or "")
            for artifact in relevant
            if str((artifact.get("metadata") or {}).get("batch_id") or "").strip()
            and str((artifact.get("metadata") or {}).get("batch_id") or "").strip() != resolved_batch_id
        ]
        if mismatched:
            reason_tags.append("mixed_batch_artifacts")
            score -= 0.6

    score = max(score, 0.0)
    verdict = "pass" if score >= 0.7 and not reason_tags else "fail"
    return {
        "metric": "resolved_batch_parity",
        "verdict": verdict,
        "score": round(score, 3),
        "reason_tags": sorted(set(reason_tags)),
    }


def evaluate_packet_to_prose_fidelity(case: dict) -> dict:
    report = case.get("report") or {}
    sections = report.get("sections") or []
    section_by_id = {
        str(section.get("section_id", "")).strip(): dict(section)
        for section in sections
        if isinstance(section, dict) and str(section.get("section_id", "")).strip()
    }
    final_report_text = " ".join(str(report.get("final_report") or case.get("final_report") or "").split()).lower()
    selected_bank = case.get("selected_bank") or case.get("evidence_bank") or []
    reason_tags: list[str] = []
    checked_packets = 0
    for bank in selected_bank:
        if not isinstance(bank, dict):
            continue
        section_id = str(bank.get("section_id", "")).strip()
        section = section_by_id.get(section_id, {})
        section_text = " ".join(
            " ".join(
                [
                    str(section.get("summary", "") or ""),
                    str(section.get("prose", "") or ""),
                    *[
                        str(claim.get("text", "") or "")
                        for claim in section.get("claims", []) or []
                        if isinstance(claim, dict)
                    ],
                ]
            ).split()
        ).lower()
        for row in bank.get("selected_rows", []) or []:
            if not isinstance(row, dict):
                continue
            evidence_id = str(row.get("evidence_id", "")).strip()
            if not evidence_id:
                continue
            checked_packets += 1
            packet_reflected = bool([claim_id for claim_id in row.get("claim_ids", []) or [] if str(claim_id).strip()])
            if not packet_reflected:
                coverage_tags = [
                    " ".join(str(tag).split()).lower()
                    for tag in row.get("coverage_tags", []) or []
                    if " ".join(str(tag).split())
                ]
                packet_reflected = any(
                    tag and (tag in section_text or tag in final_report_text)
                    for tag in coverage_tags
                )
            if not packet_reflected:
                reason_tags.append("selected_packet_missing_from_prose")
    verdict = "pass" if not reason_tags else "fail"
    return {
        "metric": "packet_to_prose_fidelity",
        "verdict": verdict,
        "score": 1.0 if verdict == "pass" else 0.0,
        "reason_tags": sorted(set(reason_tags)),
        "checked_packets": checked_packets,
    }


def _iter_claims(case: dict):
    report = case.get("report") or {}
    citations = case.get("citations") or {}
    sections = report.get("sections") or citations.get("sections") or []
    for section in sections:
        for claim in section.get("claims") or []:
            yield claim


def _source_registry(case: dict) -> dict:
    citations = case.get("citations") or {}
    registry = citations.get("source_registry")
    if registry:
        return registry
    sources = case.get("sources") or []
    return {source["source_id"]: source for source in sources if source.get("source_id")}


def _source_text(source: dict) -> str:
    return " ".join(
        str(source.get(field, ""))
        for field in ("title", "description", "snippet", "url")
    )


def _keywords(text: str) -> set[str]:
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    normalized = re.sub(r"\[\[[^\]]+\]\]\([^)]+\)", " ", normalized)
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]+", normalized)
        if len(token) >= 4 and token.lower() not in STOPWORDS
    }
    return tokens


def _surface_contains_noise(case: dict) -> bool:
    texts: list[str] = []
    plan = case.get("plan") or {}
    brief = plan.get("brief") or case.get("brief") or {}
    for value in brief.get("must_cover") or []:
        texts.append(str(value))
    for value in brief.get("coverage_checklist") or []:
        texts.append(str(value))
    for value in brief.get("continuation_focus") or []:
        texts.append(str(value))
    strategy = plan.get("search_strategy") or {}
    for value in strategy.get("search_queries") or []:
        texts.append(str(value))
    for item in plan.get("sub_questions") or []:
        texts.append(str(item.get("question") or ""))
    continuation = case.get("continuation") or {}
    for value in continuation.get("continuation_focus") or []:
        texts.append(str(value))
    return any(marker in text.lower() for text in texts for marker in NOISE_MARKERS)


def _constraint_surface(case: dict) -> dict:
    plan = case.get("plan") or {}
    brief = plan.get("brief") or case.get("brief") or {}
    scope = brief.get("scope") or {}
    return {
        "include_domains": list(scope.get("include_domains") or plan.get("include_domains") or []),
        "exclude_domains": list(scope.get("exclude_domains") or plan.get("exclude_domains") or []),
        "allowed_sources": list(scope.get("allowed_sources") or []),
    }


def _constraint_domains(case: dict) -> list[str]:
    surface = _constraint_surface(case)
    domains = [str(value).lower() for value in surface["include_domains"] if str(value).strip()]
    if domains:
        return domains
    return [str(value).lower() for value in surface["allowed_sources"] if str(value).strip()]


def _is_off_domain_for_official_docs_query(query: str, domain: str, allowed_domains: list[str]) -> bool:
    if not domain:
        return False
    normalized_allowed = {value.lower() for value in allowed_domains if value}
    if normalized_allowed:
        return domain not in normalized_allowed
    if "official docs only" not in query:
        return False
    return not domain.startswith("docs.")


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_round6b.json",
        "eval_probe_final.json",
    ],
)
def test_citation_faithfulness_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["citation_faithfulness"]

    result = evaluate_case_metric(case, "citation_faithfulness")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_5_half.json",
        "eval_round8.json",
        "eval_probe_final.json",
        "eval_probe_round12_main_snapshot.json",
        "eval_probe_round13_main_snapshot.json",
        "eval_probe_round14_main_snapshot.json",
        "eval_probe_round15_main_snapshot.json",
        "eval_probe_round16_main_snapshot.json",
        "eval_probe_round18_aws_dms.json",
        "eval_probe_round19_aws_dms.json",
        "eval_probe_round20_aws_dms.json",
        "eval_probe_round22_coverage_ledger.json",
        "eval_probe_round24_aws_dms_fallback_coverage.json",
        "eval_probe_round30_aws_dms_official_doc.json",
    ],
)
def test_coverage_completeness_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["coverage_completeness"]

    result = evaluate_case_metric(case, "coverage_completeness")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_5_real.json",
        "eval_round9_live.json",
        "eval_probe_round11_interrupted_continue.json",
        "eval_probe_round12_continue_resume.json",
        "eval_probe_round13_continue_resume.json",
        "eval_probe_round14_lifecycle.json",
        "eval_probe_round16_lifecycle.json",
        "eval_probe_round18_lifecycle.json",
    ],
)
def test_resume_continue_semantics_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["resume_continue_semantics"]

    result = evaluate_case_metric(case, "resume_continue_semantics")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round10_planner.json",
        "eval_probe_round10_continuation.json",
        "eval_probe_round11_main_snapshot.json",
        "eval_probe_round11_interrupted_continue.json",
        "eval_probe_round12_main_snapshot.json",
        "eval_probe_round12_continue_resume.json",
        "eval_probe_round13_main_snapshot.json",
        "eval_probe_round13_continue_resume.json",
        "eval_probe_round14_main_snapshot.json",
        "eval_probe_round15_main_snapshot.json",
        "eval_probe_round16_main_snapshot.json",
        "eval_probe_round16_lifecycle.json",
        "eval_probe_round18_lifecycle.json",
        "eval_probe_round19_lifecycle_b.json",
        "eval_probe_round20_lifecycle.json",
        "eval_probe_round24_aws_dms_fallback_coverage.json",
        "eval_probe_round30_aws_dms_official_doc.json",
    ],
)
def test_planner_boundary_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["planner_boundary"]

    result = evaluate_case_metric(case, "planner_boundary")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round8_noise.json",
        "eval_probe_round10_continuation.json",
        "eval_probe_round11_main_snapshot.json",
        "eval_probe_round11_interrupted_continue.json",
        "eval_probe_round12_main_snapshot.json",
        "eval_probe_round13_main_snapshot.json",
        "eval_probe_round14_main_snapshot.json",
        "eval_probe_round15_main_snapshot.json",
        "eval_probe_round16_main_snapshot.json",
        "eval_probe_round18_aws_dms.json",
        "eval_probe_round22_noise_filters.json",
        "eval_probe_round23_aws_dms_ranking_noise.json",
        "eval_probe_round24_aws_dms_ranking_success.json",
        "eval_probe_round30_aws_dms_official_doc.json",
    ],
)
def test_ranking_noise_suppression_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["ranking_noise_suppression"]

    result = evaluate_case_metric(case, "ranking_noise_suppression")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round10_planner.json",
        "eval_probe_round10_continuation.json",
        "eval_probe_round11_main_snapshot.json",
        "eval_probe_round11_interrupted_continue.json",
        "eval_probe_round12_main_snapshot.json",
        "eval_probe_round12_continue_resume.json",
        "eval_probe_round13_main_snapshot.json",
        "eval_probe_round13_continue_resume.json",
        "eval_probe_round14_main_snapshot.json",
        "eval_probe_round15_main_snapshot.json",
        "eval_probe_round16_main_snapshot.json",
        "eval_probe_round18_aws_dms.json",
        "eval_probe_round18_lifecycle.json",
        "eval_probe_round19_aws_dms.json",
        "eval_probe_round19_lifecycle_b.json",
        "eval_probe_round20_aws_dms.json",
        "eval_probe_round20_lifecycle.json",
        "eval_probe_round22_noise_filters.json",
        "eval_probe_round24_aws_dms_fallback_coverage.json",
        "eval_probe_round24_aws_dms_ranking_success.json",
        "eval_probe_round30_aws_dms_official_doc.json",
    ],
)
def test_diagnostics_consistency_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["diagnostics_consistency"]

    result = evaluate_case_metric(case, "diagnostics_consistency")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round19_aws_dms.json",
        "eval_probe_round19_lifecycle_b.json",
        "eval_probe_round20_aws_dms.json",
    ],
)
def test_release_gate_consistency_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["release_gate_consistency"]

    result = evaluate_case_metric(case, "release_gate_consistency")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    ("fixture_name", "metric", "golden"),
    [
        (
            "probe_round11_main_snapshot.json",
            "planner_boundary",
            {
                "verdict": "fail",
                "score": 0.6,
                "reason_tags": ["planner_fallback_used", "unsafe_plan_fallback"],
            },
        ),
        (
            "probe_round11_main_snapshot.json",
            "diagnostics_consistency",
            {
                "verdict": "pass",
                "score": 1.0,
                "reason_tags": [],
            },
        ),
        (
            "probe_round11_interrupted_continue_snapshot.json",
            "planner_boundary",
            {
                "verdict": "fail",
                "score": 0.2,
                "reason_tags": [
                    "missing_constraint_inheritance",
                    "planner_fallback_used",
                    "unsafe_plan_fallback",
                ],
            },
        ),
        (
            "probe_round11_interrupted_continue_snapshot.json",
            "diagnostics_consistency",
            {
                "verdict": "fail",
                "score": 0.6,
                "reason_tags": ["planner_fallback_not_exposed"],
            },
        ),
        (
            "probe_round12_main_snapshot.json",
            "planner_boundary",
            {
                "verdict": "fail",
                "score": 0.6,
                "reason_tags": ["planner_fallback_used", "unsafe_plan_fallback"],
            },
        ),
        (
            "probe_round12_main_snapshot.json",
            "diagnostics_consistency",
            {
                "verdict": "pass",
                "score": 1.0,
                "reason_tags": [],
            },
        ),
    ],
)
def test_round11_round12_snapshot_smoke_metrics(fixture_name, metric, golden):
    case = load_eval_case(fixture_name)

    result = evaluate_case_metric(case, metric)

    assert_metric_matches_golden(result, golden)


def test_provenance_bundle_consistency_detects_unbound_binding_and_invalid_span():
    case = {
        "sources": [
            {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"},
        ],
        "citations": {
            "source_registry": {
                "R1": {"source_id": "R1", "url": "https://docs.example.com/runtime/checkpoints"},
            },
            "sections": [
                {
                    "section_id": "resume-semantics",
                    "title": "Resume Semantics",
                    "claims": [
                        {
                            "claim_id": "c1",
                            "text": "Resume continues from the last durable checkpoint.",
                            "citations": ["R1"],
                            "evidence_ids": ["e1"],
                            "evidence_bindings": [
                                {
                                    "source_id": "R9",
                                    "evidence_id": "e1",
                                    "source_backed": True,
                                    "line_start": None,
                                    "line_end": None,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "report": {
            "sections": [
                {
                    "section_id": "resume-semantics",
                    "title": "Resume Semantics",
                    "claims": [
                        {
                            "claim_id": "c1",
                            "text": "Resume continues from the last durable checkpoint.",
                            "citations": ["R1"],
                            "evidence_ids": ["e1"],
                            "evidence_bindings": [
                                {
                                    "source_id": "R9",
                                    "evidence_id": "e1",
                                    "source_backed": True,
                                    "line_start": None,
                                    "line_end": None,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "evidence_items": [
            {"evidence_id": "e1", "source_ids": ["R1"], "evidence_kind": "fetch"},
        ],
    }

    result = evaluate_case_metric(case, "provenance_bundle_consistency")

    assert result["verdict"] == "fail"
    assert set(result["reason_tags"]) >= {"unbound_citation_source", "invalid_source_backed_span"}


def test_release_gate_consistency_detects_warning_and_terminal_state_mismatch():
    case = {
        "report": {
            "status": "degraded",
            "runtime": {
                "warnings": ["coverage_incomplete"],
                "release_gate": {
                    "passed": False,
                    "reason_codes": ["coverage_incomplete", "invalid_source_backed_span"],
                },
                "verifier": {
                    "reason_codes": ["invalid_source_backed_span"],
                },
            },
        }
    }

    result = evaluate_case_metric(case, "release_gate_consistency")

    assert result["verdict"] == "fail"
    assert set(result["reason_tags"]) >= {"release_gate_status_mismatch", "release_gate_warning_gap"}


def test_resolved_batch_parity_detects_mixed_batch_artifacts():
    case = {
        "resolved_artifact_batch_id": "batch-good",
        "artifacts": [
            {"kind": "sources.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "citations.json", "metadata": {"batch_id": "batch-bad"}},
            {"kind": "report.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "final_report.md", "metadata": {"batch_id": "batch-good"}},
        ],
    }

    result = evaluate_case_metric(case, "resolved_batch_parity")

    assert result["verdict"] == "fail"
    assert result["reason_tags"] == ["mixed_batch_artifacts"]


def test_resolved_batch_parity_detects_mixed_batch_provenance_sidecars():
    case = {
        "resolved_artifact_batch_id": "batch-good",
        "artifacts": [
            {"kind": "sources.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "citations.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "report.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "final_report.md", "metadata": {"batch_id": "batch-good"}},
            {"kind": "evidence_items.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "coverage.json", "metadata": {"batch_id": "batch-bad"}},
            {"kind": "grounding.json", "metadata": {"batch_id": "batch-good"}},
            {"kind": "verifier.json", "metadata": {"batch_id": "batch-good"}},
        ],
    }

    result = evaluate_case_metric(case, "resolved_batch_parity")

    assert result["verdict"] == "fail"
    assert result["reason_tags"] == ["mixed_batch_artifacts"]


def test_packet_to_prose_fidelity_passes_when_selected_packets_are_reflected():
    case = {
        "report": {
            "sections": [
                {
                    "section_id": "resume-semantics",
                    "summary": "RecoveryTimeout and awsdms_txn_state govern AWS DMS recovery behavior.",
                    "prose": "RecoveryTimeout and awsdms_txn_state govern AWS DMS recovery behavior.",
                    "claims": [
                        {
                            "claim_id": "resume-semantics-claim-1",
                            "text": "RecoveryTimeout governs recovery wait behavior.",
                        }
                    ],
                }
            ],
            "final_report": "RecoveryTimeout and awsdms_txn_state govern AWS DMS recovery behavior.",
        },
        "selected_bank": [
            {
                "section_id": "resume-semantics",
                "selected_rows": [
                    {
                        "evidence_id": "e1",
                        "claim_ids": ["resume-semantics-claim-1"],
                        "coverage_tags": ["RecoveryTimeout", "awsdms_txn_state"],
                    }
                ],
            }
        ],
    }

    result = evaluate_case_metric(case, "packet_to_prose_fidelity")

    assert result["verdict"] == "pass"
    assert result["reason_tags"] == []


def test_packet_to_prose_fidelity_detects_selected_packet_missing_from_prose():
    case = {
        "report": {
            "sections": [
                {
                    "section_id": "resume-semantics",
                    "summary": "Resume-processing continues from the last checkpoint.",
                    "prose": "Resume-processing continues from the last checkpoint.",
                    "claims": [
                        {
                            "claim_id": "resume-semantics-claim-1",
                            "text": "Resume-processing continues from the last checkpoint.",
                        }
                    ],
                }
            ],
            "final_report": "Resume-processing continues from the last checkpoint.",
        },
        "selected_bank": [
            {
                "section_id": "resume-semantics",
                "selected_rows": [
                    {
                        "evidence_id": "e1",
                        "claim_ids": [],
                        "coverage_tags": ["RecoveryTimeout"],
                    }
                ],
            }
        ],
    }

    result = evaluate_case_metric(case, "packet_to_prose_fidelity")

    assert result["verdict"] == "fail"
    assert result["reason_tags"] == ["selected_packet_missing_from_prose"]


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round22_provenance_bundle.json",
    ],
)
def test_provenance_bundle_consistency_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["provenance_bundle_consistency"]

    result = evaluate_case_metric(case, "provenance_bundle_consistency")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_round22_provenance_bundle.json",
    ],
)
def test_resolved_batch_parity_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["resolved_batch_parity"]

    result = evaluate_case_metric(case, "resolved_batch_parity")

    assert_metric_matches_golden(result, golden)


@pytest.mark.parametrize("fixture_name", PROBE_PUBLIC_SURFACE_FIXTURES)
def test_probe_public_surface_goldens(fixture_name):
    case = load_eval_case(fixture_name)

    for metric in PROBE_PUBLIC_SURFACE_METRICS:
        golden = case["golden"][metric]
        result = evaluate_case_metric(case, metric)
        assert_metric_matches_golden(result, golden)


def test_resolved_batch_parity_flags_additive_final_sidecar_mismatch():
    result = evaluate_case_metric(
        {
            "resolved_artifact_batch_id": "batch-good",
            "artifacts": [
                {"kind": "selected_bank.json", "metadata": {"batch_id": "batch-good"}},
                {"kind": "evidence_bank.json", "metadata": {"batch_id": "batch-good"}},
                {"kind": "verification.json", "metadata": {"batch_id": "batch-good"}},
                {"kind": "coverage_gaps.json", "metadata": {"batch_id": "batch-bad"}},
            ],
        },
        "resolved_batch_parity",
    )

    assert result["verdict"] == "fail"
    assert result["reason_tags"] == ["mixed_batch_artifacts"]


@pytest.mark.parametrize(("eval_fixture_name", "live_fixture_name"), RECENT_PROBE_EVAL_PARITY_FIXTURES)
def test_recent_probe_eval_and_live_fixtures_stay_in_parity(eval_fixture_name, live_fixture_name):
    eval_case = load_eval_case(eval_fixture_name)
    live_case = load_deep_research_fixture(live_fixture_name)

    assert live_case["query"] == eval_case["query"]
    assert live_case["job"]["status"] == eval_case["sample"]["status"]
    assert live_case["job"]["phase"] == eval_case["sample"]["phase"]
    assert live_case["public_surface"]["status"]["runtime_warnings"] == eval_case["sample"]["runtime_warnings"]
    live_batch_id = live_case["public_surface"]["status"].get("resolved_artifact_batch_id", "")
    eval_batch_id = eval_case["resolved_artifact_batch_id"]
    if live_batch_id != "$seeded_batch_id":
        assert live_batch_id == eval_batch_id
    if live_case.get("artifacts"):
        assert [artifact["kind"] for artifact in live_case.get("artifacts", [])] == [
            artifact["kind"] for artifact in eval_case.get("artifacts", [])
        ]
    if "artifact_payload" in live_case:
        assert live_case["artifact_payload"]["report"]["status"] == eval_case["report"]["status"]
        assert live_case["artifact_payload"]["report"]["runtime"]["release_gate"] == eval_case["report"]["runtime"]["release_gate"]
        assert live_case["artifact_payload"]["report"]["runtime"]["verifier"] == eval_case["report"]["runtime"]["verifier"]
        if any(artifact.get("kind") == "selected_bank.json" for artifact in eval_case.get("artifacts", [])):
            assert bool(live_case["selected_bank"]) == any(
                artifact.get("kind") == "selected_bank.json" for artifact in eval_case.get("artifacts", [])
            )
        if any(artifact.get("kind") == "evidence_bank.json" for artifact in eval_case.get("artifacts", [])):
            assert bool(live_case["artifact_payload"]["evidence_bank"]) == any(
                artifact.get("kind") == "evidence_bank.json" for artifact in eval_case.get("artifacts", [])
            )
        if any(artifact.get("kind") == "verification.json" for artifact in eval_case.get("artifacts", [])):
            assert bool(live_case["artifact_payload"]["verification"]) == any(
                artifact.get("kind") == "verification.json" for artifact in eval_case.get("artifacts", [])
            )
        if any(artifact.get("kind") == "coverage_gaps.json" for artifact in eval_case.get("artifacts", [])):
            assert bool(live_case["artifact_payload"]["coverage_gaps"]) == any(
                artifact.get("kind") == "coverage_gaps.json" for artifact in eval_case.get("artifacts", [])
            )
