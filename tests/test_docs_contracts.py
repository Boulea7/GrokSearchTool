from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
README = ROOT_DIR / "README.md"
COMPATIBILITY = ROOT_DIR / "docs" / "COMPATIBILITY.md"
README_EN = ROOT_DIR / "README.en.md"
README_ZH_TW = ROOT_DIR / "README.zh-TW.md"
README_JA = ROOT_DIR / "README.ja.md"
README_RU = ROOT_DIR / "README.ru.md"
PYPROJECT = ROOT_DIR / "pyproject.toml"
SECURITY = ROOT_DIR / "SECURITY.md"


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
    assert "不改变安装时依赖声明" in readme
    assert "`fastmcp`" in readme
    assert "`grok_search.providers.GrokSearchProvider`" in readme
    assert "非 provider 导入不应" in readme
    assert "`grok_search.mcp`" in compatibility
    assert "until that export is actually accessed" in compatibility
    assert "does not change the install-time dependency declaration" in compatibility
    assert "`grok_search.providers.GrokSearchProvider`" in compatibility
    assert "non-provider imports should not fail early" in compatibility

    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
    assert "不应被理解为安装依赖已变成 optional extra" in agents


def test_multilingual_readmes_explain_lazy_import_boundaries():
    localized_expectations = {
        README_EN: [
            "`grok_search.mcp`",
            "access-time lazy export",
            "`fastmcp`",
            "`grok_search.providers.GrokSearchProvider`",
            "does not change the install-time dependency declaration",
        ],
        README_ZH_TW: [
            "`grok_search.mcp`",
            "access-time lazy export",
            "`fastmcp`",
            "`grok_search.providers.GrokSearchProvider`",
            "不改變安裝時依賴宣告",
        ],
        README_JA: [
            "`grok_search.mcp`",
            "access-time lazy export",
            "`fastmcp`",
            "`grok_search.providers.GrokSearchProvider`",
            "インストール時の依存宣言は変わりません",
        ],
        README_RU: [
            "`grok_search.mcp`",
            "access-time lazy export",
            "`fastmcp`",
            "`grok_search.providers.GrokSearchProvider`",
            "не меняет декларацию зависимостей на этапе установки",
        ],
    }

    for path, expected_fragments in localized_expectations.items():
        text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in text


def test_lazy_import_docs_do_not_imply_install_time_optional_extras():
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")

    assert "optional dependencies" not in compatibility
    assert "Grok-provider dependencies are missing" in compatibility


def test_docs_keep_masking_scope_narrow_for_ambiguous_keys():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")

    assert "裸 `auth` / `key`" in readme
    assert "默认不会把裸 `auth` / `key` 这类宽泛参数名一并视为敏感字段" in readme
    assert "bare `auth` / `key` keys are intentionally not masked by default" in compatibility

    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
    assert "裸 `auth` / `key`" in agents


def test_docs_cover_high_confidence_cloud_signed_credential_keys():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "X-Amz-Credential" in readme
    assert "X-Goog-Credential" in readme
    assert "GoogleAccessId" in readme
    assert "X-Amz-Credential" in readme_en
    assert "X-Goog-Credential" in readme_en
    assert "GoogleAccessId" in readme_en
    assert "X-Amz-Credential" in compatibility
    assert "X-Goog-Credential" in compatibility
    assert "GoogleAccessId" in compatibility
    assert "X-Amz-Credential" in agents
    assert "X-Goog-Credential" in agents
    assert "GoogleAccessId" in agents


def test_localized_readmes_cover_cloud_signed_credential_keys():
    localized_expectations = {
        README_ZH_TW: ["X-Amz-Credential", "X-Goog-Credential", "GoogleAccessId"],
        README_JA: ["X-Amz-Credential", "X-Goog-Credential", "GoogleAccessId"],
        README_RU: ["X-Amz-Credential", "X-Goog-Credential", "GoogleAccessId"],
    }

    for path, expected_fragments in localized_expectations.items():
        text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in text


def test_readme_fastmcp_badge_matches_pyproject_minimum_dependency():
    readme = README.read_text(encoding="utf-8")
    pyproject = PYPROJECT.read_text(encoding="utf-8")

    assert "FastMCP-2.3.0+" in readme
    assert 'fastmcp>=2.3.0' in pyproject


