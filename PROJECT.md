# Research Assistant - Project Documentation

## Project Overview

**Name:** Research Assistant PWA  
**Version:** 1.0.0  
**Type:** Progressive Web Application with FastAPI Backend  
**Purpose:** Automated research orchestration tool that accepts research questions, performs deep searches, crawls references, saves results to a note-taking app, and sends push notifications

## Problem Statement

Conducting thorough research requires:
1. Searching across multiple sources
2. Reading and extracting information from numerous web pages
3. Organizing findings in a structured format
4. Tracking when research is complete

This process is time-consuming and repetitive. This application automates the entire workflow.

## Solution

A cross-platform PWA that orchestrates multiple APIs to:
- Accept research questions from users
- Perform deep AI-powered searches via Parallel.ai
- Automatically crawl and extract content from references using Firecrawl
- Save organized results to a note-taking application
- Notify users via push notifications when research is complete

## Target Platforms

- macOS (Desktop browser + Safari)
- Linux (Desktop browser)
- iPadOS (Safari)
- iOS (Safari)

## Tech Stack

### Frontend
- **Core:** HTML5, CSS3, JavaScript (vanilla or React/Vue/Svelte)
- **PWA Features:** Service Workers, Web Push API, Cache API
- **UI Framework:** Optional (TailwindCSS, Bootstrap, or custom)

### Backend
- **Framework:** FastAPI (Python 3.9+)
- **Async HTTP Client:** httpx
- **Push Notifications:** pywebpush
- **Environment Management:** python-dotenv
- **Data Validation:** Pydantic

### External APIs
1. **Parallel.ai** - Deep search and research
2. **Firecrawl.dev** - Web scraping and content extraction in markdown
3. **Notion API** (recommended) - Note storage and organization
   - Alternatives: Obsidian (with plugin), Logseq, Evernote

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         PWA Frontend                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │   UI Form   │  │Service Worker│  │ Push Notifications│   │
│  └─────────────┘  └──────────────┘  └──────────────────┘   │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTPS/REST
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Backend                           │
│  ┌──────────────┐  ┌───────────────┐  ┌─────────────────┐  │
│  │API Endpoints │  │Background Tasks│  │ Service Layer   │  │
│  └──────────────┘  └───────────────┘  └─────────────────┘  │
└────────┬────────────────┬────────────────┬──────────────────┘
         │                │                │
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Parallel.ai  │  │ Firecrawl.dev│  │ Notion API   │
│   (Search)   │  │  (Crawling)  │  │   (Storage)  │
└──────────────┘  └──────────────┘  └──────────────┘
```

## Project Structure

```
research-assistant/
├── README.md
├── PROJECT.md
├── .gitignore
├── backend/
│   ├── main.py                    # FastAPI application entry point
│   ├── requirements.txt           # Python dependencies
│   ├── .env.example              # Environment variables template
│   ├── .env                      # Environment variables (not in git)
│   ├── config.py                 # Configuration management
│   ├── services/                 # External API integrations
│   │   ├── __init__.py
│   │   ├── parallel_ai.py        # Parallel.ai API client
│   │   ├── firecrawl.py          # Firecrawl API client
│   │   ├── notion.py             # Notion API client
│   │   └── notifications.py      # Push notification service
│   ├── models/                   # Data models
│   │   ├── __init__.py
│   │   └── schemas.py            # Pydantic models
│   └── tests/                    # Backend tests
│       ├── __init__.py
│       └── test_api.py
├── frontend/
│   ├── index.html                # Main PWA interface
│   ├── app.js                    # Application logic
│   ├── style.css                 # Styling
│   ├── manifest.json             # PWA manifest
│   ├── service-worker.js         # Service worker for offline & push
│   ├── assets/                   # Icons and images
│   │   ├── icon-192.png
│   │   └── icon-512.png
│   └── config.js                 # Frontend configuration
└── docs/
    ├── API.md                    # API documentation
    ├── DEPLOYMENT.md             # Deployment guide
    └── SETUP.md                  # Setup instructions
```

## Core Features

### 1. Research Question Submission
- User-friendly textarea input
- Form validation
- Real-time status updates

### 2. Deep Search via Parallel.ai
- Asynchronous API calls
- Comprehensive search with configurable depth
- Result parsing and structuring

### 3. Reference Crawling via Firecrawl
- Extract URLs from search results
- Scrape content in markdown format
- Handle multiple URLs concurrently
- Error handling for failed scrapes

### 4. Note Storage
- Create structured notes with:
  - Research question as title
  - Search results summary
  - Full crawled content from references
  - Metadata (timestamp, sources)
- Support for markdown formatting

### 5. Push Notifications
- Browser push notifications
- Notification on research completion
- Error notifications
- Click-to-open functionality

### 6. Job Status Tracking
- Unique job IDs
- Status endpoint for polling
- States: pending, processing, completed, failed

## API Endpoints

### POST `/research`
Start a new research job.

**Request Body:**
```json
{
  "question": "What are the latest developments in quantum computing?",
  "subscription": {
    "endpoint": "...",
    "keys": {...}
  }
}
```

**Response:**
```json
{
  "status": "started",
  "message": "Research job started",
  "job_id": "job_123"
}
```

### GET `/research/{job_id}`
Check the status of a research job.

**Response:**
```json
{
  "status": "processing",
  "question": "What are the latest developments in quantum computing?"
}
```

### GET `/`
Health check endpoint.

**Response:**
```json
{
  "message": "Research Assistant API"
}
```

## Data Flow

1. **User submits research question** → Frontend validates and sends to backend
2. **Backend creates job** → Returns job ID, starts background task
3. **Background task orchestrates:**
   - Call Parallel.ai for deep search
   - Extract URLs from results
   - Call Firecrawl for each URL (concurrent)
   - Format results for note-taking app
   - Save to Notion (or chosen app)
   - Send push notification
4. **User receives notification** → Can view results in note-taking app

## Environment Variables

```bash
# Backend API Keys
PARALLEL_AI_API_KEY=your_parallel_ai_key
FIRECRAWL_API_KEY=your_firecrawl_key
NOTION_API_KEY=your_notion_key
NOTION_DATABASE_ID=your_database_id

