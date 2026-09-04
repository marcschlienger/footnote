# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""External-API pipeline: Parallel task runs, Firecrawl scrapes, report files.

The flow (driven by app.run_research):

    question ─► Parallel Task API (deep research, minutes)  ─► report text
                                                            └► basis: citations
    citations ─► Firecrawl /v2/scrape (markdown, concurrent) ─► local source copies
    everything ─► OUTPUT_DIR/"YYYY-MM-DD question…"/  (report.md + sources/)

Verified API contracts (August 2026):
  Parallel  POST https://api.parallel.ai/v1/tasks/runs   header x-api-key
            body {input, processor, task_spec:{output_schema:{type:"text",…}}}
            → 202 {run_id, status:"queued"}
            GET  …/runs/{id}/result?timeout=N   blocks ≤N s; 408 while active
            → {run:{status,…}, output:{type, content, basis:[{field, reasoning,
               confidence, citations:[{url, title, excerpts}]}]}}
  Firecrawl POST https://api.firecrawl.dev/v2/scrape     Bearer auth
            body {url, formats:["markdown"], onlyMainContent:true}
            → {success, data:{markdown, metadata:{title,…}}}
"""
from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import random
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

PARALLEL_BASE = "https://api.parallel.ai/v1"
# A run id goes into a URL path; keep it to what cannot change its shape.
_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
# A DNS name: labels of letters, digits and inner hyphens. IP literals are
# handled separately, since urlsplit().hostname strips a v6 address's
# brackets and leaves colons behind.
_HOSTNAME = re.compile(
    r"[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*")
MAX_HOSTNAME_BYTES = 253        # RFC 1035, which idna does not enforce
FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"

# Task API processors, cheap/fast → expensive/deep, with a generous overall
# polling deadline (seconds) per tier — upper bounds from the processor docs
# plus headroom for queueing.
PROCESSORS: dict[str, int] = {
    "lite": 20 * 60, "base": 20 * 60,
    "core": 40 * 60, "core2x": 60 * 60,
    "pro": 60 * 60,
    "ultra": 90 * 60, "ultra2x": 2 * 3600,
    "ultra4x": 3 * 3600, "ultra8x": 5 * 3600,
}
# Every processor also has a "-fast" variant (same capability, lower latency).
ALL_PROCESSORS = tuple(PROCESSORS) + tuple(f"{p}-fast" for p in PROCESSORS)

# What we ask the Task API to produce. Text schema: the description *is* the
# output instruction.
REPORT_SPEC = (
    "A thorough, well-structured research report in Markdown. Begin with a "
    "short paragraph that directly answers the question (bold the key "
    "finding), then organized sections with '##' headings covering the "
    "evidence in depth, then a '## Open questions' section if genuine "
    "uncertainties remain. Prefer concrete facts, numbers, and dates over "
    "generalities, and attribute claims to their sources by name in the "
    "prose. Do not append a bibliography or link list — sources are attached "
    "separately from the citation metadata."
)


# Schemes a dossier may link to. Anything else is written as text: a
# citation is data from the open web, and a Markdown file is portable — it
# will be opened by notes apps that follow links without asking.
SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def valid_run_id(run_id) -> bool:
    """The same shape a freshly returned run id has to satisfy.

    fullmatch, not match: `$` also matches before a terminal newline, so
    "trun_x\n" passed and was then sent as a differently encoded path.
    """
    return isinstance(run_id, str) and bool(_RUN_ID.fullmatch(run_id))


def ascii_host(url: str):
    """The hostname as a resolver will see it, or None if there is not one.

    One function, because *validating one form and classifying another* is
    how "127。0。0。1" got past the push policy: `is_http_url` checked the
    IDNA-normalised name while `is_push_endpoint` classified the original,
    and IDNA turns those ideographic full stops into ordinary dots.
    """
    text = str(url or "")
    # Rejected, not cleaned: callers keep the original string, so validating a
    # scrubbed version would bless a value nobody else has seen.
    if text != re.sub(r"[\x00-\x20]", "", text):
        return None
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        parsed.port                     # raises for an invalid port
    except ValueError:
        return None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)      # a literal address is a fine host
        return host
    except ValueError:
        pass
    try:
        # The regex below is an ASCII rule, and "bücher.example" is a
        # perfectly good host whose punycode passes it.
        encoded = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # No fallback to the original: the encoder rejects an over-long label
        # for a reason, and falling back admitted exactly what it refused.
        return None
    if len(encoded) > MAX_HOSTNAME_BYTES:
        return None                     # idna checks labels, not the whole name
    # A bare "." or "_" is syntactically an authority and addresses nothing.
    return encoded if _HOSTNAME.fullmatch(encoded) else None


def is_http_url(url: str) -> bool:
    """Fetchable over the web — a stricter test than is_safe_url's link policy.

    A host is required: "https:" satisfies a scheme check and is not an
    address, and such a value would otherwise be stored and retried forever
    as a push endpoint.
    """
    match = _SCHEME.match(str(url or ""))
    if match is None or match.group(1).lower() not in ("http", "https"):
        return False
    return ascii_host(url) is not None


def _as_list(value) -> list:
    """A JSON array, or nothing. `for x in 5` is a TypeError, not a shape."""
    return value if isinstance(value, list) else []


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub_surrogates(text: str) -> str:
    """Replace anything UTF-8 cannot represent, so this string can be stored."""
    return _LONE_SURROGATE.sub("\ufffd", text) if isinstance(text, str) else ""


def clean_json(value, _depth: int = 0):
    """A structure that json.dump cannot refuse.

    Applied where data is stored rather than field by field: a list of field
    names is exactly the kind of thing that goes stale, and the store has to
    hold whatever a job accumulated. Strings are scrubbed, keys are made
    strings, non-finite floats become null (NaN is not JSON and does not
    survive a reload), and anything unrecognised becomes its str().
    """
    if _depth > 32:                     # cyclic or absurd; not our data
        return None
    if isinstance(value, str):
        return scrub_surrogates(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {scrub_surrogates(str(k)): clean_json(v, _depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_json(v, _depth + 1) for v in value]
    return scrub_surrogates(str(value))


def has_lone_surrogate(text: str) -> bool:
    """Whether this string cannot be written as UTF-8.

    A lone surrogate survives JSON *decoding* — a client can send "\\ud800"
    — but json.dump(ensure_ascii=False) then refuses to encode it, and the
    store cannot be written again until the record is gone.
    """
    return bool(_LONE_SURROGATE.search(text))


def clean_text(value, fallback: str = "") -> str:
    """Text from outside, made into something this app can keep.

    The one rule applied at every ingress: a value that is not a string
    becomes the fallback (containers) or its str() (scalars), and anything
    UTF-8 cannot represent is replaced. Everything here crossed a network or
    came off a disk someone else can write to. A title that is a list reaches
    slug_for and takes the whole dossier down with a TypeError from
    unicodedata.normalize, long after the scrape it came from was recorded as
    a success; a lone surrogate does the same on the next save.
    """
    if isinstance(value, str):
        # Providers relay text off the open web: unpaired surrogates would
        # poison the job store, and NUL or ESC would sit in a Markdown file
        # as raw bytes and make it look binary to notes and indexing tools.
        return _CONTROL.sub("", _LONE_SURROGATE.sub("\ufffd", value))
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return fallback
    return str(value)


def is_push_endpoint(url: str) -> bool:
    """A push endpoint we are willing to have the server POST to.

    Stricter than is_http_url on purpose: this address is supplied by a
    client and then requested by the server, which is a server-side request
    forgery primitive if it can be aimed inwards. HTTPS only (push services
    are), no embedded credentials, and no address inside the machine or its
    network. Names are resolved by the push library, not here, so a name that
    resolves inward is not caught — the loopback and private *literals* are,
    which is what an attacker reaches for first.
    """
    if not is_http_url(url):
        return False
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        return False
    if parsed.username or parsed.password:
        return False
    # The same string is_http_url validated, which is the one a resolver will
    # be given — not the original, which IDNA may still fold onto loopback.
    host = (ascii_host(url) or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    address = _as_address(host)
    if address is not None:
        # is_global covers carrier-grade NAT (100.64/10), which is neither
        # private nor reserved — but Python also calls multicast and IPv6
        # site-local addresses global, so those are excluded by name.
        return (address.is_global and not address.is_multicast
                and not getattr(address, "is_site_local", False))
    return True                         # a name; the library resolves it


def _as_address(host: str):
    """The address this host denotes, including the resolver's shorthands.

    "127.1", "2130706433" and "0x7f000001" all reach 127.0.0.1 — inet_aton
    accepts them and so does every resolver, while ipaddress alone does not.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def scrapable(citations: list, max_sources: int) -> list:
    """The citations archiving will actually attempt.

    mailto: and the like are legitimate citations but not scrape targets;
    spending a request and a credit to be told so helps nobody. The caller
    counts these too, so the progress it announces matches what happens.
    """
    return [c for c in citations if is_http_url(c.url)][:max_sources]


