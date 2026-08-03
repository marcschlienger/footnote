# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Research Assistant PWA - an automated research orchestration tool that:
- Accepts research questions via a web interface
- Performs deep searches using Parallel.ai API
- Crawls references using Firecrawl.dev API (extracts markdown)
- Saves results to Notion (or alternative note-taking app)
- Sends push notifications on completion

## Tech Stack

- **Backend:** FastAPI (Python 3.9+), httpx for async HTTP, pywebpush for notifications
- **Frontend:** PWA with vanilla HTML/CSS/JS, Service Workers, Web Push API
- **External APIs:** Parallel.ai (search), Firecrawl.dev (crawling), Notion API (storage)

## Development Commands

### Backend
```bash
cd backend
pip install -r requirements.txt --break-system-packages
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend
```bash
cd frontend
python -m http.server 8080
```

### Generate VAPID Keys (for push notifications)
```bash
pip install py-vapid --break-system-packages
vapid --gen
```

### Run Tests
```bash
cd backend
pytest tests/
```

## Architecture

```
PWA Frontend (index.html, app.js, service-worker.js)
         │
         │ HTTPS/REST
         ▼
FastAPI Backend (main.py)
    │
    ├── services/parallel_ai.py  → Parallel.ai API
    ├── services/firecrawl.py    → Firecrawl.dev API
    ├── services/notion.py       → Notion API
    └── services/notifications.py → Web Push
```

## Key API Endpoints

- `POST /research` - Start new research job (returns job_id)
- `GET /research/{job_id}` - Check job status
- `GET /` - Health check

## Environment Variables

Required in `backend/.env`:
```
PARALLEL_AI_API_KEY=
FIRECRAWL_API_KEY=
NOTION_API_KEY=
NOTION_DATABASE_ID=
VAPID_PUBLIC_KEY=
VAPID_PRIVATE_KEY=
VAPID_CLAIM_EMAIL=
```

## Implementation Notes

- All external API calls must be async using httpx.AsyncClient
- Use FastAPI BackgroundTasks for long-running research operations
- Job status stored in memory (consider database for production)
- HTTPS required for PWA features and push notifications
- CORS must be configured to allow frontend origin
