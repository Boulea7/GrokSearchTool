# Planning Contract

Use these rules when invoking the `plan_*` tools.

Default rule:

- for non-obviously-trivial research, start with `plan_*`
- for clear single-hop lookups, direct `web_search` is acceptable

## Required order

`plan_intent -> plan_complexity -> plan_sub_query -> plan_search_term -> plan_tool_mapping -> plan_execution`

## Session handling

- `plan_intent` creates the planning session
- keep the returned `session_id`
- reuse the same `session_id` for every later planning call
- planning `session_id` values are in-process transient handles; restart, TTL expiry, or LRU eviction requires a fresh `plan_intent`

## Complexity stop points

- level `1`: planning completes after `plan_sub_query`
- level `2`: planning completes after `plan_tool_mapping`
- level `3`: planning completes after `plan_execution`

## Important validation rules

- every `plan_*` call requires a short `thought`
- the first `plan_search_term` call must include `approach`
- valid `approach` values: `broad_first`, `narrow_first`, `targeted`
- valid tool values: `web_search`, `web_fetch`, `web_map`
- `plan_sub_query.boundary` must explicitly say what the sub-query excludes
- `plan_search_term` appends terms by default; use `is_revision=true` when replacing the existing strategy

## Wrapper input shapes

The MCP wrappers intentionally accept host-friendly scalar inputs:

- `plan_sub_query.depends_on`: comma-separated IDs
- `plan_tool_mapping.params_json`: JSON string for tool parameters
- `plan_execution.parallel_groups`: semicolon-separated groups, with comma-separated IDs inside each group

Structured aliases may also be available in newer hosts, but the scalar forms are the safest cross-host shape. The returned `executable_plan` is already structured; do not apply an extra JSON decode layer to `plan_*` results.

## When to skip planning

Do not use the planning chain for trivial or already-bounded single-hop lookups when direct `web_search` is enough.
