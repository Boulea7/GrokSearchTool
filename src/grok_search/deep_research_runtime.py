import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import config
from .deep_research_store import DeepResearchStore
from .deep_research_types import DeepResearchJob, utc_now_iso
from .providers.base import _filter_supported_search_kwargs
from .providers.grok import GrokSearchProvider
from .sources import merge_sources, split_answer_and_sources, standardize_sources


Runner = Callable[["DeepResearchRuntime", str], Awaitable[None]]


def _json_markdown_block(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


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
        plan = self._build_plan(job)
        self.write_artifact(job.job_id, "plan.json", _json_markdown_block(plan), "application/json")
        self.store.save_checkpoint(job.job_id, phase="planning", checkpoint_key="planning", state=plan)
        self.store.append_event(
            job.job_id,
            type="job_created",
            phase="planning",
            message="Deep research job created.",
            data={"plan_only": plan_only},
        )
        job = self.store.get_job(job.job_id)

        if not plan_only and schedule:
            await self._schedule(job.job_id)

        return self._job_payload(job, reused=False)

    async def status(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        payload = self._serialize_job(job)
        payload["artifact_kinds"] = [artifact.kind for artifact in self.store.list_artifacts(job_id)]
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
        return {
            "job_id": job_id,
            "status": job.status,
            "phase": job.phase,
            "plan": json.loads(plan_text) if plan_text else None,
            "partial_report": partial_text,
            "final_report": final_text,
            "citations": json.loads(citations_text) if citations_text else None,
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
        )
        self.store.append_event(
            job_id,
            type="job_resumed",
            phase=job.phase,
            message="Deep research job resumed.",
            data={},
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
        path.write_text(content, encoding="utf-8")
        relative_path = str(path.relative_to(self.store.root_dir))
        artifact = self.store.upsert_artifact(
            job_id,
            kind=kind,
            path=relative_path,
            content_type=content_type,
            metadata={"bytes": len(content.encode("utf-8"))},
        )
        return artifact.model_dump()

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

    def _build_plan(self, job: DeepResearchJob) -> dict[str, Any]:
        query = job.query.strip()
        context = job.context.strip()
        base_queries = [query]
        if context:
            base_queries.append(f"{query} {context}".strip())
        if job.effort == "deep":
            base_queries.append(f"{query} recent developments")
            base_queries.append(f"{query} comparisons and tradeoffs")
        else:
            base_queries.append(f"{query} key findings")

        search_queries: list[str] = []
        seen: set[str] = set()
        for item in base_queries:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            search_queries.append(normalized)

        return {
            "query": query,
            "context": context,
            "effort": job.effort,
            "time_budget_seconds": job.resolved_budget_seconds,
            "include_domains": job.include_domains,
            "exclude_domains": job.exclude_domains,
            "search_queries": search_queries,
            "report_sections": [
                "Executive Summary",
                "Research Plan",
                "Key Findings",
                "Open Questions / Gaps",
                "Detailed Report",
                "Citations",
            ],
        }

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


async def _default_runner(runtime: DeepResearchRuntime, job_id: str) -> None:
    job = runtime.store.update_job(
        job_id,
        status="running",
        started_at=utc_now_iso(),
        heartbeat_at=utc_now_iso(),
    )
    plan_text = runtime.store.read_artifact_text(job_id, "plan.json")
    plan = json.loads(plan_text) if plan_text else runtime._build_plan(job)

    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="planning",
        message="Planning started.",
        data={"query_count": len(plan["search_queries"])},
    )
    runtime.store.save_checkpoint(job_id, phase="planning", checkpoint_key="planning", state=plan)
    runtime.store.update_job(job_id, progress_pct=10.0, heartbeat_at=utc_now_iso())

    if runtime.store.get_job(job_id).cancel_requested:
        runtime.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
        runtime.store.append_event(job_id, type="job_canceled", phase="planning", message="Canceled.", data={})
        return

    notes: list[dict[str, Any]] = []
    merged_sources: list[dict] = []
    runtime.store.update_job(job_id, phase="researching", progress_pct=20.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="researching",
        message="Researching source material.",
        data={},
    )

    search_queries = plan["search_queries"][: config.deep_research_max_concurrency + 1]
    for index, query in enumerate(search_queries, start=1):
        if runtime.store.get_job(job_id).cancel_requested:
            runtime.store.update_job(job_id, status="canceled", finished_at=utc_now_iso(), heartbeat_at=utc_now_iso())
            runtime.store.append_event(job_id, type="job_canceled", phase="researching", message="Canceled.", data={})
            return
        answer, sources = await _search_query(query)
        note = {
            "query": query,
            "answer": answer,
            "sources": sources,
        }
        notes.append(note)
        merged_sources = merge_sources(merged_sources, sources)
        runtime.store.save_checkpoint(
            job_id,
            phase="researching",
            checkpoint_key=f"researching-{index}",
            state={"completed_queries": index, "last_query": query},
        )
        runtime.store.append_event(
            job_id,
            type="research_unit_completed",
            phase="researching",
            message=f"Completed research unit {index}.",
            data={"query": query, "sources_count": len(sources)},
        )
        runtime.store.update_job(job_id, progress_pct=min(20.0 + index * 20.0, 75.0), heartbeat_at=utc_now_iso())

    notes_jsonl = "\n".join(_json_markdown_block(item) for item in notes)
    runtime.write_artifact(job_id, "notes.jsonl", notes_jsonl, "application/x-ndjson")
    standardized_sources = standardize_sources(merged_sources)
    runtime.write_artifact(job_id, "sources.json", _json_markdown_block(standardized_sources), "application/json")

    runtime.store.update_job(job_id, phase="synthesizing", progress_pct=80.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="synthesizing",
        message="Synthesizing findings.",
        data={"note_count": len(notes)},
    )
    partial_report = _build_partial_report(plan, notes)
    runtime.write_artifact(job_id, "partial_report.md", partial_report, "text/markdown")
    runtime.store.save_checkpoint(
        job_id,
        phase="synthesizing",
        checkpoint_key="synthesizing",
        state={"note_count": len(notes), "sources_count": len(standardized_sources)},
    )

    runtime.store.update_job(job_id, phase="finalizing", progress_pct=92.0, heartbeat_at=utc_now_iso())
    runtime.store.append_event(
        job_id,
        type="phase_started",
        phase="finalizing",
        message="Finalizing report.",
        data={},
    )
    final_report = _build_final_report(plan, notes, standardized_sources)
    runtime.write_artifact(job_id, "final_report.md", final_report, "text/markdown")
    citations = _build_citations(standardized_sources)
    runtime.write_artifact(job_id, "citations.json", _json_markdown_block(citations), "application/json")
    runtime.write_artifact(
        job_id,
        "report.json",
        _json_markdown_block(
            {
                "query": job.query,
                "status": "completed",
                "notes_count": len(notes),
                "sources_count": len(standardized_sources),
            }
        ),
        "application/json",
    )
    runtime.store.save_checkpoint(
        job_id,
        phase="finalizing",
        checkpoint_key="finalizing",
        state={"artifact_kinds": [artifact.kind for artifact in runtime.store.list_artifacts(job_id)]},
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
        data={"sources_count": len(standardized_sources)},
    )


async def _search_query(query: str) -> tuple[str, list[dict]]:
    api_url = config.grok_api_url
    api_key = config.grok_api_key
    provider = GrokSearchProvider(api_url, api_key, config.grok_model)
    content, sources = await _provider_search_with_sources(provider, query, min_results=3, max_results=8)
    answer, extracted_sources = split_answer_and_sources(content)
    merged = standardize_sources(merge_sources(sources, extracted_sources))
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


def _build_partial_report(plan: dict[str, Any], notes: list[dict[str, Any]]) -> str:
    lines = [
        "# Partial Report",
        "",
        "## Research Plan",
        "",
    ]
    lines.extend(f"- {query}" for query in plan["search_queries"])
    lines.extend(["", "## Working Notes", ""])
    for note in notes:
        lines.append(f"### {note['query']}")
        lines.append("")
        lines.append(note["answer"] or "No answer returned.")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _build_final_report(plan: dict[str, Any], notes: list[dict[str, Any]], sources: list[dict[str, Any]]) -> str:
    lines = [
        f"# {plan['query']}",
        "",
        "## Executive Summary",
        "",
    ]
    for note in notes:
        summary_line = note["answer"].splitlines()[0].strip() if note["answer"].strip() else "No summary available."
        lines.append(f"- {summary_line}")
    lines.extend(["", "## Research Plan", ""])
    lines.extend(f"- {query}" for query in plan["search_queries"])
    lines.extend(["", "## Detailed Report", ""])
    for note in notes:
        lines.append(f"### {note['query']}")
        lines.append("")
        lines.append(note["answer"] or "No answer returned.")
        lines.append("")
    lines.extend(["## Citations", ""])
    for citation_id, item in _build_citations(sources).items():
        title = item.get("title") or item["url"]
        lines.append(f"- [{citation_id}] {title} - {item['url']}")
    return "\n".join(lines).strip() + "\n"


def _build_citations(sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    citations: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources, start=1):
        citations[f"R{index}"] = source
    return citations
