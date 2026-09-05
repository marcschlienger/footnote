# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project Overview

Footnote — a self-hosted deep-research server (sibling of ../margin, same
conventions). Ask a question via the PWA / iOS Shortcut / curl; a background
job runs Parallel.ai deep research, optionally archives cited sources with
Firecrawl, and writes a cited Markdown dossier into OUTPUT_DIR (a synced notes
folder). Web Push notifies on completion; Notion mirroring is optional.

## Layout

- `app.py` — FastAPI app: routes, token auth middleware, JSON job/subscription
  stores (`data/`), the `run_research` orchestrator, PWA shell serving.
- `pipeline.py` — external APIs: Parallel Task API (create run + long-poll
  result, `basis` → citations), Firecrawl v2 scrape, report/dossier writer,
  optional Notion mirror.
- `static/` — PWA (index.html, app.js, style.css, service-worker.js,
  manifest.json, icon.svg + generated PNGs).
- `deploy/` — `gen_icons.py` (re-render PNGs from icon.svg), systemd unit,
  `install.sh` + `add-instance.sh` (Ubuntu, one instance per person),
  `paths.sh` (shared path guards), `make-constraints.sh` (version pins).
- `tests/` — `test_footnote.py`: pytest, no network (httpx.MockTransport +
  TestClient). `test_browser.py`: Playwright against a real server on a
  loopback port, for what only exists in a browser; skips without chromium.
- `description.md` — architecture doc (job lifecycle, API contracts, dossier
  format, design decisions); `README.md` — full user-facing reference. Keep
  both in sync with behavior changes.

## Commands

```bash
.venv/bin/python -m pytest            # run tests (browser suite skips w/o chromium)
.venv/bin/playwright install chromium # enable tests/test_browser.py
./start.sh                            # run server (port 8010)
.venv/bin/python deploy/gen_icons.py  # regenerate icons after editing SVG
bash deploy/make-constraints.sh       # regenerate pins — on the target host only
shellcheck -x deploy/*.sh             # -x, or it stops at the sourced paths.sh
```

## Verified API contracts (do not "fix" from memory)

- Parallel: `POST https://api.parallel.ai/v1/tasks/runs` (header `x-api-key`),
  result via `GET …/runs/{id}/result?timeout=N` — blocks, 408 while running;
  citations live in `output.basis[].citations[]` (url/title/excerpts).
- Firecrawl: `POST https://api.firecrawl.dev/v2/scrape` (Bearer),
  `{url, formats:["markdown"], onlyMainContent:true}` →
  `{success, data:{markdown, metadata:{title}}}`. Free plan: 10 requests/min,
  2 concurrent browsers, 1 credit per page; 429 on breach (honour
  `Retry-After`), 402 when credits are gone — and pay-as-you-go is not
  available on the free plan, so 402 is terminal, not transient.

## Conventions

- Single-app, personal-use philosophy: no database, JSON stores written
  atomically; jobs resume after restart via the stored Parallel run_id.
- AGPL header on every source file, like Margin.
- Paper-and-ink UI palette shared with Margin (see static/style.css :root).
- Report layout: one folder per question in OUTPUT_DIR, report .md named
  after the question, archived sources in `sources/NN title.md`.
