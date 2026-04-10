# Compatibility Notes

## Support Levels

Compatibility claims are grouped into three levels:

- `Officially tested`: maintainer-validated for the documented local `stdio` path in a clean environment, not a full host-level E2E matrix
- `Community-tested`: supported by host documentation and maintainer usage, but not yet documented as a full official validation matrix
- `Planned`: a future target, not a current install promise

## Host Summary

### Officially tested

- Claude Code
  - documented local `stdio` MCP flow is maintainer-validated
  - `toggle_builtin_tools` is specifically designed for Claude Code project settings

### Community-tested

- Codex-style MCP clients
  - `plan_*`, `web_search`, `get_sources`, `web_fetch`, and `web_map` work when the client supports stdio MCP and regular tool descriptions
  - Claude-specific tool toggling is not relevant

- Cherry Studio
  - core planning, search, and fetch flows are supported
  - behavior still depends on upstream model endpoint compatibility

### Planned

- Dify
- n8n
- Coze

These hosts remain planned targets until remote transport and host-specific verification are documented responsibly.

## Transport Scope

- Public documentation currently prioritizes local `stdio`
- Public `stdio` install snippets currently use the maintained release repo `Boulea7/GrokSearchTool`
- Remote `HTTP` / `Streamable HTTP` remains a later compatibility track
- Long-running `deep research` workflows should not be treated as the default MCP install story
- The recommended core interaction path remains `plan_* -> web_search`
- Clear single-hop lookups may still call `web_search` directly when planning would add little value
- Any interactive `deep research` experience should remain CLI-first; MCP and skill integrations should stay non-interactive

## Provider Requirements

- `GROK_API_URL` should use an OpenAI-compatible root with an explicit `/v1` suffix; the current code path does not pre-block the request on its own when `/v1` is omitted, but many OpenAI-compatible endpoints may still fail at runtime without it and usually surface a compatibility warning
- model resolution order is process `GROK_MODEL` env -> project `.env.local` -> project `.env` -> persisted `~/.config/grok-search/config.json` value -> code default `grok-4.1-fast`
- process env presence overrides project `.env.local` / `.env` fallback, even when the env value is explicitly empty
- the base config snapshot now includes `GROK_MODEL_SOURCE`, so callers can see the active model source (`process_env`, `project_env_local`, `project_env`, `persisted_config`, or `default`)
- project env fallback accepts both `KEY=value` and optional `export KEY=value` lines
- OpenRouter-compatible URLs automatically receive the `:online` suffix when needed
- `GROK_TIME_CONTEXT_MODE` controls local time-context injection for `web_search`; the default is `always`
- `web_search` depends on a working `/chat/completions` implementation
- `TAVILY_API_KEY` is used by `web_fetch`, `web_map`, and Tavily-backed supplemental `web_search`
- `FIRECRAWL_API_KEY` is used by fetch fallback and optional supplemental `web_search`
- `web_search.topic` currently supports `general`, `news`, and `finance`
- `web_search.time_range` currently supports `day`, `week`, `month`, `year`, and normalizes aliases `d`, `w`, `m`, `y`
- Tavily-backed supplemental search currently clamps `max_results` to Tavily's documented upper bound of `20`
- `Config.get_config_info()` returns only the base config snapshot; the MCP tool `get_config_info` keeps that snapshot and adds `connection_test`, `doctor`, `feature_readiness`, and minimal real `search/fetch` probes
- `get_config_info` now also supports additive `detail=full|summary` output levels; `full` remains the default and preserves the current payload shape
- `detail=summary` is currently a compact projection of the same diagnostic run, not a separate lightweight execution path
- `connection_test` reflects `/models` reachability only; use `doctor` and `feature_readiness` to judge runtime readiness
- when diagnosing degraded `web_search`, treat `GROK_MODEL_SOURCE` as part of the root-cause contract: a model mismatch caused by process env or project `.env.local` / `.env` overrides is different from a persisted-config mismatch
- `doctor.recommendations_detail` is an additive structured hint layer; clients that only read `recommendations` remain compatible
- `feature_readiness.web_fetch.providers.verified_path` identifies the backend that passed the real fetch probe, and skipped providers may include `skipped_reason`
- `get_config_info` is still not a full end-to-end compatibility guarantee
- `GROK_DEBUG=false` suppresses helper progress logs entirely, including `ctx.info()` forwarding; these signals are intentionally debug-only
- `grok_search.mcp` is an access-time lazy export; importing the root package does not require `fastmcp` until that export is actually accessed
- `grok_search.providers.GrokSearchProvider` is also an access-time lazy export; non-provider imports should not fail early because Grok-provider dependencies are missing
- this lazy-export boundary only narrows import-time behavior and does not change the install-time dependency declaration; it should not be read as turning package dependencies into optional extras
- `web_fetch`, `web_map`, and Tavily-backed supplemental `web_search` intentionally expose a curated subset of provider options rather than the providers' complete native API surfaces
- Tavily `map` may include external-domain URLs unless callers further constrain and post-filter the crawl results; this reflects Tavily's documented default `allow_external=true` behavior, and this wrapper does not currently expose that flag directly
- loopback upstream endpoints are requested with `trust_env=False`, which also bypasses proxy and local-CA environment variables for that request
- `web_fetch` / `web_map` now reject non-HTTP(S), loopback, obviously private-network targets, single-label hosts, common private suffixes such as `.internal` / `.local` / `.lan` / `.home` / `.corp`, common loopback helper domains such as `localtest.me` / `lvh.me`, and common public DNS aliases that encode local/private IPs
- after static URL validation passes, `web_fetch` / `web_map` also re-check visible redirect targets before dispatching the provider call
- visible redirect re-checks currently use `GET` rather than `HEAD`, so presigned URLs, one-shot tokens, or read-side-effect links may incur an extra preflight read
- redirect preflight currently makes at most 5 visible preflight requests; if the fifth preflight still encounters a new redirect, it returns the current hard-reject contract (`目标 URL 重定向次数过多`) before any downstream provider call
- redirect preflight timeouts and request-level failures are currently surfaced as `skipped_due_to_error`; `web_fetch` / `web_map` still continue to the downstream provider path in that case
- this boundary does not provide a strong guarantee against split-horizon or locally poisoned DNS that resolves a public-looking hostname to a private target

