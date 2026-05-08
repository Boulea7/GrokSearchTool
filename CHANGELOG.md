# Changelog

All notable changes to this repository are documented here.

## Unreleased

### Changed

- Strengthened the deep research AWS DMS `DescribeReplicationTasks` / `RecoveryCheckpoint` report path so final reports preserve the exact response-field evidence and explain how `RecoveryCheckpoint` can inform `CdcStartPosition` resume/restart decisions.
- Added Provider Accounting prose to the final deep research report surface, with matching additive sections in `report.json` and `citations.json` for provider attempts, warnings, failed attempts, and budget usage.
- Hardened deep research artifact/public surfaces around `deep_research_status`, `deep_research_result`, CLI `result --artifact`, `operator_summary`, `artifact_errors`, and resolved final batch identity.
- Added the public `deep_research_artifact(job_id, artifact)` MCP surface for single-artifact reads with the same resolved-final-batch visibility rules as CLI `result --artifact`.
- Updated `plan_*` wrappers to return structured objects directly instead of JSON-string payloads, and documented object response contracts for `web_map`, `switch_model`, and `toggle_builtin_tools`.
- Added public host-integration guidance in `docs/HOSTS.md` for companion skills, host assets, and stdio setup.

### Fixed

- Improved planner JSON parsing for array/envelope-wrapped plan payloads.
- Reduced false release-gate failures and false positives around coverage gaps, duplicate/conflicting checkpoint claims, same-domain off-topic sources, and single-source search-only evidence.

### Verification

- Validated the AWS DMS RecoveryCheckpoint path with a narrow live Grok probe and checked in sanitized live fixtures.
- Verified focused deep research tests (`659 passed`), Python compile checks, Ruff, staged secret patterns, and the full local pytest suite (`1286 passed`).

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
