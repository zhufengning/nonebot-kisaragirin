from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests
from langchain_core.tools import BaseTool, tool


def build_default_tools(
    fetch_url_content: Callable[[str, int], str],
    *,
    exa_api_key: str = "",
    brave_search_api_key: str = "",
    serpapi_api_key: str = "",
) -> list[BaseTool]:
    @tool("read_url")
    def read_url(url: str, max_chars: int = 4000) -> str:
        """Read and extract text from one URL."""

        return fetch_url_content(url=url, max_chars=max_chars)

    tools: list[BaseTool] = []
    exa_key = exa_api_key.strip()
    brave_api_key = brave_search_api_key.strip()
    if exa_key:

        def _exa_search_impl(query: str, max_results: int, max_chars_per_result: int) -> str:
            try:
                from exa_py import Exa
            except Exception as exc:
                return f"Exa SDK is unavailable: {exc}"
            limit = max(1, min(int(max_results), 20))
            max_chars = max(200, min(int(max_chars_per_result), 4000))
            exa = Exa(api_key=exa_key)
            response = exa.search_and_contents(
                query,
                num_results=limit,
                text={"max_characters": max_chars},
            )
            results = getattr(response, "results", None)

            lines: list[str] = []
            if isinstance(results, list):
                for idx, item in enumerate(results[:limit], start=1):
                    title = str(getattr(item, "title", "") or "").strip()
                    url = str(getattr(item, "url", "") or "").strip()
                    text = str(getattr(item, "text", "") or "").strip()

                    parts = [f"{idx}."]
                    if title:
                        parts.append(title)
                    if url:
                        parts.append(f"({url})")
                    if text:
                        parts.append(f"- {_compact_text(text, max_chars=260)}")
                    if len(parts) > 1:
                        lines.append(" ".join(parts))

            if not lines:
                return "No concise web search result from Exa."
            return "\n".join(lines)

        @tool("exa_search")
        def exa_search(query: str, max_results: int = 5, max_chars_per_result: int = 600) -> str:
            """Search the web via Exa and return concise results."""

            return _exa_search_impl(query, max_results, max_chars_per_result)

        @tool("web_search")
        def web_search(query: str, max_results: int = 5, max_chars_per_result: int = 600) -> str:
            """Alias of exa_search for backward compatibility."""

            return _exa_search_impl(query, max_results, max_chars_per_result)

        tools.append(exa_search)
        tools.append(web_search)
    elif brave_api_key:

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


def _compact_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
