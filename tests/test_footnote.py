# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests: pipeline parsing, report writing, API surface, token auth.

Run with:  python -m pytest
No network access required — external APIs are mocked.
"""
import asyncio
import json

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
        if "bad" in url:
            return httpx.Response(500, json={"success": False, "error": "nope"})
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
    report = pipeline.write_report(
        tmp_path, "How do solid-state batteries work?", "core",
        _sample_result(), sources)

    assert report.exists()
    assert report.parent.name.endswith("How do solid-state batteries work")
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
                               _sample_result(), [])
    r2 = pipeline.write_report(tmp_path, "Same question here", "core",
                               _sample_result(), [])
    assert r1.parent != r2.parent and r2.parent.name.endswith("(2)")


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


def test_report_source_served_and_traversal_blocked(client, tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "01 a.md").write_text("body")
    report = tmp_path / "r.md"
    report.write_text("# r")
    app_module.jobs.data["xyz"] = {"id": "xyz", "question": "q",
                                   "status": "done",
                                   "created_at": "2026-01-01T00:00:00Z",
                                   "report_path": str(report)}
    ok = client.get("/jobs/xyz/sources/01 a.md")
    assert ok.status_code == 200 and ok.text == "body"
    assert client.get("/jobs/xyz/sources/.hidden").status_code == 404
    assert client.get("/jobs/xyz/sources/%2e%2e%2fr.md").status_code == 404


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
