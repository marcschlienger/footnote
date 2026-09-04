# Footnote — App Description

## Purpose

Footnote automates the "go find out" half of note-taking. You hand it a
question; it hands you back a **dossier**: a researched, cited Markdown
report filed into a folder your notes app watches, together with archived
copies of the evidence. It exists because deep-research tools live inside
chat interfaces — the answer scrolls away, the citations are ephemeral
links, and nothing lands in the system where you actually keep knowledge.
Footnote inverts that: the durable artifact in your own folder *is* the
product; the app UI is just a way to ask and check progress.

Design principles, inherited from Margin:

- **Files first.** The dossier folder is the source of truth. Notion is an
  optional mirror, job history is disposable bookkeeping, and deleting a job
  from the app never touches the files.
- **Single small app.** One `app.py` + one `pipeline.py`, JSON files instead
  of a database, one instance per person.
- **Every claim hangs off a citation.** The report body, the numbered source
  list, the used excerpts, the archived copies, and the failure list for
  sources that couldn't be archived are all parts of one evidentiary chain.

---

## Architecture

```
static/ (PWA)          app.py                          pipeline.py
┌────────────┐   ┌───────────────────┐   ┌────────────────────────────────┐
│ index.html │   │ token middleware  │   │ start_task_run()   Parallel    │
│ app.js ────┼──►│ /research, /jobs… │   │ fetch_task_result()  Task API  │
│ sw.js      │   │ JsonStore (jobs,  │──►│ scrape_sources()   Firecrawl   │
│ manifest   │   │   subscriptions)  │   │ write_report()     OUTPUT_DIR  │
└────────────┘   │ run_research() ───┼──►│ save_to_notion()   optional    │
                 │ notify_all() push │   └────────────────────────────────┘
                 └───────────────────┘
```

`app.py` owns HTTP, state, and orchestration; `pipeline.py` owns every
conversation with the outside world and all file writing. The pipeline
functions are pure with respect to app state (client, keys, and data in;
results out), which is what makes the test suite able to run without a
network: unit tests exercise them through `httpx.MockTransport`, and the
orchestrator is tested by substituting the pipeline functions entirely.

### The job lifecycle

A job is a dict in `data/jobs.json`:

```
queued ──► researching ──► archiving ──► saving ──► done
   │            │              │            │
   └────────────┴──────────────┴────────────┴─────► failed (error recorded)
```

- **queued** — accepted, background task not yet past validation.
- **researching** — a Parallel task run exists; its `run_id` is stored on
  the job *immediately after creation*, which is the linchpin of crash
  safety (below). Footnote long-polls the result endpoint.
- **archiving** — only entered when a Firecrawl key is configured and the
  result cites sources; scrapes up to `MAX_SOURCES` pages concurrently.
- **saving** — dossier being written; then the optional Notion mirror (its
  failure is logged, never fatal).
