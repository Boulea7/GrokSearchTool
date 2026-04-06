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


def test_process_env_takes_precedence_over_project_env_files(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.setenv("GROK_API_URL", "https://env.example.com/v1")
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("GROK_API_URL=https://fallback.example.com/v1\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_api_url == "https://env.example.com/v1"


def test_project_env_local_takes_precedence_over_project_env(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("TAVILY_API_URL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    (tmp_path / ".env.local").write_text("TAVILY_API_URL=http://localhost:18080/\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TAVILY_API_URL=https://api.tavily.com\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.tavily_api_url == "http://localhost:18080/"


def test_grok_model_uses_project_env_fallback_before_persisted_config(monkeypatch, tmp_path):
    config = Config()
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(config, "_load_config_file", lambda: {"model": "persisted-model"})
    (tmp_path / ".env.local").write_text("GROK_MODEL=project-model\n", encoding="utf-8")
    config.reset_runtime_state()

    assert config.grok_model == "project-model"
