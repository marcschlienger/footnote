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
import base64
import binascii
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
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

try:                             # ships with pywebpush; validates public keys
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
except ModuleNotFoundError:
    _ec = None

try:                             # ships with pywebpush; carries the push POST
    import requests as _requests
except ModuleNotFoundError:
    _requests = None

def _load_env() -> None:
    """Read .env if it is there and readable — and start either way.

    A permissions problem on an optional config file is not a reason to
    refuse to boot: systemd and per-instance env files may carry everything
    the app needs, and a process that exits 1 at import is far harder to
    diagnose than a warning and a /health that reports what is missing.
    """
    try:
        load_dotenv()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"could not read .env ({exc}); continuing with the environment",
              flush=True)


_load_env()

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

def _int_env(name: str, default: int, minimum: int) -> int:
    """A numeric setting, or a startup failure naming the setting."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, not {raw!r}") from None
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}, not {value}")
    return value


DEFAULT_PROCESSOR = os.getenv("DEFAULT_PROCESSOR", "core").strip() or "core"
if DEFAULT_PROCESSOR not in ALL_PROCESSORS:
    raise RuntimeError(
        f"DEFAULT_PROCESSOR is {DEFAULT_PROCESSOR!r}, which is not a processor; "
        f"one of: {', '.join(ALL_PROCESSORS)}")
MAX_SOURCES = _int_env("MAX_SOURCES", 12, minimum=0)
# Firecrawl's free plan allows 10 /scrape requests a minute and 2 concurrent
# browsers; those are the defaults, so an unconfigured install stays inside
# them. Raise on a paid plan, or set the rate limit to 0 to stop pacing.
FIRECRAWL_RATE_LIMIT = _int_env("FIRECRAWL_RATE_LIMIT",
                                pipeline.FREE_RATE_LIMIT, minimum=0)
FIRECRAWL_CONCURRENCY = _int_env("FIRECRAWL_CONCURRENCY",
                                 pipeline.FREE_CONCURRENCY, minimum=1)
# Whether a site's robots.txt is consulted before its page is archived. On by
# default: it is the only machine-readable way a site says "not by machine",
# and the citation is kept either way — only the local copy is skipped.
RESPECT_ROBOTS = os.getenv("RESPECT_ROBOTS", "true").strip().lower() not in (
    "0", "false", "no", "off")
MAX_JOBS_KEPT = 200
MAX_CITATIONS_KEPT = 100     # per job, so jobs.json stays small
PUSH_CONCURRENCY = 4         # devices notified at once
PUSH_CONNECT_TIMEOUT_S = 5   # per device; the library's default is no limit
PUSH_READ_TIMEOUT_S = 15     # inactivity, not total — see _push_one
MAX_SUBSCRIPTIONS = 50       # devices kept; a personal server has a handful
MAX_ENDPOINT_CHARS = 2000

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

def _storable_key(key) -> bool:
    """A key that can be written, read back, and addressed unchanged."""
    return (isinstance(key, str) and key != ""
            and not pipeline.has_lone_surrogate(key)
            and key == pipeline.clean_text(key))


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self.repaired: set = set()      # records cleaning had to change
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise json.JSONDecodeError(
                    f"expected an object, found {type(loaded).__name__}", "", 0)
            # One damaged record must not cost the rest of the history, and
            # what survives is cleaned here rather than field by field: an
            # escaped surrogate in the file comes back as a surrogate, and
            # would break the next response carrying it.
            # Records are cleaned one at a time with their keys left alone:
            # cleaning the outer dictionary let two keys ("\ud800" and the
            # replacement character it becomes) collide, so one record
            # silently overwrote the other.
            for key, value in loaded.items():
                if not isinstance(value, dict):
                    continue
                if not _storable_key(key):
                    # Rekeyed rather than dropped: the history is worth
                    # keeping, and _normalize_jobs would have rekeyed this
                    # record anyway had it survived to be looked at.
                    key = uuid.uuid4().hex[:12]
                    print(f"{path.name}: rekeyed a record whose key could not "
                          f"be used", flush=True)
                    self.repaired.add(key)
                cleaned = pipeline.clean_json(value)
                self.data[key] = cleaned
                # Cleaning makes a record storable; it does not make it
                # *valid*. Eight surrogates become eight replacement
                # characters, which is a long enough question to pass every
                # later check and be sent to Parallel again.
                if cleaned != value:
                    self.repaired.add(key)
            dropped = len(loaded) - len(self.data)
            if dropped:
                print(f"{path.name}: ignored {dropped} malformed record"
                      f"{'s' * (dropped != 1)}", flush=True)
        except OSError as exc:
            print(f"could not read {path.name}: {exc}", flush=True)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Starting empty would silently drop the history; the file is kept
            # under a new name so it can be looked at, since the next save
            # would otherwise overwrite the evidence.
            spoiled = path.with_suffix(
                f".corrupt-{int(time.time())}-{uuid.uuid4().hex[:6]}.json")
            try:
                path.rename(spoiled)
                print(f"{path.name} is not valid JSON ({exc}); kept as "
                      f"{spoiled.name} and starting empty", flush=True)
            except OSError:
                print(f"{path.name} is not valid JSON ({exc}); starting empty",
                      flush=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        # clean_json rather than a fallback: escaping a surrogate kept the
        # file writable but reloaded it as a surrogate, so the problem came
        # back on the next read. Cleaning here makes the file structurally
        # unable to hold something a later load could choke on.
        # The cleaned structure becomes the store, not just the bytes: the
        # invariant is about what is held in memory, and a copy that only the
        # file gets is an invariant nobody can rely on. Values only — a key
        # is already required to be storable, and cleaning keys is how two of
        # them came to collide.
        self.data = {key: pipeline.clean_json(value)
                     for key, value in self.data.items()
                     if _storable_key(key)}   # keys are already normalized
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


jobs = JsonStore(DATA_DIR / "jobs.json")
subs = JsonStore(DATA_DIR / "subscriptions.json")

# Fields that are only rendered or ordered by: whatever they hold becomes
# text, which is all sorting needs.
_JOB_DISPLAY_FIELDS = ("progress", "error", "notion_url", "finished_at",
                       "created_at")
# Fields the app acts on. Stringifying these is worse than dropping them —
# None becomes "None", {} becomes "{}", and both are truthy.
_JOB_SEMANTIC_FIELDS = ("question", "processor", "status", "run_id",
                        "report_path")
_TERMINAL_STATUSES = ("done", "failed")
QUESTION_MIN, QUESTION_MAX = 8, 4000
# Store keys become public job ids and go straight into URLs.
_JOB_ID = re.compile(r"[0-9a-f]{12}")     # used with fullmatch
# Tab and newline are fine in a typed question; NUL and friends are not.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _question_problem(question) -> str:
    """Why this question cannot be researched, or "" if it can.

    One rule for both doors: a question that submission would refuse must not
    get in through a resume either, where it would spend the same quota.
    """
    if not isinstance(question, str):
        return "question must be text"
    text = question.strip()
    if len(text) < QUESTION_MIN:
        return "question is too short"
    if len(text) > QUESTION_MAX:
        return f"question is too long ({QUESTION_MAX} chars max)"
    if pipeline.has_lone_surrogate(text):
        # It survives JSON decoding and then cannot be written back out.
        return "question contains unpaired surrogate characters"
    if _CONTROL_CHARS.search(text):
        # Encodable, but they would end up as raw bytes in the dossier's
        # Markdown and YAML.
        return "question contains control characters"
    return ""


def _normalize_jobs() -> None:
    """Make every stored record usable, or terminal.

    A record is read by the orchestrator, the poll endpoint, the trimmer and
    the deleter, all of which assumed fields that a hand-edited or truncated
    file need not have. Semantic fields are *validated*, never coerced: a
    question of `null` stringified into "None" is not a repair, it is a
    corrupt record made to look runnable.
    """
    changed = False
    for job_id in [k for k in jobs.data if not _JOB_ID.fullmatch(str(k))]:
        # The key is the public id and goes into URLs. A hand-edited file can
        # hold anything; rekey rather than drop, so the history survives and
        # every record stays addressable.
        record = jobs.data.pop(job_id)
        fresh = uuid.uuid4().hex[:12]
        record["id"] = fresh
        jobs.data[fresh] = record
        if job_id in jobs.repaired:
            # The marker travels with the record. Without this the damaged
            # job was rekeyed out from under its own repair flag and resumed.
            jobs.repaired.discard(job_id)
            jobs.repaired.add(fresh)
        print(f"job history: rekeyed an unusable job id to {fresh}", flush=True)
        changed = True

    for job_id, job in list(jobs.data.items()):
        if job.get("id") != job_id:
            # The API resolves by key; the PWA and the source index build
            # links from the stored id. They have to be the same string.
            job["id"] = job_id
            changed = True
        for field in _JOB_DISPLAY_FIELDS:
            if field in job and not isinstance(job[field], str):
                job[field] = str(job[field])
                changed = True

        for field in _JOB_SEMANTIC_FIELDS:
            if field in job and not isinstance(job[field], str):
                del job[field]                    # absent beats nonsense
                changed = True

        if job.get("status") not in ACTIVE_STATUSES + _TERMINAL_STATUSES:
            job["status"] = "failed"
            changed = True
        if job_id in jobs.repaired and job["status"] in ACTIVE_STATUSES:
            # It was not storable as written, so what is here now is a repair,
            # not the question that was asked. Not something to spend on.
            job.update(status="failed", finished_at=_now(),
                       error="Job record was damaged; not resumed",
                       progress="Failed: damaged job record")
            changed = True
        elif job.get("run_id") and not pipeline.valid_run_id(job["run_id"]):
            if job["status"] in ACTIVE_STATUSES:
                # The run may exist and be paid for, but it cannot be fetched
                # and starting over would pay for a second one.
                job.update(status="failed", finished_at=_now(),
                           error="Stored run id is unusable; not resumed",
                           progress="Failed: unusable run id")
            else:
                # A finished job has a dossier. The run id is bookkeeping
                # nobody will read again; failing the job over it would hide
                # the research behind it.
                del job["run_id"]
            changed = True
        elif job["status"] in ACTIVE_STATUSES and not _resumable(job):
            job.update(status="failed", finished_at=_now(),
                       error="Incomplete job record; cannot be resumed",
                       progress="Failed: incomplete job record")
            changed = True
        if job.get("notion_url") and not pipeline.is_http_url(job["notion_url"]):
            # The PWA assigns this straight to an anchor's href.
            del job["notion_url"]
            changed = True
    if changed:
        jobs.save()
        print("job history repaired on load", flush=True)


def _resumable(job: dict) -> bool:
    """Whether run_research could actually run this record.

    The processor is checked against the real list: it is sent to Parallel
    verbatim, and an unknown one is refused at submission but was never
    re-checked on the way back in.
    """
    return (not _question_problem(job.get("question"))
            and job.get("processor") in ALL_PROCESSORS)


# Web Push key sizes: an uncompressed P-256 point, and the auth secret.
_KEY_BYTES = {"p256dh": 65, "auth": 16}
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")   # used with fullmatch
# Encoded length of exactly that many bytes, padding included.
_KEY_CHARS = {name: (size + 2) // 3 * 4 for name, size in _KEY_BYTES.items()}


def _valid_subscription(sub) -> bool:
    """What _push_one needs to exist, and to be usable, before it tries."""
    if not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint")
    if (not isinstance(endpoint, str) or len(endpoint) > MAX_ENDPOINT_CHARS
            or not pipeline.is_push_endpoint(endpoint)):
        return False
    if not isinstance(sub.get("keys"), dict):
        return False
    for name, size in _KEY_BYTES.items():
        value = sub["keys"].get(name)
        # Length before decoding: base64 of N bytes is a known size, and
        # matching a regex against megabytes to learn that is wasteful.
        if not isinstance(value, str) or not (0 < len(value) <= _KEY_CHARS[name]):
            return False
        if not _BASE64URL.fullmatch(value):
            return False        # the decoder ignores trailing rubbish
        try:
            # base64url without padding, as the Push API hands it over.
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error):
            return False
        if len(decoded) != size:
            return False        # would fail on every notification, for ever
        if name == "p256dh" and not _is_p256_point(decoded):
            return False        # right length, right prefix, not on the curve
    return True


def _is_p256_point(raw: bytes) -> bool:
    """Whether these 65 bytes are actually a public key.

    Length and the 0x04 prefix are necessary and not sufficient: a value that
    is neither is accepted by every check we could make cheaply and then
    rejected by the push library on every notification, for ever.
    """
    if _ec is None:                     # cryptography absent: keep the cheap test
        return raw[:1] == b"\x04"
    try:
        _ec.EllipticCurvePublicKey.from_encoded_point(_ec.SECP256R1(), raw)
        return True
    except (ValueError, TypeError):
        return False


def _drop_unusable_subscriptions() -> None:
    """Subscriptions stored before they were checked on the way in.

    A malformed one cannot be pushed to, so keeping it means failing and
    logging once per job, forever.
    """
    bad = [key for key, sub in subs.data.items() if not _valid_subscription(sub)]
    for key in bad:
        del subs.data[key]
    if bad:
        subs.save()
        print(f"dropped {len(bad)} unusable push subscription"
              f"{'s' * (len(bad) != 1)}", flush=True)


_drop_unusable_subscriptions()


def _trim_jobs() -> None:
    """Evict the oldest finished jobs until the store is back under the cap.

    Walks the whole history rather than only the oldest `excess` entries: a
    running job among the oldest is skipped, and the next finished one has to
    take its place or the cap is not a cap.
    """
    by_age = sorted(jobs.data.items(),
                    key=lambda kv: str(kv[1].get("created_at", "")))
    for job_id, job in by_age:
        if len(jobs.data) <= MAX_JOBS_KEPT:
            return
        if job.get("status") not in ACTIVE_STATUSES:
            del jobs.data[job_id]


def _put_job(job_id: str, record: dict) -> None:
    """The only way a record enters the store — cleaned, whatever built it."""
    jobs.data[job_id] = pipeline.clean_json(record)


def _update_job(job_id: str, **fields) -> None:
    job = jobs.data.get(job_id)
    if job is None:
        return
    job.update(fields)
    # Provider text arrives through here; cleaning the whole record beats a
    # list of field names that has to be kept in step with the record.
    _put_job(job_id, job)
    jobs.save()


# ---------------------------------------------------------------------------
# Web Push
# ---------------------------------------------------------------------------

def _push_session():
    """A requests session that refuses redirects, or None if requests is absent."""
    if _requests is None:
        return None
    session = _requests.Session()
    # max_redirects is deliberately left alone: requests computes the "next"
    # request even when it is not following one, and a limit of 0 makes that
    # raise TooManyRedirects — a generic failure, which _push_one would read
    # as "keep retrying" instead of "this endpoint has moved".
    original = session.request

    def no_redirects(*args, **kwargs):
        kwargs["allow_redirects"] = False
        return original(*args, **kwargs)

    session.request = no_redirects
    return session


def _push_one(sub: dict, payload: dict) -> bool:
    """Blocking pywebpush call; returns False if the subscription is dead."""
    session = _push_session()
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIM_EMAIL}"},
            # Without this the library waits forever, and one unreachable
            # device holds a slot against every other device's notification.
            # A pair, not a scalar: requests reads it as connect and
            # *inactivity* between reads, so a slow drip can outlast any
            # single number. The pool above bounds what that can cost.
            timeout=(PUSH_CONNECT_TIMEOUT_S, PUSH_READ_TIMEOUT_S),
            # A 307/308 preserves the POST, so following one would send the
            # payload to an address that was never checked.
            requests_session=session,
        )
        return True
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", 0)
        if code in (404, 410):
            return False                # gone → forget this device
        if 300 <= code < 400:
            # We declined to follow it. An endpoint that has moved will keep
            # answering this way, so keeping it means retrying for ever.
            print("push endpoint redirected; dropping the subscription",
                  flush=True)
            return False
        return True
    except Exception as exc:            # noqa: BLE001
        # /subscribe accepts whatever keys a browser sent, so a malformed
        # subscription surfaces here rather than as a WebPushException. Keep
        # it: the device is not provably gone, and a notification is never
        # worth failing over.
        # The endpoint is a capability URL — anyone holding it can push to
        # the device — so it stays out of the log.
        print(f"push to a subscribed device failed: {exc}", flush=True)
        return True
    finally:
        if session is not None:
            session.close()             # one per device, otherwise leaked


async def notify_all(title: str, body: str, url: str = "/") -> None:
    """Send a notification to every registered device.

    Best-effort to the last: this is called from inside run_research's try,
    so anything escaping here would rewrite a finished job as failed. Nothing
    escapes.
    """
    try:
        await _notify_all(title, body, url)
    except Exception as exc:            # noqa: BLE001
        print(f"notification failed: {exc}", flush=True)


async def _notify_all(title: str, body: str, url: str) -> None:
    if webpush is None or not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY
                               and VAPID_CLAIM_EMAIL):
        return
    payload = {"title": title, "body": body, "url": url,
               "icon": "/static/icon-192.png", "badge": "/static/icon-192.png"}
    # A pool of its own, sized once for the process. A semaphore per call
    # gave two jobs finishing together twice the slots, and asyncio.to_thread
    # would put these on the default executor — the same one write_report
    # uses, so a hung push would eventually stall saving a dossier.
    loop = asyncio.get_event_loop()
    pool = getattr(app.state, "push_pool", None)

    async def deliver(key, sub):
        if pool is None:                       # outside the app's lifespan
            return key, await asyncio.to_thread(_push_one, sub, payload)
        return key, await loop.run_in_executor(pool, _push_one, sub, payload)

    results = await asyncio.gather(
        *(deliver(k, v) for k, v in list(subs.data.items())))
    dead = [key for key, alive in results if not alive]
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
    if job.get("status") == "done":
        return
    # Normalization at load should have caught this; checked again because the
    # cost of being wrong is a task that dies here while the record says
    # "researching" for ever.
    if not _resumable(job):
        _update_job(job_id, status="failed", finished_at=_now(),
                    error="Incomplete job record; cannot be resumed",
                    progress="Failed: incomplete job record")
        return
    question, processor = job["question"], job["processor"]
    client: httpx.AsyncClient = app.state.client
    # A dossier already on disk from an interrupted run: the research is paid
    # for and filed, so it is adopted rather than repeated. The same test the
    # endpoints apply — adopting something they would refuse to serve would
    # mark a job done whose every report URL then answers 404.
    resumed = _servable_report(job.get("report_path") or "")
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

        # 2. Archive cited sources locally (optional, best-effort). The list
        # is prepared once: what is announced is exactly what is attempted.
        sources = []
        todo = pipeline.scrapable(result.citations, MAX_SOURCES)
        if resumed is not None:
            # The copies are on disk; what did not survive is the record of
            # which citation each belongs to. Read it back off the folder.
            written = pipeline.WrittenReport(
                path=resumed,
                source_files=await asyncio.to_thread(
                    pipeline.archived_source_files, resumed))
            archived = len(written.source_files)
            print(f"[{job_id}] adopting the dossier written before the restart",
                  flush=True)
        elif FIRECRAWL_API_KEY and MAX_SOURCES > 0 and todo:
            n = len(todo)
            _update_job(job_id, status="archiving",
                        progress=f"Archiving {n} cited source{'s' * (n != 1)}…")


            # Pacing makes this the slow step on a free plan — say where it is.
            def archived_so_far(done: int, total: int) -> None:
                _update_job(job_id, progress=f"Archiving sources… {done}/{total}")

            sources = await pipeline.scrape_sources(
                client, FIRECRAWL_API_KEY, todo, app.state.limiter,
                on_progress=archived_so_far,
                robots=getattr(app.state, "robots", None))

        # 3. Write the dossier into the synced folder
        if resumed is None:
            _update_job(job_id, status="saving", progress="Writing report…")
            written = await asyncio.to_thread(
                pipeline.write_report, OUTPUT_DIR, question, processor,
                result, sources, job_id)
            archived = sum(1 for s in sources if s.ok)
            # Stored before anything else can fail: from here the job is
            # finished, and a restart adopts this dossier instead of paying
            # for the research a second time.
            _update_job(job_id, report_path=str(written.path))

        summary = (f"{len(result.citations)} sources cited"
                   + (f", {archived} archived" if archived else ""))
        # Worth saying plainly: it is a billing state, not a flaky website,
        # and every unarchived source in this dossier has the same cause.
        if any(s.error == pipeline.OUT_OF_CREDITS for s in sources):
            summary += " — Firecrawl credits exhausted"

        # 4. The complete outcome, in one write. Notion and push follow it,
        # so neither can leave a job looking unfinished.
        _update_job(job_id, status="done", progress=f"Done — {summary}",
                    report_path=str(written.path), finished_at=_now(),
                    sources_cited=len(result.citations),
                    sources_archived=archived,
                    citations=_citation_records(result, sources, written))

        # 5. Post-processing: an optional mirror, then the notification.
        # Both are skipped if the job has since been removed — a notification
        # for a deleted job opens a 404, and a mirror of it would be a page
        # nothing points at.
        if job_id not in jobs.data:
            return
        if NOTION_API_KEY and NOTION_DATABASE_ID:
            try:
                notion_url = await pipeline.save_to_notion(
                    client, NOTION_API_KEY, NOTION_DATABASE_ID, question, result)
                # Only a real link reaches the PWA, which renders it as one.
                if pipeline.is_http_url(notion_url):
                    _update_job(job_id, notion_url=notion_url)
            except Exception as exc:                       # noqa: BLE001
                # Everything, not just PipelineError: a DNS failure or an HTML
                # body where JSON was promised would otherwise escape and
                # rewrite a dossier that is already on disk as failed.
                print(f"[{job_id}] notion mirror failed: {_scrub(exc)}", flush=True)

        if job_id in jobs.data:
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
    only place that knows which citations *failed* to archive and why. On a
    dossier adopted after a restart `sources` is empty — the files on disk
    say what was archived, but why the rest were not is gone with the run.
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


def _output_dir_writable() -> bool:
    """Whether a dossier folder could be created, asked of the right directory.

    Once OUTPUT_DIR exists it is the only directory that matters — a writable
    parent says nothing about a read-only folder inside it. Before it exists,
    the nearest ancestor that does is what mkdir will have to write into.
    """
    target = OUTPUT_DIR
    while not target.exists() and target.parent != target:
        target = target.parent
    return os.access(target, os.W_OK | os.X_OK)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_normalize_jobs()


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
# One URL policy for the whole app: what the dossier is willing to link to
# is what the renderer is willing to keep (pipeline.is_safe_url).
_safe_url = pipeline.is_safe_url


def _safe_image_url(url: str) -> bool:
    """Images additionally have to satisfy the CSP, which allows no http:.

    Letting the sanitizer keep an http image the policy then blocks would
    give a silently broken picture instead of an honest missing one.
    """
    if not url:
        return False
    bare = re.sub(r"[\x00-\x20]", "", url).lower()
    if bare.startswith("data:image/") and "svg" not in bare.split(";", 1)[0]:
        return True
    return _safe_url(url) and not bare.startswith("http:")


class _Sanitizer(HTMLParser):
    """Re-emit only allowlisted markup; everything else becomes text."""

    def __init__(self, base: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.out: list = []
        self._muted = 0
        # Where relative links should point. A dossier links its sources
        # relatively, which is right in a notes folder and right on the
        # report's own page — but a fragment shown inside the app is at a
        # different URL, and those links would resolve against that instead.
        self.base = base

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
            if name == "href" and not _safe_url(value):
                continue
            if name == "src" and not _safe_image_url(value):
                continue
            if self.base and name in ("href", "src"):
                value = urljoin(self.base, value)
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


def _render_markdown(text: str, base: str = "") -> str:
    if _markdown is None:              # optional dependency; show it plain
        return f"<pre style='white-space:pre-wrap'>{escape(text)}</pre>"
    parser = _Sanitizer(base)
    parser.feed(_markdown.markdown(text, extensions=["tables", "fenced_code"]))
    parser.close()
    return "".join(parser.out)


def _page(title: str, nav: str, body: str, status_code: int = 200) -> HTMLResponse:
    """The shared paper-styled document shell (report and source views)."""
    return HTMLResponse(status_code=status_code, content=f"""<!doctype html><html><head><meta charset="utf-8">
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
    # One limiter for the whole process: Firecrawl counts requests per key,
    # and a restart can resume several jobs at once.
    application.state.limiter = pipeline.ScrapeLimiter(
        rate_limit=FIRECRAWL_RATE_LIMIT, concurrency=FIRECRAWL_CONCURRENCY)
    # Isolated from the default executor: a push to an endpoint that dribbles
    # a byte inside every read timeout holds its thread indefinitely, and the
    # blast radius should be "no more notifications" rather than "no more
    # dossiers written".
    application.state.robots = (pipeline.RobotsCache()
                               if RESPECT_ROBOTS else None)
    application.state.push_pool = ThreadPoolExecutor(
        max_workers=PUSH_CONCURRENCY, thread_name_prefix="footnote-push")
    # Jobs interrupted by a restart: the Parallel run survives server-side,
    # so re-attach by run_id; jobs that never got a run_id start over.
    application.state.tasks = set()
    for job_id, job in jobs.data.items():
        if job.get("status") in ACTIVE_STATUSES:
            task = asyncio.create_task(run_research(job_id))
            application.state.tasks.add(task)
            task.add_done_callback(application.state.tasks.discard)
    yield
    tasks = list(application.state.tasks)
    for task in tasks:
        task.cancel()
    # Unwind before the client closes — a task cancelled mid-request would
    # otherwise wake up holding a closed transport.
    await asyncio.gather(*tasks, return_exceptions=True)
    # Not waited on: a thread stuck in a push cannot be interrupted, and
    # shutdown should not be held hostage to one.
    application.state.push_pool.shutdown(wait=False)
    await application.state.client.aclose()


