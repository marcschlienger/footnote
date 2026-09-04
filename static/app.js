// Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
// Licensed under the GNU AGPL v3.0 or later; see the LICENSE file.

/* Front-end logic: submit questions, poll job status, manage push. */

const $ = (id) => document.getElementById(id);
const ACTIVE = new Set(["queued", "researching", "archiving", "saving"]);
let pollTimer = null;
let pushConfigured = false;
// Which source panels the reader has open, and what they hold — polling
// re-renders the whole list, and an open panel must survive that.
const openSources = new Set();
const sourcesCache = new Map();
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
  try {
    const res = await fetchJSON("/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, processor: $("processor").value }),
    });
    $("question").value = "";
    flash(res.message + " — you can close this page; results land in your notes.");
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
  // Not fetchJSON: the service worker marks a list it served from cache
  // because the network was gone, and that is worth saying out loud.
  let data, fromCache = false;
  try {
    const res = await fetch("/jobs");
    if (!res.ok) throw new Error(res.statusText);
    fromCache = res.headers.get("X-Footnote-Cached") === "1";
    data = await res.json();
  } catch (e) { pollTimer = setTimeout(refreshJobs, 15000); return; }

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

  meta.appendChild(text("span", job.processor));
  meta.appendChild(text("span", when(job.created_at)));

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
    links.appendChild(link(`/jobs/${job.id}/report`, "Report"));
    links.appendChild(link(`/jobs/${job.id}/report.md`, ".md"));
    links.appendChild(sourcesToggle(job));
    links.appendChild(link(`/jobs/${job.id}/bundle.zip`, "Everything (.zip)"));
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
  button.onclick = () => {
    const panel = button.closest(".job").querySelector(".sources");
    panel.hidden = !panel.hidden;
    if (panel.hidden) openSources.delete(job.id);
    else { openSources.add(job.id); fillSources(job.id, panel); }
  };
  return button;
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
  foot.appendChild(link(data.bundle_url, "download report + sources (.zip)"));
  panel.appendChild(foot);
}

function renderSource(src) {
  const li = document.createElement("li");
  // Archived: read the local copy. Not archived: the live page is all there is.
  const title = src.archived ? link(src.read_url, src.title)
              : src.url ? link(src.url, src.title)
              : text("span", src.title);
  title.className = "src-title";
  li.appendChild(title);

  const row = document.createElement("span");
  row.className = "src-links";
  if (src.archived) {
    row.appendChild(link(src.download_url, ".md"));
    if (src.url) row.appendChild(link(src.url, "original ↗"));
    row.appendChild(text("span", size(src.bytes)));
  } else {
    row.appendChild(text("span",
      src.note ? `not archived — ${src.note}` : "not archived"));
  }
  li.appendChild(row);
  return li;
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
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const { publicKey } = await fetchJSON("/vapid-public-key");
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: b64ToUint8(publicKey),
    });
  }
  await fetchJSON("/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
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

function link(href, label) {
  const a = document.createElement("a");
  a.href = href;
  a.textContent = label;
  if (href.startsWith("http")) {         // off-site: new tab, no opener handle
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

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const days = (Date.now() - d.getTime()) / 86400000;
  if (days < 1) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
