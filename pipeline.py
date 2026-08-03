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
import re
import time
import unicodedata
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

async def scrape_sources(
    client: httpx.AsyncClient,
    api_key: str,
    citations: list[Citation],
    max_sources: int,
    concurrency: int = 4,
) -> list[SourceCopy]:
    """Fetch local Markdown copies of the cited pages (best-effort)."""
    todo = citations[:max_sources]
    sem = asyncio.Semaphore(concurrency)

    async def one(cit: Citation) -> SourceCopy:
        async with sem:
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
                data = resp.json() if resp.content else {}
                if resp.status_code != 200 or not data.get("success", False):
                    detail = (data.get("error") or f"HTTP {resp.status_code}")
                    return SourceCopy(cit.url, cit.title, "", False, str(detail)[:200])
                page = data.get("data") or {}
                md = page.get("markdown") or ""
                title = (page.get("metadata") or {}).get("title") or cit.title
                if not md.strip():
                    return SourceCopy(cit.url, title, "", False, "empty extraction")
                return SourceCopy(cit.url, title or cit.url, md, True)
            except Exception as exc:                       # noqa: BLE001
                return SourceCopy(cit.url, cit.title, "", False, str(exc)[:200])

    return list(await asyncio.gather(*(one(c) for c in todo)))


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


def _unique_dir(base: Path, name: str) -> Path:
    candidate = base / name
    n = 2
    while candidate.exists():
        candidate = base / f"{name} ({n})"
        n += 1
    return candidate


def write_report(
    output_dir: Path,
    question: str,
    processor: str,
    result: ResearchResult,
    sources: list[SourceCopy],
) -> Path:
    """Write the report folder; returns the path of the report .md file.

    Layout (one folder per research question, friendly to Obsidian/Nextcloud):

        OUTPUT_DIR/2026-08-03 how do solid-state batteries work/
        ├── how do solid-state batteries work.md      ← the report
        └── sources/
            ├── 01 Nature — Solid-state batteries.md  ← local copies
            └── …
    """
    slug = slug_for(question)
    folder = _unique_dir(output_dir, f"{date_str()} {slug}")
    (folder / "sources").mkdir(parents=True) if sources else folder.mkdir(parents=True)

    saved: list[tuple[SourceCopy, str]] = []   # (source, relative link)
    for i, src in enumerate((s for s in sources if s.ok), start=1):
        stitle = slug_for(src.title or src.url, 60)
        fname = f"{i:02d} {stitle}.md"
        body = (
            f"---\nsource: {src.url}\ntitle: \"{_yaml_escape(src.title)}\"\n"
            f"retrieved: {date_str()}\n---\n\n{src.markdown.strip()}\n"
        )
        (folder / "sources" / fname).write_text(body, encoding="utf-8")
        saved.append((src, f"sources/{fname}"))

    lines = [
        "---",
        f'question: "{_yaml_escape(question)}"',
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"processor: {processor}",
    ]
    if result.confidence:
        lines.append(f"confidence: {result.confidence}")
    lines += [f"sources: {len(result.citations)}", "app: Footnote", "---", ""]
    lines += [f"# {question}", "", result.content, ""]

    if result.citations:
        lines += ["## Sources", ""]
        local = {src.url: rel for src, rel in saved}
        for i, cit in enumerate(result.citations, start=1):
            label = cit.title or cit.url
            entry = f"{i}. [{label}]({cit.url})"
            if cit.url in local:
                entry += f" — [local copy](<{local[cit.url]}>)"
            lines.append(entry)
            if cit.excerpts:
                first = " ".join(cit.excerpts[0].split())
                lines.append(f"   > {first[:400]}")
        lines.append("")

    failed = [s for s in sources if not s.ok]
    if failed:
        lines += ["## Sources that could not be archived", ""]
        lines += [f"- {s.url} — {s.error}" for s in failed]
        lines.append("")

    if result.reasoning:
        lines += ["## Method note", "", f"> {result.reasoning}", ""]

    report = folder / f"{slug}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _yaml_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


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
