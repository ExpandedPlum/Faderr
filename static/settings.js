// ── Settings page: connect Plex, Lidarr and Last.fm, set the login password ──
// Secrets are only ever sent to the server; it reports back whether they're set.

const $ = id => document.getElementById(id);
const CSRF_HEADERS = { "X-Faderr-Request": "1" };

let state = null;            // last GET /api/settings
let setupMode = false;       // page opened before Faderr was configured
let pinPoll = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...CSRF_HEADERS, ...(opts.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

function showModal(title, message) {
  $("modal-title").textContent = title;
  $("modal-message").textContent = message;
  const wrap = $("modal-buttons");
  wrap.innerHTML = "";
  const btn = document.createElement("button");
  btn.className = "btn btn-secondary";
  btn.textContent = "OK";
  btn.addEventListener("click", () => $("modal-backdrop").classList.add("hidden"));
  wrap.appendChild(btn);
  $("modal-backdrop").classList.remove("hidden");
}
const showError = e => showModal("Couldn't save", e.message);

async function busy(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (e) { showError(e); } finally { button.disabled = false; }
}

function setStatus(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = "settings-status " + (ok ? "status-ok" : "status-missing");
}

// ── Render ─────────────────────────────────────────────────────────────────

function render(s) {
  state = s;
  const f = s.fields;
  $("username").textContent = s.username;

  $("setup-banner").classList.toggle("hidden", s.configured);
  $("setup-missing").textContent = s.missing.length ? `Still needed: ${s.missing.join(", ")}.` : "";
  $("setup-done").classList.toggle("hidden", !(setupMode && s.configured));
  $("link-back").classList.toggle("hidden", !s.configured);

  // Password
  const pw = s.password;
  setStatus("password-status", pw.set ? "Set" : "Not set", pw.set);
  $("password-form").classList.toggle("hidden", pw.source === "env");
  $("password-env").classList.toggle("hidden", pw.source !== "env");
  $("btn-password-save").textContent = pw.set ? "Change password" : "Set password";

  // Plex
  const plexEnv = ["plex_url", "plex_token", "plex_library"].some(k => f[k].source === "env");
  const plexSet = f.plex_url.set && f.plex_token.set && f.plex_library.set;
  const serverName = f.plex_server_name.value || f.plex_url.value;
  setStatus("plex-status", plexSet ? `Connected: ${serverName} · ${f.plex_library.value}` : "Not connected", plexSet);
  $("plex-env").classList.toggle("hidden", !plexEnv);
  $("plex-editor").classList.toggle("hidden", plexEnv);
  if (f.plex_url.value && !$("plex-url").value) $("plex-url").value = f.plex_url.value;

  // Lidarr
  const lidarrEnv = f.lidarr_url.source === "env" || f.lidarr_api_key.source === "env";
  const lidarrSet = f.lidarr_url.set && f.lidarr_api_key.set;
  setStatus("lidarr-status", lidarrSet ? `Connected: ${f.lidarr_url.value}` : "Not connected", lidarrSet);
  $("lidarr-env").classList.toggle("hidden", !lidarrEnv);
  $("lidarr-editor").classList.toggle("hidden", lidarrEnv);
  if (f.lidarr_url.value && !$("lidarr-url").value) $("lidarr-url").value = f.lidarr_url.value;
  $("lidarr-key").placeholder = f.lidarr_api_key.set
    ? `Saved (${f.lidarr_api_key.value}); leave blank to keep`
    : "Lidarr → Settings → General → Security";

  // Last.fm
  const lastfmEnv = f.lastfm_api_key.source === "env";
  setStatus("lastfm-status", f.lastfm_api_key.set ? `Set (${f.lastfm_api_key.value})` : "Not set", f.lastfm_api_key.set);
  $("lastfm-env").classList.toggle("hidden", !lastfmEnv);
  $("lastfm-editor").classList.toggle("hidden", lastfmEnv);
  $("btn-lastfm-remove").classList.toggle("hidden", !f.lastfm_api_key.set);
}

async function load() {
  render(await api("/api/settings"));
}

// ── Password ───────────────────────────────────────────────────────────────

$("btn-password-save").addEventListener("click", () => busy($("btn-password-save"), async () => {
  const password = $("password-new").value;
  render(await post("/api/settings/password", { password }));
  $("password-new").value = "";
  showModal("Password saved", `Your browser will now ask you to log in: username "${state.username}" and the new password.`);
}));

// ── Plex: Sign in with Plex ───────────────────────────────────────────────

function stopPinPoll() {
  if (pinPoll) { clearInterval(pinPoll); pinPoll = null; }
}

$("btn-plex-signin").addEventListener("click", async () => {
  stopPinPoll();
  // Open the window now, while this click still counts as a user action,
  // so popup blockers allow it; it's pointed at plex.tv once the PIN exists.
  const popup = window.open("about:blank", "faderr-plex-auth", "width=600,height=760");
  $("btn-plex-signin").disabled = true;
  $("plex-signin-status").textContent = "Opening Plex…";
  let pin;
  try {
    pin = await post("/api/settings/plex/pin");
  } catch (e) {
    if (popup) popup.close();
    $("btn-plex-signin").disabled = false;
    $("plex-signin-status").textContent = "";
    showError(e);
    return;
  }
  if (popup) {
    popup.location = pin.auth_url;
    $("plex-signin-status").textContent = "Approve Faderr in the Plex window…";
  } else {
    $("plex-signin-status").innerHTML = "";
    const a = document.createElement("a");
    a.href = pin.auth_url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = "Open Plex to sign in";
    $("plex-signin-status").appendChild(a);
  }

  const started = Date.now();
  pinPoll = setInterval(async () => {
    if (Date.now() - started > 10 * 60 * 1000) {
      stopPinPoll();
      $("btn-plex-signin").disabled = false;
      $("plex-signin-status").textContent = "Sign-in timed out. Try again.";
      return;
    }
    let result;
    try { result = await api(`/api/settings/plex/pin/${pin.id}`); }
    catch (e) {
      stopPinPoll();
      $("btn-plex-signin").disabled = false;
      $("plex-signin-status").textContent = e.message;
      return;
    }
    if (!result.authorized) return;
    stopPinPoll();
    if (popup && !popup.closed) popup.close();
    $("btn-plex-signin").disabled = false;
    if (!result.servers.length) {
      $("plex-signin-status").textContent = "Signed in, but this Plex account has no servers.";
      return;
    }
    $("plex-signin-status").textContent = "Signed in. Choose your server:";
    const select = $("plex-server");
    select.innerHTML = "";
    result.servers.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.owned ? s.name : `${s.name} (shared with you)`;
      select.appendChild(opt);
    });
    $("plex-server-step").classList.remove("hidden");
    $("plex-library-step").classList.add("hidden");
  }, 2000);
});

