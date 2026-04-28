import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

from .config import config
from .deep_research_runtime import DeepResearchRuntime


TERMINAL_STATUSES = {"completed", "failed", "canceled", "interrupted"}
ATTACH_TERMINAL_EVENT_TYPES = {
    "job_canceled",
    "job_completed",
    "job_failed",
    "job_interrupted",
    "job_resolved_from_final_batch",
}


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
    last_error = _summary_value(payload.get("last_error"))
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
    if last_error != "-":
        parts.append(f"last_error={last_error}")
    if payload.get("planner_fallback_used"):
        parts.append("planner_fallback=true")
    if payload.get("partial_payload_available"):
        parts.append("partial_payload_available=true")
    watch_attach_after_seq = payload.get("watch_attach_after_seq")
    if isinstance(watch_attach_after_seq, int) and watch_attach_after_seq > 0:
        parts.append(f"watch_attach_after_seq={watch_attach_after_seq}")
    attempt_window_start_seq = payload.get("attempt_window_start_seq")
    if isinstance(attempt_window_start_seq, int) and attempt_window_start_seq > 0:
        parts.append(f"attempt_window_start_seq={attempt_window_start_seq}")
    artifact_visibility_reason = _summary_value(payload.get("artifact_visibility_reason"))
    if artifact_visibility_reason != "-":
        parts.append(f"artifact_visibility_reason={artifact_visibility_reason}")
    runtime_warnings = payload.get("runtime_warnings")
    if isinstance(runtime_warnings, list) and runtime_warnings:
        parts.append(f"warnings={len(runtime_warnings)}")
        parts.append(f"warning_codes={','.join(str(item).strip() for item in runtime_warnings[:3] if str(item).strip())}")
    constraint_violations = payload.get("constraint_violations")
    if isinstance(constraint_violations, list) and constraint_violations:
        parts.append(f"constraint_violations={len(constraint_violations)}")
        constraint_codes: list[str] = []
        normalized_items = [item for item in constraint_violations if isinstance(item, dict)]
        repeated_reason_mode = (
            len(normalized_items) > 1
            and len(
                {
                    _summary_value(item.get("reason"))
                    for item in normalized_items
                    if _summary_value(item.get("reason")) != "-"
                }
            )
            == 1
        )
        for item in normalized_items[:3]:
            preferred_keys = ("unit_id", "reason", "code") if repeated_reason_mode else ("reason", "code", "unit_id")
            if not isinstance(item, dict):
                continue
            for key in preferred_keys:
                value = _summary_value(item.get(key))
                if value != "-" and value not in constraint_codes:
                    constraint_codes.append(value)
                    break
        if constraint_codes:
            parts.append(f"constraint_codes={','.join(constraint_codes)}")
    return parts


def _print_summary_line(parts: list[str]) -> None:
    print(f"summary: {' '.join(parts)}", file=sys.stderr)


def _print_job_summary(payload: dict[str, Any], *, fallback_job_id: str = "", extra_parts: list[str] | None = None) -> None:
    parts = _job_summary_parts(payload, fallback_job_id=fallback_job_id)
    if extra_parts:
        parts.extend(extra_parts)
    _print_summary_line(parts)


def _watch_existing_state_message(payload: dict[str, Any], *, fallback_job_id: str = "") -> str | None:
    attempts = payload.get("attempt_count")
    try:
        numeric_attempts = int(attempts)
    except (TypeError, ValueError):
        numeric_attempts = 0
    continued_from = _summary_value(payload.get("continued_from_job_id"))
    if numeric_attempts <= 1 and continued_from == "-":
        return None

    parts = [f"job={_summary_value(payload.get('job_id') or fallback_job_id)}"]
    if numeric_attempts > 1:
        parts.append(f"attempts={numeric_attempts}")
    checkpoint = _summary_value(payload.get("current_checkpoint"))
    if checkpoint != "-":
        parts.append(f"checkpoint={checkpoint}")
    if continued_from != "-":
        parts.append(f"continued_from={continued_from}")
    return f"watch: attached_to_existing_state {' '.join(parts)}"


def _event_attempt_count(event: dict[str, Any]) -> int | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("attempt_count")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_attempt_id(event: dict[str, Any]) -> str:
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    value = str(data.get("attempt_id", "") or "").strip()
    if value:
        return value
    attempt_count = _event_attempt_count(event)
    return f"attempt-{attempt_count}" if attempt_count is not None and attempt_count > 0 else ""


