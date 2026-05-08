import inspect
from abc import ABC, abstractmethod
from typing import Any, Dict


class SearchResult:
    def __init__(
        self,
        title: str,
        url: str,
        snippet: str,
        source: str = "",
        published_date: str = "",
    ):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.source = source
        self.published_date = published_date

    def to_dict(self) -> Dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published_date": self.published_date,
        }


def _filter_supported_search_kwargs(method, kwargs: dict[str, Any]) -> dict[str, Any]:
    parameters = inspect.signature(method).parameters.values()
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return kwargs

    supported_names = {
        parameter.name
        for parameter in parameters
        if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return {key: value for key, value in kwargs.items() if key in supported_names}


class BaseSearchProvider(ABC):
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url
        self.api_key = api_key

    @abstractmethod
    async def search(
        self,
        query: str,
        platform: str = "",
        min_results: int = 3,
        max_results: int = 10,
        ctx=None,
    ) -> str:
        pass

    async def search_with_sources(
        self,
        query: str,
        platform: str = "",
        min_results: int = 3,
        max_results: int = 10,
        ctx=None,
    ) -> tuple[str, list[dict[str, Any]]]:
        kwargs = {
            "platform": platform,
            "min_results": min_results,
            "max_results": max_results,
            "ctx": ctx,
        }
        supported_kwargs = _filter_supported_search_kwargs(self.search, kwargs)
        return await self.search(query, **supported_kwargs), []

    @abstractmethod
    def get_provider_name(self) -> str:
        pass
