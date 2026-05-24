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
DeepResearchSearchApproach = Literal["targeted", "breadth_first", "depth_first"]


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    checkpoint_seq: int = 0
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
    must_cover: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    coverage_checklist: list[str] = Field(default_factory=list)
    stop_policy: dict[str, Any] = Field(default_factory=dict)
    continuation_focus: list[str] = Field(default_factory=list)


class DeepResearchSubQuestion(BaseModel):
    id: str
    question: str
    reason: str


class DeepResearchSelectiveFetchConfig(BaseModel):
    max_urls_per_search: int = Field(default=1, ge=0, le=10)
    prefer_titles_matching_outline: bool = True


class DeepResearchSearchStrategy(BaseModel):
    approach: DeepResearchSearchApproach
    search_queries: list[str] = Field(default_factory=list)
    selective_fetch: DeepResearchSelectiveFetchConfig = Field(default_factory=DeepResearchSelectiveFetchConfig)


class DeepResearchReportSection(BaseModel):
    section_id: str
    title: str
    goal: str
    status: str = ""
    coverage_state: dict[str, Any] = Field(default_factory=dict)
    rewrite_reason: str = ""


class DeepResearchOutlineVersion(BaseModel):
    version_id: str
    parent_version_id: str = ""
    kind: str = "planned"
    sections: list[DeepResearchReportSection] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    created_at: str = ""


class DeepResearchContinuation(BaseModel):
    mode: Literal["fresh", "continue"] = "fresh"
    source_job_id: str = ""
    source_job_status: str = ""
    lineage_root_job_id: str = ""
    parent_job_id: str = ""
    continuation_identity: str = ""
    compaction_policy: str = ""
    compaction_reason_codes: list[str] = Field(default_factory=list)
    focused_snapshot: dict[str, Any] = Field(default_factory=dict)
    previous_summary: str = ""
    prior_plan_summary: str = ""
    continuation_goal: str = ""
    source_count: int = 0
    checkpoint_key: str = ""
    resume_from_checkpoint_key: str = ""
    replay_from_checkpoint_key: str = ""
    state_version: int = 2
    confirmed_claims: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    follow_up_hints: list[dict[str, Any]] = Field(default_factory=list)
    suggested_research_units: list[dict[str, Any]] = Field(default_factory=list)
    coverage_gap_scopes: dict[str, str] = Field(default_factory=dict)
    trusted_source_headers: list[str] = Field(default_factory=list)
    carry_forward_constraints: dict[str, Any] = Field(default_factory=dict)
    skipped_unit_ids: list[str] = Field(default_factory=list)


class DeepResearchContinuationState(DeepResearchContinuation):
    carry_forward_sources: list[dict[str, Any]] = Field(default_factory=list)
    carry_forward_outline_versions: list[dict[str, Any]] = Field(default_factory=list)
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
    line_start: int | None = None
    line_end: int | None = None


class DeepResearchClaim(BaseModel):
    claim_id: str
    text: str
    citations: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    unit_id: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_bindings: list[dict[str, Any]] = Field(default_factory=list)
    cluster_type: str = ""
    supporting_source_count: int = 0
    supporting_domain_count: int = 0
    confidence: str = ""


class DeepResearchSectionCitations(BaseModel):
    section_id: str
    title: str
    summary: str = ""
    prose: str = ""
    claims: list[DeepResearchClaim] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: str = ""
    claim_cluster_count: int = 0
    supporting_source_count: int = 0
    supporting_domain_count: int = 0
    pool_mode: str = ""
    question_ids: list[str] = Field(default_factory=list)


class DeepResearchSectionNode(BaseModel):
    section_id: str
    title: str
    goal: str
    parent_id: str = ""
    question_ids: list[str] = Field(default_factory=list)
    matched_unit_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    candidate_evidence_ids: list[str] = Field(default_factory=list)
    selected_evidence_ids: list[str] = Field(default_factory=list)
    rejected_evidence_ids: list[str] = Field(default_factory=list)
    status: str = "planned"
    coverage_state: dict[str, Any] = Field(default_factory=dict)
    rewrite_reason: str = ""
    last_updated_at: str = ""


class DeepResearchSectionGraphState(BaseModel):
    version: int = 1
    root_section_ids: list[str] = Field(default_factory=list)
    nodes: list[DeepResearchSectionNode] = Field(default_factory=list)


class DeepResearchEvidenceLedgerEntry(BaseModel):
    ledger_id: str
    evidence_id: str
    unit_id: str
    question_id: str = ""
    question_ids: list[str] = Field(default_factory=list)
    origin_query: str = ""
    candidate_section_ids: list[str] = Field(default_factory=list)
    selected_section_id: str = ""
    rejected_section_ids: list[str] = Field(default_factory=list)
    disposition: str = ""
    disposition_reason: str = ""
    source_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    summary: str = ""
    evidence_kind: str = ""
    derived_from_source_url: str = ""
    line_start: int | None = None
    line_end: int | None = None
    selection_score: int = 0
    selection_basis_tokens: list[str] = Field(default_factory=list)
    decision_state: str = ""
    section_decisions: list[dict[str, str]] = Field(default_factory=list)
    materialized_claim_ids: list[str] = Field(default_factory=list)
    recorded_at: str = ""


class DeepResearchSectionEvidenceBank(BaseModel):
    section_id: str
    candidate_evidence_ids: list[str] = Field(default_factory=list)
    selected_evidence_ids: list[str] = Field(default_factory=list)
    rejected_evidence_ids: list[str] = Field(default_factory=list)
    candidate_packets: list[dict[str, Any]] = Field(default_factory=list)
    selected_packets: list[dict[str, Any]] = Field(default_factory=list)
    rejected_packets: list[dict[str, Any]] = Field(default_factory=list)
    last_updated_at: str = ""


class DeepResearchPlan(BaseModel):
    query: str
    context: str = ""
    effort: str
    time_budget_seconds: int
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    brief: DeepResearchBrief
    sub_questions: list[DeepResearchSubQuestion] = Field(default_factory=list)
    search_strategy: DeepResearchSearchStrategy
    report_outline: list[DeepResearchReportSection] = Field(default_factory=list)
    outline_versions: list[DeepResearchOutlineVersion] = Field(default_factory=list)
    research_units: list[DeepResearchResearchUnit] = Field(default_factory=list)
    continuation: DeepResearchContinuation = Field(default_factory=DeepResearchContinuation)
    planner_metadata: dict[str, Any] = Field(default_factory=dict)


class DeepResearchCheckpointState(BaseModel):
    plan: DeepResearchPlan
    completed_unit_ids: list[str] = Field(default_factory=list)
    failed_unit_ids: list[str] = Field(default_factory=list)
    failed_units: list[dict[str, Any]] = Field(default_factory=list)
    skipped_unit_ids: list[str] = Field(default_factory=list)
    skipped_units: list[dict[str, Any]] = Field(default_factory=list)
    constraint_violations: list[dict[str, Any]] = Field(default_factory=list)
    coverage_state: dict[str, Any] = Field(default_factory=dict)
    unit_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    evidence_items: list[dict[str, Any]] = Field(default_factory=list)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    outline_versions: list[dict[str, Any]] = Field(default_factory=list)
    section_graph: dict[str, Any] = Field(default_factory=dict)
    evidence_ledger: list[dict[str, Any]] = Field(default_factory=list)
    section_banks: list[dict[str, Any]] = Field(default_factory=list)
