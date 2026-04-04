# Planning Contract

Use these rules when invoking the `plan_*` tools.

## Required order

`plan_intent -> plan_complexity -> plan_sub_query -> plan_search_term -> plan_tool_mapping -> plan_execution`

## Session handling

- `plan_intent` creates the planning session
- keep the returned `session_id`
- reuse the same `session_id` for every later planning call

## Complexity stop points

- level `1`: planning completes after `plan_sub_query`
- level `2`: planning completes after `plan_tool_mapping`
- level `3`: planning completes after `plan_execution`

## Important validation rules

- the first `plan_search_term` call must include `approach`
- valid `approach` values: `broad_first`, `narrow_first`, `targeted`
- valid tool values: `web_search`, `web_fetch`, `web_map`

## Use planning sparingly

Do not use the planning chain for trivial lookups when direct `web_search` is enough.