def test_security_policy_mentions_redirect_preflight_degraded_boundary():
    text = SECURITY.read_text(encoding="utf-8")

    assert "redirect preflight" in text
    assert "skipped_due_to_error" in text
    assert "timeout" in text
    assert "continue downstream provider calls" in text
    assert "best-effort safety boundary" in text


def test_docs_pin_release_repo_and_stdio_first_host_story():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "Boulea7/GrokSearchTool" in readme
    assert "fork/upstream" in readme
    assert "本地 `stdio` 路径" in readme
    assert "Boulea7/GrokSearchTool" in compatibility
    assert "local `stdio`" in compatibility
    assert "fork/upstream" in agents


def test_localized_readmes_pin_release_repo_and_fork_story():
    localized_expectations = {
        README_ZH_TW: ["Boulea7/GrokSearchTool", "fork/upstream"],
        README_JA: ["Boulea7/GrokSearchTool", "fork/upstream"],
        README_RU: ["Boulea7/GrokSearchTool", "fork/upstream"],
    }

    for path, expected_fragments in localized_expectations.items():
        text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in text


def test_docs_keep_planning_first_and_cli_first_research_story():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "`plan_* -> web_search`" in readme
    assert "`deep research`" in readme
    assert "CLI" in readme
    assert "`plan_* -> web_search`" in compatibility
    assert "CLI-first" in compatibility
    assert "`plan_* -> web_search`" in agents
    assert "deep research" in agents


def test_docs_lock_finance_topic_and_diagnostic_detail_contracts():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "目前支持 `general` / `news` / `finance`" in readme
    assert '`"full"` 保留完整 doctor/probe 细节' in readme
    assert "`web_search.topic` currently supports `general`, `news`, and `finance`" in compatibility
    assert "`detail=full|summary`" in compatibility
    assert "`general` / `news` / `finance`" in agents
    assert "`detail=full|summary`" in agents


def test_docs_explain_preferred_default_model_and_flexible_grok_selection():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "`grok-4.20-0309`" in readme
    assert "Grok 4.1+" in readme
    assert "后缀不匹配而直接失败" in readme
    assert "`grok-4.20-0309`" in readme_en
    assert "Grok 4.1+" in readme_en
    assert "failing just because a suffix differs" in readme_en
    assert "`grok-4.20-0309`" in compatibility
    assert "Grok 4.1+" in compatibility
    assert "suffix differs" in compatibility
    assert "`grok-4.20-0309`" in agents
    assert "Grok 4.1+" in agents


def test_docs_explain_runtime_model_source_and_env_override_boundary():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    readme_zh_tw = README_ZH_TW.read_text(encoding="utf-8")
    readme_ja = README_JA.read_text(encoding="utf-8")
    readme_ru = README_RU.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "`GROK_MODEL_SOURCE`" in readme
    assert "当前活动模型来自哪一层" in readme
    assert "单独调用 `switch_model` 不会改变当前进程" in readme
    assert "`GROK_MODEL_SOURCE`" in readme_en
    assert "which layer currently supplies the active model" in readme_en
    assert "calling `switch_model` alone does not change the current process" in readme_en
    assert "`GROK_MODEL_SOURCE`" in compatibility
    assert "active model source" in compatibility
    assert "project `.env.local`" in compatibility
    assert "`GROK_MODEL_SOURCE`" in agents
    assert "单独调用 `switch_model` 不会改变当前进程" in agents
    assert "`GROK_MODEL_SOURCE`" in readme_zh_tw
    assert "目前活動模型實際來自哪一層" in readme_zh_tw
    assert "單獨呼叫 `switch_model` 不會改變目前進程" in readme_zh_tw
    assert "`GROK_MODEL_SOURCE`" in readme_ja
    assert "どの層が供給しているか" in readme_ja
    assert "単独で呼んでも現在のプロセスは切り替わりません" in readme_ja
    assert "`GROK_MODEL_SOURCE`" in readme_ru
    assert "какой слой сейчас задаёт активную модель" in readme_ru
    assert "одного вызова `switch_model` недостаточно" in readme_ru


