# Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
# Licensed under the GNU AGPL v3.0 or later; see the LICENSE file for details.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Browser tests: the behaviour that only exists in a browser.

The rest of the suite reads `static/*.js` as text, which cannot tell whether
a panel survives a poll, whether a download navigates, or what the service
worker serves when the network is gone. Every one of those has been a real
bug here, and each was found by driving a browser by hand.

These run a real server on a loopback port and drive Chromium against it, so
they are slower than the rest and need a browser:

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/playwright install chromium
    .venv/bin/python -m pytest tests/test_browser.py

Without it they skip rather than fail — the offline suite is what gates a
commit, and a missing browser is not a broken Footnote.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JOB_ID = "df31fcbed547"
QUESTION = "What is the current evidence on creatine and cognition?"

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed").sync_playwright


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _fixture_dossier(root: Path):
    """One finished job on disk, as write_report would have left it."""
    folder = root / f"2026-09-04 {QUESTION.rstrip('?')}"
    (folder / "sources").mkdir(parents=True)
    report = folder / f"{QUESTION.rstrip('?')}.md"
    report.write_text(
        f"---\nquestion: {QUESTION}\n---\n\n# {QUESTION}\n\n"
        "Creatine shows small but consistent effects under sleep deprivation."
        "\n\n## Sources\n\n1. [Meta-analysis](https://example.test/meta) — "
        "[local copy](sources/01 Meta-analysis.md)\n")
    for number, title in ((1, "Meta-analysis"), (2, "Examine")):
        (folder / "sources" / f"0{number} {title}.md").write_text(
            f"---\ntitle: {title}\nsource: https://example.test/{number}\n"
            f"retrieved: 2026-09-04\n---\n\n# {title}\n\nArchived body text.\n")
    return report


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("browser")
    output, data = root / "out", root / "data"
    output.mkdir()
    data.mkdir()
    report = _fixture_dossier(output)
    (data / "jobs.json").write_text(json.dumps({JOB_ID: {
        "id": JOB_ID, "question": QUESTION, "processor": "core",
        "status": "done", "progress": "Done — 2 sources cited, 2 archived",
        "created_at": "2026-09-04T09:12:44Z",
        "finished_at": "2026-09-04T09:18:02Z", "run_id": "trun_x",
        "report_path": str(report), "notion_url": "", "error": "",
        "sources_cited": 2, "sources_archived": 2,
        "citations": [
            {"url": "https://example.test/1", "title": "Meta-analysis",
             "file": "01 Meta-analysis.md", "note": ""},
            {"url": "https://example.test/2", "title": "Examine",
             "file": "02 Examine.md", "note": ""}],
    }}))
    port = _free_port()
    env = dict(os.environ, OUTPUT_DIR=str(output), DATA_DIR=str(data),
               HOST="127.0.0.1", PORT=str(port), PARALLEL_API_KEY="dummy")
    env.pop("FOOTNOTE_TOKEN", None)          # no auth: fewer moving parts
    process = subprocess.Popen([sys.executable, "app.py"], cwd=REPO, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for(base, process)
        yield base
    finally:
        process.terminate()
        process.wait(timeout=10)


def _wait_for(base, process):
    import urllib.request
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(process.stdout.read().decode("utf-8", "replace"))
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as answer:
                if answer.status == 200:
                    return
        except Exception:                                  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError("the server did not come up")


@pytest.fixture(scope="module")
def browser():
    # Installed and *usable* are different questions: the package can be
    # present with no browser behind it, and that is a missing tool, not a
    # broken Footnote.
    with sync_playwright() as play:
        try:
            engine = play.chromium.launch()
        except Exception as why:                           # noqa: BLE001
            pytest.skip(f"chromium is not installed for playwright: {why}")
        yield engine
        engine.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context(viewport={"width": 375, "height": 812})
    sheet = context.new_page()
    yield sheet
    context.close()


def _card_ready(sheet, base):
    sheet.goto(base)
    sheet.wait_for_selector(".job .links button")


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def test_only_the_newest_refresh_renders_and_arms_one_timer(page, server):
    """A refresh is started by the poll, by submitting and by removing, so
    two can be in flight. A slow first response landed after a fast second
    one and put the older list back — and both armed a timer, which doubled
    the poll rate for the rest of the session."""
    _card_ready(page, server)
    result = page.evaluate("""async () => {
      const realFetch = window.fetch;
      let call = 0;
      const list = (question) => new Response(JSON.stringify({active: 0, jobs: [
        {id: 'aaaaaaaaaaaa', question, processor: 'core', status: 'done',
         created_at: '2026-09-04T09:00:00Z', report_available: true,
         report_name: 'r.md', sources_cited: 0}]}),
        {headers: {'Content-Type': 'application/json'}});
      window.fetch = (url, opts) => {
        if (String(url) === '/jobs') {
          const which = ++call;
          return new Promise((done) => setTimeout(
            () => done(list(which === 1 ? 'OLD list' : 'NEW list')),
            which === 1 ? 400 : 30));
        }
        return realFetch(url, opts);
      };
      const realTimeout = window.setTimeout;
      let armed = 0;
      window.setTimeout = (fn, ms) => {
        if (fn === refreshJobs) armed += 1;
        return realTimeout(fn, ms);
      };
      await Promise.all([refreshJobs(), refreshJobs()]);
      await new Promise((done) => realTimeout(done, 600));
      const shown = document.querySelector('.job .q').textContent;
      window.fetch = realFetch; window.setTimeout = realTimeout;
      return {shown, armed};
    }""")
    assert result["shown"] == "NEW list"
    assert result["armed"] == 1


def test_every_open_panel_survives_a_poll(page, server):
    """The five-second poll rebuilds the whole list. Something you are
    part-way through reading must not vanish because a timer fired."""
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    page.get_by_role("button", name="Sources").click()
    page.wait_for_selector(".sources ol li")
    page.locator(".sources ol li button.src-title").first.click()
    page.wait_for_selector(".sources ol li > .src-body")

    reading = page.locator(".job > .src-body .src-content").inner_text()
    page.evaluate("refreshJobs()")
    page.wait_for_timeout(700)

    assert page.locator(".job > .src-body .src-content").inner_text() == reading
    assert page.locator(".sources").first.is_visible()
    assert page.locator(".sources ol li > .src-body").count() == 1


# ---------------------------------------------------------------------------
# Taking a file away without leaving the page
# ---------------------------------------------------------------------------

def test_the_bundle_downloads_without_navigating(page, server):
    """A link to a file is a navigation whose outcome the browser chooses,
    and on iOS that is a sheet with no way back. The zip is fetched and
    handed over instead, from a button."""
    _card_ready(page, server)
    before = page.url
    with page.expect_download() as caught:
        page.get_by_role("button", name="Everything (.zip)").click()
    assert caught.value.suggested_filename.endswith(".zip")
    assert page.url == before
    # And nothing on the card is an anchor to a file.
    hrefs = page.eval_on_selector_all(
        ".job a", "links => links.map((a) => a.getAttribute('href'))")
    assert not [h for h in hrefs if h and h.endswith((".md", ".zip"))]


def test_a_failed_action_says_so_on_a_standalone_page(page, server):
    """These pages have no flash area, and their handlers caught everything
    into an empty block: Copy, Save and Download were indistinguishable from
    a button that does nothing."""
    page.goto(f"{server}/jobs/{JOB_ID}/report")
    page.click("a[data-file='text']")
    page.wait_for_selector(".file-actions")
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard',
                            {value: undefined, configurable: true});
      document.execCommand = () => false;
    }""")
    page.get_by_role("button", name="Copy").click()
    note = page.wait_for_selector(".file-note")
    assert "Could not copy" in note.inner_text()


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------

CACHED_JOB = """async (id) => {
  for (const name of await caches.keys()) {
    const hits = await (await caches.open(name)).keys();
    if (hits.some((r) => r.url.includes(id))) return true;
  }
  return false;
}"""


def _poll(sheet, script, arg, want, seconds=10):
    """page.evaluate in a loop, because wait_for_function does not await.

    Playwright checks the predicate's return value for truthiness without
    awaiting it, so an async predicate hands it a Promise — always truthy,
    always an instant pass. A mutation that ignored the "forget this job"
    message went undetected until this was noticed.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if sheet.evaluate(script, arg) == want:
            return True
        sheet.wait_for_timeout(100)
    return False


