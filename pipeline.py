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
import random
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

PARALLEL_BASE = "https://api.parallel.ai/v1"
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
    run_id = resp.json().get("run_id", "")
    if not run_id:
        raise PipelineError("Parallel returned no run_id")
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
    while True:
        remaining = deadline_s - (time.monotonic() - started)
        if remaining <= 0:
            raise PipelineError(
                f"Research run {run_id} still not finished after "
                f"{deadline_s // 60} minutes — giving up"
            )
        try:
            resp = await client.get(
                f"{PARALLEL_BASE}/tasks/runs/{run_id}/result",
                headers={"x-api-key": api_key},
                params={"timeout": int(min(120, max(10, remaining)))},
                timeout=httpx.Timeout(150.0, connect=15.0),
            )
        except httpx.TransportError:
            await asyncio.sleep(10)   # transient network blip; run is server-side
            continue
        if resp.status_code == 408:   # still running
            continue
        if resp.status_code != 200:
            raise PipelineError(
                f"Parallel run {run_id} failed ({resp.status_code}): "
                f"{_err_detail(resp)}"
            )
        return _parse_task_result(resp.json(), run_id)


def _parse_task_result(data: dict, run_id: str) -> ResearchResult:
    status = (data.get("run") or {}).get("status", "completed")
    if status != "completed":
        err = ((data.get("run") or {}).get("error") or {}).get("message", status)
        raise PipelineError(f"Parallel run {run_id} ended as {status}: {err}")

    output = data.get("output") or {}
    content = output.get("content", "")
    if isinstance(content, dict):     # json output type — flatten to text
        content = "\n\n".join(f"## {k}\n\n{v}" for k, v in content.items())
    if not str(content).strip():
        raise PipelineError(f"Parallel run {run_id} returned an empty report")

    citations: list[Citation] = []
    seen: set[str] = set()
    confidence = reasoning = ""
    for basis in output.get("basis") or []:
        confidence = confidence or (basis.get("confidence") or "")
        reasoning = reasoning or (basis.get("reasoning") or "")
        for cit in basis.get("citations") or []:
            url = (cit.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            citations.append(
                Citation(
                    url=url,
                    title=(cit.get("title") or "").strip(),
                    excerpts=[e for e in (cit.get("excerpts") or []) if e],
                )
            )
    return ResearchResult(
        content=str(content).strip(),
        citations=citations,
        confidence=confidence,
        reasoning=reasoning.strip(),
    )


def _err_detail(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return err.get("message") or resp.text[:200]
    except Exception:
        return resp.text[:200]


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
OUT_OF_CREDITS = "Firecrawl credits exhausted (HTTP 402)"


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
        except ValueError:                  # HTTP-date form: back off instead
            pass
    return min(fallback_s * random.uniform(0.5, 1.5), MAX_BACKOFF_S)


async def scrape_sources(
    client: httpx.AsyncClient,
    api_key: str,
    citations: list[Citation],
    max_sources: int,
    limiter: ScrapeLimiter,
    on_progress=None,
) -> list[SourceCopy]:
    """Fetch local Markdown copies of the cited pages (best-effort).

    Paced and retried to survive a free-plan key: `limiter` holds the whole
    key inside its request rate and browser count, a rate-limited or transient
    failure is retried honouring Retry-After, and the first 402 stops the
    batch — once the credits are gone every further request would fail
    identically, and the dossier says so once instead of N times. Requests
    already in flight when that 402 lands still complete, so up to
    `limiter.concurrency` are spent, not one.

    Archiving is best-effort and can never fail the job: every path out of
    here is a SourceCopy, including the ones that had no business happening.

    `on_progress(done, total)` is called as copies land; archiving is slow
    enough under pacing to be worth reporting.
    """
    todo = citations[:max_sources]
    state = {"out_of_credits": False, "done": 0}

    async def one(cit: Citation) -> SourceCopy:
        try:
            async with limiter.sem:
                if state["out_of_credits"]:
                    copy = SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)
                else:
                    copy = await _scrape_one(client, api_key, cit, limiter, state)
        except Exception as exc:                           # noqa: BLE001
            copy = SourceCopy(cit.url, cit.title, "", False, str(exc)[:200])
        state["done"] += 1
        if on_progress:
            on_progress(state["done"], len(todo))
        return copy

    return list(await asyncio.gather(*(one(c) for c in todo)))


async def _scrape_one(
    client: httpx.AsyncClient,
    api_key: str,
    cit: Citation,
    limiter: ScrapeLimiter,
    state: dict,
) -> SourceCopy:
    backoff = BASE_BACKOFF_S
    error = "no attempt made"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await limiter.pacer.take()
        # Waiting for a slot can take a minute; the credits may have run out
        # while this request sat in the queue.
        if state["out_of_credits"]:
            return SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)
        try:
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
            error = str(exc)[:200]
            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(min(backoff * random.uniform(0.5, 1.5),
                                    MAX_BACKOFF_S))
            backoff *= 2
            continue

        if resp.status_code == 402:
            state["out_of_credits"] = True
            return SourceCopy(cit.url, cit.title, "", False, OUT_OF_CREDITS)

        try:
            data = resp.json() if resp.content else {}
        except ValueError:      # an HTML 502 from a proxy, say — not our JSON
            data = {}
        if resp.status_code == 200 and data.get("success", False):
            page = data.get("data") or {}
            md = page.get("markdown") or ""
            title = (page.get("metadata") or {}).get("title") or cit.title
            if not md.strip():
                return SourceCopy(cit.url, title, "", False, "empty extraction")
            return SourceCopy(cit.url, title or cit.url, md, True)

        error = str(data.get("error") or f"HTTP {resp.status_code}")[:200]
        if resp.status_code not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS:
            break
        await asyncio.sleep(_retry_after(resp, backoff))
        backoff *= 2

    return SourceCopy(cit.url, cit.title, "", False, error)


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def slug_for(question: str, max_len: int = 64) -> str:
    """Filesystem- and Obsidian-safe slug that stays human-readable."""
    text = unicodedata.normalize("NFKC", question).strip()
    text = _UNSAFE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] or text[:max_len]
    return text.strip(" .") or "research"


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


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _one_line(text: str) -> str:
    """Collapse anything that would break a single-line field."""
    return " ".join(str(text).split())