app = FastAPI(title="Footnote", version="1.0.0", lifespan=lifespan)

# The PWA is served from this origin, and the Shortcut and curl are not
# browsers, so nothing Footnote ships needs CORS. A wildcard here would let
# any page you happen to be visiting start research on a reachable instance
# and spend your Parallel credits — and with FOOTNOTE_TOKEN unset, that is
# the default install. Opt in explicitly if you write a browser client of
# your own: FOOTNOTE_CORS_ORIGINS=https://one.example,https://two.example
CORS_ORIGINS = [o.strip() for o in os.getenv("FOOTNOTE_CORS_ORIGINS", "").split(",")
                if o.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


MAX_BODY_BYTES = 64 * 1024       # a question is 4000 chars; keys are ~90


@app.middleware("http")
async def _limit_body(request: Request, call_next):
    """Refuse an oversized body before it is read into memory.

    Declared before the token check so it runs *inside* it: an oversized
    request to a locked instance should be unauthorized rather than told its
    body is too large, and the 413 has to carry the security headers like
    every other response.

    Content-Length only: a chunked upload has none, and bounding that means
    reading the stream, which is more machinery than a personal server on a
    private network needs.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"},
                            status_code=413)
    return await call_next(request)


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


def _token_matches(presented: str) -> bool:
    """compare_digest refuses non-ASCII strings outright, with a TypeError.

    A token in a query string is whatever the client sent, so the comparison
    is done on bytes — a non-ASCII token is simply wrong, not a 500.
    """
    try:
        return secrets.compare_digest(presented.encode("utf-8", "surrogatepass"),
                                      FOOTNOTE_TOKEN.encode("utf-8", "surrogatepass"))
    except (UnicodeEncodeError, AttributeError):
        return False


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

    if not _token_matches(_request_token(request)):
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(_UNAUTHORIZED_HTML, status_code=401)
        return JSONResponse(
            {"status": "error",
             "message": "Unauthorized: missing or wrong token "
                        "(Authorization: Bearer header or ?token= parameter)"},
            status_code=401,
        )

    if request.query_params.get("token"):
        # The cookie now carries it, so bounce a browser to a clean URL: a
        # token in the address bar ends up in history, logs and cache keys.
        # Only for navigations — an API client sending ?token= gets its
        # answer, not a redirect it may not follow.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            clean = request.url.remove_query_params("token")
            response = RedirectResponse(str(clean), status_code=303)
        else:
            response = await call_next(request)
        response.set_cookie(
            _TOKEN_COOKIE, FOOTNOTE_TOKEN, max_age=365 * 24 * 3600,
            httponly=True, samesite="strict",
            # Secure when the request came over TLS — behind Tailscale serve
            # or a reverse proxy that is always; on a plain-HTTP LAN setting
            # it would stop the cookie being sent at all.
            secure=request.url.scheme == "https")
        return response
    return await call_next(request)


# Declared last so it wraps the token middleware too — the 401 page and
# the token redirect are responses like any other.
# Belt and braces behind _Sanitizer: script-src 'self' means an injected
# inline script has nothing to execute even if the sanitizer were bypassed,
# and connect-src 'self' means it would have nowhere to send anything. Styles
# stay inline-capable (the markdown fallback carries a style attribute) and
# images still load from the archived pages. img-src has no http:, and the
# sanitizer drops http: images to match — a picture that is going to be
# blocked should be absent, not broken. data: images are allowed except SVG,
# which is a script container.
_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https: data:; font-src 'self'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    """An error a browser can get out of.

    A stale bookmark, a notification for a job since removed, a dossier moved
    in the notes folder — all of these are ordinary, and answering them with
    a bare JSON body leaves the reader on a page with no way back to the app.
    API clients still get the JSON they expect.
    """
    wants_html = ("text/html" in request.headers.get("accept", "")
                  and request.method == "GET")
    if not wants_html:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    detail = escape(str(exc.detail))
    return _page(f"{exc.status_code}", '<a href="/">← Footnote</a>',
                 f"<h1>Not here</h1><p>{detail}</p>"
                 f"<p class='source-note'>The dossier itself is a folder of "
                 f"Markdown files; if it was moved or removed, it is still "
                 f"wherever you put it.</p>",
                 status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """A 422 that can always be encoded.

    FastAPI's default handler echoes the offending input, and the input is
    sometimes exactly what could not be encoded — a question carrying an
    unpaired surrogate would fail validation and then fail again on the way
    out. ensure_ascii escapes it instead, and the body is written directly so
    nothing re-encodes it.
    """
    # Without the echoed input: it is unbounded, attacker-supplied, and the
    # message already says which field was refused and why.
    detail = [{k: v for k, v in error.items() if k not in ("input", "ctx")}
              for error in exc.errors()]
    body = json.dumps({"detail": detail}, ensure_ascii=True, default=str)
    return Response(body, media_type="application/json", status_code=422)


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
        problem = _question_problem(v)
        if problem:
            raise ValueError(problem)
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
    _put_job(job_id, {
        "id": job_id, "question": req.question, "processor": processor,
        "status": "queued", "progress": "Queued", "created_at": _now(),
        "run_id": "", "report_path": "", "notion_url": "", "error": "",
    })
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
    if job.get("status") == "done" or job.get("report_path"):
        # Completion and availability are different facts: the dossier lives
        # in a folder people reorganise, and a link to a file that is gone is
        # worse than no link. Always present for a finished job, so a record
        # with no path at all reads as unavailable rather than as unknown.
        public["report_available"] = _servable_report(
            job.get("report_path") or "") is not None
    return public


@app.get("/research/{job_id}")
async def job_status(job_id: str):
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_public(job)


@app.get("/jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=MAX_JOBS_KEPT)):
    ordered = sorted(jobs.data.values(),
                     key=lambda j: j.get("created_at", ""), reverse=True)
    return {"jobs": [_job_public(j) for j in ordered[:limit]],
            "active": sum(j.get("status") in ACTIVE_STATUSES for j in ordered)}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") in ACTIVE_STATUSES:
        raise HTTPException(409, "Job is still running")
    del jobs.data[job_id]
    jobs.save()
    return {"status": "deleted"}


def _inside(path: Path, folder: Path) -> bool:
    """Whether path really lives under folder, symlinks resolved.

    The dossier folder is a synced notes directory that people edit, so a
    name inside it can be a link to somewhere else entirely.
    """
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def _servable_report(report_path: str):
    """The report at this path, if it is one Footnote is willing to hand out.

    A regular file inside the output directory, resolved. The dossier lives
    in a synced notes folder: the name Footnote recorded can since have
    become a directory, or a link to anything the service account can read.
    """
    if not report_path:
        return None
    path = Path(report_path)
    return path if path.is_file() and _inside(path, OUTPUT_DIR) else None


def _report_file(job_id: str) -> Path:
    job = jobs.data.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    path = _servable_report(job.get("report_path") or "")
    if path is None:
        raise HTTPException(404, "No report for this job (yet)")
    return path


@app.get("/jobs/{job_id}/report.md")
async def report_markdown(job_id: str):
    path = _report_file(job_id)
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@app.get("/jobs/{job_id}/report")
async def report_html(job_id: str, embed: int = 0):
    """The dossier, rendered. `?embed=1` is the same body without the page,
    for reading it inside the app rather than navigating away from it."""
    path = _report_file(job_id)
    _, text = pipeline.split_front_matter(_read_file(path))
    if embed:
        return HTMLResponse(
            _render_markdown(text, base=f"/jobs/{job_id}/report"))
    rendered = _render_markdown(text)
    nav = (f'<a href="/">← Footnote</a> &nbsp;·&nbsp; '
           f'<a href="/jobs/{job_id}/report.md" download>Download .md</a>'
           f' &nbsp;·&nbsp; '
           f'<a href="/jobs/{job_id}/bundle.zip">Download everything (.zip)</a>')
    return _page(path.stem, nav, rendered)


def _source_file(job_id: str, name: str) -> Path:
    """Resolve an archived source copy by file name only — never by path."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404, "Not found")
    folder = _report_file(job_id).parent
    path = folder / "sources" / name
    if not path.is_file() or not _inside(path, folder):
        raise HTTPException(404, "No such source copy")
    return path


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _read_file(path: Path) -> str:
    """Read a dossier file. It lives in a folder other software writes to, so
    bytes that are not UTF-8 are shown as replacement characters rather than
    turned into a 500."""
    return path.read_text(encoding="utf-8", errors="replace")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _head(path: Path) -> str:
    """The delimiter-aware reader, so a long source URL is not cut in half."""
    return pipeline.read_front_matter(path)


def _source_entries(job: dict, folder: Path) -> list:
    """Every source behind a dossier: what was cited, what was archived.

    The citation list recorded on the job is the spine (it carries the ones
    that could *not* be archived, and why); the files actually present in
    `sources/` decide what is readable, so a copy deleted in the notes folder
    degrades to a plain citation instead of a broken link. Dossiers written
    before jobs kept a citation list are described by their files alone.
    """
    # Only what the read endpoint would serve: a link pointing out of the
    # folder is not an archived source, and a broken one must not take the
    # whole index down when its size is asked for.
    files = {f.name: f for f in sorted((folder / "sources").glob("*.md"))
             if f.is_file() and _inside(f, folder)}
    # Records come off a JSON file that can be edited or truncated; one bad
    # entry should cost that entry, not the whole index.
    stored = job.get("citations")
    listed = [(_str(cit.get("title")), _str(cit.get("url")),
               _str(cit.get("file")) if _str(cit.get("file")) in files else "",
               _str(cit.get("note")))
              for cit in (stored if isinstance(stored, list) else [])
              if isinstance(cit, dict)]
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
         # The same rule the report writer uses: absolute safe links only.
         # is_http_url would drop mailto:, which is a legitimate citation;
         # allowing relative URLs would let "not-a-url" resolve against
         # Footnote's own origin in the PWA.
         "url": url if pipeline.is_safe_url(url, relative_ok=False) else "",
         "file": name,
         "archived": bool(name),
         "read_url": base + quote(name) if name else "",
         "download_url": f"{base}{quote(name)}?raw=1" if name else "",
         "bytes": _size(files[name]) if name else 0,
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
async def report_source(job_id: str, name: str, raw: int = 0, embed: int = 0):
    """One archived source: rendered for reading, `?raw=1` for the file.

    The rendered form is also what the report's relative "local copy" links
    resolve to in the web view, exactly as they do in a notes app. `?embed=1`
    returns the same body without the page around it, for the PWA to show in
    place — the sanitising has already happened either way.
    """
    path = _source_file(job_id, name)
    if raw:
        return FileResponse(path, media_type="text/markdown", filename=path.name)
    meta, text = pipeline.split_front_matter(_read_file(path))
    origin = meta.get("source", "")
    title = meta.get("title") or path.stem
    retrieved = (f", retrieved {escape(meta['retrieved'])}"
                 if meta.get("retrieved") else "")
    if embed:
        base = f"/jobs/{job_id}/sources/{quote(name)}"
        return HTMLResponse(f'<p class="source-note">Archived copy{retrieved}'
                            f'</p>{_render_markdown(text, base=base)}')
    rendered = _render_markdown(text)

    # Back to the app, not only back to the report: this page is reached from
    # the source list as often as from the report, and "← Report" was then a
    # way out to somewhere the reader had never been.
    nav = [f'<a href="/">← Footnote</a>',
           f'<a href="/jobs/{job_id}/report">Report</a>']
    if _safe_url(origin):
        nav.append(f'<a href="{escape(origin, quote=True)}" target="_blank" '
                   f'rel="noopener noreferrer">Original page ↗</a>')
    nav.append('<a href="?raw=1" download>Download .md</a>')
    heading = (f'<h1>{escape(title)}</h1>'
               f'<p class="source-note">Archived copy{retrieved}</p>')
    return _page(title, " &nbsp;·&nbsp; ".join(nav), heading + rendered)


def _zip_bundle(report: Path) -> bytes:
    """The report and its archived sources, laid out as they are on disk."""
    folder = report.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(report, f"{folder.name}/{report.name}")
        for path in sorted((folder / "sources").glob("*.md")):
            if _inside(path, folder):      # never follow a link out of it
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
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_CLAIM_EMAIL):
        raise HTTPException(503, "Push is not configured (VAPID keys or "
                                 "VAPID_CLAIM_EMAIL missing)")
    # Checked on the way in: a subscription that cannot be pushed to is
    # otherwise kept forever and fails once per job, for every job.
    if not _valid_subscription({"endpoint": req.endpoint, "keys": req.keys}):
        raise HTTPException(
            422, "a subscription needs an https endpoint on a public address, "
                 "and p256dh and auth keys of the sizes the Push API sends")
    if (len(subs.data) >= MAX_SUBSCRIPTIONS
            and uuid.uuid5(uuid.NAMESPACE_URL, req.endpoint).hex not in subs.data):
        raise HTTPException(409, f"too many devices registered "
                                 f"({MAX_SUBSCRIPTIONS} maximum)")
    key = uuid.uuid5(uuid.NAMESPACE_URL, req.endpoint).hex
    # Only the two fields push needs: a subscription is stored indefinitely,
    # and there is no reason to keep whatever else a client attached.
    subs.data[key] = {"endpoint": req.endpoint,
                      "keys": {name: req.keys[name] for name in _KEY_BYTES}}
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
        "output_dir_writable": _output_dir_writable(),
        "parallel_configured": bool(PARALLEL_API_KEY),
        "firecrawl_configured": bool(FIRECRAWL_API_KEY),
        "respects_robots": RESPECT_ROBOTS,
        "notion_configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
        # The claim email is not optional: pywebpush signs with a
        # "mailto:{...}" subject, and push services reject an empty one.
        "push_configured": bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY
                                and VAPID_CLAIM_EMAIL and webpush is not None),
        "auth_required": bool(FOOTNOTE_TOKEN),
        "active_jobs": sum(j.get("status") in ACTIVE_STATUSES
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