# Push Notifications (VAPID)
VAPID_PUBLIC_KEY=your_public_key
VAPID_PRIVATE_KEY=your_private_key
VAPID_CLAIM_EMAIL=your_email@example.com

# Optional
API_RATE_LIMIT=100
LOG_LEVEL=INFO
```

## Note-Taking App Options

### Recommended: Notion
**Pros:**
- Excellent API documentation
- Rich formatting support
- Database functionality
- Free tier available
- Great mobile apps

**Setup:**
1. Create integration at notion.so/my-integrations
2. Create a database for research
3. Share database with integration
4. Copy database ID from URL

### Alternative: Obsidian
**Pros:**
- Local-first, privacy-focused
- Markdown native
- Powerful linking

**Setup:**
1. Install Local REST API plugin
2. Configure API endpoint
3. Enable CORS

### Alternative: Logseq
**Pros:**
- Open-source
- Graph-based
- Local or cloud sync

**Setup:**
1. Enable API server in settings
2. Configure authentication

## Security Considerations

1. **API Keys:** Never commit to git, use .env files
2. **CORS:** Configure appropriately for production
3. **HTTPS:** Required for PWA features and push notifications
4. **Input Validation:** Sanitize all user inputs
5. **Rate Limiting:** Implement on backend endpoints
6. **Authentication:** Consider adding user auth for production

## Development Setup

### Prerequisites
- Python 3.9+
- pip
- Modern web browser with PWA support
- API keys for external services

### Backend Setup
```bash
cd backend
pip install -r requirements.txt --break-system-packages
cp .env.example .env
# Edit .env with your API keys
uvicorn main:app --reload
```

### Frontend Setup
```bash
cd frontend
# Serve with any static server
python -m http.server 8080
# Or use VS Code Live Server
```

### Generate VAPID Keys
```bash
pip install py-vapid --break-system-packages
vapid --gen
# Copy keys to .env
```

## Testing

### Backend Tests
```bash
cd backend
pytest tests/
```

### Manual Testing Checklist
- [ ] Submit research question
- [ ] Receive job ID
- [ ] Check job status endpoint
- [ ] Verify Parallel.ai search call
- [ ] Verify Firecrawl scraping
- [ ] Verify note creation in Notion
- [ ] Receive push notification
- [ ] Click notification opens app

## Deployment

### Backend Deployment Options

#### Option 1: Render/Railway/Fly.io
- Easy deployment from Git
- Automatic HTTPS
- Environment variable management
- Built-in monitoring

#### Option 2: Docker Container
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### Option 3: VPS (DigitalOcean, Linode)
- Full control
- Run with systemd service
- Nginx reverse proxy
- Let's Encrypt SSL

### Frontend Deployment
- **Static Hosting:** Netlify, Vercel, GitHub Pages
- **Requirements:**
  - HTTPS (required for service workers)
  - Update API_URL in config
  - Configure CORS on backend

## Performance Considerations

1. **Async Operations:** All external API calls are async
2. **Background Tasks:** Long-running tasks don't block responses
3. **Caching:** Consider caching search results (Redis)
4. **Rate Limiting:** Respect API rate limits
5. **Timeout Handling:** Configure appropriate timeouts

## Monitoring & Logging

- FastAPI automatic OpenAPI docs at `/docs`
- Log all API calls with timestamps
- Track job success/failure rates
- Monitor external API response times
- Set up alerts for errors

## Future Enhancements

### Phase 2
- [ ] User authentication and accounts
- [ ] Research history and saved queries
- [ ] Custom search parameters
- [ ] Multiple note-taking app support
- [ ] Email notifications option

### Phase 3
- [ ] AI-powered summarization of results
- [ ] Collaborative research (sharing)
- [ ] Schedule recurring research queries
- [ ] Export results to PDF/DOCX
- [ ] Browser extension

### Phase 4
- [ ] Mobile native apps (React Native/Flutter)
- [ ] Offline mode with sync
- [ ] Advanced search filters
- [ ] Citation management
- [ ] Integration with reference managers

## Known Limitations

1. **API Dependencies:** Relies on third-party API availability
2. **Rate Limits:** Subject to external API rate limits
3. **Storage:** Job status stored in memory (use database for production)
4. **Push Notifications:** Requires user permission and HTTPS
5. **Browser Support:** PWA features vary by browser

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## License

MIT License (or your choice)

## Support & Contact

- **Documentation:** [Link to docs]
- **Issues:** [GitHub Issues link]
- **Email:** [Your email]

## Changelog

### Version 1.0.0 (Initial Release)
- Basic research automation workflow
- Parallel.ai integration
- Firecrawl integration
- Notion integration
- Push notifications
- Cross-platform PWA support

---

**Last Updated:** January 16, 2026  
**Maintained By:** [Your Name/Team]
