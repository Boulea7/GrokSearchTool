# Roadmap

## Positioning

GrokSearch is being developed as a lightweight MCP plus companion skill for fast, high-frequency information injection during LLM tool use.

The core product focus is:

- return useful, current, source-backed results quickly
- help models gather broad and relevant context without wasting their own token budget
- stay practical across multiple MCP-capable hosts instead of over-optimizing for a single CLI workflow

Long-running `deep research` remains a separate advanced capability direction and should not complicate the default MCP experience.

## Now

- Strengthen the core `web_search`, `get_sources`, `web_fetch`, `web_map`, and `get_config_info` experience while keeping the default workflow fast and lightweight
- Improve source provenance, confidence cues, and result ordering so useful evidence is surfaced before low-value noise
- Expand compatibility and installation validation for important MCP hosts, with clear distinctions between officially tested, community-tested, and planned integrations
- Keep tightening diagnostics and release discipline around configuration checks, compatibility smoke tests, tags, changelog hygiene, and installation verification, with packaging contracts enforced in CI and release steps documented explicitly

## Next

- Finish aligning any remaining companion-skill guidance and host-facing examples around the existing rule: `plan_* -> web_search` is the recommended core path, while clear single-hop lookups may still call `web_search` directly
- Improve structured observability and troubleshooting guidance without widening the default tool surface too aggressively
- Add richer source metadata and more selective result packaging so calling models receive high-value context first and low-value output is suppressed
- Clarify transport and host integration guidance for local `stdio` usage first, then remote MCP patterns where they are stable enough to document responsibly

## Later

- Evolve `deep research` as a separate advanced product layer, with the default priority on CLI-oriented workflows and careful evaluation of optional advanced MCP modes
- Evaluate deeper research orchestration such as extended `map -> fetch` flows, resumable jobs, progress reporting, exported research artifacts, and support bundles
- Revisit expert versus basic interaction layers only if they can be added without making the default MCP and skill experience heavier or slower

## Compatibility Intent

Public compatibility communication should continue to prioritize clarity over breadth:

- document what is officially tested
- distinguish planned integrations from already-verified ones
- keep machine-specific workarounds, temporary experiments, and local-only setup notes out of the public roadmap
