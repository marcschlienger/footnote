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
- **saving** — the dossier is written, then the whole outcome is recorded in
  one store write: status, summary, citation records, counts, `finished_at`.
  That write is the job's durable checkpoint. Everything after it — the
  optional Notion mirror, the push notification — is post-processing that
  updates `notion_url` at most and cannot leave a job looking unfinished.
  Both are skipped if the job has been deleted by the time they start, and
  `_update_job` is a no-op for a job that is gone, so nothing is written back
  to a removed record. Neither is *cancelled* mid-call, though: a request
  already in flight finishes, so deletion suppresses the steps that have not
  begun and not the one that has. The
  mirror's failures are logged and never fatal, *any* failure and not only a
  PipelineError, because the dossier is already on disk by then. The flip
  side is worth stating: a crash *after* the checkpoint skips both
  permanently, because a job that is already `done` is not resumed — the
  dossier is complete, but the Notion copy and the notification are not
  retried.
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


**A dossier is adopted, never rewritten.** `report_path` is stored as soon as
the file exists — before the outcome write — so a restart in that interval
finds the folder and takes it: the Parallel result is fetched again (the run
is complete server-side, so this costs nothing), the archived copies are read
back off the folder to rebuild which citation got which file, and the job
finishes with real counts, a real summary, Notion and push. It does **not**
re-scrape, and it does not write a second folder.

What that recovery cannot restore is why a source failed to archive: those
reasons lived only in the `SourceCopy` list of the run that died, so an
adopted dossier shows unarchived citations without a reason. The remaining
window is narrow and honest: a crash between `write_report` returning and the
`report_path` store write leaves an orphan folder, and the restarted job
writes its own. The dossier's frontmatter carries `job:` so such an orphan can
be recognised by hand.

### Deadlines

