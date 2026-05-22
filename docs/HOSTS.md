# Host Integration Assets

This page collects broad host adaptation guidance for MCP-capable tools that can use GrokSearch as a local `stdio` MCP server and optionally consume a host-native prompt/rule/skill layer.

The goal is not to claim identical native feature parity across every host. The goal is to provide:

- one shared `stdio` MCP snippet
- one shared companion guidance baseline
- host-native adaptation notes for rules, assistant presets, or skills where the host supports them

## Core Principles

- Prefer local `stdio` MCP for all current broad-host guidance.
- Keep the recommended lightweight path as `plan_* -> web_search`, then call `get_sources` when source verification matters.
- Keep `deep_research_*` non-interactive inside MCP hosts. Rich interactive deep research should remain CLI-first.
- Treat host-native rules, presets, and skills as adaptation layers around the same MCP server, not as separate product surfaces.

## Shared MCP Snippet

- Use the shared snippet at [docs/host-assets/grok-search-stdio.json](./host-assets/grok-search-stdio.json)
- It is intentionally neutral and mirrors the maintained release repo `Boulea7/GrokSearchTool`.

## Host Matrix

| Host | MCP path | Native adaptation surface | Recommended asset | Notes |
| --- | --- | --- | --- | --- |
| Claude Code / Claude Desktop | local `stdio` | project/user settings, prompt instructions | existing README install snippets + [docs/host-assets/claude-code-rule.md](./host-assets/claude-code-rule.md) | `toggle_builtin_tools` remains Claude-specific |
| Codex CLI / Codex-style clients | local `stdio` | global or project instructions | README install snippets + [docs/host-assets/codex-rule.md](./host-assets/codex-rule.md) | keep secrets in local MCP env or ignored env files |
| Cherry Studio | local `STDIO` MCP server entry | assistant / preset prompt | [docs/host-assets/cherry-studio-assistant-preset.md](./host-assets/cherry-studio-assistant-preset.md) | best fit is MCP + assistant preset |
| Cursor | local MCP config | project rules | [docs/host-assets/cursor-project-rule.md](./host-assets/cursor-project-rule.md) | pair MCP with a project rule, not a copied agent file |
| Cline | local MCP config | Skills / custom instructions | [docs/host-assets/cline-skill.md](./host-assets/cline-skill.md) | use MCP for tools, skill for workflow guidance |
| Continue | local MCP config | rules / system prompt | [docs/host-assets/continue-rule.md](./host-assets/continue-rule.md) | keep tool use narrow and source-backed |
| Windsurf | local MCP config | rules / memories | [docs/host-assets/windsurf-rule.md](./host-assets/windsurf-rule.md) | use MCP + rule/memory pairing |

## Recommended Companion Guidance

All host-native adaptation assets keep the same baseline:

1. Use `plan_* -> web_search` for most non-trivial lookups.
2. Use `get_sources` whenever the answer needs source verification.
3. Use `web_fetch` for focused page extraction and `web_map` for site structure.
4. Reserve `deep_research_*` for longer report-style tasks.
5. Keep interactive deep research orchestration in `grok-search-research`, not inside conversational MCP loops.

## Scope Boundary

- This page does not upgrade any host into `Officially tested` on its own.
- It does not promise that every host can consume Codex/Claude-style `SKILL.md` files natively.
- Where a host does not have a native skill surface, the adaptation target is a rule, preset, or assistant prompt.
- For host-specific installation UX and auth/storage details, defer to the host's own current documentation.