def is_safe_url(url: str, relative_ok: bool = True) -> bool:
    """True for the schemes above, and for relative URLs when allowed."""
    if not url:
        return False
    # Browsers ignore control characters inside a scheme ("java\tscript:").
    match = _SCHEME.match(re.sub(r"[\x00-\x20]", "", str(url)))
    if match is None:
        return relative_ok
    return match.group(1).lower() in SAFE_SCHEMES


class PipelineError(Exception):
    """A step failed in a way worth showing to the user."""


@dataclass
class Citation:
    url: str
    title: str = ""
    excerpts: list[str] = field(default_factory=list)


@dataclass
class ResearchResult:
    content: str                 # Markdown report text
    citations: list[Citation]    # deduplicated, in basis order
    confidence: str = ""         # low/medium/high, if the processor emits it
    reasoning: str = ""          # basis reasoning for the output field


@dataclass
class SourceCopy:
    url: str
    title: str
    markdown: str
    ok: bool
    error: str = ""


@dataclass
class WrittenReport:
    """What write_report put on disk."""

    path: Path                    # the report .md itself
    source_files: dict            # cited URL → file name inside sources/


# ---------------------------------------------------------------------------
# Parallel Task API
# ---------------------------------------------------------------------------

async def start_task_run(
    client: httpx.AsyncClient, api_key: str, question: str, processor: str
) -> str:
    """Create a task run; returns the run_id (the run continues server-side)."""
    resp = await client.post(
        f"{PARALLEL_BASE}/tasks/runs",
        headers={"x-api-key": api_key},
        json={
            "input": question,
            "processor": processor,
            "task_spec": {
                "output_schema": {"type": "text", "description": REPORT_SPEC}
            },
        },
    )
    if resp.status_code not in (200, 201, 202):
        raise PipelineError(
            f"Parallel refused the task ({resp.status_code}): {_err_detail(resp)}"
        )
    try:
        body = resp.json()
    except ValueError:
        body = None
    raw = body.get("run_id") if isinstance(body, dict) else None
    # Not coerced: 123 is not a run id, and _text would happily make it "123"
    # and interpolate that into every later URL.
    run_id = raw.strip() if isinstance(raw, str) else ""
    if not valid_run_id(run_id):
        # Not retried: the POST may well have created a run, and asking again
        # would pay for a second one while the first goes unrecorded.
        raise PipelineError(
            "Parallel accepted the task but returned no usable run_id")
    return run_id


