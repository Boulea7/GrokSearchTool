# Changelog

All notable changes to this repository are documented here.

## Unreleased

No unreleased changes yet.

## 1.1.0 - 2026-05-10

### Changed

- Strengthened the deep research AWS DMS `DescribeReplicationTasks` / `RecoveryCheckpoint` report path so final reports preserve the exact response-field evidence and explain how `RecoveryCheckpoint` can inform `CdcStartPosition` resume/restart decisions.
- Added Provider Accounting prose to the final deep research report surface, with matching additive sections in `report.json` and `citations.json` for provider attempts, warnings, failed attempts, and budget usage.
- Hardened deep research artifact/public surfaces around `deep_research_status`, `deep_research_result`, CLI `result --artifact`, `operator_summary`, `artifact_errors`, and resolved final batch identity.
- Added the public `deep_research_artifact(job_id, artifact)` MCP surface for single-artifact reads with the same resolved-final-batch visibility rules as CLI `result --artifact`.
- Updated `plan_*` wrappers to return structured objects directly instead of JSON-string payloads, and documented object response contracts for `web_map`, `switch_model`, and `toggle_builtin_tools`.
- Added public host-integration guidance in `docs/HOSTS.md` for companion skills, host assets, and stdio setup.
- Clarified public docs for Tavily fallback defaults, localized deep research artifact surfaces, and release-safe relay fixture placeholders.
- Added required GitHub release gates for Ruff, compile checks, full pytest, packaging contracts, and built artifact smoke checks on protected `main`.

### Fixed

- Improved planner JSON parsing for array/envelope-wrapped plan payloads.
- Reduced false release-gate failures and false positives around coverage gaps, duplicate/conflicting checkpoint claims, same-domain off-topic sources, and single-source search-only evidence.
- Cleaned GitHub Actions warning noise by moving release workflows to Node 24 action runtimes, using explicit `uv` cache dependency globs, and disabling release-check cache saves that could race across concurrent workflow runs.

### Verification

- Validated the AWS DMS RecoveryCheckpoint path with a narrow live Grok probe and checked in sanitized live fixtures.
- Verified focused deep research tests (`659 passed`), Python compile checks, Ruff, staged secret patterns, GitHub packaging artifact smoke, and the full local pytest suite (`1300 passed`).
- Completed a release dry-run from `main` without tagging or publishing: wheel/sdist build, wheel and sdist install smoke, docs/package contract tests (`74 passed`), GitHub Release Gates, Packaging Contracts, and branch protection checks.

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
