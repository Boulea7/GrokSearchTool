import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"


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
    )


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
