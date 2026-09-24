// ── State ──────────────────────────────────────────────────────────────────
let focusedArtist = null;
let sidebarFilter = "";
let sidebarSearch = "";
let sidebarArtists = [];   // current rendered list (used for prev/next)
let historyFilter = "";

let seekingScrub = false;

// Play queue for "Listen to More"
let trackQueue = [];     // [{title, rating_key, stream_key}, ...]
let queueIndex = -1;     // index into trackQueue currently playing (-1 = triage track)

// ── DOM refs ───────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

const sectionCurrent  = $("section-current");
const sectionDone     = $("section-done");
const sectionEmpty    = $("section-empty");
const sectionStats    = $("section-stats");
const sectionHistory  = $("section-history");

const artistThumb      = $("artist-thumb");
const artistName       = $("artist-name");
const trackTitle       = $("track-title");
const sourceBadge      = $("source-badge");
const deleteConfirm    = $("delete-confirm");
const deleteArtistName = $("delete-artist-name");
const artistBioWrap    = $("artist-bio-wrap");
const artistBio        = $("artist-bio");

const audio        = $("audio");
const btnPlayPause = $("btn-playpause");
const audioSeek    = $("audio-seek");
const audioCurrent = $("audio-current");
const audioDuration= $("audio-duration");
const audioStatus  = $("audio-status");

const genProgressWrap  = $("gen-progress-wrap");
const genProgressBar   = $("gen-progress-bar");
const genProgressLabel = $("gen-progress-label");

// ── Modal ──────────────────────────────────────────────────────────────────

function showModal(title, message, buttons) {
  // buttons: [{label, primary, danger, action}]
  $("modal-title").textContent = title;
  $("modal-message").textContent = message;
  const wrap = $("modal-buttons");
  wrap.innerHTML = "";
  buttons.forEach(b => {
    const btn = document.createElement("button");
    btn.textContent = b.label;
    btn.className = "btn " + (b.danger ? "btn-delete" : b.primary ? "btn-secondary" : "btn-ghost");
    btn.addEventListener("click", () => {
      $("modal-backdrop").classList.add("hidden");
      b.action();
    });
    wrap.appendChild(btn);
  });
  $("modal-backdrop").classList.remove("hidden");
}

$("modal-backdrop").addEventListener("click", e => {
  if (e.target === $("modal-backdrop")) $("modal-backdrop").classList.add("hidden");
});

// ── Audio player ───────────────────────────────────────────────────────────

// Persist volume
audio.volume = parseFloat(localStorage.getItem("volume") ?? "1");
audio.addEventListener("volumechange", () => localStorage.setItem("volume", String(audio.volume)));

