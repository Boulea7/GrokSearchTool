English | [简体中文](README.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

# GrokSearch

GrokSearch is an independently maintained MCP server for assistants and clients that need fast, reliable, source-backed web context.

It combines `Grok` search with `Tavily` and `Firecrawl` extraction, then exposes a stable MCP tool surface for lightweight lookups, source verification, focused page fetching, a recommended `plan_* -> web_search` workflow for complex searches, and a future `deep research` direction for heavier exploration tasks. For clear, low-ambiguity single-hop lookups where planning adds little value, direct `web_search` is still acceptable.

The public package import contract currently has two boundaries: `grok_search.mcp` is an access-time lazy export, so `fastmcp` is only required when that export is actually accessed; `grok_search.providers.GrokSearchProvider` is also an access-time lazy export, so ordinary non-provider imports should not fail early just because Grok-provider dependencies are missing. This only narrows import-time behavior, does not change the install-time dependency declaration, and should not be read as turning package dependencies into optional extras.

Public `stdio` installation snippets currently use the maintained release repo `Boulea7/GrokSearchTool`. Local worktrees, historical remote names, or legacy collaboration traces should not be read as an active `fork/upstream` PR workflow.

## Overview

- `web_search`: AI-driven web search with cached sources
- `get_sources`: retrieve cached sources from `web_search`
- `web_fetch`: Tavily-first page extraction with Firecrawl fallback
- `web_map`: website structure mapping
- `plan_*`: phased planning tools for complex or ambiguous searches
- `get_config_info`: inspect configuration and test `/models`
- `switch_model`: change the default Grok model
- `toggle_builtin_tools`: toggle Claude Code built-in WebSearch / WebFetch

The public MCP surface currently includes `13` tools:

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

`plan_search_term` sets `approach` / `fallback_plan` when `search_strategy` is first created; later non-revision calls append `search_terms` only and do not implicitly rewrite existing strategy metadata.
planning `session_id` values are in-process transient handles with about a 1-hour TTL and a 256-session LRU cap, so restart / expiry / eviction requires starting again from a fresh `plan_intent`.
The wrappers intentionally keep scalar shim inputs such as CSV `depends_on`, semicolon-grouped `parallel_groups`, and stringified `params_json`; the first `plan_search_term` call must provide `approach`.

## Installation

### Requirements

- Python `3.10+`
- `uv`
- A client that supports stdio MCP, such as Claude Code, Codex CLI, or Cherry Studio

### Support levels

- `Officially tested`: Claude Code for the documented local `stdio` flow and project-level settings path, not as a full host-level E2E matrix
- `Community-tested`: Codex-style MCP clients, Cherry Studio
- `Planned`: Dify, n8n, Coze

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
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
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
GROK_API_URL = "https://your-api-endpoint.com/v1"
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
    "GROK_API_URL": "https://your-api-endpoint.com/v1",
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
| `GROK_TIME_CONTEXT_MODE` | No | Time-context injection mode: `always`, `auto`, or `never` |
| `TAVILY_API_KEY` | No | Tavily key for `web_fetch` / `web_map`, and for Tavily-backed supplemental `web_search` |
| `TAVILY_API_URL` | No | Tavily endpoint |
| `TAVILY_ENABLED` | No | Enable or disable Tavily-backed fetch/map paths |
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

Notes:

- model resolution order is process `GROK_MODEL` env -> project `.env.local` -> project `.env` -> persisted `~/.config/grok-search/config.json` value from `switch_model` -> code default `grok-4.20-0309`
- process env presence wins over project `.env.local` / `.env`, even when the env value is explicitly empty
- the base `get_config_info` snapshot now includes `GROK_MODEL_SOURCE`, which tells you which layer currently supplies the active model (`process_env`, `project_env_local`, `project_env`, `persisted_config`, or `default`)
- the preferred built-in default is now `grok-4.20-0309`; runtime selection stays flexible for Grok 4.1+ models and can fall back to a compatible available Grok model instead of failing just because a suffix differs
- OpenRouter-compatible URLs automatically receive the `:online` suffix when needed
- `GROK_TIME_CONTEXT_MODE` defaults to `always`, which preserves the current behavior of always injecting local time context
- `GROK_DEBUG=false` suppresses these helper progress logs entirely, including `ctx.info()` forwarding; they are intentionally debug-only progress/debug signals
- when redirect preflight falls back to `skipped_due_to_error`, the implementation now emits a caller-visible warning through MCP context, but does not rewrite successful return payloads
- the recommended core path is `plan_* -> web_search`
- direct `web_search` is still allowed for clear single-hop lookups when planning adds little value
- interactive `deep research` workflows are planned CLI-first rather than as conversational MCP/skill interactions
- `web_fetch` still works with Firecrawl only.
- `web_map` requires Tavily and `TAVILY_ENABLED=true`.
- `web_search` injects local time context according to `GROK_TIME_CONTEXT_MODE` (`always` by default)
- loopback upstream endpoints are requested with `trust_env=False`, which also bypasses `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` and `SSL_CERT_FILE` / `SSL_CERT_DIR` for that request
- `web_fetch` and `web_map` reject non-HTTP(S), loopback, obviously private-network targets, single-label hosts, common private suffixes such as `.internal` / `.local` / `.lan` / `.home` / `.corp`, common loopback helper domains such as `localtest.me` / `lvh.me`, and common public DNS aliases that encode local/private IPs
- after the static URL check passes, `web_fetch` and `web_map` also re-check visible redirect targets before dispatching the provider call
- visible redirect re-checks currently use `GET` rather than `HEAD`, so presigned URLs, one-shot tokens, or read-side-effect links may incur an extra preflight read
- redirect preflight currently makes at most 5 visible preflight requests; if the fifth preflight still encounters a new redirect, it returns the current hard-reject contract (`目标 URL 重定向次数过多`) before any downstream provider call
- if redirect preflight times out or hits a request-level error, the current implementation marks that step as `skipped_due_to_error`; `web_fetch` / `web_map` currently still continue to the downstream provider call
- that `skipped_due_to_error` path also emits a caller-visible warning through MCP context, but does not rewrite successful return payloads
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
- `feature_readiness`: readiness summaries for `web_search`, `get_sources`, `web_fetch`, `web_map`, and `toggle_builtin_tools`
- `doctor.recommendations_detail`: additive structured repair hints linked to `check_id` and feature scope
- `feature_readiness.web_fetch.providers`: provider-level readiness details with stable `check_id`; `verified_path` shows which real fetch probe succeeded, and degraded or skipped providers include `reason_code` when it can be derived and may also include `skipped_reason`
- `GROK_MODEL_SOURCE` in the base snapshot: the active model source, so callers can tell whether runtime behavior comes from process env, project env files, persisted config, or code defaults
- minimal real `web_search` / `web_fetch` probe results

Optional provider probes are read-only and run only when the corresponding configuration is already present.
The `/models` connection test uses a 10-second timeout; additional real `web_search` / `web_fetch` probes may take longer.
`detail="summary"` keeps the base config snapshot, `connection_test`, `doctor.status` / `doctor.summary` / `doctor.recommendations`, and `feature_readiness`, while omitting the large `doctor.checks` array and probe-detail fields.
`detail="summary"` is currently a compact projection of the same diagnostic run, not a separate lightweight execution path.
`connection_test` only reflects `/models` reachability; if `web_search` is degraded, combine `doctor`, `feature_readiness`, `GROK_MODEL_SOURCE`, and the `grok_model_selection` / `grok_model_runtime_fallback` / `grok_search_probe` checks before concluding the root cause.
`grok_model_selection` means the configured model was already unsuitable at the `/models` visibility stage, while `grok_model_runtime_fallback` means the real `/chat/completions` path only succeeded after a runtime retry against another Grok candidate; both checks may appear in the same diagnostic run.
`grok_search_probe` may now return a body-quality `warning` as well as `ok` or `error`; for example, a sources-only probe or a probably truncated probe body degrades `feature_readiness.web_search` even though the endpoint itself still responded successfully.
`feature_readiness.get_sources` only reports `ready` when the current process already holds at least one readable non-error source session; error-only cached sessions keep it at `partial_ready`. Even if `web_search` is currently not ready, `get_sources` can still report `ready` when the running process still holds a readable session, while surfacing the upstream config problem through `degraded_by`.
`feature_readiness.get_sources` now also includes an additive `cache_summary` with `total_sessions`, `readable_sessions`, `error_sessions`, `partial_sessions`, and `unreadable_sessions`.
`feature_readiness` now also carries summary-safe machine fields: `based_on_checks`, `probe_scope`, and `degraded_by`. For `web_search`, it additionally returns `runtime_override_active` and `runtime_model_source` so callers can tell when a higher-priority runtime override is still in effect.
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
