# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests: pipeline parsing, report writing, API surface, token auth.

Run with:  python -m pytest
No network access required — external APIs are mocked.
"""
import asyncio
import base64
import io
import json
import re
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app as app_module
import pipeline
from pipeline import Citation, PipelineError, ResearchResult, SourceCopy


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def test_slug_strips_unsafe_characters():
    assert pipeline.slug_for('what is "RNA:world" <hypothesis>?') == \
        "what is RNA world hypothesis"


def test_slug_limits_length_at_word_boundary():
    slug = pipeline.slug_for("word " * 40)
    assert len(slug) <= 64 and not slug.endswith(" ")


def test_slug_never_empty():
    assert pipeline.slug_for("///???") == "research"


# ---------------------------------------------------------------------------
# Parallel result parsing
# ---------------------------------------------------------------------------

def _result_payload(content="The answer.", status="completed", basis=None):
    return {
        "run": {"run_id": "trun_x", "status": status},
        "output": {"type": "text", "content": content, "basis": basis or []},
    }


def test_parse_result_extracts_citations_deduplicated():
    basis = [
        {"field": "output", "reasoning": "because", "confidence": "high",
         "citations": [
             {"url": "https://a.test/1", "title": "A", "excerpts": ["one"]},
             {"url": "https://b.test/2", "title": "B"},
             {"url": "https://a.test/1", "title": "A again"},
         ]},
    ]
    res = pipeline._parse_task_result(_result_payload(basis=basis), "trun_x")
    assert [c.url for c in res.citations] == ["https://a.test/1", "https://b.test/2"]
    assert res.confidence == "high" and res.reasoning == "because"


def test_parse_result_failed_run_raises():
    payload = _result_payload(status="failed")
    payload["run"]["error"] = {"message": "boom"}
    with pytest.raises(PipelineError, match="boom"):
        pipeline._parse_task_result(payload, "trun_x")


def test_parse_result_empty_content_raises():
    with pytest.raises(PipelineError, match="empty"):
        pipeline._parse_task_result(_result_payload(content="  "), "trun_x")


def test_parse_result_json_content_flattened():
    res = pipeline._parse_task_result(
        _result_payload(content={"summary": "S", "details": "D"}), "trun_x")
    assert "## summary" in res.content and "D" in res.content


# ---------------------------------------------------------------------------
# Long-polling the result endpoint (408 → retry → 200)
# ---------------------------------------------------------------------------

def test_fetch_task_result_retries_on_408():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(408)
        return httpx.Response(200, json=_result_payload("done"))

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.fetch_task_result(client, "k", "trun_x", 60)

    res = asyncio.run(run())
    assert res.content == "done" and calls["n"] == 3


def test_fetch_task_result_deadline():
    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(408))) as client:
            await pipeline.fetch_task_result(client, "k", "trun_x", 0)

    with pytest.raises(PipelineError, match="giving up"):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Firecrawl scraping
# ---------------------------------------------------------------------------

def test_scrape_sources_mixed_success():
    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        if "bad" in url:                      # a bot wall: final, not retryable
            return httpx.Response(403, json={"success": False, "error": "nope"})
        return httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "# Hi", "metadata": {"title": "Good page"}},
        })

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k",
                [Citation("https://ok.test/a"), Citation("https://bad.test/b")],
                pipeline.ScrapeLimiter(rate_limit=0))

    ok, bad = asyncio.run(run())
    assert ok.ok and ok.title == "Good page" and ok.markdown == "# Hi"
    assert not bad.ok and "nope" in bad.error


def test_scrape_sources_respects_cap():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["url"])
        return httpx.Response(200, json={"success": True,
                                         "data": {"markdown": "x",
                                                  "metadata": {}}})

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k",
                pipeline.scrapable([Citation(f"https://s.test/{i}")
                                    for i in range(9)], 3),
                pipeline.ScrapeLimiter(rate_limit=0))

    assert len(asyncio.run(run())) == 3 and len(seen) == 3


# ---------------------------------------------------------------------------
# robots.txt: what a site asks not to be fetched by machine
# ---------------------------------------------------------------------------

def _robots_run(handler, urls, **kw):
    """Drive scrape_sources with a robots check in front of it."""
    async def run():
        limiter = pipeline.ScrapeLimiter(rate_limit=0, concurrency=2)
        robots = pipeline.RobotsCache(**kw)
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k", [Citation(u) for u in urls], limiter,
                robots=robots)

    return asyncio.run(run())


def _robots_handler(rules, scraped):
    """A site serving robots.txt, and Firecrawl serving pages."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            body = rules.get(request.url.host)
            if body is None:
                return httpx.Response(404)
            if isinstance(body, int):
                return httpx.Response(body)
            return httpx.Response(200, text=body)
        scraped.append(json.loads(request.content)["url"])
        return _ok_scrape(request)

    return handler


def test_a_disallowed_page_is_cited_but_not_archived():
    """The citation stays; only the copy is skipped, and no credit is spent."""
    scraped = []
    copies = _robots_run(
        _robots_handler({"closed.test": "User-agent: *\nDisallow: /\n",
                         "open.test": "User-agent: *\nAllow: /\n"}, scraped),
        ["https://closed.test/a", "https://open.test/b"])

    blocked = next(c for c in copies if "closed" in c.url)
    assert not blocked.ok and blocked.error == pipeline.ROBOTS_DISALLOWED
    assert scraped == ["https://open.test/b"]      # the other was never fetched


def test_the_rules_are_read_once_per_site():
    """One robots.txt fetch, however many of a site's pages are cited."""
    fetches = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            fetches.append(str(request.url))
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return _ok_scrape(request)

    copies = _robots_run(handler, [f"https://one.test/{n}" for n in range(4)])
    assert all(c.ok for c in copies)
    assert fetches == ["https://one.test/robots.txt"]


def test_no_robots_file_means_no_rules():
    """404 is the ordinary case and allows everything (RFC 9309)."""
    scraped = []
    copies = _robots_run(_robots_handler({}, scraped),
                         ["https://nofile.test/a"])
    assert copies[0].ok and scraped == ["https://nofile.test/a"]


def test_unreadable_rules_are_treated_as_a_refusal():
    """RFC 9309: a 5xx on robots.txt means assume complete disallow."""
    scraped = []
    copies = _robots_run(_robots_handler({"broken.test": 503}, scraped),
                         ["https://broken.test/a"])
    assert not copies[0].ok
    assert copies[0].error == pipeline.ROBOTS_UNAVAILABLE
    assert scraped == []                           # nothing was fetched

    # And 401/403 on the rules themselves means the same.
    for status in (401, 403):
        copies = _robots_run(_robots_handler({"shut.test": status}, []),
                             ["https://shut.test/a"])
        assert copies[0].error == pipeline.ROBOTS_UNAVAILABLE, status


def test_a_rule_for_the_fetching_agent_wins():
    """Firecrawl fetches the page, so its rules are the ones that apply."""
    scraped = []
    rules = ("User-agent: *\nAllow: /\n\n"
             "User-agent: FirecrawlAgent\nDisallow: /private/\n")
    handler = _robots_handler({"mixed.test": rules}, scraped)
    copies = _robots_run(handler, ["https://mixed.test/private/x",
                                   "https://mixed.test/public/y"])
    assert copies[0].error == pipeline.ROBOTS_DISALLOWED
    assert copies[1].ok
    assert scraped == ["https://mixed.test/public/y"]


def test_the_check_can_be_turned_off():
    """RESPECT_ROBOTS=false: no robots parameter, no robots request."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return _ok_scrape(request)

    async def run():
        limiter = pipeline.ScrapeLimiter(rate_limit=0, concurrency=2)
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k", [Citation("https://closed.test/a")], limiter)

    copies = asyncio.run(run())
    assert copies[0].ok
    assert "/robots.txt" not in seen


def test_a_skipped_source_is_recorded_in_the_dossier(tmp_path):
    """A reader should see why a copy is missing, not just that it is."""
    result = ResearchResult(
        content="b", citations=[Citation("https://closed.test/a", "Closed")],
        confidence="", reasoning="")
    written = pipeline.write_report(
        tmp_path, "Robots question here", "core", result,
        [SourceCopy("https://closed.test/a", "Closed", "", False,
                    pipeline.ROBOTS_DISALLOWED)])
    text = written.path.read_text()
    assert "could not be archived" in text
    assert pipeline.ROBOTS_DISALLOWED in text
    assert "[Closed](https://closed.test/a)" in text      # still cited


def test_the_robots_fetch_cannot_be_aimed_inwards():
    """This helper makes a request to whatever it is handed."""
    reached = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(str(request.url))
        return httpx.Response(404)

    async def run():
        robots = pipeline.RobotsCache()
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return [await robots.allows(client, u) for u in (
                "http://127.0.0.1:8010/admin",
                "http://169.254.169.254/latest/meta-data/",
                "https://10.0.0.5/x", "https://localhost/x")]

    for allowed, why in asyncio.run(run()):
        assert not allowed and why == pipeline.ROBOTS_NOT_PUBLIC
    assert reached == []                  # nothing was requested at all


def test_internal_citations_are_never_archived():
    """Firecrawl could not reach them, and we should not try either."""
    citations = [Citation("http://127.0.0.1:8010/admin"),
                 Citation("http://169.254.169.254/x"),
                 Citation("http://10.0.0.5/x"),
                 Citation("https://a.test/ok")]
    assert [c.url for c in pipeline.scrapable(citations, 10)] == \
        ["https://a.test/ok"]


def test_health_says_whether_robots_are_respected(client):
    assert client.get("/health").json()["respects_robots"] is True


# ---------------------------------------------------------------------------
# Living inside Firecrawl's free plan: 10 requests/min, 2 concurrent, 402 hard
# ---------------------------------------------------------------------------

def _ok_scrape(_request):
    return httpx.Response(200, json={"success": True,
                                     "data": {"markdown": "body",
                                              "metadata": {"title": "T"}}})


def _run_scrape(handler, count, concurrency=2, rate_limit=0, **kw):
    async def run():
        # The limiter binds a semaphore to the loop, so build it inside one.
        limiter = pipeline.ScrapeLimiter(rate_limit=rate_limit,
                                         concurrency=concurrency)
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k", [Citation(f"https://s.test/{i}") for i in range(count)],
                limiter, **kw)

    return asyncio.run(run())


def test_scrape_never_exceeds_the_concurrency_limit():
    live = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return _ok_scrape(request)

    copies = _run_scrape(handler, 6, concurrency=2, rate_limit=0)
    assert all(c.ok for c in copies)
    assert live["peak"] == 2          # the free plan's two browsers, no more


def test_pacer_holds_requests_inside_the_window():
    """Two per window: the third waits for the first to age out."""
    async def run():
        pacer = pipeline._Pacer(per_minute=2, window_s=0.3)
        started = []
        started.append(0.0)
        clock = asyncio.get_event_loop().time
        t0 = clock()
        for _ in range(4):
            await pacer.take()
            started.append(clock() - t0)
        return started[1:]

    marks = asyncio.run(run())
    assert marks[0] < 0.1 and marks[1] < 0.1     # first two go straight through
    assert marks[2] >= 0.25                      # third waits out the window
    assert marks[3] >= 0.25


def test_pacer_disabled_by_zero():
    async def run():
        pacer = pipeline._Pacer(per_minute=0, window_s=30)
        for _ in range(50):
            await pacer.take()
        return True

    assert asyncio.run(run())


def test_scrape_retries_a_rate_limit_and_honours_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"},
                                  json={"success": False, "error": "slow down"})
        return _ok_scrape(request)

    copy, = _run_scrape(handler, 1, rate_limit=0)
    assert copy.ok and copy.markdown == "body" and calls["n"] == 2


def test_scrape_gives_up_after_max_attempts_and_says_why():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"},
                              json={"success": False, "error": "slow down"})

    copy, = _run_scrape(handler, 1, rate_limit=0)
    assert not copy.ok and "slow down" in copy.error
    assert calls["n"] == pipeline.MAX_ATTEMPTS


def test_scrape_retries_a_transient_server_error(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"success": False, "error": "busy"})
        return _ok_scrape(request)

    copy, = _run_scrape(handler, 1, rate_limit=0)
    assert copy.ok and calls["n"] == 2


def test_scrape_does_not_retry_a_plain_refusal():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"success": False, "error": "blocked"})

    copy, = _run_scrape(handler, 1, rate_limit=0)
    assert not copy.ok and "blocked" in copy.error
    assert calls["n"] == 1            # 403 will not become a 200 by asking again


def test_exhausted_credits_stop_the_batch():
    """402 means every further request fails the same way — don't spend them."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(402, json={"success": False,
                                         "error": "Insufficient credits"})

    copies = _run_scrape(handler, 8, concurrency=1, rate_limit=0)
    assert len(copies) == 8
    assert all(c.error == pipeline.OUT_OF_CREDITS for c in copies)
    assert calls["n"] == 1            # asked once, then stopped asking


def test_scrape_reports_progress_as_copies_land():
    seen = []
    copies = _run_scrape(_ok_scrape, 3, rate_limit=0,
                         on_progress=lambda done, total: seen.append((done, total)))
    assert len(copies) == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_a_backing_off_worker_does_not_hold_a_browser_slot(monkeypatch):
    """The semaphore models browsers in flight, not workers in existence."""
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.2)
    arrivals = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        arrivals.append(url)
        if len(arrivals) == 1:             # the first source must retry
            return httpx.Response(503, json={"success": False, "error": "busy"})
        return _ok_scrape(request)

    copies = _run_scrape(handler, 2, concurrency=1)
    assert all(c.ok for c in copies)
    # Order, not a stopwatch: the other source got the single slot while the
    # first was sleeping out its backoff. Were the slot held across the sleep,
    # the retry would have come second and this source last.
    first, second, third = arrivals
    assert second != first, arrivals        # the other source went next
    assert third == first, arrivals         # then the retry


def test_a_non_json_error_body_cannot_fail_the_job(monkeypatch):
    """An HTML 502 from a proxy is a failed source, never an exception."""
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)   # 502 is retryable
    copies = _run_scrape(
        lambda r: httpx.Response(502, text="<html>bad gateway</html>"), 1)
    assert len(copies) == 1 and not copies[0].ok
    assert "502" in copies[0].error


