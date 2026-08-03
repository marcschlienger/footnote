import httpx
import asyncio
from typing import Optional
from dataclasses import dataclass
from config import config


@dataclass
class ScrapedPage:
    url: str
    title: str
    markdown: str
    success: bool
    error: Optional[str] = None


async def scrape_url(client: httpx.AsyncClient, url: str) -> ScrapedPage:
    """Scrape a single URL using Firecrawl API."""
    try:
        response = await client.post(
            f"{config.FIRECRAWL_BASE_URL}/scrape",
            headers={
                "Authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "url": url,
                "formats": ["markdown"],
            },
        )
        response.raise_for_status()
        data = response.json()

        # Extract content from response
        # Adjust based on actual API response structure
        content = data.get("data", data)
        markdown = content.get("markdown", content.get("content", ""))
        title = content.get("metadata", {}).get("title", url)

        return ScrapedPage(
            url=url,
            title=title,
            markdown=markdown,
            success=True,
        )
    except httpx.HTTPStatusError as e:
        return ScrapedPage(
            url=url,
            title=url,
            markdown="",
            success=False,
            error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
        )
    except Exception as e:
        return ScrapedPage(
            url=url,
            title=url,
            markdown="",
            success=False,
            error=str(e),
        )


async def scrape_urls(urls: list[str], max_concurrent: int = 5) -> list[ScrapedPage]:
    """
    Scrape multiple URLs concurrently using Firecrawl API.

    Args:
        urls: List of URLs to scrape
        max_concurrent: Maximum number of concurrent requests

    Returns:
        List of ScrapedPage results
    """
    if not config.FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY not configured")

    results = []
    semaphore = asyncio.Semaphore(max_concurrent)

    async def scrape_with_semaphore(client: httpx.AsyncClient, url: str) -> ScrapedPage:
        async with semaphore:
            return await scrape_url(client, url)

    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [scrape_with_semaphore(client, url) for url in urls]
        results = await asyncio.gather(*tasks)

    return list(results)