async def _resolve_watch_attach_after_seq(
    runtime: DeepResearchRuntime,
    job_id: str,
    payload: dict[str, Any],
    *,
    page_limit: int = 100,
) -> int:
    explicit_anchor = payload.get("watch_attach_after_seq")
    try:
        normalized_explicit_anchor = int(explicit_anchor)
    except (TypeError, ValueError):
        normalized_explicit_anchor = None
    if normalized_explicit_anchor is not None and normalized_explicit_anchor >= 0:
        return normalized_explicit_anchor
    attempt_window_start_seq = payload.get("attempt_window_start_seq")
    try:
        normalized_attempt_window_start_seq = int(attempt_window_start_seq)
    except (TypeError, ValueError):
        normalized_attempt_window_start_seq = None
    if normalized_attempt_window_start_seq is not None and normalized_attempt_window_start_seq > 0:
        return normalized_attempt_window_start_seq - 1
    attempts = payload.get("attempt_count")
    continued_from = _summary_value(payload.get("continued_from_job_id"))
    try:
        numeric_attempts = int(attempts)
    except (TypeError, ValueError):
        numeric_attempts = 0
    payload_attempt_id = str(payload.get("attempt_id", "") or "").strip()
    if not payload_attempt_id and numeric_attempts > 0:
        payload_attempt_id = f"attempt-{numeric_attempts}"
    previous_attempt_id = f"attempt-{max(0, numeric_attempts - 1)}" if numeric_attempts > 1 else ""
    if numeric_attempts <= 1 and continued_from == "-":
        return 0

    after_seq = 0
    fallback_after_seq = 0
    previous_attempt_after_seq = 0
    previous_attempt_count = max(0, numeric_attempts - 1)
    while True:
        events_payload = await runtime.events(job_id, after_seq=after_seq, limit=page_limit)
        events = events_payload.get("events", [])
        if not events:
            return fallback_after_seq or previous_attempt_after_seq

        for event in events:
            try:
                event_seq = int(event.get("seq", 0))
            except (TypeError, ValueError):
                event_seq = 0
            attempt_count = _event_attempt_count(event)
            attempt_id = _event_attempt_id(event)
            event_type = str(event.get("type", ""))
            if (
                (
                    (previous_attempt_id and attempt_id == previous_attempt_id)
                    or (previous_attempt_count > 0 and attempt_count == previous_attempt_count)
                )
                and event_type in ATTACH_TERMINAL_EVENT_TYPES
            ):
                previous_attempt_after_seq = max(previous_attempt_after_seq, event_seq)
                continue
            if payload_attempt_id:
                if attempt_id != payload_attempt_id:
                    continue
            elif attempt_count != numeric_attempts:
                continue
            if event_type == "job_resumed":
                return previous_attempt_after_seq or max(0, event_seq - 1)
            if fallback_after_seq == 0:
                fallback_after_seq = previous_attempt_after_seq or max(0, event_seq - 1)

        next_after_seq = events_payload.get("next_after_seq", after_seq)
        try:
            normalized_next_after_seq = int(next_after_seq)
        except (TypeError, ValueError):
            return fallback_after_seq or previous_attempt_after_seq
        if normalized_next_after_seq <= after_seq:
            return fallback_after_seq or previous_attempt_after_seq
        after_seq = normalized_next_after_seq
    return fallback_after_seq or previous_attempt_after_seq


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
    terminal: bool | None,
    fallback_job_id: str = "",
) -> None:
    events = payload.get("events", [])
    returned_count = payload.get("returned_count", len(events))
    last_event = payload.get("last_event_type") or (events[-1]["type"] if events else "-")
    window_has_terminal_event = payload.get("window_has_terminal_event")
    job_terminal = payload.get("job_terminal")
    effective_terminal = (
        bool(job_terminal or window_has_terminal_event)
        if job_terminal is not None
        else (bool(window_has_terminal_event) if window_has_terminal_event is not None else bool(terminal))
    )
    _print_summary_line(
        [
            f"job={_summary_value(payload.get('job_id') or fallback_job_id)}",
            f"events={returned_count}",
            f"after_seq={after_seq}",
            f"next_after_seq={payload.get('next_after_seq', after_seq)}",
            f"last_event={last_event}",
            f"terminal={'true' if effective_terminal else 'false'}",
            f"window_terminal={_summary_value(window_has_terminal_event) if window_has_terminal_event is not None else '-'}",
        ]
    )


async def _run_observation(coro: Any) -> bool:
    try:
        await coro
    except KeyboardInterrupt:
        return True
    return False


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


