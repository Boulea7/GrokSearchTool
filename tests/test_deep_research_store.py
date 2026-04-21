from pathlib import Path

from grok_search import deep_research_store as deep_research_store_module
from grok_search.deep_research_store import DeepResearchStore
from grok_search.deep_research_types import (
    DeepResearchArtifact,
    DeepResearchCheckpoint,
    DeepResearchEvent,
    DeepResearchJob,
    utc_now_iso,
)


def make_store(tmp_path: Path) -> DeepResearchStore:
    return DeepResearchStore(tmp_path / "deep-research")


def test_store_creates_and_reads_job(tmp_path):
    store = make_store(tmp_path)

    created = store.create_job(
        query="Compare frontier coding agents",
        request_fingerprint="fp-1",
        status="queued",
        phase="planning",
        effort="standard",
        context="Focus on resumable research patterns.",
        include_domains=["github.com"],
        exclude_domains=["reddit.com"],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )

    loaded = store.get_job(created.job_id)

    assert isinstance(created, DeepResearchJob)
    assert loaded == created
    assert loaded.query == "Compare frontier coding agents"
    assert loaded.status == "queued"
    assert loaded.phase == "planning"
    assert loaded.include_domains == ["github.com"]
    assert loaded.exclude_domains == ["reddit.com"]
    assert loaded.resolved_budget_seconds == 240


def test_store_appends_events_with_monotonic_sequence(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research stateful agents",
        request_fingerprint="fp-events",
        status="running",
        phase="researching",
        effort="deep",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=300,
        continued_from_job_id="",
    )

    first = store.append_event(
        job.job_id,
        type="phase_started",
        phase="planning",
        message="Planning started.",
        data={"step": 1},
    )
    second = store.append_event(
        job.job_id,
        type="phase_completed",
        phase="planning",
        message="Planning completed.",
        data={"step": 1},
    )

    events = store.list_events(job.job_id)

    assert isinstance(first, DeepResearchEvent)
    assert isinstance(second, DeepResearchEvent)
    assert [event.seq for event in events] == [1, 2]
    assert store.list_events(job.job_id, after_seq=1) == [second]


def test_store_list_events_paginates_and_replays_after_seq_boundaries(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research event pagination",
        request_fingerprint="fp-events-pagination",
        status="running",
        phase="researching",
        effort="deep",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=300,
        continued_from_job_id="",
    )
    first = store.append_event(job.job_id, type="job_created", phase="planning", message="Created.")
    second = store.append_event(job.job_id, type="phase_started", phase="planning", message="Planning.")
    third = store.append_event(job.job_id, type="phase_started", phase="researching", message="Researching.")

    first_page = store.list_events(job.job_id, after_seq=0, limit=1)
    second_page = store.list_events(job.job_id, after_seq=first_page[-1].seq, limit=1)
    tail_page = store.list_events(job.job_id, after_seq=second_page[-1].seq, limit=10)
    empty_page = store.list_events(job.job_id, after_seq=third.seq, limit=10)

    assert first_page == [first]
    assert second_page == [second]
    assert tail_page == [third]
    assert empty_page == []


def test_store_list_events_clamps_negative_after_seq_and_non_positive_limit(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research event boundary handling",
        request_fingerprint="fp-events-boundaries",
        status="running",
        phase="researching",
        effort="deep",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=False,
        force_new=False,
        resolved_budget_seconds=300,
        continued_from_job_id="",
    )
    first = store.append_event(job.job_id, type="job_created", phase="planning", message="Created.")
    second = store.append_event(job.job_id, type="phase_started", phase="researching", message="Researching.")

    assert store.list_events(job.job_id, after_seq=-99, limit=10) == [first, second]
    assert store.list_events(job.job_id, after_seq=0, limit=0) == []
    assert store.list_events(job.job_id, after_seq=0, limit=-5) == []


