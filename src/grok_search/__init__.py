__all__ = ["mcp"]


def __getattr__(name: str):
    if name != "mcp":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    try:
        from .server import mcp
    except ModuleNotFoundError as exc:
        if exc.name == "fastmcp":
            raise ModuleNotFoundError(
                "fastmcp is required to access grok_search.mcp. "
                "Install fastmcp or import non-server modules directly.",
                name="fastmcp",
            ) from exc
        raise

    return mcp


def __dir__() -> list[str]:
    return sorted(globals().keys() | {"mcp"})
