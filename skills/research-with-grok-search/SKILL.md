---
name: research-with-grok-search
description: Use when answering questions that need up-to-date web information, phased search planning, or explicit source verification through GrokSearch MCP tools.
---

# Research with GrokSearch

Use this skill when a task needs current web information and the GrokSearch MCP tools are available.

## Core workflow

### 1. Pick the lightest reliable path

Default to `plan_* -> web_search` when the task is not obviously trivial.
Use direct `web_search` only when the task is clearly single-hop and planning would add little value, such as:

- single-hop factual questions
- straightforward lookups
- quick current checks
- bounded comparisons that do not need decomposition

When source verification matters, follow `web_search` with `get_sources`.

Use the `plan_*` tools when the task is:

- ambiguous
- multi-step
- time-sensitive and high-risk
- likely to need decomposition before searching

Required order:

`plan_intent -> plan_complexity -> plan_sub_query -> plan_search_term -> plan_tool_mapping -> plan_execution`

Stop early based on complexity:

- level `1`: stop after `plan_sub_query`
- level `2`: stop after `plan_tool_mapping`
- level `3`: continue through `plan_execution`

Planning sessions are transient. If the server restarts, the session expires, or the cache evicts it, restart from `plan_intent`.

### 2. Route tools deliberately

- `web_search`: find answers and candidate sources
- `get_sources`: inspect cached sources for a prior search
- `web_fetch`: read the content of a specific page
- `web_map`: discover pages in a site section
- `deep_research_*`: start, inspect, resume, cancel, and retrieve advanced report-style research jobs when the task is multi-minute, open-ended, or needs resumable artifacts

For object-first callers, prefer `response_format=object` on `web_map`, `switch_model`, and `toggle_builtin_tools`. Their legacy default is a JSON string, while object mode returns a stable `{ok, error, message, data}` envelope.

`web_search` control fields such as `topic`, `time_range`, `include_domains`, and `exclude_domains` depend on the supplemental provider path for full effect. If Tavily-backed supplemental search is unavailable, read the returned `warnings` before assuming those controls were honored.

`web_fetch` and `web_map` fail closed on unsafe or unreadable targets before provider dispatch. Treat URL validation, redirect-preflight, loopback/private-network, and timeout/request preflight failures as boundary decisions, not ordinary empty search results.

Use `deep_research_*` instead of overloading the lightweight path when the task needs:

- multi-phase progress
- resumable or cancelable execution
- partial reports or exported artifacts
- a report-oriented result rather than a single direct answer

For deep research control flows:

- prefer `grok-search-research` for long-running watch loops, `status` / `events` inspection, targeted artifact reads, and continuation-heavy local workflows
- use CLI `events --follow` for a live event stream and `events --after-seq` when resuming event reads from a known sequence boundary
- use `deep_research_resume` when resuming the same job from its latest checkpoint boundary
- use `deep_research_start(...continue_from_job_id=...)` or CLI `continue` when opening a new follow-up job that should consume previous artifacts and findings
- remember that `deep_research_start` may return `reused=true` unless you explicitly force a brand-new job
- treat `sources.json` / `citations.json` as the canonical source registry surfaces; prefer the enriched source metadata and claim provenance fields over raw source counts when judging evidence quality

### 3. Run preflight when behavior looks degraded

Use `get_config_info` before deeper debugging or when installation/provider readiness is uncertain. Check `doctor`, `feature_readiness`, `GROK_MODEL_SOURCE`, and routing diagnostics before changing prompts.

- if `GROK_MODEL_SOURCE` is `process_env`, `project_env_local`, or `project_env`, `switch_model` only writes persisted config and will not change the running process
- `toggle_builtin_tools` is Claude-specific and should not be treated as a universal MCP feature
- a `web_search` result with `status=partial` requires reading `warnings` and usually `get_sources`; do not treat `sources_count` alone as evidence quality
- `get_sources` handles are in-process cache IDs, not durable links or secrets

### 4. Verify sources before making strong claims

- prefer at least two independent sources for key facts
- call out uncertainty when source quality is weak
- distinguish “no result” from “provider or compatibility failure”

### 5. Answer cleanly

- summarize the finding
- state the limits
- include source-backed claims
- do not present source count alone as evidence quality

## When not to use this skill

Do not use this skill for:

- purely local file tasks
- creative writing without research
- timeless explanations that do not need web evidence

## References

- For planning rules, read `references/planning-contract.md`
- For tool selection examples, read `references/tool-routing.md`
