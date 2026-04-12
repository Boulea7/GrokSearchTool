from abc import ABC, abstractmethod
import inspect
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
        parameters = inspect.signature(self.search).parameters
        supported_kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        return await self.search(query, **supported_kwargs), []

    @abstractmethod
    def get_provider_name(self) -> str:
        pass
