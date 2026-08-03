# Footnote

> \* see Margin. &nbsp;† this one goes and finds out.

Footnote is a self-hosted deep-research server — the research-out counterpart
to [Margin](../margin)'s read-it-later. Ask it a question and it runs a real
web-research task in the background (minutes, not seconds), then files a
**cited Markdown dossier** into a folder you control: the report itself, a
numbered source list, and — optionally — a local Markdown copy of every cited
page, so the evidence is still there when links rot.

Point `OUTPUT_DIR` at a synced folder (iCloud → Obsidian, Nextcloud,
Syncthing) and research lands in your notes; enable Web Push and your phone
buzzes when it's done. It is a single small FastAPI app for personal use, in
the same shape as Margin: run it on a Mac or an Ubuntu server, talk to it from
the built-in PWA, an iOS Shortcut, or `curl`.

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
 │  everything ─► OUTPUT_DIR/"2026-08-03 your question…"/                │
 │                ├── your question….md          ← report, front-matter, │
 │                │                                numbered source list  │
 │                └── sources/01 Some page.md    ← archived copies       │
 │                                                                       │
 │  done ─► Web Push notification ─► tap → rendered report               │
 └───────────────────────────────────────────────────────────────────────┘
```

Job state persists in `data/jobs.json`; a research run survives a server
restart (the Parallel run continues server-side and Footnote re-attaches on
boot). Reports are also viewable in the app (`/jobs/{id}/report`) and can be
mirrored to a Notion database if you configure keys.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # add PARALLEL_API_KEY (required), rest optional
./start.sh                # → http://localhost:8010
```

API keys:

- **Parallel.ai** (required) — https://platform.parallel.ai. Runs the actual
  research. Processors from `lite` (~1 min) to `ultra8x` (hours); the UI
  exposes base/core/pro/ultra/ultra4x, the API accepts all of them.
- **Firecrawl** (optional) — https://firecrawl.dev. Archives cited pages as
  Markdown next to the report. Without it, reports still cite and link
  every source.
- **VAPID keys** (optional) — push notifications:
  `.venv/bin/pip install py-vapid && .venv/bin/vapid --gen`, put both keys and
  a contact email in `.env`, then tap “Notify me” in the PWA.
- **Notion** (optional) — mirror reports into a database.

Everything else is env vars in `.env` (`OUTPUT_DIR`, `DEFAULT_PROCESSOR`,
`MAX_SOURCES`, `HOST`/`PORT`) — see [.env.example](.env.example).

## Using it

The PWA at the server root is the main interface — install it to the home
screen from Safari/Chrome. From anywhere else:

```bash
curl -X POST http://localhost:8010/research \
     -H 'Content-Type: application/json' \
     -d '{"question": "What is the current evidence on creatine and cognition?",
          "processor": "core"}'
```

| Endpoint | Purpose |
| --- | --- |
| `POST /research` | start a job (`question`, optional `processor`) |
| `GET /research/{id}` | job status |
| `GET /jobs` | recent jobs |
| `GET /jobs/{id}/report` | rendered report (HTML) |
| `GET /jobs/{id}/report.md` | raw Markdown |
| `DELETE /jobs/{id}` | remove finished job from history |
| `GET /health` | status + configuration check |

Asking from an iPhone (Share Sheet / Siri) is covered in
[shortcut_setup.md](shortcut_setup.md).

## Security

Same model as Margin: designed for a private network or Tailscale. For
anything beyond that, set `FOOTNOTE_TOKEN` in `.env` — every endpoint except
`/health` and the PWA shell then requires `Authorization: Bearer <token>`,
`?token=…`, or the browser cookie set after one authenticated visit.

## Deployment

- **macOS**: run `./start.sh` from a LaunchAgent (see Margin's setup — the
  layout is identical).
- **Ubuntu**: `deploy/footnote@.service` is a systemd template unit —
  `/opt/footnote` checkout, per-user env file in `/etc/footnote/<user>.env`,
  `systemctl enable footnote@<user>`.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # no network needed
.venv/bin/python deploy/gen_icons.py  # re-render PNGs after editing icon.svg
```

## Name and icon

“Footnote” is Margin's sibling: Margin keeps what you want to read, Footnote
goes and finds out, and everything it claims hangs off a citation. The icon
continues the family — the same paper and red rule, but here the rule is a
footnote separator, and the reader's blue-ink mark is the **dagger (†)**,
typography's second footnote symbol after Margin's asterisk (\*).

## License

GNU AGPL v3.0 or later — see [LICENSE](LICENSE).
