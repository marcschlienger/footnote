# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Footnote — ask a question, get a cited research dossier in your notes folder.

POST /research    { "question": "…", "processor": "core" }  → job id
GET  /research/{id}                     job status (poll target)
GET  /jobs                              recent jobs for the UI
GET  /jobs/{id}/report                  rendered report (HTML)
GET  /jobs/{id}/report.md               raw Markdown download
GET  /jobs/{id}/sources                 the job's source material (JSON)
GET  /jobs/{id}/sources/{file}          one archived source (HTML, ?raw=1 → .md)
GET  /jobs/{id}/bundle.zip              report + every archived source (zip)
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
import io
import json
import os
import re
import secrets
import sys
import tempfile
import traceback
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
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
# Firecrawl's free plan allows 10 /scrape requests a minute and 2 concurrent
# browsers; those are the defaults, so an unconfigured install stays inside
# them. Raise on a paid plan, or set the rate limit to 0 to stop pacing.
FIRECRAWL_RATE_LIMIT = int(os.getenv("FIRECRAWL_RATE_LIMIT",
                                     pipeline.FREE_RATE_LIMIT))
FIRECRAWL_CONCURRENCY = int(os.getenv("FIRECRAWL_CONCURRENCY",
                                      pipeline.FREE_CONCURRENCY))
MAX_JOBS_KEPT = 200
MAX_CITATIONS_KEPT = 100     # per job, so jobs.json stays small

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
            # Pacing makes this the slow step on a free plan — say where it is.
            def archived_so_far(done: int, total: int) -> None:
                _update_job(job_id, progress=f"Archiving sources… {done}/{total}")

            sources = await pipeline.scrape_sources(
                client, FIRECRAWL_API_KEY, result.citations, MAX_SOURCES,
                concurrency=FIRECRAWL_CONCURRENCY,
                rate_limit=FIRECRAWL_RATE_LIMIT,
                on_progress=archived_so_far)

        # 3. Write the dossier into the synced folder
        _update_job(job_id, status="saving", progress="Writing report…")
        written = await asyncio.to_thread(
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
        # Worth saying plainly: it is a billing state, not a flaky website,
        # and every unarchived source in this dossier has the same cause.
        if any(s.error == pipeline.OUT_OF_CREDITS for s in sources):
            summary += " — Firecrawl credits exhausted"
        _update_job(job_id, status="done", progress=f"Done — {summary}",
                    report_path=str(written.path), notion_url=notion_url,
                    finished_at=_now(), sources_cited=len(result.citations),
                    sources_archived=archived,
                    citations=_citation_records(result, sources, written))
        await notify_all("Research complete",
                         f"“{_ellipsize(question)}” — {summary}",
                         url=f"/jobs/{job_id}/report")
    except PipelineError as exc:
        reason = _scrub(exc)
        _update_job(job_id, status="failed", error=reason,
                    progress=f"Failed: {reason}", finished_at=_now())
        await notify_all("Research failed", f"“{_ellipsize(question)}”: {reason}")
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        reason = _scrub(exc)
        _update_job(job_id, status="failed", error=f"Unexpected error: {reason}",
                    progress=f"Failed: {reason}", finished_at=_now())
        await notify_all("Research failed", f"“{_ellipsize(question)}”: {reason}")


def _citation_records(result, sources: list, written) -> list:
    """The job's source apparatus, as the /sources endpoint serves it.

    Kept on the job rather than re-derived from the report text: it is the
    only place that knows which citations *failed* to archive and why.
    """
    errors = {src.url: src.error for src in sources if not src.ok}
    return [
        {"url": cit.url, "title": cit.title,
         "file": written.source_files.get(cit.url, ""),
         "note": errors.get(cit.url, "")}
        for cit in result.citations[:MAX_CITATIONS_KEPT]
    ]


def _scrub(text) -> str:
    """Keep server filesystem paths out of anything a client can see.

    The dossier folder is the server's business; a client gets told what went
    wrong, not where the server keeps its files.
    """
    out = str(text)
    for path, label in ((str(OUTPUT_DIR), "the output folder"),
                        (str(DATA_DIR), "the data folder"),
                        (str(Path.home()), "~")):
        out = out.replace(path, label)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ellipsize(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Markdown → HTML for the report and source views
#
# Everything rendered here started life on the open web — a scraped page, or a
# report written from scraped pages — and python-markdown passes raw HTML
# straight through. So the rendered HTML is rebuilt from an allowlist rather
# than filtered: unknown tags become text, unknown attributes are dropped, and
# only http/https/mailto (or relative) URLs survive in href/src.
# ---------------------------------------------------------------------------

_ALLOWED_TAGS = frozenset("""
    p br hr em strong i b u s del ins mark sub sup small
    h1 h2 h3 h4 h5 h6 blockquote pre code kbd
    ul ol li dl dt dd a img figure figcaption span div
    table thead tbody tfoot tr th td caption
""".split())
_VOID_TAGS = frozenset({"br", "hr", "img"})
_DROP_WITH_CONTENT = frozenset({"script", "style", "iframe", "object", "embed"})
_ALLOWED_ATTRS = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title"}),
    "th": frozenset({"align"}),
    "td": frozenset({"align"}),
    "code": frozenset({"class"}),      # language-* from fenced code blocks
    "pre": frozenset({"class"}),
}
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def _safe_url(url: str) -> bool:
    """True for relative URLs and for the schemes a document may link to."""
    if not url:
        return False
    # Browsers ignore control characters inside a scheme ("java\tscript:").
    bare = re.sub(r"[\x00-\x20]", "", url)
    match = _SCHEME.match(bare)
    return match is None or match.group(1).lower() in _SAFE_SCHEMES


class _Sanitizer(HTMLParser):
    """Re-emit only allowlisted markup; everything else becomes text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list = []
        self._muted = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_WITH_CONTENT:
            self._muted += 1
            return
        if self._muted or tag not in _ALLOWED_TAGS:
            return
        allowed = _ALLOWED_ATTRS.get(tag, frozenset())
        parts = [tag]
        for name, value in attrs:
            value = value or ""
            if name not in allowed:
                continue
            if name in ("href", "src") and not _safe_url(value):
                continue
            parts.append(f'{name}="{escape(value, quote=True)}"')
        self.out.append("<" + " ".join(parts) + ">")

    def handle_endtag(self, tag):
        if tag in _DROP_WITH_CONTENT:
            self._muted = max(0, self._muted - 1)
            return
        if not self._muted and tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._muted:
            self.out.append(escape(data, quote=False))


def _render_markdown(text: str) -> str:
    if _markdown is None:              # optional dependency; show it plain
        return f"<pre style='white-space:pre-wrap'>{escape(text)}</pre>"
    parser = _Sanitizer()
    parser.feed(_markdown.markdown(text, extensions=["tables", "fenced_code"]))
    parser.close()
    return "".join(parser.out)


def _page(title: str, nav: str, body: str) -> HTMLResponse:
    """The shared paper-styled document shell (report and source views)."""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Footnote</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/style.css"></head>
<body class="report"><main class="report-body">
<p class="page-nav">{nav}</p>
{body}</main></body></html>""")