def _service_worker_ready(sheet, base):
    sheet.goto(base)
    sheet.wait_for_function("() => navigator.serviceWorker.controller !== null",
                            timeout=15000)


def test_a_dossier_read_once_is_readable_with_no_network(page, server):
    _service_worker_ready(page, server)
    page.goto(f"{server}/jobs/{JOB_ID}/report")
    page.wait_for_selector(".report-body")
    page.context.set_offline(True)
    try:
        page.goto(f"{server}/jobs/{JOB_ID}/report")
        assert "creatine" in page.inner_text("body").lower()
    finally:
        page.context.set_offline(False)


def test_removing_a_job_empties_its_cache(page, server):
    """A deleted dossier that stays in the cache is readable offline for
    ever, which is not what "remove" means."""
    _service_worker_ready(page, server)
    page.goto(f"{server}/jobs/{JOB_ID}/report")
    page.wait_for_selector(".report-body")
    assert page.evaluate(CACHED_JOB, JOB_ID), "it was never cached at all"

    page.goto(server)
    page.wait_for_selector(".job .del")
    page.evaluate(
        """(id) => navigator.serviceWorker.controller.postMessage(
             {type: 'forget-job', jobId: id})""", JOB_ID)
    assert _poll(page, CACHED_JOB, JOB_ID, False), \
        "the dossier is still cached after the job was removed"