async def fetch_task_result(
    client: httpx.AsyncClient, api_key: str, run_id: str, deadline_s: int
) -> ResearchResult:
    """Long-poll the result endpoint until the run finishes or deadline_s passes.

    The endpoint blocks up to `timeout` seconds per request and returns 408
    while the run is still active, so this is one outstanding request at a
    time, not a tight loop.
    """
    started = time.monotonic()

    def left() -> float:
        return deadline_s - (time.monotonic() - started)

    while True:
        remaining = left()
        if remaining <= 0:
            raise PipelineError(
                f"Research run {run_id} still not finished after "
                f"{deadline_s // 60} minutes — giving up"
            )
        try:
            # httpx's timeouts bound individual operations — connect, and
            # inactivity between reads — not the wall clock of the whole
            # request. A slow response that keeps trickling can outlast them,
            # so the call is wrapped in the deadline as well.
            resp = await asyncio.wait_for(
                client.get(
                    f"{PARALLEL_BASE}/tasks/runs/{quote(run_id, safe='')}/result",
                    headers={"x-api-key": api_key},
                    # Ask the provider to hold the connection for at most what
                    # is left; its own minimum is 10 s, which is why the
                    # timeouts around it matter.
                    params={"timeout": int(min(120, max(1, remaining)))},
                    timeout=httpx.Timeout(min(150.0, remaining),
                                          connect=min(15.0, remaining)),
                ),
                timeout=remaining,
            )
        except (httpx.TransportError, asyncio.TimeoutError):
            # A transient blip, or our own timeout firing. The run is
            # server-side and keeps going; the loop decides whether there is
            # time left to ask again.
            await asyncio.sleep(min(10, max(left(), 0)))
            continue
        if resp.status_code == 408:   # still running
            continue
        if resp.status_code in RETRYABLE_STATUS:
            # The run is finished and paid for; a rate limit or a bad gateway
            # in front of the result is no reason to throw it away — but the
            # wait has to fit inside what is left of the deadline, or the
            # bound the caller was given is not a bound.
            await asyncio.sleep(min(_retry_after(resp, RESULT_RETRY_S),
                                    max(left(), 0)))
            continue
        if resp.status_code != 200:
            raise PipelineError(
                f"Parallel run {run_id} failed ({resp.status_code}): "
                f"{_err_detail(resp)}"
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            # A broken hop, not an answer.
            await asyncio.sleep(min(RESULT_RETRY_S, max(left(), 0)))
            continue
        return _parse_task_result(payload, run_id)


def _parse_task_result(data: dict, run_id: str) -> ResearchResult:
    run = data.get("run")
    run = run if isinstance(run, dict) else {}
    status = clean_text(run.get("status"), fallback="completed") or "completed"
    if status != "completed":
        error = run.get("error")
        err = clean_text((error if isinstance(error, dict) else {}).get("message"),
                    fallback=status)
        raise PipelineError(f"Parallel run {run_id} ended as {status}: {err}")

    output = data.get("output")
    output = output if isinstance(output, dict) else {}
    content = output.get("content", "")
    if isinstance(content, dict):     # json output type — flatten to text
        content = "\n\n".join(f"## {k}\n\n{v}" for k, v in content.items())
    content = clean_text(content)
    if not content.strip():
        raise PipelineError(f"Parallel run {run_id} returned an empty report")

    citations: list[Citation] = []
    seen: set[str] = set()
    confidence = reasoning = ""
    for basis in _as_list(output.get("basis")):
        if not isinstance(basis, dict):
            continue
        confidence = confidence or clean_text(basis.get("confidence"))
        reasoning = reasoning or clean_text(basis.get("reasoning"))
        for cit in _as_list(basis.get("citations")):
            if not isinstance(cit, dict):
                continue
            url = clean_text(cit.get("url")).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            citations.append(
                Citation(
                    url=url,
                    title=clean_text(cit.get("title")).strip(),
                    excerpts=[clean_text(e) for e in _as_list(cit.get("excerpts"))
                              if clean_text(e)],
                )
            )
    return ResearchResult(
        content=content.strip(),
        citations=citations,
        confidence=confidence,
        reasoning=reasoning.strip(),
    )


def _err_detail(resp: httpx.Response) -> str:
    """A provider's explanation — which is provider text, and gets scrubbed.

    Error strings end up in the job store and in the dossier's "could not be
    archived" list, so an unpaired surrogate here fails the write just as
    surely as one in a title.
    """
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        return clean_text(message) or clean_text(resp.text)[:200]
    except Exception:                                      # noqa: BLE001
        return clean_text(resp.text)[:200]


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------

# The free plan's published limits, which are also the safe defaults: 10
# /scrape requests per minute and 2 concurrent browsers. A paid plan raises
# both (FIRECRAWL_RATE_LIMIT / FIRECRAWL_CONCURRENCY); 0 disables pacing.
FREE_RATE_LIMIT = 10
FREE_CONCURRENCY = 2

# Retry these rather than losing the citation; 402 is not among them, because
# exhausted credits do not come back by asking again.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BASE_BACKOFF_S = 2.0
MAX_BACKOFF_S = 120.0
RESULT_RETRY_S = 5.0         # between attempts at a finished run's result
OUT_OF_CREDITS = "Firecrawl credits exhausted (HTTP 402)"
# How long a 402 speaks for the whole key. Long enough that the jobs behind
# it don't each rediscover the same wall, short enough that a top-up or the
# monthly reset is noticed without a restart.
CREDIT_COOLDOWN_S = 15 * 60


class ScrapeLimiter:
    """One API key's budget, shared by every job that spends it.

    Firecrawl counts requests per key, so a limiter built per job is no limit
    at all: two jobs archiving at once would double both the request rate and
    the browsers in flight. The app builds exactly one of these and hands it
    to every scrape.

    Built inside the running loop — asyncio.Semaphore binds to the loop it is
    created on for Python < 3.10.
    """

    def __init__(self, rate_limit: int = FREE_RATE_LIMIT,
                 concurrency: int = FREE_CONCURRENCY):
        self.pacer = _Pacer(rate_limit)
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.concurrency = max(1, concurrency)
        self._credits_gone_at = None

    def out_of_credits(self) -> bool:
        """Whether the key is known to be spent, as far as anyone can tell.

        Credits belong to the key, so one job's 402 answers for the jobs
        queued behind it. It expires: credits do come back — a top-up, or the
        monthly reset — and a latched flag would silently stop archiving for
        the life of the process.
        """
        if self._credits_gone_at is None:
            return False
        if time.monotonic() - self._credits_gone_at >= CREDIT_COOLDOWN_S:
            self._credits_gone_at = None       # let one request find out
            return False
        return True

    def note_out_of_credits(self) -> None:
        self._credits_gone_at = time.monotonic()


class _Pacer:
    """Holds request starts inside a fixed per-minute budget.

    Firecrawl counts requests, not successes, so the cheapest way to stay
    inside the limit is never to exceed it: a waiter takes its turn only once
    the oldest start in the window has aged out. The lock is held across the
    sleep on purpose — waiters then re-check one at a time instead of waking
    together and bursting.
    """

    def __init__(self, per_minute: int, window_s: float = 60.0):
        self.per_minute = per_minute
        self.window_s = window_s
        self.starts: deque = deque()
        self.lock = asyncio.Lock()

    async def take(self) -> None:
        if self.per_minute <= 0:            # pacing disabled
            return
        async with self.lock:
            self._expire()
            if len(self.starts) >= self.per_minute:
                await asyncio.sleep(self.window_s - (time.monotonic()
                                                     - self.starts[0]))
                self._expire()
            self.starts.append(time.monotonic())

    def _expire(self) -> None:
        now = time.monotonic()
        while self.starts and now - self.starts[0] >= self.window_s:
            self.starts.popleft()


def _retry_after(resp: httpx.Response, fallback_s: float) -> float:
    """Seconds to wait before retrying — the server's answer wins."""
    header = resp.headers.get("retry-after", "").strip()
    if header:
        try:
            return min(max(float(header), 0.0), MAX_BACKOFF_S)
        except ValueError:
            pass
        try:                                # RFC 9110 allows an HTTP date
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            wait = (when - datetime.now(timezone.utc)).total_seconds()
            return min(max(wait, 0.0), MAX_BACKOFF_S)
        except (TypeError, ValueError):
            pass
    return min(fallback_s * random.uniform(0.5, 1.5), MAX_BACKOFF_S)


async def scrape_sources(
    client: httpx.AsyncClient,
    api_key: str,
    todo: list[Citation],
    limiter: ScrapeLimiter,
    on_progress=None,
) -> list[SourceCopy]:
    """Fetch local Markdown copies of the cited pages (best-effort).

    Paced and retried to survive a free-plan key: `limiter` holds the whole
    key inside its request rate and browser count, a rate-limited or transient
    failure is retried honouring Retry-After, and the first 402 stops the
    batch — once the credits are gone every further request would fail
    identically, and the dossier says so once instead of N times — for this
    job and for the ones queued behind it, since the credits belong to the
    key. Requests already in flight when that 402 lands still complete, so up
    to `limiter.concurrency` are spent, not one.

    Archiving is best-effort and can never fail the job: every path out of
    here is a SourceCopy, including the ones that had no business happening.

    `todo` is what will be attempted, already prepared with `scrapable()` —
    one place decides which citations are worth a request, so the count the
    caller announces is the count that happens.

    `on_progress(done, total)` is called as copies land; archiving is slow
    enough under pacing to be worth reporting.
    """
    state = {"done": 0}

    async def one(cit: Citation) -> SourceCopy:
        try:
            if limiter.out_of_credits():
                copy = SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)
            else:
                copy = await _scrape_one(client, api_key, cit, limiter)
        except Exception as exc:                           # noqa: BLE001
            copy = SourceCopy(cit.url, cit.title, "", False, clean_text(str(exc))[:200])
        state["done"] += 1
        if on_progress:
            try:
                on_progress(state["done"], len(todo))
            except Exception as exc:                       # noqa: BLE001
                # A progress report is not worth a source, let alone a job.
                print(f"progress callback failed: {exc}", flush=True)
        return copy

    return list(await asyncio.gather(*(one(c) for c in todo)))


