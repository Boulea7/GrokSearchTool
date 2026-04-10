from grok_search.config import Config


def test_setup_command_uses_release_repo_and_v1_placeholder():
    config = Config()

    assert "git+https://github.com/Boulea7/GrokSearchTool@main" in config._SETUP_COMMAND
    assert '"GROK_API_URL":"https://your-api-endpoint.com/v1"' in config._SETUP_COMMAND


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


def test_empty_process_env_still_blocks_project_env_fallback(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("TAVILY_API_KEY=tvly-file-value\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.tavily_api_key == ""


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
