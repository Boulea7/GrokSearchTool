# Releasing

This project currently keeps release discipline simple and explicit.

## Release Checklist

1. Update the version in [`pyproject.toml`](../pyproject.toml) if the release requires a version bump.
2. Update [`CHANGELOG.md`](../CHANGELOG.md) with the user-visible changes and contract notes included in the release.
3. Run the full local verification suite:

```bash
uv run --with pytest --with pytest-asyncio pytest -q
uv run --with ruff ruff check .
python3 -m py_compile src/grok_search/*.py src/grok_search/providers/*.py tests/*.py
```

4. Build both public distribution artifacts:

```bash
uv build --wheel --sdist
```

5. Run artifact smoke checks against the built wheel in a fresh virtual environment:

```bash
python3 -m venv .venv-release-smoke
. .venv-release-smoke/bin/activate
python -m pip install dist/*.whl
python -c "import grok_search.planning; import grok_search.server; from grok_search.server import main; print(callable(main))"
python -c "import importlib.metadata; print(importlib.metadata.distribution('grok-search').entry_points)"
deactivate
rm -rf .venv-release-smoke
```

The smoke check must confirm:

- `grok_search.planning` imports from the built artifact
- `grok_search.server` imports from the built artifact
- `grok_search.server.main` resolves as the console entry target
- the installed `grok-search` distribution exposes the expected console script

6. Push the release branch and create an annotated Git tag:

```bash
git push origin <release-branch>
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

## Notes

- Public installation guidance should stay aligned with the maintained release repository and the current `stdio`-first story.
- If packaging or install behavior changes, update the README set, compatibility docs, and packaging contract tests in the same change.
