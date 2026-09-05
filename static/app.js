// Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
// Licensed under the GNU AGPL v3.0 or later; see the LICENSE file.

/* Front-end logic: submit questions, poll job status, manage push. */

const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "researching", "archiving", "saving"]);
let pollTimer = null;
// Refreshes are started by the poll, by submitting, and by removing a job,
// so two can be in flight at once. The last response to arrive is not
// necessarily the newest one.
let pollGeneration = 0;
let pushConfigured = false;
// Which source panels the reader has open, and what they hold — polling
// re-renders the whole list, and an open panel must survive that.
const openSources = new Set();
const sourcesCache = new Map();
// Readers opened in place, by the URL they show. Polling rebuilds the whole
// list, and something you are part-way through reading should not vanish
// because a timer fired.
const openReaders = new Set();
// One cache, keyed by URL — a rendered fragment and the raw file behind it
// are fetched from different URLs, so they cannot collide.
const readerCache = new Map();
// An archived page can be a few hundred kB of HTML, and a long session can
// open a lot of them. Keep the most recent handful.
const READER_CACHE_MAX = 12;
const SOURCES_TTL_MS = 60000;   // a copy can be moved or deleted in the notes

// --------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------

(async function boot() {
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/service-worker.js"); }
    catch (e) { console.warn("SW registration failed", e); }
  }
  try {
    const health = await fetchJSON("/health");
    pushConfigured = health.push_configured;
    $("server-note").textContent =
      "Dossiers are filed in the server's notes folder" +
      (health.notion_configured ? " · mirrored to Notion" : "");
    if (!health.parallel_configured) {
      flash("PARALLEL_API_KEY is not configured on the server — research " +
            "jobs will fail. See README.", true);
    } else if (!health.output_dir_writable) {
      flash("The server cannot write to its output folder — research will " +
            "fail when it tries to save. See README.", true);
    }
  } catch (e) { /* offline shell — the list will populate when back online */ }
  // Settle the depth picker before the form can be used, so an immediate
  // submission cannot go out under the hard-coded default.
  await selectServerDefault();
  $("go").disabled = false;
  offerPush();
  refreshJobs();
})();

// The picker offers a curated five, not the server's full processor list —
// friendly labels are the point. But which of them is preselected should be
// the server's own DEFAULT_PROCESSOR, not a guess baked into the HTML.
async function selectServerDefault() {
  try {
    const { default: preferred, processors } = await fetchJSON("/processors");
    const picker = $("processor");
    if (!processors.includes(preferred)) return;
    if (![...picker.options].some((o) => o.value === preferred)) {
      picker.add(new Option(preferred, preferred));
    }
    picker.value = preferred;
  } catch (e) { /* offline, or an older server: the HTML default stands */ }
}

// --------------------------------------------------------------------
// Submit
// --------------------------------------------------------------------

$("ask").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const question = $("question").value.trim();
  if (question.length < 8) return;
  $("go").disabled = true;
  const asked = $("processor").value;   // before an await; the picker can move
  try {
    await fetchJSON("/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, processor: asked }),
    });
    $("question").value = "";
    // The server answers in processor ids, which is right for curl and the
    // Shortcut and wrong here: the picker said "Exhaustive", so does this.
    flash(`Started: ${depthLabel(asked)}. You can close this page; ` +
          `results land in your notes.`);
    offerPush();
    refreshJobs();
  } catch (e) {
    flash("Could not start research: " + e.message, true);
  } finally {
    $("go").disabled = false;
  }
});

// --------------------------------------------------------------------
// Jobs list + polling
// --------------------------------------------------------------------

async function refreshJobs() {
  clearTimeout(pollTimer);
  // Whoever started last owns the render and the next timer. Without this a
  // slow first response landed after a fast second one and put the older
  // list back on screen, and both of them armed a timer — so every
  // submission and every removal permanently doubled the poll rate.
  const generation = ++pollGeneration;
  const current = () => generation === pollGeneration;
  // Not fetchJSON: the service worker marks a list it served from cache
  // because the network was gone, and that is worth saying out loud.
  let data, fromCache = false;
  try {
    const res = await fetch("/jobs");
    if (!res.ok) throw new Error(res.statusText);
    fromCache = res.headers.get("X-Footnote-Cached") === "1";
    data = await res.json();
  } catch (e) {
    if (current()) pollTimer = setTimeout(refreshJobs, 15000);
    return;
  }
  if (!current()) return;         // a newer refresh has this in hand

  const list = $("jobs");
  $("jobs-section").hidden = data.jobs.length === 0;
  $("offline").hidden = !fromCache;
  $("active-count").textContent =
    data.active ? `${data.active} running` : "";
  list.innerHTML = "";
  for (const job of data.jobs) list.appendChild(renderJob(job));

  pollTimer = setTimeout(
    refreshJobs, fromCache ? 30000 : data.active > 0 ? 5000 : 60000);
}

