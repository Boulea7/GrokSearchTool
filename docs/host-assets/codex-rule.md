# Codex Rule

Suggested global or project instruction for Codex:

```markdown
Use the `grok-search` MCP server whenever a task needs current web information, technical documentation lookup, source verification, product research, or cited evidence.

- Prefer `plan_* -> web_search` for most non-trivial questions when planning tools are enabled; otherwise use `web_search` directly.
- Use direct `web_search` for clear single-hop questions.
- Call `get_sources` before presenting source-backed conclusions.
- Use `web_fetch` for a known page and `web_map` for site discovery.
- Reserve `deep_research_*` for longer report-style workflows when deep research tools are enabled; use `grok-search-research` for interactive local monitoring.
```

Keep real API keys in local MCP configuration or ignored env files, not in project rules.
