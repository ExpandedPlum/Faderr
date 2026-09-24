# Faderr

**Faderr is a triage tool for overgrown Plex music libraries** — built for people who want to prune artists intentionally instead of bulk deleting blindly.

If you've accumulated years of artists you no longer remember or care about, Faderr walks you through them one by one. Listen to a representative track, read the Last.fm bio, then decide: keep it, listen to more, or permanently delete it. Progress is saved in a local SQLite database so you can work through hundreds of artists over days or weeks without losing your place.

> Designed to run persistently on a home server (LXC, NAS, etc.) and be accessed from any device — desktop or mobile — on your network.

![Python](https://img.shields.io/badge/python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.110-green) ![Status](https://img.shields.io/badge/status-stable%20for%20personal%20use-brightgreen)

<!-- Add a screenshot here once you have one: -->
<!-- ![Faderr triage view](screenshot.png) -->

---

## Quick start

```bash
git clone https://github.com/yourname/faderr.git
cd faderr
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8811
```

Open `http://localhost:8811`. The first time, Faderr opens its Settings page: set a login password, click **Sign in with Plex**, pick your server and music library, and enter your Lidarr URL and API key ([details below](#setup)). Then click **Generate Playlist** and start triaging.

---

## How it works

1. **Generate** — Faderr scans your Plex library, fetches a representative top track from Last.fm for each artist, and builds a triage queue. If Last.fm has no data for an artist, it picks a random track from your library instead.

2. **Listen** — Each artist's track loads in the built-in player alongside their Last.fm bio. Use the sidebar to jump around or work through artists in order. On mobile, the artist list lives in a slide-out drawer so the triage view stays uncluttered.

3. **Decide** — For each artist, you can:
   - **Keep** — stays in your library, marked done in the triage queue
   - **Listen to More** — queues up additional tracks from that artist in the player
   - **Delete** — queues the artist for deletion. After a grace period (5 minutes by default) Faderr removes it from Lidarr and permanently deletes its files from disk

4. **Repeat** — Undo a Keep at any time, or a Delete until it runs, from the History panel. Regenerate at any time to pick up library changes without affecting previous decisions.

---

## ⚠️ Deletion is permanent

When a delete runs, Faderr instructs Lidarr to remove the artist with `deleteFiles=true`. **Once it has run, it cannot be undone from within Faderr.**

Deletes don't run the moment you choose them:
- **Grace period:** a delete waits `DELETE_GRACE_SECONDS` (default 300, i.e. 5 minutes; `0` runs immediately). Until then, **Undo** in History (or on the artist) cancels it and restores the artist's previous decision.
- **Queue and audit log:** each delete is a recorded job. History shows whether it is waiting, running, done or failed, and the record keeps how the files were removed (Lidarr or Plex), which Lidarr artist and folder were matched, and any error. `GET /api/deletions` returns the full log.
- **Failures wait for you:** a failed delete stays failed, with the reason, until you **Retry** or **Undo** it. If the server stops in the middle of a delete, that job is marked as interrupted rather than silently re-run, so you can check Lidarr and Plex first.
- **Survives restarts:** pending deletes are stored in the database and run after a restart.

Faderr only deletes when it can tell exactly which files belong to the artist:
- The Lidarr artist is found by matching its **folder** against the file paths Plex reports, not just by name. This keeps two artists with the same name (or names in non-Latin scripts) from being confused. When Plex knows the artist's MusicBrainz ID, it must match the Lidarr artist's ID as well; if the ID and the folder disagree, nothing is deleted.
- If Lidarr is unreachable, or the match is ambiguous (for example, a Lidarr artist has the same name but a different folder), **nothing is deleted** and you get an explanation instead.
- Files are only deleted through Plex when Lidarr definitely doesn't manage the artist, or after Lidarr has been told to stop monitoring it, so Lidarr won't download them again.
- By default, deleted artists are added to Lidarr's import list exclusions so import lists don't re-add them (`LIDARR_ADD_IMPORT_EXCLUSION`).
- If a Lidarr delete times out, Faderr asks Lidarr whether the artist is gone before doing anything else, and never deletes through Plex while Lidarr might still be working.
- A deleted (or pending-delete) artist can't be given any other decision. These rules are enforced by the database, so they hold even with several server processes.

Before using the delete action at scale:
- Confirm your Lidarr recycle bin or backup is configured if you want a safety net
- Test on a small set of artists first to verify the Lidarr connection is working

---

## Features

- Built-in audio player — stream directly from Plex, no app switching
- Responsive layout — off-canvas sidebar drawer, large tap targets, single-column triage view on mobile
- Sidebar with search and filter by decision status (All / Undecided / Keep / Deleted)
- Keyboard shortcuts: `K` keep · `E` listen to more · `D` delete · `←` `→` navigate · `Space` play/pause · `/` search
- Last.fm artist bio pulled automatically
- Skip track to try a different song before deciding
- Live generation progress streamed to the header via SSE
- History panel with undo, delete status, and retry for failed deletes
- Plex playlist of the current queue, rebuilt after each generation or on demand with **Sync Plex Playlist**
- Dark mode

---

## Requirements

- [Plex Media Server](https://www.plex.tv/) with a music library
- [Lidarr](https://lidarr.audio/) managing that library
- Optional: a [Last.fm API key](https://www.last.fm/api/account/create) (free), to pick each artist's most popular track and show bios
- Python 3.11+

---

## Setup

### Configure

Everything is set up in the web UI, on the **Settings** page (⚙ in the header). It opens automatically until Plex and Lidarr are connected:

1. **Login password.** Set this first. Without it, anyone who can reach Faderr can delete artists. Your browser asks for it; the username is `faderr`.
2. **Plex.** Click **Sign in with Plex** and approve Faderr in the Plex window. Then choose your server; Faderr finds an address it can reach, preferring your local network. Finally, choose your music library. Faderr stores that server's own access token, never your account password. If you'd rather not sign in, open **Enter the server URL and token yourself**.
3. **Lidarr.** Enter the URL and API key (Lidarr → Settings → General → Security). They're tested before being saved.
4. **Last.fm (optional).** Paste an API key to get popular tracks and bios; without one, Faderr picks random tracks.

Settings are stored in the database. Tokens and keys are never sent back to the browser: the page only shows whether they're set and their last four characters.

**Prefer a config file?** Every setting can also be set in `.env` (see `.env.example`) or as environment variables. Those override the web UI and appear read-only there. Other options: `FADERR_USERNAME`, `DELETE_GRACE_SECONDS` (default `300`), `LIDARR_ADD_IMPORT_EXCLUSION` (default `true`), `TRIAGE_PLAYLIST_NAME`, `DATABASE_URL`.

> **Security:** until a password is set, whoever opens Faderr first can configure it, so set the password straight away (or with `FADERR_PASSWORD`). Audio and artwork are fetched from Plex by the server, so your Plex token is never sent to the browser. Never commit `.env` to version control; it is already in `.gitignore`.

### Run

```bash
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8811
```

For persistent home server deployment, run under `systemd` or your container's process supervisor. Faderr finds its files, `.env` and the default `triage.db` next to `app.py`, so it doesn't matter which directory it's started from.

---

## Operational notes

- **First run:** Generation can take a few minutes on large libraries due to Last.fm rate limiting. Progress streams live in the header. Generation runs on the server, so closing the tab doesn't stop it; reopening the page picks the progress back up, and a second Generate click joins the run already in progress.
- **Regenerating:** Updates the queue in place. Undecided artists keep their place, notes, and any track you picked with Skip (if it's still in Plex); new artists are added; undecided artists no longer in Plex are removed. Decided artists are never changed. Artists are tracked by their Plex ID, so two different artists with the same name are triaged separately. If Plex fails to load one artist's tracks, that artist is skipped (and listed when generation finishes) rather than failing the whole run.
- **Plex playlist:** The "Artist Triage" playlist in Plex holds one track per undecided artist. It is rebuilt after each generation and with **Sync Plex Playlist**, not after every decision, so it can lag behind until you sync.
- **Upgrades:** The database schema is versioned. On startup Faderr applies any pending migrations in one transaction, and first saves a copy of the database next to it (e.g. `triage.db.pre-v4.bak`). Upgrading to this version merges any duplicate rows for the same artist and folds the old "Exploring" state into undecided (and "Kept via explore" into Keep).
- **Multiple workers:** Safe. Decisions, the generation lock and the deletion queue are coordinated through the database, so running uvicorn with `--workers` doesn't break them.
- **Deletion flow:** Faderr calls Lidarr's delete endpoint. Lidarr handles file removal on the media server. Empty folders can be cleaned via Lidarr → System → Scheduled Tasks → "Clean Up Recycle Bin", or by enabling **Settings → Media Management → Delete Empty Folders**.
- **Artists not in Lidarr:** If an artist's files aren't in any Lidarr artist folder, deletion goes through the Plex API instead (Plex's "Allow media deletion" setting must be on).

---

## Troubleshooting

**Sign in with Plex: "None of the server's addresses could be reached"**
Faderr tries each address Plex knows for the server, from the machine running Faderr: local first, then remote, then Plex's relay. Some routers block Plex's `*.plex.direct` names (DNS rebinding protection). Faderr also tries the server's local IP over plain HTTP to get around that, but if nothing works, use **Enter the server URL and token yourself** with e.g. `http://192.168.1.10:32400`.

**Playlist generation fails immediately**
Check the Plex connection on the Settings page (it shows the connected server and library). If Plex is configured in `.env`, check that `PLEX_URL` and `PLEX_TOKEN` are correct.

**Artists are missing after generation**
Artists with no tracks in Plex are skipped. Artists added manually to Plex (not managed by Lidarr) still appear and are deleted through Plex.

**A delete shows "Delete failed"**
Hover the status in History (or open the artist) to see why. Common reasons: Lidarr was unreachable; a Lidarr artist has the same name (or MusicBrainz ID) but its folder isn't where Plex finds the files; Plex couldn't be reached. Nothing was deleted. Fix the cause and **Retry**, or **Undo** and delete the artist manually in Lidarr.

**Last.fm bio or top track not loading**
Last.fm returns no data for some artists. Faderr falls back to a random track silently — this is expected behavior, not an error.

**Audio won't play**
Faderr streams audio from Plex through the server. Check that `PLEX_URL` is reachable from the machine running Faderr and that the token is correct. Library names and tokens are case-sensitive.

**Last.fm rate limit errors during generation**
Normal for large libraries. Faderr uses concurrency limiting and automatic backoff — let it run. If it fails partway through, run Generate again: it starts over, but decisions you've already made are kept.

---

## Project structure

```
├── app.py                  # FastAPI app and routes
├── settings_api.py         # Settings page API (Sign in with Plex, Lidarr, Last.fm, password)
├── config.py               # Operational options from the environment
├── models.py               # Database models and SQLite setup
├── migrations.py           # Numbered schema migrations (the schema's source of truth)
├── services/
│   ├── settings_service.py # Settings from the web UI or environment; password hashing
│   ├── plex_auth.py        # Sign in with Plex and finding a reachable server address
│   ├── plex_service.py     # Long-lived async Plex client
│   ├── lastfm_service.py   # Last.fm API interactions
│   ├── lidarr_service.py   # Async Lidarr client and artist matching
│   ├── triage_service.py   # Queue reads, decisions, playlist sync
│   ├── generation_service.py # Building the queue; the shared generation lock
│   └── deletion_service.py # Deletion jobs, the safe delete routine, the worker
├── tests/                  # pytest suite
├── static/
│   ├── app.js
│   ├── settings.js
│   └── style.css
├── templates/
│   ├── index.html
│   └── settings.html
├── .env.example
├── requirements.txt
└── requirements-dev.txt
```

## Running tests

```bash
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest
```

The tests use a temporary SQLite database and fake Plex/Lidarr responses; they don't touch your real servers.

---

## Contributing

Issues and PRs welcome. This is a personal tool that may be useful to others — bug reports, Plex/Lidarr compatibility notes, and UX improvements are especially appreciated.

---

## License

MIT
