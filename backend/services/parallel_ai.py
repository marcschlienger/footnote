import httpx
import re
from typing import Optional
from config import config


class ParallelAIResult:
    def __init__(self, summary: str, urls: list[str], raw_response: dict):
        self.summary = summary
        self.urls = urls
        self.raw_response = raw_response


async def search_parallel_ai(question: str) -> Optional[ParallelAIResult]:
    """
    Perform a deep search using Parallel.ai API.

    Returns a ParallelAIResult with the search summary and extracted URLs.
    """
    if not config.PARALLEL_AI_API_KEY:
        raise ValueError("PARALLEL_AI_API_KEY not configured")

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{config.PARALLEL_AI_BASE_URL}/search",
            headers={
                "Authorization": f"Bearer {config.PARALLEL_AI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "query": question,
                "mode": "deep",
            },
        )
        response.raise_for_status()
        data = response.json()

    # Extract summary and URLs from response
    # Adjust based on actual API response structure
    summary = data.get("summary", data.get("answer", ""))

    # Extract URLs from references/sources
    urls = []

    # Try common response structures
    if "references" in data:
        for ref in data["references"]:
            if isinstance(ref, dict) and "url" in ref:
                urls.append(ref["url"])
            elif isinstance(ref, str) and ref.startswith("http"):
                urls.append(ref)

    if "sources" in data:
        for source in data["sources"]:
            if isinstance(source, dict) and "url" in source:
                urls.append(source["url"])
            elif isinstance(source, str) and source.startswith("http"):
                urls.append(source)

    # Also extract URLs from the summary text
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    text_urls = re.findall(url_pattern, summary)
    urls.extend(text_urls)

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    return ParallelAIResult(
        summary=summary,
        urls=unique_urls,
        raw_response=data,
    )
