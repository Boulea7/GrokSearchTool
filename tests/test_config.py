from grok_search.config import Config


def test_setup_command_uses_release_repo_and_v1_placeholder():
    config = Config()

    assert "git+https://github.com/Boulea7/GrokSearchTool@main" in config._SETUP_COMMAND
    assert '"GROK_API_URL":"https://your-api-endpoint.com/v1"' in config._SETUP_COMMAND