Each processor tier gets a polling deadline with headroom over Parallel's
documented upper latency (`core` → 40 min, …, `ultra8x` → 5 h; `-fast`
variants share their base tier's deadline). Hitting the deadline fails the
job with an explicit message. Transient network errors during polling are
retried after 10 s indefinitely — the deadline, not the retry count, bounds
the wait, because the run keeps progressing server-side regardless of our
connectivity.

---

The deadline is enforced on the calls, not only between them: the HTTP
client's own timeout and the long-poll window Parallel is asked for are both
cut to what remains, and every retry sleep is measured from *after* the
request rather than before it. The request is also wrapped in
`asyncio.wait_for`, because httpx's timeouts bound individual operations —
connect, and inactivity between reads — not the wall clock of the whole call,
and a slow response that keeps trickling would otherwise outlast them. A
bound the caller passes is a bound.

### Nothing arrives trusted

Both providers' payloads cross a network as someone else's JSON, the state
files and the dossier folder are written by other software, and the request
body is whatever a client sent. These were fixed one at a time for several
rounds — a title, then an error string, then a stored value, then a hostname
— which is the signature of patching instances instead of a class. So they
are enumerated, and each has one rule.

| Boundary | Rule |
|---|---|
| Parallel: create-run, result, error bodies | `clean_text` on every scalar, `_as_list` on every collection; a wrong-shaped body is retried, not crashed on |
| Firecrawl: markdown, title, **error** | same; the error path matters as much as the success path, since both reach the dossier |
| Notion: response | checked as an object, URL through `clean_text` and `is_http_url` before it is stored |
| HTTP request bodies | validated by type and content (`_question_problem`, `_valid_subscription`); a 422 is rendered with `ensure_ascii` and **without** the echoed input, which is unbounded and attacker-supplied |
| Push endpoints | a policy of their own (`is_push_endpoint`), because the server requests this address on a client's say-so: HTTPS, no credentials, and no loopback, private, link-local or reserved literal. A *name* that resolves inward is not caught here — the library resolves it — which is the residual risk |
| Query and path parameters | `limit` is a bounded int; a source name is matched against the files on disk; the token is compared as **bytes**, since `compare_digest` raises `TypeError` on a non-ASCII string |
| `jobs.json`, `subscriptions.json` | `clean_json` on load *and* on save; not valid UTF-8, not JSON, or not an object of records → quarantined under a fresh name. **Cleaning is recorded, not just done**: a record it had to change is not a valid record, and an active one is failed rather than resumed — eight surrogates become eight replacement characters, which would otherwise pass every later check and be sent to Parallel again |
| Dossier files (report, sources) | read with `errors="replace"`; frontmatter values through `clean_text`. Written through it too: `clean_text` drops C0 controls apart from tab, newline and return, so a provider's NUL cannot make a Markdown file look binary to a notes app |
| Filenames written | `slug_for` budgets UTF-8 **bytes**, and every file is written through `scrub_surrogates` |
| Environment | integers validated with a minimum, `DEFAULT_PROCESSOR` against the real list, both at startup |
| systemd env files (deploy) | quoting handled and only absolute paths accepted, rather than sourcing a root-owned file or stripping a literal prefix |

Two helpers carry all of it. `clean_text` is the ingress rule: not a string
becomes the fallback or its `str()`, and anything UTF-8 cannot represent is
replaced. `clean_json` is the structural one, applied where data is *stored*
rather than field by field — a list of field names is exactly the thing that
goes stale, and it did: the first two attempts at this enumerated fields and
missed the ones added since. Keys become strings, non-finite floats become
null (`NaN` is not JSON and does not survive a reload), and anything
unrecognised becomes its `str()`, so `json.dump` cannot refuse what the store
holds.

The one thing that rule costs: a path that is not valid UTF-8 — possible on
Linux, where filenames are bytes — is scrubbed on its way into the store, and
that job's report then reads as unavailable. It could not have been stored as
JSON at all, so the choice was between a degraded record and no store.

A test drives hostile values through every row of that table in one go and
then asserts the store still saves and `/jobs` still answers. Each protection
was checked by removing it and watching that test fail.

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

Scraping is **best-effort by design**: up to `MAX_SOURCES` pages, 90 s
timeout each; any failure (HTTP error, `success: false`, empty extraction)
demotes that source to the "could not be archived" list with its reason. A
scrape failure can never fail the job — the dossier's claims and links don't
depend on it.

**Pacing is built for the free plan**, whose published limits are 10
`/scrape` requests a minute and 2 concurrent browsers; `FIRECRAWL_RATE_LIMIT`
and `FIRECRAWL_CONCURRENCY` default to exactly those, so an unconfigured
install stays inside them and a paid key just raises two numbers (a rate
limit of 0 turns pacing off). `_Pacer` admits a request only once the oldest
start in the 60-second window has aged out, holding its lock across the
sleep so waiters take turns instead of waking together and bursting.

The budget belongs to **the API key, not the job** — within a process: the
app builds one `ScrapeLimiter` in `lifespan` and every scrape shares it. A limiter per job
would be no limiter at all — two jobs archiving at once would double both
numbers, and a restart resumes every unfinished job simultaneously. The
exhausted-credits flag lives there too, so one job's 402 answers for the jobs
queued behind it; it expires after `CREDIT_COOLDOWN_S` so a top-up or the
monthly reset is noticed without a restart.

**The limiter does not cross process boundaries**, and the Ubuntu layout runs
one service per person against API keys that may be shared in
`/opt/footnote/.env`. Two instances archiving at once would then present the
key with twice the configured rate and twice the browsers. Nothing in a
single process can see its siblings, so this is a deployment decision: give
each instance its own Firecrawl key, or divide the limits between them in
each `/etc/footnote/<user>.env` (`FIRECRAWL_RATE_LIMIT=5` apiece for two
instances on one free key). A cross-process lock would be the wrong shape for
an app whose whole premise is one small process per person.

Archiving cannot fail a dossier, and that is enforced rather than intended:
every path out of `scrape_sources` produces a `SourceCopy`, including a
response body that is not JSON at all (an HTML 502 from a proxy) and any
exception a worker manages to raise.

Failures are then sorted by whether asking again could help:

| response | what Footnote does |
|---|---|
| 408, 429, 500, 502, 503, 504 | retry, up to `MAX_ATTEMPTS`, honouring `Retry-After` when the server sends it and otherwise backing off exponentially with jitter (capped at `MAX_BACKOFF_S`) |
| 402 | stop the batch. Credits are gone and pay-as-you-go is unavailable on the free plan, so every further request would fail identically; the remaining sources are marked with the same reason and the job summary says so once. Requests already in flight when it lands still finish, so up to `concurrency` are spent — not one |
| anything else (403 bot wall, paywall, empty extraction) | final — record the reason and move on |

Under the free-plan defaults a 12-source dossier archives in about a minute
rather than a few seconds, so `scrape_sources` reports `archived k/n` into
the job's progress as copies land.

### Notion (optional mirror)

`POST https://api.notion.com/v1/pages` with `Notion-Version: 2022-06-28`,
into `NOTION_DATABASE_ID`. The report body is converted paragraph-wise
(headings preserved, blocks chunked under Notion's ~2000-char rich-text
limit, capped at 100 blocks) plus a linked source list. Mirror failure logs
and moves on — the file already exists.

Two limits shape what is sent. Notion accepts at most
`NOTION_MAX_BLOCKS` blocks per page-create, so the report body is truncated
to leave room for the source list rather than letting a long report consume
the whole allowance — the sources are the point of a dossier. And a citation
whose URL is not a safe link is left out of the mirror entirely: Notion
rejects the whole page over one bad link, and those citations are already
recorded as text in the dossier itself.

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
  Nothing is ever overwritten. The name is claimed with `mkdir` rather than
  chosen after an existence check, so two jobs finishing the same question at
  the same moment get two folders instead of one `FileExistsError` on top of
  research already paid for.
- **Source copies** are written first, so the report can link them:
  `sources/NN title-slug.md`, each with `source:`/`title:`/`retrieved:`
  frontmatter. Only successful scrapes get files, but the number in each name
  is the source's **citation** number, not its position among the successes —
  if citation 1 is paywalled and citation 2 archives, the file is `02 …`, so
  the folder and the report's numbered list always mean the same thing. The
  returned `WrittenReport` carries the URL → file-name mapping, so the
  caller records which citation got which copy without re-deriving names.
- **Relative links** use the `[text](<path with spaces>)` angle-bracket form
  so paths with spaces survive strict Markdown parsers; they resolve both in
  a notes app and in Footnote's own report view (which serves
  `/jobs/{id}/sources/{file}` for exactly this reason).