async def _scrape_one(
    client: httpx.AsyncClient,
    api_key: str,
    cit: Citation,
    limiter: ScrapeLimiter,
) -> SourceCopy:
    backoff = BASE_BACKOFF_S
    error = "no attempt made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await limiter.pacer.take()
        # Waiting for a slot can take a minute; the credits may have run out
        # while this request sat in the queue.
        if limiter.out_of_credits():
            return SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)
        try:
            # The semaphore models Firecrawl's concurrent *browsers*, so it
            # covers the request and nothing else — a worker sleeping out a
            # backoff or waiting on the pacer is not using a browser, and
            # holding a slot through that would make the real concurrency
            # lower than the number configured.
            async with limiter.sem:
                # Checked here, holding the slot: a worker that queued while
                # the credits were good must not spend one after a 402 has
                # landed, or the batch bound becomes the batch size.
                if limiter.out_of_credits():
                    return SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)
                resp = await client.post(
                    f"{FIRECRAWL_BASE}/scrape",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "url": cit.url,
                        "formats": ["markdown"],
                        "onlyMainContent": True,
                    },
                    timeout=httpx.Timeout(90.0, connect=15.0),
                )
        except Exception as exc:                           # noqa: BLE001
            error = clean_text(str(exc))[:200]
            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(min(backoff * random.uniform(0.5, 1.5),
                                    MAX_BACKOFF_S))
            backoff *= 2
            continue

        if resp.status_code == 402:
            limiter.note_out_of_credits()
            return SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)

        malformed = False
        try:
            data = resp.json() if resp.content else {}
        except ValueError:      # an HTML 502 from a proxy, say — not our JSON
            data, malformed = {}, True
        if not isinstance(data, dict) or (
                "data" in data and not isinstance(data["data"], dict)):
            # Valid JSON, wrong shape — a list, a bare null, or a "data" that
            # is not an object. Same fault as unparseable text, and it takes
            # the same retry path rather than an AttributeError from the first
            # .get() or a misleading "empty extraction".
            data, malformed = {}, True
        # `is True`: the string "false" is truthy, and this field decides
        # whether a page counts as archived.
        if resp.status_code == 200 and data.get("success") is True:
            page = data.get("data") or {}
            md = clean_text(page.get("markdown"))
            metadata = page.get("metadata")
            title = clean_text((metadata if isinstance(metadata, dict) else {}).get(
                "title"), fallback=cit.title)
            if not md.strip():
                return SourceCopy(cit.url, title, "", False, "empty extraction")
            return SourceCopy(cit.url, title or cit.url, md, True)

        if malformed:
            # A 200 that is not the documented JSON is a broken hop, not an
            # answer: say so, and treat it like the other transient faults.
            error = f"HTTP {resp.status_code} with an unreadable body"
        else:
            error = clean_text(data.get("error"))[:200] or f"HTTP {resp.status_code}"
        retryable = malformed or resp.status_code in RETRYABLE_STATUS
        if not retryable or attempt == MAX_ATTEMPTS:
            break
        await asyncio.sleep(_retry_after(resp, backoff))
        backoff *= 2

    return SourceCopy(cit.url, cit.title, "", False, error)


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slug_for(question: str, max_bytes: int = 64) -> str:
    """Filesystem- and Obsidian-safe slug that stays human-readable.

    The budget is in *bytes*, because that is what filesystems limit: ext4
    allows 255 bytes per component, and 64 emoji are 256. The date prefix and
    a possible " (2)" have to fit alongside, hence the modest default.
    """
    text = unicodedata.normalize("NFKC", _LONE_SURROGATE.sub("", question)).strip()
    text = _UNSAFE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text.encode("utf-8")) > max_bytes:
        text = _truncate_bytes(text, max_bytes)
        text = text.rsplit(" ", 1)[0] if " " in text.strip() else text
    return text.strip(" .") or "research"


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Cut to a byte budget without splitting a character."""
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore")


def _claim_dir(base: Path, name: str, with_sources: bool) -> Path:
    """Create the dossier folder, taking the first name nobody else holds.

    mkdir is the claim: testing existence first and creating after leaves a
    window where two jobs finishing the same question pick the same name, and
    the loser's paid research dies on FileExistsError.
    """
    for n in range(1, 1000):
        candidate = base / (name if n == 1 else f"{name} ({n})")
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        if with_sources:
            (candidate / "sources").mkdir()
        return candidate
    raise PipelineError(f"could not find a free folder name for {name!r}")


# Everything C0 except the three that carry meaning in a document.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")


def _one_line(text: str) -> str:
    """Collapse anything that would break a single-line field — or the write.

    Surrogates are scrubbed here as well as at the provider boundary: this is
    the last thing every value passes through before it reaches a file.
    """
    return " ".join(scrub_surrogates(str(text)).split())


def _md_label(text: str) -> str:
    """Link text: brackets and backslashes would end the label early."""
    return re.sub(r"([\\\[\]])", r"\\\1", _one_line(text))


def _md_code(text: str) -> str:
    """A code span that its own content cannot end.

    CommonMark closes a span with the same run of backticks that opened it,
    so the fence has to be longer than the longest run inside — a provider
    URL with a backtick in it would otherwise close the span and let the
    rest through as Markdown.
    """
    body = _one_line(text)
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"


def _md_quote(text: str) -> str:
    """Every line of a block quote needs its own marker, not just the first."""
    lines = [line.strip() for line in str(text).strip().splitlines()]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _md_url(url: str) -> str:
    """Link destination, in the angle-bracket form when it needs one."""
    cleaned = _CONTROL.sub("", str(url)).replace("<", "").replace(">", "")
    return f"<{cleaned}>" if any(c in cleaned for c in " ()") else cleaned


def write_report(
    output_dir: Path,
    question: str,
    processor: str,
    result: ResearchResult,
    sources: list[SourceCopy],
    job_id: str = "",
) -> WrittenReport:
    """Write the report folder; returns the report path and its source files.

    Layout (one folder per research question, friendly to Obsidian/Nextcloud):

        OUTPUT_DIR/2026-08-03 how do solid-state batteries work/
        ├── how do solid-state batteries work.md      ← the report
        └── sources/
            ├── 01 Nature — Solid-state batteries.md  ← local copies
            └── …
    """
    slug = slug_for(question)
    folder = _claim_dir(output_dir, f"{date_str()} {slug}", bool(sources))

    # Number copies by their citation, not by how many scrapes happened to
    # succeed: "01" must be the first cited source, so the evidence in the
    # folder and the numbered list in the report refer to the same thing.
    numbers = {cit.url: i for i, cit in enumerate(result.citations, start=1)}
    saved: dict = {}                      # source URL → file name in sources/
    for fallback, src in enumerate((s for s in sources if s.ok), start=1):
        stitle = slug_for(src.title or src.url, 60)
        fname = f"{numbers.get(src.url, fallback):02d} {stitle}.md"
        body = (
            f'---\nsource: "{_yaml_escape(src.url)}"\n'
            f'title: "{_yaml_escape(src.title)}"\n'
            f"retrieved: {date_str()}\n---\n\n{clean_text(src.markdown).strip()}\n"
        )
        (folder / "sources" / fname).write_text(clean_text(body),
                                                encoding="utf-8")
        saved[src.url] = fname

    lines = [
        "---",
        f'question: "{_yaml_escape(question)}"',
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"processor: {processor}",
    ]
    if result.confidence:
        lines.append(f'confidence: "{_yaml_escape(result.confidence)}"')
    lines += [f"sources: {len(result.citations)}"]
    if job_id:
        lines.append(f"job: {job_id}")     # so a restart can find its own work
    lines += ["app: Footnote", "---", ""]
    lines += [f"# {_one_line(question)}", "", clean_text(result.content), ""]

    if result.citations:
        lines += ["## Sources", ""]
        for i, cit in enumerate(result.citations, start=1):
            label = cit.title or cit.url
            if is_safe_url(cit.url, relative_ok=False):
                entry = f"{i}. [{_md_label(label)}]({_md_url(cit.url)})"
            else:
                # Written as text: the dossier is portable, and the app that
                # opens it next may follow links without asking.
                entry = (f"{i}. {_md_label(label)} — not a web link: "
                         f"{_md_code(cit.url)}")
            if cit.url in saved:
                entry += f" — [local copy](<sources/{saved[cit.url]}>)"
            lines.append(entry)
            if cit.excerpts:
                first = " ".join(cit.excerpts[0].split())
                lines.append(f"   > {first[:400]}")
        lines.append("")

    failed = [s for s in sources if not s.ok]
    if failed:
        lines += ["## Sources that could not be archived", ""]
        lines += [f"- {_md_url(s.url)} — {_one_line(s.error)}" for s in failed]
        lines.append("")

    if result.reasoning:
        lines += ["## Method note", "", _md_quote(result.reasoning), ""]

    report = folder / f"{slug}.md"
    report.write_text(clean_text("\n".join(lines)), encoding="utf-8")
    return WrittenReport(path=report, source_files=saved)


def archived_source_files(report: Path) -> dict:
    """Map each archived source's URL to its file name, read off the disk.

    Used when a job is recovering: the copies are there, but the record of
    which citation each belongs to did not survive.
    """
    found: dict = {}
    folder = report.parent / "sources"
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.md")):
        # Same boundary the read endpoint applies: a link out of the folder
        # is not an archived source, and must not be credited to a citation.
        if not path.is_file() or not _inside(path, report.parent):
            continue
        try:
            meta, _ = split_front_matter(read_front_matter(path))
        except OSError:
            continue
        if meta.get("source"):
            found[meta["source"]] = path.name
    return found


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def read_front_matter(path: Path, cap: int = 64 * 1024) -> str:
    """Read as far as the closing delimiter rather than a fixed guess.

    A long source URL or title can push the block past any fixed prefix; the
    cap is only there so a file without a closing delimiter cannot be read
    whole.
    """
    chunks: list = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        while sum(len(c) for c in chunks) < cap:
            chunk = handle.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if "\n---" in "".join(chunks)[3:]:
                break
    return "".join(chunks)


def split_front_matter(text: str) -> tuple:
    """Split a Footnote-written file into (metadata, body).

    Understands only the flat `key: value` block write_report emits — it is a
    reader for our own files, not a YAML parser. Text without a frontmatter
    block comes back unchanged with empty metadata.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        # The notes folder is editable by people and sync clients: what comes
        # back out of it is outside text like any other.
        meta[clean_text(key.strip())] = clean_text(value)
    return meta, clean_text(text[end + 4:].lstrip("\n"))


