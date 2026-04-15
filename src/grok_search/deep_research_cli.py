import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

from .config import config
from .deep_research_runtime import DeepResearchRuntime


TERMINAL_STATUSES = {"completed", "failed", "canceled", "interrupted"}


def _build_runtime() -> DeepResearchRuntime:
    return DeepResearchRuntime(config.deep_research_dir)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _summary_value(value: Any, *, empty: str = "-") -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return empty
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or empty
    return str(value)


def _format_progress(progress: Any) -> str:
    if progress is None or progress == "":
        return "-"
    try:
        return f"{float(progress):.1f}%"
    except (TypeError, ValueError):
        return _summary_value(progress)


def _quote_summary_text(value: str, *, limit: int = 80) -> str:
    text = value.strip()
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return json.dumps(text, ensure_ascii=False)


def _job_summary_parts(payload: dict[str, Any], *, fallback_job_id: str = "") -> list[str]:
    artifact_fallback = payload.get("artifact_fallback_used")
    parts = [
        f"job={_summary_value(payload.get('job_id') or fallback_job_id)}",
        f"status={_summary_value(payload.get('status'))}",
        f"phase={_summary_value(payload.get('phase'))}",
        f"progress={_format_progress(payload.get('progress_pct'))}",
        f"checkpoint={_summary_value(payload.get('current_checkpoint'))}",
        f"attempts={_summary_value(payload.get('attempt_count', 0), empty='0')}",
        f"cancel_requested={_summary_value(payload.get('cancel_requested', False), empty='false')}",
        f"continued_from={_summary_value(payload.get('continued_from_job_id'))}",
        f"resolved_batch={_summary_value(payload.get('resolved_artifact_batch_id'))}",
        f"artifact_fallback={_summary_value(artifact_fallback) if artifact_fallback is not None else '-'}",
    ]
    if payload.get("planner_fallback_used"):
        parts.append("planner_fallback=true")
    runtime_warnings = payload.get("runtime_warnings")
    if isinstance(runtime_warnings, list) and runtime_warnings:
        parts.append(f"warnings={len(runtime_warnings)}")
    constraint_violations = payload.get("constraint_violations")
    if isinstance(constraint_violations, list) and constraint_violations:
        parts.append(f"constraint_violations={len(constraint_violations)}")
    return parts


def _print_summary_line(parts: list[str]) -> None:
    print(f"summary: {' '.join(parts)}", file=sys.stderr)


def _print_job_summary(payload: dict[str, Any], *, fallback_job_id: str = "", extra_parts: list[str] | None = None) -> None:
    parts = _job_summary_parts(payload, fallback_job_id=fallback_job_id)
    if extra_parts:
        parts.extend(extra_parts)
    _print_summary_line(parts)


def _print_list_summary(payload: dict[str, Any], *, status_filter: str, limit: int) -> None:
    jobs = payload.get("jobs", [])
    _print_summary_line(
        [
            f"jobs={len(jobs)}",
            f"status_filter={status_filter or 'all'}",
            f"limit={limit}",
        ]
    )
    for job in jobs:
        _print_job_summary(
            job,
            extra_parts=[f"query={_quote_summary_text(str(job.get('query', '')))}"],
        )


def _print_events_summary(
    payload: dict[str, Any],
    *,
    after_seq: int,
    terminal: bool,
    fallback_job_id: str = "",
) -> None:
    events = payload.get("events", [])
    last_event = events[-1]["type"] if events else "-"
    _print_summary_line(
        [
            f"job={_summary_value(payload.get('job_id') or fallback_job_id)}",
            f"events={len(events)}",
            f"after_seq={after_seq}",
            f"next_after_seq={payload.get('next_after_seq', after_seq)}",
            f"last_event={last_event}",
            f"terminal={'true' if terminal else 'false'}",
        ]
    )


def _spawn_worker(job_id: str) -> None:
    log_dir = config.deep_research_dir / "worker-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{job_id}.stdout.log"
    stderr_path = log_dir / f"{job_id}.stderr.log"
    subprocess.Popen(
        [sys.executable, "-m", "grok_search.deep_research_cli", "_worker", job_id],
        stdout=stdout_path.open("ab"),
        stderr=stderr_path.open("ab"),
        start_new_session=True,
    )


async def _watch_job(runtime: DeepResearchRuntime, job_id: str, *, interval_seconds: float = 1.0) -> None:
    last_seq = 0
    last_status_line = ""
    while True:
        status = await runtime.status(job_id)
        events = await runtime.events(job_id, after_seq=last_seq, limit=100)
        for event in events["events"]:
            print(f"[{event['seq']}] {event['phase']} {event['type']}: {event['message']}", file=sys.stderr)
        last_seq = events["next_after_seq"]
        summary_parts = _job_summary_parts(status, fallback_job_id=job_id)
        status_line = " ".join(summary_parts)
        if status_line != last_status_line:
            _print_summary_line(summary_parts)
            last_status_line = status_line
        if status["status"] in TERMINAL_STATUSES:
            return
        await asyncio.sleep(interval_seconds)