function fmtTime(secs) {
  if (!isFinite(secs)) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

function loadAudio(artistId, autoplay = true) {
  // Reset queue — we're starting fresh on this artist's triage track
  trackQueue = [];
  queueIndex = -1;
  audio.src = `/api/stream/${artistId}`;
  audio.load();
  audioStatus.textContent = "Loading…";
  btnPlayPause.textContent = "▶";
  audioSeek.value = 0;
  audioSeek.style.setProperty("--pct", "0%");
  audioCurrent.textContent = "0:00";
  audioDuration.textContent = "0:00";
  if (autoplay) audio.play().catch(() => {});
}

function loadQueueTrack(index) {
  const track = trackQueue[index];
  if (!track || !track.rating_key) {
    audioStatus.textContent = "No more tracks";
    return;
  }
  const url = `/api/tracks/${encodeURIComponent(track.rating_key)}/stream`;
  audio.src = url;
  audio.load();
  audioStatus.textContent = "";
  trackTitle.textContent = track.title;
  btnPlayPause.textContent = "▶";
  audioSeek.value = 0;
  audioSeek.style.setProperty("--pct", "0%");
  audioCurrent.textContent = "0:00";
  audioDuration.textContent = "0:00";
  audio.play().catch(() => {});
}

function resetAudio() {
  audio.pause();
  audio.src = "";
  trackQueue = [];
  queueIndex = -1;
  btnPlayPause.textContent = "▶";
  audioSeek.value = 0;
  audioSeek.style.setProperty("--pct", "0%");
  audioCurrent.textContent = "0:00";
  audioDuration.textContent = "0:00";
  audioStatus.textContent = "";
}

audio.addEventListener("canplay",  () => { audioStatus.textContent = ""; });
audio.addEventListener("waiting",  () => { audioStatus.textContent = "Buffering…"; });
audio.addEventListener("error",    () => { audioStatus.textContent = "Couldn't load track"; });
audio.addEventListener("play",     () => { btnPlayPause.textContent = "⏸"; });
audio.addEventListener("pause",    () => { btnPlayPause.textContent = "▶"; });
audio.addEventListener("ended", () => {
  btnPlayPause.textContent = "▶";
  if (trackQueue.length > 0 && queueIndex + 1 < trackQueue.length) {
    queueIndex++;
    loadQueueTrack(queueIndex);
  } else if (trackQueue.length > 0) {
    audioStatus.textContent = "End of tracks";
  } else {
    audioStatus.textContent = "Track ended";
  }
});

audio.addEventListener("timeupdate", () => {
  if (seekingScrub) return;
  audioCurrent.textContent = fmtTime(audio.currentTime);
  if (audio.duration) {
    const pct = (audio.currentTime / audio.duration) * 100;
    audioSeek.value = pct;
    audioSeek.style.setProperty("--pct", pct + "%");
  }
});
audio.addEventListener("durationchange", () => { audioDuration.textContent = fmtTime(audio.duration); });

btnPlayPause.addEventListener("click", () => {
  if (audio.paused) audio.play(); else audio.pause();
});

audioSeek.addEventListener("mousedown",  () => { seekingScrub = true; });
audioSeek.addEventListener("touchstart", () => { seekingScrub = true; }, { passive: true });
audioSeek.addEventListener("input", () => {
  if (audio.duration) audioCurrent.textContent = fmtTime((audioSeek.value / 100) * audio.duration);
});
audioSeek.addEventListener("change", () => {
  seekingScrub = false;
  if (audio.duration) {
    audio.currentTime = (audioSeek.value / 100) * audio.duration;
    audioSeek.style.setProperty("--pct", audioSeek.value + "%");
  }
  audioSeek.blur();
});

// ── Helpers ────────────────────────────────────────────────────────────────

// Sent on every request; the server rejects state-changing requests without it (CSRF protection)
const CSRF_HEADERS = { "X-Faderr-Request": "1" };

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...CSRF_HEADERS, ...(opts.headers || {}) } });
  if (!res.ok) {
    const text = await res.text();
    if (res.status === 503 && text.includes("setup_required")) window.location = "/settings";
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Sidebar ────────────────────────────────────────────────────────────────

let sidebarSeq = 0;

async function loadSidebar() {
  const seq = ++sidebarSeq;
  const params = new URLSearchParams();
  if (sidebarFilter) params.set("status", sidebarFilter);
  if (sidebarSearch) params.set("search", sidebarSearch);
  let artists;
  try {
    artists = await api(`/api/artists?${params}`);
  } catch(e) { return; }
  if (seq !== sidebarSeq) return;  // a newer search/filter has been requested
  sidebarArtists = artists;

  const list = $("sidebar-list");
  list.innerHTML = "";
  sidebarArtists.forEach(a => {
    const li = document.createElement("li");
    li.className = "sidebar-item" + (focusedArtist && focusedArtist.id === a.id ? " active" : "");
    li.dataset.id = a.id;
    const dotClass = a.decision ? `dot-${a.decision}` : "dot-undecided";
    li.innerHTML = `<span class="status-dot ${dotClass}"></span><span class="sidebar-item-name">${esc(a.artist_name)}</span>`;
    li.addEventListener("click", () => focusArtist(a.id));
    list.appendChild(li);
  });
}

function updateSidebarActive() {
  document.querySelectorAll(".sidebar-item").forEach(li => {
    li.classList.toggle("active", focusedArtist && parseInt(li.dataset.id) === focusedArtist.id);
  });
  // Scroll active item into view
  const active = document.querySelector(".sidebar-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

// ── Artist bio ─────────────────────────────────────────────────────────────

async function loadBio(artistId) {
  try {
    const data = await api(`/api/artists/${artistId}/bio`);
    if (!focusedArtist || focusedArtist.id !== artistId) return;  // user moved on
    if (data.bio) {
      artistBio.textContent = data.bio;
      artistBioWrap.classList.remove("hidden");
    }
  } catch(e) {
    // Bio is nice-to-have — fail silently
  }
}

// ── Focus an artist ────────────────────────────────────────────────────────

let focusSeq = 0;

async function focusArtist(id, autoplay = true) {
  // Close mobile drawer when an artist is selected
  if (window.innerWidth <= 768) closeMobileDrawer();

  const seq = ++focusSeq;
  let data;
  try { data = await api(`/api/artists/${id}`); }
  catch(e) { console.error("focusArtist:", e); return; }
  if (seq !== focusSeq) return;  // a newer navigation superseded this one

  const artistChanged = !focusedArtist || focusedArtist.id !== data.id;
  focusedArtist = data;
  if (data.decision === "delete") loadStats();  // its delete may have finished or failed since

  sectionEmpty.classList.add("hidden");
  sectionDone.classList.add("hidden");
  sectionCurrent.classList.remove("hidden");
  deleteConfirm.classList.add("hidden");

  artistName.textContent  = data.artist_name;
  trackTitle.textContent  = data.track_title || "Unknown track";
  sourceBadge.textContent = data.source === "lastfm" ? "Last.fm top track" : "Random track";

  // Load bio async — clear first so stale bio doesn't linger
  artistBioWrap.classList.add("hidden");
  artistBio.textContent = "";
  if (artistChanged) loadBio(data.id);

  if (data.thumb) { artistThumb.src = data.thumb; artistThumb.style.display = ""; }
  else { artistThumb.src = ""; artistThumb.style.display = "none"; }

  updateActionButtons();

  if (artistChanged && data.stream_key) {
    loadAudio(data.id, autoplay);
  } else if (!data.stream_key) {
    resetAudio();
    audioStatus.textContent = "No stream available";
  }

  updateSidebarActive();
}

// ── Navigation (prev / next) ───────────────────────────────────────────────

async function navigateDir(dir) {
  if (!sidebarArtists.length) return;
  if (!focusedArtist) { await focusArtist(sidebarArtists[0].id); return; }
  const idx = sidebarArtists.findIndex(a => a.id === focusedArtist.id);
  const next = idx + dir;
  if (next >= 0 && next < sidebarArtists.length) {
    await focusArtist(sidebarArtists[next].id);
  }
}

$("btn-prev").addEventListener("click", () => navigateDir(-1));
$("btn-next").addEventListener("click", () => navigateDir(1));

// ── Load initial view (first undecided) ────────────────────────────────────

async function loadInitial(autoplay = true) {
  const data = await api("/api/artists/current").catch(() => null);
  if (!data) { sectionEmpty.classList.remove("hidden"); return; }
  if (data.done) { sectionCurrent.classList.add("hidden"); sectionDone.classList.remove("hidden"); return; }
  await focusArtist(data.id, autoplay);
}

// ── Stats ──────────────────────────────────────────────────────────────────

async function loadStats() {
  let s;
  try { s = await api("/api/stats"); } catch(e) { return; }
  if (s.total === 0) { sectionStats.classList.add("hidden"); return; }
  sectionStats.classList.remove("hidden");
  const pct = s.total > 0 ? Math.round((s.triaged / s.total) * 100) : 0;
  $("stats-label").textContent = `${s.triaged} / ${s.total} artists triaged`;
  $("progress-bar").style.width = pct + "%";
  let breakdown = `Keep: ${s.keep}  ·  Deleted: ${s.deleted}`;
  if (s.delete_pending) breakdown += `  ·  ${s.delete_pending} waiting to delete`;
  if (s.delete_failed) breakdown += `  ·  ${s.delete_failed} failed to delete`;
  $("stats-breakdown").textContent = breakdown;
}

// ── Deletion status ────────────────────────────────────────────────────────
// Deletes are queued jobs: "pending" during the grace period (undoable),
// then "running", then "done" or "failed" (retry or undo).

function fmtClock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function deletionLabel(job) {
  if (!job || job.status === "done") return "Deleted";
  if (job.status === "pending") return `Deleting at ${fmtClock(job.run_after)}`;
  if (job.status === "running") return "Deleting…";
  if (job.status === "failed") return "Delete failed";
  return "Deleted";
}

function deletionSentence(job) {
  if (!job || job.status === "done") return "This artist has been deleted. Its files are gone, so no other decision can be made.";
  if (job.status === "pending") return `Scheduled for deletion at ${fmtClock(job.run_after)}. You can undo it until then.`;
  if (job.status === "running") return "Deleting now…";
  if (job.status === "failed") return `The delete failed and nothing more will happen until you retry or undo it. ${job.error || ""}`;
  return "This artist has been deleted.";
}

async function undoArtist(artistId) {
  await api(`/api/artists/${artistId}/undo`, { method: "POST" });
}

async function retryDeletion(jobId) {
  await api(`/api/deletions/${jobId}/retry`, { method: "POST" });
}

function errorModal(e) {
  showModal("Error", e.message, [{label:"OK", primary:true, action:()=>{}}]);
}

// ── History ────────────────────────────────────────────────────────────────

let historySeq = 0;

async function loadHistory() {
  const seq = ++historySeq;
  const url = historyFilter ? `/api/artists/history?decision=${encodeURIComponent(historyFilter)}` : "/api/artists/history";
  let artists;
  try { artists = await api(url); } catch(e) { return; }
  if (seq !== historySeq) return;
  const list = $("history-list");
  list.innerHTML = "";
  if (artists.length === 0) {
    list.innerHTML = '<li style="color:var(--text-muted);padding:0.5rem">No entries yet.</li>';
    return;
  }
  artists.forEach(a => {
    const li = document.createElement("li");
    li.className = "history-item";
    const job = a.deletion;
    const isDelete = a.decision === "delete";
    const label = isDelete ? deletionLabel(job) : "Keep";
    const canUndo = !isDelete || (job && job.cancellable);
    const canRetry = isDelete && job && job.status === "failed";
    const title = isDelete && job && job.error ? ` title="${esc(job.error)}"` : "";
    li.innerHTML = `
      <span>${esc(a.artist_name)}</span>
      <div style="display:flex;align-items:center;gap:0.4rem">
        <span class="history-decision decision-${esc(a.decision)}${canRetry ? " decision-failed" : ""}"${title}>${esc(label)}</span>
        ${canRetry ? `<button class="history-undo history-retry" data-job="${job.id}">Retry</button>` : ""}
        ${canUndo ? `<button class="history-undo history-undo-btn" data-id="${a.id}">Undo</button>` : ""}
      </div>`;
    list.appendChild(li);
  });
  list.querySelectorAll(".history-undo-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await undoArtist(btn.dataset.id); await refresh(); await loadHistory(); }
      catch(e) { errorModal(e); btn.disabled = false; }
    });
  });
  list.querySelectorAll(".history-retry").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await retryDeletion(btn.dataset.job); await loadHistory(); await loadStats(); }
      catch(e) { errorModal(e); btn.disabled = false; }
    });
  });
}