function renderJob(job) {
  const li = document.createElement("li");
  li.className = "job";

  const q = document.createElement("p");
  q.className = "q";
  q.textContent = job.question;
  li.appendChild(q);

  const meta = document.createElement("div");
  meta.className = "meta";

  const badge = document.createElement("span");
  badge.className = "badge " + job.status;
  badge.textContent = job.status;
  meta.appendChild(badge);

  if (job.processor) {
    const depth = text("span", depthLabel(job.processor));
    depth.className = "depth";
    depth.title = job.processor;        // the name the API uses, on hover
    meta.appendChild(depth);
  }
  const stamp = when(job.created_at);
  if (stamp) meta.appendChild(stamp);

  if (ACTIVE.has(job.status)) {
    const p = document.createElement("span");
    p.className = "progress";
    p.innerHTML = `<span class="spin"></span>`;
    p.appendChild(document.createTextNode(job.progress || "Working…"));
    meta.appendChild(p);
  } else if (job.status === "failed") {
    const p = text("span", job.error || "Failed");
    p.className = "progress";
    p.style.color = "var(--rule-red)";
    meta.appendChild(p);
  } else if (job.status === "done") {
    const links = document.createElement("span");
    links.className = "links";
    if (job.report_available !== true) {
      // The dossier moved or was deleted in the notes folder. Say so instead
      // of offering links that answer 404.
      const gone = text("span", "report not where it was filed");
      gone.style.color = "var(--rule-red)";
      links.appendChild(gone);
    } else {
      // One way in, as with a source: the title of the thing opens it here,
      // and the page, the text and the file are things you can then do with
      // what is open. Two controls onto one dossier read as two documents.
      links.appendChild(reader({
        label: "Read",
        embedUrl: `/jobs/${job.id}/report?embed=1`,
        rawUrl: `/jobs/${job.id}/report.md`,
        filename: job.report_name,
        pageUrl: `/jobs/${job.id}/report`,
        host: li,
      }));
      links.appendChild(sourcesToggle(job));
      links.appendChild(bundleButton(`/jobs/${job.id}/bundle.zip`,
                                     "Everything (.zip)"));
    }
    if (job.notion_url) links.appendChild(link(job.notion_url, "Notion"));
    if (job.progress) {
      const note = text("span", job.progress.replace(/^Done — /, ""));
      note.style.cssText = "color:var(--ink-soft);font-weight:400;margin-left:auto";
      links.appendChild(note);
    }
    meta.appendChild(links);
  }

  if (!ACTIVE.has(job.status)) {
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "remove";
    del.title = "Remove this job from the list. The dossier stays in your notes.";
    del.onclick = async () => {
      try {
        await fetchJSON(`/jobs/${job.id}`, { method: "DELETE" });
      } catch (e) {
        // Swallowing this left the row sitting there with no explanation.
        flash("Could not remove that job: " + e.message, true);
        return;
      }
      openSources.delete(job.id);
      sourcesCache.delete(job.id);
      forgetReaders(job.id);
      // Tell the worker now: the 404-driven cleanup only fires if someone
      // asks for the job again, and offline that may never happen.
      navigator.serviceWorker?.controller?.postMessage(
        { type: "forget-job", jobId: job.id });
      refreshJobs();
    };
    meta.appendChild(del);
  }

  li.appendChild(meta);
  if (job.status === "done") {
    // Directly under the links that open it, and before any file or reader
    // panel, which are appended to the end of the card.
    const panel = document.createElement("div");
    panel.className = "sources";
    panel.hidden = !openSources.has(job.id);
    li.appendChild(panel);
    if (!panel.hidden) fillSources(job.id, panel);
  }
  return li;
}

// --------------------------------------------------------------------
// Source material: read a local copy, download one, or download the lot
// --------------------------------------------------------------------