- **done / failed** — terminal. Push notifications fire for both. A done
  job also keeps its citation list (URL, title, the file its copy went to,
  and the archiving error if there wasn't one) — capped at
  `MAX_CITATIONS_KEPT`, and the only record of what could *not* be
  archived, which is why `/jobs/{id}/sources` reads it rather than
  re-parsing the report.

Every transition is persisted by rewriting `jobs.json` atomically
(`tempfile` + `os.replace`), so the file is never observed half-written —
relevant because `OUTPUT_DIR`-style sync clients may watch the data
directory's parent. History is capped at 200 jobs; the oldest *finished*
jobs are evicted first and running jobs are never evicted.

### Crash safety and resume

The research itself executes on Parallel's servers; Footnote is only a
waiting client. On startup, the lifespan hook scans `jobs.json` for jobs in
a non-terminal state and restarts `run_research` for each:

- a job **with** a stored `run_id` re-attaches — it skips creation and
  resumes long-polling the same run;
- a job **without** one (crashed between accept and create) starts the run
  fresh.

So a deploy, reboot, or crash mid-`ultra8x` costs nothing but the downtime.
The one deliberate gap: if the process dies *after* `start_task_run`
returns but *before* the `run_id` lands in `jobs.json` (a window of
milliseconds), the run is orphaned on Parallel's side and a duplicate is
created on resume — accepted as the cheapest correct-enough behavior.

### Deadlines

Each processor tier gets a polling deadline with headroom over Parallel's
documented upper latency (`core` → 40 min, …, `ultra8x` → 5 h; `-fast`
variants share their base tier's deadline). Hitting the deadline fails the
job with an explicit message. Transient network errors during polling are
retried after 10 s indefinitely — the deadline, not the retry count, bounds
the wait, because the run keeps progressing server-side regardless of our
connectivity.

---

## External API contracts

Verified against the providers' documentation in August 2026. These are
recorded here (and in CLAUDE.md) because both scaffolding-era guesses and
LLM memory get them wrong.

### Parallel Task API

**Create** — `POST https://api.parallel.ai/v1/tasks/runs`, header
`x-api-key: …`:

```json
{ "input": "<the question, verbatim>",
  "processor": "core",
  "task_spec": { "output_schema": {
      "type": "text",
      "description": "<REPORT_SPEC — the output instruction>" } } }
```

Returns `202` with `{"run_id": "trun_…", "status": "queued"}` immediately.
For a text output schema, the `description` *is* the prompt for the output
format — Footnote's `REPORT_SPEC` asks for a Markdown report that leads with
the answer, uses `##` sections, attributes claims in prose, and **omits any
bibliography** (the source list is built from structured citation data
instead, so it can carry excerpts and local-copy links).

**Result** — `GET /v1/tasks/runs/{run_id}/result?timeout=N`: blocks up to
`N` seconds (Footnote uses ≤ 120 s per request), returns `408` while the run
is still active — so waiting is one outstanding request at a time, not a
poll loop. On `200`:

```json
{ "run": { "status": "completed", … },
  "output": {
    "type": "text",
    "content": "…the report Markdown…",
    "basis": [ { "field": "output",
                 "reasoning": "…how the answer was assembled…",
                 "confidence": "high",
                 "citations": [ { "url": "…", "title": "…",
                                  "excerpts": ["…", …] } ] } ] } }
```

Footnote flattens the basis: citations from all fields, deduplicated by URL
in order of appearance (that order becomes the dossier's numbering), the
first non-empty `reasoning` becomes the Method note, the first `confidence`
goes into the frontmatter. A JSON-type output (shouldn't happen with a text
schema, but tolerated) is flattened to `## key` sections.

### Firecrawl

`POST https://api.firecrawl.dev/v2/scrape`, `Authorization: Bearer …`:

```json
{ "url": "…", "formats": ["markdown"], "onlyMainContent": true }
```

→ `{"success": true, "data": {"markdown": "…", "metadata": {"title": "…"}}}`.

Scraping is **best-effort by design**: up to `MAX_SOURCES` pages, 4
concurrent, 90 s timeout each; any failure (HTTP error, `success: false`,
empty extraction) demotes that source to the "could not be archived" list
with its reason. A scrape failure can never fail the job — the dossier's
claims and links don't depend on it.

### Notion (optional mirror)

`POST https://api.notion.com/v1/pages` with `Notion-Version: 2022-06-28`,
into `NOTION_DATABASE_ID`. The report body is converted paragraph-wise
(headings preserved, blocks chunked under Notion's ~2000-char rich-text
limit, capped at 100 blocks) plus a linked source list. Mirror failure logs
and moves on — the file already exists.

### Web Push

`pywebpush` with VAPID keys; the blocking `webpush()` call runs in a thread
(`asyncio.to_thread`) per subscription. Subscriptions are keyed by a UUIDv5
of their endpoint (idempotent re-subscribe); a `404`/`410` from the push
service deletes the subscription. The payload carries `title`, `body`, and a
`url` (`/jobs/{id}/report`), which the service worker opens on tap.

---

## The dossier writer

`pipeline.write_report` is deliberately boring, and the details are the
point:

- **Slugs** (`slug_for`) keep the question human-readable: NFKC-normalize,
  strip filesystem-unsafe characters (`<>:"/\|?*` and control chars),
  collapse whitespace, cut at 64 chars on a word boundary. No lowercasing,
  no hyphen-mangling — the filename should read like the question, because
  in Obsidian the filename *is* the title.
- **Folders** are `YYYY-MM-DD slug`; existing names get ` (2)`, ` (3)` …
  Nothing is ever overwritten.
- **Source copies** are written first, so the report can link them:
  `sources/NN title-slug.md`, each with `source:`/`title:`/`retrieved:`
  frontmatter. Only successful scrapes get files; the numbering in filenames
  matches the citation numbering for the sources that have copies. The
  returned `WrittenReport` carries the URL → file-name mapping, so the
  caller records which citation got which copy without re-deriving names.
- **Relative links** use the `[text](<path with spaces>)` angle-bracket form
  so paths with spaces survive strict Markdown parsers; they resolve both in
  a notes app and in Footnote's own report view (which serves
  `/jobs/{id}/sources/{file}` for exactly this reason).
- **Frontmatter** is quoted/escaped YAML (`question`, `date`, `processor`,
  optional `confidence`, `sources`, `app: Footnote`) — enough for Obsidian
  Dataview queries like "all high-confidence dossiers this month".

## Report rendering

`GET /jobs/{id}/report` strips the frontmatter (`pipeline.split_front_matter`,
the reader for what `write_report` emits) and renders the Markdown
server-side with `python-markdown` (`tables`, `fenced_code`) into the shared
paper stylesheet; if the `markdown` package is missing the raw text is shown
in a `<pre>` — the dependency is optional, like Margin's. No client-side
rendering: the page must be readable from a push-notification tap on a phone
that has never loaded the PWA.

**The rendered HTML is rebuilt, not filtered.** Everything on these pages
started on the open web — an archived page, or a report written from archived
pages — and python-markdown passes raw HTML straight through. So the output
goes through `_Sanitizer`, an `HTMLParser` that re-emits only allowlisted
tags and attributes: unknown tags become text, `script`/`style`/`iframe` lose
their content, and `href`/`src` survive only for relative URLs and
`http`/`https`/`mailto`. Constructing the output from parsed tokens rather
than stripping patterns out of a string is what makes it safe to render a
scraped page on the same origin as the session cookie.

## Reading the sources

The dossier is meant to be readable from the phone that asked the question,
which may not sync the notes folder at all. Three endpoints do that:

- `GET /jobs/{id}/sources` lists every source, archived or not. Two
  sources of truth are merged: the citation list recorded on the job carries
  what *failed* to archive and why, and the files actually in `sources/`
  decide what is readable — so a copy deleted in the notes folder degrades
  to a plain citation instead of a broken link. A dossier written before
  jobs recorded citations is described by its files' frontmatter alone.
- `GET /jobs/{id}/sources/{file}` renders one archived copy in the same
  paper style, with the frontmatter turned into a header (title, retrieval
  date, link to the original). `?raw=1` returns the Markdown file itself as
  a download. File names are resolved by name only — never a path from the
  client — under the job's own folder.
- `GET /jobs/{id}/bundle.zip` zips the report and its sources in memory,
  laid out exactly as they sit on disk, so unzipping reproduces the folder.
  Bounded by `MAX_SOURCES` and Markdown-only, which is why in-memory is fine.

## What the client is not told

The dossier's location on the server is not part of any response body.
`report_path` and the citation list are stripped from the job JSON (which
carries `report_name` instead), `/health` reports whether the output folder
is *writable* rather than where it is, and `_scrub` replaces `OUTPUT_DIR`,
`DATA_DIR` and the home directory in error text before it is stored on the
job — an `OSError` from a failed write would otherwise put the full path in
the UI. Clients address research by job id throughout.

---

## The PWA

Same paper-and-ink family as Margin's queue (cream paper, slate text, red
rule, blue ink for the app's own marks; serif for questions and headings).
Behavior notes:

- **Polling, not websockets**: `/jobs` every 5 s while anything is active,
  every 60 s otherwise. Research takes minutes — realtime infrastructure
  would be all cost, no benefit.
- **The depth picker** maps friendly labels to processors ("Quick look" →
  `base`, "Standard" → `core`, "Deep" → `pro`, "Exhaustive" → `ultra`,
  "Heroic" → `ultra4x`); the API accepts the full processor list for
  anything scripted.
- **Configuration surface**: on load the app calls `/health` and shows a
  red flash if `PARALLEL_API_KEY` is missing or the output folder is not
  writable — the two failures worth catching before the first question.
- **Source panels** expand in place on a finished job: the list comes from
  `/jobs/{id}/sources`, cached per job and re-expanded after every poll, so
  a five-second refresh doesn't collapse what you are reading. Each entry
  links to the rendered copy, its `.md`, and the original page; the ones
  that could not be archived say why.
- **Service worker**: one policy per kind of response, because they age
  differently. The shell (`/`, CSS, JS, manifest, icons) is network-first
  with a cache fallback, so an upgrade arrives at once. A written report and
  its archived sources are cache-first with a background refresh: those files
  never change once written, and the refresh exists only so a server-side
  rendering change reaches the next read. The job list falls back to the last
  list seen *only* when the network is gone — stale status is worse than an
  error while online, but offline the alternative is a blank page — and that
  fallback carries an `X-Footnote-Cached` header so the UI can say what it
  is. `?raw=1` downloads, `report.md` and the bundle zip are never cached;
  they are files to save, not pages to read. Only successful responses are
  stored, so an unauthorized page cannot poison the cache. Push and
  notification-click handlers complete the notification loop.
- **Offline**: the app opens, shows the last job list it saw under a notice
  saying so, and any dossier already read stays readable — report, source
  pages and the source index all come from the cache. What is *not* cached
  fails visibly rather than pretending. Submitting a question fails, and
  queued offline capture (à la background sync) is deliberately out of
  scope — a research request needs the server anyway.

---

## Security model

Threat model: a personal server on a private network (LAN or tailnet),
optionally hardened one notch.

- `FOOTNOTE_TOKEN` gates everything except `/health` and the PWA shell
  assets (icons/manifest/service worker must load for browser chrome and
  home-screen installs; none are sensitive). Comparison is
  `secrets.compare_digest`; the cookie is `HttpOnly` + `SameSite=Strict`,
  which also closes the CSRF window an open LAN server has.
- Job IDs are 12 hex chars of UUID4 — unguessable enough for the threat
  model, and the report/source endpoints resolve only through the job
  store (no filesystem paths from the client; source filenames are checked
  against `/`, `\` and leading dots).
- Rendered Markdown is rebuilt from an allowlist, so an archived page
  cannot run script on the origin that holds the session cookie.
- Server filesystem paths stay server-side (see *What the client is not
  told*) — not secrecy so much as keeping the API's vocabulary to job ids.
- API keys live in `.env` / systemd env files, never in the repo; the
  frontend never sees them.
- What the token does *not* provide: per-user separation (that's the
  one-instance-per-person model), rate limiting, or audit logging.

---

## Files

| Path | Purpose |
|---|---|
| `app.py` | FastAPI server: endpoints, auth middleware, JsonStore, orchestrator, push |
| `pipeline.py` | Parallel/Firecrawl/Notion clients, dossier writer |
| `static/index.html` `app.js` `style.css` | PWA shell, logic, paper stylesheet (also styles the report view) |
| `static/service-worker.js` | shell + dossier caches, offline reading, push/notification handlers |
| `static/manifest.json` | PWA manifest |
| `static/icon.svg` | icon master; PNGs generated by `deploy/gen_icons.py` |
| `data/jobs.json` | job store (created at runtime; gitignored) |
| `data/subscriptions.json` | push subscriptions (runtime; gitignored) |
| `deploy/install.sh` | Ubuntu: shared platform into `/opt/footnote` |
| `deploy/add-instance.sh` | Ubuntu: per-person env file + service instance |
| `deploy/footnote@.service` | systemd template unit |
| `deploy/gen_icons.py` | re-render icon PNGs from the SVG (Playwright) |
| `tests/` | pytest suite, fully offline |

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` / `uvicorn` | HTTP server |
| `httpx` | async client for all three external APIs |
| `pydantic` | request validation |
| `python-dotenv` | `.env` loading (env vars win — enables the shared/per-user key layering) |
| `pywebpush` | Web Push with VAPID (optional at runtime) |
| `markdown` | server-side report rendering (optional at runtime) |
| `playwright` + `pillow` | dev only: icon generation |

## Running the Server

**Start manually** (any platform):

```bash
.venv/bin/python app.py --host 0.0.0.0 --port 8010 [--output-dir DIR]
```

**Ubuntu — systemd template** (installed by `deploy/install.sh` +
`deploy/add-instance.sh <user> <port>`):

```bash
systemctl status footnote@<user>
journalctl -u footnote@<user> -f   # logs
```

**macOS — Launch Agent (auto-starts at login):** save the plist below as
`~/Library/LaunchAgents/<label>.plist`, where `<label>` is the reverse-DNS
label it declares. Substitute a label of your own and the location of your
checkout — the file name follows the label, and every path in the plist sits
inside the checkout:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.footnote</string>
  <key>ProgramArguments</key>
    <array><string>/path/to/footnote/start.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key>
    <string>/path/to/footnote/server.log</string>
  <key>StandardErrorPath</key>
    <string>/path/to/footnote/server.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.example.footnote.plist
tail -f server.log               # logs, in the app directory
```

`start.sh` execs the venv Python directly (sourcing `activate` trips the
launchd sandbox on some macOS configurations) and rotates `server.log` at
~5 MB.
