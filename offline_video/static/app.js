const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mediaUrl = (rel) => "/media/" + rel.split("/").map(encodeURIComponent).join("/");

function fmtDuration(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}
function fmtBytes(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + " " + u[i];
}

// ---- status, save location, opening files ------------------------------
const AUDIO = ["mp3", "m4a", "wav", "flac", "opus", "ogg"];
const BROWSER_PLAYABLE = ["mp4", "webm", "m4a", "mp3", "aac", "wav", "flac", "opus", "ogg", "mov"];
let hasFfmpeg = true;

async function loadStatus() {
  const st = await (await fetch("/api/status")).json();
  hasFfmpeg = st.ffmpeg;
  $("#library-dir").textContent = st.library_dir;
  $("#ffmpeg-warning").hidden = st.ffmpeg;
  updateFormatNote();
}

function updateFormatNote() {
  const out = $("#output").value;
  $("#quality").disabled = AUDIO.includes(out);
  const notes = {
    mkv: "MKV keeps the original streams; most browsers can't play it here, use Open file (VLC, IINA…).",
    webm: "WebM may be re-encoded if the video isn't available as WebM, which takes longer.",
    mov: "MOV plays in QuickTime and Safari.",
    avi: "AVI needs re-encoding, which takes a while. Use Open file to watch it.",
    mpeg: "MPEG needs re-encoding, which takes a while. Use Open file to watch it.",
  };
  let note = notes[out] || "";
  if (!hasFfmpeg && !["mp4", "m4a"].includes(out)) note = "Needs FFmpeg (see above). Without it you'll get MP4/M4A instead.";
  $("#format-note").textContent = note;
}
$("#output").addEventListener("change", updateFormatNote);

async function openPath(path, reveal) {
  const res = await fetch("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, reveal }),
  });
  if (res.status === 403) alert("Files can only be opened from a browser on the computer running Offline Video.");
  else if (!res.ok) alert((await res.json().catch(() => ({}))).error || "Couldn't open it.");
}

$("#open-library").addEventListener("click", () => openPath("", false));
$("#change-library").addEventListener("click", async () => {
  const dir = prompt("Save downloads to this folder (full path, e.g. /Users/you/Movies/YouTube):", $("#library-dir").textContent);
  if (!dir) return;
  const res = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ library_dir: dir }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return alert(data.error || "Couldn't use that folder.");
  await loadStatus();
  loadLibrary();
});

// Buttons with data-open / data-reveal anywhere on the page open files in the OS.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-open],[data-reveal]");
  if (!b) return;
  e.preventDefault();
  if (b.dataset.open !== undefined) openPath(b.dataset.open, false);
  else openPath(b.dataset.reveal, true);
});

// ---- adding downloads --------------------------------------------------
function formOptions() {
  return { quality: $("#quality").value, output: $("#output").value, subtitles: $("#subtitles").checked };
}

function showResult(ok, message) {
  $("#form-error").hidden = ok;
  $("#form-ok").hidden = !ok;
  $(ok ? "#form-ok" : "#form-error").textContent = message;
}

async function handleQueued(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    showResult(false, data.error || (res.status === 413 ? "That file is too large (max 5 MB)." : "Could not add download"));
    return false;
  }
  showResult(true, data.queued === 1 ? "Added to the download queue." : `Queued ${data.queued} downloads.`);
  pollJobs();
  return true;
}

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await fetch("/api/downloads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: $("#url").value, ...formOptions() }),
  });
  if (await handleQueued(res)) $("#url").value = "";
});

async function uploadList(file) {
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  for (const [k, v] of Object.entries(formOptions())) body.append(k, v);
  await handleQueued(await fetch("/api/downloads/file", { method: "POST", body }));
}