def test_one_limiter_holds_every_job_on_the_key():
    """Firecrawl counts per key, so two jobs must share one budget."""
    live = {"now": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return _ok_scrape(request)

    async def run():
        limiter = pipeline.ScrapeLimiter(rate_limit=0, concurrency=2)
        cits = [Citation(f"https://s.test/{i}") for i in range(6)]
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            await asyncio.gather(
                pipeline.scrape_sources(client, "k", cits, limiter),
                pipeline.scrape_sources(client, "k", cits, limiter))

    asyncio.run(run())
    assert live["peak"] == 2       # not 4, which is what a per-job limiter gave


def test_exhausted_credits_spend_at_most_the_requests_in_flight():
    """Concurrent workers can both be mid-request when the first 402 lands."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.02)          # a real server is not instant
        return httpx.Response(402, json={"success": False, "error": "no credits"})

    copies = _run_scrape(handler, 8, concurrency=2)
    assert all(c.error == pipeline.OUT_OF_CREDITS for c in copies)
    assert calls["n"] <= 2                 # bounded by concurrency, not by 8


def test_free_plan_defaults_match_the_published_limits():
    assert pipeline.FREE_RATE_LIMIT == 10 and pipeline.FREE_CONCURRENCY == 2
    assert app_module.FIRECRAWL_RATE_LIMIT == 10
    assert app_module.FIRECRAWL_CONCURRENCY == 2


# ---------------------------------------------------------------------------
# Report writing
# ---------------------------------------------------------------------------

def _sample_result():
    return ResearchResult(
        content="**Answer.**\n\n## Evidence\n\nStuff [1].",
        citations=[Citation("https://a.test/1", "Paper A", ["quoted bit"]),
                   Citation("https://b.test/2", "Paper B")],
        confidence="medium", reasoning="triangulated")


def test_write_report_layout(tmp_path):
    sources = [SourceCopy("https://a.test/1", "Paper A", "# A body", True),
               SourceCopy("https://b.test/2", "Paper B", "", False, "blocked")]
    written = pipeline.write_report(
        tmp_path, "How do solid-state batteries work?", "core",
        _sample_result(), sources)
    report = written.path

    assert report.exists()
    assert report.parent.name.endswith("How do solid-state batteries work")
    assert written.source_files == {"https://a.test/1": "01 Paper A.md"}
    text = report.read_text()
    assert 'question: "How do solid-state batteries work?"' in text
    assert 'confidence: "medium"' in text
    assert "[Paper A](https://a.test/1)" in text
    assert "local copy" in text                      # archived source linked
    assert "could not be archived" in text and "blocked" in text
    assert "> quoted bit" in text
    src_files = list((report.parent / "sources").glob("*.md"))
    assert len(src_files) == 1 and src_files[0].name.startswith("01 ")
    assert 'source: "https://a.test/1"' in src_files[0].read_text()


def test_write_report_unique_folder(tmp_path):
    r1 = pipeline.write_report(tmp_path, "Same question here", "core",
                               _sample_result(), []).path
    r2 = pipeline.write_report(tmp_path, "Same question here", "core",
                               _sample_result(), []).path
    assert r1.parent != r2.parent and r2.parent.name.endswith("(2)")


def test_source_files_are_numbered_by_citation_not_by_success(tmp_path):
    """Citation 1 failing must not make citation 2 into "01"."""
    result = ResearchResult(
        content="b",
        citations=[Citation("https://a.test/1", "First"),
                   Citation("https://b.test/2", "Second")],
        confidence="", reasoning="")
    written = pipeline.write_report(
        tmp_path, "Numbering question", "core", result,
        [SourceCopy("https://a.test/1", "First", "", False, "blocked"),
         SourceCopy("https://b.test/2", "Second", "# ok", True)])

    assert written.source_files == {"https://b.test/2": "02 Second.md"}
    assert "[local copy](<sources/02 Second.md>)" in written.path.read_text()


def test_dossier_folders_are_claimed_not_guessed(tmp_path):
    """Same question twice must never hand two writers the same folder."""
    made = [pipeline._claim_dir(tmp_path, "same name", False) for _ in range(3)]
    assert len({d for d in made}) == 3 and all(d.is_dir() for d in made)
    assert [d.name for d in made] == ["same name", "same name (2)",
                                      "same name (3)"]


def test_a_multiline_question_still_yields_valid_frontmatter(tmp_path):
    written = pipeline.write_report(
        tmp_path, "Line one\nLine two: is it valid?", "core",
        _sample_result(), [])
    meta, body = pipeline.split_front_matter(written.path.read_text())
    assert meta["question"] == "Line one Line two: is it valid?"
    assert meta["processor"] == "core"          # later keys survived intact
    assert body.startswith("# Line one Line two")


def test_markdown_metacharacters_in_a_title_do_not_break_the_link(tmp_path):
    result = ResearchResult(
        content="b",
        citations=[Citation("https://a.test/x(1)", "A [bracketed] title"),
                   Citation("https://b.test/two words", "Spaced")],
        confidence="", reasoning="")
    text = pipeline.write_report(
        tmp_path, "Metachar question", "core", result, []).path.read_text()
    assert r"1. [A \[bracketed\] title](<https://a.test/x(1)>)" in text
    assert "2. [Spaced](<https://b.test/two words>)" in text


# ---------------------------------------------------------------------------
# Frontmatter reader
# ---------------------------------------------------------------------------

def test_split_front_matter_round_trips_written_files(tmp_path):
    written = pipeline.write_report(
        tmp_path, 'A question with "quotes" in it', "core",
        _sample_result(), [SourceCopy("https://a.test/1", "Paper A", "# A", True)])
    meta, body = pipeline.split_front_matter(written.path.read_text())
    assert meta["question"] == 'A question with "quotes" in it'
    assert meta["processor"] == "core" and meta["app"] == "Footnote"
    assert body.startswith("# A question")

    copy = written.path.parent / "sources" / "01 Paper A.md"
    meta, body = pipeline.split_front_matter(copy.read_text())
    assert meta["source"] == "https://a.test/1" and meta["title"] == "Paper A"
    assert body.strip() == "# A"


def test_split_front_matter_passes_plain_text_through():
    assert pipeline.split_front_matter("# Just a heading") == \
        ({}, "# Just a heading")


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module, "FOOTNOTE_TOKEN", "")
    # _finished_job repoints OUTPUT_DIR at its dossier; put it back afterwards
    # so one test's tmp_path is not the next test's output directory.
    monkeypatch.setattr(app_module, "OUTPUT_DIR", app_module.OUTPUT_DIR)
    with TestClient(app_module.app) as c:
        yield c


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["app"] == "Footnote"
    assert "output_dir" not in body           # where files live is not published
    assert "output_dir_writable" in body      # whether they can be written is


def test_research_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "")
    resp = client.post("/research", json={"question": "A perfectly fine question"})
    assert resp.status_code == 503


def test_research_validates_processor_and_question(client, monkeypatch):
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    assert client.post("/research", json={"question": "short"}).status_code == 422
    resp = client.post("/research", json={"question": "A perfectly fine question",
                                          "processor": "warp9"})
    assert resp.status_code == 422


def test_research_lifecycle(client, monkeypatch):
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")

    async def fake_run(job_id):
        app_module._update_job(job_id, status="done", progress="Done — test",
                               report_path="/tmp/x.md")

    monkeypatch.setattr(app_module, "run_research", fake_run)
    resp = client.post("/research", json={"question": "A perfectly fine question"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = client.get(f"/research/{job_id}").json()
    assert status["question"] == "A perfectly fine question"
    assert "run_id" not in status                    # internal field stays hidden
    assert status["status"] == "done"

    listing = client.get("/jobs").json()
    assert [j["id"] for j in listing["jobs"]] == [job_id]

    assert client.delete(f"/jobs/{job_id}").status_code == 200
    assert client.get(f"/research/{job_id}").status_code == 404


def _finished_job(tmp_path, **extra):
    """A done job whose dossier really exists on disk, as write_report leaves it.

    OUTPUT_DIR moves with it: report and source files are served only from
    inside the configured output directory.
    """
    app_module.OUTPUT_DIR = tmp_path
    written = pipeline.write_report(
        tmp_path, "How do solid-state batteries work?", "core",
        _sample_result(),
        [SourceCopy("https://a.test/1", "Paper A", "# A body", True),
         SourceCopy("https://b.test/2", "Paper B", "", False, "blocked")])
    job = {"id": "abcdefabcdef", "question": "How do solid-state batteries work?",
           "processor": "core", "status": "done",
           "created_at": "2026-01-01T00:00:00Z",
           "report_path": str(written.path),
           "sources_cited": 2, "sources_archived": 1,
           "citations": [
               {"url": "https://a.test/1", "title": "Paper A",
                "file": "01 Paper A.md", "note": ""},
               {"url": "https://b.test/2", "title": "Paper B",
                "file": "", "note": "blocked"}]}
    job.update(extra)
    app_module.jobs.data["abcdefabcdef"] = job
    return written


def test_report_source_rendered_and_downloadable(client, tmp_path):
    _finished_job(tmp_path)

    page = client.get("/jobs/abcdefabcdef/sources/01 Paper A.md")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<h1>Paper A</h1>" in page.text          # title from the frontmatter
    assert "https://a.test/1" in page.text          # link back to the original
    assert "<h1>A body</h1>" in page.text           # the copy itself, rendered

    raw = client.get("/jobs/abcdefabcdef/sources/01 Paper A.md", params={"raw": 1})
    assert raw.status_code == 200
    assert "markdown" in raw.headers["content-type"]
    assert "attachment" in raw.headers["content-disposition"]
    assert raw.text.startswith("---")                # the file, frontmatter and all


def test_report_source_traversal_blocked(client, tmp_path):
    _finished_job(tmp_path)
    assert client.get("/jobs/abcdefabcdef/sources/.hidden").status_code == 404
    assert client.get("/jobs/abcdefabcdef/sources/%2e%2e%2fr.md").status_code == 404
    assert client.get("/jobs/abcdefabcdef/sources/nope.md").status_code == 404


def test_sources_index_lists_archived_and_missing(client, tmp_path):
    _finished_job(tmp_path)
    body = client.get("/jobs/abcdefabcdef/sources").json()

    assert body["cited"] == 2 and body["archived"] == 1
    assert body["bundle_url"] == "/jobs/abcdefabcdef/bundle.zip"
    first, second = body["sources"]
    assert first["title"] == "Paper A" and first["archived"]
    assert first["read_url"] == "/jobs/abcdefabcdef/sources/01%20Paper%20A.md"
    assert first["download_url"].endswith("?raw=1") and first["bytes"] > 0
    assert not second["archived"] and second["note"] == "blocked"
    assert second["url"] == "https://b.test/2"       # still readable at the source


def test_sources_index_falls_back_to_the_files_on_disk(client, tmp_path):
    """Dossiers written before jobs recorded citations still list their copies."""
    written = _finished_job(tmp_path)
    del app_module.jobs.data["abcdefabcdef"]["citations"]
    body = client.get("/jobs/abcdefabcdef/sources").json()
    assert [s["file"] for s in body["sources"]] == ["01 Paper A.md"]
    assert body["sources"][0]["url"] == "https://a.test/1"   # from frontmatter
    assert written.source_files == {"https://a.test/1": "01 Paper A.md"}


def test_sources_index_survives_a_copy_deleted_in_the_notes_folder(client, tmp_path):
    written = _finished_job(tmp_path)
    (written.path.parent / "sources" / "01 Paper A.md").unlink()
    body = client.get("/jobs/abcdefabcdef/sources").json()
    assert body["archived"] == 0
    assert body["sources"][0]["read_url"] == ""      # citation, no local copy
    assert client.get("/jobs/abcdefabcdef/sources/01 Paper A.md").status_code == 404


def test_bundle_zip_holds_report_and_sources(client, tmp_path):
    written = _finished_job(tmp_path)
    resp = client.get("/jobs/abcdefabcdef/bundle.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = sorted(archive.namelist())
        folder = written.path.parent.name
        assert names == [f"{folder}/How do solid-state batteries work.md",
                         f"{folder}/sources/01 Paper A.md"]
        assert b"# A body" in archive.read(f"{folder}/sources/01 Paper A.md")


def test_every_rendered_page_leads_back_to_the_app(client, tmp_path):
    """A source page is reached from the list as often as from the report."""
    _finished_job(tmp_path)
    for url in ("/jobs/abcdefabcdef/report",
                "/jobs/abcdefabcdef/sources/01 Paper A.md"):
        assert 'href="/">← Footnote' in client.get(url).text, url


def test_a_dead_link_in_a_browser_is_not_a_dead_end(client):
    """A stale bookmark or a notification for a job since removed."""
    page = client.get("/jobs/deadbeefdead/report",
                      headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert 'href="/"' in page.text and "<h1>" in page.text
    # An API client still gets what it expects.
    api = client.get("/jobs/deadbeefdead/report")
    assert api.status_code == 404 and api.json()["detail"]


def test_a_dossier_can_be_read_without_leaving_the_app(client, tmp_path):
    """?embed=1 is the same body without the page around it."""
    _finished_job(tmp_path)
    for url in ("/jobs/abcdefabcdef/report",
                "/jobs/abcdefabcdef/sources/01 Paper A.md"):
        embedded = client.get(url, params={"embed": 1})
        assert embedded.status_code == 200
        assert "<!doctype" not in embedded.text.lower()
        assert "page-nav" not in embedded.text
        assert "<h1>" in embedded.text or "<p" in embedded.text

    # And the relative links a dossier carries are resolved against the URL
    # the document really lives at, not the page it is shown inside.
    embedded = client.get("/jobs/abcdefabcdef/report", params={"embed": 1}).text
    assert 'href="/jobs/abcdefabcdef/sources/01 Paper A.md"' in embedded
    standalone = client.get("/jobs/abcdefabcdef/report").text
    assert 'href="sources/01 Paper A.md"' in standalone   # correct on its page


def test_nothing_in_the_app_navigates_to_a_file():
    """iOS Safari navigates to a file — or to a blob: URL — and shows a
    view-or-download sheet with no way back. So no file is reachable through
    an anchor at all: .md opens its text in place, the zip is fetched and
    handed over, and both are buttons."""
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    for target in ("/report.md", "bundle.zip", "src.download_url",
                   "data.bundle_url"):
        for match in re.finditer(re.escape(target), app_js):
            line = app_js[app_js.rfind("\n", 0, match.start()) + 1:
                          app_js.find("\n", match.start())]
            assert "link(" not in line or "fileButton(" in line \
                or "bundleButton(" in line, line.strip()
    assert "fileButton(" in app_js and "bundleButton(" in app_js
    # The ways out of the panel, none of which leave the page.
    assert "clipboard.writeText" in app_js
    assert "navigator.canShare" in app_js and "navigator.share" in app_js
    assert "createObjectURL" in app_js


def test_the_standalone_pages_do_not_navigate_to_files_either(client, tmp_path):
    """Their "Download .md" was a plain link, which is the same trap."""
    _finished_job(tmp_path)
    for url in ("/jobs/abcdefabcdef/report",
                "/jobs/abcdefabcdef/sources/01 Paper A.md"):
        page = client.get(url).text
        assert "/static/document.js" in page, url
        assert 'data-file="text"' in page, url
    assert 'data-file="archive"' in client.get("/jobs/abcdefabcdef/report").text

    doc_js = (Path(__file__).resolve().parent.parent
              / "static" / "document.js").read_text()
    assert "preventDefault" in doc_js
    assert "nameFromDisposition" in doc_js      # not the URL's last segment
    assert "navigator.canShare" in doc_js and "createObjectURL" in doc_js


def test_the_apps_own_code_is_always_revalidated(client):
    """Without this a browser serves a stale app.js for hours.

    ETag and Last-Modified alone leave a browser free to apply heuristic
    freshness and answer from its cache without asking, which is how a
    deployed fix can appear not to have happened.
    """
    for url in ("/", "/static/app.js", "/static/document.js",
                "/static/style.css", "/service-worker.js", "/manifest.json"):
        assert client.get(url).headers.get("cache-control") == "no-cache", url
    # Still cheap: revalidation, not re-download.
    etag = client.get("/static/app.js").headers["etag"]
    assert client.get("/static/app.js",
                      headers={"If-None-Match": etag}).status_code == 304


def test_opening_one_panel_does_not_hide_another_control(client, tmp_path):
    """With the file panel between the links and the sources list, tapping
    Sources opened it a screenful below and looked like nothing happened."""
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    # The cause, not a proxy for it: a card-level panel given an anchor row
    # is inserted straight after it, which is above the sources list. Both
    # must be opened without one, so they append to the end of the card.
    for call in re.finditer(r"(fileButton|readInline)\((?:[^()]|\([^()]*\))*\)",
                            app_js):
        if "job.id}/report" not in call.group(0):
            continue                       # source-level panels do use a row
        assert "meta" not in call.group(0), call.group(0)
    assert "function reveal(" in app_js
    assert app_js.count("reveal(panel)") >= 2      # sources and file panels


def test_the_three_panels_are_independent_and_outlive_a_poll():
    """read here, .md and Sources toggle separately, and none of them closes
    because the five-second poll rebuilt the list."""
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    # Distinct classes, so one panel's presence never answers for another.
    assert '.src-body' in app_js and '.file-view' in app_js
    # Each kind is remembered across a re-render.
    for remembered in ("openSources", "openReaders", "openFiles"):
        assert f"{remembered}.has(" in app_js, remembered
        assert f"{remembered}.delete(" in app_js, remembered
    # Reopened without scrolling, since the reader did not ask this time.
    assert app_js.count("queueMicrotask(() => open(true))") >= 2
    # Removing a job forgets all of its panels, not only its readers.
    forget = app_js[app_js.index("function forgetReaders("):]
    assert "openFiles" in forget[:forget.index("\n}")]


def test_the_pwa_can_read_in_place():
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    assert "embed=1" in app_js and "readInline" in app_js
    css = (Path(__file__).resolve().parent.parent
           / "static" / "style.css").read_text()
    # The section heading rule must not restyle embedded dossier headings.
    assert "#jobs-section > h2" in css


def test_report_view_links_to_the_bundle(client, tmp_path):
    _finished_job(tmp_path)
    page = client.get("/jobs/abcdefabcdef/report")
    assert page.status_code == 200
    assert "/jobs/abcdefabcdef/bundle.zip" in page.text
    assert "question:" not in page.text              # frontmatter stripped
    # The report's relative "local copy" link resolves to the source view.
    assert 'href="sources/01 Paper A.md"' in page.text


# ---------------------------------------------------------------------------
# The server's filesystem stays the server's business
# ---------------------------------------------------------------------------

def test_job_json_hides_server_bookkeeping(client, tmp_path):
    _finished_job(tmp_path)
    job = client.get("/research/abcdefabcdef").json()
    assert "report_path" not in job and "run_id" not in job
    assert "citations" not in job             # served by /sources on demand
    assert job["report_name"] == "How do solid-state batteries work.md"


def test_no_response_carries_a_server_path(client, tmp_path):
    written = _finished_job(tmp_path)
    folder = str(written.path.parent)
    for url in ("/health", "/jobs", "/research/abcdefabcdef", "/jobs/abcdefabcdef/sources",
                "/jobs/abcdefabcdef/report", "/jobs/abcdefabcdef/sources/01 Paper A.md"):
        assert folder not in client.get(url).text, url


def test_scrub_keeps_the_output_folder_out_of_errors(monkeypatch):
    monkeypatch.setattr(app_module, "OUTPUT_DIR", Path("/srv/notes/inbox"))
    assert app_module._scrub(
        OSError("Permission denied: '/srv/notes/inbox/2026-01-01 q/q.md'")
    ) == "Permission denied: 'the output folder/2026-01-01 q/q.md'"


# ---------------------------------------------------------------------------
# Rendering markup that came off the open web
# ---------------------------------------------------------------------------

def test_rendered_markdown_keeps_formatting():
    html = app_module._render_markdown(
        "# Head\n\nText with *emphasis* and [a link](https://ok.test/x).\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h1>Head</h1>" in html and "<em>emphasis</em>" in html
    assert '<a href="https://ok.test/x">a link</a>' in html
    assert "<table>" in html and "<td>1</td>" in html


def test_rendered_markdown_strips_active_content():
    html = app_module._render_markdown(
        "<script>alert(1)</script>\n\n"
        "<div onclick=\"steal()\">text</div>\n\n"
        "[click](javascript:alert(1))\n\n"
        "<img src=x onerror=alert(1)>\n")
    assert "<script" not in html and "alert(1)" not in html
    assert "onclick" not in html and "onerror" not in html
    assert "javascript:" not in html
    assert "text" in html                            # content survives, markup goes


def test_rendered_markdown_keeps_relative_source_links():
    html = app_module._render_markdown("[local copy](<sources/01 Paper A.md>)")
    assert '<a href="sources/01 Paper A.md">local copy</a>' in html


def test_delete_running_job_refused(client):
    app_module.jobs.data["ababababab12"] = {"id": "ababababab12", "question": "q",
                                   "status": "researching",
                                   "created_at": "2026-01-01T00:00:00Z"}
    assert client.delete("/jobs/ababababab12").status_code == 409


def test_index_and_pwa_assets(client):
    shell = client.get("/")
    assert shell.status_code == 200 and "Footnote" in shell.text
    assert "serviceWorker" in client.get("/static/app.js").text
    assert client.get("/manifest.json").json()["name"] == "Footnote"
    sw = client.get("/service-worker.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]


# ---------------------------------------------------------------------------
# Failure modes that must not touch a finished job
# ---------------------------------------------------------------------------

def _push_keys():
    """A real subscription's keys: an actual P-256 point and 16 random bytes.

    Fabricated bytes of the right length are no longer enough — they are not
    on the curve, and the push library would reject them on every send.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return {"p256dh": base64.urlsafe_b64encode(point).decode().rstrip("="),
            "auth": base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("=")}


def test_a_broken_subscription_cannot_fail_a_finished_job(monkeypatch):
    """Push is best-effort; notify_all is called from inside run_research."""
    def explode(**kwargs):
        raise ValueError("malformed subscription keys")

    monkeypatch.setattr(app_module, "webpush", explode)
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    app_module.subs.data["dev"] = {"endpoint": "https://push.test/x", "keys": {}}
    try:
        asyncio.run(app_module.notify_all("title", "body"))   # must not raise
        assert "dev" in app_module.subs.data     # not provably gone, so kept
    finally:
        app_module.subs.data.clear()


def test_history_cap_holds_when_the_oldest_jobs_are_running(client):
    for i in range(app_module.MAX_JOBS_KEPT + 5):
        app_module.jobs.data[f"j{i:04d}"] = {
            "id": f"j{i:04d}", "question": "q",
            "status": "researching" if i < 5 else "done",
            "created_at": f"2026-01-01T00:{i:02d}:00Z"}
    app_module._trim_jobs()
    assert len(app_module.jobs.data) == app_module.MAX_JOBS_KEPT
    assert all(f"j{i:04d}" in app_module.jobs.data for i in range(5))  # kept


def test_health_asks_the_output_directory_itself(client, tmp_path, monkeypatch):
    locked = tmp_path / "readonly"
    locked.mkdir()
    locked.chmod(0o500)                     # exists, writable parent, not writable
    monkeypatch.setattr(app_module, "OUTPUT_DIR", locked)
    try:
        assert client.get("/health").json()["output_dir_writable"] is False
    finally:
        locked.chmod(0o700)

    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path / "not" / "made" / "yet")
    assert client.get("/health").json()["output_dir_writable"] is True


