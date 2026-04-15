import json
from pathlib import Path

from grok_search import deep_research_cli
from grok_search.deep_research_runtime import DeepResearchRuntime
from grok_search.deep_research_types import utc_now_iso


def build_runtime(tmp_path):
    return DeepResearchRuntime(tmp_path / "deep-research")


def test_cli_plan_only_start_prints_draft_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)

    exit_code = deep_research_cli.main(
        ["start", "Plan a deep research run", "--context", "Need the plan first.", "--plan-only"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "draft"
    assert payload["plan"]["context"] == "Need the plan first."


def test_cli_result_artifact_prints_artifact_content(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    response = runtime.store.create_job(
        query="Artifact job",
        request_fingerprint="fp-artifact",
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
    runtime.write_artifact(response.job_id, "final_report.md", "# Final Report\n\nArtifact body.", "text/markdown")

    exit_code = deep_research_cli.main(["result", response.job_id, "--artifact", "final_report.md"])

    assert exit_code == 0
    assert capsys.readouterr().out == "# Final Report\n\nArtifact body.\n"


def test_cli_result_artifact_prefers_resolved_final_batch(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Artifact batch job",
        request_fingerprint="fp-artifact-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Artifact batch job"}), "application/json")
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

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "sources.json"])

    assert exit_code == 0
    assert capsys.readouterr().out == '[{"source_id": "R1", "url": "https://good.example.com"}]\n'


def test_cli_result_artifact_reads_resolved_final_batch_for_interrupted_finalizing_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="Interrupted artifact batch job",
        request_fingerprint="fp-interrupted-artifact-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Interrupted artifact batch job"}), "application/json")
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
    runtime.write_artifact(
        job.job_id,
        "final_report.md",
        "# Final Report\n\nStale current report.\n",
        "text/markdown",
    )

    exit_code = deep_research_cli.main(["result", job.job_id, "--artifact", "final_report.md"])

    assert exit_code == 0
    assert capsys.readouterr().out == "# Final Report\n\nRecovered report.\n\n"


def test_cli_status_surfaces_resolved_artifact_batch_id(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    job = runtime.store.create_job(
        query="CLI status resolved batch",
        request_fingerprint="fp-cli-status-resolved-batch",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "CLI status resolved batch"}), "application/json")
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

    exit_code = deep_research_cli.main(["status", job.job_id])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["resolved_artifact_batch_id"]
    assert payload["artifact_fallback_used"] is True


def test_cli_start_spawns_worker_for_background_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    exit_code = deep_research_cli.main(["start", "Run in background"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert spawned == [payload["job_id"]]


def test_cli_continue_creates_follow_up_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    original = runtime.store.create_job(
        query="Original job",
        request_fingerprint="fp-original",
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

    exit_code = deep_research_cli.main(["continue", original.job_id, "Continue from previous findings"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["continued_from_job_id"] == original.job_id
    assert spawned == [payload["job_id"]]


def test_cli_start_does_not_spawn_worker_for_reused_completed_job(monkeypatch, tmp_path, capsys):
    runtime = build_runtime(tmp_path)
    spawned = []
    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: runtime)
    monkeypatch.setattr(deep_research_cli, "_spawn_worker", lambda job_id: spawned.append(job_id))

    fingerprint = runtime._request_fingerprint(
        query="Reuse me",
        context="",
        effort="standard",
        include_domains=[],
        exclude_domains=[],
        continue_from_job_id="",
        plan_only=False,
    )
    job = runtime.store.create_job(
        query="Reuse me",
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
    runtime.write_artifact(job.job_id, "plan.json", json.dumps({"query": "Reuse me"}), "application/json")
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

    exit_code = deep_research_cli.main(["start", "Reuse me"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["reused"] is True
    assert payload["status"] == "completed"
    assert spawned == []


def test_spawn_worker_writes_logs_to_worker_log_dir(monkeypatch, tmp_path):
    captured = {}
    runtime_dir = tmp_path / "deep-research"
    monkeypatch.setenv("GROK_DEEP_RESEARCH_DIR", str(runtime_dir))

    def fake_popen(cmd, stdout=None, stderr=None, start_new_session=None):
        captured["cmd"] = cmd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["start_new_session"] = start_new_session
        return object()

    monkeypatch.setattr(deep_research_cli.subprocess, "Popen", fake_popen)

    deep_research_cli._spawn_worker("job-123")

    assert captured["cmd"][-2:] == ["_worker", "job-123"]
    assert captured["start_new_session"] is True
    assert Path(captured["stdout"].name).name == "job-123.stdout.log"
    assert Path(captured["stderr"].name).name == "job-123.stderr.log"
    assert Path(captured["stdout"].name).parent == runtime_dir / "worker-logs"
    captured["stdout"].close()
    captured["stderr"].close()


def test_cli_worker_returns_nonzero_when_job_fails(monkeypatch, tmp_path):
    class FailingRuntime:
        async def run_job(self, job_id):
            return {"status": "failed"}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: FailingRuntime())

    exit_code = deep_research_cli.main(["_worker", "job-123"])

    assert exit_code == 1


def test_cli_worker_returns_nonzero_when_job_is_interrupted(monkeypatch, tmp_path):
    class InterruptedRuntime:
        async def run_job(self, job_id):
            return {"status": "interrupted"}

    monkeypatch.setattr(deep_research_cli, "_build_runtime", lambda: InterruptedRuntime())

    exit_code = deep_research_cli.main(["_worker", "job-123"])

    assert exit_code == 1
