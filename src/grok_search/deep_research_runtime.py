import asyncio
import datetime as dt
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config
from .deep_research_store import DeepResearchStore
from .deep_research_types import (
    DeepResearchCheckpointState,
    DeepResearchClaim,
    DeepResearchContinuation,
    DeepResearchEvidenceItem,
    DeepResearchJob,
    DeepResearchPlan,
    DeepResearchReportSection,
    DeepResearchResearchUnit,
    DeepResearchSectionCitations,
    utc_now_iso,
)
from .providers.base import _filter_supported_search_kwargs
from .providers.grok import GrokSearchProvider
from .sources import merge_sources, split_answer_and_sources, standardize_sources
from .utils import extract_unique_urls


Runner = Callable[["DeepResearchRuntime", str], Awaitable[None]]
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_MAX_CLAIM_LENGTH = 220
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "later",
    "of",
    "on",
    "or",
    "the",
    "to",
    "up",
    "use",
    "with",
}
_NOISY_EVIDENCE_MARKERS = (
    "sitemap",
    "open in app",
    "dev community",
    "sign in",
    "sign up",
    "skip to content",
)
_PREFERRED_TECHNICAL_TERMS = (
    "checkpoint",
    "recovery",
    "runtime",
    "migration",
    "database",
    "sqlite",
    "postgresql",
    "resumability",
    "continuation",
    "state",
)