function sourcesToggle(job) {
  const button = document.createElement("button");
  button.className = "linkish";
  const count = job.sources_cited;
  button.textContent = "Sources" + (count ? ` (${count})` : "");
  button.setAttribute("aria-expanded", String(openSources.has(job.id)));
  button.onclick = () => {
    const panel = button.closest(".job").querySelector(".sources");
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
    if (panel.hidden) {
      openSources.delete(job.id);
      return;
    }
    openSources.add(job.id);
    fillSources(job.id, panel);
    reveal(panel);
  };
  return button;
}

// Bring what was just opened into view. Without this, opening one panel
// while another is already open puts the new one below a screenful of text,
// and the control appears to have done nothing at all.
function reveal(element) {
  requestAnimationFrame(() => {
    const box = element.getBoundingClientRect();
    if (box.top < 0 || box.top > window.innerHeight - 60) {
      element.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  });
}

async function fillSources(jobId, panel) {
  // The cache exists so a poll re-render doesn't refetch; it should not
  // outlive the thing it describes, since a copy can be moved or deleted in
  // the notes folder while the page is open.
  const held = sourcesCache.get(jobId);
  let data = held && Date.now() - held.at < SOURCES_TTL_MS ? held.data : null;
  if (!data) {
    if (!held) panel.textContent = "Loading sources…";
    try {
      data = await fetchJSON(`/jobs/${jobId}/sources`);
      sourcesCache.set(jobId, { data, at: Date.now() });
    } catch (e) {
      if (!held) { panel.textContent = "Could not list the sources: " + e.message; return; }
      data = held.data;                 // offline: what we have beats nothing
    }
  }
  panel.textContent = "";
  if (!data.sources.length) {
    panel.textContent = "No sources recorded for this dossier.";
    return;
  }
  const list = document.createElement("ol");
  for (const src of data.sources) list.appendChild(renderSource(src));
  panel.appendChild(list);
  const foot = document.createElement("p");
  foot.className = "srcs-foot";
  foot.appendChild(text("span",
    `${data.archived} of ${data.cited} archived locally · `));
  foot.appendChild(bundleButton(data.bundle_url,
                                "download report + sources (.zip)"));
  panel.appendChild(foot);
}

function renderSource(src) {
  const li = document.createElement("li");
  const row = document.createElement("span");
  row.className = "src-links";

  // The title is the way in: tapping it opens the archived copy here. Not
  // archived, and the live page is all there is.
  let title;
  if (src.archived) {
    title = reader({
      label: src.title,
      slot: "list",
      embedUrl: src.read_url + "?embed=1",
      rawUrl: src.download_url,
      filename: src.file,
      pageUrl: src.read_url,
      host: li,
      after: row,
    });
  } else {
    title = src.url ? link(src.url, src.title) : text("span", src.title);
  }
  // Added, not assigned: replacing the class list stripped "linkish" off the
  // reader button, and a bare <button> is the page's primary button — white
  // on blue, where a source title should read as a link.
  title.classList.add("src-title");
  li.appendChild(title);

  if (src.archived) {
    if (src.url) row.appendChild(link(src.url, "original ↗"));
    row.appendChild(text("span", size(src.bytes)));
  } else {
    // "no local copy" rather than "not archived": it is true both of a page
    // that refused to be saved and of a copy that has since left the folder.
    row.appendChild(text("span",
      src.note ? `no local copy — ${src.note}` : "no local copy"));
  }
  li.appendChild(row);
  return li;
}

// Read a dossier or an archived copy without leaving the page. The links
// beside these still open the standalone views — those are what a push
// notification and the report's "local copy" links point at.
function remember(url, body) {
  readerCache.set(url, body);
  while (readerCache.size > READER_CACHE_MAX) {
    readerCache.delete(readerCache.keys().next().value);
  }
}

function forgetReaders(jobId) {
  const prefix = `/jobs/${jobId}/`;
  for (const key of [...openReaders]) {
    // A key may carry a slot name in front of the URL; nothing but a URL
    // contains "/jobs/<id>/", so looking for it anywhere in the key is safe.
    if (key.includes(prefix)) openReaders.delete(key);
  }
  for (const url of [...readerCache.keys()]) {
    if (url.startsWith(prefix)) readerCache.delete(url);
  }
}

// One way to look at a dossier or an archived copy: open it here. What you
// might want to do with the file — copy it, save it, share it, see it as a
// page — are actions on what you are reading, offered inside the panel
// rather than competing with it in the row above.
function reader(spec) {
  const { label, embedUrl, host, after, control } = spec;
  // What is remembered across a poll is a reader, not a document. The same
  // archived page is reachable from the Sources list and from the citation
  // inside the dossier, and keying by URL alone made opening one silently
  // open the other — and closing either one arrange for the survivor to
  // vanish at the next poll.
  const slot = spec.slot ? `${spec.slot} ${embedUrl}` : embedUrl;
  // Usually a button this makes; sometimes a link already in the text that
  // should stop being a link (see openLocalCopiesHere).
  const button = control || document.createElement("button");
  if (!control) {
    button.className = "linkish";
    button.textContent = label;
  }
  button.setAttribute("aria-expanded", "false");

  const close = () => {
    host.querySelector(":scope > .src-body")?.remove();
    openReaders.delete(slot);
    button.setAttribute("aria-expanded", "false");
  };

  const open = async (auto) => {
    openReaders.add(slot);
    button.setAttribute("aria-expanded", "true");
    const panel = document.createElement("div");
    panel.className = "src-body";
    panel.textContent = readerCache.has(embedUrl) ? "" : "Loading…";
    host.insertBefore(panel, (after && after.nextSibling) || null);
    if (!auto) reveal(panel);
    try {
      let html = readerCache.get(embedUrl);
      if (html === undefined) {
        const res = await fetch(embedUrl);
        if (!res.ok) throw new Error(res.statusText || `HTTP ${res.status}`);
        html = await res.text();
        remember(embedUrl, html);
      }
      panel.textContent = "";
      panel.appendChild(readerActions(spec));
      // Warm the raw file now. Copy and Save must not await a fetch inside
      // the tap that asked for them: the older copy route and a download
      // both need the click's user activation, which an await spends.
      rawText(spec).catch(() => {});
      const content = document.createElement("div");
      content.className = "src-content";
      // Sanitised server-side, by the same allowlist the standalone page uses.
      content.innerHTML = html;
      panel.appendChild(content);
      openLocalCopiesHere(content, spec);
      // The toggle that opened this is far above by now.
      const foot = document.createElement("div");
      foot.className = "src-actions src-foot";
      foot.appendChild(action("Close", () => { close(); button.focus(); }));
      panel.appendChild(foot);
    } catch (e) {
      panel.textContent = "Could not read it: " + e.message;
      openReaders.delete(slot);
      button.setAttribute("aria-expanded", "false");
    }
  };

  button.onclick = (event) => {
    if (event) event.preventDefault();   // a link that is not a navigation
    return host.querySelector(":scope > .src-body") ? close() : open(false);
  };
  // Deferred: the card is still being assembled, so the row this panel
  // belongs after may not be in the DOM yet. Reopening after a poll must not
  // scroll — the reader did not ask for it this time.
  if (openReaders.has(slot)) queueMicrotask(() => open(true));
  return button;
}

// Every archived citation in a dossier ends with a "local copy" link, and it
// is written into the Markdown file itself, relatively, so the dossier works
// as a plain file in the notes folder. Rendered here it became the one link
// in the app that left the app: tapping it navigated to the source's own
// page and took the shell with it, closing everything else that was open.
//
// So inside a reader it opens the same way a source in the Sources panel
// does — under the citation it belongs to, with the dossier still where it
// was. The standalone pages keep the plain link, since there is nothing to
// stay inside there.
const LOCAL_COPY = /^\/jobs\/[0-9a-f]{12}\/sources\/.+/;

function openLocalCopiesHere(content, spec) {
  for (const anchor of content.querySelectorAll("a[href]")) {
    let target;
    try {
      target = new URL(anchor.getAttribute("href"), location.href);
    } catch (e) { continue; }
    if (target.origin !== location.origin) continue;
    if (!LOCAL_COPY.test(target.pathname)) continue;
    const path = target.pathname;
    if (`${path}?embed=1` === spec.embedUrl) continue;      // itself
    // One panel per host, because that is how a reader finds its own: the
    // citation's list item, which is where the copy belongs anyway.
    const item = anchor.closest("li");
    if (!item || !content.contains(item)) continue;
    if (item.querySelector("[data-reader]")) continue;
    anchor.dataset.reader = "1";
    reader({
      control: anchor,
      slot: "cited",
      embedUrl: `${path}?embed=1`,
      rawUrl: `${path}?raw=1`,
      pageUrl: path,
      host: item,
    });
  }
}

function readerActions(spec) {
  const row = document.createElement("div");
  row.className = "src-actions";
  row.appendChild(action("Copy text", async () => {
    if (await copyText(await rawText(spec))) flash("Copied to the clipboard.");
    else flash("This browser would not let the page copy — " +
               "select the text below instead.", true);
  }));
  row.appendChild(action("Save .md", async () => {
    const body = await rawText(spec);
    const name = spec.filename || "dossier.md";
    if (await tryShare(name, body)) return;
    saveFile(name, body);
  }));
  // Only where the title no longer leads there: the job card keeps its own
  // "Report" link, and two ways to the same page is what we just removed.
  if (spec.pageUrl) {
    row.appendChild(link(spec.pageUrl, "Open as page ↗"));
  }
  return row;
}

async function rawText(spec) {
  let body = readerCache.get(spec.rawUrl);
  if (body === undefined) {
    const res = await fetch(spec.rawUrl);
    if (!res.ok) throw new Error(res.statusText || `HTTP ${res.status}`);
    if (!spec.filename) {
      spec.filename = nameFromDisposition(res.headers.get("content-disposition"));
    }
    body = await res.text();
    remember(spec.rawUrl, body);
  }
  return body;
}

// Footnote is normally reached over plain HTTP on a home network, and the
// Clipboard API is a secure-context feature: navigator.clipboard is not
// merely refused there, it does not exist. The selection route is deprecated
// but is what still works, so it is the fallback rather than the error.
async function copyText(body) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(body); return true; }
    catch (e) { /* denied, or no permission on this origin */ }
  }
  const box = document.createElement("textarea");
  box.value = body;
  box.readOnly = true;                 // stops the keyboard opening on iOS
  box.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
  document.body.appendChild(box);
  box.select();
  box.setSelectionRange(0, body.length);      // iOS ignores select() alone
  let copied = false;
  try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
  box.remove();
  return copied;
}

