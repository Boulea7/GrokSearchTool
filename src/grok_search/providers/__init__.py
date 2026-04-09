from .base import BaseSearchProvider, SearchResult

__all__ = ["BaseSearchProvider", "SearchResult", "GrokSearchProvider"]


def __getattr__(name: str):
    if name != "GrokSearchProvider":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from .grok import GrokSearchProvider

    return GrokSearchProvider
