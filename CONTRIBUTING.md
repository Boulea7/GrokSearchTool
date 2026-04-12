# Contributing to GrokSearch

## Scope

GrokSearch is maintained as an independent MCP project with a companion skill. Contributions are welcome for:

- bug fixes
- reliability improvements
- documentation updates
- tests
- new MCP features that fit the project scope
- companion skill improvements that stay aligned with the MCP tool contract

## Development Setup

### Requirements

- Python `3.10+`
- `uv`

### Install dependencies

```bash
uv sync --extra dev
```

### Run locally

```bash
PYTHONPATH=src uv run python -m grok_search.server
```

## Verification

Run all of the following before opening a pull request:

```bash
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

If your change touches only documentation, state that clearly in the PR and explain why code verification was not needed.

Release-specific packaging and tagging steps are documented in [`docs/RELEASING.md`](docs/RELEASING.md).

## What to Include in a Pull Request

- a clear summary of the problem and the change
- linked issue when applicable
- test coverage for behavior changes
- documentation updates for any user-visible behavior change
- environment variable and compatibility notes when relevant

## MCP + Skill Sync Rules

If your change affects the MCP tool contract, update the companion skill as needed:

- tool names, parameter expectations, or planning order changes must be reflected in `skills/research-with-grok-search/`
- documentation should stay aligned across `README.md`, localized README files, and companion skill references
- avoid copying full README-style content into `SKILL.md`; keep the skill focused on workflow guidance

## Issue Guidelines

Open an issue first when:

- the change introduces a new public MCP tool
- the change alters expected tool behavior
- the change adds a new environment variable or changes configuration precedence
- the change expands the project beyond the current roadmap

Direct PRs are fine for:

- straightforward bug fixes
- test additions
- typo fixes
- documentation consistency fixes

## Style Notes

- keep implementations simple and explicit
- prefer focused tests over broad mocks
- avoid silent behavior changes
- keep compatibility notes honest and specific

## Security

Please do not open public issues for security-sensitive problems. Use the process documented in [SECURITY.md](SECURITY.md).
