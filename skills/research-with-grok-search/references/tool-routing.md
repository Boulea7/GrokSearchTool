# Tool Routing

## Choose the right tool

### `web_search`

Use for:

- current facts
- finding candidate pages
- quick comparison questions

### `get_sources`

Use after `web_search` when:

- source quality matters
- you need the exact URLs
- you need to compare multiple returned sources

### `web_fetch`

Use for:

- reading one page in full
- inspecting documentation pages
- verifying the content behind a known URL

### `web_map`

Use for:

- listing pages under a docs section
- discovering likely URLs before fetching
- narrowing a large site before detailed reading

## Simple examples

- “What changed in X this week?” -> `web_search`, then `get_sources`
- “Read the page at URL Y” -> `web_fetch`
- “Find the right docs page under this domain” -> `web_map`, then `web_fetch`
- “Research a complex topic with multiple sub-questions” -> `plan_*`, then execute the planned searches
