import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, Field


DeepResearchStatus = Literal[
    "draft",
    "queued",
    "running",
    "completed",
    "failed",
    "canceled",
    "interrupted",
]

DeepResearchPhase = Literal[
    "planning",
    "researching",
    "synthesizing",
    "finalizing",
]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DeepResearchJob(BaseModel):
    job_id: str
    request_fingerprint: str
    query: str
    context: str = ""
    status: DeepResearchStatus
    phase: DeepResearchPhase
    effort: str
    progress_pct: float = 0.0
    plan_only: bool = False
    force_new: bool = False
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    continued_from_job_id: str = ""
    resolved_budget_seconds: int
    attempt_count: int = 0
    last_error: str = ""
    cancel_requested: bool = False
    current_checkpoint: str = ""
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    heartbeat_at: str = ""


class DeepResearchEvent(BaseModel):
    job_id: str
    seq: int
    timestamp: str
    type: str
    phase: DeepResearchPhase
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class DeepResearchCheckpoint(BaseModel):
    job_id: str
    checkpoint_key: str
    phase: DeepResearchPhase
    created_at: str
    state: dict[str, Any] = Field(default_factory=dict)


class DeepResearchArtifact(BaseModel):
    job_id: str
    kind: str
    path: str
    content_type: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
