import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
README = ROOT_DIR / "README.md"
README_EN = ROOT_DIR / "README.en.md"
README_ZH_TW = ROOT_DIR / "README.zh-TW.md"
README_JA = ROOT_DIR / "README.ja.md"
README_RU = ROOT_DIR / "README.ru.md"


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath = str(SRC_DIR)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _run_subprocess(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_script(venv_dir: Path, script_name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{script_name}.exe"
    return venv_dir / "bin" / script_name


def _extract_json_block_after_marker(text: str, marker: str) -> dict:
    pattern = re.escape(marker) + r".*?```json\s*(\{.*?\})\s*```"
    match = re.search(pattern, text, re.DOTALL)
    assert match, f"Expected JSON code block after marker: {marker}"
    return json.loads(match.group(1))


def _extract_claude_add_json_payload(text: str, marker: str) -> dict:
    pattern = re.escape(marker) + r".*?claude mcp add-json grok-search --scope user '(\{.*?\})'"
    match = re.search(pattern, text, re.DOTALL)
    assert match, f"Expected claude mcp add-json payload after marker: {marker}"
    return json.loads(match.group(1))


def _extract_toml_block_after_marker(text: str, marker: str) -> str:
    pattern = re.escape(marker) + r".*?```toml\s*(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    assert match, f"Expected TOML code block after marker: {marker}"
    return match.group(1)


def _extract_toml_string(block: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}\s*=\s*\"([^\"]+)\"", block, re.MULTILINE)
    assert match, f"Expected TOML string for {key}"
    return match.group(1)


def _extract_toml_array(block: str, key: str) -> list[str]:
    match = re.search(rf"^{re.escape(key)}\s*=\s*(\[[^\n]+\])", block, re.MULTILINE)
    assert match, f"Expected TOML array for {key}"
    return json.loads(match.group(1).replace("'", '"'))


def _extract_toml_env_keys(block: str) -> set[str]:
    env_lines = re.findall(r"^([A-Z0-9_]+)\s*=\s*\"[^\"]*\"$", block, re.MULTILINE)
    return set(env_lines)


def test_run_python_sets_timeout(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_python("print('ok')")

    assert result.returncode == 0
    assert captured["timeout"] == 30


def test_non_server_modules_import_without_fastmcp():
    code = textwrap.dedent(
        """
        import builtins
        import importlib
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        modules = [
            "grok_search",
            "grok_search.config",
            "grok_search.planning",
            "grok_search.sources",
            "grok_search.providers.grok",
        ]
        results = {}
        for module_name in modules:
            try:
                importlib.import_module(module_name)
                results[module_name] = "ok"
            except Exception as exc:
                results[module_name] = f"{type(exc).__name__}: {exc}"

        print(json.dumps(results))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "grok_search": "ok",
        "grok_search.config": "ok",
        "grok_search.planning": "ok",
        "grok_search.sources": "ok",
        "grok_search.providers.grok": "ok",
    }


def test_mcp_export_fails_lazily_without_fastmcp():
    code = textwrap.dedent(
        """
        import builtins
        import importlib
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        package = importlib.import_module("grok_search")
        try:
            package.mcp
        except Exception as exc:
            print(json.dumps({
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }))
        else:
            print(json.dumps({"type": "ok"}))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "ModuleNotFoundError"
    assert payload["name"] == "fastmcp"
    assert "fastmcp" in payload["message"]


def test_mcp_export_still_works_when_fastmcp_is_available():
    code = "from grok_search import mcp; print(hasattr(mcp, 'tool'))"
    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_from_import_mcp_fails_lazily_without_fastmcp():
    code = textwrap.dedent(
        """
        import builtins
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        try:
            from grok_search import mcp
        except Exception as exc:
            print(json.dumps({
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }))
        else:
            print(json.dumps({"type": "ok", "has_tool": hasattr(mcp, "tool")}))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "ModuleNotFoundError"
    assert payload["name"] == "fastmcp"
    assert "fastmcp" in payload["message"]


def test_mcp_export_propagates_non_fastmcp_dependency_errors():
    code = textwrap.dedent(
        """
        import builtins
        import importlib
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "pydantic" or name.startswith("pydantic."):
                raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        package = importlib.import_module("grok_search")
        try:
            package.mcp
        except Exception as exc:
            print(json.dumps({
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }))
        else:
            print(json.dumps({"type": "ok"}))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "ModuleNotFoundError"
    assert payload["name"] == "pydantic"
    assert "fastmcp is required" not in payload["message"]


def test_provider_lazy_export_propagates_dependency_errors_only_on_access():
    code = textwrap.dedent(
        """
        import builtins
        import importlib
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tenacity" or name.startswith("tenacity."):
                raise ModuleNotFoundError("No module named 'tenacity'", name="tenacity")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        providers = importlib.import_module("grok_search.providers")
        package_import_ok = hasattr(providers, "BaseSearchProvider")
        try:
            providers.GrokSearchProvider
        except Exception as exc:
            payload = {
                "package_import_ok": package_import_ok,
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }
        else:
            payload = {"package_import_ok": package_import_ok, "type": "ok"}

        print(json.dumps(payload))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["package_import_ok"] is True
    assert payload["type"] == "ModuleNotFoundError"
    assert payload["name"] == "tenacity"


def test_built_local_wheel_exposes_console_script_and_install_surface(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build a local wheel artifact")

    dist_dir = tmp_path / "dist"
    build_result = _run_subprocess(
        [uv, "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=ROOT_DIR,
        timeout=180,
    )
    assert build_result.returncode == 0, build_result.stderr

    wheels = sorted(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel_path = wheels[0]

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(venv_dir)
    venv_python = _venv_python(venv_dir)
    install_result = _run_subprocess(
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
        cwd=ROOT_DIR,
        timeout=180,
    )
    assert install_result.returncode == 0, install_result.stderr

    script_path = _venv_script(venv_dir, "grok-search")
    assert script_path.exists(), f"Expected console script at {script_path}"

    metadata_code = textwrap.dedent(
        """
        import importlib.metadata
        import json
        import pathlib

        dist = importlib.metadata.distribution("grok-search")
        console_scripts = {
            entry.name: entry.value
            for entry in dist.entry_points
            if entry.group == "console_scripts"
        }
        print(json.dumps({
            "console_scripts": console_scripts,
            "files_include_server": "grok_search/server.py" in {str(item) for item in dist.files or []},
            "script_exists": pathlib.Path(%(script_path)r).exists(),
            "requires": sorted(dist.requires or []),
        }))
        """
        % {"script_path": str(script_path)}
    )
    metadata_result = _run_subprocess(
        [str(venv_python), "-c", metadata_code],
        cwd=ROOT_DIR,
    )
    assert metadata_result.returncode == 0, metadata_result.stderr
    metadata_payload = json.loads(metadata_result.stdout)
    assert metadata_payload["console_scripts"]["grok-search"] == "grok_search.server:main"
    assert metadata_payload["files_include_server"] is True
    assert metadata_payload["script_exists"] is True
    assert {
        "fastmcp>=2.3.0",
        "httpx[socks]>=0.28.0",
        "mcp[cli]>=1.21.2",
        "pydantic>=2.0.0",
        "tenacity>=8.0.0",
    }.issubset(set(metadata_payload["requires"]))

    import_surface_code = textwrap.dedent(
        """
        import builtins
        import importlib
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fastmcp" or name.startswith("fastmcp."):
                raise ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        grok_search = importlib.import_module("grok_search")
        config = importlib.import_module("grok_search.config")
        sources = importlib.import_module("grok_search.sources")
        providers = importlib.import_module("grok_search.providers")

        payload = {
            "package_name": grok_search.__name__,
            "config_name": config.__name__,
            "sources_name": sources.__name__,
            "providers_package_name": providers.__name__,
        }

        try:
            grok_search.mcp
        except Exception as exc:
            payload["mcp_error"] = {
                "type": type(exc).__name__,
                "name": getattr(exc, "name", ""),
            }
        else:
            payload["mcp_error"] = {"type": "ok"}

        print(json.dumps(payload))
        """
    )
    import_surface_result = _run_subprocess(
        [str(venv_python), "-c", import_surface_code],
        cwd=ROOT_DIR,
    )
    assert import_surface_result.returncode == 0, import_surface_result.stderr
    import_payload = json.loads(import_surface_result.stdout)
    assert import_payload["package_name"] == "grok_search"
    assert import_payload["config_name"] == "grok_search.config"
    assert import_payload["sources_name"] == "grok_search.sources"
    assert import_payload["providers_package_name"] == "grok_search.providers"
    assert import_payload["mcp_error"] == {
        "type": "ModuleNotFoundError",
        "name": "fastmcp",
    }


def test_built_local_distributions_include_wheel_and_sdist(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build local distribution artifacts")

    dist_dir = tmp_path / "dist"
    build_result = _run_subprocess(
        [uv, "build", "--wheel", "--sdist", "--out-dir", str(dist_dir)],
        cwd=ROOT_DIR,
        timeout=180,
    )
    assert build_result.returncode == 0, build_result.stderr

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1


def test_built_local_wheel_supports_direct_artifact_import_surface(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build a local wheel artifact")

    dist_dir = tmp_path / "dist"
    build_result = _run_subprocess(
        [uv, "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=ROOT_DIR,
        timeout=180,
    )
    assert build_result.returncode == 0, build_result.stderr

    wheels = sorted(dist_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel_path = wheels[0]

    import_code = textwrap.dedent(
        """
        import importlib
        import json
        import sys

        sys.path.insert(0, %(wheel_path)r)

        planning = importlib.import_module("grok_search.planning")
        server = importlib.import_module("grok_search.server")

        payload = {
            "planning_module": planning.__name__,
            "server_module": server.__name__,
            "main_callable": callable(server.main),
            "main_module": getattr(server.main, "__module__", ""),
        }
        print(json.dumps(payload))
        """
        % {"wheel_path": str(wheel_path)}
    )
    result = _run_subprocess([sys.executable, "-c", import_code], cwd=ROOT_DIR)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "planning_module": "grok_search.planning",
        "server_module": "grok_search.server",
        "main_callable": True,
        "main_module": "grok_search.server",
    }


def test_readme_install_snippets_match_distribution_contract():
    readme = README.read_text(encoding="utf-8")
    readme_en = README_EN.read_text(encoding="utf-8")
    readme_zh_tw = README_ZH_TW.read_text(encoding="utf-8")
    readme_ja = README_JA.read_text(encoding="utf-8")
    readme_ru = README_RU.read_text(encoding="utf-8")

    expected_repo = "git+https://github.com/Boulea7/GrokSearchTool@main"
    expected_executable = "grok-search"
    expected_env_keys = {
        "GROK_API_URL",
        "GROK_API_KEY",
        "TAVILY_API_KEY",
        "TAVILY_API_URL",
        "FIRECRAWL_API_KEY",
    }

    claude_payloads = [
        _extract_claude_add_json_payload(readme, "### 一键安装"),
        _extract_claude_add_json_payload(readme_en, "### Add as an MCP server"),
        _extract_claude_add_json_payload(readme_zh_tw, "### 安裝為 MCP"),
        _extract_claude_add_json_payload(readme_ja, "### MCP として追加"),
        _extract_claude_add_json_payload(readme_ru, "### Добавление как MCP"),
    ]
    for payload in claude_payloads:
        assert payload["command"] == "uvx"
        assert payload["args"] == ["--from", expected_repo, expected_executable]
        assert set(payload["env"]) == expected_env_keys

    codex_blocks = [
        _extract_toml_block_after_marker(readme, "#### Codex CLI / Codex 风格 MCP 客户端"),
        _extract_toml_block_after_marker(readme_en, "#### Codex CLI / Codex-style clients"),
        _extract_toml_block_after_marker(readme_zh_tw, "#### Codex CLI / Codex 風格 MCP 客戶端"),
        _extract_toml_block_after_marker(readme_ja, "#### Codex CLI / Codex-style clients"),
        _extract_toml_block_after_marker(readme_ru, "#### Codex CLI / клиенты в стиле Codex"),
    ]
    for block in codex_blocks:
        assert _extract_toml_string(block, "command") == "uvx"
        assert _extract_toml_array(block, "args") == ["--from", expected_repo, expected_executable]
        assert _extract_toml_env_keys(block) == expected_env_keys

    cherry_payloads = [
        _extract_json_block_after_marker(readme, "#### Cherry Studio"),
        _extract_json_block_after_marker(readme_en, "#### Cherry Studio"),
        _extract_json_block_after_marker(readme_zh_tw, "#### Cherry Studio"),
        _extract_json_block_after_marker(readme_ja, "#### Cherry Studio"),
        _extract_json_block_after_marker(readme_ru, "#### Cherry Studio"),
    ]
    for payload in cherry_payloads:
        assert payload["name"] == "grok-search"
        assert payload["command"] == "uvx"
        assert payload["args"] == ["--from", expected_repo, expected_executable]
        assert set(payload["env"]) == expected_env_keys


def test_from_import_base_provider_does_not_trigger_grok_provider_dependencies():
    code = textwrap.dedent(
        """
        import builtins
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tenacity" or name.startswith("tenacity."):
                raise ModuleNotFoundError("No module named 'tenacity'", name="tenacity")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        try:
            from grok_search.providers import BaseSearchProvider
        except Exception as exc:
            payload = {
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }
        else:
            payload = {"type": "ok", "name": BaseSearchProvider.__name__}

        print(json.dumps(payload))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {"type": "ok", "name": "BaseSearchProvider"}


def test_from_import_provider_lazy_export_matches_dependency_failure_contract():
    code = textwrap.dedent(
        """
        import builtins
        import json

        original_import = builtins.__import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tenacity" or name.startswith("tenacity."):
                raise ModuleNotFoundError("No module named 'tenacity'", name="tenacity")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = blocked_import

        try:
            from grok_search.providers import GrokSearchProvider
        except Exception as exc:
            payload = {
                "type": type(exc).__name__,
                "message": str(exc),
                "name": getattr(exc, "name", ""),
            }
        else:
            payload = {"type": "ok", "name": GrokSearchProvider.__name__}

        print(json.dumps(payload))
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["type"] == "ModuleNotFoundError"
    assert payload["name"] == "tenacity"


def test_from_import_provider_lazy_export_works_when_dependencies_available():
    pytest.importorskip("tenacity")

    code = "from grok_search.providers import GrokSearchProvider; print(GrokSearchProvider.__name__)"
    result = _run_python(code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "GrokSearchProvider"