async def _watch_job(
    runtime: DeepResearchRuntime,
    job_id: str,
    *,
    interval_seconds: float = 1.0,
    initial_status: dict[str, Any] | None = None,
    suppress_initial_status_line: bool = False,
    after_seq: int = 0,
) -> None:
    last_seq = max(0, after_seq)
    last_status_line = ""
    printed_existing_state_message = False
    pending_status = initial_status
    first_status_from_initial = initial_status is not None
    if suppress_initial_status_line and initial_status is not None:
        last_status_line = " ".join(_job_summary_parts(initial_status, fallback_job_id=job_id))
    while True:
        status = pending_status if pending_status is not None else await runtime.status(job_id)
        pending_status = None
        events_payload: dict[str, Any] | None = None
        if not printed_existing_state_message:
            existing_state_message = _watch_existing_state_message(status, fallback_job_id=job_id)
            if existing_state_message:
                print(existing_state_message, file=sys.stderr)
                attach_after_seq = await _resolve_watch_attach_after_seq(runtime, job_id, status)
                last_seq = max(last_seq, attach_after_seq)
            printed_existing_state_message = True
        events_payload = await runtime.events(job_id, after_seq=last_seq, limit=100)
        for event in events_payload["events"]:
            print(f"[{event['seq']}] {event['phase']} {event['type']}: {event['message']}", file=sys.stderr)
        last_seq = events_payload["next_after_seq"]
        if first_status_from_initial and events_payload["events"]:
            status = await runtime.status(job_id)
            first_status_from_initial = False
        summary_parts = _job_summary_parts(status, fallback_job_id=job_id)
        status_line = " ".join(summary_parts)
        if status_line != last_status_line:
            _print_summary_line(summary_parts)
            last_status_line = status_line
        if status["status"] in TERMINAL_STATUSES:
            return
        first_status_from_initial = False
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
        interrupted = await _run_observation(
            _watch_job(
                runtime,
                response["job_id"],
                interval_seconds=args.interval_seconds,
                after_seq=args.after_seq,
            )
        )
        if interrupted:
            return 130
    return 0


async def _handle_watch(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    interrupted = await _run_observation(
        _watch_job(
            runtime,
            args.job_id,
            interval_seconds=args.interval_seconds,
            after_seq=args.after_seq,
        )
    )
    if interrupted:
        return 130
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
        async def follow_events() -> None:
            last_seq = args.after_seq
            while True:
                payload = await runtime.events(args.job_id, after_seq=last_seq, limit=args.limit)
                status = await runtime.status(args.job_id)
                _print_events_summary(
                    payload,
                    after_seq=last_seq,
                    terminal=payload.get("job_terminal"),
                    fallback_job_id=args.job_id,
                )
                if payload["events"]:
                    _print_json(payload)
                last_seq = payload["next_after_seq"]
                if status["status"] in TERMINAL_STATUSES:
                    return
                await asyncio.sleep(args.interval_seconds)

        interrupted = await _run_observation(follow_events())
        if interrupted:
            return 130
        return 0
    payload = await runtime.events(args.job_id, after_seq=args.after_seq, limit=args.limit)
    _print_events_summary(
        payload,
        after_seq=args.after_seq,
        terminal=payload.get("job_terminal"),
        fallback_job_id=args.job_id,
    )
    _print_json(payload)
    return 0


async def _handle_result(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    if args.artifact:
        artifact = runtime.read_artifact(args.job_id, args.artifact)
        content = artifact.get("content")
        if content is None:
            if artifact.get("state") == "hidden":
                print(
                    f"artifact_hidden: {args.artifact} reason={artifact.get('artifact_visibility_reason') or '-'}",
                    file=sys.stderr,
                )
                return 1
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
    if args.watch:
        if response.get("plan_only") and response.get("status") == "draft":
            print("plan_only_resume_blocked: plan-only draft jobs cannot be watched for execution", file=sys.stderr)
            return 0
        interrupted = await _run_observation(
            _watch_job(
                runtime,
                args.job_id,
                interval_seconds=args.interval_seconds,
                initial_status=response,
                suppress_initial_status_line=True,
                after_seq=args.after_seq,
            )
        )
        if interrupted:
            return 130
    return 0


async def _handle_cancel(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.cancel(args.job_id)
    _print_job_summary(response, fallback_job_id=args.job_id)
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
        interrupted = await _run_observation(
            _watch_job(
                runtime,
                response["job_id"],
                interval_seconds=args.interval_seconds,
                after_seq=args.after_seq,
            )
        )
        if interrupted:
            return 130
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
        command.add_argument("--effort", default="standard", help="Research effort profile: standard, deep, or ultra.")
        command.add_argument("--time-budget-seconds", type=int, default=0)
        command.add_argument("--include-domain", action="append", default=None)
        command.add_argument("--exclude-domain", action="append", default=None)
        command.add_argument("--force-new", action="store_true")
        command.add_argument("--watch", action="store_true")
        command.add_argument("--interval-seconds", type=float, default=1.0)
        command.add_argument("--after-seq", type=int, default=0)

    start = subparsers.add_parser("start")
    start.add_argument("query")
    add_common_research_args(start)
    start.add_argument("--continue-from-job-id", default="")
    start.add_argument("--plan-only", action="store_true")

    watch = subparsers.add_parser("watch")
    watch.add_argument("job_id")
    watch.add_argument("--interval-seconds", type=float, default=1.0)
    watch.add_argument("--after-seq", type=int, default=0)

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
    result.add_argument("--include-partial", action=argparse.BooleanOptionalAction, default=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("job_id")
    resume.add_argument("--watch", action="store_true")
    resume.add_argument("--interval-seconds", type=float, default=1.0)
    resume.add_argument("--after-seq", type=int, default=0)

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
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
