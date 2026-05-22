# GrokSearch Companion Guidance

Use GrokSearch as a lightweight MCP + companion layer:

- Prefer `plan_* -> web_search` for most non-trivial questions when planning tools are enabled; otherwise use `web_search` directly.
- For clear single-hop lookups, direct `web_search` is acceptable.
- Call `get_sources` when the answer needs source verification or structured citations.
- Use `web_fetch` for focused page extraction and `web_map` for site structure.
- Reserve `deep_research_*` for longer report-style workflows when deep research tools are enabled.
- Keep interactive deep research in the CLI with `grok-search-research`.

When a host supports its own rules, memories, or skills, adapt this guidance into the host-native surface and keep credentials out of version-controlled configuration.