- **A run id is checked before it is stored.** A create response that is not
  an object, or whose `run_id` is not a non-empty string, fails the job
  rather than being interpolated into later URLs — and it is not retried,
  because the POST may well have created a run that a second attempt would
  pay for again while the first goes unrecorded.
- **Only web links are written as links**, and the source index applies the
  same rule as the report writer (`is_safe_url(relative_ok=False)`): absolute
  http, https and mailto, never a relative-looking string that would resolve
  against Footnote's own origin. A citation URL outside that set is listed as
  text in a code span instead: the dossier
  is a portable Markdown file, and the next app to open it may follow a link
  without asking. The span is fenced with a backtick run longer than any
  inside it, so a URL containing backticks cannot close it and continue in
  Markdown. Only http(s) citations are handed to Firecrawl at all — a
  `mailto:` is a fine citation and a pointless scrape.
- **Every line of the method note carries its own `>`**; reasoning arrives as
  prose and a paragraph break would otherwise drop the rest of it back into
  the report body, where a `##` becomes a heading.
- **Escaping** assumes hostile text, because most of it came off the web:
  every frontmatter value (the question, a source URL, a title, the
  confidence) is collapsed to one line and quoted (a question typed
  into a textarea can contain newlines, which would otherwise end the scalar
  and turn the rest into bogus keys), link labels have their brackets
  escaped, and a URL with spaces or parentheses takes the angle-bracket form.
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
- **The depth picker's five labels are curated**, not the server's processor
  list — friendly names are the point, and the API takes the full list for
  anything scripted. Which of them starts selected comes from `/processors`,
  so the picker agrees with the server's `DEFAULT_PROCESSOR`.
- **The depth picker** maps friendly labels to processors ("Quick look" →
  `base`, "Standard" → `core`, "Deep" → `pro`, "Exhaustive" → `ultra`,
  "Heroic" → `ultra4x`); the API accepts the full processor list for
  anything scripted.
- **Configuration surface**: on load the app calls `/health` and shows a
  red flash if `PARALLEL_API_KEY` is missing or the output folder is not
  writable — the two failures worth catching before the first question.
- **Source panels** expand in place on a finished job: the list comes from
  `/jobs/{id}/sources`, cached per job and re-expanded after every poll, so
  a five-second refresh doesn't collapse what you are reading. That cache
  holds for a minute — it exists to stop the poll refetching, not to outlive
  the folder it describes, since a copy can be moved in the notes app while
  the page is open — and falls back to the stale copy when offline. Each entry
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
- **Cache writes are awaited.** A `put` still in flight when the response
  settles can be cut short — the browser is free to stop the worker at that
  point — which would make caching of the shell, the job list and the source
  indexes quietly unreliable.
