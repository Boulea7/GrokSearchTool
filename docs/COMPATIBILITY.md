# Compatibility Notes

## Client Scope

GrokSearch is designed around stdio MCP and works best with clients that can expose tool descriptions faithfully.

### First-class

- Claude Code
  - full MCP tool surface
  - `toggle_builtin_tools` is specifically designed for Claude Code project settings

### Supported with core search workflow

- Codex-style MCP clients
  - `web_search`, `get_sources`, `web_fetch`, `web_map`, and `plan_*` work when the client supports stdio MCP and regular tool descriptions
  - Claude-specific tool toggling is not relevant

- Cherry Studio
  - core search and fetch flows are supported
  - behavior still depends on upstream model endpoint compatibility

## Provider Requirements

- `GROK_API_URL` must be OpenAI-compatible and should include `/v1`
- `web_search` depends on a working `/chat/completions` implementation
- `get_config_info` currently checks `/models`, which is useful but not a full end-to-end compatibility guarantee

## Feature Dependencies

| Feature | Required configuration |
| --- | --- |
| `web_search` | `GROK_API_URL`, `GROK_API_KEY` |
| `get_sources` | a previous successful `web_search` call |
| `web_fetch` | `TAVILY_API_KEY` or `FIRECRAWL_API_KEY` |
| `web_map` | `TAVILY_API_KEY` |
| `toggle_builtin_tools` | Claude Code project layout |

## Known Practical Limits

- endpoint compatibility still varies across Grok-compatible providers
- source extraction is best-effort and may depend on how the upstream response encodes links or annotations
- `toggle_builtin_tools` is intentionally client-specific and should not be treated as a universal MCP feature
