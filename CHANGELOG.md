# Changelog

All notable changes to this repository are documented here.

## 1.0.0 - 2026-04-04

### Added

- companion skill under `skills/research-with-grok-search/`
- `CONTRIBUTING.md`, `SECURITY.md`, and `CODE_OF_CONDUCT.md`
- GitHub issue and pull request templates
- localized README files for English, Traditional Chinese, Japanese, and Russian
- `docs/ROADMAP.md` and `docs/COMPATIBILITY.md`

### Changed

- aligned repository documentation with the current independent project direction
- corrected MCP tool documentation to reflect the actual `plan_*` tool surface
- updated package metadata to `1.0.0`
- updated setup guidance to use the maintained repository URL

### Fixed

- fixed `extra_sources` distribution when Tavily and Firecrawl are both configured
- added runtime validation for planning inputs
- improved detection of obviously truncated fetch results
- expanded tests around source extraction and planning validation