def _json_markdown_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return text.strip("-") or "item"


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _trim_text(value: str, *, limit: int = 400) -> str:
    text = _normalize_whitespace(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        normalized = _normalize_whitespace(item)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(normalized)
    return unique


def _tokenize_keywords(value: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*", (value or "").lower())
    return [token for token in tokens if len(token) > 2 and token not in _STOPWORDS and not token.isdigit()]


def _is_noisy_text(value: str) -> bool:
    text = (value or "").strip()
    lowered = text.lower()
    if not text:
        return True
    if "![" in text:
        return True
    if any(marker in lowered for marker in _NOISY_EVIDENCE_MARKERS):
        return True
    if lowered.startswith("[") and "](" in lowered:
        return True
    if len(re.findall(r"https?://", lowered)) >= 3 and len(text) < 500:
        return True
    return False


def _extract_meaningful_lines(value: str) -> list[str]:
    meaningful: list[str] = []
    for raw_line in (value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if _is_noisy_text(line):
            continue
        meaningful.append(line)
    return meaningful


def _summarize_evidence_text(value: str, *, limit: int = _MAX_CLAIM_LENGTH) -> str:
    lines = _extract_meaningful_lines(value)
    text = " ".join(lines[:4]) if lines else _normalize_whitespace(value)
    return _trim_text(text, limit=limit)


def _count_keyword_overlap(text: str, keywords: list[str]) -> int:
    lowered = (text or "").lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _continuation_anchor_terms(continuation: DeepResearchContinuation) -> list[str]:
    texts = [
        continuation.continuation_goal,
        continuation.prior_plan_summary,
        continuation.previous_summary,
        " ".join(section.get("title", "") for section in continuation.carry_forward_sections),
    ]
    keyword_counts: dict[str, int] = {}
    for text in texts:
        for token in _tokenize_keywords(text):
            keyword_counts[token] = keyword_counts.get(token, 0) + 1

    anchors: list[str] = []
    for preferred in _PREFERRED_TECHNICAL_TERMS:
        if keyword_counts.get(preferred, 0) > 0:
            anchors.append(preferred)
    if not anchors:
        anchors = sorted(keyword_counts, key=lambda token: (-keyword_counts[token], token))
    return anchors[:4]


def _rewrite_research_query(query: str, continuation: DeepResearchContinuation) -> str:
    text = _normalize_whitespace(query)
    if not text or continuation.mode != "continue":
        return text

    if re.search(r"\bresume\b", text, flags=re.IGNORECASE):
        text = re.sub(r"\bresume\b", "checkpoint resume", text, flags=re.IGNORECASE)
    if re.search(r"\bcontinue\b", text, flags=re.IGNORECASE):
        text = re.sub(r"\bcontinue\b", "continuation workflow", text, flags=re.IGNORECASE)

    anchors = _continuation_anchor_terms(continuation)
    if anchors:
        lowered = text.lower()
        missing = [anchor for anchor in anchors if anchor not in lowered]
        if missing:
            text = f"{text} {' '.join(missing[:2])}"
    return _normalize_whitespace(text)


def _best_text_claim(lines: list[str], fallback: str) -> str:
    for line in lines:
        if not _is_noisy_text(line):
            return line
    return _normalize_whitespace(fallback)


def _parse_utc_iso(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return None


def _parse_json_object(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if not text:
        raise ValueError("planner returned empty content")
    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    return json.loads(text)


def _normalize_depends_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _validate_research_units(units: list[DeepResearchResearchUnit]) -> None:
    known_ids = {unit.unit_id for unit in units}
    for unit in units:
        unknown = [dependency for dependency in unit.depends_on if dependency not in known_ids]
        if unknown:
            raise ValueError(f"unknown dependencies for {unit.unit_id}: {', '.join(unknown)}")

    visiting: set[str] = set()
    visited: set[str] = set()
    units_by_id = {unit.unit_id: unit for unit in units}

    def visit(unit_id: str) -> None:
        if unit_id in visited:
            return
        if unit_id in visiting:
            raise ValueError(f"cyclic dependency detected at {unit_id}")
        visiting.add(unit_id)
        for dependency in units_by_id[unit_id].depends_on:
            visit(dependency)
        visiting.remove(unit_id)
        visited.add(unit_id)

    for unit in units:
        visit(unit.unit_id)


def _sanitize_source_id_list(source_ids: list[str], source_registry: list[dict[str, Any]]) -> list[str]:
    valid_ids = {item["source_id"] for item in source_registry if item.get("source_id")}
    return [source_id for source_id in source_ids if source_id in valid_ids]


def _sanitize_unit_results(
    unit_results: dict[str, dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    sanitized: dict[str, dict[str, Any]] = {}
    for unit_id, result in unit_results.items():
        normalized = dict(result)
        normalized["source_ids"] = _sanitize_source_id_list(list(normalized.get("source_ids", [])), source_registry)
        normalized["citations"] = _sanitize_source_id_list(list(normalized.get("citations", [])), source_registry)
        sanitized[unit_id] = normalized
    return sanitized


def _sanitize_evidence_items(
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in evidence_items:
        normalized = dict(item)
        normalized["source_ids"] = _sanitize_source_id_list(list(normalized.get("source_ids", [])), source_registry)
        sanitized.append(normalized)
    return sanitized


def _sanitize_sections(
    sections: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for section in sections:
        normalized_section = dict(section)
        normalized_claims: list[dict[str, Any]] = []
        for claim in section.get("claims", []):
            normalized_claim = dict(claim)
            normalized_claim["citations"] = _sanitize_source_id_list(list(normalized_claim.get("citations", [])), source_registry)
            normalized_claims.append(normalized_claim)
        normalized_section["claims"] = normalized_claims
        normalized_section["citations"] = sorted(
            {citation for claim in normalized_claims for citation in claim.get("citations", [])}
        )
        sanitized.append(normalized_section)
    return sanitized


def _artifact_metadata(content: str, *, batch_id: str = "") -> dict[str, Any]:
    metadata = {"bytes": len(content.encode("utf-8"))}
    if batch_id:
        metadata["batch_id"] = batch_id
    return metadata


def _build_carry_forward_evidence(
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence_items: list[dict[str, Any]] = []
    for unit_id, result in unit_results.items():
        summary = _summarize_evidence_text(result.get("summary") or result.get("detail") or "")
        if not summary:
            continue
        evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"carry-forward-{unit_id}",
                unit_id=unit_id,
                source_ids=list(result.get("source_ids") or result.get("citations") or []),
                summary=summary,
                detail=result.get("detail", ""),
            ).model_dump()
        )
    if evidence_items:
        return evidence_items

    for section in sections:
        for index, claim in enumerate(section.get("claims", []), start=1):
            text = _summarize_evidence_text(claim.get("text", ""))
            if not text:
                continue
            evidence_items.append(
                DeepResearchEvidenceItem(
                    evidence_id=f"carry-forward-{section.get('section_id', 'section')}-{index}",
                    unit_id=section.get("section_id", "section"),
                    source_ids=list(claim.get("citations", [])),
                    summary=text,
                    detail=claim.get("text", ""),
                ).model_dump()
            )
    return evidence_items


class DeepResearchRuntime:
    def __init__(self, root_dir: Path, *, runner: Runner | None = None):
        self.store = DeepResearchStore(root_dir)
        self._runner = runner or _default_runner
        self._tasks: dict[str, asyncio.Task] = {}
        self._task_lock = asyncio.Lock()
        self._startup_reconciled = False

    async def start(
        self,
        *,
        query: str,
        context: str = "",
        effort: str = "standard",
        time_budget_seconds: int | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        continue_from_job_id: str = "",
        plan_only: bool = False,
        force_new: bool = False,
        schedule: bool = True,
    ) -> dict[str, Any]:
        await self._ensure_startup_reconciled()
        normalized_include_domains = list(include_domains or [])
        normalized_exclude_domains = list(exclude_domains or [])
        resolved_budget_seconds = self._resolve_budget_seconds(time_budget_seconds, effort)
        request_fingerprint = self._request_fingerprint(
            query=query,
            context=context,
            effort=effort,
            include_domains=normalized_include_domains,
            exclude_domains=normalized_exclude_domains,
            continue_from_job_id=continue_from_job_id,
        )

        reused_job = None
        if not force_new and not continue_from_job_id:
            reused_job = self.store.find_reusable_job(
                request_fingerprint,
                recent_reuse_seconds=config.deep_research_recent_reuse_seconds,
            )
        if reused_job is not None:
            return self._job_payload(reused_job, reused=True)

        initial_status = "draft" if plan_only else "queued"
        job = self.store.create_job(
            query=query,
            request_fingerprint=request_fingerprint,
            status=initial_status,
            phase="planning",
            effort=effort,
            context=context,
            include_domains=normalized_include_domains,
            exclude_domains=normalized_exclude_domains,
            plan_only=plan_only,
            force_new=force_new,
            resolved_budget_seconds=resolved_budget_seconds,
            continued_from_job_id=continue_from_job_id,
        )
        continuation = self._build_continuation_context(continue_from_job_id)
        plan = await self._build_plan(job, continuation)
        self.write_artifact(job.job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
        self.store.save_checkpoint(
            job.job_id,
            phase="planning",
            checkpoint_key="planning",
            state=DeepResearchCheckpointState(
                plan=plan,
                unit_results=dict(continuation.carry_forward_unit_results),
                sources=list(continuation.carry_forward_sources),
                evidence_items=list(continuation.carry_forward_evidence),
                sections=list(continuation.carry_forward_sections),
            ).model_dump(),
        )
        if continuation.mode == "continue":
            self.write_artifact(
                job.job_id,
                "continuation.json",
                _json_markdown_block(continuation.model_dump()),
                "application/json",
            )
        self.store.append_event(
            job.job_id,
            type="job_created",
            phase="planning",
            message="Deep research job created.",
            data={"plan_only": plan_only, "continuation_mode": continuation.mode},
        )
        job = self.store.get_job(job.job_id)

        if not plan_only and schedule:
            await self._schedule(job.job_id)

        return self._job_payload(job, reused=False)

    async def status(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        artifacts = [artifact.model_dump() for artifact in self.store.list_artifacts(job_id)]
        payload = self._serialize_job(job)
        payload["artifact_kinds"] = [artifact["kind"] for artifact in artifacts]
        payload["artifacts"] = artifacts
        return payload

    async def events(self, job_id: str, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        events = self.store.list_events(job_id, after_seq=after_seq, limit=limit)
        return {
            "job_id": job_id,
            "events": [event.model_dump() for event in events],
            "next_after_seq": events[-1].seq if events else after_seq,
        }

    async def result(self, job_id: str, *, include_partial: bool = True) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        plan_text = self.store.read_artifact_text(job_id, "plan.json")
        partial_text = self.store.read_artifact_text(job_id, "partial_report.md") if include_partial else None
        final_text = self.store.read_artifact_text(job_id, "final_report.md")
        citations_text = self.store.read_artifact_text(job_id, "citations.json")
        report_text = self.store.read_artifact_text(job_id, "report.json")
        sources_text = self.store.read_artifact_text(job_id, "sources.json")
        citations = json.loads(citations_text) if citations_text else None
        return {
            "job_id": job_id,
            "status": job.status,
            "phase": job.phase,
            "plan": json.loads(plan_text) if plan_text else None,
            "partial_report": partial_text,
            "final_report": final_text,
            "sources": json.loads(sources_text) if sources_text else None,
            "citations": citations,
            "report": json.loads(report_text) if report_text else None,
            "artifacts": [artifact.model_dump() for artifact in self.store.list_artifacts(job_id)],
        }

    async def resume(self, job_id: str, *, schedule: bool = True) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job.status not in {"draft", "failed", "interrupted"}:
            return self._job_payload(job, reused=False)
        job = self.store.update_job(
            job_id,
            status="queued",
            last_error="",
            cancel_requested=False,
            heartbeat_at=utc_now_iso(),
        )
        self.store.append_event(
            job_id,
            type="job_resumed",
            phase=job.phase,
            message="Deep research job resumed from checkpoint.",
            data={"checkpoint_key": job.current_checkpoint},
        )
        if schedule:
            await self._schedule(job_id)
        return self._job_payload(job, reused=False)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        job = self.store.update_job(job_id, cancel_requested=True)
        self.store.append_event(
            job_id,
            type="cancel_requested",
            phase=job.phase,
            message="Cancel requested.",
            data={},
        )
        if job.status in {"queued", "draft"}:
            job = self.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
            self.store.append_event(
                job_id,
                type="job_canceled",
                phase=job.phase,
                message="Deep research canceled before execution.",
                data={},
            )
        return {
            "job_id": job_id,
            "cancel_requested": True,
            "status": self.store.get_job(job_id).status,
        }

    async def list_jobs(self, *, status: str = "", limit: int = 50) -> dict[str, Any]:
        jobs = self.store.list_jobs(status=status, limit=limit)
        return {
            "jobs": [self._serialize_job(job) for job in jobs],
        }

    async def run_job(self, job_id: str) -> dict[str, Any]:
        await self._run(job_id)
        return await self.result(job_id)

    def write_artifact(self, job_id: str, kind: str, content: str, content_type: str) -> dict[str, Any]:
        path = self.store.artifact_abspath(job_id, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{kind}.{secrets.token_hex(4)}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
        relative_path = str(path.relative_to(self.store.root_dir))
        artifact = self.store.upsert_artifact(
            job_id,
            kind=kind,
            path=relative_path,
            content_type=content_type,
            metadata=_artifact_metadata(content),
        )
        return artifact.model_dump()

    def write_artifact_batch(self, job_id: str, artifacts: list[dict[str, str]]) -> list[dict[str, Any]]:
        batch_id = utc_now_iso().replace(":", "").replace("-", "").replace("Z", "") + "-" + secrets.token_hex(4)
        persisted: list[dict[str, Any]] = []
        payloads: list[dict[str, Any]] = []
        batch_dir = self.store.artifacts_dir / job_id / "batches" / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        for item in artifacts:
            kind = item["kind"]
            content = item["content"]
            content_type = item["content_type"]
            path = batch_dir / kind
            temp_path = path.with_name(f".{kind}.{secrets.token_hex(4)}.tmp")
            temp_path.write_text(content, encoding="utf-8")
            temp_path.replace(path)
            payloads.append(
                {
                    "kind": kind,
                    "path": str(path.relative_to(self.store.root_dir)),
                    "content_type": content_type,
                    "metadata": _artifact_metadata(content, batch_id=batch_id),
                }
            )
        persisted_artifacts = self.store.upsert_artifact_batch(job_id, artifacts=payloads)
        for artifact in persisted_artifacts:
            persisted.append(artifact.model_dump())
        return persisted

    async def _ensure_startup_reconciled(self) -> None:
        if self._startup_reconciled:
            return
        self.store.reconcile_incomplete_jobs()
        self._startup_reconciled = True

    async def _schedule(self, job_id: str) -> None:
        async with self._task_lock:
            existing = self._tasks.get(job_id)
            if existing and not existing.done():
                return
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))

    async def _run(self, job_id: str) -> None:
        try:
            job = self.store.get_job(job_id)
            if job.cancel_requested:
                self.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
                self.store.append_event(
                    job_id,
                    type="job_canceled",
                    phase=job.phase,
                    message="Deep research canceled before start.",
                    data={},
                )
                return
            await self._runner(self, job_id)
        except Exception as exc:
            self.store.update_job(
                job_id,
                status="failed",
                last_error=str(exc),
                finished_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
            )
            self.store.append_event(
                job_id,
                type="job_failed",
                phase=self.store.get_job(job_id).phase,
                message="Deep research failed.",
                data={"error": str(exc)},
            )
        finally:
            async with self._task_lock:
                self._tasks.pop(job_id, None)

    async def _build_plan(self, job: DeepResearchJob, continuation: DeepResearchContinuation) -> DeepResearchPlan:
        try:
            raw_plan = await self._generate_plan_with_model(job, continuation)
            return self._normalize_plan_payload(job, raw_plan, continuation)
        except Exception:
            return self._build_fallback_plan(job, continuation)

    async def _generate_plan_with_model(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuation,
    ) -> dict[str, Any]:
        api_url = config.grok_api_url
        api_key = config.grok_api_key
        provider = GrokSearchProvider(api_url, api_key, config.grok_model)
        planner_prompt = (
            "You are planning a deep research job.\n"
            "Return valid JSON only with keys: brief, sub_questions, search_strategy, report_outline, research_units, planner_metadata.\n"
            "Keep the plan lightweight and execution-ready.\n"
            "research_units must be an array of objects with unit_id, unit_type, title, goal, query/url/instructions, depends_on, status, notes.\n"
            "Prefer search units, and only include fetch/map units when clearly justified.\n"
            "Keep report_outline concise and aligned with the research goal.\n"
            "When continuation.mode=continue, treat ambiguous terms like resume/continue as technical workflow concepts unless the prior job is explicitly career-related.\n"
            "Reuse the previous job's evidence and unfinished angles when refining search queries and outline.\n"
        )
        user_prompt = _json_markdown_block(
            {
                "query": job.query,
                "context": job.context,
                "effort": job.effort,
                "time_budget_seconds": job.resolved_budget_seconds,
                "include_domains": job.include_domains,
                "exclude_domains": job.exclude_domains,
                "continuation": continuation.model_dump(),
            }
        )
        headers = provider._build_api_headers()
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": planner_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        content, _ = await provider._execute_completion_with_retry_result(headers, payload, render_sources=False)
        try:
            return _parse_json_object(content)
        except Exception:
            repair_payload = {
                "model": provider.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Repair the following into valid JSON matching the requested schema. Return JSON only.",
                    },
                    {"role": "user", "content": content},
                ],
                "stream": False,
            }
            repaired, _ = await provider._execute_completion_with_retry_result(
                headers,
                repair_payload,
                render_sources=False,
            )
            return _parse_json_object(repaired)

    def _build_fallback_plan(
        self,
        job: DeepResearchJob,
        continuation: DeepResearchContinuation,
    ) -> DeepResearchPlan:
        query = _rewrite_research_query(job.query.strip(), continuation)
        context = _rewrite_research_query(job.context.strip(), continuation)
        search_queries = [query]
        if context:
            search_queries.append(f"{query} {context}".strip())
        if continuation.previous_summary:
            search_queries.append(f"{query} follow up findings checkpoint recovery".strip())
        if job.effort == "deep":
            search_queries.append(f"{query} tradeoffs")
        unique_queries: list[str] = []
        seen: set[str] = set()
        for item in search_queries:
            normalized = _normalize_whitespace(item)
            if normalized and normalized not in seen:
                unique_queries.append(normalized)
                seen.add(normalized)

        raw_plan = {
            "brief": {
                "objective": query,
                "deliverable": "A structured deep research report with citations.",
                "success_criteria": [
                    "Answer the main research question.",
                    "Ground each section in verifiable sources.",
                ],
            },
            "sub_questions": [
                {"id": f"sq{index}", "question": item, "reason": "Cover the core research surface."}
                for index, item in enumerate(unique_queries[:3], start=1)
            ],
            "search_strategy": {
                "approach": "targeted",
                "search_queries": unique_queries[: max(1, config.deep_research_max_concurrency)],
                "selective_fetch": {
                    "max_urls_per_search": 1 if job.effort != "deep" else 2,
                    "prefer_titles_matching_outline": True,
                },
            },
            "report_outline": [
                {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
                {"section_id": "key-findings", "title": "Key Findings", "goal": "Cover the strongest findings."},
                {"section_id": "open-questions", "title": "Open Questions", "goal": "Call out remaining gaps."},
            ],
            "research_units": [
                {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": f"Research query {index}",
                    "goal": f"Investigate: {item}",
                    "query": item,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
                for index, item in enumerate(unique_queries[: max(1, config.deep_research_max_concurrency)], start=1)
            ],
            "planner_metadata": {"planner": "fallback", "used_fallback": True},
        }
        return self._normalize_plan_payload(job, raw_plan, continuation)

    def _normalize_plan_payload(
        self,
        job: DeepResearchJob,
        raw_plan: dict[str, Any],
        continuation: DeepResearchContinuation,
    ) -> DeepResearchPlan:
        if raw_plan.get("search_queries") and not raw_plan.get("search_strategy"):
            raw_plan = {
                **raw_plan,
                "search_strategy": {
                    "approach": "targeted",
                    "search_queries": raw_plan.get("search_queries") or [],
                    "selective_fetch": {"max_urls_per_search": 1, "prefer_titles_matching_outline": True},
                },
            }
        if raw_plan.get("report_sections") and not raw_plan.get("report_outline"):
            raw_plan = {
                **raw_plan,
                "report_outline": [
                    {"section_id": _slugify(title), "title": title, "goal": title}
                    for title in raw_plan.get("report_sections") or []
                ],
            }
        sub_questions = raw_plan.get("sub_questions") or []
        if not sub_questions:
            sub_questions = [{"id": "sq1", "question": _rewrite_research_query(job.query, continuation), "reason": "Cover the primary question."}]
        else:
            normalized_sub_questions: list[dict[str, Any]] = []
            for item in sub_questions:
                normalized_sub_questions.append(
                    {
                        **item,
                        "question": _rewrite_research_query(str(item.get("question", "")), continuation),
                    }
                )
            sub_questions = normalized_sub_questions

        report_outline = raw_plan.get("report_outline") or [
            {"section_id": "executive-summary", "title": "Executive Summary", "goal": "Summarize the answer."},
            {"section_id": "key-findings", "title": "Key Findings", "goal": "Present the main evidence."},
        ]
        research_units = raw_plan.get("research_units") or []
        if not research_units:
            search_queries = (raw_plan.get("search_strategy") or {}).get("search_queries") or [job.query]
            research_units = [
                {
                    "unit_id": f"unit-search-{index}",
                    "unit_type": "search",
                    "title": f"Search {index}",
                    "goal": f"Investigate {query}",
                    "query": query,
                    "depends_on": [],
                    "status": "pending",
                    "notes": "",
                }
                for index, query in enumerate(search_queries[: max(1, config.deep_research_max_concurrency)], start=1)
            ]

        normalized_units: list[dict[str, Any]] = []
        for index, item in enumerate(research_units, start=1):
            unit_id = item.get("unit_id") or f"unit-{item.get('unit_type', 'search')}-{index}"
            unit_query = item.get("query", "")
            if item.get("unit_type", "search") == "search":
                unit_query = _rewrite_research_query(str(unit_query), continuation)
            normalized_units.append(
                {
                    "unit_id": unit_id,
                    "unit_type": item.get("unit_type", "search"),
                    "title": item.get("title") or f"Unit {index}",
                    "goal": item.get("goal") or unit_query or item.get("url") or f"Unit {index}",
                    "query": unit_query,
                    "url": item.get("url", ""),
                    "instructions": item.get("instructions", ""),
                    "depends_on": _normalize_depends_on(item.get("depends_on")),
                    "status": item.get("status", "pending"),
                    "notes": item.get("notes", ""),
                }
            )

        normalized_outline: list[dict[str, Any]] = []
        for index, item in enumerate(report_outline, start=1):
            title = item.get("title") or f"Section {index}"
            normalized_outline.append(
                {
                    "section_id": item.get("section_id") or _slugify(title),
                    "title": title,
                    "goal": item.get("goal") or title,
                }
            )

        strategy = raw_plan.get("search_strategy") or {}
        if not strategy.get("search_queries"):
            strategy["search_queries"] = [
                unit["query"] for unit in normalized_units if unit["unit_type"] == "search" and unit["query"]
            ] or [job.query]
        strategy["search_queries"] = _dedupe_preserve_order(
            [_rewrite_research_query(str(query), continuation) for query in strategy.get("search_queries") or [job.query]]
        )
        strategy.setdefault("approach", "targeted")
        strategy.setdefault("selective_fetch", {"max_urls_per_search": 1, "prefer_titles_matching_outline": True})

        normalized = {
            "query": job.query,
            "context": job.context,
            "effort": job.effort,
            "time_budget_seconds": job.resolved_budget_seconds,
            "include_domains": job.include_domains,
            "exclude_domains": job.exclude_domains,
            "brief": raw_plan.get("brief")
            or {
                "objective": job.query,
                "deliverable": "A structured deep research report with citations.",
                "success_criteria": ["Answer the query with source-backed sections."],
            },
            "sub_questions": sub_questions,
            "search_strategy": strategy,
            "report_outline": normalized_outline,
            "research_units": normalized_units,
            "continuation": continuation.model_dump(),
            "planner_metadata": raw_plan.get("planner_metadata") or {"planner": "fallback", "used_fallback": True},
        }
        plan = DeepResearchPlan.model_validate(normalized)
        _validate_research_units(plan.research_units)
        return plan

    def _build_continuation_context(self, continue_from_job_id: str) -> DeepResearchContinuation:
        if not continue_from_job_id:
            return DeepResearchContinuation(mode="fresh")
        job = self.store.get_job(continue_from_job_id)

        report_text = self.store.read_artifact_text(continue_from_job_id, "report.json")
        final_report = self.store.read_artifact_text(continue_from_job_id, "final_report.md") or ""
        partial_report = self.store.read_artifact_text(continue_from_job_id, "partial_report.md") or ""
        plan_text = self.store.read_artifact_text(continue_from_job_id, "plan.json") or ""
        sources_text = self.store.read_artifact_text(continue_from_job_id, "sources.json") or "[]"
        checkpoints = self.store.list_checkpoints(continue_from_job_id)
        report: dict[str, Any] = {}
        if report_text:
            try:
                report = json.loads(report_text)
            except Exception:
                report = {}

        previous_summary = ""
        if report:
            previous_summary = report.get("summary") or ""
        if not previous_summary:
            lines = [line.strip() for line in final_report.splitlines() if line.strip() and not line.startswith("#")]
            previous_summary = lines[0] if lines else ""
        if not previous_summary:
            lines = [line.strip() for line in partial_report.splitlines() if line.strip() and not line.startswith("#")]
            previous_summary = lines[0] if lines else ""

        prior_plan_summary = ""
        plan_payload: dict[str, Any] = {}
        if plan_text:
            try:
                plan_payload = json.loads(plan_text)
                sub_questions = plan_payload.get("sub_questions") or []
                prior_plan_summary = "; ".join(
                    item.get("question", "").strip() for item in sub_questions if item.get("question", "").strip()
                )
            except Exception:
                prior_plan_summary = ""
        if not prior_plan_summary and checkpoints:
            latest_state = checkpoints[-1].state or {}
            plan_state = latest_state.get("plan") if isinstance(latest_state, dict) else None
            if isinstance(plan_state, dict):
                prior_plan_summary = "; ".join(
                    item.get("question", "").strip()
                    for item in plan_state.get("sub_questions", []) or []
                    if item.get("question", "").strip()
                )

        try:
            carry_forward_sources = list(json.loads(sources_text))
        except Exception:
            carry_forward_sources = []

        latest_state = checkpoints[-1].state or {} if checkpoints else {}
        if not carry_forward_sources and isinstance(latest_state, dict):
            carry_forward_sources = list(latest_state.get("sources") or [])
        source_count = len(carry_forward_sources)

        carry_forward_sections = list(report.get("sections") or [])
        if not carry_forward_sections and isinstance(latest_state, dict):
            carry_forward_sections = list(latest_state.get("sections") or [])

        carry_forward_unit_results = dict(report.get("unit_results") or {})
        if not carry_forward_unit_results and isinstance(latest_state, dict):
            carry_forward_unit_results = dict(latest_state.get("unit_results") or {})

        carry_forward_evidence = []
        if isinstance(latest_state, dict):
            carry_forward_evidence = list(latest_state.get("evidence_items") or [])
        if not carry_forward_evidence:
            carry_forward_evidence = _build_carry_forward_evidence(carry_forward_unit_results, carry_forward_sections)

        checkpoint_key = job.current_checkpoint or (checkpoints[-1].checkpoint_key if checkpoints else "")
        continuation_goal = plan_payload.get("query") or job.query

        return DeepResearchContinuation(
            mode="continue",
            source_job_id=continue_from_job_id,
            source_job_status=job.status,
            previous_summary=_trim_text(previous_summary or f"Continuation from {job.status} job {continue_from_job_id}.", limit=400),
            prior_plan_summary=_trim_text(prior_plan_summary, limit=400),
            continuation_goal=_trim_text(continuation_goal, limit=200),
            source_count=source_count,
            checkpoint_key=checkpoint_key,
            carry_forward_sources=carry_forward_sources,
            carry_forward_evidence=carry_forward_evidence,
            carry_forward_sections=carry_forward_sections,
            carry_forward_unit_results=carry_forward_unit_results,
        )

    def _request_fingerprint(
        self,
        *,
        query: str,
        context: str,
        effort: str,
        include_domains: list[str],
        exclude_domains: list[str],
        continue_from_job_id: str,
    ) -> str:
        payload = {
            "query": query.strip(),
            "context": context.strip(),
            "effort": effort.strip(),
            "include_domains": include_domains,
            "exclude_domains": exclude_domains,
            "continue_from_job_id": continue_from_job_id.strip(),
        }
        return hashlib.sha256(_json_markdown_block(payload).encode("utf-8")).hexdigest()

    def _resolve_budget_seconds(self, requested_budget_seconds: int | None, effort: str) -> int:
        if requested_budget_seconds and requested_budget_seconds > 0:
            return min(requested_budget_seconds, config.deep_research_hard_timeout_seconds)
        if effort == "deep":
            candidate = int(config.deep_research_default_budget_seconds * 1.5)
        else:
            candidate = config.deep_research_default_budget_seconds
        return min(candidate, config.deep_research_hard_timeout_seconds)

    def _job_payload(self, job: DeepResearchJob, *, reused: bool) -> dict[str, Any]:
        payload = self._serialize_job(job)
        payload["reused"] = reused
        plan_text = self.store.read_artifact_text(job.job_id, "plan.json")
        payload["plan"] = json.loads(plan_text) if plan_text else None
        return payload

    def _serialize_job(self, job: DeepResearchJob) -> dict[str, Any]:
        return job.model_dump()

    def _read_plan(self, job_id: str, job: DeepResearchJob | None = None) -> DeepResearchPlan:
        plan_text = self.store.read_artifact_text(job_id, "plan.json")
        if plan_text:
            raw_plan = json.loads(plan_text)
            if job is None:
                job = self.store.get_job(job_id)
            continuation = self._build_continuation_context(job.continued_from_job_id)
            plan = self._normalize_plan_payload(job, raw_plan, continuation)
            if raw_plan != plan.model_dump():
                self.write_artifact(job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
            return plan
        if job is None:
            job = self.store.get_job(job_id)
        continuation = self._build_continuation_context(job.continued_from_job_id)
        return self._build_fallback_plan(job, continuation)

    def _load_checkpoint_state(self, job: DeepResearchJob) -> tuple[DeepResearchCheckpointState | None, dict[str, Any] | None]:
        checkpoints = self.store.list_checkpoints(job.job_id)
        if not checkpoints:
            return None, None
        candidates: list[Any] = []
        invalid_checkpoint_keys: list[str] = []
        if job.current_checkpoint:
            candidates.extend(
                checkpoint
                for checkpoint in checkpoints
                if checkpoint.checkpoint_key == job.current_checkpoint
            )
        candidates.extend(
            checkpoint
            for checkpoint in reversed(checkpoints)
            if checkpoint.checkpoint_key != job.current_checkpoint
        )
        for checkpoint in candidates:
            raw_state = checkpoint.state or {}
            if "plan" not in raw_state and isinstance(raw_state, dict) and raw_state.get("query"):
                raw_state = {"plan": raw_state}
            if "plan" not in raw_state:
                invalid_checkpoint_keys.append(checkpoint.checkpoint_key)
                continue
            try:
                if checkpoint.phase != "planning" and not any(
                    key in raw_state for key in ("completed_unit_ids", "unit_results", "sources", "evidence_items", "sections")
                ):
                    raise ValueError("checkpoint missing runtime state")
                if isinstance(raw_state.get("plan"), dict):
                    raw_state = {
                        **raw_state,
                        "plan": self._normalize_plan_payload(
                            job,
                            raw_state["plan"],
                            self._build_continuation_context(job.continued_from_job_id),
                        ).model_dump(),
                    }
                state = DeepResearchCheckpointState.model_validate(raw_state)
                if job.current_checkpoint and checkpoint.checkpoint_key != job.current_checkpoint:
                    return (
                        state,
                        {
                            "fallback_from": job.current_checkpoint,
                            "fallback_to": checkpoint.checkpoint_key,
                            "invalid_checkpoint_keys": invalid_checkpoint_keys,
                        },
                    )
                return state, None
            except Exception:
                invalid_checkpoint_keys.append(checkpoint.checkpoint_key)
                continue
        return None, None


async def _default_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    job = runtime.store.get_job(job_id)
    now_iso = utc_now_iso()
    started_at = job.started_at or now_iso
    job = runtime.store.update_job(
        job_id,
        status="running",
        started_at=started_at,
        heartbeat_at=now_iso,
        attempt_count=job.attempt_count + 1,
        last_error="",
    )
    checkpoint_state, checkpoint_meta = runtime._load_checkpoint_state(job)
    plan = checkpoint_state.plan if checkpoint_state else runtime._read_plan(job_id, job)
    runtime.write_artifact(job_id, "plan.json", _json_markdown_block(plan.model_dump()), "application/json")
    if checkpoint_meta:
        runtime.store.update_job(job_id, current_checkpoint=checkpoint_meta["fallback_to"])
        runtime.store.append_event(
            job_id,
            type="checkpoint_fallback",
            phase=job.phase,
            message="Latest checkpoint was not readable; resumed from an earlier checkpoint.",
            data=checkpoint_meta,
        )

    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="planning",
        message="Planning started.",
        data={"unit_count": len(plan.research_units)},
    )
    runtime.store.update_job(job_id, phase="planning", progress_pct=10.0, heartbeat_at=utc_now_iso())

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, "planning")
        return

    continuation = plan.continuation
    completed_unit_ids = list(checkpoint_state.completed_unit_ids) if checkpoint_state else []
    unit_results = dict(checkpoint_state.unit_results) if checkpoint_state else dict(continuation.carry_forward_unit_results)
    source_registry = list(checkpoint_state.sources) if checkpoint_state else list(continuation.carry_forward_sources)
    evidence_items = list(checkpoint_state.evidence_items) if checkpoint_state else list(continuation.carry_forward_evidence)
    sections = list(checkpoint_state.sections) if checkpoint_state else list(continuation.carry_forward_sections)
    unit_results = _sanitize_unit_results(unit_results, source_registry)
    evidence_items = _sanitize_evidence_items(evidence_items, source_registry)
    sections = _sanitize_sections(sections, source_registry)
    started_at_dt = _parse_utc_iso(job.started_at) or dt.datetime.now(dt.UTC)

    runtime.store.update_job(job_id, phase="researching", progress_pct=20.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="researching",
        message="Researching source material.",
        data={"completed_units": len(completed_unit_ids)},
    )

    unit_total = max(len(plan.research_units), 1)
    while len(completed_unit_ids) < len(plan.research_units):
        ready_units = [
            unit
            for unit in plan.research_units
            if unit.unit_id not in completed_unit_ids and all(dep in completed_unit_ids for dep in unit.depends_on)
        ]
        if not ready_units:
            remaining_units = [unit.unit_id for unit in plan.research_units if unit.unit_id not in completed_unit_ids]
            raise ValueError(f"research_plan_blocked: unresolved dependencies for {', '.join(remaining_units)}")
        if runtime.store.get_job(job_id).cancel_requested:
            _mark_canceled(runtime, job_id, "researching")
            return
        elapsed_seconds = (dt.datetime.now(dt.UTC) - started_at_dt).total_seconds()
        if elapsed_seconds >= job.resolved_budget_seconds:
            _write_partial_outputs(runtime, job_id, plan, completed_unit_ids, unit_results, sections)
            runtime.store.update_job(
                job_id,
                status="interrupted",
                phase="researching",
                last_error="time_budget_exceeded",
                finished_at=utc_now_iso(),
                heartbeat_at=utc_now_iso(),
            )
            runtime.store.append_event(
                job_id,
                type="job_interrupted",
                phase="researching",
                message="Deep research paused after reaching the time budget.",
                data={"completed_units": len(completed_unit_ids)},
            )
            return

        batch_units = ready_units[: max(1, config.deep_research_max_concurrency)]
        batch_results = await asyncio.gather(*[_execute_research_unit(runtime, plan, unit) for unit in batch_units])
        for unit, (unit_result, new_sources, new_evidence) in zip(batch_units, batch_results, strict=False):
            source_registry, source_ids = _merge_source_registry(source_registry, new_sources)
            for evidence in new_evidence:
                evidence["source_ids"] = [source_id for source_id in evidence.get("source_ids", []) if source_id in source_ids] or source_ids
            completed_unit_ids.append(unit.unit_id)
            unit_results[unit.unit_id] = {
                "unit_id": unit.unit_id,
                "unit_type": unit.unit_type,
                "summary": unit_result["summary"],
                "detail": unit_result["detail"],
                "citations": source_ids,
                "source_ids": source_ids,
            }
            evidence_items.extend(new_evidence)
            unit_results = _sanitize_unit_results(unit_results, source_registry)
            evidence_items = _sanitize_evidence_items(evidence_items, source_registry)
            checkpoint_state = DeepResearchCheckpointState(
                plan=plan,
                completed_unit_ids=completed_unit_ids,
                unit_results=unit_results,
                sources=source_registry,
                evidence_items=evidence_items,
                sections=sections,
            )
            runtime.store.save_checkpoint(
                job_id,
                phase="researching",
                checkpoint_key=f"researching-{unit.unit_id}",
                state=checkpoint_state.model_dump(),
            )
            runtime.store.append_event(
                job_id,
                type="research_unit_completed",
                phase="researching",
                message=f"Completed {unit.unit_id}.",
                data={"unit_type": unit.unit_type, "sources_count": len(source_ids)},
            )
            progress = 20.0 + (len(completed_unit_ids) / unit_total) * 50.0
            runtime.store.update_job(job_id, progress_pct=min(progress, 75.0), heartbeat_at=utc_now_iso())

    sections = _build_section_citations(plan.report_outline, evidence_items, source_registry)
    unit_results = _sanitize_unit_results(unit_results, source_registry)
    runtime.store.update_job(job_id, phase="synthesizing", progress_pct=82.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="synthesizing",
        message="Synthesizing findings.",
        data={"section_count": len(sections)},
    )
    partial_report = _build_partial_report(plan, completed_unit_ids, unit_results, sections)
    runtime.write_artifact(job_id, "partial_report.md", partial_report, "text/markdown")
    runtime.store.save_checkpoint(
        job_id,
        phase="synthesizing",
        checkpoint_key="synthesizing",
        state=DeepResearchCheckpointState(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
        ).model_dump(),
    )

    if runtime.store.get_job(job_id).cancel_requested:
        _mark_canceled(runtime, job_id, "synthesizing")
        return

    citations = {
        "source_registry": {item["source_id"]: item for item in source_registry},
        "sections": _sanitize_sections(sections, source_registry),
    }
    report = {
        "query": plan.query,
        "summary": citations["sections"][0]["claims"][0]["text"] if citations["sections"] and citations["sections"][0]["claims"] else "",
        "status": "completed",
        "sections": citations["sections"],
        "unit_results": unit_results,
    }
    final_report = _build_final_report(plan, sections, citations["source_registry"])
    runtime.store.update_job(job_id, phase="finalizing", progress_pct=94.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="finalizing",
        message="Finalizing report.",
        data={},
    )
    persisted_artifacts = runtime.write_artifact_batch(
        job_id,
        [
            {"kind": "sources.json", "content": _json_markdown_block(source_registry), "content_type": "application/json"},
            {"kind": "citations.json", "content": _json_markdown_block(citations), "content_type": "application/json"},
            {"kind": "report.json", "content": _json_markdown_block(report), "content_type": "application/json"},
            {"kind": "final_report.md", "content": final_report, "content_type": "text/markdown"},
        ],
    )
    artifact_batch_id = next(
        (artifact.get("metadata", {}).get("batch_id", "") for artifact in persisted_artifacts if artifact.get("metadata")),
        "",
    )
    runtime.store.save_checkpoint(
        job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state=DeepResearchCheckpointState(
            plan=plan,
            completed_unit_ids=completed_unit_ids,
            unit_results=unit_results,
            sources=source_registry,
            evidence_items=evidence_items,
            sections=sections,
        ).model_dump(),
    )
    runtime.store.update_job(
        job_id,
        status="completed",
        phase="finalizing",
        progress_pct=100.0,
        finished_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
    )
    runtime.store.append_event(
        job_id,
        type="job_completed",
        phase="finalizing",
        message="Deep research completed.",
        data={"sources_count": len(source_registry), "artifact_batch_id": artifact_batch_id},
    )


def _mark_canceled(runtime: DeepResearchRuntime, job_id: str, phase: str) -> None:
    runtime.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
    runtime.store.append_event(job_id, type="job_canceled", phase=phase, message="Canceled.", data={})


def _write_partial_outputs(
    runtime: DeepResearchRuntime,
    job_id: str,
    plan: DeepResearchPlan,
    completed_unit_ids: list[str],
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> None:
    partial_report = _build_partial_report(plan, completed_unit_ids, unit_results, sections)
    runtime.write_artifact(job_id, "partial_report.md", partial_report, "text/markdown")


async def _execute_research_unit(
    runtime: DeepResearchRuntime,
    plan: DeepResearchPlan,
    unit: DeepResearchResearchUnit,
) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
    if unit.unit_type == "fetch":
        fetched = await _fetch_url(unit.url)
        detail = fetched or "No content fetched."
        source = {"url": unit.url, "title": unit.title}
        return (
            {"summary": _summarize_evidence_text(detail, limit=180), "detail": detail},
            [source],
            [
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}",
                    unit_id=unit.unit_id,
                    summary=_summarize_evidence_text(detail, limit=_MAX_CLAIM_LENGTH),
                    detail=detail,
                ).model_dump()
            ],
        )

    if unit.unit_type == "map":
        mapped = await _map_url(unit.url, unit.instructions)
        detail = mapped or "No site map returned."
        return (
            {"summary": _summarize_evidence_text(detail, limit=180), "detail": detail},
            [{"url": unit.url, "title": unit.title}],
            [
                DeepResearchEvidenceItem(
                    evidence_id=f"evidence-{unit.unit_id}",
                    unit_id=unit.unit_id,
                    summary=_summarize_evidence_text(detail, limit=_MAX_CLAIM_LENGTH),
                    detail=detail,
                ).model_dump()
            ],
        )

    answer, sources = await _search_query(unit.query or unit.goal)
    evidence_items = [
        DeepResearchEvidenceItem(
            evidence_id=f"evidence-{unit.unit_id}-search",
            unit_id=unit.unit_id,
            summary=_summarize_evidence_text(answer, limit=_MAX_CLAIM_LENGTH),
            detail=answer,
        ).model_dump()
    ]
    selective_fetch = plan.search_strategy.selective_fetch
    fetch_limit = max(0, selective_fetch.max_urls_per_search)
    for source in _select_fetch_sources(sources, plan, unit)[:fetch_limit]:
        fetched = await _fetch_url(source["url"])
        if not fetched:
            continue
        evidence_items.append(
            DeepResearchEvidenceItem(
                evidence_id=f"evidence-{unit.unit_id}-fetch-{len(evidence_items)}",
                unit_id=unit.unit_id,
                summary=_summarize_evidence_text(fetched, limit=_MAX_CLAIM_LENGTH),
                detail=fetched,
            ).model_dump()
        )
    return (
        {"summary": _summarize_evidence_text(answer, limit=180), "detail": answer},
        sources,
        evidence_items,
    )


def _merge_source_registry(
    existing_sources: list[dict[str, Any]],
    new_sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    existing_by_url = {
        item["url"]: dict(item)
        for item in existing_sources
        if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].strip()
    }
    next_index = len(existing_by_url) + 1
    merged_ids: list[str] = []
    for source in standardize_sources(new_sources):
        url = source.get("url", "").strip()
        if not url:
            continue
        if url in existing_by_url:
            current = existing_by_url[url]
            current.update({key: value for key, value in source.items() if value not in ("", None, [])})
            existing_by_url[url] = current
        else:
            current = dict(source)
            current["source_id"] = f"R{next_index}"
            next_index += 1
            existing_by_url[url] = current
        merged_ids.append(existing_by_url[url]["source_id"])
    merged_sources = sorted(existing_by_url.values(), key=lambda item: item["source_id"])
    return merged_sources, merged_ids


def _select_fetch_sources(
    sources: list[dict[str, Any]],
    plan: DeepResearchPlan,
    unit: DeepResearchResearchUnit,
) -> list[dict[str, Any]]:
    outline_keywords = _tokenize_keywords(" ".join(f"{section.title} {section.goal}" for section in plan.report_outline))
    query_keywords = _tokenize_keywords(f"{unit.title} {unit.goal} {unit.query}")

    def score(source: dict[str, Any]) -> tuple[int, int, str]:
        title = f"{source.get('title', '')} {source.get('description', '')}"
        return (
            _count_keyword_overlap(title, outline_keywords),
            _count_keyword_overlap(title, query_keywords),
            source.get("url", ""),
        )

    ranked = sorted(sources, key=score, reverse=True)
    if plan.search_strategy.selective_fetch.prefer_titles_matching_outline:
        return ranked
    return sources


def _build_section_citations(
    outline: list[DeepResearchReportSection],
    evidence_items: list[dict[str, Any]],
    source_registry: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    all_source_ids = [item["source_id"] for item in source_registry]
    claims_pool = [
        DeepResearchEvidenceItem.model_validate(item)
        for item in evidence_items
        if item.get("summary") and not _is_noisy_text(item.get("summary", ""))
    ]
    if not claims_pool:
        claims_pool = [
            DeepResearchEvidenceItem(
                evidence_id="evidence-empty",
                unit_id="none",
                summary="No conclusive evidence was collected.",
                detail="No conclusive evidence was collected.",
            )
        ]

    sections: list[dict[str, Any]] = []
    claim_index = 1
    used_evidence_ids: set[str] = set()
    for section in outline:
        section_keywords = _tokenize_keywords(f"{section.title} {section.goal}")
        section_claims: list[dict[str, Any]] = []
        ranked_pool = sorted(
            claims_pool,
            key=lambda evidence: (
                _count_keyword_overlap(f"{evidence.summary} {evidence.detail}", section_keywords),
                0 if evidence.evidence_id in used_evidence_ids else 1,
                len(evidence.source_ids),
                evidence.evidence_id,
            ),
            reverse=True,
        )
        for evidence in ranked_pool:
            claim_text = _summarize_evidence_text(evidence.summary or evidence.detail, limit=_MAX_CLAIM_LENGTH)
            if not claim_text or _is_noisy_text(claim_text):
                continue
            claim = DeepResearchClaim(
                claim_id=f"{section.section_id}-claim-{claim_index}",
                text=claim_text,
                citations=evidence.source_ids or all_source_ids[:1],
            )
            section_claims.append(claim.model_dump())
            used_evidence_ids.add(evidence.evidence_id)
            claim_index += 1
            if len(section_claims) >= 2:
                break
        section_model = DeepResearchSectionCitations(
            section_id=section.section_id,
            title=section.title,
            claims=section_claims,
            citations=sorted({citation for claim in section_claims for citation in claim.get("citations", [])}),
        )
        sections.append(section_model.model_dump())
    return sections


def _build_partial_report(
    plan: DeepResearchPlan,
    completed_unit_ids: list[str],
    unit_results: dict[str, dict[str, Any]],
    sections: list[dict[str, Any]],
) -> str:
    lines = [
        "# Partial Report",
        "",
        "## Progress",
        "",
        f"- Completed units: {len(completed_unit_ids)} / {len(plan.research_units)}",
        "",
        "## Research Brief",
        "",
        f"- Objective: {plan.brief.objective}",
        f"- Deliverable: {plan.brief.deliverable}",
        "",
        "## Completed Units",
        "",
    ]
    for unit_id in completed_unit_ids:
        result = unit_results.get(unit_id, {})
        lines.append(f"- `{unit_id}`: {result.get('summary', 'Completed.')}")
    if sections:
        lines.extend(["", "## Draft Sections", ""])
        for section in sections:
            lines.append(f"### {section['title']}")
            lines.append("")
            for claim in section.get("claims", []):
                refs = ", ".join(claim.get("citations", []))
                lines.append(f"- {claim['text']} [{refs}]".rstrip())
            lines.append("")
    pending_titles = [unit.title for unit in plan.research_units if unit.unit_id not in completed_unit_ids]
    if pending_titles:
        lines.extend(["## Remaining Work", ""])
        lines.extend(f"- {title}" for title in pending_titles)
    return "\n".join(lines).strip() + "\n"


def _build_final_report(
    plan: DeepResearchPlan,
    sections: list[dict[str, Any]],
    source_registry: dict[str, dict[str, Any]],
) -> str:
    lines = [f"# {plan.query}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        lines.append("")
        for claim in section.get("claims", []):
            refs = ", ".join(claim.get("citations", []))
            lines.append(f"- {claim['text']} [{refs}]".rstrip())
        lines.append("")
    lines.extend(["## Sources", ""])
    for source_id, item in source_registry.items():
        title = item.get("title") or item["url"]
        lines.append(f"- [{source_id}] {title} - {item['url']}")
    return "\n".join(lines).strip() + "\n"


async def _search_query(query: str) -> tuple[str, list[dict]]:
    api_url = config.grok_api_url
    api_key = config.grok_api_key
    provider = GrokSearchProvider(api_url, api_key, config.grok_model)
    content, sources = await _provider_search_with_sources(provider, query, min_results=3, max_results=8)
    answer, extracted_sources = split_answer_and_sources(content)
    merged = standardize_sources(merge_sources(sources, extracted_sources))
    if not merged:
        merged = standardize_sources([{"url": url} for url in extract_unique_urls(answer or content)])
    return answer.strip() or content.strip(), merged


async def _provider_search_with_sources(
    provider: GrokSearchProvider,
    query: str,
    *,
    platform: str = "",
    min_results: int = 3,
    max_results: int = 10,
) -> tuple[str, list[dict]]:
    kwargs = {
        "platform": platform,
        "min_results": min_results,
        "max_results": max_results,
        "ctx": None,
    }
    supported_kwargs = _filter_supported_search_kwargs(provider.search_with_sources, kwargs)
    content, sources = await provider.search_with_sources(query, **supported_kwargs)
    return content, sources


async def _fetch_url(url: str) -> str | None:
    from . import server

    result = await server.web_fetch(url)
    if not result or result.startswith("提取失败:") or result.startswith("配置错误:"):
        return None
    return result


async def _map_url(url: str, instructions: str = "") -> str | None:
    from . import server

    result = await server.web_map(url, instructions=instructions)
    if not result or result.startswith("映射失败:") or result.startswith("配置错误:"):
        return None
    return result
