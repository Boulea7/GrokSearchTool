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