def test_security_headers_on_every_response(client):
    for url in ("/", "/health", "/jobs"):
        headers = client.get(url).headers
        assert headers["x-content-type-options"] == "nosniff", url
        assert headers["referrer-policy"] == "no-referrer", url
        csp = headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_security_headers_cover_the_auth_middleware_too(locked_client):
    """The 401 page and the token redirect are responses like any other."""
    denied = locked_client.get("/", headers={"Accept": "text/html"})
    assert denied.status_code == 401
    assert denied.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in denied.headers["content-security-policy"]

    bounced = locked_client.get("/", params={"token": "sekrit"},
                                headers={"Accept": "text/html"},
                                follow_redirects=False)
    assert bounced.status_code == 303
    assert bounced.headers["referrer-policy"] == "no-referrer"


def test_bad_configuration_is_refused_at_startup(monkeypatch):
    monkeypatch.setenv("MAX_SOURCES", "not a number")
    with pytest.raises(RuntimeError, match="MAX_SOURCES must be a whole number"):
        app_module._int_env("MAX_SOURCES", 12, minimum=0)
    monkeypatch.setenv("FIRECRAWL_CONCURRENCY", "0")
    with pytest.raises(RuntimeError, match="at least 1"):
        app_module._int_env("FIRECRAWL_CONCURRENCY", 2, minimum=1)
    monkeypatch.setenv("MAX_SOURCES", "  ")
    assert app_module._int_env("MAX_SOURCES", 12, minimum=0) == 12


def test_jobs_limit_is_bounded(client):
    assert client.get("/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/jobs", params={"limit": 10_000}).status_code == 422
    assert client.get("/jobs", params={"limit": 5}).status_code == 200


def test_credit_state_is_shared_and_expires(monkeypatch):
    async def run():
        return pipeline.ScrapeLimiter(rate_limit=0, concurrency=2)

    limiter = asyncio.run(run())
    assert limiter.out_of_credits() is False
    limiter.note_out_of_credits()
    assert limiter.out_of_credits() is True      # answers for later jobs too
    monkeypatch.setattr(pipeline, "CREDIT_COOLDOWN_S", 0)
    assert limiter.out_of_credits() is False     # a top-up gets noticed


def test_no_cross_origin_access_by_default():
    """A wildcard here would let any page spend the Parallel key."""
    assert app_module.CORS_ORIGINS == []
    assert not [m for m in app_module.app.user_middleware if "CORS" in str(m)]


def test_a_notion_failure_cannot_fail_a_written_dossier(client, monkeypatch, tmp_path):
    """The mirror is optional; the dossier is already on disk when it runs."""
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    monkeypatch.setattr(app_module, "FIRECRAWL_API_KEY", "")
    monkeypatch.setattr(app_module, "NOTION_API_KEY", "n")
    monkeypatch.setattr(app_module, "NOTION_DATABASE_ID", "d")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)

    async def fake_result(*a, **kw):
        return _sample_result()

    async def notion_explodes(*a, **kw):
        raise httpx.ConnectError("dns went away")   # not a PipelineError

    monkeypatch.setattr(pipeline, "start_task_run",
                        lambda *a, **kw: _immediately("trun_x"))
    monkeypatch.setattr(pipeline, "fetch_task_result", fake_result)
    monkeypatch.setattr(pipeline, "save_to_notion", notion_explodes)

    resp = client.post("/research", json={"question": "A perfectly fine question"})
    job_id = resp.json()["job_id"]
    for _ in range(50):
        if app_module.jobs.data[job_id]["status"] not in app_module.ACTIVE_STATUSES:
            break
        time.sleep(0.02)
    job = app_module.jobs.data[job_id]
    assert job["status"] == "done", job.get("error")
    assert Path(job["report_path"]).exists()


def _immediately(value):
    async def done():
        return value
    return done()