- **Deleting a job clears everything cached under it.** The PWA posts a
  `forget-job` message the moment a delete succeeds, since waiting for
  someone to ask for a deleted job again could mean waiting forever. A 404 or
  410 for any
  URL beneath `/jobs/{id}/` drops every entry with that prefix, so an offline
  visit cannot resurrect a deleted dossier one page at a time.
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
- **One containment rule, applied everywhere.** The report and every source
  copy must be a regular file that resolves inside `OUTPUT_DIR`, checked when
  serving, when zipping, when listing — and when a restart decides whether to
  adopt a dossier, since adopting something the endpoints would refuse marks
  a job done whose every report URL then answers 404. The dossier lives in a synced
  notes folder that people and sync clients write to, so a name Footnote
  recorded can since have become a link to anything the service account can
  read; the index lists only what the read endpoint would actually serve, so
  the two can never disagree.
- A push endpoint must be `http(s)`, which is stricter than the link policy
  the dossier uses: `mailto:` is a legitimate citation and not somewhere a
  notification can be delivered.
- Rendered Markdown is rebuilt from an allowlist, so an archived page
  cannot run script on the origin that holds the session cookie.
- Server filesystem paths stay server-side (see *What the client is not
  told*) — not secrecy so much as keeping the API's vocabulary to job ids.
- **No cross-origin access by default.** The PWA is same-origin and the
  Shortcut and curl are not browsers, so nothing Footnote ships needs CORS —
  and a wildcard would let any page you happen to be visiting start research
  on a reachable instance and spend the Parallel key, which with
  `FOOTNOTE_TOKEN` unset is the default install. `FOOTNOTE_CORS_ORIGINS`
  names origins explicitly when a browser client of your own needs one.
- Push endpoints are subject to `is_push_endpoint`, not the general link
  policy: a subscription names an address the *server* then POSTs to, so
  accepting `http://127.0.0.1:8010/internal` made blind server-side request
  forgery a feature of registering a device. Calls carry a timeout, keys are
  verified as actual P-256 points rather than 65 bytes beginning 0x04, and
  both the number of devices and the endpoint length are bounded.
- Push is isolated from job outcome: `notify_all` swallows everything, since
  it is called from inside `run_research`'s `try` and a malformed
  subscription would otherwise rewrite a finished job as failed. Devices are
  notified `PUSH_CONCURRENCY` at a time, so one unreachable endpoint does not
  hold up the rest; subscriptions are checked when registered, and any stored
  before that check are dropped at startup rather than failing once per job
  forever.
- The token cookie is marked `Secure` when the request arrived over TLS, and
  not when it did not — setting it unconditionally would stop the cookie
  being sent at all on a plain-HTTP LAN.
- A state file that is not valid JSON — or valid JSON that is not an object
  of records — is renamed aside (under a name that cannot collide) rather
  than silently treated as empty, so the history can be looked at instead of
  being overwritten by the next save. A single damaged record is dropped with
  a note instead of costing the rest of the history, and what survives is
  normalized on load. Two kinds of field, two treatments: what is only
  rendered or sorted by is coerced to text; what the app *acts on* is
  validated, because `null` stringified into `"None"` is a corrupt record
  made to look runnable. An active record whose question or processor would
  not pass submission is failed rather than resumed, by the same rule
  submission applies — a question refused at the front door must not get in
  through a resume and spend the same quota. Store keys are the public job
  ids and go straight into URLs, so a key that is not one is rekeyed (the
  record is kept; only its address changes), and a `notion_url` that is not a
  web link is dropped, since the PWA assigns it to an anchor.
- **Response headers back the sanitizer up.** Every response carries a CSP
  (`script-src 'self'`, `connect-src 'self'`, `frame-ancestors 'none'`,
  `base-uri 'none'`), `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. The sanitizer and the policy agree on
  images: `img-src` has no `http:`, so the sanitizer drops `http:` images
  too — a picture the policy is going to block should be absent rather than
  broken — and `data:` images are kept except SVG, which is a script
  container. The middleware is declared last so it wraps
  the token check too — the 401 page and the token redirect are responses
  like any other. Styles keep `'unsafe-inline'` (the no-markdown fallback
  carries a style attribute) and images still load from the archived pages.
- **`?token=` is bounced.** Once the cookie is set, a browser navigation is
  redirected to the same URL without the token, so it stops appearing in the
  address bar, in later history entries and in cache keys. The *first*
  request still carries it, so it appears once in the access log — the
  redirect limits the exposure, it does not erase it. Only navigations: an API client sending
  `?token=` gets its answer, not a redirect it might not follow.
- **A changed token does not reach a device that is offline.** The service
  worker drops both caches as soon as a request comes back 401, so revoking
  the token clears the dossiers it cached on the next online visit — but a
  device that never comes back online keeps what it already read until its
  site data is cleared. That is a property of browser storage, not something
  a server can revoke; treat an installed PWA as holding the dossiers it has
  opened.
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
