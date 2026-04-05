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
- Remote `HTTP` / `Streamable HTTP` remains a later compatibility track
- Long-running `deep research` workflows should not be treated as the default MCP install story
- The recommended core interaction path remains `plan_* -> web_search`
- Any interactive `deep research` experience should remain CLI-first; MCP and skill integrations should stay non-interactive

## Provider Requirements

- `GROK_API_URL` must be OpenAI-compatible and should include `/v1`
- `web_search` depends on a working `/chat/completions` implementation
- `get_config_info` now provides a lightweight doctor view over `/models`, optional provider probes, and feature readiness, but it is still not a full end-to-end compatibility guarantee

## Feature Dependencies

| Feature | Required configuration |
| --- | --- |
| `plan_*` | none beyond a working MCP host |
| `web_search` | `GROK_API_URL`, `GROK_API_KEY` |
| `get_sources` | a previous successful `web_search` call |
| `web_fetch` | `FIRECRAWL_API_KEY`, or `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `web_map` | `TAVILY_API_KEY` with `TAVILY_ENABLED=true` |
| `toggle_builtin_tools` | Claude Code project layout |

## Known Practical Limits

- endpoint compatibility still varies across Grok-compatible providers
- source extraction is best-effort and may depend on how the upstream response encodes links or annotations
- `toggle_builtin_tools` is intentionally client-specific and should not be treated as a universal MCP feature
