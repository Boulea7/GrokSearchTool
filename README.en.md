English | [简体中文](README.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

# GrokSearch

GrokSearch is an independently maintained MCP server for assistants and clients that need fast, reliable, source-backed web context.

It combines `Grok` search with `Tavily` and `Firecrawl` extraction, then exposes a stable MCP tool surface for lightweight lookups, source verification, focused page fetching, a recommended `plan_* -> web_search` workflow for complex searches, and an advanced `deep research` layer for heavier exploration tasks. For clear, low-ambiguity single-hop lookups where planning adds little value, direct `web_search` is still acceptable.

The public package import contract currently has two boundaries: `grok_search.mcp` is an access-time lazy export, so `fastmcp` is only required when that export is actually accessed; `grok_search.providers.GrokSearchProvider` is also an access-time lazy export, so ordinary non-provider imports should not fail early just because Grok-provider dependencies are missing. This only narrows import-time behavior, does not change the install-time dependency declaration, and should not be read as turning package dependencies into optional extras.

Public `stdio` installation snippets currently use the maintained release repo `Boulea7/GrokSearchTool`. Local worktrees, historical remote names, or legacy collaboration traces should not be read as an active `fork/upstream` PR workflow.

## Overview

- `web_search`: AI-driven web search with cached sources
- `get_sources`: retrieve cached sources from `web_search`
- `web_fetch`: Tavily-first page extraction with Firecrawl fallback
- `web_map`: website structure mapping
- `plan_*`: phased planning tools for complex or ambiguous searches
- `deep_research_*`: asynchronous job tools for advanced report-style research
- `get_config_info`: inspect configuration and test `/models`
- `switch_model`: change the default Grok model
- `toggle_builtin_tools`: toggle Claude Code built-in WebSearch / WebFetch

The public MCP surface currently includes `21` tools:

- `web_search`
- `get_sources`
- `web_fetch`
- `web_map`
- `get_config_info`
- `switch_model`
- `toggle_builtin_tools`
- `plan_intent`
- `plan_complexity`
- `plan_sub_query`
- `plan_search_term`
- `plan_tool_mapping`
- `plan_execution`
- `deep_research_start`
- `deep_research_status`
- `deep_research_events`
- `deep_research_result`
- `deep_research_artifact`
- `deep_research_resume`
- `deep_research_cancel`
- `deep_research_list`

`plan_search_term` sets `approach` / `fallback_plan` when `search_strategy` is first created; later non-revision calls append `search_terms` only and do not implicitly rewrite existing strategy metadata.
planning `session_id` values are in-process transient handles with about a 1-hour TTL and a 256-session LRU cap, so restart / expiry / eviction requires starting again from a fresh `plan_intent`.
The wrappers intentionally keep scalar shim inputs such as CSV `depends_on`, semicolon-grouped `parallel_groups`, and stringified `params_json`; the first `plan_search_term` call must provide `approach`.
`plan_*` now returns structured objects directly rather than JSON strings, so callers should not wrap the result in an extra `json.loads(...)`.
`plan_sub_query.boundary` now enforces a minimum machine-checkable exclusion contract; vague “focus on X” wording without an explicit exclusion boundary is rejected.
`web_map`, `switch_model`, and `toggle_builtin_tools` now also support an additive `response_format` parameter. Set `response_format="object"` for a structured return value; this is the recommended `object` mode, while legacy JSON-string compatibility mode remains the default.
For new callers, prefer `response_format="object"` so you can consume the payload directly without an extra JSON decode step.

## Installation

### Requirements

- Python `3.10+`
- `uv`
- A client that supports stdio MCP, such as Claude Code, Codex CLI, or Cherry Studio

### Support levels

- `Officially tested`: Claude Code for the documented local `stdio` flow and project-level settings path, not as a full host-level E2E matrix
- `Community-tested`: Codex-style MCP clients, Cherry Studio
- `Planned`: Dify, n8n, Coze

Broader MCP host adaptation assets, including mappings for host-native rules, presets, memories, or skills, are collected in [docs/HOSTS.md](./docs/HOSTS.md). These assets help practical integration for tools such as Cursor, Cline, Continue, Windsurf, and Cherry Studio, but do not change the support-level claims above on their own.

Notes:

- Public installation guidance currently covers local `stdio` only.
- `toggle_builtin_tools` is specific to Claude Code project settings.
- `toggle_builtin_tools` readiness in `get_config_info` only means a local Git project context was detected; it is not a full Claude Code host verification.
- The installation snippets below intentionally use the current maintained public install source `Boulea7/GrokSearchTool`.

### Add as an MCP server

Replace the environment variables below with your own values:

```bash
claude mcp add-json grok-search --scope user '{
  "type": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "git+https://github.com/Boulea7/GrokSearchTool@main",
    "grok-search"
  ],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

If your environment requires system certificates, add `--native-tls` to `uvx`. This is a startup/install-layer TLS workaround for enterprise proxies or self-signed chains, not a generic runtime replacement for disabling certificate verification.

### Minimal `stdio` examples for other hosts

#### Codex CLI / Codex-style clients

Add the following snippet to `~/.codex/config.toml` or project-level `.codex/config.toml`:

```toml
[mcp_servers.grok-search]
command = "uvx"
args = ["--from", "git+https://github.com/Boulea7/GrokSearchTool@main", "grok-search"]

[mcp_servers.grok-search.env]
GROK_API_URL = "https://api.example.com/v1"
GROK_API_KEY = "your-grok-api-key"
TAVILY_API_KEY = "tvly-your-tavily-key"
TAVILY_API_URL = "https://api.tavily.com"
FIRECRAWL_API_KEY = "fc-your-firecrawl-key"
```

If you use a project-level `.codex/config.toml`, avoid committing real keys into the repository; this repo now ignores `.codex/` by default. For local development, prefer keeping secrets in an ignored `.env.local`.

`grok-search` automatically resolves configuration as `process env -> project .env.local -> project .env -> persisted config -> code defaults`, so you usually do not need to `source` `.env.local` as shell code. Project env fallback currently accepts both plain dotenv lines like `KEY=value` and optional `export KEY=value` prefixes; if you must export variables into the current shell, use an explicit shell-safe workflow instead of sourcing the file blindly.

If you plan to call `toggle_builtin_tools`, also avoid committing project-level `.claude/settings.json`; this repo now ignores `.claude/` by default.

#### Cherry Studio

Create a `STDIO` MCP server entry with the same core fields:

```json
{
  "name": "grok-search",
  "type": "stdio",
  "command": "uvx",
  "args": ["--from", "git+https://github.com/Boulea7/GrokSearchTool@main", "grok-search"],
  "env": {
    "GROK_API_URL": "https://api.example.com/v1",
    "GROK_API_KEY": "your-grok-api-key",
    "TAVILY_API_KEY": "tvly-your-tavily-key",
    "TAVILY_API_URL": "https://api.tavily.com",
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}
```

### Core environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `GROK_API_URL` | Yes | OpenAI-compatible Grok endpoint; using an explicit `/v1` suffix is recommended, the current code path does not pre-block omission on its own, but many OpenAI-compatible endpoints may still fail at runtime without it and usually surface a compatibility warning |
| `GROK_API_KEY` | Yes | Grok API key |
| `GROK_MODEL` | No | Default model; see the precedence notes below |
| `GROK_MODEL_PROFILE` | No | `balanced_auto` | When `GROK_MODEL` is not explicitly set, resolve a provider-aware default model for official xAI, OpenRouter, and common OpenAI-compatible relays / grok2api-like proxies |
| `GROK_DEEP_RESEARCH_STANDARD_PROFILE` | No | `reasoning` | Default deep research profile for `standard` effort; automatically downgrades when unavailable |
| `GROK_DEEP_RESEARCH_DEEP_PROFILE` | No | `multi_agent` | Default deep research profile for `deep` effort; automatically downgrades to single-agent when multi-agent is unavailable |
| `GROK_DEEP_RESEARCH_ULTRA_PROFILE` | No | `ultra` | Default deep research profile for `ultra` effort; when explicitly requested it prefers `grok-4.20-heavy-16-agent` and automatically downgrades to lighter multi-agent or single-agent models when needed |
| `GROK_API_URL_2` / `GROK_API_KEY_2` / `GROK_MODEL_2` | No | The second Grok provider; real requests automatically fail over to it when the primary provider fails |
| `GROK_API_URL_3+` / `GROK_API_KEY_3+` / `GROK_MODEL_3+` | No | Additional Grok providers, tried in numeric order as the fallback chain |
| `GROK_MODEL_FALLBACKS` | No | Built-in downgrade chain | A comma-separated model fallback order used when a provider explicitly reports that the requested model is unavailable; this can explicitly include `grok-4.1-fast` |
| `GROK_TIME_CONTEXT_MODE` | No | Time-context injection mode: `always`, `auto`, or `never` |
| `TAVILY_API_KEY` | No | Tavily key for `web_fetch` / `web_map`, and for Tavily-backed supplemental `web_search` |
| `TAVILY_API_URL` | No | Tavily endpoint |
| `TAVILY_ENABLED` | No | Enable or disable Tavily-backed fetch/map paths |
| `TAVILY_FALLBACK_API_URL` | No | Remote HTTP API fallback used when the primary Tavily URL is a local loopback endpoint and is unavailable; must be explicitly configured |
| `TAVILY_FALLBACK_API_KEY` | No | Tavily fallback Bearer token; defaults to `TAVILY_API_KEY` and must not be committed |
| `TAVILY_FALLBACK_ENABLED` | No | Enable or disable the local-loopback Tavily fallback path; defaults to disabled |
| `FIRECRAWL_API_KEY` | No | Firecrawl key for fetch fallback and optional supplemental `web_search` |
| `FIRECRAWL_API_URL` | No | Firecrawl endpoint |
| `GROK_DEBUG` | No | Enable debug logging |
| `GROK_LOG_LEVEL` | No | Log level |
| `GROK_LOG_DIR` | No | Log directory; `get_config_info` returns the resolved runtime path |
| `GROK_OUTPUT_CLEANUP` | No | Enable answer cleanup |
| `GROK_FILTER_THINK_TAGS` | No | Legacy alias for `GROK_OUTPUT_CLEANUP`; prefer `GROK_OUTPUT_CLEANUP` |
| `GROK_RETRY_MAX_ATTEMPTS` | No | Max retry attempts |
| `GROK_RETRY_MULTIPLIER` | No | Retry backoff multiplier |
| `GROK_RETRY_MAX_WAIT` | No | Max retry wait |
| `GROK_DEEP_RESEARCH_DIR` | No | Root directory for deep research SQLite state and artifacts |
| `GROK_DEEP_RESEARCH_DEFAULT_BUDGET_SECONDS` | No | Default target budget for deep research jobs |
| `GROK_DEEP_RESEARCH_HARD_TIMEOUT_SECONDS` | No | Hard upper timeout for a deep research job |
| `GROK_DEEP_RESEARCH_MAX_CONCURRENCY` | No | Max concurrently executed ready research units in the default runtime |
| `GROK_DEEP_RESEARCH_RECENT_REUSE_SECONDS` | No | Reuse window for completed deep research jobs, evaluated against `finished_at` |

Notes:

- model resolution order is process `GROK_MODEL` env -> project `.env.local` -> project `.env` -> persisted `~/.config/grok-search/config.json` value from `switch_model` -> code default `grok-4.20-0309`
- process env presence wins over project `.env.local` / `.env`, even when the env value is explicitly empty
- when `GROK_API_URL_2` / `GROK_API_KEY_2` and higher-numbered siblings are configured, runtime Grok requests treat them as an ordered provider chain and automatically fail over to the next provider when the current one fails
- when `GROK_MODEL_FALLBACKS` is configured, runtime requests use that explicit model downgrade order on the same provider after a model-unavailable error; otherwise the built-in chain still includes options such as `grok-4.1-fast`
- the base `get_config_info` snapshot now includes `GROK_MODEL_SOURCE`, which tells you which layer currently supplies the active model (`process_env`, `project_env_local`, `project_env`, `persisted_config`, or `default`)
- the preferred built-in default is now `grok-4.20-0309`; runtime selection stays flexible for Grok 4.1+ models and can fall back to a compatible available Grok model instead of failing just because a suffix differs
- when no explicit `GROK_MODEL` is present, runtime can now derive a provider-aware default from `GROK_MODEL_PROFILE`; the base config snapshot also includes additive `GROK_MODEL_PROFILE`, `GROK_DEEP_RESEARCH_STANDARD_PROFILE`, `GROK_DEEP_RESEARCH_DEEP_PROFILE`, `GROK_DEEP_RESEARCH_ULTRA_PROFILE`, and `GROK_PROVIDER_FAMILY`
- the base config snapshot now also includes `GROK_ROUTING_DIAGNOSTICS`, which summarizes the active provider, numbered provider chain, profile-derived default models, the expected `/chat/completions` vs `/responses` path, and multi-agent routing signals for official xAI, OpenRouter, generic relays, and grok2api-like proxies
- Grok routing now supports both `/chat/completions` and `/responses`; multi-agent families and response-only relay models prefer `/responses`, while OpenRouter and most relays remain primarily `chat/completions`
- deep research now defaults to single-agent for `standard`, multi-agent-first for `deep`, and explicitly opt-in heavy multi-agent-first for `ultra`; `ultra` prefers `grok-4.20-heavy-16-agent` and automatically downgrades to lighter multi-agent or single-agent models when the current provider, account, relay, or single-model setup cannot serve that tier
- OpenRouter-compatible URLs automatically receive the `:online` suffix when needed
- `GROK_TIME_CONTEXT_MODE` defaults to `always`, which preserves the current behavior of always injecting local time context
- `GROK_DEBUG=false` suppresses these helper progress logs entirely, including `ctx.info()` forwarding; they are intentionally debug-only progress/debug signals
- when redirect preflight hits a timeout or request-level error, `web_fetch` / `web_map` now fail closed before downstream provider dispatch; `skipped_due_to_error` remains an internal diagnostic reason code rather than a continue-execution path
- the recommended core path is `plan_* -> web_search`
- direct `web_search` is still allowed for clear single-hop lookups when planning adds little value
- advanced `deep research` is now exposed as a non-interactive MCP job surface plus a richer CLI workflow
- interactive `deep research` workflows remain CLI-first rather than as conversational MCP/skill interactions
- `web_fetch` still works with Firecrawl only.
- `web_map` requires Tavily and `TAVILY_ENABLED=true`.
- `web_search` injects local time context according to `GROK_TIME_CONTEXT_MODE` (`always` by default)
- loopback upstream endpoints are requested with `trust_env=False`, which also bypasses `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` and `SSL_CERT_FILE` / `SSL_CERT_DIR` for that request
- `web_fetch` and `web_map` reject non-HTTP(S), loopback, obviously private-network targets, single-label hosts, common private suffixes such as `.internal` / `.local` / `.lan` / `.home` / `.corp`, common loopback helper domains such as `localtest.me` / `lvh.me`, and common public DNS aliases that encode local/private IPs
- after the static URL check passes, `web_fetch` and `web_map` also re-check visible redirect targets before dispatching the provider call
- visible redirect re-checks currently use `GET` rather than `HEAD`, so presigned URLs, one-shot tokens, or read-side-effect links may incur an extra preflight read
- redirect preflight currently makes at most 5 visible preflight requests; if the fifth preflight still encounters a new redirect, it returns the current hard-reject contract (`目标 URL 重定向次数过多`) before any downstream provider call
- if redirect preflight times out or hits a request-level error, the current implementation now hard-stops the request before downstream provider dispatch; `skipped_due_to_error` remains available as an internal diagnostic reason code only
- this boundary intentionally does not hard-block ordinary public-looking hostnames based only on local DNS answers, so it should not be treated as a strong guarantee against split-horizon or locally poisoned DNS resolution
- `get_config_info` now combines the base config snapshot with doctor checks, readiness summaries, and minimal real `search/fetch` probes, but it is still not a full end-to-end compatibility guarantee.
- `web_fetch`, `web_map`, and Tavily-backed supplemental `web_search` expose a curated subset of provider options rather than the providers' full native API surfaces.
- `web_fetch` returns extracted Markdown text, not the provider's full structured raw response payload.
- Tavily `web_map` may include external-domain URLs unless you further narrow the crawl and post-filter results; this follows Tavily's default `allow_external=true` behavior.

### Minimal smoke check

For any local `stdio` host, start with this lightweight verification flow:

1. Call `get_config_info` and confirm the base config snapshot, `connection_test`, `doctor`, and `feature_readiness` match your install target; optional `search/fetch` probes may be skipped when their providers are not configured
2. Run one `web_search`
3. Use `get_sources` if source verification matters
4. Validate `web_fetch` only when Tavily or Firecrawl is configured, and validate `web_map` only when Tavily is configured and enabled

### `get_config_info` doctor output

`Config.get_config_info()` only returns the base config snapshot. The MCP tool `get_config_info` keeps that snapshot and also adds:

- optional `detail="full" | "summary"` output levels; `full` remains the default and preserves the current payload shape
- `doctor`: overall doctor status, structured checks, and repair recommendations
- `feature_readiness`: readiness summaries for `web_search`, `get_sources`, `web_fetch`, `web_map`, `toggle_builtin_tools`, `deep_research_planner`, and `deep_research_runtime`
- `doctor.recommendations_detail`: additive structured repair hints linked to `check_id` and feature scope
- `feature_readiness.web_fetch.providers`: provider-level readiness details with stable `check_id`; `verified_path` shows which real fetch probe succeeded, and degraded or skipped providers include `reason_code` when it can be derived and may also include `skipped_reason`
- `grok_provider_chain`: a structured summary of the currently resolved Grok provider count, provider names, and each provider's family / resolved model / expected endpoint path
- `GROK_ROUTING_DIAGNOSTICS` in the base snapshot: active provider details, provider-chain routing hints, profile-derived defaults, endpoint-path visibility, and multi-agent routing signals
- `GROK_MODEL_SOURCE` in the base snapshot: the active model source, so callers can tell whether runtime behavior comes from process env, project env files, persisted config, or code defaults
- minimal real `web_search` / `web_fetch` probe results

Optional provider probes are read-only and run only when the corresponding configuration is already present.
The `/models` connection test uses a 10-second timeout; additional real `web_search` / `web_fetch` probes may take longer.
`detail="summary"` keeps the base config snapshot, `connection_test`, `doctor.status` / `doctor.summary` / `doctor.recommendations`, and `feature_readiness`, while omitting the large `doctor.checks` array and probe-detail fields.
`detail="summary"` is currently a compact projection of the same diagnostic run, not a separate lightweight execution path.
`connection_test` only reflects `/models` reachability; if `web_search` is degraded, combine `doctor`, `feature_readiness`, `GROK_MODEL_SOURCE`, and the `grok_model_selection` / `grok_model_runtime_fallback` / `grok_search_probe` checks before concluding the root cause.
`grok_model_selection` means the configured model was already unsuitable at the `/models` visibility stage, while `grok_model_runtime_fallback` means the real `/chat/completions` path only succeeded after a runtime retry against another Grok candidate; both checks may appear in the same diagnostic run.
`grok_search_probe` may now return a body-quality `warning` as well as `ok` or `error`; for example, a sources-only probe or a probably truncated probe body degrades `feature_readiness.web_search` even though the endpoint itself still responded successfully.
Successful `grok_search_probe` results now also report the actual `provider_name` / `provider_model` that satisfied the probe, and the reported endpoint path now follows that winner model's routing rules, so diagnostics can distinguish primary success, numbered-provider failover, and multi-agent `/responses` routing.
`feature_readiness.get_sources` only reports `ready` when the current process already holds at least one readable non-error source session; error-only cached sessions keep it at `partial_ready`. Even if `web_search` is currently not ready, `get_sources` can still report `ready` when the running process still holds a readable session, while surfacing the upstream config problem through `degraded_by`.
`feature_readiness.get_sources` now also includes an additive `cache_summary` with `total_sessions`, `readable_sessions`, `error_sessions`, `partial_sessions`, and `unreadable_sessions`.
`feature_readiness` now also carries summary-safe machine fields: `based_on_checks`, `probe_scope`, and `degraded_by`. For `web_search`, it additionally returns `runtime_override_active` and `runtime_model_source` so callers can tell when a higher-priority runtime override is still in effect.
`feature_readiness.deep_research_planner` and `feature_readiness.deep_research_runtime` now also expose `profile_probes`, which report the real `standard` / `deep` / `ultra` deep research probes, the winning provider/model, and the endpoint path that actually served the request.
`ready` means the capability is verified, `degraded` means it exists but probes or partial dependencies are unhealthy, `not_ready` means prerequisites are missing, and `partial_ready` means the interface exists but still depends on transient runtime state; `transient` and `client_specific` items do not lower the overall doctor status on their own.

If `GROK_MODEL_SOURCE` comes back as `process_env`, `project_env_local`, or `project_env`, calling `switch_model` alone does not change the current process; update or remove that higher-priority override first.
In that override case, `switch_model` still updates the persisted config, but the returned `current_model` remains the current runtime-effective model. Use `runtime_model_source` to see which higher-priority layer is still active.

Even with API keys masked, the diagnostic payload may still include local absolute paths, endpoint/hostname details, and short upstream error summaries. Sensitive query tokens, bearer values, common OAuth/OIDC credential parameters, and high-confidence cloud-signed credential keys such as `X-Amz-Credential`, `X-Goog-Credential`, and `GoogleAccessId` are masked, but you should still review the payload before sharing it externally.

### `web_search` response contract

`web_search` keeps the legacy `session_id`, `content`, and `sources_count` fields, and also returns:

- `status`: `ok`, `partial`, or `error`
- `effective_params`: the final normalized search controls
- `warnings`: non-fatal warnings, especially when Tavily-only filters cannot be applied, or when the upstream returns sources without a usable body (`body_missing_sources_only`) or a body that looks truncated (`body_probably_truncated`)
- `error`: a stable machine-readable error code, or `null`

Optional additive controls:

- `topic`: `general`, `news`, or `finance`
- `time_range`: `day`, `week`, `month`, or `year` (aliases `d`, `w`, `m`, `y` are normalized)
- `include_domains`: Tavily allowlist for supplemental search
- `exclude_domains`: Tavily denylist for supplemental search

If supplemental search goes through Tavily, `max_results` is currently clamped to the provider's documented limit of `20`.
These controls currently apply to Tavily-backed supplemental search only; if Tavily is unavailable or not selected for the supplemental path, the request may still succeed with warnings and the controls will not be fully enforced.
When the upstream returns only source links without a usable body, or when the answer matches the current truncation heuristics, `web_search` also returns `partial`. `get_sources.search_status` keeps that downgraded status and now also replays the cached `search_warnings` codes for the same session.

Successful `get_sources` responses include `session_id`, `sources`, and `sources_count`, where each source is standardized with metadata such as `provider`, `domain`, `score`, `retrieved_at`, and `rank`, and may add provenance fields such as `origin_type` and `contributors` when available. They also return:

- `search_status`
- `search_error`
- `search_warnings`
- `source_state`
- `error` when the `session_id` is missing or expired

`get_sources` currently reads from an in-process memory-backed LRU cache on the running server. Session IDs are shared-daemon transient handles rather than durable, caller-bound capabilities or secret tokens, and `session_id_not_found_or_expired` covers restart, TTL expiry, eviction, and unreadable legacy-cache misses.
Legacy cache entries that predate this contract simply return `search_warnings=[]`.

`sources_count` is the final post-standardization, post-dedupe source count written into the cache, not the upstream raw citation count.
After dedupe, each source row should be treated as a lossy aggregate display row: `provider` reflects the winner provider for that row, while `source` / `origin_type` may still come from another contributing row. Additive `contributors` is only exposed when a distinct contributor identity survives inside the same aggregated row and contributor-level attribution is still useful.
`source` is still a legacy-overloaded field: when `origin_type` is absent it may still be reused as an old provider alias, and only becomes closer to a provenance label when provenance signals survive the upstream path.
`rank` currently follows `score`, source identity quality, and stable dedupe order without giving Grok-origin citations extra priority.
`standardize_sources` canonicalizes scheme/host casing for dedupe, so mixed-case variants of the same page may collapse into one source; it still preserves ordinary URL fragments, removes URL userinfo, and masks common signature/token parameters plus common OAuth/OIDC credential keys such as `client_secret`, `refresh_token`, `id_token`, and `password`. High-confidence cloud-signed credential keys such as `X-Amz-Credential`, `X-Goog-Credential`, and `GoogleAccessId` are also masked. Explicit default ports such as `:443` and `:80` are still preserved and are not collapsed into implicit-default URLs.

## Companion Skill

This repository also ships a companion skill: [`skills/research-with-grok-search`](skills/research-with-grok-search/SKILL.md)

Use it when you want a structured workflow for:

- up-to-date web research
- phased planning before searching
- source verification after `web_search`
- choosing between `web_search`, `get_sources`, `web_fetch`, and `web_map`
- routing heavier, multi-minute report jobs into `deep_research_*` instead of overloading the lightweight path

### Install the skill

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/GrokSearch/skills/research-with-grok-search ~/.codex/skills/research-with-grok-search
```

## Development

### Run locally

```bash
PYTHONPATH=src uv run python -m grok_search.server
```

### Deep Research CLI

Use `grok-search-research` when you want richer local interaction around the advanced deep research job layer.

The current deep research runtime now centers on a structured `plan.json` with `brief`, `sub_questions`, `search_strategy`, `report_outline`, and `research_units`. In continuation mode, the `continuation` object inside `plan.json` now stays compact, while the full carry-forward state is written separately to `continuation.json`. `resume` continues the same job from its latest completed checkpoint boundary, while `continue` opens a new follow-up job that consumes the previous job's artifacts and findings.

The `force_new` flag controls whether `deep_research_start` must create a brand-new job.
When `force_new=false`, `deep_research_start` may reuse a matching in-flight job or a recently completed job and will surface that via the `reused` field. That reuse rule now also applies to follow-up jobs keyed by `continue_from_job_id`. Set `force_new=true` when you require a brand-new job. Completed jobs are only reusable when the readable final provenance bundle is consistent: `sources.json`, `citations.json`, `report.json`, `final_report.md`, plus the currently bundled provenance sidecars (`evidence_items.json`, `coverage.json`, `grounding.json`, `verifier.json`) must agree on the resolved `batch_id`. Completed final artifacts are published with a shared `batch_id`, `deep_research_result.citations` now uses the same structure as `citations.json`, and unreadable JSON artifacts are surfaced through `artifact_errors` instead of failing the whole result read. For `completed` jobs, missing final artifacts now surface through `artifact_errors`; for `failed`, `canceled`, and `interrupted` jobs that already reached `finalizing`, a readable resolved final batch may still be surfaced before falling back to checkpoints or partial artifacts. `deep_research_status` may also surface additive attempt-window/operator fields such as `watch_attach_after_seq`, `attempt_window_start_seq`, `partial_payload`, and the mirrored values inside `operator_summary`.

`sources.json` now carries additive source-quality metadata such as `source_key`, `quality_score`, `quality_tier`, `source_type`, and `ranking_reasons` alongside `source_id`. Source rows may also expose additive `winner_provider`, `citation_count`, and `section_count` fields. `report.json.runtime.provider_winners` now exposes each winning unit's `provider_name`, `provider_model`, `effective_model`, `provider_api_url`, `source_count`, and `evidence_count`; `provider_attempts` / `provider_capabilities` expose the search/fetch/map provider paths that were used or failed. Supplemental search attempts use `attempt_role=supplemental`, search/map internal selective fetch attempts use `attempt_role=selective_fetch`, and failed attempts may include additive `failure_reason` and `warnings`. `report.json.runtime.budget` exposes the deep research budget, concurrency summary, and local call/evidence counts; selective fetch is counted in `budget.usage.fetch_calls`. Provider Accounting may also appear as an additive final-report section in both `report.json` and `citations.json`, describing search/fetch/map call counts, provider attempts, failed attempts, warnings, and budget usage in report prose. Sections inside `citations.json` and `report.json` may also include additive `summary`, `prose`, `confidence`, `claim_cluster_count`, `supporting_source_count`, and `supporting_domain_count` fields, and claims may include additive `unit_id`, `evidence_ids`, `cluster_type`, `supporting_source_count`, `supporting_domain_count`, and `confidence` provenance/quality fields. `status` / `result` may also surface additive operator diagnostics such as `artifact_fallback_used`, `resolved_artifact_batch_id`, `artifact_visibility_reason`, `watch_attach_after_seq`, `attempt_window_start_seq`, and `partial_payload`; those attempt-window and partial-availability fields are also mirrored inside `operator_summary`. Continuation rebuilds now prefer `sources.json`, but can fall back to `citations.json.source_registry` when the sources artifact is missing or unreadable. When the current artifact pointers for a completed job are mixed or incomplete, continuation first falls back to the latest complete final artifact batch before falling back again to checkpoints or partial artifacts. Jobs recovered from in-flight `queued` or `running` state are now reconciled into `interrupted` with an explicit recovery reason. `resume` now starts a new attempt time window instead of replaying the previous terminal timestamps.

When reading a single artifact through the CLI, `grok-search-research result --artifact <kind>` now follows the same resolved-final-batch preference as `deep_research_result`, instead of blindly reading the current artifact pointer for completed jobs. MCP callers can use `deep_research_artifact(job_id, artifact)` for the same single-artifact visibility behavior, including `state` and `artifact_visibility_reason`.

```bash
grok-search-research start "Compare open-source deep research frameworks" --watch
grok-search-research list
grok-search-research result JOB_ID --artifact final_report.md
grok-search-research continue JOB_ID "Focus on resume and checkpoint trade-offs" --watch
```

### Verification

```bash
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

## Project Docs

- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Compatibility](docs/COMPATIBILITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE)