def test_docs_explain_runtime_model_fallback_boundary():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "`grok_model_selection`" in readme
    assert "`grok_model_runtime_fallback`" in readme
    assert "best-effort 兼容路径" in readme
    assert "上游错误摘要命中“模型不可用”类文案" in readme
    assert "这两个 check 可能同时出现" in readme
    assert "`grok_model_selection`" in compatibility
    assert "`grok_model_runtime_fallback`" in compatibility
    assert "best-effort compatibility path" in compatibility
    assert "model unavailable" in compatibility
    assert "both checks may appear" in compatibility
    assert "`grok_model_selection`" in agents
    assert "`grok_model_runtime_fallback`" in agents
    assert "best-effort 兼容路径" in agents
    assert "这两个 check 可能同时出现" in agents


def test_docs_explain_planning_session_and_wrapper_contract():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    readme_zh_tw = README_ZH_TW.read_text(encoding="utf-8")
    readme_ja = README_JA.read_text(encoding="utf-8")
    readme_ru = README_RU.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "planning `session_id` 当前是进程内的 transient handle" in readme
    assert "`params_json`" in readme
    assert "首次建立 `search_strategy` 时必须提供 `approach`" in readme
    assert "planning `session_id` values are in-process transient handles" in readme_en
    assert "`params_json`" in readme_en
    assert "first `plan_search_term` call must provide `approach`" in readme_en
    assert "planning `session_id`" in compatibility
    assert "in-process transient handles" in compatibility
    assert "`params_json`" in compatibility
    assert "must provide `approach`" in compatibility
    assert "planning `session_id` 当前是进程内的 transient handle" in agents
    assert "`params_json`" in agents
    assert "首次建立 `search_strategy` 时必须提供 `approach`" in agents
    assert "planning `session_id` 是目前進程內的 transient handle" in readme_zh_tw
    assert "`params_json`" in readme_zh_tw
    assert "首次建立 `search_strategy` 時必須提供 `approach`" in readme_zh_tw
    assert "planning `session_id` は現在のプロセス内だけで有効な transient handle" in readme_ja
    assert "`params_json`" in readme_ja
    assert "最初の `plan_search_term` 呼び出しでは `approach` が必須" in readme_ja
    assert "planning `session_id` — это in-process transient handle" in readme_ru
    assert "`params_json`" in readme_ru
    assert "Первый вызов `plan_search_term` обязан передавать `approach`" in readme_ru


def test_docs_explain_redirect_preflight_timeout_and_redirect_limit_contract():
    readme = README.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")
    security = SECURITY.read_text(encoding="utf-8")

    assert "redirect 预检发生超时或请求级错误" in readme
    assert "重定向次数过多" in readme
    assert "第 `5` 次预检时仍然看到新的可见重定向" in readme
    assert "redirect preflight timeouts" in compatibility
    assert "目标 URL 重定向次数过多" in compatibility
    assert "fifth preflight still encounters a new redirect" in compatibility
    assert "redirect 预检发生超时或请求级错误" in agents
    assert "重定向次数过多" in agents
    assert "第 `5` 次预检时仍然看到新的可见重定向" in agents
    assert "timeout failures" in security
    assert "fifth preflight still encounters a new redirect" in security


def test_docs_explain_preflight_warning_side_channel_without_payload_shape_change():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    compatibility = COMPATIBILITY.read_text(encoding="utf-8")
    agents = (ROOT_DIR / "AGENTS.md").read_text(encoding="utf-8")

    assert "caller-visible warning" in readme
    assert "不会改写成功返回体" in readme
    assert "caller-visible warning" in readme_en
    assert "does not rewrite successful return payloads" in readme_en
    assert "caller-visible warning" in compatibility
    assert "does not change successful tool payloads" in compatibility
    assert "caller-visible warning" in agents
    assert "不会改写成功返回体" in agents


def test_localized_readmes_explain_redirect_preflight_contract():
    localized_expectations = {
        README_ZH_TW: ["第 `5` 次預檢", "skipped_due_to_error", "best-effort safety boundary"],
        README_JA: ["`5` 回", "skipped_due_to_error", "best-effort safety boundary"],
        README_RU: ["`5`", "skipped_due_to_error", "best-effort safety boundary"],
    }

    for path, expected_fragments in localized_expectations.items():
        text = path.read_text(encoding="utf-8")
        for fragment in expected_fragments:
            assert fragment in text


def test_ja_and_ru_readmes_enumerate_public_tool_surface():
    ja = README_JA.read_text(encoding="utf-8")
    ru = README_RU.read_text(encoding="utf-8")

    for text in (ja, ru):
        assert "- `plan_intent`" in text
        assert "- `plan_execution`" in text
        assert "- `web_search`" in text