$("btn-plex-server").addEventListener("click", () => busy($("btn-plex-server"), async () => {
  $("plex-signin-status").textContent = "Finding an address Faderr can reach…";
  let result;
  try {
    result = await post("/api/settings/plex/server", { server_id: $("plex-server").value });
  } catch (e) {
    $("plex-signin-status").textContent = "";
    throw e;
  }
  $("plex-signin-status").textContent = `Connected to ${result.server_name} at ${result.url}.`;
  fillLibraries($("plex-library"), result.libraries);
  $("plex-library-step").classList.remove("hidden");
}));

$("btn-plex-library").addEventListener("click", () => busy($("btn-plex-library"), async () => {
  render(await post("/api/settings/plex/library", { library: $("plex-library").value }));
  $("plex-server-step").classList.add("hidden");
  $("plex-library-step").classList.add("hidden");
  $("plex-signin-status").textContent = "Saved.";
}));

function fillLibraries(select, libraries) {
  select.innerHTML = "";
  if (!libraries.length) {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "No music libraries found";
    select.appendChild(opt);
    return;
  }
  libraries.forEach(name => {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    select.appendChild(opt);
  });
  const current = state && state.fields.plex_library.value;
  if (current && libraries.includes(current)) select.value = current;
}

// ── Plex: by hand ──────────────────────────────────────────────────────────

$("btn-plex-test").addEventListener("click", () => busy($("btn-plex-test"), async () => {
  const result = await post("/api/settings/plex/manual", { url: $("plex-url").value, token: $("plex-token").value });
  fillLibraries($("plex-manual-library"), result.libraries);
  showModal("Plex connection works", `Connected to ${result.server_name}. Pick the music library, then Save.`);
}));

$("btn-plex-manual-save").addEventListener("click", () => busy($("btn-plex-manual-save"), async () => {
  const library = $("plex-manual-library").value;
  if (!library) throw new Error("Press Test first, then pick the music library.");
  render(await post("/api/settings/plex/manual", { url: $("plex-url").value, token: $("plex-token").value, library }));
  $("plex-token").value = "";
}));

// ── Lidarr and Last.fm ─────────────────────────────────────────────────────

$("btn-lidarr-save").addEventListener("click", () => busy($("btn-lidarr-save"), async () => {
  render(await post("/api/settings/lidarr", { url: $("lidarr-url").value, api_key: $("lidarr-key").value }));
  $("lidarr-key").value = "";
}));

$("btn-lastfm-save").addEventListener("click", () => busy($("btn-lastfm-save"), async () => {
  const key = $("lastfm-key").value.trim();
  if (!key) throw new Error("Enter an API key (or use Remove to clear the saved one).");
  render(await post("/api/settings/lastfm", { api_key: key }));
  $("lastfm-key").value = "";
}));

$("btn-lastfm-remove").addEventListener("click", () => busy($("btn-lastfm-remove"), async () => {
  render(await post("/api/settings/lastfm", { api_key: "" }));
}));

// ── Init ───────────────────────────────────────────────────────────────────

(async () => {
  try {
    const s = await api("/api/settings");
    setupMode = !s.configured;
    render(s);
  } catch (e) {
    showModal("Couldn't load settings", e.message);
  }
})();
