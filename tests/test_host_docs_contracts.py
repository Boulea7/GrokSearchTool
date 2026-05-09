import json
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
HOSTS = ROOT_DIR / "docs" / "HOSTS.md"
COMPATIBILITY = ROOT_DIR / "docs" / "COMPATIBILITY.md"
README = ROOT_DIR / "README.md"
README_EN = ROOT_DIR / "README.en.md"
HOST_ASSETS = ROOT_DIR / "docs" / "host-assets"


def test_hosts_doc_lists_broad_host_targets_and_shared_principles():
    text = HOSTS.read_text(encoding="utf-8")

    for fragment in (
        "Cherry Studio",
        "Claude Code / Claude Desktop",
        "Cursor",
        "Cline",
        "Continue",
        "Windsurf",
        "`plan_* -> web_search`",
        "`get_sources`",
        "`deep_research_*`",
        "`grok-search-research`",
    ):
        assert fragment in text


def test_shared_stdio_snippet_is_valid_json_and_points_to_release_repo():
    payload = json.loads((HOST_ASSETS / "grok-search-stdio.json").read_text(encoding="utf-8"))
    server = payload["mcpServers"]["grok-search"]

    assert server["command"] == "uvx"
    assert server["args"] == [
        "--from",
        "git+https://github.com/Boulea7/GrokSearchTool@main",
        "grok-search",
    ]
    assert server["env"]["GROK_API_URL"] == "https://api.example.com/v1"
    assert "your-grok-api-key" in server["env"]["GROK_API_KEY"]


def test_host_assets_keep_common_companion_guidance_aligned():
    expected_fragments = [
        "`plan_* -> web_search`",
        "`get_sources`",
        "`web_fetch`",
        "`web_map`",
        "`deep_research_*`",
        "`grok-search-research`",
    ]
    for path in [
        HOST_ASSETS / "common-companion-guidance.md",
        HOST_ASSETS / "cherry-studio-assistant-preset.md",
        HOST_ASSETS / "cursor-project-rule.md",
        HOST_ASSETS / "cline-skill.md",
        HOST_ASSETS / "continue-rule.md",
        HOST_ASSETS / "windsurf-rule.md",
    ]:
        text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in text


def test_docs_link_broad_host_assets_without_upgrading_support_claims():
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")

    assert "[HOSTS.md](./HOSTS.md)" in compatibility
    assert "do not upgrade a host into `Officially tested`" in compatibility
    assert "[docs/HOSTS.md](./docs/HOSTS.md)" in readme
    assert "[docs/HOSTS.md](./docs/HOSTS.md)" in readme_en


def test_hosts_doc_uses_repo_relative_asset_links_only():
    text = HOSTS.read_text(encoding="utf-8")
    linked_paths = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)

    assert "/Users/" not in text
    for path in linked_paths:
        if path.startswith("./host-assets/"):
            assert (ROOT_DIR / "docs" / path.removeprefix("./")).exists()