// ── Full refresh ───────────────────────────────────────────────────────────

async function refresh() {
  await Promise.all([loadStats(), loadSidebar()]);
  // After a decision, advance to next undecided
  const next = await api("/api/artists/current").catch(() => null);
  if (!next) return;
  if (next.done) {
    sectionCurrent.classList.add("hidden");
    sectionDone.classList.remove("hidden");
    resetAudio();
    focusedArtist = null;
    updateActionButtons();
    updateSidebarActive();
  } else if (!focusedArtist || focusedArtist.id !== next.id) {
    await focusArtist(next.id);
  }
}

// ── Decision helpers ───────────────────────────────────────────────────────

// Only one decision at a time: a second click or key press while a (possibly
// slow) delete is running must not send a competing decision.
let decisionInFlight = false;

function actionsAllowed() {
  return !!focusedArtist && !decisionInFlight && focusedArtist.decision !== "delete";
}

function updateActionButtons() {
  const blocked = !actionsAllowed();
  ["btn-keep", "btn-explore", "btn-delete", "btn-skip", "btn-delete-confirm"].forEach(id => { $(id).disabled = blocked; });
  const deleted = !!focusedArtist && focusedArtist.decision === "delete";
  $("deleted-note").classList.toggle("hidden", !deleted);
  if (deleted) {
    deleteConfirm.classList.add("hidden");
    const job = focusedArtist.deletion;
    $("deleted-note-text").textContent = deletionSentence(job);
    $("btn-undo-delete").classList.toggle("hidden", !(job && job.cancellable));
    $("btn-retry-delete").classList.toggle("hidden", !(job && job.status === "failed"));
  }
  scheduleDeletionPoll();
}

