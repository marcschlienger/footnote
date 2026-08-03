# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Footnote — ask a question, get a cited research dossier in your notes folder.

POST /research    { "question": "…", "processor": "core" }  → job id
GET  /research/{id}                     job status (poll target)
GET  /jobs                              recent jobs for the UI
GET  /jobs/{id}/report                  rendered report (HTML)
GET  /jobs/{id}/report.md               raw Markdown download
DELETE /jobs/{id}                       remove a job from history
POST /subscribe                         register a Web Push subscription
GET  /vapid-public-key                  key for push subscription
GET  /                                  PWA (static/index.html)
GET  /health                            status and configuration check

A research job runs in the background: Parallel.ai does the deep research
(minutes to hours depending on processor), Firecrawl archives the cited
sources, and everything lands as Markdown in OUTPUT_DIR — point that at a
synced folder (iCloud/Nextcloud → Obsidian) and the dossier appears in your
notes. A Web Push notification fires when it's done.

Output directory: --output-dir flag > OUTPUT_DIR env var > platform default.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import tempfile
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

import pipeline
from pipeline import ALL_PROCESSORS, PROCESSORS, PipelineError

try:
    import markdown as _markdown
except ModuleNotFoundError:      # optional — report view falls back to <pre>
    _markdown = None

try:
    from pywebpush import webpush, WebPushException
except ModuleNotFoundError:      # optional — app works without push
    webpush = None
    WebPushException = Exception

load_dotenv()

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

PARALLEL_API_KEY = (os.getenv("PARALLEL_API_KEY")
                    or os.getenv("PARALLEL_AI_API_KEY", "")).strip()
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip()
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "").strip()

DEFAULT_PROCESSOR = os.getenv("DEFAULT_PROCESSOR", "core").strip()
MAX_SOURCES = int(os.getenv("MAX_SOURCES", "12"))
MAX_JOBS_KEPT = 200

# Optional API token, same contract as Margin's: unset → open server for
# private-network use; set → everything but /health and the PWA shell assets
# requires `Authorization: Bearer`, `?token=`, or the cookie set on first
# authenticated visit.
FOOTNOTE_TOKEN = os.getenv("FOOTNOTE_TOKEN", "").strip()


def _default_output_dir() -> Path:
    if sys.platform == "darwin":
        return (Path.home()
                / "Library/Mobile Documents/com~apple~CloudDocs/Research/inbox")
    return Path.home() / "Research" / "inbox"


OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR") or _default_output_dir()).expanduser()
DATA_DIR = Path(os.getenv("DATA_DIR") or ROOT / "data").expanduser()

ACTIVE_STATUSES = ("queued", "researching", "archiving", "saving")


# ---------------------------------------------------------------------------
# Tiny JSON stores (jobs, push subscriptions) — atomic rewrite on change
# ---------------------------------------------------------------------------

class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


jobs = JsonStore(DATA_DIR / "jobs.json")
subs = JsonStore(DATA_DIR / "subscriptions.json")


def _trim_jobs() -> None:
    if len(jobs.data) <= MAX_JOBS_KEPT:
        return
    by_age = sorted(jobs.data.items(), key=lambda kv: kv[1].get("created_at", ""))
    for job_id, job in by_age[: len(jobs.data) - MAX_JOBS_KEPT]:
        if job.get("status") not in ACTIVE_STATUSES:
            del jobs.data[job_id]


def _update_job(job_id: str, **fields) -> None:
    job = jobs.data.get(job_id)
    if job is None:
        return
    job.update(fields)
    jobs.save()


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------

def _push_one(sub: dict, payload: dict) -> bool:
    """Blocking pywebpush call; returns False if the subscription is dead."""
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
        )
        return True
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", 0)
        return code not in (404, 410)   # gone → forget this device