// The share sheet sits over the page, which is the only way iOS hands over a
// file without leaving the app. It needs a secure context, so on plain HTTP
// this is absent and Save is what carries the day.
async function tryShare(name, body) {
  const file = new File([body], name, { type: "text/markdown" });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: name });
      return true;
    }
  } catch (e) { /* dismissed, or unavailable */ }
  return false;
}

function action(label, run) {
  const button = document.createElement("button");
  button.className = "linkish";
  button.textContent = label;
  button.onclick = () => Promise.resolve()
    .then(run)
    .catch((e) => flash(`Could not ${label.toLowerCase()}: ` + e.message, true));
  return button;
}

// The card used to print the processor id — a job asked for as "Exhaustive"
// came back labelled "ultra". The picker is where these names are written
// down, so it is what answers.
//
// The API takes eighteen processors and the picker offers five of them, but
// the other thirteen are those same depths with a multiplier or the fast
// variant, so they can be said in the same words. A name that fits neither
// keeps its own, which is the honest answer.
function depthLabel(processor) {
  const named = (value) => {
    const option = [...$("processor").options].find((o) => o.value === value);
    return option ? option.textContent.split("·")[0].trim() : "";
  };
  const exact = named(processor);
  if (exact) return exact;
  // The fast variant of a depth the picker does name: ultra4x is "Heroic",
  // so ultra4x-fast is "Heroic (fast)" rather than "Exhaustive ×4 (fast)".
  const slower = named(String(processor || "").replace(/-fast$/, ""));
  if (slower) return `${slower} (fast)`;
  const parts = /^([a-z]+?)(\d+x)?(-fast)?$/.exec(processor || "");
  const base = parts && named(parts[1]);
  if (!base) return processor;
  return base + (parts[2] ? ` ×${parts[2].slice(0, -1)}` : "")
              + (parts[3] ? " (fast)" : "");
}

