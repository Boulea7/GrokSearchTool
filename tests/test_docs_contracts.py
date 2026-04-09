from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
README = ROOT_DIR / "README.md"
COMPATIBILITY = ROOT_DIR / "docs" / "COMPATIBILITY.md"


def test_readme_requires_explicit_v1_suffix_for_grok_api_url():
    text = README.read_text(encoding="utf-8")

    assert "尽量写成 OpenAI 兼容根路径并显式带上 `/v1`" not in text
    assert "`GROK_API_URL` 推荐使用带显式 `/v1` 的 OpenAI 兼容根路径" in text
    assert "兼容性 warning" in text


def test_docs_explain_lazy_import_boundaries_for_optional_dependencies():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")

    assert "`grok_search.mcp`" in readme
    assert "访问该导出时" in readme
    assert "`fastmcp`" in readme
    assert "`grok_search.providers.GrokSearchProvider`" in readme
    assert "非 provider 导入不应" in readme
    assert "`grok_search.mcp`" in compatibility
    assert "until that export is actually accessed" in compatibility
    assert "`grok_search.providers.GrokSearchProvider`" in compatibility
    assert "non-provider imports should not fail early" in compatibility