$("#upload-btn").addEventListener("click", () => $("#upload-file").click());
$("#upload-file").addEventListener("change", async (e) => {
  await uploadList(e.target.files[0]);
  e.target.value = "";
});
const urlBox = $("#url");
urlBox.addEventListener("dragover", (e) => { e.preventDefault(); urlBox.classList.add("dragover"); });
urlBox.addEventListener("dragleave", () => urlBox.classList.remove("dragover"));
urlBox.addEventListener("drop", (e) => {
  urlBox.classList.remove("dragover");
  if (!e.dataTransfer.files.length) return; // dropped text/links go into the box as usual
  e.preventDefault();
  uploadList(e.dataTransfer.files[0]);
});

$("#clear-jobs").addEventListener("click", async () => {
  await fetch("/api/jobs/clear", { method: "POST" });
  pollJobs();
});
$("#cancel-all").addEventListener("click", async () => {
  if (!confirm("Cancel all queued and running downloads?")) return;
  await fetch("/api/jobs/cancel-all", { method: "POST" });
  pollJobs();
});

// ---- job list ----------------------------------------------------------
let lastDoneCount = -1;
let pollTimer = null;
const openLists = new Set(); // playlist video lists the user has expanded

const ITEM_ICON = { pending: "·", downloading: "⬇", done: "✓", skipped: "↺", failed: "✕", cancelled: "–" };

function renderItems(j) {
  if (!j.items.length) return "";
  const counts = {};
  for (const it of j.items) counts[it.status] = (counts[it.status] || 0) + 1;
  const summary = [
    counts.done && `${counts.done} downloaded`,
    counts.skipped && `${counts.skipped} already in library`,
    counts.failed && `${counts.failed} failed`,
    counts.pending && `${counts.pending} waiting`,
  ].filter(Boolean).join(", ");
  const rows = j.items.map((it) => `<li class="${esc(it.status)}" title="${esc(it.status)}">
      <span class="n">${it.index}.</span><span>${ITEM_ICON[it.status] || ""}</span><span class="it">${esc(it.title)}</span>
      ${it.error ? `<span class="ie">${esc(it.error)}</span>` : ""}</li>`).join("");
  return `<details data-job="${esc(j.id)}" ${openLists.has(j.id) ? "open" : ""}>
    <summary>Videos in this playlist (${j.items.length})${summary ? " — " + esc(summary) : ""}</summary>
    <ol>${rows}</ol></details>`;
}

function renderJob(j) {
  const active = j.status === "downloading" || j.status === "processing";
  let overall = j.status === "done" ? 100 : j.percent;
  if (j.is_playlist && j.total_items) {
    const finished = j.items.length ? j.items.filter((i) => !["pending", "downloading"].includes(i.status)).length : j.completed_items;
    overall = j.status === "done" ? 100 : ((finished + (active ? j.percent / 100 : 0)) / j.total_items) * 100;
  }
  const parts = [];
  if (j.status === "queued") parts.push("Waiting in queue");
  if (j.is_playlist) parts.push(`${j.completed_items}/${j.total_items} videos`);
  if (active && j.current_item && j.is_playlist) parts.push("Now: " + j.current_item);
  if (j.status === "downloading" && j.percent) parts.push(j.percent.toFixed(0) + "%");
  if (j.speed) parts.push(fmtBytes(j.speed) + "/s");
  if (j.eta) parts.push("ETA " + fmtDuration(j.eta));
  if (j.status === "processing") parts.push("Converting…");
  parts.push(j.output.toUpperCase());
  const canCancel = j.status === "queued" || active;
  return `<div class="card job ${esc(j.status)}">
    <div>
      <div class="title">${esc(j.title || j.url)}</div>
      <div class="sub">${esc(j.url)}</div>
      <div class="sub">${esc(parts.join(" · "))}</div>
      ${j.error ? `<div class="sub msg">${esc(j.error)}</div>` : ""}
      ${jobLinks(j)}
    </div>
    <div>
      <span class="badge">${esc(j.status)}</span>
      ${canCancel ? `<button class="link" data-cancel="${esc(j.id)}">Cancel</button>` : ""}
    </div>
    ${renderItems(j)}
    <div class="bar"><div style="width:${Math.min(100, overall).toFixed(1)}%"></div></div>
  </div>`;
}

