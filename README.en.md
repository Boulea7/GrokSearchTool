English | [简体中文](README.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md) | [Русский](README.ru.md)

# GrokSearch

GrokSearch is an independently maintained MCP server for assistants and clients that need reliable web research workflows.

It combines `Grok` search with `Tavily` and `Firecrawl` extraction, then exposes a stable MCP tool surface for both simple lookups and multi-step research tasks.

## Overview

- `web_search`: AI-driven web search with cached sources
- `get_sources`: retrieve cached sources from `web_search`
- `web_fetch`: Tavily-first page extraction with Firecrawl fallback
- `web_map`: website structure mapping
- `plan_*`: phased planning tools for complex research
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
    "FIRECRAWL_API_KEY": "fc-your-firecrawl-key"
  }
}'
```

If your environment requires system certificates, add `--native-tls` to `uvx`.

### Core environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `GROK_API_URL` | Yes | OpenAI-compatible Grok endpoint, ideally with `/v1` |
| `GROK_API_KEY` | Yes | Grok API key |
| `GROK_MODEL` | No | Default model |
| `TAVILY_API_KEY` | No | Tavily key for `web_fetch` / `web_map` |
| `TAVILY_API_URL` | No | Tavily endpoint |
| `FIRECRAWL_API_KEY` | No | Firecrawl fallback key |
| `FIRECRAWL_API_URL` | No | Firecrawl endpoint |
| `GROK_DEBUG` | No | Enable debug logging |
| `GROK_LOG_LEVEL` | No | Log level |
| `GROK_OUTPUT_CLEANUP` | No | Enable answer cleanup |
| `GROK_RETRY_MAX_ATTEMPTS` | No | Max retry attempts |
| `GROK_RETRY_MULTIPLIER` | No | Retry backoff multiplier |
| `GROK_RETRY_MAX_WAIT` | No | Max retry wait |

Notes:

- `web_fetch` still works with Firecrawl only.
- `web_map` requires Tavily.
- `web_search` always injects local time context into the search prompt.
- `get_config_info` currently validates `/models` only; it is not a full search-compatibility doctor.

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
