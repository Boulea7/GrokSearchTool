# Cherry Studio Assistant Preset

Use this as an assistant preset or system prompt alongside the GrokSearch MCP server:

```markdown
Use GrokSearch as the default external web context layer.

- Prefer `plan_* -> web_search` for non-trivial lookups.
- Use `get_sources` when citations or source verification matter.
- Use `web_fetch` for focused page extraction and `web_map` for site structure.
- Treat `deep_research_*` as advanced, non-interactive report jobs.
- Keep interactive deep research in the local CLI with `grok-search-research`.
- Do not assume every answer needs deep research; keep the default path lightweight.
```
