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
carries on.

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
  "report_path": "", "notion_url": "", "error": "" }
```

`status` walks `queued → researching → archiving → saving → done` (or
`failed`, with `error` set). Once `done`, the response also carries
`report_path` (absolute path of the dossier on the server),
`sources_cited`, `sources_archived`, and `finished_at`.

### `GET /jobs` — recent jobs

`{"jobs": […most recent first…], "active": 1}` — the PWA's poll target.
`?limit=N` caps the list (default 50). History keeps the last 200 jobs;
finished ones age out first, running ones never.

### `DELETE /jobs/{job_id}`

Removes a *finished* job from history (409 while it is running). The dossier
files in `OUTPUT_DIR` are never touched — job history is bookkeeping, the
folder is the record.

### `GET /jobs/{job_id}/report` — read the dossier

Server-rendered HTML view of the report (paper-styled, with a download
link) — this is what the push notification opens.
`GET /jobs/{job_id}/report.md` serves the raw Markdown, and
`GET /jobs/{job_id}/sources/{file}` serves an archived source copy, so the
report's relative "local copy" links work in the browser exactly as they do
in your notes app.

### `POST /subscribe` — register for push

Body: a standard
[PushSubscription JSON](https://developer.mozilla.org/en-US/docs/Web/API/PushSubscription/toJSON)
(the PWA does this for you). Subscriptions are stored server-side in
`data/subscriptions.json`, so **every** registered device gets notified on
completion, not only the one that asked. Dead subscriptions (HTTP 404/410
from the push service) are pruned automatically. `GET /vapid-public-key`
supplies the key the browser needs to subscribe.

### `GET /processors`

`{"default": "core", "processors": ["lite", "base", …]}` — for clients that
build their own depth picker.

### `GET /health`

```json
{ "status": "ok", "app": "Footnote",
  "output_dir": "/home/marc/Research/inbox", "output_dir_writable": true,
  "parallel_configured": true, "firecrawl_configured": true,
  "notion_configured": false, "push_configured": true,
  "auth_required": true, "active_jobs": 1 }
```

Always public (it reports `auth_required` so clients can detect the token
requirement). The PWA uses it to warn about missing configuration.

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
  self-contained: sync it, move it, zip it, nothing breaks.

## Configuration

| Setting | How | Default |
|---|---|---|
| Parallel API key | `PARALLEL_API_KEY` in `.env` (**required**) | unset — jobs refuse to start |
| Firecrawl API key | `FIRECRAWL_API_KEY` | unset — no local copies, dossiers still cite |
| Output directory | `--output-dir` flag > `OUTPUT_DIR` env var | iCloud `Research/inbox` on macOS, `~/Research/inbox` elsewhere |
| Job/subscription state | `DATA_DIR` | `data/` next to `app.py` |
| Bind address / port | `--host` / `--port` flags, or `HOST` / `PORT` | `0.0.0.0` / `8010` |
| Default depth | `DEFAULT_PROCESSOR` | `core` |
| Sources archived per dossier | `MAX_SOURCES` | `12` |
| API token | `FOOTNOTE_TOKEN` | unset — no authentication |
| Push keys | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_CLAIM_EMAIL` | unset — push disabled |
| Notion mirror | `NOTION_API_KEY`, `NOTION_DATABASE_ID` | unset — mirror disabled |

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
  (1 year); after that the PWA works with no decoration. The home-screen app
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
(`http://footnote-box:8010`). Use it in the Shortcut and the PWA; it works
identically at home and away — no port forwarding, no dynamic DNS.

**3. Optionally, tailnet-only + HTTPS** (recommended, and the clean way to
get push everywhere):

```bash
# in /etc/footnote/<user>.env:  HOST=127.0.0.1
sudo systemctl restart footnote@<user>
sudo tailscale serve --bg 8010
```

Footnote is now at `https://footnote-box.<your-tailnet>.ts.net` — real TLS
certificate, unreachable from the LAN or the internet, and a secure context
for service workers and Web Push on every platform. `FOOTNOTE_TOKEN` is
still worth setting on shared tailnets.

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

Footnote is single-user by design, so the deployment model is **one instance
per person**: each instance runs as that person's own Unix account, writes
into that person's own (synced) folder, and has its own port, token, and job
history. From a checkout on the server:

```bash
sudo bash deploy/install.sh                    # shared platform (once)
sudoedit /opt/footnote/.env                    # shared API keys
sudo bash deploy/add-instance.sh marc 8010     # one line per person
sudo bash deploy/add-instance.sh anna 8011
```

`install.sh` sets up the shared parts — code and venv in `/opt/footnote` and
the [footnote@.service](deploy/footnote@.service) systemd template.
`add-instance.sh <user> <port> [output-dir]` writes
`/etc/footnote/<user>.env` (output dir defaults to
`/home/<user>/Research/inbox`, job state to `/var/lib/footnote/<user>`; a
`FOOTNOTE_TOKEN` is generated and printed) and enables `footnote@<user>`.
Both are idempotent. API keys resolve per-person first
(`/etc/footnote/<user>.env`), then shared (`/opt/footnote/.env`).

Day-2 operations:

```bash
systemctl status footnote@marc
journalctl -u footnote@marc -f
sudoedit /etc/footnote/marc.env && sudo systemctl restart footnote@marc
sudo bash deploy/install.sh && sudo systemctl restart 'footnote@*'   # upgrade
```

The service listens on all interfaces; run it on a private network (LAN,
Tailscale, WireGuard) and/or use the per-instance `FOOTNOTE_TOKEN`.

## Clients

- **The PWA** — open `http://YOUR-SERVER:8010/`: ask, pick a depth, watch
  status live, read finished reports. On iPhone/iPad, Safari Share → **Add
  to Home Screen** installs it as a full-screen app with the Footnote icon
  (and enables push). With a token set, it prompts once on first launch.
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

- **Answer quality is Parallel's** — Footnote orchestrates and files; it
  doesn't verify claims itself. The dossier's excerpts and archived copies
  exist precisely so *you* can. Confidence ratings come from Parallel and
  are calibrated, not guarantees.
- **Costs are per-question** and rise steeply with processor tier; Footnote
  has no budget cap (yet). Watch your Parallel dashboard early on.
- Sources behind aggressive bot protection or paywalls can't be archived;
  the dossier lists them with the reason instead.
- Authentication is optional and coarse — one shared token per instance, no
  rate limiting. Keep the server on a private network regardless.
- Planned: budget guardrails (per-month task caps), scheduled standing
  questions, and a dossier index over `OUTPUT_DIR` in the PWA.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | FastAPI server: endpoints, auth, job store, orchestrator, PWA shell |
| `pipeline.py` | Parallel + Firecrawl clients, dossier writer, Notion mirror |
| `static/` | PWA (HTML/JS/CSS, service worker, manifest, icon SVG + PNGs) |
| `tests/` | Unit tests (`pip install -r requirements-dev.txt && python -m pytest`) — no network |
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