// While the focused artist's delete is waiting or running, check on it
let deletionPollTimer = null;
function scheduleDeletionPoll() {
  clearTimeout(deletionPollTimer);
  const job = focusedArtist && focusedArtist.decision === "delete" ? focusedArtist.deletion : null;
  if (!job || !["pending", "running"].includes(job.status)) return;
  const id = focusedArtist.id;
  deletionPollTimer = setTimeout(async () => {
    if (!focusedArtist || focusedArtist.id !== id) return;
    try {
      const data = await api(`/api/artists/${id}`);
      if (focusedArtist && focusedArtist.id === id) {
        focusedArtist = data;
        updateActionButtons();
        if (!["pending", "running"].includes((data.deletion || {}).status)) { loadStats(); loadSidebar(); }
      }
    } catch(e) { scheduleDeletionPoll(); }
  }, 5000);
}

$("btn-undo-delete").addEventListener("click", async () => {
  if (!focusedArtist) return;
  const id = focusedArtist.id;
  $("btn-undo-delete").disabled = true;
  try {
    await undoArtist(id);
    focusedArtist = await api(`/api/artists/${id}`);
    updateActionButtons();
    await Promise.all([loadStats(), loadSidebar()]);
  } catch(e) { errorModal(e); }
  finally { $("btn-undo-delete").disabled = false; }
});

