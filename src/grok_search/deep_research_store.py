import json
import secrets
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Any

from .deep_research_types import (
    DeepResearchArtifact,
    DeepResearchCheckpoint,
    DeepResearchEvent,
    DeepResearchJob,
    DeepResearchPhase,
    DeepResearchStatus,
    utc_now_iso,
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _parse_utc_iso(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _checkpoint_kind(checkpoint_key: str) -> str:
    normalized = (checkpoint_key or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("researching-dispatch-"):
        return "research_dispatch"
    if normalized.startswith("researching-"):
        return "research_unit"
    if normalized == "planning":
        return "planning"
    if normalized == "synthesizing":
        return "synthesizing"
    if normalized == "finalizing":
        return "finalizing"
    return "unknown"


class DeepResearchStore:
    def __init__(self, root_dir: Path):
        self._root_dir = Path(root_dir)
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir = self._root_dir / "artifacts"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._root_dir / "deep_research.sqlite3"
        self._initialize()

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def artifacts_dir(self) -> Path:
        return self._artifacts_dir

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    query TEXT NOT NULL,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    progress_pct REAL NOT NULL,
                    plan_only INTEGER NOT NULL,
                    force_new INTEGER NOT NULL,
                    include_domains_json TEXT NOT NULL,
                    exclude_domains_json TEXT NOT NULL,
                    continued_from_job_id TEXT NOT NULL,
                    resolved_budget_seconds INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    current_checkpoint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint
                ON jobs(request_fingerprint, updated_at DESC);

                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, seq)
                );

                CREATE TABLE IF NOT EXISTS job_checkpoints (
                    job_id TEXT NOT NULL,
                    checkpoint_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, checkpoint_key)
                );

                CREATE TABLE IF NOT EXISTS job_artifacts (
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, kind)
                );
                """
            )

    def create_job(
        self,
        *,
        query: str,
        request_fingerprint: str,
        status: DeepResearchStatus,
        phase: DeepResearchPhase,
        effort: str,
        context: str,
        include_domains: list[str],
        exclude_domains: list[str],
        plan_only: bool,
        force_new: bool,
        resolved_budget_seconds: int,
        continued_from_job_id: str,
    ) -> DeepResearchJob:
        now = utc_now_iso()
        job = DeepResearchJob(
            job_id=secrets.token_hex(12),
            request_fingerprint=request_fingerprint,
            query=query,
            context=context,
            status=status,
            phase=phase,
            effort=effort,
            progress_pct=0.0,
            plan_only=plan_only,
            force_new=force_new,
            include_domains=list(include_domains),
            exclude_domains=list(exclude_domains),
            continued_from_job_id=continued_from_job_id,
            resolved_budget_seconds=resolved_budget_seconds,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_fingerprint, query, context, status, phase, effort,
                    progress_pct, plan_only, force_new, include_domains_json, exclude_domains_json,
                    continued_from_job_id, resolved_budget_seconds, attempt_count, last_error,
                    cancel_requested, current_checkpoint, created_at, updated_at, started_at,
                    finished_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.request_fingerprint,
                    job.query,
                    job.context,
                    job.status,
                    job.phase,
                    job.effort,
                    job.progress_pct,
                    int(job.plan_only),
                    int(job.force_new),
                    _json_dumps(job.include_domains),
                    _json_dumps(job.exclude_domains),
                    job.continued_from_job_id,
                    job.resolved_budget_seconds,
                    job.attempt_count,
                    job.last_error,
                    int(job.cancel_requested),
                    job.current_checkpoint,
                    job.created_at,
                    job.updated_at,
                    job.started_at,
                    job.finished_at,
                    job.heartbeat_at,
                ),
            )
        return job

    def get_job(self, job_id: str) -> DeepResearchJob:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown deep research job: {job_id}")
        return self._row_to_job(row)

    def update_job(self, job_id: str, **changes: Any) -> DeepResearchJob:
        if not changes:
            return self.get_job(job_id)

        field_map = {
            "include_domains": "include_domains_json",
            "exclude_domains": "exclude_domains_json",
            "plan_only": "plan_only",
            "force_new": "force_new",
            "cancel_requested": "cancel_requested",
        }
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in changes.items():
            column = field_map.get(key, key)
            if key in {"include_domains", "exclude_domains"}:
                value = _json_dumps(value)
            elif key in {"plan_only", "force_new", "cancel_requested"}:
                value = int(bool(value))
            assignments.append(f"{column} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(job_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?", values)
        return self.get_job(job_id)

    def append_event(
        self,
        job_id: str,
        *,
        type: str,
        phase: DeepResearchPhase,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> DeepResearchEvent:
        timestamp = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM job_events WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            seq = int(row["max_seq"]) + 1
            connection.execute(
                """
                INSERT INTO job_events (job_id, seq, timestamp, type, phase, message, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, seq, timestamp, type, phase, message, _json_dumps(data or {})),
            )
        event = DeepResearchEvent(
            job_id=job_id,
            seq=seq,
            timestamp=timestamp,
            type=type,
            phase=phase,
            message=message,
            data=data or {},
        )
        return event

    def list_events(self, job_id: str, *, after_seq: int = 0, limit: int = 100) -> list[DeepResearchEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (job_id, after_seq, limit),
            ).fetchall()
        return [
            DeepResearchEvent(
                job_id=row["job_id"],
                seq=row["seq"],
                timestamp=row["timestamp"],
                type=row["type"],
                phase=row["phase"],
                message=row["message"],
                data=_json_loads(row["data_json"]) or {},
            )
            for row in rows
        ]

    def list_jobs(self, *, status: str = "", limit: int = 50) -> list[DeepResearchJob]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_to_job(row) for row in rows]

    def save_checkpoint(
        self,
        job_id: str,
        *,
        phase: DeepResearchPhase,
        checkpoint_key: str,
        state: dict[str, Any],
    ) -> DeepResearchCheckpoint:
        created_at = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO job_checkpoints (job_id, checkpoint_key, phase, created_at, state_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, checkpoint_key, phase, created_at, _json_dumps(state)),
            )
        self.update_job(job_id, current_checkpoint=checkpoint_key)
        return DeepResearchCheckpoint(
            job_id=job_id,
            checkpoint_key=checkpoint_key,
            phase=phase,
            created_at=created_at,
            state=state,
        )

    def list_checkpoints(self, job_id: str) -> list[DeepResearchCheckpoint]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_checkpoints
                WHERE job_id = ?
                ORDER BY created_at ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            DeepResearchCheckpoint(
                job_id=row["job_id"],
                checkpoint_key=row["checkpoint_key"],
                phase=row["phase"],
                created_at=row["created_at"],
                state=_json_loads(row["state_json"]) or {},
            )
            for row in rows
        ]

    def upsert_artifact(
        self,
        job_id: str,
        *,
        kind: str,
        path: str,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeepResearchArtifact:
        existing = self._get_artifact(job_id, kind)
        now = utc_now_iso()
        created_at = existing.created_at if existing else now
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO job_artifacts (
                    job_id, kind, path, content_type, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, kind, path, content_type, created_at, now, _json_dumps(metadata or {})),
            )
        return DeepResearchArtifact(
            job_id=job_id,
            kind=kind,
            path=path,
            content_type=content_type,
            created_at=created_at,
            updated_at=now,
            metadata=metadata or {},
        )

    def upsert_artifact_batch(
        self,
        job_id: str,
        *,
        artifacts: list[dict[str, Any]],
    ) -> list[DeepResearchArtifact]:
        existing_by_kind = {artifact.kind: artifact for artifact in self.list_artifacts(job_id)}
        now = utc_now_iso()
        persisted: list[DeepResearchArtifact] = []
        with self._connect() as connection:
            for item in artifacts:
                kind = str(item["kind"])
                path = str(item["path"])
                content_type = str(item["content_type"])
                metadata = dict(item.get("metadata") or {})
                created_at = existing_by_kind[kind].created_at if kind in existing_by_kind else now
                connection.execute(
                    """
                    INSERT OR REPLACE INTO job_artifacts (
                        job_id, kind, path, content_type, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, kind, path, content_type, created_at, now, _json_dumps(metadata)),
                )
                persisted.append(
                    DeepResearchArtifact(
                        job_id=job_id,
                        kind=kind,
                        path=path,
                        content_type=content_type,
                        created_at=created_at,
                        updated_at=now,
                        metadata=metadata,
                    )
                )
        return persisted

    def list_artifacts(self, job_id: str) -> list[DeepResearchArtifact]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_artifacts
                WHERE job_id = ?
                ORDER BY kind ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            DeepResearchArtifact(
                job_id=row["job_id"],
                kind=row["kind"],
                path=row["path"],
                content_type=row["content_type"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                metadata=_json_loads(row["metadata_json"]) or {},
            )
            for row in rows
        ]

    def artifact_abspath(self, job_id: str, kind: str) -> Path:
        return self._artifacts_dir / job_id / kind

    def read_artifact_text(self, job_id: str, kind: str) -> str | None:
        artifact = self._get_artifact(job_id, kind)
        if artifact is None:
            return None
        path = self._root_dir / artifact.path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def find_reusable_job(self, request_fingerprint: str, *, recent_reuse_seconds: int) -> DeepResearchJob | None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE request_fingerprint = ?
                ORDER BY updated_at DESC
                """,
                (request_fingerprint,),
            ).fetchall()
        for row in rows:
            job = self._row_to_job(row)
            if job.status in {"draft", "queued", "running"}:
                return job
            if job.status == "interrupted" and (job.phase == "finalizing" or job.current_checkpoint == "finalizing"):
                return job
            if job.status == "completed" and recent_reuse_seconds > 0:
                finished_at = _parse_utc_iso(job.finished_at)
                if finished_at is None:
                    continue
                age_seconds = (dt.datetime.now(dt.UTC) - finished_at.astimezone(dt.UTC)).total_seconds()
                if age_seconds <= recent_reuse_seconds:
                    return job
        return None

    def reconcile_incomplete_jobs(self, *, stale_after_seconds: int = 0) -> list[DeepResearchJob]:
        interrupted_at = utc_now_iso()
        now = dt.datetime.now(dt.UTC)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC
                """
            ).fetchall()
        recovered: list[DeepResearchJob] = []
        for row in rows:
            if bool(row["cancel_requested"]):
                job = self.update_job(
                    row["job_id"],
                    status="canceled",
                    last_error="",
                    finished_at=interrupted_at,
                    heartbeat_at=interrupted_at,
                )
                self.append_event(
                    row["job_id"],
                    type="job_canceled",
                    phase=row["phase"],
                    message="Deep research canceled during worker recovery.",
                    data={
                        "reason": "cancel_requested_during_recovery",
                        "checkpoint_key": row["current_checkpoint"],
                        "checkpoint_kind": _checkpoint_kind(row["current_checkpoint"]),
                    },
                )
                recovered.append(job)
                continue
            if stale_after_seconds > 0:
                candidate_times = [
                    _parse_utc_iso(row["heartbeat_at"]),
                    _parse_utc_iso(row["updated_at"]),
                    _parse_utc_iso(row["started_at"]),
                    _parse_utc_iso(row["created_at"]),
                ]
                visible_times = [item.astimezone(dt.UTC) for item in candidate_times if item is not None]
                if visible_times:
                    newest = max(visible_times)
                    age_seconds = (now - newest).total_seconds()
                    if age_seconds < stale_after_seconds:
                        continue
            job = self.update_job(
                row["job_id"],
                status="interrupted",
                last_error="worker_restarted",
                finished_at=interrupted_at,
                heartbeat_at=interrupted_at,
            )
            self.append_event(
                row["job_id"],
                type="job_interrupted",
                phase=row["phase"],
                message="Deep research interrupted during worker recovery.",
                data={
                    "reason": "worker_restarted",
                    "checkpoint_key": row["current_checkpoint"],
                    "checkpoint_kind": _checkpoint_kind(row["current_checkpoint"]),
                },
            )
            recovered.append(job)
        return recovered

    def _get_artifact(self, job_id: str, kind: str) -> DeepResearchArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_artifacts WHERE job_id = ? AND kind = ?",
                (job_id, kind),
            ).fetchone()
        if row is None:
            return None
        return DeepResearchArtifact(
            job_id=row["job_id"],
            kind=row["kind"],
            path=row["path"],
            content_type=row["content_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=_json_loads(row["metadata_json"]) or {},
        )

    def _row_to_job(self, row: sqlite3.Row) -> DeepResearchJob:
        return DeepResearchJob(
            job_id=row["job_id"],
            request_fingerprint=row["request_fingerprint"],
            query=row["query"],
            context=row["context"],
            status=row["status"],
            phase=row["phase"],
            effort=row["effort"],
            progress_pct=row["progress_pct"],
            plan_only=bool(row["plan_only"]),
            force_new=bool(row["force_new"]),
            include_domains=_json_loads(row["include_domains_json"]) or [],
            exclude_domains=_json_loads(row["exclude_domains_json"]) or [],
            continued_from_job_id=row["continued_from_job_id"],
            resolved_budget_seconds=row["resolved_budget_seconds"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            cancel_requested=bool(row["cancel_requested"]),
            current_checkpoint=row["current_checkpoint"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            heartbeat_at=row["heartbeat_at"],
        )
