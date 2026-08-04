# Apple Shortcuts Setup

An iOS Shortcut turns Footnote into "hey, go research this": type or dictate
a question from anywhere — Siri, the Share Sheet, a Home Screen icon — and
the dossier shows up in your notes folder minutes later, with a push
notification if you enabled it in the PWA.

Two shortcuts are described:

| Shortcut | Trigger | Depth |
|---|---|---|
| **Ask Footnote** | Siri / Share Sheet / Home Screen | `core` (~2–5 min) |
| **Ask Footnote (deep)** | duplicate of the first | `ultra` (~10–30 min) |

Time to build: about five minutes for the first, one for the second.

## Prerequisites

- Footnote running and reachable from the iPhone/iPad:
  - same Wi-Fi: `http://your-mac.local:8010` or the server's LAN IP, or
  - anywhere via [Tailscale](https://tailscale.com) (see the README):
    `http://footnote-box:8010` or `https://footnote-box.<tailnet>.ts.net`.
- Verify from iOS Safari first: open `http://YOUR-SERVER:8010/health` — you
  should see `"status": "ok"`. If this doesn't load, fix connectivity before
  building the shortcut.
- Your `FOOTNOTE_TOKEN`, if you set one.

Below, `YOUR-SERVER` stands for whatever base address worked in Safari.

## Shortcut 1 of 2 — "Ask Footnote"

Open **Shortcuts** → **+** (new shortcut), then add these actions in order
(search each by the name in bold).

**1. Configure input.** Tap the ⓘ info panel → enable **Show in Share
Sheet**. Then tap the "Any" input-type filter that appeared at the top of
the editor and restrict it to **Text** — sharing a web page's *selection* is
useful ("research this claim"); arbitrary files are not.

In the same input header, set **If there's no input** to **Ask For** →
**Text**. This one setting is what makes the shortcut work from all three
triggers: from the Share Sheet the shared text flows in; from Siri or the
Home Screen, iOS prompts for the question — and a Siri prompt takes
dictation automatically.

**2. Get Contents of URL.** Add the **Get Contents of URL** action and
expand **Show More**:

- **URL**: `http://YOUR-SERVER:8010/research`
- **Method**: `POST`
- **Headers** — one row, only if you use a token:
  - `Authorization` : `Bearer YOUR-TOKEN`  *(the word "Bearer", one space,
    then the token)*
- **Request Body**: `JSON`, with two fields:
  - `question` (Text) → select the **Shortcut Input** variable
  - `processor` (Text) → `core`

  (No `Content-Type` header needed — Shortcuts sets `application/json`
  automatically for a JSON body.)

**3. Get Dictionary Value.** Add **Get Dictionary Value** → *Get* **Value**
*for* `message` *in* **Contents of URL**.

**4. Show Notification.** Add **Show Notification** with the **Dictionary
Value** variable as the body. On success it reads *"Research started on the
core processor"*. Footnote answers errors with a readable JSON message too,
so a wrong token or too-short question shows its reason here as well. (Use
**Show Alert** instead if you prefer a banner you must dismiss.)

**5. Name and icon.** Name it **Ask Footnote** — the name is also the Siri
phrase. For the icon, blue-gray with the magnifying-glass glyph sits
closest to the app's blue-ink dagger.

## Shortcut 2 of 2 — "Ask Footnote (deep)"

Long-press **Ask Footnote** → **Duplicate**. In the copy, change
`processor` from `core` to `ultra` and rename it. Use it for questions
worth half an hour of digging — and expect it to cost accordingly. Any
processor from the README's table works; `pro` is a good middle setting.

## Using the shortcuts

- **Siri**: "Hey Siri, Ask Footnote." Siri asks *"What should I research?"*
  — dictate the question. This is the killer path: research fired off
  hands-free, dossier waiting in Obsidian when you're back at a desk.
- **Share Sheet**: select text anywhere (Safari, Mail, a PDF), **Share →
  Ask Footnote**. The selection becomes the research question — best for
  claim-shaped selections ("creatine improves cognition in adults").
- **Home Screen**: long-press the shortcut → **Add to Home Screen**. It
  sits well next to the Footnote PWA icon: the shortcut *asks*, the PWA
  *watches and reads*.
- **Back Tap** (Settings → Accessibility → Touch → Back Tap): map
  double-tap to **Ask Footnote** for the full sci-fi experience.

## The results

The dossier lands in `OUTPUT_DIR` — if that's your synced notes folder, it
simply appears there (in Obsidian: a new folder named date + question). For
the completion notification, enable push **in the PWA once per device**
(🔔 *Notify me when research finishes*); the shortcut itself only confirms
the start. Tapping the completion notification opens the rendered report.

The asymmetry is deliberate: the shortcut is fire-and-forget and needs no
notification permission; the PWA — installed to the Home Screen, which iOS
requires for Web Push anyway — is the receiving end.

## Troubleshooting

- **"Could not connect to the server"** — the phone can't reach
  `YOUR-SERVER`. Re-test `…/health` in Safari; on cellular you need the
  Tailscale variant (with the Tailscale app connected).
- **Notification says "Unauthorized: missing or wrong token"** — the
  `Authorization` header is missing or typo'd. The value must read
  `Bearer xxxxx` with exactly one space.
- **Notification says "question is too short"** — Footnote requires ≥ 8
  characters; Siri sometimes hears only a fragment. Ask again.
- **Notification says "unknown processor …"** — typo in step 2's
  `processor` field; use a name from the README's table, e.g. `core`.
- **Shortcut succeeds but no dossier appears** — the job may simply still
  be running (check the PWA or `…/jobs` in a browser), or it failed after
  starting — the job card shows the error. If `…/health` shows
  `"parallel_configured": false`, the server has no API key.
- **Siri triggers the wrong thing** — rename the shortcut to something
  more distinctive ("Research this"); the name *is* the phrase.
- **Certificate error on the Tailscale HTTPS URL** — use the full
  `….ts.net` machine name; certificates don't cover bare `100.x.y.z` IPs.

## Quick reference — server endpoints

| Endpoint | Method | Body | Use |
|---|---|---|---|
| `/research` | POST | `{"question": …, "processor": …}` | start a research job |
| `/research/{id}` | GET | — | status of one job |
| `/jobs` | GET | — | recent jobs + active count |
| `/jobs/{id}/report` | GET | — | rendered dossier (HTML) |
| `/health` | GET | — | reachability / configuration check |

All POST bodies are JSON. With `FOOTNOTE_TOKEN` set, add
`Authorization: Bearer <token>` to everything except `/health`.
