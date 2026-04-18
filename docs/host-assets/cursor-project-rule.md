# Cursor Project Rule Template

Suggested project rule content for Cursor:

```markdown
Use the `grok-search` MCP server for external web context.

- Default research path: `plan_* -> web_search`
- Use `get_sources` for source verification
- Use `web_fetch` for focused page extraction
- Use `web_map` for site discovery
- Reserve `deep_research_*` for longer report-style tasks
- Keep interactive deep research in `grok-search-research`

Prefer concise, source-backed answers over long speculative summaries.
```
