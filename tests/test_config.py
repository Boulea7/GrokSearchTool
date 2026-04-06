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
