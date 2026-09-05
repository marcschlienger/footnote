# Footnote

> † the proof that didn't fit in the margin.

Fermat jotted the original read-it-later note — a marvelous claim, proof
deferred for lack of space — and it took the world 358 years to chase the
evidence down. [Margin](https://codeberg.org/blutlauge/margin) is where
claims like that get saved. Footnote is the app that goes and chases.

Ask it a question and it runs a real web-research task in the background
(minutes, not seconds), then files a **cited Markdown dossier** into a folder
you control: the report itself, a numbered source list with the excerpts the
research actually used, and — optionally — a local Markdown copy of every
cited page, so the evidence is still there when links rot. Point
`OUTPUT_DIR` at a synced folder (iCloud → Obsidian, Nextcloud, Syncthing)
and research lands in your notes; enable Web Push and your phone buzzes when
it's done.

The dossier is also readable where you asked for it: the PWA lists every
source behind a finished report, renders the archived copies in the browser,
and hands you the whole folder as a zip — useful from a phone that doesn't
sync the notes folder at all.

It is a single small FastAPI app designed for personal use, in the same shape
as Margin: run it on a Mac or an Ubuntu server, talk to it from the built-in
PWA, an iOS Shortcut, or any HTTP client.

## How it works

```
 PWA / iOS Shortcut / curl
        │  POST /research {question, processor}
        ▼
 ┌──────────────────── Footnote (FastAPI, port 8010) ────────────────────┐
 │                                                                       │
 │  question ─► Parallel.ai Task API ──► report text + citation basis    │
 │              (deep web research, ~1 min to hours by processor)        │
 │                                                                       │
 │  citations ─► Firecrawl /v2/scrape ──► local Markdown source copies   │
 │               (optional, best-effort)                                 │
 │                                                                       │
 │  everything ─► OUTPUT_DIR/"2026-08-04 your question…"/                │
 │                ├── your question….md          ← the dossier           │
 │                └── sources/01 Some page.md    ← archived copies       │
 │                                                                       │
 │  done ─► Web Push notification ─► tap → rendered report               │
 └───────────────────────────────────────────────────────────────────────┘
```

The heavy lifting is [Parallel.ai](https://parallel.ai)'s Task API — a
deep-research engine that searches, reads, and cross-checks the web
server-side and returns a report together with its **basis**: citations
(URL, title, excerpts), reasoning, and a calibrated confidence rating.
Footnote turns that basis into the dossier's source apparatus and (via
[Firecrawl](https://firecrawl.dev)) into local copies. Architecture and
design decisions are documented in [description.md](description.md).

Jobs are persistent: state lives in `data/jobs.json`, and because the
research run executes on Parallel's side, a Footnote restart doesn't lose
work — on boot it re-attaches to every unfinished run by its `run_id` and
carries on. A dossier that was already written is adopted rather than
repeated, so a restart mid-save costs neither Firecrawl credits nor a
duplicate folder.

## Research depth: processors

`processor` selects Parallel's effort tier per question — deeper tiers read
more sources, reason longer, and cost more. Footnote accepts all of them and
defaults to `DEFAULT_PROCESSOR` (shipping as `core`).

| Processor | Typical latency | Good for |
|---|---|---|
| `lite` | 10 s – 1 min | a fact with a source |
| `base` | 15 s – 2 min | simple questions, quick overviews |
| `core` | 1 – 5 min | **the everyday default** — solid multi-source answers |
| `core2x` | 1 – 10 min | core with more headroom |
| `pro` | 2 – 10 min | exploratory research, harder questions |
| `ultra` | 5 – 25 min | difficult deep research |
| `ultra2x` / `ultra4x` / `ultra8x` | up to hours | the most demanding dossiers |

Each tier also has a `-fast` variant (same capability, lower latency —
`core-fast`, `ultra-fast`, …). The PWA exposes a sensible subset
(base / core / pro / ultra / ultra4x); the API takes any of them. Footnote
gives each tier a generous polling deadline (40 min for `core`, up to 5 h
for `ultra8x`) and fails the job with a clear error if a run exceeds it.

Parallel (and Firecrawl) are paid APIs with free starter credits — a `core`
run costs cents, `ultra` tiers noticeably more. Mind your usage dashboard the
first few days.

## API

All endpoints speak JSON; errors use HTTP status codes with a `detail`
message. If `FOOTNOTE_TOKEN` is set, every endpoint except `GET /health` and
the PWA shell assets requires the token — see
[Authentication](#authentication-optional).

### `POST /research` — start a job

```json
{ "question": "What is the current evidence on creatine and cognition?",
  "processor": "core" }
```

`question`: 8–4000 characters — a question, a claim to verify, a topic
brief. `processor` is optional (server default applies). Returns
immediately:

```json
{ "status": "started", "job_id": "df31fcbed547",
  "message": "Research started on the core processor" }
```

The job runs unattended from here; close the browser, sleep the laptop that
sent the request — the dossier appears in `OUTPUT_DIR` when research
finishes.

### `GET /research/{job_id}` — job status

```json
{ "id": "df31fcbed547",
  "question": "What is the current evidence on creatine and cognition?",
  "processor": "core", "status": "researching",
  "progress": "Researching on Parallel (core) — this can take a while…",
  "created_at": "2026-08-04T09:12:44Z",
  "notion_url": "", "error": "" }
```

`status` walks `queued → researching → archiving → saving → done` (or
`failed`, with `error` set). Once `done`, the response also carries
`report_name` (the dossier's file name), `report_available` (whether that
file is still where it was filed — the folder is yours to reorganise, and a
link to a dossier you moved is worse than none), `sources_cited`,
`sources_archived`, and `finished_at`.

Where the dossier sits on the server is deliberately not in the response.
Clients address research by job id; the filesystem layout is the server's
business, and error messages have server paths scrubbed out of them.

### `GET /jobs` — recent jobs

`{"jobs": […most recent first…], "active": 1}` — the PWA's poll target.
`?limit=N` caps the list (default 50). History keeps the last 200 jobs;
finished ones age out first, running ones never.

### `DELETE /jobs/{job_id}`

Removes a *finished* job from history (409 while it is running). The dossier
files in `OUTPUT_DIR` are never touched — job history is bookkeeping, the
folder is the record.

### `GET /jobs/{job_id}/report` — read the dossier

Server-rendered HTML view of the report (paper-styled) — this is what the
push notification opens. `GET /jobs/{job_id}/report.md` serves the raw
Markdown.

Markdown is rendered through an allowlist: the report and the archived
pages come off the open web, so scripts, event handlers and `javascript:`
URLs are dropped rather than passed through into a page that holds your
session cookie.

### `GET /jobs/{job_id}/sources` — the evidence, listed

Every source behind the dossier, whether or not it could be archived:

```json
{ "job_id": "df31fcbed547", "cited": 9, "archived": 7,
  "bundle_url": "/jobs/df31fcbed547/bundle.zip",
  "sources": [
    { "n": 1, "title": "Creatine and cognitive performance — meta-analysis",
      "url": "https://…", "file": "01 Creatine and cognitive….md",
      "archived": true, "bytes": 48210, "note": "",
      "read_url": "/jobs/df31fcbed547/sources/01%20Creatine%20and%20…",
      "download_url": "/jobs/df31fcbed547/sources/01%20Creatine%20and%20…?raw=1" },
    { "n": 2, "title": "Journal of Nutrition", "url": "https://…",
      "file": "", "archived": false, "bytes": 0, "note": "HTTP 403" } ]}
```

The archived copies on disk decide what is readable, so a file you moved or
deleted in your notes folder degrades to a plain citation rather than a
broken link. `cited` is what the dossier cited; `sources` lists them, and is
shorter for a question that drew more than 100 citations.

### `GET /jobs/{job_id}/sources/{file}` — read one source

The archived page, rendered in the same paper style, with a link back to the
report and out to the original. This is also where the report's relative
"local copy" links land in the browser, exactly as they do in your notes app.
Add `?raw=1` for the Markdown file itself, frontmatter and all.

### `GET /jobs/{job_id}/bundle.zip` — take the lot

The report and every archived source in one zip, folded exactly as they sit
in your notes folder:

```
2026-08-04 what is the current evidence on creatine and cognition/
├── what is the current evidence on creatine and cognition.md
└── sources/01 Creatine and cognitive performance — meta-analysis.md
```

### `POST /subscribe` — register for push

Body: a standard
[PushSubscription JSON](https://developer.mozilla.org/en-US/docs/Web/API/PushSubscription/toJSON)
(the PWA does this for you). Subscriptions are stored server-side in
`data/subscriptions.json`, so **every** registered device gets notified on
completion, not only the one that asked — `PUSH_CONCURRENCY` bounds how many
deliveries are in flight at once, not how many happen. Dead subscriptions
(HTTP 404/410 from the push service) are pruned automatically, unless the
device re-subscribed while that delivery was in the air. `GET /vapid-public-key`
supplies the key the browser needs to subscribe.

### `GET /processors`

`{"default": "core", "processors": ["lite", "base", …]}` — for clients that
build their own depth picker.

### `GET /health`

```json
{ "status": "ok", "app": "Footnote",
  "output_dir_writable": true,
  "parallel_configured": true, "firecrawl_configured": true,
  "notion_configured": false, "push_configured": true,
  "auth_required": true, "active_jobs": 1 }
```

Always public (it reports `auth_required` so clients can detect the token
requirement), which is also why it says whether the output folder can be
written but not where it is. The PWA uses it to warn about missing
configuration.

## The dossier

One folder per question, named `YYYY-MM-DD question-slug` (collisions get
`(2)`, `(3)`, … — nothing is ever overwritten). Inside, the report is a
Markdown file named after the question — so Obsidian shows the question as
the note title — plus one file per archived source:

```
2026-08-04 what is the current evidence on creatine and cognition/
├── what is the current evidence on creatine and cognition.md
└── sources/
    ├── 01 Creatine and cognitive performance — meta-analysis.md
    └── 02 Examine — Creatine.md
```

The report starts with YAML frontmatter, then the body Parallel wrote
(`##` sections, claims attributed in prose), then the evidence apparatus:

```markdown
---
question: "What is the current evidence on creatine and cognition?"
date: 2026-08-04T09:18:02Z
processor: core
confidence: high
sources: 9
app: Footnote
---

# What is the current evidence on creatine and cognition?

**Creatine shows small but consistent cognitive benefits under sleep
deprivation and in vegetarians; effects in rested omnivores are weak.**

## Evidence
…

## Sources

1. [Creatine and cognitive performance — meta-analysis](https://…) — [local copy](<sources/01 Creatine and cognitive performance — meta-analysis.md>)
   > the excerpt Parallel actually used from this source…
2. …

## Sources that could not be archived

- https://… — HTTP 403

## Method note

> Parallel's reasoning summary for the answer; its confidence rating sits
> in the frontmatter.
```

Details worth knowing:

- **Archived copies carry their citation's number** — `02 …md` is the second
  cited source whether or not the first one could be archived, so the folder
  and the report's numbered list never disagree.
- **Source order is basis order** — numbering follows the citation basis
  Parallel returned, deduplicated, so `[1]`-style markers in the body line
  up with the list.
- **Excerpts** under a source are the passages the research reports having
  used — a scent trail for spot-checking claims without opening everything.
- **Local copies** are the Firecrawl-extracted main content with their own
  frontmatter (`source:` URL, `title:`, `retrieved:` date), named
  `NN title.md` in citation order. Sources that refuse scraping (bot walls,
  paywalls) are listed with the reason instead — the dossier records what it
  *couldn't* keep, too.
- **Everything is plain Markdown with relative links** — the folder is
  self-contained: sync it, move it, zip it, nothing breaks. The same links
  resolve in the web view, which is why a dossier reads the same in Obsidian
  and in the browser.

## Configuration

| Setting | How | Default |
|---|---|---|
| Parallel API key | `PARALLEL_API_KEY` in `.env` (**required**) | unset — jobs refuse to start |
| Firecrawl API key | `FIRECRAWL_API_KEY` | unset — no local copies, dossiers still cite |
| Output directory | `--output-dir` flag > `OUTPUT_DIR` env var | iCloud `Research/inbox` on macOS, `~/Research/inbox` elsewhere |
| Job/subscription state | `DATA_DIR` | `data/` next to `app.py` |
| Bind address / port | `--host` / `--port` flags, or `HOST` / `PORT` | `0.0.0.0` / `8010` |
| Default depth | `DEFAULT_PROCESSOR` | `core` |
| Sources archived per dossier | `MAX_SOURCES` (`0` disables archiving) | `12` |
| Firecrawl requests per minute | `FIRECRAWL_RATE_LIMIT` (`0` disables pacing) | `10` — the free plan's limit |
| Firecrawl requests in flight | `FIRECRAWL_CONCURRENCY` | `2` — the free plan's limit |
| Cross-origin browser clients | `FOOTNOTE_CORS_ORIGINS` (comma-separated) | none — the PWA is same-origin |
| Honour a site's robots.txt | `RESPECT_ROBOTS` | `true` |
| API token | `FOOTNOTE_TOKEN` | unset — no authentication |
| Push keys | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIM_EMAIL` | unset — push disabled |
| Notion mirror | `NOTION_API_KEY`, `NOTION_DATABASE_ID` | unset — mirror disabled |

Numeric settings are validated at startup: a non-numeric or out-of-range
`MAX_SOURCES`, `FIRECRAWL_RATE_LIMIT` or `FIRECRAWL_CONCURRENCY`, or a
`DEFAULT_PROCESSOR` that is not a processor, refuses to start with a message
naming the setting rather than failing on the first request.

All of it can live in `.env` (see [.env.example](.env.example)). Real
environment variables win over `.env` entries — which is what lets the
Ubuntu deployment keep shared keys in `/opt/footnote/.env` with per-person
overrides in `/etc/footnote/<user>.env`.

## Authentication (optional)

Set `FOOTNOTE_TOKEN` and every endpoint except `GET /health` (and the PWA
shell assets — icons, manifest, service worker) requires it:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"  # generate one
echo "FOOTNOTE_TOKEN=paste-it-here" >> .env                    # then restart
```

Clients can present the token three ways, same contract as Margin:

- **`Authorization: Bearer <token>` header** — preferred for curl and the
  iOS Shortcut (one extra header field). Keeps the token out of access logs.
- **`?token=<token>` query parameter** — for URL-only contexts. Query
  strings do appear in the server's access log.
- **Browser cookie** — open `http://YOUR-SERVER:8010/?token=<token>` once
  and the token is stored in an `HttpOnly`, `SameSite=Strict` cookie
  (1 year), and the browser is then redirected to the same URL without the
  token, so it stops appearing in the address bar and in later history
  entries. That first request still carries it, so it appears once in the
  server's access log — the redirect limits the exposure rather than erasing
  it. After that the PWA works with no decoration. The home-screen app
  authenticates the same way — its cookie storage is separate from Safari's,
  so it shows a token form on first launch. Because the cookie is `Strict`,
  other websites can never ride it.

## Push notifications

Deep research takes minutes to hours — push is how you find out it's done
without keeping a tab open.

```bash
.venv/bin/pip install py-vapid
.venv/bin/vapid --gen          # prints the key pair
```

Put both keys and a contact email in `.env` (`VAPID_PUBLIC_KEY` /
`VAPID_PRIVATE_KEY` / `VAPID_CLAIM_EMAIL`), restart, then tap **🔔 Notify me
when research finishes** in the PWA on each device you care about.
Notifications fire on completion *and* failure; tapping one opens the
rendered report.

Platform notes:

- **iOS/iPadOS**: Web Push requires iOS 16.4+ *and* the app installed to the
  Home Screen (Safari Share → Add to Home Screen) — a Safari tab alone
  cannot receive push.
- Notifications go to **all** registered devices; there is no per-job
  targeting.
- Some platforms require a secure context for push: `http://localhost` works
  for testing, a bare LAN IP may not — the Tailscale HTTPS setup below
  solves this cleanly.

## Remote access via Tailscale

[Tailscale](https://tailscale.com) is the easiest way to ask Footnote things
away from home without exposing it to the internet — and
asking-from-anywhere is half the point of a research server you can talk to
by Shortcut.

**1. Install it on the server and your devices.**

```bash
curl -fsSL https://tailscale.com/install.sh | sh   # Ubuntu server
sudo tailscale up
```

iPhone/iPad/Mac: install the Tailscale app and sign in to the same tailnet.

**2. Point clients at the tailnet address** — `tailscale ip -4` (e.g.
`100.101.102.103`) or, with MagicDNS, the machine name
(`http://<machine>:8010`). Use it in the Shortcut and the PWA; it works
identically at home and away — no port forwarding, no dynamic DNS.

**3. Serve it over HTTPS** (recommended — and what switches the offline
shell and Web Push on at all).

A plain-`http://` address that is not `localhost` is **not a secure
context**, and browsers withhold whole APIs there rather than refusing them.
Measured in Chromium against `http://192.168.1.42:8010`:
`'serviceWorker' in navigator` is `false` — absent, not denied — so the
offline shell never registers, and `navigator.clipboard` and
`navigator.canShare` are undefined too. Push needs the same secure origin.
One line fixes all of it:

```bash
# in /etc/footnote/<user>.env:  HOST=127.0.0.1
sudo systemctl restart footnote@<user>
sudo tailscale serve --bg --https=443 8010
tailscale serve status                      # confirm
```

Enable HTTPS certificates for the tailnet once first (admin console → DNS →
HTTPS Certificates); `serve` cannot get a certificate without it. Footnote is
then at `https://<machine>.<your-tailnet>.ts.net` — real TLS certificate,
tailnet-only, and a secure context on every platform. `serve` is not
`funnel`: nothing is exposed to the internet. `FOOTNOTE_TOKEN` is still worth
setting on shared tailnets.

**Running Margin on the same machine?** Give each its own HTTPS port —
`--https=443` for one, `--https=8443` for the other (Tailscale allows 443,
8443 and 10000). Do **not** try to put them on one name under different paths
with `--set-path`: both apps address everything from the root (`/static/…`,
`/manifest.json`, `/jobs/…`), and a service worker's scope is the directory
it is served from, so one served under `/footnote/` could not control its own
pages.

**Then, on each device** — easy to miss, and none of it is optional:

1. Open the new URL once with `?token=…` to store the cookie. It now carries
   `Secure`, taken from `X-Forwarded-Proto`, because Tailscale terminates TLS
   and speaks plain HTTP to Footnote on loopback.
2. **Re-add the home-screen app from the HTTPS URL and delete the old one.**
   A PWA keeps the origin it was installed from, and service workers and
   caches are per-origin: the existing install will never gain the offline
   shell.
3. **Enable notifications again.** Push subscriptions are per-origin too, so
   the ones registered on the `http://` origin are dead. Footnote prunes them
   itself when the push service answers 404/410.
4. Update the iOS Shortcut to the HTTPS URL.

**Can you still reach it over plain HTTP?** Yes — leave `HOST=0.0.0.0`
(the default) instead of binding to loopback, and `serve` will proxy to the
same port while the tailnet and LAN addresses keep working. Two things follow.
The two addresses are two *origins*, so each has its own service worker
cache, PWA install, push subscription and cookie; only the HTTPS one reads
offline or notifies. And do not use the **same hostname** over both schemes:
a `Secure` cookie is only sent to a URI whose scheme is secure
([RFC 6265 §5.4](https://www.rfc-editor.org/rfc/rfc6265#section-5.4)), so
the HTTPS session's cookie is never sent over `http://` to that host and you
would be re-authenticating constantly. Use the machine name for HTTPS and the
tailnet IP for HTTP, and they stay independent.

## Install

Requirements: Python ≥ 3.10 and a Parallel.ai API key
(https://platform.parallel.ai). Optional: a Firecrawl key (source
archiving), VAPID keys (push), Notion keys (mirror).

### Local / macOS

```bash
git clone https://github.com/marcschlienger/footnote.git && cd footnote
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                 # add PARALLEL_API_KEY at minimum

./start.sh                           # → http://localhost:8010
curl http://localhost:8010/health
```

For auto-start at login, `start.sh` is a launchd-friendly wrapper — a
LaunchAgent plist example is in
[description.md](description.md#running-the-server).

### Ubuntu server (systemd)

**22.04 or newer**, for Python 3.10. Ubuntu 20.04's default `python3` is 3.8,
which will start the service and then fail inside jobs — the installer checks
and refuses rather than letting you find out that way; pass
`PYTHON=/usr/bin/python3.12` if you have a newer interpreter installed
another way.

Footnote is single-user by design, so the deployment model is **one instance
per person**: each instance runs as that person's own Unix account, writes
into that person's own (synced) folder, and has its own port, token, and job
history. From a checkout on the server:

```bash
sudo bash deploy/install.sh                          # shared platform (once)
sudoedit /opt/footnote/.env                          # shared API keys
sudo bash deploy/add-instance.sh <user> 8010         # one line per person
sudo bash deploy/add-instance.sh <other-user> 8011
```

`install.sh` sets up the shared parts — code and venv in `/opt/footnote` and
the [footnote@.service](deploy/footnote@.service) systemd template.
`add-instance.sh <user> <port> [output-dir]` writes
`/etc/footnote/<user>.env` (output dir defaults to
`/home/<user>/Research/inbox`, job state to `/var/lib/footnote/<user>`; a
`FOOTNOTE_TOKEN` is generated and printed) and enables `footnote@<user>`.
Dossier and data directories are created `0700` and the unit runs with
`UMask=0077`, so research stays private to its own user whatever the host
default is; the shared `/opt/footnote/.env` is `0640` and readable through a
`footnote` group each instance user joins, rather than by every local
account.
Both are idempotent, and both refuse a destination that is not theirs:
`/`, a top-level system directory, or a populated directory that is not a
Footnote installation. Both run as root and turn a path into the target of
`chown`, a recursive `chmod` and — for the application directory —
`rsync --delete`, so a mistyped one is not a misconfiguration to correct on
the next run. `add-instance.sh` also stops rather than guessing when an
existing `/etc/footnote/<user>.env` does not say what the instance runs as:
systemd reads that file, not the command line. API keys resolve per-person
first (`/etc/footnote/<user>.env`), then shared (`/opt/footnote/.env`).

Versions are lower bounds in `requirements.txt`, so an install resolves them
afresh and two installs a month apart are not the same software. Generate
`deploy/constraints.txt` on the server, test it, commit it, and every later
install gets exactly those versions:

```bash
bash deploy/make-constraints.sh python3   # on the server, not on a laptop
.venv/bin/python -m pytest
```

Upgrading is `install.sh` plus a restart, and `install.sh` repairs what it
needs to: existing instance users are added to the `footnote` group so they
keep reading the shared keys. If a service ever fails to start after an
upgrade, `journalctl -u footnote@<user>` says why — Footnote will not exit
over an unreadable `.env`, it says so and runs on whatever the environment
provides.

Day-2 operations:

```bash
systemctl status footnote@<user>
journalctl -u footnote@<user> -f
sudoedit /etc/footnote/<user>.env && sudo systemctl restart footnote@<user>
sudo bash deploy/install.sh && sudo systemctl restart 'footnote@*'   # upgrade
```

The service listens on all interfaces; run it on a private network (LAN,
Tailscale, WireGuard) and/or use the per-instance `FOOTNOTE_TOKEN`.

## Clients

- **The PWA** — open `http://YOUR-SERVER:8010/`: ask, pick a depth, watch
  status live, and read finished dossiers without leaving the page. A
  finished job offers **Read**, **Sources (n)** and **Everything (.zip)**;
  in the source list, a title opens that archived page in the card. Inside
  whatever you are reading sit **Copy text**, **Save .md** and **Open as
  page ↗**, so the file and the standalone page are things you do with the
  document rather than other ways of opening it. Inside a dossier, a
  **local copy** link opens that source under the citation it belongs to
  rather than navigating away. Nothing in the app
  navigates to a file: iOS Safari answers that with a view-or-download sheet
  you cannot get back from. Every standalone page has a link back to the app.
  A dossier
  you have opened once stays readable with no network at all — the service
  worker keeps the report and its sources, and says so when the list it is
  showing you is the last one it saw. On
  iPhone/iPad, Safari Share → **Add to Home Screen** installs it as a
  full-screen app with the Footnote icon (and enables push). With a token
  set, it prompts once on first launch.
- **iOS Shortcut** — "Ask Footnote": dictate a question to Siri or share
  selected text from any app; build instructions in
  [shortcut_setup.md](shortcut_setup.md).
- **Anything that speaks HTTP** — curl, Raycast, a cron job that re-runs a
  standing question weekly:

  ```bash
  curl -X POST http://server:8010/research \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $FOOTNOTE_TOKEN" \
    -d '{"question": "What changed in EU AI Act implementation this month?",
         "processor": "pro"}'
  ```

  (drop the `Authorization` header if you haven't set `FOOTNOTE_TOKEN`)

## Limitations & roadmap

- **Post-processing is not resumed, and only mostly suppressed.** A crash
  after a job is marked done leaves the dossier complete but skips the Notion
  copy and the notification permanently. Deleting a job that has just
  finished skips whichever of the two has not started yet — but a Notion
  request or a push already in flight completes, so a page can be created for
  a job you removed, and a notification already sent can open a 404. The
  window is the length of those two calls.
- **Answer quality is Parallel's** — Footnote orchestrates and files; it
  doesn't verify claims itself. The dossier's excerpts and archived copies
  exist precisely so *you* can. Confidence ratings come from Parallel and
  are calibrated, not guarantees.
- **Costs are per-question** and rise steeply with processor tier; Footnote
  has no budget cap (yet). Watch your Parallel dashboard early on.
- Sources behind aggressive bot protection or paywalls can't be archived;
  the dossier lists them with the reason instead.
- **A site's robots.txt is honoured before a page is archived.** Footnote
  fetches pages that were already cited, one at a time, into a folder only
  you read — closer to saving a page than to crawling — but robots.txt is the
  only machine-readable way a site says "not by machine". A refused page
  keeps its citation and its excerpt; only the local copy is skipped, the
  reason is recorded in the dossier, and no Firecrawl credit is spent on it.
  Set `RESPECT_ROBOTS=false` if you judge your own use differently. What this
  cannot see is everything that is not written down: licence terms, or
  whether a particular operator would mind.
- **Archiving is paced for Firecrawl's free plan** (10 requests/minute, 2
  concurrent browsers, 1 credit per page), which is what the defaults
  encode — so a 12-source dossier takes about a minute to archive rather
  than a few seconds, and the job's progress counts the copies as they land.
  Rate-limited and transient failures are retried, honouring `Retry-After`;
  a bot wall or paywall is recorded as final without a retry. If credits run
  out, the batch stops on the first `402` instead of spending the rest of the
  job on requests that cannot succeed (the two already in flight still
  finish), and the dossier and the job summary both say so. The budget is
  shared by the whole server, not per job, because Firecrawl counts requests
  per key. On a paid plan, raise `FIRECRAWL_RATE_LIMIT` and
  `FIRECRAWL_CONCURRENCY`. The budget is per process, so if you run several
  instances against one Firecrawl key, divide the limits between them in each
  instance's env file — or give each one its own key.
- Authentication is optional and coarse — one shared token per instance, no
  rate limiting. Keep the server on a private network regardless.
- **The bundle zip is built in memory** and holds the report plus the
  archived sources — fine at Footnote's scale (Markdown, capped by
  `MAX_SOURCES`), not a general-purpose folder export.
- Planned: budget guardrails (per-month task caps), scheduled standing
  questions, and a dossier index over `OUTPUT_DIR` in the PWA.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | FastAPI server: endpoints, auth, job store, orchestrator, PWA shell |
| `pipeline.py` | Parallel + Firecrawl clients, dossier writer, Notion mirror |
| `static/` | PWA (HTML/JS/CSS, service worker, manifest, icon SVG + PNGs) |
| `tests/test_footnote.py` | Unit tests (`pip install -r requirements-dev.txt && python -m pytest`) — no network |
| `tests/test_browser.py` | Browser tests: polling, panels, downloads, service worker. Needs `playwright install chromium`; skips without it |
| `deploy/` | Ubuntu installer, per-person instance script, systemd template, icon regeneration |
| `description.md` | Architecture: job lifecycle, API contracts, dossier format, design decisions |
| `shortcut_setup.md` | Step-by-step iOS Shortcut construction |
| `start.sh` | launchd-friendly start wrapper (macOS) |

## Name and icon

“Footnote” is Margin's sibling, and the name continues Margin's Fermat
epigraph: his margin note is the canonical claim filed *without* its
evidence — the proof didn't fit. A footnote is the part of the page where
the proof goes. Margin keeps what you want to read; Footnote goes and finds
out, and everything it claims hangs off a citation. The icon continues the
family — the same paper and red rule, but here the rule is a footnote
separator, and the reader's blue-ink mark is the **dagger (†)**,
typography's second footnote symbol after Margin's asterisk (\*).

## License

Footnote is free software, licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-or-later).
You may run, study, modify, and share it; if you offer a modified version
as a network service, you must make your modified source available to its
users. Copyright © 2026 Marc Schlienger.