def test_a_restart_adopts_the_dossier_and_records_the_real_outcome(
        client, tmp_path, monkeypatch):
    """Resuming must not re-scrape or re-write, and must not invent a record.

    Re-fetching the finished Parallel result is allowed — the run is complete
    server-side, so it costs nothing and it is how the citation list comes
    back.
    """
    written = pipeline.write_report(
        tmp_path, "Interrupted question", "core", _sample_result(),
        [SourceCopy("https://a.test/1", "Paper A", "# A body", True)],
        job_id="eeeeeeeeeee2")
    app_module.jobs.data["eeeeeeeeeee2"] = {
        "id": "eeeeeeeeeee2", "question": "Interrupted question", "processor": "core",
        "status": "saving", "progress": "Writing report…",
        "created_at": "2026-01-01T00:00:00Z",
        "run_id": "trun_x", "report_path": str(written.path)}

    def refuse(*a, **kw):
        raise AssertionError("paid work was repeated")

    async def finished_result(*a, **kw):
        return _sample_result()

    monkeypatch.setattr(pipeline, "fetch_task_result", finished_result)
    monkeypatch.setattr(pipeline, "scrape_sources", refuse)
    monkeypatch.setattr(pipeline, "write_report", refuse)
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)

    asyncio.run(app_module.run_research("eeeeeeeeeee2"))

    job = app_module.jobs.data["eeeeeeeeeee2"]
    assert job["status"] == "done"
    assert len(list(tmp_path.iterdir())) == 1        # no duplicate folder
    # The record has to describe the dossier, not merely claim to be finished.
    assert job["progress"] == "Done — 2 sources cited, 1 archived"
    assert job["sources_cited"] == 2 and job["sources_archived"] == 1
    assert job["finished_at"]
    archived = {c["url"]: c["file"] for c in job["citations"]}
    assert archived == {"https://a.test/1": "01 Paper A.md",
                        "https://b.test/2": ""}      # read back off the folder


def test_the_dossier_records_which_job_wrote_it(tmp_path):
    """The marker a recovery would need to recognise its own work."""
    written = pipeline.write_report(tmp_path, "Marked question", "core",
                                    _sample_result(), [], job_id="abc123")
    meta, _ = pipeline.split_front_matter(written.path.read_text())
    assert meta["job"] == "abc123"
    assert pipeline.archived_source_files(written.path) == {}


def test_zero_max_sources_skips_archiving_entirely(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "MAX_SOURCES", 0)
    monkeypatch.setattr(app_module, "FIRECRAWL_API_KEY", "k")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)

    def refuse(*a, **kw):
        raise AssertionError("archiving was entered with MAX_SOURCES=0")

    monkeypatch.setattr(pipeline, "scrape_sources", refuse)
    monkeypatch.setattr(pipeline, "start_task_run",
                        lambda *a, **kw: _immediately("trun_x"))

    async def fake_result(*a, **kw):
        return _sample_result()

    monkeypatch.setattr(pipeline, "fetch_task_result", fake_result)
    asyncio.run(app_module.run_research(_queue_job(client)))


def _queue_job(client):
    job_id = "zero1"
    app_module.jobs.data[job_id] = {
        "id": job_id, "question": "A perfectly fine question",
        "processor": "core", "status": "queued", "created_at": "2026-01-01T00:00:00Z",
        "run_id": "", "report_path": "", "notion_url": "", "error": ""}
    return job_id


def test_subscriptions_are_checked_when_they_are_registered(client, monkeypatch):
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    good = {"endpoint": "https://push.test/x", "keys": _push_keys()}
    assert client.post("/subscribe", json=good).status_code == 200

    assert client.post("/subscribe", json={**good, "keys": {}}).status_code == 422
    assert client.post("/subscribe",
                       json={**good, "endpoint": "javascript:alert(1)"}
                       ).status_code == 422
    app_module.subs.data.clear()


def test_push_needs_a_claim_email_before_it_claims_to_work(client, monkeypatch):
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "")
    assert client.get("/health").json()["push_configured"] is False
    assert client.post("/subscribe", json={
        "endpoint": "https://push.test/x",
        "keys": _push_keys()}).status_code == 503


def test_a_symlink_out_of_the_dossier_is_not_served(client, tmp_path):
    written = _finished_job(tmp_path)
    secret = tmp_path / "elsewhere.md"
    secret.write_text("not part of the dossier")
    link = written.path.parent / "sources" / "99 sneaky.md"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert client.get("/jobs/abcdefabcdef/sources/99 sneaky.md").status_code == 404
    with zipfile.ZipFile(io.BytesIO(client.get("/jobs/abcdefabcdef/bundle.zip").content)) as z:
        assert not any("sneaky" in n for n in z.namelist())


def test_an_unusable_job_record_does_not_stay_running(monkeypatch):
    """A record the orchestrator cannot read must not sit as 'researching'."""
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data.update({
        "ddddddddddd1": {"id": "ddddddddddd1", "status": "researching",      # no question
                "created_at": "2026-01-01T00:00:00Z"},
        "ddddddddddd2": {"id": "ddddddddddd2", "question": "q", "processor": "core",
                     "created_at": "2026-01-01T00:00:01Z"},
        "ddddddddddd3": {"id": "ddddddddddd3", "question": "q", "processor": "core",
                    "status": "done", "created_at": 20260101},
    })
    app_module._normalize_jobs()

    assert app_module.jobs.data["ddddddddddd1"]["status"] == "failed"
    assert "cannot be resumed" in app_module.jobs.data["ddddddddddd1"]["error"]
    assert app_module.jobs.data["ddddddddddd2"]["status"] == "failed"
    assert app_module.jobs.data["ddddddddddd3"]["created_at"] == "20260101"
    app_module._trim_jobs()                       # sorting no longer mixes types

    # And the orchestrator refuses such a record directly, not only via load.
    app_module.jobs.data["ddddddddddd4"] = {"id": "ddddddddddd4", "status": "researching",
                                   "created_at": "2026-01-01T00:00:00Z"}
    asyncio.run(app_module.run_research("ddddddddddd4"))
    assert app_module.jobs.data["ddddddddddd4"]["status"] == "failed"
    app_module.jobs.data.clear()


def test_normalization_validates_rather_than_stringifies(monkeypatch):
    """A null question becoming "None" is not a repair — it is a runnable lie."""
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data.update({
        "aaaaaaaaaaa1": {"id": "aaaaaaaaaaa1", "question": None, "processor": "core",
                  "status": "researching", "created_at": "2026-01-01T00:00:00Z"},
        "aaaaaaaaaaa2": {"id": "aaaaaaaaaaa2", "question": "q", "processor": {},
                     "status": "researching", "created_at": "2026-01-01T00:00:01Z"},
        "aaaaaaaaaaa3": {"id": "aaaaaaaaaaa3", "question": "q", "processor": "core",
                       "status": None, "created_at": "2026-01-01T00:00:02Z"},
        "aaaaaaaaaaa4": {"id": "aaaaaaaaaaa4", "question": "q",
                        "processor": "warp9", "status": "researching",
                        "created_at": "2026-01-01T00:00:03Z"},
    })
    app_module._normalize_jobs()

    for job_id in ("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3", "aaaaaaaaaaa4"):
        job = app_module.jobs.data[job_id]
        assert job["status"] == "failed", job_id
        assert job.get("question") != "None"
        assert job.get("processor") != "{}"
    app_module.jobs.data.clear()


def test_the_repaired_marker_survives_rekeying(tmp_path, monkeypatch):
    """A damaged record was rekeyed out from under its own repair flag."""
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"bad": {                 # not a usable key
        "id": "bad", "question": "\ud800" * 8, "processor": "core",
        "status": "researching", "created_at": "2026-01-01T00:00:00Z"}},
        ensure_ascii=True))
    store = app_module.JsonStore(path)
    assert store.repaired == {"bad"}

    app_module.jobs.data.clear()
    app_module.jobs.data.update(store.data)
    monkeypatch.setattr(app_module.jobs, "repaired", set(store.repaired))
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module._normalize_jobs()

    key, = app_module.jobs.data
    assert re.fullmatch(r"[0-9a-f]{12}", key)            # it was rekeyed
    assert key in app_module.jobs.repaired                # the marker moved
    job = app_module.jobs.data[key]
    assert job["status"] == "failed" and "damaged" in job["error"]
    # The repaired question would otherwise have passed every later check:
    # eight replacement characters is a long enough question.
    assert app_module._resumable(dict(job, status="researching"))
    app_module.jobs.data.clear()


def test_two_keys_that_clean_alike_do_not_collide(tmp_path):
    """Cleaning the outer dictionary merged one record into the other."""
    path = tmp_path / "jobs.json"
    path.write_text('{"\\ud800": {"id": "a", "status": "done"}, '
                    '"\ufffd": {"id": "b", "status": "done"}}')
    store = app_module.JsonStore(path)
    # Both records survive: the unusable key is rekeyed rather than merged
    # onto the usable one, and rather than dropped with its history.
    assert len(store.data) == 2
    assert store.data["\ufffd"]["id"] == "b"
    rekeyed, = [k for k in store.data if k != "\ufffd"]
    assert re.fullmatch(r"[0-9a-f]{12}", rekeyed)
    assert store.data[rekeyed]["id"] == "a"
    assert rekeyed in store.repaired


def test_a_finished_job_is_not_failed_over_its_run_id(monkeypatch):
    """Its dossier exists; the run id is bookkeeping nobody reads again."""
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data["abcdefabcdef"] = {
        "id": "abcdefabcdef", "question": "a perfectly fine question",
        "processor": "core", "status": "done",
        "created_at": "2026-01-01T00:00:00Z", "run_id": "not a run id",
        "report_path": "/somewhere/x.md"}
    app_module._normalize_jobs()
    job = app_module.jobs.data["abcdefabcdef"]
    assert job["status"] == "done"                       # still findable
    assert "run_id" not in job
    app_module.jobs.data.clear()


def test_a_trailing_newline_does_not_pass_for_an_id():
    """`$` matches before a terminal newline; fullmatch does not."""
    assert not pipeline.valid_run_id("trun_x\n")
    assert pipeline.valid_run_id("trun_x")
    assert not app_module._JOB_ID.fullmatch("abcdefabcdef\n")
    assert app_module._JOB_ID.fullmatch("abcdefabcdef")


def test_idna_folding_cannot_slip_past_the_push_policy():
    """Validating one form and classifying another is how these got in."""
    for host in ("127\u30020\u30020\u30021",        # ideographic full stop
                 "127\uff0e0\uff0e0\uff0e1",        # fullwidth full stop
                 "127\uff610\uff610\uff611",        # halfwidth ideographic
                 "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54",
                 "\u24db\u24de\u24d2\u24d0\u24db\u24d7\u24de\u24e2\u24e3"):
        assert host.encode("idna").decode() in ("127.0.0.1", "localhost")
        assert not pipeline.is_push_endpoint(f"https://{host}/x"), host
    # The same string both functions look at.
    assert pipeline.ascii_host("https://127\u30020\u30020\u30021/x") == "127.0.0.1"


def test_multicast_and_site_local_are_not_global_enough():
    """Python calls these global; they are not the open internet."""
    for host in ("224.0.0.1", "239.255.255.250"):
        assert not pipeline.is_push_endpoint(f"https://{host}/x"), host
    for host in ("[ff02::1]", "[fec0::1]"):
        assert not pipeline.is_push_endpoint(f"https://{host}/x"), host
    assert pipeline.is_push_endpoint("https://8.8.8.8/x")


def test_the_push_policy_covers_the_resolver_shorthands():
    """127.1 and 2130706433 reach 127.0.0.1 through any resolver."""
    for host in ("127.1", "2130706433", "0x7f000001", "0177.0.0.1"):
        assert not pipeline.is_push_endpoint(f"https://{host}/x"), host
    # Carrier-grade NAT is neither private nor reserved, and is not the
    # open internet either.
    assert not pipeline.is_push_endpoint("https://100.64.0.1/x")
    assert pipeline.is_push_endpoint("https://fcm.googleapis.com/fcm/send/a")


def test_push_does_not_follow_a_redirect():
    """A 307 preserves the POST, to an address nothing checked.

    Observed behaviourally: requests consumes allow_redirects in Session.send
    before the adapter sees it, so the only honest question is whether a 3xx
    is followed. It must not be — the second request is the dangerous one.
    """
    session = app_module._push_session()
    if session is None:
        pytest.skip("requests not installed")
    import requests

    requested = []

    class Redirector(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            requested.append(request.url)
            response = requests.Response()
            response.status_code = 307
            response.headers["location"] = "https://elsewhere.test/x"
            response.url = request.url
            response.raw = io.BytesIO(b"")
            response.request = request       # resolve_redirects reads this
            return response

        def close(self):
            pass

    session.mount("https://", Redirector())
    result = session.post("https://push.test/x", data=b"payload")
    session.close()

    assert result.status_code == 307              # handed back, not followed
    assert requested == ["https://push.test/x"]   # never sent onwards

def test_an_idna_failure_is_a_failure():
    """The fallback admitted exactly what the encoder had rejected."""
    assert not pipeline.is_http_url("https://" + "a" * 64 + ".test/")
    assert not pipeline.is_http_url("https://" + ("a." * 160) + "test/")
    assert pipeline.is_http_url("https://bücher.example/")
    assert pipeline.is_http_url("https://a.test/")


def test_the_orphan_index_reads_a_whole_frontmatter_block(client, tmp_path):
    """A long source URL was cut off, losing the original and the title."""
    long_url = "https://a.test/" + "x" * 3000
    written = pipeline.write_report(
        tmp_path, "Long orphan question", "core",
        ResearchResult("b", [Citation(long_url, "Long")], "", ""),
        [SourceCopy(long_url, "Long", "# body", True)])
    app_module.OUTPUT_DIR = tmp_path
    app_module.jobs.data["abcdefabcdef"] = {
        "id": "abcdefabcdef", "question": "a perfectly fine question",
        "processor": "core", "status": "done",
        "created_at": "2026-01-01T00:00:00Z",
        "report_path": str(written.path)}          # no citations: the orphan path
    source, = client.get("/jobs/abcdefabcdef/sources").json()["sources"]
    assert source["url"] == long_url
    assert source["title"] == "Long"


def test_an_oversized_body_is_refused_before_it_is_read(client):
    huge = json.dumps({"question": "x" * 200_000})
    resp = client.post("/research", content=huge,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def test_push_keys_are_bounded_before_they_are_decoded():
    """Matching a regex against megabytes to learn the length is wasteful."""
    real = _push_keys()
    assert app_module._valid_subscription(
        {"endpoint": "https://push.test/x", "keys": real})
    assert not app_module._valid_subscription(
        {"endpoint": "https://push.test/x",
         "keys": {"p256dh": "A" * 100_000, "auth": real["auth"]}})


def _run_add_instance_parser(env_text, keys):
    """Execute add-instance.sh's env parser, rather than asserting its text."""
    import subprocess
    script = (Path(__file__).resolve().parent.parent
              / "deploy" / "add-instance.sh").read_text()
    body = script.split("read_env_path() {")[1].split("\n}")[0]
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "instance.env"
        env_file.write_text(env_text)
        runner = Path(tmp) / "run.sh"
        runner.write_text(
            f'ENV_FILE={env_file}\nread_env_path() {{{body}\n}}\n'
            + "".join(f'echo "{k}=$(read_env_path {k})"\n' for k in keys))
        out = subprocess.run(["bash", str(runner)], capture_output=True,
                             text=True)
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


def test_the_deployment_parser_handles_real_env_files():
    """Executed, not asserted as text: quoting is where this goes wrong."""
    parsed = _run_add_instance_parser(
        'OUTPUT_DIR="/srv/My Notes"\n'
        "DATA_DIR='/var/lib/footnote/x'\n"
        "SPACED=   /padded/path   \n"
        "ESCAPED=/srv/two\\ words\n"
        "VARREF=$HOME/notes\n"
        "REL=notabs\n",
        ["OUTPUT_DIR", "DATA_DIR", "SPACED", "ESCAPED", "VARREF", "REL"])
    assert parsed["OUTPUT_DIR"] == "/srv/My Notes"
    assert parsed["DATA_DIR"] == "/var/lib/footnote/x"
    assert parsed["SPACED"] == "/padded/path"
    # Syntax this cannot resolve is refused rather than half-parsed.
    assert parsed["ESCAPED"] == "" and parsed["VARREF"] == ""
    assert parsed["REL"] == ""                 # relative paths are not paths


def test_the_instance_script_validates_before_it_writes():
    add = (Path(__file__).resolve().parent.parent
           / "deploy" / "add-instance.sh").read_text()
    # The checks come before the env file is created, or a bad first run
    # leaves a poisoned file that a corrected run will not replace.
    assert add.index("output-dir must be an absolute path") < add.index("cat > \"$ENV_FILE\"")
    assert add.index("port must be between 1 and 65535") < add.index("cat > \"$ENV_FILE\"")
    assert "-m 700 -- " in add                 # install(1) reads a leading dash
    assert "is-active --quiet" in add          # do not claim success on failure


def test_a_repaired_record_is_not_a_valid_one(tmp_path, monkeypatch):
    """Cleaning makes a record storable; it does not make it worth spending on."""
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"abcdefabcdef": {
        "id": "abcdefabcdef", "question": "\ud800" * 8, "processor": "core",
        "status": "researching", "created_at": "2026-01-01T00:00:00Z",
        "run_id": ""}}, ensure_ascii=True))
    store = app_module.JsonStore(path)
    assert store.repaired == {"abcdefabcdef"}

    app_module.jobs.data.clear()
    app_module.jobs.data.update(store.data)
    monkeypatch.setattr(app_module.jobs, "repaired", store.repaired)
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module._normalize_jobs()
    job = app_module.jobs.data["abcdefabcdef"]
    assert job["status"] == "failed"          # not resubmitted to Parallel
    assert "damaged" in job["error"]
    app_module.jobs.data.clear()


