import json
from pathlib import Path
import re

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deep_research"
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


def load_eval_case(name: str) -> dict:
    fixture_path = FIXTURE_DIR / name
    return json.loads(fixture_path.read_text())


def evaluate_case_metric(case: dict, metric: str) -> dict:
    if metric == "citation_faithfulness":
        return evaluate_citation_faithfulness(case)
    if metric == "coverage_completeness":
        return evaluate_coverage_completeness(case)
    if metric == "resume_continue_semantics":
        return evaluate_resume_continue_semantics(case)
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

    assert result["verdict"] == golden["verdict"]
    assert set(golden.get("reason_tags", [])).issubset(result["reason_tags"])


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_5_half.json",
        "eval_round8.json",
        "eval_probe_final.json",
    ],
)
def test_coverage_completeness_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["coverage_completeness"]

    result = evaluate_case_metric(case, "coverage_completeness")

    assert result["verdict"] == golden["verdict"]
    assert set(golden.get("reason_tags", [])).issubset(result["reason_tags"])


@pytest.mark.parametrize(
    "fixture_name",
    [
        "eval_probe_5_real.json",
        "eval_round9_live.json",
    ],
)
def test_resume_continue_semantics_probe_goldens(fixture_name):
    case = load_eval_case(fixture_name)
    golden = case["golden"]["resume_continue_semantics"]

    result = evaluate_case_metric(case, "resume_continue_semantics")

    assert result["verdict"] == golden["verdict"]
    assert set(golden.get("reason_tags", [])).issubset(result["reason_tags"])
