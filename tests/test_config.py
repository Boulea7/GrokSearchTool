import pytest

from grok_search.config import Config


def test_setup_command_uses_release_repo_and_v1_placeholder():
    config = Config()

    assert "git+https://github.com/Boulea7/GrokSearchTool@main" in config._SETUP_COMMAND
    assert '"GROK_API_URL":"https://api.example.com/v1"' in config._SETUP_COMMAND


def test_default_grok_model_prefers_grok_4_20_0309():
    config = Config()

    assert config._DEFAULT_MODEL == "grok-4.20-0309"


def test_default_grok_model_uses_unified_balanced_default_for_official_xai(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.grok_model == "grok-4.20-auto"


def test_default_grok_model_uses_unified_balanced_default_for_openrouter(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.grok_model == "grok-4.20-auto:online"


def test_default_grok_model_uses_unified_balanced_default_for_relay(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://grok2api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.grok_model == "grok-4.20-auto"


def test_grok_provider_chain_uses_unified_defaults_when_runtime_model_is_implicit(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://grok2api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    (tmp_path / ".env.local").write_text(
        (
            "GROK_API_URL_2=https://openrouter.ai/api/v1\n"
            "GROK_API_KEY_2=secondary-key\n"
            "GROK_API_URL_3=https://api.x.ai/v1\n"
            "GROK_API_KEY_3=third-key\n"
        ),
        encoding="utf-8",
    )
    config.reset_runtime_state()

    chain = config.grok_provider_chain()

    assert [item["model"] for item in chain] == [
        "grok-4.20-auto",
        "grok-4.20-auto:online",
        "grok-4.20-auto",
    ]
    assert [item["provider_family"] for item in chain] == [
        "grok2api_like",
        "openrouter",
        "official_xai",
    ]


def test_time_context_mode_defaults_to_always(monkeypatch):
    monkeypatch.delenv("GROK_TIME_CONTEXT_MODE", raising=False)
    config = Config()

    assert config.time_context_mode == "always"


def test_time_context_mode_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("GROK_TIME_CONTEXT_MODE", "sometimes")
    config = Config()

    assert config.time_context_mode == "always"


def test_grok_model_prefers_env_over_persisted_config(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_MODEL", "env-model")
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})

    assert config.grok_model == "env-model"


def test_web_search_override_does_not_replace_global_grok_model(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_WEB_SEARCH_MODEL", "balanced-override")
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})

    assert config.grok_model == "persisted-model"
    assert config.preferred_web_search_models_for_url("https://api.example.com/v1")[0] == "balanced-override"


def test_preferred_web_search_models_use_tool_level_defaults_instead_of_global_model(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_WEB_SEARCH_MODEL", raising=False)
    monkeypatch.delenv("GROK_WEB_SEARCH_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})

    assert config.grok_model == "persisted-model"
    assert config.preferred_web_search_models_for_url("https://api.example.com/v1") == [
        "grok-4.20-fast",
        "grok-4.20-0309",
        "grok-4.20-auto",
        "grok-4.20-0309-non-reasoning",
        "grok-4.20-reasoning",
        "grok-4.20-expert",
    ]
    assert config.preferred_web_search_models_for_url("https://openrouter.ai/api/v1") == [
        "grok-4.20-fast:online",
        "grok-4.20-0309:online",
        "grok-4.20-auto:online",
        "grok-4.20-0309-non-reasoning:online",
        "grok-4.20-reasoning:online",
        "grok-4.20-expert:online",
    ]


def test_set_model_does_not_override_env_priority_in_current_process(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_MODEL", "env-model")
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})
    saved = {}
    monkeypatch.setattr(config, "_save_config_file", lambda data: saved.update(data))

    config.set_model("new-persisted-model")

    assert saved["model"] == "new-persisted-model"
    assert config.grok_model == "env-model"


def test_reset_runtime_state_clears_cached_model(monkeypatch):
    config = Config()
    monkeypatch.setattr(config, "_cached_model", "cached-model", raising=False)

    config.reset_runtime_state()

    assert config._cached_model is None


def test_grok_model_cache_is_isolated_by_project_root(monkeypatch, tmp_path):
    config = Config()
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / ".env.local").write_text("GROK_MODEL=model-a\n", encoding="utf-8")
    (root_b / ".env.local").write_text("GROK_MODEL=model-b\n", encoding="utf-8")
    current_root = root_a
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: current_root)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    config.reset_runtime_state()

    assert config.grok_model == "model-a"

    current_root = root_b

    assert config.grok_model == "model-b"


