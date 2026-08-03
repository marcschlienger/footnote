import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Parallel.ai
    PARALLEL_AI_API_KEY: str = os.getenv("PARALLEL_AI_API_KEY", "")
    PARALLEL_AI_BASE_URL: str = "https://api.parallel.ai/v1"

    # Firecrawl
    FIRECRAWL_API_KEY: str = os.getenv("FIRECRAWL_API_KEY", "")
    FIRECRAWL_BASE_URL: str = "https://api.firecrawl.dev/v1"

    # Notion
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_DATABASE_ID: str = os.getenv("NOTION_DATABASE_ID", "")
    NOTION_BASE_URL: str = "https://api.notion.com/v1"

    # VAPID for push notifications
    VAPID_PUBLIC_KEY: str = os.getenv("VAPID_PUBLIC_KEY", "")
    VAPID_PRIVATE_KEY: str = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_CLAIM_EMAIL: str = os.getenv("VAPID_CLAIM_EMAIL", "")


config = Config()
