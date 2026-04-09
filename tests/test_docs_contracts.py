from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
README = ROOT_DIR / "README.md"
COMPATIBILITY = ROOT_DIR / "docs" / "COMPATIBILITY.md"
README_EN = ROOT_DIR / "README.en.md"
README_ZH_TW = ROOT_DIR / "README.zh-TW.md"
README_JA = ROOT_DIR / "README.ja.md"
README_RU = ROOT_DIR / "README.ru.md"


def test_readme_requires_explicit_v1_suffix_for_grok_api_url():
    text = README.read_text(encoding="utf-8")

    assert "尽量写成 OpenAI 兼容根路径并显式带上 `/v1`" not in text
    assert "值必须显式包含 `/v1` 后缀" not in text
    assert "`GROK_API_URL` 推荐使用带显式 `/v1` 后缀的 OpenAI 兼容根路径" in text
    assert "代码层不会仅因省略 `/v1` 就预先拦截" in text
    assert "多数 OpenAI 兼容端点仍可能因此在运行时失败" in text

    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    assert "must include an explicit `/v1` suffix" not in compatibility
    assert "does not pre-block the request on its own" in compatibility
    assert "many OpenAI-compatible endpoints may still fail at runtime" in compatibility


def test_v1_guidance_stays_aligned_across_multilingual_readmes():
    text = README.read_text(encoding="utf-8")

    assert "推荐使用带显式 `/v1` 后缀的 OpenAI 兼容根路径" in text
    assert "不会仅因省略 `/v1` 就预先拦截" in text
    assert "运行时失败" in text
    assert "需要 OpenAI 兼容根路径，并显式带上 `/v1` 后缀" not in text

    localized_expectations = {
        README_EN: ["recommended", "does not pre-block", "fail at runtime"],
        README_ZH_TW: ["建議顯式包含", "不會僅因省略", "執行期失敗"],
        README_JA: ["推奨", "事前にブロック", "実行時に失敗"],
        README_RU: ["рекомендуется", "не блокирует запрос заранее", "во время выполнения"],
    }
    localized_forbidden = {
        README_EN: "must include an explicit `/v1` suffix",
        README_ZH_TW: "值必須顯式包含 `/v1` 後綴",
        README_JA: "明示的な `/v1` サフィックスが必須です",
        README_RU: "значение должно явно оканчиваться на `/v1`",
    }

    for path, expected_fragments in localized_expectations.items():
        localized_text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in localized_text
        assert localized_forbidden[path] not in localized_text


def test_traditional_chinese_readme_covers_masking_and_shared_daemon_handle_contract():
    text = README_ZH_TW.read_text(encoding="utf-8")

    assert "shared-daemon" in text
    assert "secret token" in text
    assert "bearer" in text
    assert "簽名" in text
    assert "遮罩" in text


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
