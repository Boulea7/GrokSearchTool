English | [简体中文](README.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

# GrokSearch

GrokSearch is an independently maintained MCP server for assistants and clients that need fast, reliable, source-backed web context.

It combines `Grok` search with `Tavily` and `Firecrawl` extraction, then exposes a stable MCP tool surface for lightweight lookups, source verification, focused page fetching, a recommended `plan_* -> web_search` workflow for complex searches, and a future `deep research` direction for heavier exploration tasks. For clear, low-ambiguity single-hop lookups where planning adds little value, direct `web_search` is still acceptable.

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

## Installation

### Requirements

- Python `3.10+`
- `uv`
- A client that supports stdio MCP, such as Claude Code, Codex CLI, or Cherry Studio

### Support levels

- `Officially tested`: Claude Code
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

If you use a project-level `.codex/config.toml`, avoid committing real keys into the repository; this repo now ignores `.codex/` by default. For local development, prefer keeping secrets in an ignored `.env.local` and loading them before running commands:

```bash
set -a
source ./.env.local
set +a
```

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
| `GROK_API_URL` | Yes | OpenAI-compatible Grok endpoint, ideally with `/v1` |
| `GROK_API_KEY` | Yes | Grok API key |
| `GROK_MODEL` | No | Default model |
| `GROK_TIME_CONTEXT_MODE` | No | Time-context injection mode: `always`, `auto`, or `never` |
| `TAVILY_API_KEY` | No | Tavily key for `web_fetch` / `web_map` |
| `TAVILY_API_URL` | No | Tavily endpoint |
| `TAVILY_ENABLED` | No | Enable or disable Tavily-backed fetch/map paths |
| `FIRECRAWL_API_KEY` | No | Firecrawl fallback key |
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

- model resolution order is `GROK_MODEL` env -> persisted `~/.config/grok-search/config.json` value from `switch_model` -> code default `grok-4.1-fast`
- OpenRouter-compatible URLs automatically receive the `:online` suffix when needed
- `GROK_TIME_CONTEXT_MODE` defaults to `always`, which preserves the current behavior of always injecting local time context
- the recommended core path is `plan_* -> web_search`
- direct `web_search` is still allowed for clear single-hop lookups when planning adds little value
- interactive `deep research` workflows are planned CLI-first rather than as conversational MCP/skill interactions
- `web_fetch` still works with Firecrawl only.
- `web_map` requires Tavily and `TAVILY_ENABLED=true`.
- `web_search` injects local time context according to `GROK_TIME_CONTEXT_MODE` (`always` by default)
- `get_config_info` now combines the base config snapshot with doctor checks, readiness summaries, and minimal real `search/fetch` probes, but it is still not a full end-to-end compatibility guarantee.
- `web_fetch`, `web_map`, and Tavily-backed supplemental `web_search` expose a curated subset of provider options rather than the providers' full native API surfaces.
- `web_fetch` returns extracted Markdown text, not the provider's full structured raw response payload.
- Tavily `web_map` may include external-domain URLs unless you further narrow the crawl and post-filter results.

### Minimal smoke check

For any local `stdio` host, start with this lightweight verification flow:

1. Call `get_config_info` and confirm the base config snapshot, `connection_test`, `doctor`, and `feature_readiness` match your install target; optional `search/fetch` probes may be skipped when their providers are not configured
2. Run one `web_search`
3. Use `get_sources` if source verification matters
4. Validate `web_fetch` only when Tavily or Firecrawl is configured, and validate `web_map` only when Tavily is configured and enabled

### `get_config_info` doctor output

`Config.get_config_info()` only returns the base config snapshot. The MCP tool `get_config_info` keeps that snapshot and also adds:

- `doctor`: overall doctor status, structured checks, and repair recommendations
- `feature_readiness`: readiness summaries for `web_search`, `get_sources`, `web_fetch`, `web_map`, and `toggle_builtin_tools`
- `doctor.recommendations_detail`: additive structured repair hints linked to `check_id` and feature scope
- `feature_readiness.web_fetch.providers`: provider-level readiness details; `verified_path` shows which real fetch probe succeeded, and skipped providers may include `skipped_reason`
- minimal real `web_search` / `web_fetch` probe results

Optional provider probes are read-only and run only when the corresponding configuration is already present.
The `/models` connection test uses a 10-second timeout; additional real `web_search` / `web_fetch` probes may take longer.

Even with API keys masked, the diagnostic payload may still include local absolute paths, project-root hints, `request_id` values, or upstream error summaries. Review before sharing it externally.

### `web_search` response contract

`web_search` keeps the legacy `session_id`, `content`, and `sources_count` fields, and also returns:

- `status`: `ok`, `partial`, or `error`
- `effective_params`: the final normalized search controls
- `warnings`: non-fatal warnings, especially when Tavily-only filters cannot be applied
- `error`: a stable machine-readable error code, or `null`

Optional additive controls:

- `topic`: `general` or `news`
- `time_range`: `day`, `week`, `month`, or `year`
- `include_domains`: Tavily allowlist for supplemental search
- `exclude_domains`: Tavily denylist for supplemental search

`get_sources` returns standardized metadata including `provider`, `domain`, `score`, `retrieved_at`, and `rank`.

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
