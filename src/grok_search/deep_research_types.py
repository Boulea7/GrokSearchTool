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

DeepResearchUnitType = Literal["search", "fetch", "map"]
DeepResearchUnitStatus = Literal["pending", "running", "completed", "failed", "skipped"]


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


class DeepResearchBrief(BaseModel):
    objective: str
    deliverable: str
    success_criteria: list[str] = Field(default_factory=list)


class DeepResearchSubQuestion(BaseModel):
    id: str
    question: str
    reason: str


class DeepResearchSelectiveFetchConfig(BaseModel):
    max_urls_per_search: int = 1
    prefer_titles_matching_outline: bool = True


class DeepResearchSearchStrategy(BaseModel):
    approach: str
    search_queries: list[str] = Field(default_factory=list)
    selective_fetch: DeepResearchSelectiveFetchConfig = Field(default_factory=DeepResearchSelectiveFetchConfig)


class DeepResearchReportSection(BaseModel):
    section_id: str
    title: str
    goal: str


class DeepResearchContinuation(BaseModel):
    mode: Literal["fresh", "continue"] = "fresh"
    source_job_id: str = ""
    source_job_status: str = ""
    previous_summary: str = ""
    prior_plan_summary: str = ""
    continuation_goal: str = ""
    source_count: int = 0
    checkpoint_key: str = ""


class DeepResearchContinuationState(DeepResearchContinuation):
    carry_forward_sources: list[dict[str, Any]] = Field(default_factory=list)
    carry_forward_evidence: list[dict[str, Any]] = Field(default_factory=list)
    carry_forward_sections: list[dict[str, Any]] = Field(default_factory=list)
    carry_forward_unit_results: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DeepResearchResearchUnit(BaseModel):
    unit_id: str
    unit_type: DeepResearchUnitType
    title: str
    goal: str
    query: str = ""
    url: str = ""
    instructions: str = ""
    depends_on: list[str] = Field(default_factory=list)
    status: DeepResearchUnitStatus = "pending"
    notes: str = ""


class DeepResearchEvidenceItem(BaseModel):
    evidence_id: str
    unit_id: str
    source_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    summary: str
    detail: str = ""
    evidence_kind: str = "search"
    weight: float = 1.0
    derived_from_source_url: str = ""


class DeepResearchClaim(BaseModel):
    claim_id: str
    text: str
    citations: list[str] = Field(default_factory=list)
    unit_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    cluster_type: str = ""
    supporting_source_count: int = 0
    supporting_domain_count: int = 0
    confidence: str = ""


class DeepResearchSectionCitations(BaseModel):
    section_id: str
    title: str
    summary: str = ""
    claims: list[DeepResearchClaim] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    confidence: str = ""
    claim_cluster_count: int = 0
    supporting_source_count: int = 0
    supporting_domain_count: int = 0


class DeepResearchPlan(BaseModel):
    query: str
    context: str = ""
    effort: str
    time_budget_seconds: int
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    brief: DeepResearchBrief
    sub_questions: list[DeepResearchSubQuestion] = Field(default_factory=list)
    search_strategy: DeepResearchSearchStrategy
    report_outline: list[DeepResearchReportSection] = Field(default_factory=list)
    research_units: list[DeepResearchResearchUnit] = Field(default_factory=list)
    continuation: DeepResearchContinuation = Field(default_factory=DeepResearchContinuation)
    planner_metadata: dict[str, Any] = Field(default_factory=dict)


class DeepResearchCheckpointState(BaseModel):
    plan: DeepResearchPlan
    completed_unit_ids: list[str] = Field(default_factory=list)
    unit_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