## Feature Dependencies

| Feature | Required configuration |
| --- | --- |
| `plan_*` | none beyond a working MCP host |
| `web_search` | `GROK_API_URL`, `GROK_API_KEY` |
| `get_sources` | any previous `web_search` session ID from the current running server process; source sessions are stored in an in-memory LRU cache, act as transient non-caller-bound handles, and can disappear after restart, TTL expiry, or eviction |
| `web_fetch` | `FIRECRAWL_API_KEY`, or `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `web_map` | `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `toggle_builtin_tools` | Claude Code project layout |

`get_sources` currently uses a process-local, shared-daemon, non-secret handle model: any holder of a valid `session_id` inside the same running server process can read the cached sources, and `session_id_not_found_or_expired` covers restart, TTL expiry, eviction, and unreadable legacy-cache miss cases.

`feature_readiness.get_sources` reports `ready` only when the running process already holds at least one readable non-error source session; failed-search-only cache entries keep it at `partial_ready`. This is a `transient` readiness signal and does not lower the overall doctor status by itself.

`get_sources.rank` currently follows `score`, source identity quality, and stable dedupe order without a Grok-specific boost. `standardize_sources` also canonicalizes scheme/host casing during dedupe, so mixed-case variants of the same page may collapse into one returned source.

Supplemental `web_search` controls such as `topic`, `time_range`, and domain filters currently apply to Tavily-backed supplemental search only. If Tavily is unavailable or not used for the supplemental path, the request may still run with warnings, but those controls will not be fully enforced.

If `GROK_MODEL_SOURCE` is `process_env`, `project_env_local`, or `project_env`, calling `switch_model` alone does not change the current process; callers must update or remove that higher-priority override first.

## Minimum `stdio` smoke check

For any locally configured `stdio` host:

1. call `get_config_info` and confirm the base config snapshot, `connection_test`, `doctor`, and `feature_readiness` match your install target
2. run one `web_search`
3. call `get_sources` when source verification matters
4. validate `web_fetch` only when Tavily or Firecrawl is configured, and validate `web_map` only when Tavily is configured and enabled

Keep remote `HTTP` / `Streamable HTTP` validation out of the default install story until that transport is explicitly verified and documented.

If local `stdio` startup fails with certificate-chain errors in enterprise or self-signed environments, prefer adding `--native-tls` to the `uvx` command line instead of documenting insecure TLS bypasses.

## Known Practical Limits

- endpoint compatibility still varies across Grok-compatible providers
- source extraction is best-effort and may depend on how the upstream response encodes links or annotations
- `get_sources` sessions are transient, in-process cache handles rather than durable, caller-bound capabilities
- diagnostic payloads may still include local absolute paths, endpoint/hostname details, or short upstream error summaries even when API keys, obvious token/signature strings, common OAuth/OIDC credential parameters, and high-confidence cloud-signed credential keys such as `X-Amz-Credential`, `X-Goog-Credential`, and `GoogleAccessId` are masked
- bare `auth` / `key` keys are intentionally not masked by default; the current redaction scope stays focused on high-confidence credential and signature parameters to avoid over-redacting ordinary diagnostics and source URLs
- `toggle_builtin_tools` is intentionally client-specific and should not be treated as a universal MCP feature
- `toggle_builtin_tools` readiness in `get_config_info` currently indicates local Git project context detection, not a full Claude Code host validation
