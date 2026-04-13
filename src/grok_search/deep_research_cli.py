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
    while True:
        status = await runtime.status(job_id)
        events = await runtime.events(job_id, after_seq=last_seq, limit=100)
        for event in events["events"]:
            print(f"[{event['seq']}] {event['phase']} {event['type']}: {event['message']}")
        last_seq = events["next_after_seq"]
        if status["status"] in TERMINAL_STATUSES:
            print(f"status={status['status']} progress={status['progress_pct']}")
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
    _print_json(await runtime.status(args.job_id))
    return 0


async def _handle_events(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    if args.follow:
        last_seq = args.after_seq
        while True:
            payload = await runtime.events(args.job_id, after_seq=last_seq, limit=args.limit)
            if payload["events"]:
                _print_json(payload)
            last_seq = payload["next_after_seq"]
            status = await runtime.status(args.job_id)
            if status["status"] in TERMINAL_STATUSES:
                return 0
            await asyncio.sleep(args.interval_seconds)
    _print_json(await runtime.events(args.job_id, after_seq=args.after_seq, limit=args.limit))
    return 0


async def _handle_result(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    if args.artifact:
        content = runtime.store.read_artifact_text(args.job_id, args.artifact)
        if content is None:
            print(f"artifact_not_found: {args.artifact}", file=sys.stderr)
            return 1
        print(content)
        return 0
    _print_json(await runtime.result(args.job_id, include_partial=args.include_partial))
    return 0


async def _handle_resume(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    response = await runtime.resume(args.job_id, schedule=False)
    if response["status"] == "queued":
        _spawn_worker(args.job_id)
    _print_json(response)
    if args.watch and response["status"] == "queued":
        await _watch_job(runtime, args.job_id, interval_seconds=args.interval_seconds)
    return 0


async def _handle_cancel(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    _print_json(await runtime.cancel(args.job_id))
    return 0


async def _handle_list(args: argparse.Namespace) -> int:
    runtime = _build_runtime()
    _print_json(await runtime.list_jobs(status=args.status, limit=args.limit))
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
    return 1 if result.get("status") == "failed" else 0


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
