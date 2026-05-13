# Tool Routing

## Choose the right tool

Default routing rule:

- start with `plan_* -> web_search` unless the task is clearly single-hop
- use direct `web_search` only when planning would add little value

### `web_search`

Use for:

- clear single-hop lookups where `plan_*` would add little value
- current facts
- finding candidate pages
- quick comparison questions

`topic`, `time_range`, `include_domains`, and `exclude_domains` are best-effort controls tied to the supplemental search path. If the result has warnings about unsupported or degraded supplemental search, do not assume those controls fully constrained the answer.

### `get_sources`

Use after `web_search` when:

- source quality matters
- you need the exact URLs
- you need to compare multiple returned sources
- `web_search.status` is `partial` or includes warnings

### `web_fetch`

Use for:

- reading one page in full
- inspecting documentation pages
- verifying the content behind a known URL

If target validation or redirect preflight fails, treat the failure as a safety/compatibility boundary. Do not retry by weakening URL restrictions.

### `web_map`

Use for:

- listing pages under a docs section
- discovering likely URLs before fetching
- narrowing a large site before detailed reading

Use `response_format=object` when the host can consume structured objects. The legacy default is a JSON string.

As with `web_fetch`, URL and redirect preflight failures are hard stops before provider dispatch.

### Configuration and diagnostics

Use `get_config_info` when:

- a provider seems unavailable or degraded
- model routing looks wrong
- installation smoke checks need proof
- `switch_model` appears not to affect the current process

Use `switch_model` only for persisted model preference. If `GROK_MODEL_SOURCE` reports an env or project-file override, change that override first.

Use `response_format=object` with `switch_model` and `toggle_builtin_tools` when possible; object mode returns `{ok, error, message, data}`. Use `toggle_builtin_tools` only for Claude Code project-level routing. It is not a generic MCP host feature.

### Deep research MCP vs CLI

| Need | Prefer |
| --- | --- |
| start or inspect a non-interactive job from a host | `deep_research_start`, `deep_research_status`, `deep_research_events`, `deep_research_result` |
| inspect one job locally without a watch loop | `grok-search-research status <job_id>` |
| read events once or resume from a sequence | `grok-search-research events <job_id>` with `--after-seq` |
| follow live events | `grok-search-research events <job_id> --follow` |
| read one artifact with final-batch visibility rules | `deep_research_artifact` or `grok-search-research result --artifact <kind>` |
| watch a long job locally | `grok-search-research watch` |
| open a follow-up job from previous artifacts | `grok-search-research continue` or `deep_research_start(...continue_from_job_id=...)` |
| resume the same job from a checkpoint | `deep_research_resume` |
| list or cancel jobs | `deep_research_list`, `deep_research_cancel`, or matching CLI commands |

## Simple examples

- “What changed in X this week?” -> usually `plan_*`, then `web_search`, then `get_sources`
- “What is the latest FastAPI version?” -> direct `web_search`, then `get_sources` when source verification matters
- “Read the page at URL Y” -> `web_fetch`
- “Find the right docs page under this domain” -> `web_map`, then `web_fetch`
- “Research a complex topic with multiple sub-questions” -> `plan_*`, then execute the planned searches
- “Need a cited answer with light decomposition” -> `plan_*`, then `web_search`, then `get_sources`
- “Why is search degraded?” -> `get_config_info`, then inspect `doctor`, `feature_readiness`, and `GROK_MODEL_SOURCE`