def _md_label(text: str) -> str:
    """Link text: brackets and backslashes would end the label early."""
    return re.sub(r"([\\\[\]])", r"\\\1", _one_line(text))


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
            f"---\nsource: {src.url}\ntitle: \"{_yaml_escape(src.title)}\"\n"
            f"retrieved: {date_str()}\n---\n\n{src.markdown.strip()}\n"
        )
        (folder / "sources" / fname).write_text(body, encoding="utf-8")
        saved[src.url] = fname

    lines = [
        "---",
        f'question: "{_yaml_escape(question)}"',
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"processor: {processor}",
    ]
    if result.confidence:
        lines.append(f"confidence: {result.confidence}")
    lines += [f"sources: {len(result.citations)}", "app: Footnote", "---", ""]
    lines += [f"# {_one_line(question)}", "", result.content, ""]

    if result.citations:
        lines += ["## Sources", ""]
        for i, cit in enumerate(result.citations, start=1):
            label = cit.title or cit.url
            entry = f"{i}. [{_md_label(label)}]({_md_url(cit.url)})"
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
        lines += ["## Method note", "", f"> {result.reasoning}", ""]

    report = folder / f"{slug}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return WrittenReport(path=report, source_files=saved)


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
        meta[key.strip()] = value
    return meta, text[end + 4:].lstrip("\n")


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
    if result.citations:
        children.append(_notion_block("heading_2", "Sources"))
        for cit in result.citations[:30]:
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
            "children": children[:100],
        },
    )
    if resp.status_code != 200:
        raise PipelineError(f"Notion save failed ({resp.status_code}): "
                            f"{resp.text[:200]}")
    return resp.json().get("url", "")


def _notion_block(kind: str, text: str, link: str | None = None) -> dict:
    rich = {"type": "text", "text": {"content": text[:1900]}}
    if link:
        rich["text"]["link"] = {"url": link}
    return {"object": "block", "type": kind, kind: {"rich_text": [rich]}}


def _chunks(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)] or [""]