def test_store_persists_checkpoints_and_artifacts(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research checkpoints",
        request_fingerprint="fp-checkpoints",
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

    checkpoint = store.save_checkpoint(
        job.job_id,
        phase="researching",
        checkpoint_key="round-1",
        state={"completed_units": 2},
    )
    artifact = store.upsert_artifact(
        job.job_id,
        kind="partial_report.md",
        path="jobs/job-1/partial_report.md",
        content_type="text/markdown",
        metadata={"bytes": 128},
    )

    checkpoints = store.list_checkpoints(job.job_id)
    artifacts = store.list_artifacts(job.job_id)

    assert isinstance(checkpoint, DeepResearchCheckpoint)
    assert isinstance(artifact, DeepResearchArtifact)
    assert checkpoints == [checkpoint]
    assert artifacts == [artifact]
    assert checkpoints[0].state == {"completed_units": 2}
    assert checkpoints[0].checkpoint_seq == 1
    assert artifacts[0].metadata == {"bytes": 128}


def test_store_orders_checkpoints_by_monotonic_sequence_when_timestamps_match(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research checkpoint ordering",
        request_fingerprint="fp-checkpoint-ordering",
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
    monkeypatch.setattr(deep_research_store_module, "utc_now_iso", lambda: "2026-04-17T00:00:00Z")

    first = store.save_checkpoint(job.job_id, phase="researching", checkpoint_key="researching-u1", state={"n": 1})
    second = store.save_checkpoint(job.job_id, phase="researching", checkpoint_key="researching-u2", state={"n": 2})
    checkpoints = store.list_checkpoints(job.job_id)

    assert [checkpoint.checkpoint_key for checkpoint in checkpoints] == ["researching-u1", "researching-u2"]
    assert [checkpoint.checkpoint_seq for checkpoint in checkpoints] == [1, 2]
    assert store.get_checkpoint(job.job_id, "researching-u2") == second
    assert first.checkpoint_seq == 1
    assert second.checkpoint_seq == 2


def test_store_retains_multiple_versions_for_same_checkpoint_key(monkeypatch, tmp_path):
    store = make_store(tmp_path)
    job = store.create_job(
        query="Research checkpoint history",
        request_fingerprint="fp-checkpoint-history",
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
    monkeypatch.setattr(deep_research_store_module, "utc_now_iso", lambda: "2026-04-17T00:00:00Z")

    first = store.save_checkpoint(job.job_id, phase="researching", checkpoint_key="researching-u1", state={"n": 1})
    second = store.save_checkpoint(job.job_id, phase="researching", checkpoint_key="researching-u1", state={"n": 2})
    checkpoints = store.list_checkpoints(job.job_id)

    assert [checkpoint.checkpoint_key for checkpoint in checkpoints] == ["researching-u1", "researching-u1"]
    assert [checkpoint.checkpoint_seq for checkpoint in checkpoints] == [1, 2]
    assert checkpoints[0].state == {"n": 1}
    assert checkpoints[1].state == {"n": 2}
    assert store.get_checkpoint(job.job_id, "researching-u1") == second
    assert first.checkpoint_seq == 1
    assert second.checkpoint_seq == 2


def test_store_reuses_active_or_recent_job_by_request_fingerprint(tmp_path):
    store = make_store(tmp_path)
    active = store.create_job(
        query="Research active reuse",
        request_fingerprint="fp-reuse",
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

    assert store.find_reusable_job("fp-reuse", recent_reuse_seconds=1800) == active

    store.update_job(
        active.job_id,
        status="completed",
        phase="finalizing",
        progress_pct=100.0,
        finished_at=utc_now_iso(),
    )
    completed = store.get_job(active.job_id)

    assert store.find_reusable_job("fp-reuse", recent_reuse_seconds=1800) == completed


def test_store_reuses_matching_draft_job(tmp_path):
    store = make_store(tmp_path)
    draft = store.create_job(
        query="Draft reuse",
        request_fingerprint="fp-draft-reuse",
        status="draft",
        phase="planning",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=True,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )

    assert store.find_reusable_job("fp-draft-reuse", recent_reuse_seconds=1800) == draft


def test_store_list_jobs_orders_stably_when_updated_at_ties(tmp_path):
    store = make_store(tmp_path)
    first = store.create_job(
        query="Stable list order first",
        request_fingerprint="fp-list-order-first",
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
    second = store.create_job(
        query="Stable list order second",
        request_fingerprint="fp-list-order-second",
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
    third = store.create_job(
        query="Stable list order third",
        request_fingerprint="fp-list-order-third",
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
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2026-04-22T00:00:00Z", "2026-04-22T00:00:01Z", first.job_id),
        )
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2026-04-22T00:00:00Z", "2026-04-22T00:00:03Z", second.job_id),
        )
        connection.execute(
            "UPDATE jobs SET updated_at = ?, created_at = ? WHERE job_id = ?",
            ("2026-04-22T00:00:00Z", "2026-04-22T00:00:02Z", third.job_id),
        )

    assert [job.job_id for job in store.list_jobs(status="completed", limit=10)] == [
        second.job_id,
        third.job_id,
        first.job_id,
    ]
    assert store.list_jobs(status="completed", limit=0) == []


def test_store_does_not_reuse_expired_completed_job(tmp_path):
    store = make_store(tmp_path)
    completed = store.create_job(
        query="Research expired reuse window",
        request_fingerprint="fp-expired-reuse",
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
    store.update_job(
        completed.job_id,
        status="completed",
        phase="finalizing",
        progress_pct=100.0,
        finished_at="2020-01-01T00:00:00Z",
    )

    assert store.find_reusable_job("fp-expired-reuse", recent_reuse_seconds=1800) is None


def test_store_marks_inflight_jobs_as_interrupted_during_recovery(tmp_path):
    store = make_store(tmp_path)
    running = store.create_job(
        query="Research inflight recovery",
        request_fingerprint="fp-recover-running",
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
    queued = store.create_job(
        query="Research queued recovery",
        request_fingerprint="fp-recover-queued",
        status="queued",
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
    draft = store.create_job(
        query="Research draft recovery",
        request_fingerprint="fp-recover-draft",
        status="draft",
        phase="planning",
        effort="standard",
        context="",
        include_domains=[],
        exclude_domains=[],
        plan_only=True,
        force_new=False,
        resolved_budget_seconds=240,
        continued_from_job_id="",
    )

    recovered = store.reconcile_incomplete_jobs()
    running_events = store.list_events(running.job_id)
    queued_events = store.list_events(queued.job_id)

    assert {job.job_id for job in recovered} == {running.job_id, queued.job_id}
    assert store.get_job(running.job_id).status == "interrupted"
    assert store.get_job(queued.job_id).status == "interrupted"
    assert store.get_job(running.job_id).last_error == "worker_restarted"
    assert store.get_job(queued.job_id).finished_at
    assert running_events[-1].type == "job_interrupted"
    assert queued_events[-1].data["reason"] == "worker_restarted"
    assert store.get_job(draft.job_id).status == "draft"


def test_store_reconcile_cancel_requested_jobs_as_canceled(tmp_path):
    store = make_store(tmp_path)
    running = store.create_job(
        query="Cancel requested recovery",
        request_fingerprint="fp-recover-cancel-requested",
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
    store.update_job(running.job_id, cancel_requested=True)

    recovered = store.reconcile_incomplete_jobs()
    events = store.list_events(running.job_id)

    assert [job.job_id for job in recovered] == [running.job_id]
    assert store.get_job(running.job_id).status == "canceled"
    assert store.get_job(running.job_id).last_error == ""
    assert events[-1].type == "job_canceled"
    assert events[-1].data["reason"] == "cancel_requested_during_recovery"


def test_store_reconcile_incomplete_jobs_skips_recent_heartbeats_when_threshold_applies(tmp_path):
    store = make_store(tmp_path)
    running = store.create_job(
        query="Fresh running job",
        request_fingerprint="fp-fresh-running",
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
    stale = store.create_job(
        query="Stale running job",
        request_fingerprint="fp-stale-running",
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
    store.update_job(running.job_id, heartbeat_at=utc_now_iso())
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ?, started_at = ?, created_at = ? WHERE job_id = ?",
            ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", stale.job_id),
        )

    recovered = store.reconcile_incomplete_jobs(stale_after_seconds=30)

    assert [job.job_id for job in recovered] == [stale.job_id]
    assert store.get_job(running.job_id).status == "running"
    assert store.get_job(stale.job_id).status == "interrupted"