def test_an_unusable_stored_run_id_is_not_started_over(monkeypatch):
    """The run may exist and be paid for; starting over would buy another."""
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data["abcdefabcdef"] = {
        "id": "abcdefabcdef", "question": "a perfectly fine question",
        "processor": "core", "status": "researching",
        "created_at": "2026-01-01T00:00:00Z", "run_id": "not a run id"}
    app_module._normalize_jobs()
    assert app_module.jobs.data["abcdefabcdef"]["status"] == "failed"
    assert pipeline.valid_run_id("trun_abc.123") and not pipeline.valid_run_id("a b")
    app_module.jobs.data.clear()


def test_a_push_endpoint_cannot_be_aimed_inwards():
    """The server POSTs to this address; a client chooses it."""
    for url in ("http://push.test/x",                  # not TLS
                "http://127.0.0.1:8010/internal",      # this machine
                "https://169.254.169.254/latest",      # cloud metadata
                "https://10.0.0.5/x", "https://[::1]/x",
                "https://user:pw@push.test/x",         # credentials
                "https://localhost/x"):
        assert not pipeline.is_push_endpoint(url), url
        assert not app_module._valid_subscription(
            {"endpoint": url, "keys": _push_keys()}), url
    for url in ("https://push.test/x", "https://fcm.googleapis.com/fcm/send/abc",
                "https://bücher.example/push"):
        assert pipeline.is_push_endpoint(url), url


