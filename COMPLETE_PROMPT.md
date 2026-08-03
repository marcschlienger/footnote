# Complete Project Prompt: Research Assistant PWA

## Project Request

Build a Progressive Web Application (PWA) that automates research workflows by orchestrating multiple APIs. The application should:

1. **Accept research questions** from users through a web interface
2. **Perform deep searches** using the Parallel.ai API
3. **Crawl and extract content** from all references found in the search results using the Firecrawl.dev API (in markdown format)
4. **Save organized results** to a note-taking application via its API
5. **Send push notifications** to inform users when research is complete

## Technical Requirements

### Platform Support
- macOS (desktop browser)
- Linux (desktop browser)
- iPadOS (Safari)
- iOS (Safari)

### Backend Requirements
- **Framework:** FastAPI (Python)
- **Purpose:** 
  - Orchestrate all external API calls
  - Handle asynchronous workflows
  - Manage job status tracking
  - Send push notifications
- **Architecture:** RESTful API with background task processing

### Frontend Requirements
- **Type:** Progressive Web App (PWA)
- **Features:**
  - Responsive design for mobile and desktop
  - Service Worker for offline capability
  - Push notification support
  - Simple, clean user interface
  - Real-time status updates

### External API Integrations

#### 1. Parallel.ai API
- **Purpose:** Deep search and research
- **Usage:** Send research question, receive comprehensive search results
- **Authentication:** API key-based

#### 2. Firecrawl.dev API
- **Purpose:** Web scraping and content extraction
- **Usage:** Crawl URLs from search results, extract content as markdown
- **Authentication:** API key-based

#### 3. Note-Taking App API
**Recommended: Notion**
- **Purpose:** Store and organize research results
- **Alternative Options:** Obsidian (with Local REST API plugin), Logseq, Evernote
- **Requirements:** 
  - Must support API access
  - Should handle markdown content
  - Must be available on all target platforms

## Application Workflow

### User Journey
1. User opens PWA on any supported device
2. User enters a research question in a text field
3. User submits the question
4. System immediately returns a job ID and confirmation
5. System processes research in the background:
   - Performs deep search via Parallel.ai
   - Extracts URLs from search results
   - Crawls each URL via Firecrawl.dev
   - Formats content as markdown
   - Creates organized note with all findings
   - Saves to note-taking app
6. User receives push notification when complete
7. User can access results in their note-taking app

### Technical Flow
1. Frontend sends POST request to `/research` endpoint with:
   - Research question
   - Push notification subscription data
2. Backend creates job and returns job ID
3. Backend starts background task that:
   - Calls Parallel.ai API for deep search
   - Parses results to extract reference URLs
   - Calls Firecrawl.dev API for each URL (concurrent requests)
   - Collects all markdown content
   - Structures data for note-taking app
   - Saves via note-taking app API
   - Sends push notification to user
4. Frontend can poll `/research/{job_id}` for status updates

## Project Structure

```
research-assistant/
├── backend/
│   ├── main.py                    # FastAPI application
│   ├── requirements.txt           # Python dependencies
│   ├── .env                       # Environment variables (API keys)
│   ├── config.py                  # Configuration
│   ├── services/
│   │   ├── parallel_ai.py         # Parallel.ai integration
│   │   ├── firecrawl.py           # Firecrawl integration
│   │   ├── notion.py              # Notion integration (or alternative)
│   │   └── notifications.py      # Push notification service
│   └── models/
│       └── schemas.py             # Pydantic models
└── frontend/
    ├── index.html                 # Main interface
    ├── app.js                     # Application logic
    ├── style.css                  # Styling
    ├── manifest.json              # PWA manifest
    ├── service-worker.js          # Service worker
    └── assets/
        ├── icon-192.png           # PWA icon
        └── icon-512.png           # PWA icon
```

## API Endpoints

### POST `/research`
Start a new research job

**Request:**
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
Get job status

**Response:**
```json
{
  "status": "processing",
  "question": "...",
  "progress": "Crawling references..."
}
```

## Required Dependencies

### Backend (requirements.txt)
```
fastapi
uvicorn
httpx
python-dotenv
pydantic
aiohttp
pywebpush
```

### Environment Variables
```
PARALLEL_AI_API_KEY=your_key
FIRECRAWL_API_KEY=your_key
NOTION_API_KEY=your_key
NOTION_DATABASE_ID=your_db_id
VAPID_PUBLIC_KEY=your_vapid_public
VAPID_PRIVATE_KEY=your_vapid_private
VAPID_CLAIM_EMAIL=your_email
```

## Setup Instructions Summary

### 1. Generate VAPID Keys for Push Notifications
```bash
pip install py-vapid --break-system-packages
vapid --gen
```

### 2. Set Up Note-Taking App (Notion Example)
- Create integration at notion.so/my-integrations
- Create a database for research notes
- Share database with integration
- Copy database ID from URL

### 3. Obtain API Keys
- Sign up for Parallel.ai and get API key
- Sign up for Firecrawl.dev and get API key
- Create note-taking app integration

### 4. Install Backend Dependencies
```bash
cd backend
pip install -r requirements.txt --break-system-packages
```

### 5. Configure Environment
- Copy all API keys to `.env` file
- Set VAPID keys
- Set note-taking app credentials