def _attachment(filename: str) -> dict:
    """Content-Disposition for a download, RFC 5987 when the name needs it."""
    quoted = quote(filename)
    if quoted == filename:
        return {"content-disposition": f'attachment; filename="{filename}"'}
    return {"content-disposition": f"attachment; filename*=utf-8''{quoted}"}


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


# Server-side bookkeeping the client has no use for: the Parallel run id, the
# dossier's path on disk (clients address it by job id), and the citation list
# (served on demand by /jobs/{id}/sources — it would bloat every poll).
_PRIVATE_JOB_FIELDS = ("run_id", "report_path", "citations")


def _job_public(job: dict) -> dict:
    public = {k: v for k, v in job.items() if k not in _PRIVATE_JOB_FIELDS}
    if job.get("report_path"):
        public["report_name"] = Path(job["report_path"]).name
    return public


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
    _, text = pipeline.split_front_matter(path.read_text(encoding="utf-8"))
    nav = (f'<a href="/">← Footnote</a> &nbsp;·&nbsp; '
           f'<a href="/jobs/{job_id}/report.md" download>Download .md</a>'
           f' &nbsp;·&nbsp; '
           f'<a href="/jobs/{job_id}/bundle.zip">Download everything (.zip)</a>')
    return _page(path.stem, nav, _render_markdown(text))