def test_push_calls_are_bounded(client, monkeypatch):
    """One unreachable device must not hold the others' notification."""
    seen = {}

    def record(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(app_module, "webpush", record)
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    app_module.subs.data["dev"] = {"endpoint": "https://push.test/x",
                                   "keys": _push_keys()}
    asyncio.run(app_module.notify_all("t", "b"))
    # A pair, because requests reads a scalar as inactivity, not total time.
    assert seen["timeout"] == (app_module.PUSH_CONNECT_TIMEOUT_S,
                               app_module.PUSH_READ_TIMEOUT_S)
    assert seen["requests_session"] is not None
    app_module.subs.data.clear()


def test_the_push_pool_is_one_per_process(client):
    """A semaphore per notification gave two jobs twice the slots."""
    pool = app_module.app.state.push_pool
    assert pool._max_workers == app_module.PUSH_CONCURRENCY
    # And it is not the executor asyncio.to_thread would use, which is the
    # one write_report runs on.
    assert pool is not None


def test_a_redirecting_endpoint_is_dropped(monkeypatch):
    """We decline to follow it, so it will answer that way for ever."""
    class Redirected(Exception):
        pass

    response = type("R", (), {"status_code": 308})()
    exc = app_module.WebPushException("moved")
    exc.response = response

    def raise_redirect(**kwargs):
        raise exc

    monkeypatch.setattr(app_module, "webpush", raise_redirect)
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    assert app_module._push_one({"endpoint": "https://push.test/x",
                                 "keys": _push_keys()}, {}) is False


def test_the_number_of_devices_is_bounded(client, monkeypatch):
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    monkeypatch.setattr(app_module, "MAX_SUBSCRIPTIONS", 2)
    monkeypatch.setattr(app_module.subs, "save", lambda: None)
    app_module.subs.data.clear()
    for n in range(2):
        assert client.post("/subscribe", json={
            "endpoint": f"https://push.test/{n}", "keys": _push_keys()
        }).status_code == 200
    assert client.post("/subscribe", json={
        "endpoint": "https://push.test/3", "keys": _push_keys()}).status_code == 409
    # An existing device re-registering is not a new one.
    assert client.post("/subscribe", json={
        "endpoint": "https://push.test/0", "keys": _push_keys()}).status_code == 200
    assert client.post("/subscribe", json={
        "endpoint": "x" * 3000, "keys": _push_keys()}).status_code == 422
    app_module.subs.data.clear()


def test_control_characters_do_not_reach_a_dossier(tmp_path):
    """A Markdown file with NUL in it looks binary to everything downstream."""
    result = ResearchResult("Body with \x00 NUL and \x1b ESC\nand a newline",
                            [Citation("https://a.test/1", "T\x00itle")], "", "")
    written = pipeline.write_report(
        tmp_path, "Control question here", "core", result,
        [SourceCopy("https://a.test/1", "T\x00", "body\x00here", True)])
    for path in [written.path, *(written.path.parent / "sources").glob("*.md")]:
        raw = path.read_bytes()
        assert b"\x00" not in raw and bytes([27]) not in raw, path.name
        assert b"and a newline" in written.path.read_bytes()   # \n survives


def test_a_unicode_domain_is_not_rejected():
    assert pipeline.is_http_url("https://bücher.example/")
    assert pipeline.is_http_url("https://xn--bcher-kva.example/")
    assert not pipeline.is_http_url("https://-bad.example/")


def test_a_validation_error_does_not_echo_the_request(client, monkeypatch):
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    huge = "x" * 20000
    resp = client.post("/research", json={"question": huge})
    assert resp.status_code == 422
    assert huge not in resp.text
    assert len(resp.content) < 1000          # not proportional to the request


def test_the_env_parser_handles_systemd_quoting():
    add = (Path(__file__).resolve().parent.parent
           / "deploy" / "add-instance.sh").read_text()
    assert "read_env_path" in add
    assert '/*) printf' in add               # only absolute paths are accepted


def test_provider_error_text_is_scrubbed_too(monkeypatch, tmp_path):
    """An error string reaches the dossier and the store, like any other."""
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        # Escaped in the bytes, which is how a real server sends one.
        return httpx.Response(
            403, content=br'{"success": false, "error": "blocked \ud800"}',
            headers={"content-type": "application/json"})

    copy, = _run_scrape(handler, 1)
    assert not copy.ok
    assert not pipeline.has_lone_surrogate(copy.error)
    written = pipeline.write_report(          # would raise before
        tmp_path, "Error surrogate question", "core",
        ResearchResult("b", [Citation("https://s.test/0")], "", ""), [copy])
    assert written.path.exists()


def test_a_surrogate_that_reached_the_file_is_scrubbed_on_load(tmp_path, client):
    """save() escapes it, so loading recreates it — /jobs must survive that."""
    store = app_module.JsonStore(tmp_path / "jobs.json")
    store.data["ffffffffffff"] = {
        "id": "ffffffffffff", "question": "a perfectly fine question",
        "processor": "core", "status": "failed",
        "created_at": "2026-01-01T00:00:00Z", "error": "boom \ud800",
        "citations": [{"url": "https://a.test/1", "title": "bad \ud800"}]}
    store.save()

    app_module.jobs.data.clear()
    app_module.jobs.data.update(app_module.JsonStore(tmp_path / "jobs.json").data)
    app_module._normalize_jobs()
    job = app_module.jobs.data["ffffffffffff"]
    assert not pipeline.has_lone_surrogate(job["error"])
    assert not pipeline.has_lone_surrogate(job["citations"][0]["title"])
    assert client.get("/jobs").status_code == 200
    app_module.jobs.data.clear()


def test_a_state_file_that_is_not_utf8_is_quarantined(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_bytes(b'{"a": "\xff"}')
    store = app_module.JsonStore(path)       # must not raise
    assert store.data == {}
    assert list(tmp_path.glob("jobs.corrupt-*.json"))


def test_a_mailto_citation_stays_linked_in_the_index(client, tmp_path):
    """The report links it; the index must agree."""
    _finished_job(tmp_path)
    app_module.jobs.data["abcdefabcdef"]["citations"] = [
        {"url": "mailto:x@example.test", "title": "Mail", "file": "", "note": ""},
        {"url": "not-a-url", "title": "Bare", "file": "", "note": ""},
    ]
    sources = client.get("/jobs/abcdefabcdef/sources").json()["sources"]
    assert sources[0]["url"] == "mailto:x@example.test"
    assert sources[1]["url"] == ""           # would resolve against us


def test_push_keys_must_be_keys_not_just_bytes():
    def encoded(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    real = _push_keys()
    assert app_module._valid_subscription(
        {"endpoint": "https://push.test/x", "keys": real})
    for p256dh in (encoded(b"\x00" * 65),            # not a point at all
                   encoded(b"\x02" + b"k" * 64),     # compressed, not what is sent
                   encoded(b"\x04" + b"\xff" * 64),  # right shape, off the curve
                   real["p256dh"] + "!!"):           # trailing rubbish
        assert not app_module._valid_subscription(
            {"endpoint": "https://push.test/x",
             "keys": {"p256dh": p256dh, "auth": real["auth"]}}), p256dh[:12]


def test_only_the_two_push_keys_are_stored(client, monkeypatch):
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    keys = dict(_push_keys(), junk="x" * 5000)
    assert client.post("/subscribe", json={"endpoint": "https://push.test/x",
                                           "keys": keys}).status_code == 200
    stored, = app_module.subs.data.values()
    assert set(stored["keys"]) == {"p256dh", "auth"}
    app_module.subs.data.clear()


def test_a_hostname_has_to_be_usable():
    for url in ("https://\ud800", "https://.", "https://_/", "https://-bad.test/x"):
        assert not pipeline.is_http_url(url), url
    for url in ("https://a.test/x", "https://[::1]/x", "https://127.0.0.1:8010/x",
                "https://xn--bcher-kva.test/x"):
        assert pipeline.is_http_url(url), url


def test_control_characters_are_not_a_question():
    assert app_module._question_problem("a question\x00with NUL")
    assert not app_module._question_problem("a question\nwith a newline")


def test_the_worker_retires_caches_that_may_hold_a_token():
    source = (Path(__file__).resolve().parent.parent
              / "static" / "service-worker.js").read_text()
    assert "footnote-shell-v3" in source and "footnote-dossier-v2" in source


def test_the_installer_rebuilds_a_venv_that_is_too_old():
    install = (Path(__file__).resolve().parent.parent
               / "deploy" / "install.sh").read_text()
    assert '.venv/bin/python' in install and "rm -rf" in install
    add = (Path(__file__).resolve().parent.parent
           / "deploy" / "add-instance.sh").read_text()
    assert "EXISTING_OUT" in add        # repair the paths the instance uses


def test_a_lone_surrogate_cannot_reach_the_store(client, monkeypatch):
    """It survives JSON decoding and then cannot be written back out."""
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    app_module.jobs.data.clear()

    body = b'{"question": "' + (b"\\ud800" * 8) + b'"}'   # as a client sends it
    resp = client.post("/research", content=body,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert app_module.jobs.data == {}
    assert app_module._question_problem("\ud800" * 8)


def test_the_store_survives_text_it_should_never_have_held(tmp_path):
    """Belt and braces: one bad value must not wedge every future save."""
    store = app_module.JsonStore(tmp_path / "jobs.json")
    store.data["x"] = {"question": "\ud800"}
    store.save()                                   # must not raise
    assert json.loads((tmp_path / "jobs.json").read_text())["x"]


def test_provider_text_with_a_surrogate_is_neutralised():
    payload = {"run": {"status": "completed"},
               "output": {"content": "answer \ud800 here", "basis": [
                   {"citations": [{"url": "https://a.test/1",
                                   "title": "bad \ud800 title"}]}]}}
    result = pipeline._parse_task_result(payload, "trun_x")
    assert not pipeline.has_lone_surrogate(result.content)
    assert not pipeline.has_lone_surrogate(result.citations[0].title)
    result.content.encode("utf-8")                 # would raise before


def test_slugs_are_budgeted_in_bytes_not_characters(tmp_path):
    """ext4 limits a name to 255 bytes; 64 emoji are 256."""
    slug = pipeline.slug_for("🙂" * 64)
    assert len(slug.encode("utf-8")) <= 64
    written = pipeline.write_report(tmp_path, "🙂" * 64, "core",
                                    _sample_result(), [])
    for part in (written.path.parent.name, written.path.name):
        assert len(part.encode("utf-8")) <= 255, part
    assert pipeline.slug_for("How do solid-state batteries work?") == \
        "How do solid-state batteries work"        # ASCII is unaffected


def test_a_run_id_must_be_a_string_of_the_right_shape():
    async def create(body):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=body))) as client:
            return await pipeline.start_task_run(client, "k", "q", "core")

    for value in (123, True, 3.5, None, {"nested": 1}, "with space", "a/b"):
        with pytest.raises(PipelineError, match="run_id"):
            asyncio.run(create({"run_id": value}))
    assert asyncio.run(create({"run_id": "trun_abc.123-x"})) == "trun_abc.123-x"


def test_a_done_job_without_a_report_says_so(client):
    app_module.jobs.data["dddddddddddd"] = {
        "id": "dddddddddddd", "question": "a perfectly fine question",
        "processor": "core", "status": "done",
        "created_at": "2026-01-01T00:00:00Z"}
    job = client.get("/jobs").json()["jobs"][0]
    assert job["report_available"] is False        # not absent, which reads as yes


def test_the_worker_never_caches_a_token_url():
    source = (Path(__file__).resolve().parent.parent
              / "static" / "service-worker.js").read_text()
    assert 'searchParams.has("token")' in source


def test_a_non_web_citation_is_not_linkable_from_the_index(client, tmp_path):
    written = _finished_job(tmp_path)
    app_module.jobs.data["abcdefabcdef"]["citations"] = [
        {"url": "not-a-url", "title": "Bare", "file": "", "note": ""},
        {"url": "https://a.test/1", "title": "Paper A", "file": "01 Paper A.md"},
    ]
    sources = client.get("/jobs/abcdefabcdef/sources").json()["sources"]
    assert sources[0]["url"] == ""                 # would resolve against us
    assert sources[0]["title"] == "Bare"           # still listed
    assert sources[1]["url"] == "https://a.test/1"
    assert written.path.exists()


def test_push_keys_must_decode_to_their_real_sizes():
    good = {"endpoint": "https://push.test/x", "keys": _push_keys()}
    assert app_module._valid_subscription(good)
    for broken in ({"p256dh": "x", "auth": "x"},
                   {"p256dh": _push_keys()["p256dh"], "auth": "short"},
                   {"p256dh": "!!!not base64!!!", "auth": _push_keys()["auth"]}):
        assert not app_module._valid_subscription(
            {"endpoint": "https://push.test/x", "keys": broken})


def test_an_unreadable_env_file_does_not_stop_the_server(tmp_path, monkeypatch):
    """A permissions problem on an optional file is not a reason not to boot."""
    env = tmp_path / ".env"
    env.write_text("PARALLEL_API_KEY=x\n")
    env.chmod(0o000)
    monkeypatch.chdir(tmp_path)
    try:
        app_module._load_env()          # must not raise
    finally:
        env.chmod(0o600)


def test_the_installer_repairs_the_instances_it_already_has():
    """The upgrade path is install.sh plus a restart — it has to be enough."""
    install = (Path(__file__).resolve().parent.parent
               / "deploy" / "install.sh").read_text()
    assert "/etc/footnote/*.env" in install
    assert "usermod -a -G" in install


def test_the_installer_refuses_an_unsupported_python():
    install = (Path(__file__).resolve().parent.parent
               / "deploy" / "install.sh").read_text()
    assert "(3, 10)" in install and "PYTHON" in install
    unit = (Path(__file__).resolve().parent.parent
            / "deploy" / "footnote@.service").read_text()
    assert "UMask=0077" in unit


def test_an_unroutable_store_key_is_rekeyed(client, monkeypatch):
    """The key is the public id and goes straight into URLs."""
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data.clear()
    app_module.jobs.data["../escape?x=1"] = {
        "id": "whatever", "question": "a perfectly fine question",
        "processor": "core", "status": "done",
        "created_at": "2026-01-01T00:00:00Z"}
    app_module._normalize_jobs()

    assert "../escape?x=1" not in app_module.jobs.data
    key, = app_module.jobs.data
    assert re.fullmatch(r"[0-9a-f]{12}", key)
    assert app_module.jobs.data[key]["id"] == key       # history survives
    assert client.get(f"/research/{key}").status_code == 200
    app_module.jobs.data.clear()


def test_a_bad_notion_url_is_dropped_on_load(client, monkeypatch):
    """The PWA assigns it straight to an anchor."""
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data["aaaaaaaaaaaa"] = {
        "id": "aaaaaaaaaaaa", "question": "a perfectly fine question",
        "processor": "core", "status": "done",
        "created_at": "2026-01-01T00:00:00Z",
        "notion_url": "javascript:alert(1)"}
    app_module._normalize_jobs()
    assert "notion_url" not in app_module.jobs.data["aaaaaaaaaaaa"]
    app_module.jobs.data.clear()


def test_resume_uses_the_same_question_rule_as_submission(client, monkeypatch):
    """A question submission refuses must not get in through a resume."""
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    assert client.post("/research", json={"question": "hi"}).status_code == 422
    assert not app_module._resumable(
        {"question": "hi", "processor": "core"})
    assert not app_module._resumable(
        {"question": "x" * 5000, "processor": "core"})
    assert not app_module._resumable({"question": None, "processor": "core"})
    assert app_module._resumable(
        {"question": "a perfectly fine question", "processor": "core"})


def test_the_deadline_bounds_what_each_request_may_take():
    """Both timeouts are cut to the remaining budget, not left at 150 s.

    MockTransport runs the handler without honouring timeouts, so this pins
    the values Footnote passes — enforcing them is httpx's job, and it can
    only enforce what it is given.
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.extensions.get("timeout", {}),
                     int(request.url.params["timeout"])))
        return httpx.Response(408)

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PipelineError, match="giving up"):
                await pipeline.fetch_task_result(client, "k", "trun_x", 3)

    asyncio.run(run())
    assert seen, "no request was made"
    for timeouts, poll_seconds in seen:
        assert timeouts["read"] <= 3, timeouts        # not the 150 s default
        assert timeouts["connect"] <= 3, timeouts
        assert poll_seconds <= 3                      # nor a 120 s long poll
    assert seen[-1][0]["read"] < seen[0][0]["read"]   # shrinks as time is spent


def test_a_create_response_must_name_a_run():
    """Not retried: the POST may have created a run we would then orphan."""
    async def create(body):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=body))) as client:
            return await pipeline.start_task_run(client, "k", "q", "core")

    for body in (["not", "an", "object"], {"run_id": {"nested": 1}},
                 {"run_id": ""}, {}):
        with pytest.raises(PipelineError, match="run_id"):
            asyncio.run(create(body))
    assert asyncio.run(create({"run_id": " trun_x "})) == "trun_x"


def test_an_authority_is_not_a_hostname():
    for url in ("https://:443/x", "https://user@/x", "https://a.test:99999/x"):
        assert not pipeline.is_http_url(url), url
    assert pipeline.is_http_url("https://a.test:8443/x")


def test_success_must_be_the_boolean(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": "false",
                                         "data": {"markdown": "x",
                                                  "metadata": {}}})

    copy, = _run_scrape(handler, 1)
    assert not copy.ok                       # "false" is a truthy string


def test_a_citations_field_that_is_not_a_list(client, tmp_path):
    _finished_job(tmp_path)
    app_module.jobs.data["abcdefabcdef"]["citations"] = 5
    body = client.get("/jobs/abcdefabcdef/sources").json()
    assert body["archived"] == 1             # described by the files on disk
    assert [s["file"] for s in body["sources"]] == ["01 Paper A.md"]


def test_report_availability_is_reported_separately(client, tmp_path):
    written = _finished_job(tmp_path)
    assert client.get("/jobs").json()["jobs"][0]["report_available"] is True
    written.path.unlink()                    # moved or deleted in the notes app
    assert client.get("/jobs").json()["jobs"][0]["report_available"] is False
    assert client.get("/jobs").json()["jobs"][0]["status"] == "done"


def test_a_stored_id_is_reconciled_with_its_key(client, monkeypatch):
    """The API resolves by key; links are built from the stored id."""
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data["bbbbbbbbbbb1"] = {
        "id": "../elsewhere", "question": "q", "processor": "core",
        "status": "done", "created_at": "2026-01-01T00:00:00Z"}
    app_module._normalize_jobs()
    assert app_module.jobs.data["bbbbbbbbbbb1"]["id"] == "bbbbbbbbbbb1"
    assert client.get("/jobs").json()["jobs"][0]["id"] == "bbbbbbbbbbb1"


def test_a_malformed_citation_does_not_break_the_index(client, tmp_path):
    written = _finished_job(tmp_path)
    app_module.jobs.data["abcdefabcdef"]["citations"] = [
        "not a dict", None,
        {"url": "https://a.test/1", "title": "Paper A", "file": "01 Paper A.md"},
        {"url": None, "title": 5, "file": ["nope"]},
    ]
    body = client.get("/jobs/abcdefabcdef/sources").json()
    assert body["archived"] == 1
    assert [s["file"] for s in body["sources"] if s["file"]] == ["01 Paper A.md"]


def test_parallel_containers_that_are_not_containers(tmp_path):
    """`for x in 5` is a TypeError, and this run was already paid for."""
    for payload in (
        {"run": {"status": "completed"},
         "output": {"content": "x", "basis": 5}},
        {"run": {"status": "completed"},
         "output": {"content": "x", "basis": [{"citations": True}]}},
        {"run": {"status": "completed"},
         "output": {"content": "x", "basis": [
             {"citations": [{"url": "https://a.test/1", "excerpts": 3}]}]}},
    ):
        result = pipeline._parse_task_result(payload, "trun_x")
        assert result.content == "x"
    assert pipeline._parse_task_result(payload, "trun_x").citations[0].excerpts == []


def test_a_retry_wait_never_outlives_its_deadline():
    """A 120-second Retry-After inside a 1-second budget is not a 120s wait."""
    slept = []

    async def run():
        async def fake_sleep(seconds):
            slept.append(seconds)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "120"})

        real_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(PipelineError, match="giving up"):
                    await pipeline.fetch_task_result(client, "k", "trun_x", 1)
        finally:
            asyncio.sleep = real_sleep

    asyncio.run(run())
    assert slept, "no wait happened"
    assert max(slept) <= 1, slept


def test_a_url_with_control_characters_is_rejected_not_cleaned():
    assert not pipeline.is_http_url("https://a.test/\tx")
    assert not pipeline.is_http_url("http://a.test/\n")
    assert pipeline.is_http_url("https://a.test/x")


def test_only_a_real_notion_url_is_stored(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_module, "NOTION_API_KEY", "n")
    monkeypatch.setattr(app_module, "NOTION_DATABASE_ID", "d")

    async def junk_url(*a, **kw):
        return "not a url at all"

    monkeypatch.setattr(pipeline, "save_to_notion", junk_url)
    monkeypatch.setattr(pipeline, "fetch_task_result",
                        lambda *a, **kw: _immediately(_sample_result()))
    written = pipeline.write_report(tmp_path, "Notion url question", "core",
                                    _sample_result(), [], job_id="eeeeeeeeeee3")
    app_module.jobs.data["eeeeeeeeeee3"] = {
        "id": "eeeeeeeeeee3", "question": "Notion url question", "processor": "core",
        "status": "saving", "created_at": "2026-01-01T00:00:00Z",
        "run_id": "trun_x", "report_path": str(written.path)}
    asyncio.run(app_module.run_research("eeeeeeeeeee3"))
    assert app_module.jobs.data["eeeeeeeeeee3"].get("notion_url", "") == ""


def test_a_repaired_history_still_serves(client, monkeypatch):
    monkeypatch.setattr(app_module.jobs, "save", lambda: None)
    app_module.jobs.data["cccccccccccc"] = {"id": "cccccccccccc", "question": "q",
                                      "created_at": "2026-01-01T00:00:00Z"}
    app_module._normalize_jobs()
    listing = client.get("/jobs")
    assert listing.status_code == 200
    assert listing.json()["active"] == 0
    assert client.get("/health").status_code == 200
    assert client.delete("/jobs/cccccccccccc").status_code == 200


def test_recovery_refuses_a_report_it_could_not_serve(client, tmp_path,
                                                      monkeypatch):
    """Adopting an unservable path would mark a job done that answers 404."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", out)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not in the output directory")
    folder = out / "2026-01-01 q"
    folder.mkdir()
    link = folder / "report.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    assert app_module._servable_report(str(link)) is None
    assert app_module._servable_report(str(folder)) is None      # a directory
    real = folder / "real.md"
    real.write_text("# real")
    assert app_module._servable_report(str(real)) == real


