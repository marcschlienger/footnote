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


def test_panels_stack_in_the_order_their_controls_sit(page, server):
    """The row reads Read · Sources, and the dossier reader was appended to
    the end of the card — so opening both put the sources list above the
    dossier, the reverse of the row that opens them."""
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    page.get_by_role("button", name="Sources").click()
    page.wait_for_selector(".sources ol li")

    order = page.eval_on_selector_all(
        ".job:last-child > *", "els => els.map((e) => e.className)")
    assert order.index("src-body") < order.index("sources"), order

    # Opening them the other way round gives the same card.
    page.reload()
    page.wait_for_selector(".job .links button")
    page.get_by_role("button", name="Sources").click()
    page.wait_for_selector(".sources ol li")
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    order = page.eval_on_selector_all(
        ".job:last-child > *", "els => els.map((e) => e.className)")
    assert order.index("src-body") < order.index("sources"), order


def test_opening_the_sources_list_brings_it_into_view(page, server):
    """The dossier now sits between the links and the list, which can be a
    screenful. What stops that from looking like a control that did nothing
    is that whatever was just opened is scrolled to — so this is the check
    the panel order rests on."""
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    page.locator(".job .links").first.scroll_into_view_if_needed()
    page.wait_for_timeout(200)

    page.get_by_role("button", name="Sources").click()
    page.wait_for_selector(".sources ol li")
    assert _poll(page, """() => {
        const box = document.querySelector('.sources').getBoundingClientRect();
        return box.top >= 0 && box.top < window.innerHeight;
      }""", None, True, seconds=5), "the sources list opened off screen"


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
# Saying the same thing in both directions
# ---------------------------------------------------------------------------

def test_a_job_is_labelled_with_the_depth_that_was_asked_for(page, server):
    """The picker offers "Quick look", "Standard", "Deep"; the card printed
    the processor id, so a job asked for as "Exhaustive" came back "ultra"."""
    _card_ready(page, server)
    offered = page.eval_on_selector_all(
        "#processor option",
        "opts => opts.map((o) => o.textContent.split('\u00b7')[0].trim())")
    shown = page.eval_on_selector_all(
        ".job .meta > .depth", "spans => spans.map((s) => "
        "({label: s.textContent, id: s.title}))")
    assert shown, "no job on the page to check"
    for entry in shown:
        assert entry["label"] in offered, entry
        assert entry["label"] != entry["id"]      # not the raw processor id


def test_every_processor_the_api_takes_is_said_in_the_pickers_words(page, server):
    """The picker offers five of the eighteen the API accepts, and the other
    thirteen are those same depths with a multiplier or the fast variant. A
    job started with curl should not be the one card labelled "ultra2x"."""
    _card_ready(page, server)
    processors = page.evaluate(
        "() => fetch('/processors').then((r) => r.json())")["processors"]
    labels = page.evaluate(
        "(list) => Object.fromEntries(list.map((p) => [p, depthLabel(p)]))",
        processors)
    # "lite" is below the shallowest depth the picker offers, so it has no
    # name to borrow and keeps its own. Everything else has one.
    unlabelled = sorted(p for p, label in labels.items() if label == p)
    assert unlabelled == ["lite", "lite-fast"], unlabelled
    assert labels["ultra4x"] == "Heroic"
    assert labels["ultra4x-fast"] == "Heroic (fast)"    # not "Exhaustive ×4"
    assert labels["core2x"] == "Standard ×2"


def test_starting_a_job_is_confirmed_in_the_pickers_words(page, server):
    """The API answers in processor ids — right for curl and the Shortcut,
    wrong here, where the picker had just said "Exhaustive"."""
    _card_ready(page, server)
    page.select_option("#processor", "ultra")
    page.fill("#question", "What is the current evidence on cold exposure?")
    page.evaluate("""() => document.getElementById('ask')
                       .dispatchEvent(new Event('submit', {cancelable: true}))""")
    note = page.wait_for_selector("#flash:not([hidden])")
    text = note.inner_text()
    assert "Exhaustive" in text, text
    assert "ultra" not in text, text


def test_removing_a_job_says_what_it_does_not_delete(page, server):
    """The dossier lives in a notes folder and stays there; "remove" alone
    reads like it might not."""
    _card_ready(page, server)
    title = page.locator(".job .del").first.get_attribute("title")
    assert title and "notes" in title.lower(), title


def test_a_job_carries_a_date_and_not_only_a_clock(page, server):
    """A dossier is read weeks later, and "11:12" answers a question nobody
    was asking."""
    import re as regex
    _card_ready(page, server)
    stamps = page.eval_on_selector_all(
        ".job .meta > .when", "spans => spans.map((s) => s.textContent)")
    assert stamps, "no timestamp on the card at all"
    for stamp in stamps:
        assert regex.search(r"\d{1,2}[:.]\d{2}", stamp), stamp
        # A month name or a numeric date beside the clock, whatever the locale.
        assert regex.search(r"[A-Za-z]{3}|\d{1,2}[./-]\d{1,2}", stamp), stamp


def test_one_control_opens_the_dossier(page, server):
    """"Report" and "Read" were two controls onto one document, which reads
    as two documents. The page is a thing you can do with what is open, like
    the file — and like a source, whose title has always worked this way."""
    _card_ready(page, server)
    labels = page.eval_on_selector_all(
        ".job .links a, .job .links button",
        "els => els.map((e) => e.textContent)")
    assert labels == ["Read", "Sources (2)", "Everything (.zip)"], labels

    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    inside = page.eval_on_selector_all(
        ".job > .src-body .src-actions a, .job > .src-body .src-actions button",
        "els => els.map((e) => e.textContent)")
    assert "Open as page ↗" in inside, inside


