# Compatibility Notes

## Support Levels

Compatibility claims are grouped into three levels:

- `Officially tested`: validated end-to-end in a clean environment
- `Community-tested`: supported by host documentation and maintainer usage, but not yet documented as a full official validation matrix
- `Planned`: a future target, not a current install promise

## Host Summary

### Officially tested

- Claude Code
  - full core MCP tool surface
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

- `GROK_API_URL` must be OpenAI-compatible and should include `/v1`
- model resolution order is process `GROK_MODEL` env -> project `.env.local` -> project `.env` -> persisted `~/.config/grok-search/config.json` value -> code default `grok-4.1-fast`
- process env presence overrides project `.env.local` / `.env` fallback, even when the env value is explicitly empty
- OpenRouter-compatible URLs automatically receive the `:online` suffix when needed
- `GROK_TIME_CONTEXT_MODE` controls local time-context injection for `web_search`; the default is `always`
- `web_search` depends on a working `/chat/completions` implementation
- `web_search.topic` currently supports `general`, `news`, and `finance`
- `web_search.time_range` currently supports `day`, `week`, `month`, `year`, and normalizes aliases `d`, `w`, `m`, `y`
- Tavily-backed supplemental search currently clamps `max_results` to the provider's documented limit of `20`
- `Config.get_config_info()` returns only the base config snapshot; the MCP tool `get_config_info` keeps that snapshot and adds `connection_test`, `doctor`, `feature_readiness`, and minimal real `search/fetch` probes
- `connection_test` reflects `/models` reachability only; use `doctor` and `feature_readiness` to judge runtime readiness
- `doctor.recommendations_detail` is an additive structured hint layer; clients that only read `recommendations` remain compatible
- `feature_readiness.web_fetch.providers.verified_path` identifies the backend that passed the real fetch probe, and skipped providers may include `skipped_reason`
- `get_config_info` is still not a full end-to-end compatibility guarantee
- `web_fetch`, `web_map`, and Tavily-backed supplemental `web_search` intentionally expose a curated subset of provider options rather than the providers' complete native API surfaces
- Tavily `map` may include external-domain URLs unless callers further constrain and post-filter the crawl results; this reflects Tavily's default `allow_external=true` behavior
- `web_fetch` / `web_map` now reject non-HTTP(S), loopback, and obviously private-network targets by default

## Feature Dependencies

| Feature | Required configuration |
| --- | --- |
| `plan_*` | none beyond a working MCP host |
| `web_search` | `GROK_API_URL`, `GROK_API_KEY` |
| `get_sources` | any previous `web_search` session ID; richer status fields are returned when the original search failed or yielded no sources |
| `web_fetch` | `FIRECRAWL_API_KEY`, or `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `web_map` | `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `toggle_builtin_tools` | Claude Code project layout |

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
- diagnostic payloads may still include local absolute paths, project-root hints, endpoint/hostname details, `request_id` values, or short upstream error summaries even when API keys and obvious token/signature strings are masked
- `toggle_builtin_tools` is intentionally client-specific and should not be treated as a universal MCP feature
- `toggle_builtin_tools` readiness in `get_config_info` currently indicates local Git project context detection, not a full Claude Code host validation