function size(bytes) {
  if (!bytes) return "";
  return bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`;
}

// --------------------------------------------------------------------
// Push notifications
// --------------------------------------------------------------------

async function offerPush() {
  const offer = $("push-offer");
  if (!pushConfigured || !("Notification" in window) ||
      !("serviceWorker" in navigator) || !("PushManager" in window)) {
    offer.hidden = true;
    return;
  }
  if (Notification.permission === "granted") {
    await subscribePush().catch(() => {});
    offer.hidden = true;
    return;
  }
  if (Notification.permission === "denied") { offer.hidden = true; return; }
  offer.hidden = false;
  $("enable-push").onclick = async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm === "granted") { await subscribePush(); offer.hidden = true; }
    } catch (e) { flash("Could not enable notifications: " + e.message, true); }
  };
}

async function subscribePush() {
  const reg = await navigator.serviceWorker.ready;
  const { publicKey } = await fetchJSON("/vapid-public-key");
  const wanted = b64ToUint8(publicKey);
  let sub = await reg.pushManager.getSubscription();
  // A subscription is bound to the key it was made with. If the server's key
  // has been rotated, reusing it leaves a subscription the server can never
  // push to and nothing ever replaces.
  if (sub && !sameKey(sub.options?.applicationServerKey, wanted)) {
    await sub.unsubscribe().catch(() => {});
    sub = null;
  }
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: wanted,
    });
  }
  await fetchJSON("/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
}

function sameKey(existing, wanted) {
  if (!existing) return false;               // unknown: assume it is stale
  const have = new Uint8Array(existing);
  return have.length === wanted.length &&
         have.every((byte, i) => byte === wanted[i]);
}

function b64ToUint8(base64) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

// --------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = body.detail || body.message || msg;
      if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join("; ");
    } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function flash(message, isError = false) {
  const el = $("flash");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  if (!isError) setTimeout(() => { el.hidden = true; }, 8000);
}

// Getting at a file without ever navigating.
//
// A link to a .md is a navigation, and what happens next is the browser's
// decision: iOS Safari navigates to it (or to a blob: URL) and shows a
// view-or-download sheet, from which there is no way back to the app. So
// tapping ".md" opens the text here instead, with the ways of taking it away
// offered from inside the page — copy, share, save — none of which navigate.
function bundleButton(href, label) {
  const button = document.createElement("button");
  button.className = "linkish";
  button.textContent = label;
  button.onclick = () => {
    button.disabled = true;
    takeBundle(href)
      .catch((e) => flash("Could not download that file: " + e.message, true))
      .finally(() => { button.disabled = false; });
  };
  return button;
}

async function takeBundle(href) {
  const res = await fetch(href);
  if (!res.ok) throw new Error(res.statusText || `HTTP ${res.status}`);
  const name = nameFromDisposition(res.headers.get("content-disposition"))
    || "footnote.zip";
  const blob = await res.blob();
  const file = new File([blob], name, { type: "application/zip" });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: name });
      return;
    }
  } catch (e) { /* the sheet was dismissed, or sharing is unavailable */ }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function saveFile(name, body) {
  const url = URL.createObjectURL(new Blob([body], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function nameFromDisposition(header) {
  if (!header) return "";
  const encoded = /filename\*=utf-8''([^;]+)/i.exec(header);
  if (encoded) {
    try { return decodeURIComponent(encoded[1]); } catch (e) { /* fall through */ }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : "";
}

function link(href, label) {
  const a = document.createElement("a");
  a.href = href;
  a.textContent = label;
  // Off-site: new tab, no opener handle. Decided by the origin the URL
  // actually resolves to, not by a prefix — "HTTP://example.com" is a
  // perfectly good absolute URL that starts with neither "http" nor
  // "https" as written, and would have replaced the app in its own tab.
  let elsewhere = false;
  try {
    elsewhere = new URL(href, location.href).origin !== location.origin;
  } catch (e) { /* not a URL we can resolve: treat it as our own */ }
  if (elsewhere) {
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  }
  return a;
}

function text(tag, content) {
  const el = document.createElement(tag);
  el.textContent = content;
  return el;
}

// A dossier is worth keeping, and a list of them is read weeks later: "11:12"
// alone answers a question nobody was asking. toLocaleString rather than two
// calls glued together, so the order is the reader's, not this file's.
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const parts = { month: "short", day: "numeric",
                  hour: "2-digit", minute: "2-digit" };
  if (d.getFullYear() !== new Date().getFullYear()) parts.year = "numeric";
  const stamp = text("span", d.toLocaleString([], parts));
  stamp.className = "when";
  stamp.title = d.toLocaleString();     // seconds and weekday, on hover
  return stamp;
}
