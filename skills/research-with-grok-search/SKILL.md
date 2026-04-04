---
name: research-with-grok-search
description: Use when answering questions that need up-to-date web information, phased search planning, or explicit source verification through GrokSearch MCP tools.
---

# Research with GrokSearch

Use this skill when a task needs current web information and the GrokSearch MCP tools are available.

## Core workflow

### 1. Start simple

Use `web_search` directly for:

- single-hop factual questions
- straightforward lookups
- quick current checks

When source verification matters, follow `web_search` with `get_sources`.

### 2. Use planning only when it materially helps

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

### 3. Route tools deliberately

- `web_search`: find answers and candidate sources
- `get_sources`: inspect cached sources for a prior search
- `web_fetch`: read the content of a specific page
- `web_map`: discover pages in a site section

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