$("btn-retry-delete").addEventListener("click", async () => {
  if (!focusedArtist || !focusedArtist.deletion) return;
  const id = focusedArtist.id;
  $("btn-retry-delete").disabled = true;
  try {
    await retryDeletion(focusedArtist.deletion.id);
    focusedArtist = await api(`/api/artists/${id}`);
    updateActionButtons();
    await loadStats();
  } catch(e) { errorModal(e); }
  finally { $("btn-retry-delete").disabled = false; }
});

$("btn-sync-playlist").addEventListener("click", async () => {
  const btn = $("btn-sync-playlist");
  btn.disabled = true;
  try {
    const r = await api("/api/playlist/sync", { method: "POST" });
    showModal("Plex playlist updated", `"${r.playlist_name}" now has ${r.tracks} tracks: one per undecided artist.`,
      [{ label: "OK", primary: true, action: () => {} }]);
  } catch(e) { errorModal(e); }
  finally { btn.disabled = false; }
});

async function decide(decision) {
  if (!actionsAllowed()) return;
  decisionInFlight = true;
  updateActionButtons();
  try {
    await api(`/api/artists/${focusedArtist.id}/decide`, { method: "POST", body: JSON.stringify({ decision }) });
    await refresh();
  } catch(e) {
    showModal("Error", e.message, [{label:"OK", primary:true, action:()=>{}}]);
  } finally {
    decisionInFlight = false;
    updateActionButtons();
  }
}

// ── Wire up buttons ────────────────────────────────────────────────────────

$("btn-generate").addEventListener("click", () => {
  showModal(
    "Generate Playlist",
    "This will regenerate the triage playlist, replacing all undecided entries. Continue?",
    [
      { label: "Cancel", action: () => {} },
      { label: "Generate", primary: true, action: startGenerationStream },
    ]
  );
});

$("btn-keep").addEventListener("click", () => decide("keep"));

$("btn-explore").addEventListener("click", async () => {
  if (!actionsAllowed()) return;
  $("btn-explore").disabled = true;
  audioStatus.textContent = "Loading tracks…";
  try {
    const tracks = await api(`/api/artists/${focusedArtist.id}/tracks`);
    if (!tracks.length) { audioStatus.textContent = "No other tracks found"; return; }
    trackQueue = tracks;
    queueIndex = 0;
    loadQueueTrack(0);
  } catch(e) {
    audioStatus.textContent = "Couldn't load tracks";
    console.error(e);
  } finally {
    updateActionButtons();
  }
});