### 6. Run Backend
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 7. Serve Frontend
```bash
cd frontend
python -m http.server 8080
```

### 8. Test Locally
- Open browser to `http://localhost:8080`
- Grant notification permissions
- Submit a test research question
- Verify workflow completion

## Implementation Details

### Key Features to Implement

#### 1. Asynchronous Processing
- All external API calls must be async
- Use httpx.AsyncClient for HTTP requests
- Use FastAPI's BackgroundTasks for long-running operations

#### 2. Error Handling
- Graceful handling of API failures
- Retry logic for transient errors
- User notification of failures via push

#### 3. Push Notifications
- Web Push API implementation
- VAPID protocol for authentication
- Notification on success and failure

#### 4. Content Organization
- Structure notes with:
  - Research question as title
  - Search summary
  - Individual reference sections
  - Source URLs
  - Timestamps

#### 5. CORS Configuration
- Allow frontend origin
- Support credentials
- Configure for production domain

### Security Considerations
- Never expose API keys in frontend
- Use environment variables for secrets
- Implement rate limiting on endpoints
- Validate all user inputs
- Require HTTPS in production (for PWA features)

## Deployment Strategy

### Backend Deployment
**Options:**
1. Render.com, Railway.app, or Fly.io (easiest)
2. Docker container on any platform
3. Traditional VPS with Nginx + systemd

**Requirements:**
- Python 3.9+ support
- Environment variable configuration
- HTTPS endpoint

### Frontend Deployment
**Options:**
1. Netlify, Vercel, or GitHub Pages
2. Same server as backend (serve static files)
3. CDN with static hosting

**Requirements:**
- HTTPS (required for service workers and push)
- Update API_URL to point to deployed backend
- Configure appropriate CORS headers on backend

## Success Criteria

The project is successful when:
1. ✅ Users can submit research questions from any supported device
2. ✅ System performs deep search via Parallel.ai
3. ✅ All references are automatically crawled via Firecrawl
4. ✅ Content is saved to note-taking app in organized format
5. ✅ Users receive push notifications on completion
6. ✅ PWA works offline (for UI, not API calls)
7. ✅ Application is installable on all target platforms
8. ✅ Error handling provides clear feedback
9. ✅ Documentation is complete and clear

## Recommended Note-Taking App

**Primary Recommendation: Notion**

Reasons:
- Excellent API documentation
- Rich formatting support (markdown, embeds, etc.)
- Database functionality for organization
- Great mobile apps for iOS and iPadOS
- Free tier available
- Reliable and well-maintained

Setup:
1. Create free Notion account
2. Create integration at notion.so/my-integrations
3. Create database with appropriate properties
4. Share database with integration
5. Use API to create pages in database

**Alternatives:**
- Obsidian: Local-first, requires Local REST API plugin
- Logseq: Open-source, graph-based
- Evernote: Established, good mobile support

## Development Phases

### Phase 1: MVP (Minimal Viable Product)
- Basic PWA interface
- FastAPI backend with core endpoints
- Parallel.ai integration
- Firecrawl integration
- Notion integration
- Push notifications
- Basic error handling

### Phase 2: Enhancement
- Improved UI/UX
- Job status polling
- Better error messages
- Loading indicators
- Result preview in app

### Phase 3: Production Ready
- User authentication
- Research history
- Database for job persistence
- Rate limiting
- Monitoring and logging
- Comprehensive testing

## Testing Checklist

- [ ] Submit research question via UI
- [ ] Verify job ID returned immediately
- [ ] Confirm Parallel.ai API called correctly
- [ ] Verify URLs extracted from search results
- [ ] Confirm Firecrawl scrapes all URLs
- [ ] Check markdown format of scraped content
- [ ] Verify note created in Notion (or chosen app)
- [ ] Confirm push notification received
- [ ] Test on macOS Safari
- [ ] Test on iOS Safari
- [ ] Test on iPadOS Safari
- [ ] Test on Linux Chrome/Firefox
- [ ] Verify PWA installation works
- [ ] Test offline capabilities
- [ ] Verify error handling for API failures

## Additional Considerations

### Performance
- Implement concurrent crawling of multiple URLs
- Set appropriate timeout values
- Consider caching for repeated queries
- Monitor API rate limits

### User Experience
- Provide clear feedback during processing
- Show estimated completion time
- Allow cancellation of jobs
- Display partial results as they arrive

### Scalability
- Use proper database for job storage (Redis, PostgreSQL)
- Implement job queue (Celery, RQ)
- Add horizontal scaling capability
- Monitor and optimize API costs

## Documentation Requirements

Create documentation for:
1. API endpoints (OpenAPI/Swagger automatic with FastAPI)
2. Setup and installation guide
3. Configuration guide for all API keys
4. Deployment guide for both frontend and backend
5. User guide for the PWA
6. Troubleshooting common issues

## Support Resources

- FastAPI Documentation: https://fastapi.tiangolo.com/
- Web Push API: https://developer.mozilla.org/en-US/docs/Web/API/Push_API
- PWA Documentation: https://web.dev/progressive-web-apps/
- Notion API: https://developers.notion.com/
- Service Workers: https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API

---

This prompt provides a complete specification for building the Research Assistant PWA. Follow the structure, implement the features as described, and refer to the technical requirements for implementation details.