function jobLinks(j) {
  if (!j.files.length) return "";
  if (j.files.length === 1) {
    const f = j.files[0];
    return `<div class="job-links"><button class="link" data-open="${esc(f)}">Open file</button>
      <button class="link" data-reveal="${esc(f)}">Show in folder</button></div>`;
  }
  return `<div class="job-links"><button class="link" data-reveal="${esc(j.files[0])}">Show in folder</button></div>`;
}

async function pollJobs() {
  clearTimeout(pollTimer);
  try {
    const { jobs } = await (await fetch("/api/jobs")).json();
    const list = $("#jobs");
    const scroll = [...list.querySelectorAll("details ol")].map((ol) => [ol.closest("details").dataset.job, ol.scrollTop]);
    list.innerHTML = jobs.length ? jobs.map(renderJob).join("") : '<p class="muted">Nothing queued.</p>';
    for (const [id, top] of scroll) {
      const ol = list.querySelector(`details[data-job="${id}"] ol`);
      if (ol) ol.scrollTop = top;
    }
    // Refresh the library whenever another video finishes.
    const doneCount = jobs.reduce((n, j) => n + j.completed_items + (j.status === "done" ? 1 : 0), 0);
    if (doneCount !== lastDoneCount) { lastDoneCount = doneCount; loadLibrary(); }
    const busy = jobs.some((j) => ["queued", "downloading", "processing"].includes(j.status));
    pollTimer = setTimeout(pollJobs, busy ? 1000 : 5000);
  } catch {
    pollTimer = setTimeout(pollJobs, 5000);
  }
}

$("#jobs").addEventListener("toggle", (e) => {
  const id = e.target.dataset?.job;
  if (!id) return;
  if (e.target.open) openLists.add(id); else openLists.delete(id);
}, true);

$("#jobs").addEventListener("click", async (e) => {
  const id = e.target.dataset.cancel;
  if (!id) return;
  await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
  pollJobs();
});

// ---- library -----------------------------------------------------------
let library = [];
let playlistQueue = [];
let playingIndex = -1;

async function loadLibrary() {
  library = (await (await fetch("/api/library")).json()).items;
  renderLibrary();
}

