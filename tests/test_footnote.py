# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests: pipeline parsing, report writing, API surface, token auth.

Run with:  python -m pytest
No network access required — external APIs are mocked.
"""
import asyncio
import io
import json
import zipfile
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
                max_sources=10)

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
                client, "k", [Citation(f"https://s.test/{i}") for i in range(9)],
                max_sources=3)

    assert len(asyncio.run(run())) == 3 and len(seen) == 3


# ---------------------------------------------------------------------------
# Living inside Firecrawl's free plan: 10 requests/min, 2 concurrent, 402 hard
# ---------------------------------------------------------------------------

def _ok_scrape(_request):
    return httpx.Response(200, json={"success": True,
                                     "data": {"markdown": "body",
                                              "metadata": {"title": "T"}}})


def _run_scrape(handler, count, **kw):
    async def run():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            return await pipeline.scrape_sources(
                client, "k", [Citation(f"https://s.test/{i}") for i in range(count)],
                max_sources=count, **kw)

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
    assert "confidence: medium" in text
    assert "[Paper A](https://a.test/1)" in text
    assert "local copy" in text                      # archived source linked
    assert "could not be archived" in text and "blocked" in text
    assert "> quoted bit" in text
    src_files = list((report.parent / "sources").glob("*.md"))
    assert len(src_files) == 1 and src_files[0].name.startswith("01 ")
    assert "source: https://a.test/1" in src_files[0].read_text()


def test_write_report_unique_folder(tmp_path):
    r1 = pipeline.write_report(tmp_path, "Same question here", "core",
                               _sample_result(), []).path
    r2 = pipeline.write_report(tmp_path, "Same question here", "core",
                               _sample_result(), []).path
    assert r1.parent != r2.parent and r2.parent.name.endswith("(2)")


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
    """A done job whose dossier really exists on disk, as write_report leaves it."""
    written = pipeline.write_report(
        tmp_path, "How do solid-state batteries work?", "core",
        _sample_result(),
        [SourceCopy("https://a.test/1", "Paper A", "# A body", True),
         SourceCopy("https://b.test/2", "Paper B", "", False, "blocked")])
    job = {"id": "xyz", "question": "How do solid-state batteries work?",
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
    app_module.jobs.data["xyz"] = job
    return written


def test_report_source_rendered_and_downloadable(client, tmp_path):
    _finished_job(tmp_path)

    page = client.get("/jobs/xyz/sources/01 Paper A.md")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<h1>Paper A</h1>" in page.text          # title from the frontmatter
    assert "https://a.test/1" in page.text          # link back to the original
    assert "<h1>A body</h1>" in page.text           # the copy itself, rendered

    raw = client.get("/jobs/xyz/sources/01 Paper A.md", params={"raw": 1})
    assert raw.status_code == 200
    assert "markdown" in raw.headers["content-type"]
    assert "attachment" in raw.headers["content-disposition"]
    assert raw.text.startswith("---")                # the file, frontmatter and all


def test_report_source_traversal_blocked(client, tmp_path):
    _finished_job(tmp_path)
    assert client.get("/jobs/xyz/sources/.hidden").status_code == 404
    assert client.get("/jobs/xyz/sources/%2e%2e%2fr.md").status_code == 404
    assert client.get("/jobs/xyz/sources/nope.md").status_code == 404


def test_sources_index_lists_archived_and_missing(client, tmp_path):
    _finished_job(tmp_path)
    body = client.get("/jobs/xyz/sources").json()

    assert body["cited"] == 2 and body["archived"] == 1
    assert body["bundle_url"] == "/jobs/xyz/bundle.zip"
    first, second = body["sources"]
    assert first["title"] == "Paper A" and first["archived"]
    assert first["read_url"] == "/jobs/xyz/sources/01%20Paper%20A.md"
    assert first["download_url"].endswith("?raw=1") and first["bytes"] > 0
    assert not second["archived"] and second["note"] == "blocked"
    assert second["url"] == "https://b.test/2"       # still readable at the source


def test_sources_index_falls_back_to_the_files_on_disk(client, tmp_path):
    """Dossiers written before jobs recorded citations still list their copies."""
    written = _finished_job(tmp_path)
    del app_module.jobs.data["xyz"]["citations"]
    body = client.get("/jobs/xyz/sources").json()
    assert [s["file"] for s in body["sources"]] == ["01 Paper A.md"]
    assert body["sources"][0]["url"] == "https://a.test/1"   # from frontmatter
    assert written.source_files == {"https://a.test/1": "01 Paper A.md"}


def test_sources_index_survives_a_copy_deleted_in_the_notes_folder(client, tmp_path):
    written = _finished_job(tmp_path)
    (written.path.parent / "sources" / "01 Paper A.md").unlink()
    body = client.get("/jobs/xyz/sources").json()
    assert body["archived"] == 0
    assert body["sources"][0]["read_url"] == ""      # citation, no local copy
    assert client.get("/jobs/xyz/sources/01 Paper A.md").status_code == 404


def test_bundle_zip_holds_report_and_sources(client, tmp_path):
    written = _finished_job(tmp_path)
    resp = client.get("/jobs/xyz/bundle.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = sorted(archive.namelist())
        folder = written.path.parent.name
        assert names == [f"{folder}/How do solid-state batteries work.md",
                         f"{folder}/sources/01 Paper A.md"]
        assert b"# A body" in archive.read(f"{folder}/sources/01 Paper A.md")


def test_report_view_links_to_the_bundle(client, tmp_path):
    _finished_job(tmp_path)
    page = client.get("/jobs/xyz/report")
    assert page.status_code == 200
    assert "/jobs/xyz/bundle.zip" in page.text
    assert "question:" not in page.text              # frontmatter stripped
    # The report's relative "local copy" link resolves to the source view.
    assert 'href="sources/01 Paper A.md"' in page.text


# ---------------------------------------------------------------------------
# The server's filesystem stays the server's business
# ---------------------------------------------------------------------------

def test_job_json_hides_server_bookkeeping(client, tmp_path):
    _finished_job(tmp_path)
    job = client.get("/research/xyz").json()
    assert "report_path" not in job and "run_id" not in job
    assert "citations" not in job             # served by /sources on demand
    assert job["report_name"] == "How do solid-state batteries work.md"


def test_no_response_carries_a_server_path(client, tmp_path):
    written = _finished_job(tmp_path)
    folder = str(written.path.parent)
    for url in ("/health", "/jobs", "/research/xyz", "/jobs/xyz/sources",
                "/jobs/xyz/report", "/jobs/xyz/sources/01 Paper A.md"):
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
    app_module.jobs.data["abc"] = {"id": "abc", "question": "q",
                                   "status": "researching",
                                   "created_at": "2026-01-01T00:00:00Z"}
    assert client.delete("/jobs/abc").status_code == 409


def test_index_and_pwa_assets(client):
    shell = client.get("/")
    assert shell.status_code == 200 and "Footnote" in shell.text
    assert "serviceWorker" in client.get("/static/app.js").text
    assert client.get("/manifest.json").json()["name"] == "Footnote"
    sw = client.get("/service-worker.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]


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
    assert locked_client.get("/jobs/xyz/sources").status_code == 401
    assert locked_client.get("/jobs/xyz/sources/01 a.md").status_code == 401
    assert locked_client.get("/jobs/xyz/bundle.zip").status_code == 401


def test_token_accepted_via_bearer_and_query(locked_client):
    assert locked_client.get(
        "/jobs", headers={"Authorization": "Bearer sekrit"}).status_code == 200
    resp = locked_client.get("/jobs", params={"token": "sekrit"})
    assert resp.status_code == 200
    assert "footnote_token" in resp.headers.get("set-cookie", "")


def test_health_and_shell_public_when_locked(locked_client):
    assert locked_client.get("/health").status_code == 200
    assert locked_client.get("/manifest.json").status_code == 200
    assert locked_client.get("/service-worker.js").status_code == 200