$("btn-delete").addEventListener("click", () => {
  if (!actionsAllowed()) return;
  deleteArtistName.textContent = focusedArtist.artist_name;
  deleteConfirm.classList.remove("hidden");
});
$("btn-delete-cancel").addEventListener("click", () => deleteConfirm.classList.add("hidden"));
$("btn-delete-confirm").addEventListener("click", async () => {
  await decide("delete");
  deleteConfirm.classList.add("hidden");
});

$("btn-skip").addEventListener("click", async () => {
  if (!actionsAllowed()) return;
  $("btn-skip").disabled = true;
  try {
    const updated = await api(`/api/artists/${focusedArtist.id}/skip`, { method: "POST" });
    focusedArtist = updated;
    trackTitle.textContent  = updated.track_title || "Unknown track";
    sourceBadge.textContent = "Random track";
    if (updated.stream_key) loadAudio(updated.id);
  } catch(e) {
    showModal("Skip failed", e.message, [{label:"OK", primary:true, action:()=>{}}]);
  } finally {
    updateActionButtons();
  }
});

// Sidebar filters
document.querySelectorAll(".sf-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    document.querySelectorAll(".sf-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    sidebarFilter = btn.dataset.status;
    await loadSidebar();
  });
});

// Sidebar search
$("sidebar-search").addEventListener("input", async e => {
  sidebarSearch = e.target.value;
  await loadSidebar();
});

// Sidebar toggle — desktop collapses inline; mobile opens as overlay drawer
let sidebarOpen = true;
const sidebarOverlay = $("sidebar-overlay");

function closeMobileDrawer() {
  $("sidebar").classList.remove("mobile-open");
  sidebarOverlay.classList.remove("active");
}

$("btn-sidebar-toggle").addEventListener("click", () => {
  if (window.innerWidth <= 768) {
    const opening = !$("sidebar").classList.contains("mobile-open");
    $("sidebar").classList.toggle("mobile-open", opening);
    sidebarOverlay.classList.toggle("active", opening);
  } else {
    sidebarOpen = !sidebarOpen;
    $("sidebar").classList.toggle("collapsed", !sidebarOpen);
  }
});

sidebarOverlay.addEventListener("click", closeMobileDrawer);

// History
$("btn-history").addEventListener("click", async () => {
  sectionHistory.classList.toggle("hidden");
  if (!sectionHistory.classList.contains("hidden")) await loadHistory();
});
$("btn-history-close").addEventListener("click", () => sectionHistory.classList.add("hidden"));
document.querySelectorAll(".filter-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    historyFilter = btn.dataset.filter;
    await loadHistory();
  });
});

// ── Generation SSE ─────────────────────────────────────────────────────────