def date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yaml_escape(text: str) -> str:
    """Escape for a double-quoted YAML scalar, which is one line by definition.

    A question typed into a textarea can contain newlines; left in, they end
    the scalar and turn the rest of the question into bogus YAML keys.
    """
    return (_one_line(text).replace("\\", "\\\\").replace('"', '\\"'))


# ---------------------------------------------------------------------------
# Optional: Notion mirror
# ---------------------------------------------------------------------------

NOTION_BASE = "https://api.notion.com/v1"
NOTION_MAX_BLOCKS = 100      # per page-create call


async def save_to_notion(
    client: httpx.AsyncClient,
    api_key: str,
    database_id: str,
    question: str,
    result: ResearchResult,
) -> str:
    """Create a Notion page with the report; returns its URL.

    Best-effort mirror for reading on the go — the file written by
    write_report stays the canonical copy.
    """
    # Notion takes at most 100 blocks in one create, and the sources are the
    # point of the dossier: keep room for them rather than letting a long
    # report use the whole allowance.
    cited = [c for c in result.citations if is_safe_url(c.url, relative_ok=False)]
    reserved = min(len(cited) + 1, 30) if cited else 0
    body_limit = NOTION_MAX_BLOCKS - reserved

    children: list[dict] = []
    for para in result.content.split("\n\n"):
        text = para.strip()
        if not text:
            continue
        if text.startswith("#"):
            level = min(3, len(text) - len(text.lstrip("#")))
            children.append(_notion_block(f"heading_{max(2, level)}",
                                          text.lstrip("# ").strip()))
        else:
            for chunk in _chunks(text, 1900):
                children.append(_notion_block("paragraph", chunk))
    del children[body_limit:]
    if cited:
        children.append(_notion_block("heading_2", "Sources"))
        for cit in cited[:max(reserved - 1, 0)]:
            # An unsafe URL would have the whole page rejected; those
            # citations are already listed as text in the dossier itself.
            children.append(_notion_block("paragraph", cit.title or cit.url,
                                          link=cit.url))
    resp = await client.post(
        f"{NOTION_BASE}/pages",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
        },
        json={
            "parent": {"database_id": database_id},
            "properties": {"Name": {"title": [{"text": {"content": question[:100]}}]}},
            "children": children[:NOTION_MAX_BLOCKS],
        },
    )
    if resp.status_code != 200:
        raise PipelineError(f"Notion save failed ({resp.status_code}): "
                            f"{resp.text[:200]}")
    try:
        created = resp.json()
    except ValueError:
        created = None
    if not isinstance(created, dict):
        raise PipelineError("Notion returned an unreadable response")
    return clean_text(created.get("url"))


def _notion_block(kind: str, text: str, link: str | None = None) -> dict:
    rich = {"type": "text", "text": {"content": text[:1900]}}
    if link:
        rich["text"]["link"] = {"url": link}
    return {"object": "block", "type": kind, kind: {"rich_text": [rich]}}


def _chunks(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)] or [""]