async def notify_all(title: str, body: str, url: str = "/") -> None:
    """Send a notification to every registered device (best-effort)."""
    if webpush is None or not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return
    payload = {"title": title, "body": body, "url": url,
               "icon": "/static/icon-192.png", "badge": "/static/icon-192.png"}
    dead = []
    for key, sub in list(subs.data.items()):
        alive = await asyncio.to_thread(_push_one, sub, payload)
        if not alive:
            dead.append(key)
    for key in dead:
        subs.data.pop(key, None)
    if dead:
        subs.save()


# ---------------------------------------------------------------------------
# The research orchestrator (background task per job)
# ---------------------------------------------------------------------------

async def run_research(job_id: str) -> None:
    job = jobs.data.get(job_id)
    if not job:
        return
    client: httpx.AsyncClient = app.state.client
    question, processor = job["question"], job["processor"]
    try:
        # 1. Deep research on Parallel (created once; resumable by run_id)
        _update_job(job_id, status="researching",
                    progress=f"Researching on Parallel ({processor}) — this "
                             f"can take a while…")
        run_id = job.get("run_id")
        if not run_id:
            run_id = await pipeline.start_task_run(
                client, PARALLEL_API_KEY, question, processor)
            _update_job(job_id, run_id=run_id)
        deadline = PROCESSORS.get(processor.removesuffix("-fast"), 3600)
        result = await pipeline.fetch_task_result(
            client, PARALLEL_API_KEY, run_id, deadline)

        # 2. Archive cited sources locally (optional, best-effort)
        sources = []
        if FIRECRAWL_API_KEY and result.citations:
            n = min(len(result.citations), MAX_SOURCES)
            _update_job(job_id, status="archiving",
                        progress=f"Archiving {n} cited source{'s' * (n != 1)}…")
            sources = await pipeline.scrape_sources(
                client, FIRECRAWL_API_KEY, result.citations, MAX_SOURCES)

        # 3. Write the dossier into the synced folder
        _update_job(job_id, status="saving", progress="Writing report…")
        report_path = await asyncio.to_thread(
            pipeline.write_report, OUTPUT_DIR, question, processor,
            result, sources)

        # 4. Optional Notion mirror
        notion_url = ""
        if NOTION_API_KEY and NOTION_DATABASE_ID:
            try:
                notion_url = await pipeline.save_to_notion(
                    client, NOTION_API_KEY, NOTION_DATABASE_ID, question, result)
            except PipelineError as exc:
                print(f"[{job_id}] notion mirror failed: {exc}", flush=True)

        archived = sum(1 for s in sources if s.ok)
        summary = (f"{len(result.citations)} sources cited"
                   + (f", {archived} archived" if archived else ""))
        _update_job(job_id, status="done", progress=f"Done — {summary}",
                    report_path=str(report_path), notion_url=notion_url,
                    finished_at=_now(), sources_cited=len(result.citations),
                    sources_archived=archived)
        await notify_all("Research complete",
                         f"“{_ellipsize(question)}” — {summary}",
                         url=f"/jobs/{job_id}/report")
    except PipelineError as exc:
        _update_job(job_id, status="failed", error=str(exc),
                    progress=f"Failed: {exc}", finished_at=_now())
        await notify_all("Research failed", f"“{_ellipsize(question)}”: {exc}")
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        _update_job(job_id, status="failed", error=f"Unexpected error: {exc}",
                    progress=f"Failed: {exc}", finished_at=_now())
        await notify_all("Research failed", f"“{_ellipsize(question)}”: {exc}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ellipsize(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
    # Jobs interrupted by a restart: the Parallel run survives server-side,
    # so re-attach by run_id; jobs that never got a run_id start over.
    application.state.tasks = set()
    for job_id, job in jobs.data.items():
        if job.get("status") in ACTIVE_STATUSES:
            task = asyncio.create_task(run_research(job_id))
            application.state.tasks.add(task)
            task.add_done_callback(application.state.tasks.discard)
    yield
    for task in application.state.tasks:
        task.cancel()
    await application.state.client.aclose()


app = FastAPI(title="Footnote", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Optional token auth (FOOTNOTE_TOKEN) — same pattern as Margin
# ---------------------------------------------------------------------------

_TOKEN_COOKIE = "footnote_token"
_PUBLIC_PATHS = {
    "/health", "/manifest.json", "/service-worker.js",
    "/favicon.svg", "/favicon.ico", "/favicon-32.png",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
}

_UNAUTHORIZED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Footnote — unauthorized</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
</head>
<body style="font: 16px/1.5 system-ui, sans-serif; max-width: 34rem;
             margin: 4rem auto; padding: 0 1rem;">
<h1 style="font-size:1.3rem; color:#c62828;">Token required</h1>
<p>This Footnote server requires an API token. Enter it once — it is stored
in a browser cookie afterwards. (API clients: send an
<code>Authorization: Bearer</code> header instead.)</p>
<form method="get" action="/" style="display:flex; gap:.5rem;">
  <input type="password" name="token" placeholder="API token" required
         autocomplete="current-password"
         style="flex:1; font:inherit; padding:.45rem .6rem;
                border:1px solid #999; border-radius:6px;">
  <button type="submit" style="font:inherit; padding:.45rem .9rem;
          border:1px solid #999; border-radius:6px; cursor:pointer;">
    Unlock</button>
</form>
</body></html>"""


def _request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("token")
            or request.cookies.get(_TOKEN_COOKIE, ""))


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if (
        not FOOTNOTE_TOKEN
        or request.url.path in _PUBLIC_PATHS
        or request.url.path.startswith("/static/")
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    if not secrets.compare_digest(_request_token(request), FOOTNOTE_TOKEN):
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(_UNAUTHORIZED_HTML, status_code=401)
        return JSONResponse(
            {"status": "error",
             "message": "Unauthorized: missing or wrong token "
                        "(Authorization: Bearer header or ?token= parameter)"},
            status_code=401,
        )

    response = await call_next(request)
    if request.query_params.get("token"):
        response.set_cookie(
            _TOKEN_COOKIE, FOOTNOTE_TOKEN, max_age=365 * 24 * 3600,
            httponly=True, samesite="strict")
    return response


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    question: str
    processor: str = ""

    @field_validator("question")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 8:
            raise ValueError("question is too short")
        if len(v) > 4000:
            raise ValueError("question is too long (4000 chars max)")
        return v


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: dict


@app.post("/research")
async def start_research(req: ResearchRequest):
    if not PARALLEL_API_KEY:
        raise HTTPException(503, "PARALLEL_API_KEY is not configured")
    processor = (req.processor or DEFAULT_PROCESSOR).strip()
    if processor not in ALL_PROCESSORS:
        raise HTTPException(422, f"unknown processor {processor!r}; "
                                 f"one of: {', '.join(ALL_PROCESSORS)}")
    job_id = uuid.uuid4().hex[:12]
    jobs.data[job_id] = {
        "id": job_id, "question": req.question, "processor": processor,
        "status": "queued", "progress": "Queued", "created_at": _now(),
        "run_id": "", "report_path": "", "notion_url": "", "error": "",
    }
    _trim_jobs()
    jobs.save()
    task = asyncio.create_task(run_research(job_id))
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)
    return {"status": "started", "job_id": job_id,
            "message": f"Research started on the {processor} processor"}


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "run_id"}


@app.get("/research/{job_id}")
async def job_status(job_id: str):
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_public(job)


@app.get("/jobs")
async def list_jobs(limit: int = 50):
    ordered = sorted(jobs.data.values(),
                     key=lambda j: j.get("created_at", ""), reverse=True)
    return {"jobs": [_job_public(j) for j in ordered[:limit]],
            "active": sum(j["status"] in ACTIVE_STATUSES for j in ordered)}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] in ACTIVE_STATUSES:
        raise HTTPException(409, "Job is still running")
    del jobs.data[job_id]
    jobs.save()
    return {"status": "deleted"}


def _report_file(job_id: str) -> Path:
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = Path(job.get("report_path") or "")
    if not job.get("report_path") or not path.exists():
        raise HTTPException(404, "No report for this job (yet)")
    return path


@app.get("/jobs/{job_id}/report.md")
async def report_markdown(job_id: str):
    path = _report_file(job_id)
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@app.get("/jobs/{job_id}/report")
async def report_html(job_id: str):
    path = _report_file(job_id)
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):          # strip YAML front matter for display
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    if _markdown is not None:
        body = _markdown.markdown(text, extensions=["tables", "fenced_code"])
    else:
        from html import escape
        body = f"<pre style='white-space:pre-wrap'>{escape(text)}</pre>"
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{path.stem} — Footnote</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/style.css"></head>
<body class="report"><main class="report-body">
<p><a href="/">← Footnote</a> &nbsp;·&nbsp;
<a href="/jobs/{job_id}/report.md" download>Download .md</a></p>
{body}</main></body></html>"""
    return HTMLResponse(page)


@app.get("/jobs/{job_id}/sources/{name}")
async def report_source(job_id: str, name: str):
    # Makes the report's relative "local copy" links work in the web view
    # too, not only in the notes folder.
    if "/" in name or name.startswith("."):
        raise HTTPException(404, "Not found")
    path = _report_file(job_id).parent / "sources" / name
    if not path.is_file():
        raise HTTPException(404, "No such source copy")
    return PlainTextResponse(path.read_text(encoding="utf-8"),
                             media_type="text/plain; charset=utf-8")


@app.post("/subscribe")
async def subscribe(req: SubscribeRequest):
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        raise HTTPException(503, "Push is not configured (VAPID keys missing)")
    key = uuid.uuid5(uuid.NAMESPACE_URL, req.endpoint).hex
    subs.data[key] = {"endpoint": req.endpoint, "keys": req.keys}
    subs.save()
    return {"status": "subscribed", "devices": len(subs.data)}


@app.get("/vapid-public-key")
async def vapid_public_key():
    if not VAPID_PUBLIC_KEY:
        raise HTTPException(503, "Push is not configured (VAPID keys missing)")
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.get("/processors")
async def processors():
    return {"default": DEFAULT_PROCESSOR, "processors": list(ALL_PROCESSORS)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "Footnote",
        "output_dir": str(OUTPUT_DIR),
        "output_dir_writable": os.access(OUTPUT_DIR.parent, os.W_OK)
                               or os.access(OUTPUT_DIR, os.W_OK),
        "parallel_configured": bool(PARALLEL_API_KEY),
        "firecrawl_configured": bool(FIRECRAWL_API_KEY),
        "notion_configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
        "push_configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY
                                and webpush is not None),
        "auth_required": bool(FOOTNOTE_TOKEN),
        "active_jobs": sum(j["status"] in ACTIVE_STATUSES
                           for j in jobs.data.values()),
    }


# ---------------------------------------------------------------------------
# PWA shell
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    # Served from the root so the service worker scope covers the whole app.
    return FileResponse(STATIC / "service-worker.js",
                        media_type="application/javascript")


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
    return FileResponse(STATIC / "manifest.json",
                        media_type="application/manifest+json")


_ROOT_ICONS = {
    "favicon.svg": "icon.svg",
    "favicon.ico": "favicon.ico",
    "favicon-32.png": "favicon-32.png",
    "apple-touch-icon.png": "apple-touch-icon.png",
    "apple-touch-icon-precomposed.png": "apple-touch-icon.png",
}


@app.get("/{icon_name}", include_in_schema=False)
async def root_icon(icon_name: str):
    if icon_name not in _ROOT_ICONS:
        raise HTTPException(404, "Not found")
    path = STATIC / _ROOT_ICONS[icon_name]
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Footnote server")
    parser.add_argument("--output-dir", help="where reports are written")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8010")))
    args = parser.parse_args()
    if args.output_dir:
        OUTPUT_DIR = Path(args.output_dir).expanduser()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Footnote: saving research to {OUTPUT_DIR}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