async def _handle_start(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.start(
        query=args.query,
        context=args.context,
        effort=args.effort,
        time_budget_seconds=args.time_budget_seconds,
        include_domains=args.include_domain,
        exclude_domains=args.exclude_domain,
        continue_from_job_id=args.continue_from_job_id,
        plan_only=args.plan_only,
        force_new=args.force_new,
        schedule=False,
    )
    if not args.plan_only and not response.get("reused") and response.get("status") == "queued":
        _spawn_worker(response["job_id"])
    _print_json(response)
    if args.watch and not args.plan_only:
        await _watch_job(runtime, response["job_id"], interval_seconds=args.interval_seconds)
    return 0


async def _handle_watch(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    await _watch_job(runtime, args.job_id, interval_seconds=args.interval_seconds)
    return 0


async def _handle_status(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    payload = await runtime.status(args.job_id)
    _print_job_summary(payload, fallback_job_id=args.job_id)
    _print_json(payload)
    return 0


async def _handle_events(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    if args.follow:
        last_seq = args.after_seq
        while True:
            payload = await runtime.events(args.job_id, after_seq=last_seq, limit=args.limit)
            status = await runtime.status(args.job_id)
            _print_events_summary(
                payload,
                after_seq=last_seq,
                terminal=status["status"] in TERMINAL_STATUSES,
                fallback_job_id=args.job_id,
            )
            if payload["events"]:
                _print_json(payload)
            last_seq = payload["next_after_seq"]
            if status["status"] in TERMINAL_STATUSES:
                return 0
            await asyncio.sleep(args.interval_seconds)
    payload = await runtime.events(args.job_id, after_seq=args.after_seq, limit=args.limit)
    status = await runtime.status(args.job_id)
    _print_events_summary(
        payload,
        after_seq=args.after_seq,
        terminal=status["status"] in TERMINAL_STATUSES,
        fallback_job_id=args.job_id,
    )
    _print_json(payload)
    return 0


async def _handle_result(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    if args.artifact:
        content = runtime.read_artifact_text(args.job_id, args.artifact)
        if content is None:
            print(f"artifact_not_found: {args.artifact}", file=sys.stderr)
            return 1
        status = await runtime.status(args.job_id)
        _print_job_summary(
            status,
            fallback_job_id=args.job_id,
            extra_parts=[
                f"artifact={args.artifact}",
                f"bytes={len(content.encode('utf-8'))}",
            ],
        )
        print(content)
        return 0
    _print_json(await runtime.result(args.job_id, include_partial=args.include_partial))
    return 0


async def _handle_resume(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.resume(args.job_id, schedule=False)
    if response["status"] == "queued":
        _spawn_worker(args.job_id)
    _print_job_summary(response, fallback_job_id=args.job_id)
    _print_json(response)
    if args.watch and response["status"] == "queued":
        await _watch_job(runtime, args.job_id, interval_seconds=args.interval_seconds)
    return 0


async def _handle_cancel(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.cancel(args.job_id)
    _print_job_summary(await runtime.status(args.job_id), fallback_job_id=args.job_id)
    _print_json(response)
    return 0


async def _handle_list(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    payload = await runtime.list_jobs(status=args.status, limit=args.limit)
    _print_list_summary(payload, status_filter=args.status, limit=args.limit)
    _print_json(payload)
    return 0


async def _handle_continue(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.start(
        query=args.query,
        context=args.context,
        effort=args.effort,
        time_budget_seconds=args.time_budget_seconds,
        include_domains=args.include_domain,
        exclude_domains=args.exclude_domain,
        continue_from_job_id=args.job_id,
        plan_only=False,
        force_new=args.force_new,
        schedule=False,
    )
    if not response.get("reused") and response.get("status") == "queued":
        _spawn_worker(response["job_id"])
    _print_json(response)
    if args.watch:
        await _watch_job(runtime, response["job_id"], interval_seconds=args.interval_seconds)
    return 0


async def _handle_worker(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    result = await runtime.run_job(args.job_id)
    return 0 if result.get("status") in {"completed", "canceled", "draft"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grok-search-research")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_research_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--context", default="")
        command.add_argument("--effort", default="standard")
        command.add_argument("--time-budget-seconds", type=int, default=0)
        command.add_argument("--include-domain", action="append", default=[])
        command.add_argument("--exclude-domain", action="append", default=[])
        command.add_argument("--force-new", action="store_true")
        command.add_argument("--watch", action="store_true")
        command.add_argument("--interval-seconds", type=float, default=1.0)

    start = subparsers.add_parser("start")
    start.add_argument("query")
    add_common_research_args(start)
    start.add_argument("--continue-from-job-id", default="")
    start.add_argument("--plan-only", action="store_true")

    watch = subparsers.add_parser("watch")
    watch.add_argument("job_id")
    watch.add_argument("--interval-seconds", type=float, default=1.0)

    status = subparsers.add_parser("status")
    status.add_argument("job_id")

    events = subparsers.add_parser("events")
    events.add_argument("job_id")
    events.add_argument("--after-seq", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--follow", action="store_true")
    events.add_argument("--interval-seconds", type=float, default=1.0)

    result = subparsers.add_parser("result")
    result.add_argument("job_id")
    result.add_argument("--artifact", default="")
    result.add_argument("--include-partial", action="store_true")

    resume = subparsers.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--watch", action="store_true")
    resume.add_argument("--interval-seconds", type=float, default=1.0)

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("job_id")

    listing = subparsers.add_parser("list")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=50)

    cont = subparsers.add_parser("continue")
    cont.add_argument("job_id")
    cont.add_argument("query")
    add_common_research_args(cont)

    worker = subparsers.add_parser("_worker")
    worker.add_argument("job_id")
    return parser


async def _run(args: argparse.Namespace) -> int:
    handlers = {
        "start": _handle_start,
        "watch": _handle_watch,
        "status": _handle_status,
        "events": _handle_events,
        "result": _handle_result,
        "resume": _handle_resume,
        "cancel": _handle_cancel,
        "list": _handle_list,
        "continue": _handle_continue,
        "_worker": _handle_worker,
    }
    return await handlers[args.command](args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
