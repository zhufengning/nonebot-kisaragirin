from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests
from langchain_core.tools import BaseTool, tool


def build_default_tools(
    fetch_url_content: Callable[[str, int], str],
    *,
    brave_search_api_key: str = "",
    serpapi_api_key: str = "",
) -> list[BaseTool]:
    @tool("read_url")
    def read_url(url: str, max_chars: int = 4000) -> str:
        """Read and extract text from one URL."""

        return fetch_url_content(url=url, max_chars=max_chars)

    tools: list[BaseTool] = []
    brave_api_key = brave_search_api_key.strip()
    if brave_api_key:

        @tool("web_search")
        def web_search(query: str, max_results: int = 5) -> str:
            """Search the web via Brave Search API and return concise results."""

            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": brave_api_key,
                },
                params={
                    "q": query,
                    "count": max(1, min(int(max_results), 20)),
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            web_block = payload.get("web")
            results = web_block.get("results") if isinstance(web_block, dict) else None

            lines: list[str] = []
            if isinstance(results, list):
                for idx, item in enumerate(results[: max(1, max_results)], start=1):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    url = str(item.get("url") or "").strip()
                    description = str(item.get("description") or "").strip()
                    if not title and not url and not description:
                        continue
                    parts = [f"{idx}."]
                    if title:
                        parts.append(title)
                    if url:
                        parts.append(f"({url})")
                    if description:
                        parts.append(f"- {description}")
                    lines.append(" ".join(parts))

            if not lines:
                return "No concise web search result from Brave Search API."
            return "\n".join(lines)

        tools.append(web_search)

    scholar_api_key = serpapi_api_key.strip()
    if scholar_api_key:

        @tool("scholar_search")
        def scholar_search(
            query: str,
            max_results: int = 5,
            year_from: int | None = None,
            year_to: int | None = None,
        ) -> str:
            """Search academic papers via SerpApi Google Scholar."""

            params: dict[str, Any] = {
                "engine": "google_scholar",
                "api_key": scholar_api_key,
                "q": query,
                "num": max(1, min(int(max_results), 20)),
            }
            if year_from is not None:
                params["as_ylo"] = int(year_from)
            if year_to is not None:
                params["as_yhi"] = int(year_to)

            response = requests.get(
                "https://serpapi.com/search.json",
                params=params,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("organic_results")

            lines: list[str] = []
            if isinstance(results, list):
                for idx, item in enumerate(results[: max(1, max_results)], start=1):
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    link = str(item.get("link") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    publication_info = item.get("publication_info")
                    summary = ""
                    if isinstance(publication_info, dict):
                        summary = str(publication_info.get("summary") or "").strip()
                    cited_total = _extract_cited_by(item)

                    parts = [f"{idx}."]
                    if title:
                        parts.append(title)
                    if link:
                        parts.append(f"({link})")
                    if summary:
                        parts.append(f"- {summary}")
                    if snippet:
                        parts.append(f"- {snippet}")
                    if cited_total is not None:
                        parts.append(f"- cited_by={cited_total}")

                    if len(parts) > 1:
                        lines.append(" ".join(parts))

            if not lines:
                return "No Google Scholar result from SerpApi."
            return "\n".join(lines)

        tools.append(scholar_search)

    tools.append(read_url)
    return tools


def _extract_cited_by(item: dict[str, Any]) -> int | None:
    cited_by = item.get("cited_by")
    if isinstance(cited_by, dict):
        total = cited_by.get("value")
        if isinstance(total, int):
            return total
    inline_links = item.get("inline_links")
    if isinstance(inline_links, dict):
        cited_by_inline = inline_links.get("cited_by")
        if isinstance(cited_by_inline, dict):
            total = cited_by_inline.get("total")
            if isinstance(total, int):
                return total
    return None
