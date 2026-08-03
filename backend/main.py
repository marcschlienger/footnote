import uuid
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import config
from models.schemas import (
    ResearchRequest,
    ResearchResponse,
    JobStatus,
    JobStatusEnum,
)
from services.parallel_ai import search_parallel_ai
from services.firecrawl import scrape_urls
from services.notion import save_to_notion
from services.notifications import send_push_notification

app = FastAPI(
    title="Research Assistant API",
    description="Automated research orchestration tool",
    version="1.0.0",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job storage (use database for production)
jobs: dict[str, dict] = {}


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"message": "Research Assistant API", "status": "running"}


@app.get("/vapid-public-key")
async def get_vapid_public_key():
    """Get VAPID public key for push notification subscription."""
    if not config.VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=500, detail="VAPID key not configured")
    return {"publicKey": config.VAPID_PUBLIC_KEY}


@app.post("/research", response_model=ResearchResponse)
async def start_research(request: ResearchRequest, background_tasks: BackgroundTasks):
    """Start a new research job."""
    job_id = str(uuid.uuid4())

    # Initialize job status
    jobs[job_id] = {
        "status": JobStatusEnum.PENDING,
        "question": request.question,
        "progress": "Job created",
        "result": None,
        "error": None,
        "subscription": request.subscription,
    }

    # Start background task
    background_tasks.add_task(run_research, job_id, request.question, request.subscription)

    return ResearchResponse(
        status="started",
        message="Research job started",
        job_id=job_id,
    )


@app.get("/research/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    """Get the status of a research job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    return JobStatus(
        status=job["status"],
        question=job["question"],
        progress=job.get("progress"),
        result=job.get("result"),
        error=job.get("error"),
    )


async def run_research(job_id: str, question: str, subscription):
    """Background task to run the full research workflow."""
    try:
        # Step 1: Search with Parallel.ai
        jobs[job_id]["status"] = JobStatusEnum.SEARCHING
        jobs[job_id]["progress"] = "Performing deep search..."

        search_result = await search_parallel_ai(question)

        if not search_result:
            raise Exception("Search returned no results")

        # Step 2: Crawl references with Firecrawl
        jobs[job_id]["status"] = JobStatusEnum.CRAWLING
        jobs[job_id]["progress"] = f"Crawling {len(search_result.urls)} references..."

        scraped_pages = []
        if search_result.urls:
            scraped_pages = await scrape_urls(search_result.urls)

        # Step 3: Save to Notion
        jobs[job_id]["status"] = JobStatusEnum.SAVING
        jobs[job_id]["progress"] = "Saving results to Notion..."

        scraped_content = [
            {
                "title": page.title,
                "url": page.url,
                "markdown": page.markdown,
                "success": page.success,
            }
            for page in scraped_pages
        ]

        notion_url = await save_to_notion(
            question=question,
            summary=search_result.summary,
            scraped_content=scraped_content,
            source_urls=search_result.urls,
        )

        # Step 4: Mark as completed
        jobs[job_id]["status"] = JobStatusEnum.COMPLETED
        jobs[job_id]["progress"] = "Research complete"
        jobs[job_id]["result"] = notion_url

        # Step 5: Send push notification
        if subscription:
            send_push_notification(
                subscription=subscription,
                title="Research Complete",
                body=f"Your research on '{question[:50]}...' is ready!",
                url=notion_url,
            )

    except Exception as e:
        jobs[job_id]["status"] = JobStatusEnum.FAILED
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["progress"] = f"Failed: {str(e)}"

        # Notify user of failure
        if subscription:
            try:
                send_push_notification(
                    subscription=subscription,
                    title="Research Failed",
                    body=f"Error: {str(e)[:100]}",
                )
            except Exception:
                pass  # Don't fail on notification error


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