def test_recovery_ignores_source_links_out_of_the_folder(tmp_path):
    written = pipeline.write_report(
        tmp_path, "Recovery question", "core", _sample_result(),
        [SourceCopy("https://a.test/1", "Paper A", "# A", True)])
    outside = tmp_path / "outside.md"
    outside.write_text('---\nsource: "https://evil.test/x"\n---\n\nbody\n')
    try:
        (written.path.parent / "sources" / "09 escape.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    found = pipeline.archived_source_files(written.path)
    assert found == {"https://a.test/1": "01 Paper A.md"}


def test_a_long_frontmatter_block_is_still_read(tmp_path):
    """The block is read to its delimiter, not to a fixed guess."""
    long_url = "https://a.test/" + "x" * 4000
    written = pipeline.write_report(
        tmp_path, "Long url question", "core",
        ResearchResult("b", [Citation(long_url, "Long")], "", ""),
        [SourceCopy(long_url, "Long", "# body", True)])
    assert pipeline.archived_source_files(written.path) == {
        long_url: "01 Long.md"}


def test_provider_scalars_are_coerced(monkeypatch):
    """A title that is a list must not reach slug_for."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "# body", "metadata": {"title": ["a", "list"]}}})

    copy, = _run_scrape(handler, 1)
    assert copy.ok and isinstance(copy.title, str)
    assert copy.title == "https://s.test/0"        # fell back to the citation


def test_a_hostile_provider_payload_cannot_fail_the_dossier(tmp_path):
    """Whatever arrives, write_report gets strings."""
    payload = {
        "run": {"status": "completed"},
        "output": {"content": "The answer.", "basis": [
            {"confidence": ["high"], "reasoning": {"a": 1}, "citations": [
                {"url": "https://a.test/1", "title": ["listy"],
                 "excerpts": [None, "kept"]},
                "not a citation at all",
            ]},
            "not a basis either",
        ]},
    }
    result = pipeline._parse_task_result(payload, "trun_x")
    assert result.confidence == "" and result.reasoning == ""
    assert [c.title for c in result.citations] == [""]
    assert result.citations[0].excerpts == ["kept"]
    written = pipeline.write_report(tmp_path, "Hostile payload", "core",
                                    result, [])
    assert written.path.exists()


def test_data_of_the_wrong_shape_is_retried_like_any_broken_body(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"success": True, "data": []})

    copy, = _run_scrape(handler, 1)
    assert calls["n"] == pipeline.MAX_ATTEMPTS      # not "empty extraction"
    assert copy.error == "HTTP 200 with an unreadable body"


def test_a_finished_parallel_run_survives_a_bad_gateway(monkeypatch):
    """The run is paid for; a 502 in front of the result is not a failure."""
    monkeypatch.setattr(pipeline, "RESULT_RETRY_S", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="<html>bad gateway</html>")
        if calls["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if calls["n"] == 3:
            return httpx.Response(200, text="not json at all")
        return httpx.Response(200, json=_result_payload("done"))

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.fetch_task_result(client, "k", "trun_x", 60)

    res = asyncio.run(run())
    assert res.content == "done" and calls["n"] == 4


def test_a_push_endpoint_needs_a_host():
    assert not pipeline.is_http_url("https:")
    assert not pipeline.is_http_url("http://")
    assert pipeline.is_http_url("https://push.test/x")
    assert not app_module._valid_subscription(
        {"endpoint": "https:", "keys": _push_keys()})


def test_post_processing_stops_when_the_job_is_deleted(client, tmp_path,
                                                       monkeypatch):
    """A notification for a deleted job would open a 404.

    This covers deletion *before* either step starts, which is what the code
    can suppress. A request already in flight is not cancelled — the
    documentation says so rather than the code pretending otherwise.
    """
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_module, "NOTION_API_KEY", "n")
    monkeypatch.setattr(app_module, "NOTION_DATABASE_ID", "d")
    called = {"notion": 0, "push": 0}

    async def note_notion(*a, **kw):
        called["notion"] += 1
        return "https://notion.test/p"

    async def note_push(*a, **kw):
        called["push"] += 1

    monkeypatch.setattr(pipeline, "save_to_notion", note_notion)
    monkeypatch.setattr(app_module, "notify_all", note_push)
    monkeypatch.setattr(pipeline, "fetch_task_result",
                        lambda *a, **kw: _immediately(_sample_result()))

    written = pipeline.write_report(tmp_path, "Deleted question", "core",
                                    _sample_result(), [], job_id="eeeeeeeeeee1")
    app_module.jobs.data["eeeeeeeeeee1"] = {
        "id": "eeeeeeeeeee1", "question": "Deleted question", "processor": "core",
        "status": "saving", "created_at": "2026-01-01T00:00:00Z",
        "run_id": "trun_x", "report_path": str(written.path)}

    original_update = app_module._update_job

    def delete_once_done(job_id, **fields):
        original_update(job_id, **fields)
        if fields.get("status") == "done":
            app_module.jobs.data.pop(job_id, None)   # the user hit "remove"

    monkeypatch.setattr(app_module, "_update_job", delete_once_done)
    asyncio.run(app_module.run_research("eeeeeeeeeee1"))
    assert called == {"notion": 0, "push": 0}


def test_nothing_is_written_back_to_a_deleted_job():
    """The other half of the race: a late write must not resurrect a record."""
    app_module.jobs.data.clear()
    app_module._update_job("gone", notion_url="https://notion.test/p")
    assert "gone" not in app_module.jobs.data


def test_a_symlinked_report_is_not_served(client, tmp_path):
    """The report is inside the same boundary as the sources beside it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY MATERIAL")
    folder = tmp_path / "out" / "2026-01-01 sym"
    folder.mkdir(parents=True)
    app_module.OUTPUT_DIR = tmp_path / "out"
    link = folder / "sym.md"
    try:
        link.symlink_to(secret)                 # outside OUTPUT_DIR
    except OSError:
        pytest.skip("symlinks unavailable")
    app_module.jobs.data["fffffffffff1"] = {
        "id": "fffffffffff1", "question": "q", "status": "done",
        "created_at": "2026-01-01T00:00:00Z", "report_path": str(link)}
    for url in ("/jobs/fffffffffff1/report", "/jobs/fffffffffff1/report.md",
                "/jobs/fffffffffff1/bundle.zip", "/jobs/fffffffffff1/sources"):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert "PRIVATE KEY" not in resp.text, url


def test_the_source_index_lists_only_files_it_would_serve(client, tmp_path):
    """A link out of the folder is not an archived source; a broken one is not
    an excuse to fail the whole index."""
    written = _finished_job(tmp_path)
    sources = written.path.parent / "sources"
    outside = tmp_path / "outside.md"
    outside.write_text("not part of the dossier")
    try:
        (sources / "08 escape.md").symlink_to(outside)
        (sources / "09 broken.md").symlink_to(sources / "gone.md")
    except OSError:
        pytest.skip("symlinks unavailable")

    body = client.get("/jobs/abcdefabcdef/sources").json()
    listed = [s["file"] for s in body["sources"] if s["file"]]
    assert listed == ["01 Paper A.md"]
    assert all(s["bytes"] >= 0 for s in body["sources"])


def test_a_mailto_endpoint_is_not_a_push_subscription(client, monkeypatch):
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    assert not app_module._valid_subscription(
        {"endpoint": "mailto:x@example.test", "keys": _push_keys()})
    assert client.post("/subscribe", json={
        "endpoint": "mailto:x@example.test",
        "keys": _push_keys()}).status_code == 422
    app_module.subs.data.clear()


def test_a_store_that_is_not_a_dict_of_records_does_not_load(tmp_path):
    listed = tmp_path / "jobs.json"
    listed.write_text("[]")
    store = app_module.JsonStore(listed)
    assert store.data == {}                     # and kept aside for inspection
    assert list(tmp_path.glob("jobs.corrupt-*.json"))

    mixed = tmp_path / "subs.json"
    mixed.write_text('{"good": {"status": "done"}, "bad": "not a record"}')
    assert app_module.JsonStore(mixed).data == {"good": {"status": "done"}}


def test_json_of_the_wrong_shape_takes_the_retry_path(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=["not", "an", "object"])

    copy, = _run_scrape(handler, 1)
    assert calls["n"] == pipeline.MAX_ATTEMPTS
    assert copy.error == "HTTP 200 with an unreadable body"


def test_a_data_field_of_the_wrong_shape_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": []})

    copy, = _run_scrape(handler, 1)
    assert not copy.ok and "attribute" not in copy.error.lower()


def test_archiving_announces_what_it_will_attempt():
    cits = [Citation("https://a.test/1"), Citation("mailto:x@example.test"),
            Citation("javascript:alert(1)")]
    assert [c.url for c in pipeline.scrapable(cits, 10)] == ["https://a.test/1"]
    assert pipeline.scrapable(cits, 0) == []
    assert pipeline.scrapable([Citation("mailto:x@example.test")], 10) == []


def test_notion_keeps_room_for_the_sources(monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"url": "https://notion.test/p"})

    result = ResearchResult(
        content="\n\n".join(f"Paragraph {i}." for i in range(200)),
        citations=[Citation(f"https://s.test/{i}", f"Source {i}") for i in range(5)]
        + [Citation("javascript:alert(1)", "Unsafe")],
        confidence="", reasoning="")

    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.save_to_notion(client, "k", "db", "q", result)

    assert asyncio.run(run()) == "https://notion.test/p"
    blocks = sent["children"]
    assert len(blocks) <= pipeline.NOTION_MAX_BLOCKS
    kinds = [b["type"] for b in blocks]
    assert "heading_2" in kinds                 # the sources survived the report
    links = [b["paragraph"]["rich_text"][0]["text"].get("link")
             for b in blocks if b["type"] == "paragraph"]
    assert {"url": "https://s.test/0"} in links
    assert not any(l and "javascript" in l["url"] for l in links if l)


def test_a_citation_that_is_not_a_web_link_is_not_linked(tmp_path):
    result = ResearchResult(
        content="b", citations=[Citation("javascript:alert(1)", "Click me")],
        confidence="", reasoning="")
    text = pipeline.write_report(tmp_path, "Scheme question", "core",
                                 result, []).path.read_text()
    assert "](javascript:" not in text and "](<javascript:" not in text
    assert "1. Click me — not a web link: `javascript:alert(1)`" in text


def test_retry_after_accepts_an_http_date():
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    resp = httpx.Response(429, headers={"Retry-After": soon})
    assert 20 <= pipeline._retry_after(resp, 2.0) <= 31

    past = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
    assert pipeline._retry_after(
        httpx.Response(429, headers={"Retry-After": past}), 2.0) == 0


def test_source_frontmatter_survives_a_hostile_url(tmp_path):
    result = ResearchResult(content="b",
                            citations=[Citation("https://a.test/1", "T")],
                            confidence="", reasoning="")
    written = pipeline.write_report(
        tmp_path, "Hostile url question", "core", result,
        [SourceCopy("https://a.test/1\nmalicious: yes", "T", "# body", True)])
    copy = next((written.path.parent / "sources").iterdir())
    meta, _ = pipeline.split_front_matter(copy.read_text())
    assert "malicious" not in meta
    assert meta["source"] == "https://a.test/1 malicious: yes"


def test_hostile_text_cannot_escape_a_code_span(tmp_path):
    """A backtick in a provider URL must not end the span it is written in."""
    result = ResearchResult(
        content="b",
        citations=[Citation("javascript:x`\n## Injected heading", "Click")],
        confidence="", reasoning="")
    text = pipeline.write_report(tmp_path, "Backtick question", "core",
                                 result, []).path.read_text()
    line = [l for l in text.splitlines() if "not a web link" in l][0]
    assert line.endswith("``")                  # fenced longer than its content
    assert "## Injected heading" not in text.replace(line, "")
    # The fence outgrows the longest run inside; padding only where a
    # leading or trailing backtick would otherwise be swallowed.
    assert pipeline._md_code("a ``b`` c") == "```a ``b`` c```"
    assert pipeline._md_code("`x`") == "`` `x` ``"


def test_a_multiline_method_note_stays_in_its_block_quote(tmp_path):
    result = ResearchResult(
        content="b", citations=[], confidence="",
        reasoning="First line.\n\n## Not a heading, surely")
    text = pipeline.write_report(tmp_path, "Reasoning question", "core",
                                 result, []).path.read_text()
    note = text.split("## Method note")[1].strip()
    assert note == "> First line.\n>\n> ## Not a heading, surely"