function renderLibrary() {
  const q = $("#search").value.trim().toLowerCase();
  const items = library.filter((i) => !q || [i.title, i.channel, i.playlist].join(" ").toLowerCase().includes(q));
  if (!items.length) {
    $("#library").innerHTML = `<p class="muted">${library.length ? "No matches." : "Your downloaded videos will appear here."}</p>`;
    return;
  }
  const groups = new Map();
  for (const i of items) {
    const key = i.playlist || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  $("#library").innerHTML = keys.map((k) => `
    <h3 class="group-title">${k ? "📃 " + esc(k) + ` (${groups.get(k).length})` : "Videos"}</h3>
    <div class="grid">${groups.get(k).map(tile).join("")}</div>`).join("");
}

function tile(i) {
  return `<div class="tile">
    <button class="thumb" data-play="${esc(i.id)}" title="Play">
      ${i.thumbnail_url ? `<img loading="lazy" src="${mediaUrl(i.thumbnail_url)}" alt="">` : `<span class="noimg">${i.is_audio ? "♪" : "▶"}</span>`}
      ${i.duration ? `<span class="dur">${fmtDuration(i.duration)}</span>` : ""}
    </button>
    <div class="info">
      <div class="t" title="${esc(i.title)}">${i.playlist_index ? i.playlist_index + ". " : ""}${esc(i.title)}</div>
      <div class="m">${esc([i.channel, fmtBytes(i.size)].filter(Boolean).join(" · "))}<span class="fmt">${esc(i.format.toUpperCase())}</span></div>
      <div class="path" title="${esc(i.path)}">${esc(i.path)}</div>
      <div class="actions">
        <button class="link" data-open="${esc(i.media_url)}">Open file</button>
        <button class="link" data-reveal="${esc(i.media_url)}">Show in folder</button>
        <button class="link del" data-delete="${esc(i.id)}">Delete</button>
      </div>
    </div>
  </div>`;
}

$("#search").addEventListener("input", renderLibrary);

$("#library").addEventListener("click", async (e) => {
  const playBtn = e.target.closest("[data-play]");
  if (playBtn) {
    const item = library.find((i) => i.id === playBtn.dataset.play);
    // Formats browsers can't play (MKV, AVI, MPEG…) open in the computer's own player instead.
    if (item && !BROWSER_PLAYABLE.includes(item.format)) return openPath(item.media_url, false);
    return play(playBtn.dataset.play);
  }
  const del = e.target.dataset.delete;
  if (del) {
    const item = library.find((i) => i.id === del);
    if (!confirm(`Delete "${item?.title}" from this computer?`)) return;
    await fetch("/api/library/" + del.split("/").map(encodeURIComponent).join("/"), { method: "DELETE" });
    loadLibrary();
  }
});

// ---- player ------------------------------------------------------------
const video = $("#video");
const dialog = $("#player");

function play(id) {
  const item = library.find((i) => i.id === id);
  if (!item) return;
  // Next/previous move through the same playlist (or the loose videos).
  playlistQueue = library.filter((i) => (i.playlist || "") === (item.playlist || "") && BROWSER_PLAYABLE.includes(i.format));
  playingIndex = playlistQueue.indexOf(item);
  load(item);
  if (!dialog.open) dialog.showModal();
}

function load(item) {
  video.querySelectorAll("track").forEach((t) => t.remove());
  video.src = mediaUrl(item.media_url);
  if (item.is_audio && item.thumbnail_url) video.poster = mediaUrl(item.thumbnail_url); else video.removeAttribute("poster");
  item.subtitles.forEach((s, n) => {
    const t = document.createElement("track");
    Object.assign(t, { kind: "subtitles", label: s.lang, srclang: s.lang.slice(0, 2), src: mediaUrl(s.url), default: n === 0 });
    video.appendChild(t);
  });
  $("#player-title").textContent = item.title;
  $("#player-meta").textContent = [item.channel, item.playlist, fmtDuration(item.duration)].filter(Boolean).join(" · ");
  $("#prev").disabled = playingIndex <= 0;
  $("#next").disabled = playingIndex >= playlistQueue.length - 1;
  const resumeKey = "pos:" + item.id;
  video.onloadedmetadata = () => {
    const pos = parseFloat(localStorage.getItem(resumeKey) || "0");
    if (pos > 5 && pos < video.duration - 5) video.currentTime = pos;
  };
  video.ontimeupdate = () => { try { localStorage.setItem(resumeKey, video.currentTime); } catch {} };
  video.play().catch(() => {});
}

function step(d) {
  const n = playingIndex + d;
  if (n < 0 || n >= playlistQueue.length) return;
  playingIndex = n;
  load(playlistQueue[n]);
}

video.addEventListener("error", () => {
  $("#player-meta").textContent = "This browser can't play this file. Use \"Open file\" to watch it in your computer's player.";
});
video.addEventListener("ended", () => {
  try { localStorage.removeItem("pos:" + playlistQueue[playingIndex].id); } catch {}
  if ($("#autoplay").checked) step(1);
});
$("#prev").addEventListener("click", () => step(-1));
$("#next").addEventListener("click", () => step(1));
$("#player-close").addEventListener("click", () => dialog.close());
$("#player-open").addEventListener("click", () => openPath(playlistQueue[playingIndex].media_url, false));
dialog.addEventListener("close", () => { video.pause(); });

loadStatus();
pollJobs();
