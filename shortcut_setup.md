# Asking Footnote from an iPhone or iPad

An iOS Shortcut turns Footnote into "hey, go research this": type or dictate
a question from anywhere (including Siri or the Share Sheet), and the dossier
shows up in your notes folder a few minutes later, with a push notification
if you enabled it in the PWA.

## The Shortcut

Open **Shortcuts** → **+** and add these actions:

1. **Receive input** (Shortcut details → "Use as Share Sheet" on, input types
   *Text* and *URLs*; also enable "Show in Share Sheet")
2. **If** ⌘ *Shortcut Input* *has any value* — skip to step 4
3. **Ask for Input** — prompt "What should I research?", input type *Text*
   (dictation works automatically when triggered via Siri)
4. **Text** — set to the input variable (Shortcut Input, or Provided Input)
5. **Get Contents of URL**
   - URL: `http://YOUR-SERVER:8010/research`
   - Method: **POST**
   - Headers: `Content-Type: application/json`
     (plus `Authorization: Bearer YOUR-TOKEN` if you set `FOOTNOTE_TOKEN`)
   - Request Body: **JSON**
     - `question` → the Text variable
     - `processor` → `core`  *(or `pro`/`ultra` for a deeper dive)*
6. **Get Dictionary Value** — key `message`
7. **Show Notification** — the dictionary value from step 6

Name it **Ask Footnote**. Now:

- **Siri:** "Hey Siri, Ask Footnote" → dictate the question.
- **Share Sheet:** share selected text from any app → Ask Footnote — the
  selection becomes the research question.
- **Home Screen:** add the shortcut as an icon next to the Footnote PWA.

## Server address

- Same Wi-Fi: your Mac's LAN name (`http://your-mac.local:8010`) or the
  server's LAN IP.
- Anywhere: a [Tailscale](https://tailscale.com) address
  (`http://your-server.tailnet-name.ts.net:8010`) — set `FOOTNOTE_TOKEN`
  if the server is reachable beyond machines you trust.

## Depth variants

Duplicate the shortcut as **Ask Footnote (deep)** with `processor` set to
`ultra` for questions worth a half-hour of digging. The `/research` response's
`message` field confirms which processor took the job.

## Getting the results

The dossier lands in `OUTPUT_DIR` — if that's your synced notes folder
(iCloud → Obsidian, Nextcloud), it simply appears there. The push
notification (enable it once in the PWA: 🔔 "Notify me when research
finishes") links straight to the rendered report.
