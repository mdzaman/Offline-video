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

// ---- adding downloads --------------------------------------------------
$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#form-error");
  err.hidden = true;
  const res = await fetch("/api/downloads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: $("#url").value, quality: $("#quality").value, subtitles: $("#subtitles").checked }),
  });
  if (!res.ok) {
    err.textContent = (await res.json().catch(() => ({}))).error || "Could not add download";
    err.hidden = false;
    return;
  }
  $("#url").value = "";
  pollJobs();
});

$("#clear-jobs").addEventListener("click", async () => {
  await fetch("/api/jobs/clear", { method: "POST" });
  pollJobs();
});

// ---- job list ----------------------------------------------------------
let lastDoneCount = -1;
let pollTimer = null;

function renderJob(j) {
  const active = j.status === "downloading" || j.status === "processing";
  let overall = j.status === "done" ? 100 : j.percent;
  if (j.is_playlist && j.total_items) {
    overall = j.status === "done" ? 100 : ((j.completed_items + (active ? j.percent / 100 : 0)) / j.total_items) * 100;
  }
  const parts = [];
  if (j.is_playlist) parts.push(`${j.completed_items}/${j.total_items} videos`);
  if (active && j.current_item && j.is_playlist) parts.push("Now: " + j.current_item);
  if (j.status === "downloading" && j.percent) parts.push(j.percent.toFixed(0) + "%");
  if (j.speed) parts.push(fmtBytes(j.speed) + "/s");
  if (j.eta) parts.push("ETA " + fmtDuration(j.eta));
  if (j.status === "processing") parts.push("Merging audio and video…");
  const canCancel = j.status === "queued" || active;
  return `<div class="card job ${esc(j.status)}">
    <div>
      <div class="title">${esc(j.title || j.url)}</div>
      <div class="sub">${esc(parts.join(" · ") || j.url)}</div>
      ${j.error ? `<div class="sub msg">${esc(j.error)}</div>` : ""}
    </div>
    <div>
      <span class="badge">${esc(j.status)}</span>
      ${canCancel ? `<button class="link" data-cancel="${esc(j.id)}">Cancel</button>` : ""}
    </div>
    <div class="bar"><div style="width:${Math.min(100, overall).toFixed(1)}%"></div></div>
  </div>`;
}

async function pollJobs() {
  clearTimeout(pollTimer);
  try {
    const { jobs } = await (await fetch("/api/jobs")).json();
    $("#jobs").innerHTML = jobs.length ? jobs.map(renderJob).join("") : '<p class="muted">Nothing queued.</p>';
    // Refresh the library whenever another video finishes.
    const doneCount = jobs.reduce((n, j) => n + j.completed_items + (j.status === "done" ? 1 : 0), 0);
    if (doneCount !== lastDoneCount) { lastDoneCount = doneCount; loadLibrary(); }
    const busy = jobs.some((j) => ["queued", "downloading", "processing"].includes(j.status));
    pollTimer = setTimeout(pollJobs, busy ? 1000 : 5000);
  } catch {
    pollTimer = setTimeout(pollJobs, 5000);
  }
}

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
      <div class="m">${esc([i.channel, fmtBytes(i.size)].filter(Boolean).join(" · "))}</div>
      <div class="actions">
        <a href="${mediaUrl(i.media_url)}" download>Save file</a>
        <button class="link" data-delete="${esc(i.id)}">Delete</button>
      </div>
    </div>
  </div>`;
}

$("#search").addEventListener("input", renderLibrary);

$("#library").addEventListener("click", async (e) => {
  const playBtn = e.target.closest("[data-play]");
  if (playBtn) return play(playBtn.dataset.play);
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
  playlistQueue = library.filter((i) => (i.playlist || "") === (item.playlist || ""));
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
  $("#player-meta").textContent = "This browser can't play this file's format. Use \"Save file\" and open it in a media player such as VLC.";
});
video.addEventListener("ended", () => {
  try { localStorage.removeItem("pos:" + playlistQueue[playingIndex].id); } catch {}
  if ($("#autoplay").checked) step(1);
});
$("#prev").addEventListener("click", () => step(-1));
$("#next").addEventListener("click", () => step(1));
$("#player-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => { video.pause(); });

pollJobs();
