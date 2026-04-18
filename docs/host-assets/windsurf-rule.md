# Windsurf Rule / Memory Template

Suggested Windsurf rule or memory content:

```markdown
Use the `grok-search` MCP server as the primary web context layer.

- Preferred path: `plan_* -> web_search`
- Use `get_sources` when verification matters
- Use `web_fetch` for focused extraction
- Use `web_map` for discovery
- Use `deep_research_*` only for advanced non-interactive report jobs
- Keep interactive deep research in `grok-search-research`

Keep the default workflow lightweight and source-backed.
```