def test_reset_runtime_state_clears_all_project_root_buckets(monkeypatch, tmp_path):
    config = Config()
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / ".env.local").write_text("GROK_MODEL=model-a\n", encoding="utf-8")
    (root_b / ".env.local").write_text("GROK_MODEL=model-b\n", encoding="utf-8")
    current_root = root_a
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: current_root)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    config.reset_runtime_state()

    assert config.grok_model == "model-a"

    current_root = root_b
    assert config.grok_model == "model-b"

    (root_a / ".env.local").write_text("GROK_MODEL=model-a-updated\n", encoding="utf-8")
    (root_b / ".env.local").write_text("GROK_MODEL=model-b-updated\n", encoding="utf-8")

    config.reset_runtime_state()

    current_root = root_a
    assert config.grok_model == "model-a-updated"
    current_root = root_b
    assert config.grok_model == "model-b-updated"


def test_grok_api_url_falls_back_to_project_env_local(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_URL=https://fallback.example.com/v1\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_api_url == "https://fallback.example.com/v1"


def test_project_env_fallback_accepts_export_prefixed_entries(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        "export GROK_API_KEY=project-key\nexport TAVILY_API_URL=https://mirror.example.com\n",
        encoding="utf-8",
    )
    config.reset_runtime_state()

    assert config.grok_api_key == "project-key"
    assert config.tavily_api_url == "https://mirror.example.com"


def test_project_env_fallback_strips_unquoted_inline_comments(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("TAVILY_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        "TAVILY_API_URL=https://api.tavily.com # local mirror comment\n",
        encoding="utf-8",
    )
    config.reset_runtime_state()

    assert config.tavily_api_url == "https://api.tavily.com"


def test_project_env_fallback_keeps_hash_fragments_in_unquoted_values(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        "FIRECRAWL_API_URL=https://api.firecrawl.dev/v2#section\n",
        encoding="utf-8",
    )
    config.reset_runtime_state()

    assert config.firecrawl_api_url == "https://api.firecrawl.dev/v2#section"


def test_project_env_fallback_strips_comments_after_quoted_values(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        'GROK_API_KEY="project-key" # trailing comment\n',
        encoding="utf-8",
    )
    config.reset_runtime_state()

    assert config.grok_api_key == "project-key"


def test_project_env_fallback_keeps_malformed_suffix_after_quoted_values(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        'GROK_API_KEY="project-key"oops\n',
        encoding="utf-8",
    )
    config.reset_runtime_state()

    assert config.grok_api_key == '"project-key"oops'


def test_process_env_takes_precedence_over_project_env_files(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://env.example.com/v1")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_URL=https://fallback.example.com/v1\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_api_url == "https://env.example.com/v1"


def test_primary_provider_does_not_mix_env_url_with_project_key(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://env.example.com/v1")
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_KEY=project-key\n", encoding="utf-8")
    config.reset_runtime_state()

    with pytest.raises(ValueError, match="Grok API Key 未配置"):
        _ = config.grok_api_key


def test_primary_provider_does_not_mix_env_key_with_project_url(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_API_URL", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "env-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_URL=https://project.example.com/v1\n", encoding="utf-8")
    config.reset_runtime_state()

    with pytest.raises(ValueError, match="Grok API URL 未配置"):
        _ = config.grok_api_url


def test_grok_provider_chain_includes_numbered_fallback_providers(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-0309")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        (
            "GROK_API_URL_2=https://secondary.example.com/v1\n"
            "GROK_API_KEY_2=secondary-key\n"
            "GROK_MODEL_2=grok-4.20-0309\n"
            "GROK_API_URL_3=https://third.example.com/v1\n"
            "GROK_API_KEY_3=third-key\n"
        ),
        encoding="utf-8",
    )
    config.reset_runtime_state()

    chain = config.grok_provider_chain()

    assert [item["api_url"] for item in chain] == [
        "https://primary.example.com/v1",
        "https://secondary.example.com/v1",
        "https://third.example.com/v1",
    ]
    assert [item["model"] for item in chain] == [
        "grok-4.20-0309",
        "grok-4.20-0309",
        "grok-4.20-0309",
    ]
    assert chain[1]["name"] == "provider_2"


def test_grok_provider_chain_skips_numbered_provider_when_url_and_key_cross_layers(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setenv("GROK_API_URL_2", "https://secondary.example.com/v1")
    monkeypatch.delenv("GROK_API_KEY_2", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_KEY_2=secondary-key\n", encoding="utf-8")
    config.reset_runtime_state()

    chain = config.grok_provider_chain()

    assert [item["name"] for item in chain] == ["primary"]


def test_grok_provider_chain_does_not_change_base_config_snapshot(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://primary.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text(
        "GROK_API_URL_2=https://secondary.example.com/v1\nGROK_API_KEY_2=secondary-key\n",
        encoding="utf-8",
    )
    config.reset_runtime_state()

    info = config.get_config_info()

    assert "GROK_PROVIDER_COUNT" not in info
    assert "GROK_PROVIDER_CHAIN" not in info


def test_get_config_info_includes_routing_diagnostics_for_profile_defaults(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    info = config.get_config_info()
    diagnostics = info["GROK_ROUTING_DIAGNOSTICS"]

    assert diagnostics["active_provider"]["provider_family"] == "official_xai"
    assert diagnostics["active_provider"]["resolved_model"] == "grok-4.20-auto"
    assert diagnostics["active_provider"]["preferred_endpoint_path"] == "/chat/completions"
    assert diagnostics["active_provider"]["path_visibility"] == {
        "chat_completions": "https://api.x.ai/v1/chat/completions",
        "responses": "https://api.x.ai/v1/responses",
    }
    assert diagnostics["profile_defaults"]["web_search"] == {
        "profile": "balanced_auto",
        "resolved_model": "grok-4.20-auto",
        "preferred_endpoint_path": "/chat/completions",
        "path_visibility": {
            "chat_completions": "https://api.x.ai/v1/chat/completions",
            "responses": "https://api.x.ai/v1/responses",
        },
        "multi_agent_requested": False,
        "multi_agent_family": False,
        "routing_signals": [
            "profile_requests_single_agent",
            "model_family:single_agent",
            "routing_path:chat_completions",
            "official_xai_chat_completions_default",
        ],
    }
    assert diagnostics["profile_defaults"]["deep_research_deep"] == {
        "profile": "multi_agent",
        "resolved_model": "grok-4.20-expert-4-agent",
        "preferred_endpoint_path": "/responses",
        "path_visibility": {
            "chat_completions": "https://api.x.ai/v1/chat/completions",
            "responses": "https://api.x.ai/v1/responses",
        },
        "multi_agent_requested": True,
        "multi_agent_family": True,
        "routing_signals": [
            "profile_requests_multi_agent",
            "model_family:multi_agent",
            "routing_path:responses",
            "official_xai_multi_agent_prefers_responses",
        ],
    }


def test_get_config_info_routing_diagnostics_summarize_provider_chain_families_and_paths(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://relay.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "primary-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    (tmp_path / ".env.local").write_text(
        (
            "GROK_API_URL_2=https://openrouter.ai/api/v1\n"
            "GROK_API_KEY_2=secondary-key\n"
            "GROK_API_URL_3=https://api.x.ai/v1\n"
            "GROK_API_KEY_3=third-key\n"
        ),
        encoding="utf-8",
    )
    config.reset_runtime_state()

    info = config.get_config_info()
    provider_chain = info["GROK_ROUTING_DIAGNOSTICS"]["provider_chain"]

    assert provider_chain == [
        {
            "name": "primary",
            "source": "primary",
            "provider_family": "openai_compatible_relay",
            "resolved_model": "grok-4.20-auto",
            "preferred_endpoint_path": "/chat/completions",
            "multi_agent_family": False,
            "routing_signals": [
                "model_family:single_agent",
                "routing_path:chat_completions",
                "relay_chat_completions_default",
            ],
        },
        {
            "name": "provider_2",
            "source": "project_env_local",
            "provider_family": "openrouter",
            "resolved_model": "grok-4.20-auto:online",
            "preferred_endpoint_path": "/chat/completions",
            "multi_agent_family": False,
            "routing_signals": [
                "model_family:single_agent",
                "routing_path:chat_completions",
                "openrouter_chat_completions_default",
            ],
        },
        {
            "name": "provider_3",
            "source": "project_env_local",
            "provider_family": "official_xai",
            "resolved_model": "grok-4.20-auto",
            "preferred_endpoint_path": "/chat/completions",
            "multi_agent_family": False,
            "routing_signals": [
                "model_family:single_agent",
                "routing_path:chat_completions",
                "official_xai_chat_completions_default",
            ],
        },
    ]


def test_deep_research_ultra_profile_defaults_to_ultra(monkeypatch):
    monkeypatch.delenv("GROK_DEEP_RESEARCH_ULTRA_PROFILE", raising=False)
    config = Config()
    config.reset_runtime_state()

    assert config.grok_deep_research_ultra_profile() == "ultra"


def test_resolve_deep_research_model_for_ultra_prefers_heavy_16_agent_on_official_xai(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://api.x.ai/v1", effort="ultra") == "grok-4.20-heavy-16-agent"


def test_resolve_deep_research_model_for_deep_prefers_expert_4_agent_on_relay(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://grok2api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://grok2api.example.com/v1", effort="deep") == "grok-4.20-expert-4-agent"


def test_resolve_deep_research_model_for_standard_defaults_to_expert(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://api.x.ai/v1", effort="standard") == "grok-4.20-expert"
    assert config.resolve_deep_research_model_for_url("https://grok2api.example.com/v1", effort="standard") == "grok-4.20-expert"


def test_preferred_deep_research_standard_models_use_unified_tool_strategy(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.preferred_deep_research_models_for_url("https://api.x.ai/v1", effort="standard") == [
        "grok-4.20-expert",
        "grok-4.20-reasoning",
        "grok-4.20-auto",
        "grok-4.20-fast",
    ]
    assert config.preferred_deep_research_models_for_url("https://openrouter.ai/api/v1", effort="standard") == [
        "grok-4.20-expert:online",
        "grok-4.20-reasoning:online",
        "grok-4.20-auto:online",
        "grok-4.20-fast:online",
    ]


def test_resolve_deep_research_model_for_ultra_prefers_heavy_16_agent_on_relay(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://grok2api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://grok2api.example.com/v1", effort="ultra") == "grok-4.20-heavy-16-agent"


def test_resolve_deep_research_model_uses_standard_override_across_provider_families(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_DEEP_RESEARCH_STANDARD_MODEL", "standard-override")
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://api.x.ai/v1", effort="standard") == "standard-override"
    assert config.resolve_deep_research_model_for_url("https://grok2api.example.com/v1", effort="standard") == "standard-override"


def test_resolve_deep_research_model_uses_ultra_override_across_provider_families(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setenv("GROK_DEEP_RESEARCH_ULTRA_MODEL", "ultra-override")
    monkeypatch.setenv("GROK_API_URL", "https://api.x.ai/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    assert config.resolve_deep_research_model_for_url("https://api.x.ai/v1", effort="ultra") == "ultra-override"
    assert config.resolve_deep_research_model_for_url("https://openrouter.ai/api/v1", effort="ultra") == "ultra-override:online"


def test_get_config_info_exposes_tool_level_model_overrides(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setenv("GROK_WEB_SEARCH_MODEL", "web-override")
    monkeypatch.setenv("GROK_WEB_SEARCH_FALLBACK_MODELS", "web-fallback-a,web-fallback-b")
    monkeypatch.setenv("GROK_DEEP_RESEARCH_STANDARD_MODEL", "standard-override")
    monkeypatch.setenv("GROK_DEEP_RESEARCH_DEEP_MODEL", "deep-override")
    monkeypatch.setenv("GROK_DEEP_RESEARCH_ULTRA_MODEL", "ultra-override")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})

    info = config.get_config_info()

    assert info["GROK_WEB_SEARCH_MODEL"] == "web-override"
    assert info["GROK_WEB_SEARCH_FALLBACK_MODELS"] == ["web-fallback-a", "web-fallback-b"]
    assert info["GROK_DEEP_RESEARCH_STANDARD_MODEL"] == "standard-override"
    assert info["GROK_DEEP_RESEARCH_DEEP_MODEL"] == "deep-override"
    assert info["GROK_DEEP_RESEARCH_ULTRA_MODEL"] == "ultra-override"


def test_empty_process_env_still_blocks_project_env_fallback(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("TAVILY_API_KEY=tvly-file-value\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.tavily_api_key == ""


def test_tavily_fallback_config_defaults_to_disabled_without_remote_url(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-primary-secret")
    monkeypatch.delenv("TAVILY_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_FALLBACK_API_URL", raising=False)
    monkeypatch.delenv("TAVILY_FALLBACK_ENABLED", raising=False)
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "test-key")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    config.reset_runtime_state()

    info = config.get_config_info()

    assert config.tavily_fallback_enabled is False
    assert config.tavily_fallback_api_url == ""
    assert config.tavily_fallback_api_key == "tvly-primary-secret"
    assert info["TAVILY_FALLBACK_ENABLED"] is False
    assert info["TAVILY_FALLBACK_API_URL"] == ""
    assert "tvly-primary-secret" not in info["TAVILY_FALLBACK_API_KEY"]


def test_tavily_fallback_config_honors_explicit_disable_and_key(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-primary-secret")
    monkeypatch.setenv("TAVILY_FALLBACK_API_KEY", "tvly-fallback-secret")
    monkeypatch.setenv("TAVILY_FALLBACK_API_URL", "https://fallback.example.com/api")
    monkeypatch.setenv("TAVILY_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {})
    config.reset_runtime_state()

    assert config.tavily_fallback_enabled is False
    assert config.tavily_fallback_api_url == "https://fallback.example.com/api"
    assert config.tavily_fallback_api_key == "tvly-fallback-secret"


def test_project_env_local_takes_precedence_over_project_env(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("TAVILY_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("TAVILY_API_URL=http://localhost:18080/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TAVILY_API_URL=https://api.tavily.com\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.tavily_api_url == "http://localhost:18080/"


def test_reset_runtime_state_refreshes_cached_project_env(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("TAVILY_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    env_file = tmp_path / ".env.local"
    env_file.write_text("TAVILY_API_URL=https://first.example.com\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.tavily_api_url == "https://first.example.com"

    env_file.write_text("TAVILY_API_URL=https://second.example.com\n", encoding="utf-8")

    assert config.tavily_api_url == "https://first.example.com"

    config.reset_runtime_state()

    assert config.tavily_api_url == "https://second.example.com"


def test_grok_model_uses_project_env_fallback_before_persisted_config(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})
    (tmp_path / ".env.local").write_text("GROK_MODEL=project-model\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_model == "project-model"
    assert config.grok_model_source == "project_env_local"


def test_grok_model_source_prefers_process_env_over_project_and_persisted(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_MODEL", "env-model")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})
    (tmp_path / ".env.local").write_text("GROK_MODEL=project-model\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_model_source == "process_env"


def test_empty_grok_model_env_blocks_persisted_fallback(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_MODEL", "")
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})

    assert config.grok_model == ""


def test_grok_model_adds_online_suffix_for_openrouter_urls(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROK_MODEL", "openai/gpt-4.1")

    assert config.grok_model == "openai/gpt-4.1:online"


def test_grok_model_adds_online_suffix_for_mixed_case_openrouter_urls(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://OpenRouter.ai/api/v1")
    monkeypatch.setenv("GROK_MODEL", "openai/gpt-4.1")

    assert config.grok_model == "openai/gpt-4.1:online"


def test_grok_model_keeps_existing_online_suffix_for_openrouter_urls(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GROK_MODEL", "openai/gpt-4.1:online")

    assert config.grok_model == "openai/gpt-4.1:online"


def test_get_config_info_masks_sensitive_url_components(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://user:pass@api.example.com/v1?token=abc123#sig=zzz")
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")
    monkeypatch.setenv("TAVILY_API_URL", "https://user:pass@api.tavily.com?access_token=abc123")
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://user:pass@api.firecrawl.dev/v2#code=otp987")

    info = config.get_config_info()

    assert info["GROK_API_URL"] == "https://api.example.com/v1?token=***#sig=***"
    assert info["TAVILY_API_URL"] == "https://api.tavily.com?access_token=***"
    assert info["FIRECRAWL_API_URL"] == "https://api.firecrawl.dev/v2#code=***"


def test_get_config_info_masks_oauth_style_secret_params(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv(
        "GROK_API_URL",
        (
            "https://user:pass@api.example.com/v1"
            "?client_secret=example-client-secret"
            "&refresh_token=example-refresh-token"
            "&id_token=example-id-token"
            "#password=example-value"
        ),
    )
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")

    info = config.get_config_info()

    assert info["GROK_API_URL"] == (
        "https://api.example.com/v1"
        "?client_secret=***"
        "&refresh_token=***"
        "&id_token=***"
        "#password=***"
    )


def test_get_config_info_masks_cloud_signed_credential_keys(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv(
        "GROK_API_URL",
        (
            "https://user:pass@api.example.com/v1"
            "?X-Amz-Credential=cred"
            "&X-Goog-Credential=gcred"
            "&GoogleAccessId=gid"
            "&keep=ok"
        ),
    )
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")

    info = config.get_config_info()

    assert info["GROK_API_URL"] == (
        "https://api.example.com/v1"
        "?X-Amz-Credential=***"
        "&X-Goog-Credential=***"
        "&GoogleAccessId=***"
        "&keep=ok"
    )


def test_get_config_info_tolerates_invalid_port_in_masked_urls(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com:abc/v1?token=abc123")
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")

    info = config.get_config_info()

    assert info["GROK_API_URL"] == "https://api.example.com:abc/v1?token=***"


def test_get_config_info_does_not_create_log_dir(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")
    monkeypatch.setenv("GROK_LOG_DIR", "custom-logs")

    expected_path = home_dir / ".config" / "grok-search" / "custom-logs"

    info = config.get_config_info()

    assert info["GROK_LOG_DIR"] == str(expected_path)
    assert info["GROK_MODEL_SOURCE"] == "default"
    assert not expected_path.exists()


def test_log_dir_still_creates_directory_when_explicitly_accessed(monkeypatch, tmp_path):
    config = Config()
    config.reset_runtime_state()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("GROK_LOG_DIR", "custom-logs")

    expected_path = home_dir / ".config" / "grok-search" / "custom-logs"

    resolved = config.log_dir

    assert resolved == expected_path
    assert expected_path.exists()
    assert expected_path.is_dir()


def test_config_get_config_info_excludes_server_only_diagnostic_fields(monkeypatch):
    config = Config()
    config.reset_runtime_state()
    monkeypatch.setenv("GROK_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("GROK_API_KEY", "sk-secret-value")

    info = config.get_config_info()

    assert "connection_test" not in info
    assert "doctor" not in info
    assert "feature_readiness" not in info
