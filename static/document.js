// Footnote — self-hosted deep-research server. Copyright (C) 2026 Marc Schlienger
// Licensed under the GNU AGPL v3.0 or later; see the LICENSE file.

/* The standalone report and source pages.

   Their "Download .md" is a link, and a link to a file is a navigation whose
   outcome the browser chooses: iOS Safari shows a view-or-download sheet and
   the page is gone. So the link is upgraded, where scripting is available,
   into the same thing the app does — show the text here, and offer the ways
   of taking it away from inside the page. Without scripting it stays an
   ordinary download link, which is the right fallback. */

document.addEventListener("DOMContentLoaded", () => {
  for (const anchor of document.querySelectorAll("a[data-file]")) {
    const href = anchor.getAttribute("href");
    anchor.addEventListener("click", (event) => {
      event.preventDefault();
      if (anchor.dataset.file === "text") {
        toggle(anchor, href);
      } else {
        takeBinary(href).catch(() => {});
      }
    });
  }
});

// The server names the file; the URL's last segment is "report.md" for every
// dossier there has ever been.
function nameFromDisposition(header, fallback) {
  if (header) {
    const encoded = /filename\*=utf-8''([^;]+)/i.exec(header);
    if (encoded) {
      try { return decodeURIComponent(encoded[1]); } catch (e) { /* below */ }
    }
    const plain = /filename="([^"]+)"/i.exec(header);
    if (plain) return plain[1];
  }
  return fallback;
}

async function takeBinary(href) {
  const res = await fetch(href);
  if (!res.ok) throw new Error(res.statusText);
  const name = nameFromDisposition(res.headers.get("content-disposition"),
                                   "footnote.zip");
  const blob = await res.blob();
  const file = new File([blob], name, { type: "application/zip" });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: name });
      return;
    }
  } catch (e) { /* dismissed, or unavailable */ }
  saveBlob(blob, name);
}

function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function toggle(anchor, href) {
  const existing = document.querySelector(".file-view");
  if (existing) {
    existing.remove();
    anchor.setAttribute("aria-expanded", "false");
    return;
  }
  anchor.setAttribute("aria-expanded", "true");
  const panel = document.createElement("div");
  panel.className = "file-view";
  panel.textContent = "Loading…";
  anchor.closest("p").after(panel);
  try {
    const res = await fetch(href);
    if (!res.ok) throw new Error(res.statusText || `HTTP ${res.status}`);
    const name = nameFromDisposition(res.headers.get("content-disposition"),
                                     "dossier.md");
    const body = await res.text();
    panel.textContent = "";
    panel.appendChild(actions(name, body));
    const pre = document.createElement("pre");
    pre.className = "file-text";
    pre.textContent = body;
    panel.appendChild(pre);
  } catch (e) {
    panel.textContent = "Could not read that file: " + e.message;
  }
}

function actions(name, body) {
  const row = document.createElement("div");
  row.className = "file-actions";
  const label = document.createElement("span");
  label.textContent = name;
  row.appendChild(label);
  row.appendChild(button("Copy", () => navigator.clipboard.writeText(body)));
  const file = () => new File([body], name, { type: "text/markdown" });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file()] })) {
      row.appendChild(button("Share", () =>
        navigator.share({ files: [file()], title: name })));
    }
  } catch (e) { /* sharing is unavailable here */ }
  row.appendChild(button("Save", () =>
    saveBlob(new Blob([body], { type: "text/markdown" }), name)));
  return row;
}

function button(label, run) {
  const el = document.createElement("button");
  el.className = "linkish";
  el.textContent = label;
  el.onclick = () => Promise.resolve().then(run).catch(() => {});
  return el;
}
