import httpx
from datetime import datetime
from typing import Optional
from config import config


async def save_to_notion(
    question: str,
    summary: str,
    scraped_content: list[dict],
    source_urls: list[str],
) -> Optional[str]:
    """
    Save research results to Notion database.

    Args:
        question: The research question
        summary: Search summary from Parallel.ai
        scraped_content: List of dicts with 'title', 'url', 'markdown' keys
        source_urls: List of source URLs

    Returns:
        URL of created Notion page, or None on failure
    """
    if not config.NOTION_API_KEY or not config.NOTION_DATABASE_ID:
        raise ValueError("NOTION_API_KEY or NOTION_DATABASE_ID not configured")

    # Build page content blocks
    children = []

    # Add summary section
    children.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "Summary"}}]
        }
    })

    # Split summary into paragraphs (Notion has 2000 char limit per block)
    summary_chunks = _split_text(summary, 2000)
    for chunk in summary_chunks:
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            }
        })

    # Add sources section
    children.append({
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "Sources"}}]
        }
    })

    # Add scraped content for each source
    for item in scraped_content:
        if not item.get("success", True):
            continue

        # Add source heading
        children.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {"content": item.get("title", "Source")[:100]}}]
            }
        })

        # Add source URL
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {
                        "content": item.get("url", ""),
                        "link": {"url": item.get("url", "")} if item.get("url") else None
                    }
                }]
            }
        })

        # Add content (limited to avoid Notion API limits)
        content = item.get("markdown", "")[:10000]
        content_chunks = _split_text(content, 2000)
        for chunk in content_chunks[:5]:  # Max 5 blocks per source
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk}}]
                }
            })

        # Add divider
        children.append({
            "object": "block",
            "type": "divider",
            "divider": {}
        })

    # Limit total blocks (Notion API has limits)
    children = children[:100]

    # Create page payload
    payload = {
        "parent": {"database_id": config.NOTION_DATABASE_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": question[:100]}}]
            },
        },
        "children": children,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{config.NOTION_BASE_URL}/pages",
            headers={
                "Authorization": f"Bearer {config.NOTION_API_KEY}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    return data.get("url")


def _split_text(text: str, max_length: int) -> list[str]:
    """Split text into chunks of max_length, trying to break at newlines."""
    if len(text) <= max_length:
        return [text] if text else []

    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 <= max_length:
            current = current + "\n" + line if current else line
        else:
            if current:
                chunks.append(current)
            # Handle lines longer than max_length
            while len(line) > max_length:
                chunks.append(line[:max_length])
                line = line[max_length:]
            current = line

    if current:
        chunks.append(current)

    return chunks