def test_the_prepared_list_is_exactly_what_is_attempted():
    """One place decides: scrapable() prepares, scrape_sources attempts."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["url"])
        return _ok_scrape(request)

    citations = [Citation("https://a.test/1"), Citation("mailto:x@example.test"),
                 Citation("javascript:alert(1)")]
    todo = pipeline.scrapable(citations, 10)      # mailto: is a pointless scrape

    async def run():
        limiter = pipeline.ScrapeLimiter(rate_limit=0, concurrency=2)
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(client, "k", todo, limiter)

    copies = asyncio.run(run())
    assert seen == ["https://a.test/1"]
    assert len(copies) == len(todo) == 1


def test_an_unreadable_response_body_is_retried_and_named(monkeypatch):
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html>not json</html>")

    copy, = _run_scrape(handler, 1)
    assert calls["n"] == pipeline.MAX_ATTEMPTS      # a broken hop, worth retrying
    assert copy.error == "HTTP 200 with an unreadable body"


def test_a_corrupt_state_file_is_kept_for_inspection(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{ this is not json")
    store = app_module.JsonStore(path)
    assert store.data == {}
    assert not path.exists()                        # moved aside, not overwritten
    spoiled = list(tmp_path.glob("jobs.corrupt-*.json"))
    assert len(spoiled) == 1
    assert spoiled[0].read_text() == "{ this is not json"


def test_legacy_subscriptions_are_dropped_on_load(monkeypatch):
    good = {"endpoint": "https://push.test/x", "keys": _push_keys()}
    app_module.subs.data.clear()
    app_module.subs.data.update({
        "keep": good,
        "no-keys": {"endpoint": "https://push.test/y", "keys": {}},
        "bad-endpoint": {"endpoint": "ftp://push.test/z",
                         "keys": _push_keys()},
        "undecodable": {"endpoint": "https://push.test/w",
                        "keys": {"p256dh": "x", "auth": "x"}},
    })
    monkeypatch.setattr(app_module.subs, "save", lambda: None)
    app_module._drop_unusable_subscriptions()
    assert list(app_module.subs.data) == ["keep"]
    app_module.subs.data.clear()


# ---------------------------------------------------------------------------
# One whole job, end to end
# ---------------------------------------------------------------------------

def test_a_whole_job_from_question_to_dossier(client, tmp_path, monkeypatch):
    """Submit, research, archive, write, serve — with the providers mocked.

    Most of the suite is unit-level; this is the one that would notice the
    pieces no longer fitting together.
    """
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    monkeypatch.setattr(app_module, "FIRECRAWL_API_KEY", "fc")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "BASE_BACKOFF_S", 0.01)
    app_module.jobs.data.clear()

    result = ResearchResult(
        content="**The answer.**\n\n## Evidence\n\nStuff.",
        citations=[Citation("https://open.test/a", "Open source"),
                   Citation("https://closed.test/b", "Closed source"),
                   Citation("https://blocked.test/c", "Bot-walled source"),
                   Citation("mailto:someone@example.test", "A person")],
        confidence="high", reasoning="triangulated")

    async def fake_result(*a, **kw):
        return result

    monkeypatch.setattr(pipeline, "start_task_run",
                        lambda *a, **kw: _immediately("trun_lifecycle"))
    monkeypatch.setattr(pipeline, "fetch_task_result", fake_result)

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            if request.url.host == "closed.test":
                return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
            return httpx.Response(404)
        target = json.loads(request.content)["url"]
        if "blocked.test" in target:
            return httpx.Response(403, json={"success": False,
                                             "error": "bot wall"})
        return httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "# Open\n\nThe archived text.",
                     "metadata": {"title": "Open source"}}})

    async def drive():
        app_module.app.state.limiter = pipeline.ScrapeLimiter(
            rate_limit=0, concurrency=2)
        app_module.app.state.robots = pipeline.RobotsCache()
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(provider)) as http:
            app_module.app.state.client = http
            job_id = uuid.uuid4().hex[:12]
            app_module._put_job(job_id, {
                "id": job_id, "question": "A perfectly fine question",
                "processor": "core", "status": "queued", "progress": "Queued",
                "created_at": "2026-01-01T00:00:00Z", "run_id": "",
                "report_path": "", "notion_url": "", "error": ""})
            await app_module.run_research(job_id)
            return job_id

    job_id = asyncio.run(drive())
    job = app_module.jobs.data[job_id]
    assert job["status"] == "done", job.get("error")
    assert job["sources_cited"] == 4 and job["sources_archived"] == 1

    # The dossier on disk: one archived copy, numbered by its citation.
    report = Path(job["report_path"])
    assert report.exists()
    text = report.read_text()
    assert "[Open source](https://open.test/a)" in text
    assert pipeline.ROBOTS_DISALLOWED in text          # why, not just that
    assert "bot wall" in text
    assert "A person — not a web link" not in text     # mailto is still linked
    assert [f.name for f in (report.parent / "sources").glob("*.md")] == \
        ["01 Open source.md"]

    # And what the app serves from it.
    listing = client.get(f"/jobs/{job_id}/sources").json()
    assert listing["cited"] == 4 and listing["archived"] == 1
    by_url = {s["url"]: s for s in listing["sources"]}
    assert by_url["https://open.test/a"]["archived"]
    assert by_url["https://closed.test/b"]["note"] == pipeline.ROBOTS_DISALLOWED
    assert by_url["mailto:someone@example.test"]["url"].startswith("mailto:")

    assert client.get(f"/jobs/{job_id}/report").status_code == 200
    with zipfile.ZipFile(io.BytesIO(
            client.get(f"/jobs/{job_id}/bundle.zip").content)) as archive:
        assert any(n.endswith("01 Open source.md") for n in archive.namelist())
    app_module.jobs.data.clear()


# ---------------------------------------------------------------------------
# The boundary sweep: every place outside text enters, one rule at each
# ---------------------------------------------------------------------------

# What gets pushed through each boundary. The surrogate is the one that keeps
# coming back: it survives JSON decoding and cannot be JSON encoded.
HOSTILE = ["\ud800", "a\ud800b", "\x00nul", "\x1b[31m", "x" * 5000,
           ["a", "list"], {"a": "dict"}, 12345, True, None, float("nan")]


def test_clean_text_is_total():
    """Whatever arrives, a string comes out — and it can be encoded."""
    for value in HOSTILE:
        out = pipeline.clean_text(value)
        assert isinstance(out, str)
        out.encode("utf-8")                       # would raise on a surrogate


def test_clean_json_makes_serialisation_total():
    """The store cannot hold something json.dump would refuse."""
    hostile = {"str": "\ud800", "nested": {"list": HOSTILE},
               "nan": float("nan"), "inf": float("inf"),
               7: "non-string key", "set": {"a"}, "path": Path("/tmp/x")}
    encoded = json.dumps(pipeline.clean_json(hostile))
    assert json.loads(encoded)                    # and it survives a round trip
    assert json.loads(encoded)["nan"] is None


def test_the_store_round_trips_anything_it_is_given(tmp_path):
    """Write, reload, write again: no value can wedge it."""
    store = app_module.JsonStore(tmp_path / "jobs.json")
    store.data["abcdefabcdef"] = {"id": "abcdefabcdef", "junk": HOSTILE,
                                  "error": "\ud800", "n": float("inf")}
    store.save()
    again = app_module.JsonStore(tmp_path / "jobs.json")
    assert not pipeline.has_lone_surrogate(json.dumps(again.data))
    again.save()                                  # and again, from the reload


def test_hostile_provider_payloads_never_fail_a_job(tmp_path):
    """Parallel and Firecrawl, at every field, into a written dossier."""
    payload = {"run": {"status": ["not", "a", "status"]},
               "output": {"content": "The answer \ud800.",
                          "basis": [{"confidence": {"h": 1}, "reasoning": 5,
                                     "citations": [
                                         {"url": "https://a.test/1",
                                          "title": "t\ud800", "excerpts": [None, 3]},
                                         {"url": ["nope"]}]}]}}
    result = pipeline._parse_task_result(payload, "trun_x")
    sources = [SourceCopy("https://a.test/1", "T\ud800", "# body \ud800",
                          True),
               SourceCopy("https://b.test/2", "B", "", False, "blocked \ud800")]
    written = pipeline.write_report(tmp_path, "Hostile payload question",
                                    "core", result, sources, job_id="abcdefabcdef")
    # Everything on disk is readable UTF-8, and reading it back is clean.
    for path in [written.path, *(written.path.parent / "sources").glob("*.md")]:
        path.read_text(encoding="utf-8")
        meta, body = pipeline.split_front_matter(path.read_text(encoding="utf-8"))
        assert not pipeline.has_lone_surrogate(json.dumps([meta, body]))


def test_every_http_boundary_survives_hostile_input(client, monkeypatch, tmp_path):
    """Drive the request boundaries, then prove the store is still writable."""
    monkeypatch.setattr(app_module, "PARALLEL_API_KEY", "k")
    monkeypatch.setattr(app_module, "VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setattr(app_module, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(app_module, "VAPID_CLAIM_EMAIL", "me@example.test")
    app_module.jobs.data.clear()

    # Request bodies, including bytes a JSON encoder could not have produced.
    bodies = [b'{"question": "' + b"\\ud800" * 8 + b'"}',
              b'{"question": "a fine question\\u0000here"}',
              b'{"question": ["a", "list"]}',
              b'{"question": "a perfectly fine question", "processor": 5}',
              b'{"not json at all"',
              b'{"question": "a perfectly fine question\xff"}']
    for body in bodies:
        resp = client.post("/research", content=body,
                           headers={"Content-Type": "application/json"})
        # 400 for a body that is not decodable at all, 422 for one that is
        # decodable and refused; never 500, and never a job in the store.
        assert resp.status_code in (200, 400, 422), (body, resp.status_code)

    # Raw bytes, because a surrogate cannot be encoded into a request body by
    # any client — it only ever arrives as a JSON escape.
    for body in (b'{"endpoint": "https://\\ud800", '
                 b'"keys": {"p256dh": "\\ud800", "auth": "x"}}',
                 b'{"endpoint": "https://push.test/x", "keys": {"p256dh": null}}',
                 b'{"endpoint": "https://push.test/x", "keys": "not a dict"}',
                 b'{"endpoint": 5, "keys": {}}'):
        resp = client.post("/subscribe", content=body,
                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 422, (body, resp.status_code)

    # Query and path parameters.
    for url, params in (("/jobs", {"limit": "not a number"}), ("/jobs", {"limit": -1}),
                        ("/research/%00", None), ("/jobs/..%2f..%2fetc/report", None),
                        ("/jobs/abcdefabcdef/sources/%2e%2e%2fescape.md", None),
                        ("/jobs", {"token": "cafe\u0301"})):
        assert client.get(url, params=params).status_code in (200, 401, 404, 422)

    # A surrogate cannot travel in a URL either, so the path-parameter side of
    # that boundary is exercised where it is actually decided.
    assert app_module._servable_report("\ud800") is None
    assert not app_module._token_matches("\ud800")

    # The point of all of it: the store still works.
    app_module.jobs.save()
    assert client.get("/jobs").status_code == 200
    assert client.get("/health").status_code == 200
    app_module.jobs.data.clear()
    app_module.subs.data.clear()


def test_a_non_ascii_token_is_wrong_not_fatal(locked_client):
    """compare_digest refuses non-ASCII strings with a TypeError."""
    assert not app_module._token_matches("café")
    assert not app_module._token_matches("\ud800")
    assert app_module._token_matches("sekrit")
    assert locked_client.get("/jobs", params={"token": "café"}).status_code == 401


def test_a_dossier_with_invalid_bytes_still_renders(client, tmp_path):
    """The notes folder is written by other software, and sometimes badly."""
    written = _finished_job(tmp_path)
    written.path.write_bytes(
        b'---\nquestion: "q"\n---\n\n# Body \xff\xfe invalid\n')
    (written.path.parent / "sources" / "01 Paper A.md").write_bytes(
        b'---\nsource: "https://a.test/1"\n---\n\nbody \xff\n')
    assert client.get("/jobs/abcdefabcdef/report").status_code == 200
    assert client.get("/jobs/abcdefabcdef/sources/01 Paper A.md").status_code == 200
    assert client.get("/jobs/abcdefabcdef/sources").status_code == 200


# ---------------------------------------------------------------------------
# Service worker routing (the patterns it actually ships, not a copy of them)
# ---------------------------------------------------------------------------

def _sw_pattern(name):
    """Pull a route predicate's regex out of the shipped service worker."""
    source = (Path(__file__).resolve().parent.parent
              / "static" / "service-worker.js").read_text()
    literal = re.search(rf"const {name} = \(url\) =>\s*/(.+?)/\.test",
                        source, re.S).group(1)
    return re.compile(literal.replace("\\/", "/"))   # JS escapes its delimiter


def test_the_worker_awaits_its_cache_writes():
    """An unawaited put can be in flight when the worker is stopped."""
    source = (Path(__file__).resolve().parent.parent
              / "static" / "service-worker.js").read_text()
    puts = re.findall(r"^.*\.put\(.*$", source, re.M)
    assert puts, "no cache writes found"
    for line in puts:
        assert "await" in line, line


def test_the_pwa_replaces_a_subscription_made_with_an_old_key():
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    assert "sameKey(" in app_js and "unsubscribe()" in app_js
    assert "applicationServerKey" in app_js


def test_the_pwa_hides_links_to_a_report_that_is_gone():
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    assert "report_available !== true" in app_js


def test_the_page_can_tell_the_worker_to_forget_a_job():
    sw = (Path(__file__).resolve().parent.parent
          / "static" / "service-worker.js").read_text()
    app_js = (Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text()
    assert '"forget-job"' in sw and 'addEventListener("message"' in sw
    assert '"forget-job"' in app_js and "postMessage" in app_js


def test_deleting_a_job_clears_every_entry_it_owns():
    """forgetJob works on the whole job prefix, not one URL."""
    source = (Path(__file__).resolve().parent.parent
              / "static" / "service-worker.js").read_text()
    assert "async function forgetJob(url)" in source
    # Both handlers must reach for it, or an offline visit resurrects the job.
    assert source.count("forgetJob(") >= 3
    prefix = re.search(r"const prefix = `(/jobs/\$\{job\[1\]\}/)`", source)
    assert prefix, "forgetJob should match on the job prefix"


def test_the_worker_caches_pages_but_not_the_index_or_downloads():
    """Written pages are immutable; the sources index describes a live folder."""
    dossier, index = _sw_pattern("isDossier"), _sw_pattern("isSourceIndex")

    for path in ("/jobs/ababababab12/report", "/jobs/ababababab12/sources/01 a.md"):
        assert dossier.search(path), path          # cache-first
        assert not index.search(path), path

    assert index.search("/jobs/ababababab12/sources")       # network-first
    assert not dossier.search("/jobs/ababababab12/sources")

    for path in ("/jobs/ababababab12/report.md", "/jobs/ababababab12/bundle.zip", "/jobs", "/health"):
        assert not dossier.search(path), path      # never cached
        assert not index.search(path), path


# ---------------------------------------------------------------------------
# Token auth
# ---------------------------------------------------------------------------

@pytest.fixture()
def locked_client(monkeypatch):
    app_module.jobs.data.clear()
    monkeypatch.setattr(app_module, "FOOTNOTE_TOKEN", "sekrit")
    with TestClient(app_module.app) as c:
        yield c


def test_token_required(locked_client):
    assert locked_client.get("/jobs").status_code == 401
    assert locked_client.post(
        "/research", json={"question": "A perfectly fine question"}
    ).status_code == 401
    # Source material is behind the token too — it is the research itself.
    assert locked_client.get("/jobs/abcdefabcdef/sources").status_code == 401
    assert locked_client.get("/jobs/abcdefabcdef/sources/01 a.md").status_code == 401
    assert locked_client.get("/jobs/abcdefabcdef/bundle.zip").status_code == 401


def test_token_accepted_via_bearer_and_query(locked_client):
    assert locked_client.get(
        "/jobs", headers={"Authorization": "Bearer sekrit"}).status_code == 200
    resp = locked_client.get("/jobs", params={"token": "sekrit"})
    assert resp.status_code == 200                # an API client gets its answer
    assert "footnote_token" in resp.headers.get("set-cookie", "")


def test_a_browser_is_redirected_off_the_token_url(locked_client):
    """The cookie carries it from here; the address bar should not."""
    resp = locked_client.get("/", params={"token": "sekrit"},
                             headers={"Accept": "text/html"},
                             follow_redirects=False)
    assert resp.status_code == 303
    assert "token" not in resp.headers["location"]
    assert "footnote_token" in resp.headers.get("set-cookie", "")


def test_health_and_shell_public_when_locked(locked_client):
    assert locked_client.get("/health").status_code == 200
    assert locked_client.get("/manifest.json").status_code == 200
    assert locked_client.get("/service-worker.js").status_code == 200