def _source_file(job_id: str, name: str) -> Path:
    """Resolve an archived source copy by file name only — never by path."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404, "Not found")
    path = _report_file(job_id).parent / "sources" / name
    if not path.is_file():
        raise HTTPException(404, "No such source copy")
    return path


def _head(path: Path, limit: int = 1024) -> str:
    with path.open(encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)


def _source_entries(job: dict, folder: Path) -> list:
    """Every source behind a dossier: what was cited, what was archived.

    The citation list recorded on the job is the spine (it carries the ones
    that could *not* be archived, and why); the files actually present in
    `sources/` decide what is readable, so a copy deleted in the notes folder
    degrades to a plain citation instead of a broken link. Dossiers written
    before jobs kept a citation list are described by their files alone.
    """
    files = {f.name: f for f in sorted((folder / "sources").glob("*.md"))}
    listed = [(cit.get("title", ""), cit.get("url", ""),
               cit.get("file", "") if cit.get("file") in files else "",
               cit.get("note", ""))
              for cit in job.get("citations") or []]
    known = {name for _, _, name, _ in listed}
    for name, path in files.items():
        if name not in known:
            meta, _ = pipeline.split_front_matter(_head(path))
            listed.append((meta.get("title", ""), meta.get("source", ""),
                           name, ""))

    base = f"/jobs/{job['id']}/sources/"
    return [
        {"n": n,
         "title": title or url or name,
         "url": url if _safe_url(url) else "",
         "file": name,
         "archived": bool(name),
         "read_url": base + quote(name) if name else "",
         "download_url": f"{base}{quote(name)}?raw=1" if name else "",
         "bytes": files[name].stat().st_size if name else 0,
         "note": note}
        for n, (title, url, name, note) in enumerate(listed, start=1)
    ]


@app.get("/jobs/{job_id}/sources")
async def list_sources(job_id: str):
    """The dossier's source apparatus, for reading and downloading in the app."""
    report = _report_file(job_id)
    job = jobs.data[job_id]
    entries = _source_entries(job, report.parent)
    return {
        "job_id": job_id,
        "question": job.get("question", ""),
        "report_name": report.name,
        "report_url": f"/jobs/{job_id}/report",
        "markdown_url": f"/jobs/{job_id}/report.md",
        "bundle_url": f"/jobs/{job_id}/bundle.zip",
        # `sources` is the list; `cited` is what the dossier cited, which is
        # larger when a question drew more citations than MAX_CITATIONS_KEPT.
        "cited": job.get("sources_cited", len(entries)),
        "archived": sum(1 for e in entries if e["archived"]),
        "sources": entries,
    }


@app.get("/jobs/{job_id}/sources/{name}")
async def report_source(job_id: str, name: str, raw: int = 0):
    """One archived source: rendered for reading, `?raw=1` for the file.

    The rendered form is also what the report's relative "local copy" links
    resolve to in the web view, exactly as they do in a notes app.
    """
    path = _source_file(job_id, name)
    if raw:
        return FileResponse(path, media_type="text/markdown", filename=path.name)
    meta, text = pipeline.split_front_matter(path.read_text(encoding="utf-8"))
    origin = meta.get("source", "")
    nav = [f'<a href="/jobs/{job_id}/report">← Report</a>']
    if _safe_url(origin):
        nav.append(f'<a href="{escape(origin, quote=True)}" target="_blank" '
                   f'rel="noopener noreferrer">Original page ↗</a>')
    nav.append('<a href="?raw=1" download>Download .md</a>')
    title = meta.get("title") or path.stem
    heading = (f'<h1>{escape(title)}</h1>'
               f'<p class="source-note">Archived copy'
               + (f", retrieved {escape(meta['retrieved'])}"
                  if meta.get("retrieved") else "") + '</p>')
    return _page(title, " &nbsp;·&nbsp; ".join(nav),
                 heading + _render_markdown(text))


def _zip_bundle(report: Path) -> bytes:
    """The report and its archived sources, laid out as they are on disk."""
    folder = report.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(report, f"{folder.name}/{report.name}")
        for path in sorted((folder / "sources").glob("*.md")):
            archive.write(path, f"{folder.name}/sources/{path.name}")
    return buf.getvalue()


@app.get("/jobs/{job_id}/bundle.zip")
async def bundle_zip(job_id: str):
    report = _report_file(job_id)
    data = await asyncio.to_thread(_zip_bundle, report)
    return Response(data, media_type="application/zip",
                    headers=_attachment(f"{report.parent.name}.zip"))


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
        # Where the dossiers are filed is the server's business — clients get
        # told whether the folder works, not where it is.
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