def test_reading_in_place_is_not_narrower_than_the_page(page, server):
    """The measure inside the card was 276px against the standalone page's
    337 on a 375px screen — eight characters a line, which is most of why
    the page "reads better" and why two controls felt like two documents."""
    page.goto(f"{server}/jobs/{JOB_ID}/report")
    page.wait_for_selector(".report-body")
    on_the_page = page.evaluate(
        """() => [...document.querySelectorAll('.report-body p')]
                 .filter((p) => !p.className)[0].getBoundingClientRect().width""")

    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content p")
    in_the_card = page.evaluate(
        """() => document.querySelector('.job > .src-body .src-content p')
                 .getBoundingClientRect().width""")
    assert on_the_page - in_the_card < 25, (on_the_page, in_the_card)

    # And a source, which sits one level deeper again, inside the list indent.
    page.get_by_role("button", name="Sources").click()
    page.locator(".sources ol li button.src-title").first.click()
    page.wait_for_selector(".sources li > .src-body .src-content p")
    in_a_source = page.evaluate(
        """() => document.querySelector(
                   '.sources li > .src-body .src-content p')
                 .getBoundingClientRect().width""")
    assert on_the_page - in_a_source < 25, (on_the_page, in_a_source)


def test_the_header_icon_lines_up_with_the_heading(page, server):
    """Centred against the whole two-line block, it sat beside the gap
    between the title and the tagline and lined up with neither."""
    _card_ready(page, server)
    boxes = page.evaluate(
        """() => {
             const box = (sel) => {
               const r = document.querySelector(sel).getBoundingClientRect();
               return {top: r.top, height: r.height};
             };
             return {icon: box('header img'), title: box('header h1')};
           }""")
    assert abs(boxes["icon"]["top"] - boxes["title"]["top"]) <= 2, boxes
    assert abs(boxes["icon"]["height"] - boxes["title"]["height"]) <= 2, boxes


def test_a_local_copy_opens_where_it_is_cited(page, server):
    """Every archived citation ends with a "local copy" link, written into
    the Markdown itself so the dossier works as a plain file in the notes
    folder. Rendered in the app it was the one link that left the app: it
    navigated to the source's own page and took the shell with it, closing
    the dossier and everything else that was open."""
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    before = page.url

    link = page.locator(".job > .src-body .src-content a[data-reader]").first
    assert link.inner_text() == "local copy"
    link.click()
    page.wait_for_selector(".src-content li > .src-body .src-content")

    assert page.url == before                     # nothing navigated
    assert page.locator("#jobs").count() == 1     # still the shell
    assert page.locator(".job > .src-body").count() == 1   # dossier still open
    assert link.get_attribute("aria-expanded") == "true"
    inside = page.eval_on_selector_all(
        ".src-content li > .src-body .src-actions a, "
        ".src-content li > .src-body .src-actions button",
        "els => els.map((e) => e.textContent)")
    assert "Copy text" in inside and "Open as page ↗" in inside, inside


def test_a_local_copy_survives_a_poll_and_closes_again(page, server):
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content")
    link = page.locator(".job > .src-body .src-content a[data-reader]").first
    link.click()
    page.wait_for_selector(".src-content li > .src-body")

    page.evaluate("refreshJobs()")
    assert _poll(page, "() => document.querySelectorAll("
                       "'.src-content li > .src-body').length", None, 1), \
        "a copy opened from the dossier did not survive the poll"

    page.locator(".job > .src-body .src-content a[data-reader]").first.click()
    assert _poll(page, "() => document.querySelectorAll("
                       "'.src-content li > .src-body').length", None, 0)
    page.evaluate("refreshJobs()")
    page.wait_for_timeout(700)
    assert page.locator(".src-content li > .src-body").count() == 0
    assert page.locator(".job > .src-body").count() == 1   # dossier still open


def test_the_same_source_in_two_places_is_two_readers(page, server):
    """An archived page is reachable from the Sources list and from the
    citation inside the dossier. Remembering open readers by URL alone made
    opening one silently open the other, and closing either one arrange for
    the survivor to disappear at the next poll."""
    _card_ready(page, server)
    page.get_by_role("button", name="Read").click()
    page.wait_for_selector(".job > .src-body .src-content a[data-reader]")
    page.get_by_role("button", name="Sources").click()
    page.wait_for_selector(".sources ol li")

    page.locator(".sources ol li button.src-title").first.click()
    page.wait_for_selector(".sources ol li > .src-body")
    assert page.locator(".src-content li > .src-body").count() == 0

    page.locator(".job > .src-body .src-content a[data-reader]").first.click()
    page.wait_for_selector(".src-content li > .src-body")
    assert page.locator(".sources ol li > .src-body").count() == 1

    # Closing the one in the list leaves the cited one, poll and all.
    page.locator(".sources ol li button.src-title").first.click()
    page.evaluate("refreshJobs()")
    page.wait_for_timeout(800)
    assert page.locator(".sources ol li > .src-body").count() == 0
    assert page.locator(".src-content li > .src-body").count() == 1


def test_the_standalone_page_keeps_the_plain_link(page, server):
    """There is nothing to stay inside on a page of its own, and without
    scripting the relative link is the only thing that works at all."""
    page.goto(f"{server}/jobs/{JOB_ID}/report")
    page.wait_for_selector(".report-body")
    link = page.locator(".report-body a", has_text="local copy").first
    assert link.get_attribute("data-reader") is None
    assert link.get_attribute("href").startswith("sources/")


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