async function startGenerationStream() {
  genProgressWrap.style.display = "flex";
  genProgressBar.style.width = "0%";
  genProgressLabel.textContent = "Connecting…";
  $("btn-generate").disabled = true;

  let total = 0;
  let finished = false;  // saw a "done" or "error" event

  try {
    // Starts a run, or joins the one already in progress
    const resp = await fetch("/api/generate/stream", { method: "POST", headers: CSRF_HEADERS });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n\n");
      buf = lines.pop();
      for (const chunk of lines) {
        if (!chunk.startsWith("data: ")) continue;  // e.g. keepalive comments
        let evt;
        try { evt = JSON.parse(chunk.slice(6)); } catch { continue; }

        if (evt.stage === "plex_fetch") {
          genProgressLabel.textContent = "Fetching artists from Plex…";
          genProgressBar.style.width = "5%";
        } else if (evt.stage === "lidarr_fetch") {
          genProgressLabel.textContent = "Fetching artists from Lidarr…";
          genProgressBar.style.width = "10%";
        } else if (evt.stage === "lastfm") {
          total = evt.total || total;
          const pct = total ? 10 + Math.round((evt.done / total) * 50) : 35;
          genProgressLabel.textContent = `Last.fm: ${evt.done} / ${total}`;
          genProgressBar.style.width = pct + "%";
        } else if (evt.stage === "resolving") {
          total = evt.total || total;
          const pct = total ? 60 + Math.round((evt.done / total) * 30) : 80;
          genProgressLabel.textContent = `Resolving tracks: ${evt.done} / ${total}`;
          genProgressBar.style.width = pct + "%";
        } else if (evt.stage === "saving") {
          genProgressLabel.textContent = "Saving the queue…";
          genProgressBar.style.width = "92%";
        } else if (evt.stage === "playlist_create") {
          genProgressLabel.textContent = "Creating Plex playlist…";
          genProgressBar.style.width = "95%";
        } else if (evt.stage === "done") {
          finished = true;
          genProgressBar.style.width = "100%";
          genProgressLabel.textContent = `Done! ${evt.total_artists} artists to triage.`;
          setTimeout(() => {
            genProgressWrap.style.display = "none";
          }, 3000);
          let message = `${evt.total_artists} artists are waiting to be triaged (${evt.added} new, ${evt.removed} removed from Plex).`;
          message += evt.playlist_warning ? ` ${evt.playlist_warning}` : ` The Plex playlist "${evt.playlist_name}" was rebuilt.`;
          const failed = evt.failed_artists || [];
          if (failed.length) {
            const shown = failed.slice(0, 5).join(", ") + (failed.length > 5 ? `, and ${failed.length - 5} more` : "");
            message += ` ${failed.length} artist(s) were skipped because Plex couldn't load their tracks: ${shown}. See the server log for details.`;
          }
          showModal("Playlist Ready", message, [
            { label: "OK", primary: true, action: () => {} },
          ]);
          await loadInitial(false);
          await Promise.all([loadStats(), loadSidebar()]);
        } else if (evt.stage === "error") {
          finished = true;
          genProgressWrap.style.display = "none";
          showModal("Generation Failed", evt.message || "Unknown error", [{ label:"OK", primary:true, action:()=>{} }]);
        }
      }
    }
    if (!finished) {
      genProgressWrap.style.display = "none";
      showModal(
        "Lost connection",
        "The connection to the server dropped. Generation keeps running on the server; reload the page to follow its progress.",
        [{ label:"OK", primary:true, action:()=>{} }]
      );
    }
  } catch(e) {
    genProgressWrap.style.display = "none";
    showModal("Generation Failed", e.message, [{ label:"OK", primary:true, action:()=>{} }]);
  } finally {
    $("btn-generate").disabled = false;
  }
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────

function modalOpen() {
  return !$("modal-backdrop").classList.contains("hidden");
}

window.addEventListener("keydown", e => {
  if (modalOpen()) {
    if (e.key === "Escape") $("modal-backdrop").classList.add("hidden");
    return;
  }
  // Skip when typing in inputs/textareas. Buttons are deliberately NOT skipped:
  // a clicked button keeps focus, and skipping it would disable every shortcut
  // (and let Space press that button) until the user clicked elsewhere.
  if (e.target.matches("input, textarea, select")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  switch (e.key) {
    case "k": case "K":
      decide("keep");
      break;
    case "e": case "E":
      if (actionsAllowed()) $("btn-explore").click();
      break;
    case "d": case "D":
      if (actionsAllowed()) {
        deleteArtistName.textContent = focusedArtist.artist_name;
        deleteConfirm.classList.remove("hidden");
      }
      break;
    case "ArrowLeft":
      e.preventDefault();
      navigateDir(-1);
      break;
    case "ArrowRight":
      e.preventDefault();
      navigateDir(1);
      break;
    case " ":
      e.preventDefault();
      // Release focus from any button so Space doesn't also activate it on keyup
      if (document.activeElement && document.activeElement.tagName === "BUTTON") document.activeElement.blur();
      if (audio.paused) audio.play().catch(() => {}); else audio.pause();
      break;
    case "/":
      e.preventDefault();
      $("sidebar-search").focus();
      break;
  }
});

// ── Init ───────────────────────────────────────────────────────────────────

(async () => {
  await loadInitial();
  await Promise.all([loadStats(), loadSidebar()]);
  // Re-attach to a generation that is still running (e.g. after a reload)
  const status = await api("/api/generate/status").catch(() => null);
  if (status && status.running) startGenerationStream();
})();
