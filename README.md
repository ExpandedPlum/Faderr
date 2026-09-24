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
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Plex, Last.fm, and Lidarr credentials ([details below](#setup)), then:

```bash
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8811
```

Open `http://localhost:8811`, click **Generate Playlist**, and start triaging.

---

## How it works

1. **Generate** — Faderr scans your Plex library, fetches a representative top track from Last.fm for each artist, and builds a triage queue. If Last.fm has no data for an artist, it picks a random track from your library instead.

2. **Listen** — Each artist's track loads in the built-in player alongside their Last.fm bio. Use the sidebar to jump around or work through artists in order. On mobile, the artist list lives in a slide-out drawer so the triage view stays uncluttered.

3. **Decide** — For each artist, you can:
   - **Keep** — stays in your library, marked done in the triage queue
   - **Listen to More** — queues up additional tracks from that artist in the player
   - **Delete** — removes the artist from Lidarr and permanently deletes their files from disk

4. **Repeat** — Undo any non-delete decision from the History panel (deletes can't be undone). Regenerate at any time to refresh undecided artists without affecting previous decisions.

---

## ⚠️ Deletion is permanent

When you delete an artist, Faderr instructs Lidarr to remove them with `deleteFiles=true`. **This cannot be undone from within Faderr.** Deleted artists have no Undo button in History.

Faderr only deletes when it can tell exactly which files belong to the artist:
- The Lidarr artist is found by matching its **folder** against the file paths Plex reports, not just by name. This keeps two artists with the same name (or names in non-Latin scripts) from being confused.
- If Lidarr is unreachable, or the match is ambiguous (for example, a Lidarr artist has the same name but a different folder), **nothing is deleted** and you get an explanation instead.
- Files are only deleted through Plex when Lidarr definitely doesn't manage the artist, or after Lidarr has been told to stop monitoring it, so Lidarr won't download them again.
- By default, deleted artists are added to Lidarr's import list exclusions so import lists don't re-add them (`LIDARR_ADD_IMPORT_EXCLUSION`).
- If a Lidarr delete times out, Faderr asks Lidarr whether the artist is gone before doing anything else, and never deletes through Plex while Lidarr might still be working.
- A deleted artist can't be given any other decision, and only one decision per artist runs at a time.

Before using the delete action at scale:
- Confirm your Lidarr recycle bin or backup is configured if you want a safety net
- Test on a small set of artists first to verify the Lidarr connection is working

---

## Features

- Built-in audio player — stream directly from Plex, no app switching
- Responsive layout — off-canvas sidebar drawer, large tap targets, single-column triage view on mobile
- Sidebar with search and filter by decision status (All / Undecided / Keep / Exploring / Deleted)
- Keyboard shortcuts: `K` keep · `E` listen to more · `D` delete · `←` `→` navigate · `Space` play/pause · `/` search
- Last.fm artist bio pulled automatically
- Skip track to try a different song before deciding
- Live generation progress streamed to the header via SSE
- History panel with undo support for non-delete decisions
- Dark mode

---

## Requirements

- [Plex Media Server](https://www.plex.tv/) with a music library
- [Lidarr](https://lidarr.audio/) managing that library
- A [Last.fm API key](https://www.last.fm/api/account/create) (free)
- Python 3.12+

---

## Setup

### Configure

Copy `.env.example` to `.env` and fill in your credentials:

```env
PLEX_URL=http://192.168.1.x:32400
PLEX_TOKEN=your_plex_token_here
PLEX_MUSIC_LIBRARY=Music
LASTFM_API_KEY=your_lastfm_api_key_here
LIDARR_URL=http://192.168.1.x:8686
LIDARR_API_KEY=your_lidarr_api_key_here

# Recommended: require a login (username defaults to "faderr")
FADERR_PASSWORD=choose_a_password
```

Optional settings: `FADERR_USERNAME`, `LIDARR_ADD_IMPORT_EXCLUSION` (default `true`), `TRIAGE_PLAYLIST_NAME`, `DATABASE_URL`.

**Finding your Plex token:** Plex Web → any media item → ··· → Get Info → View XML → find `X-Plex-Token` in the URL.

**Finding your Lidarr API key:** Lidarr → Settings → General → Security.

> **Security:** never commit `.env` to version control. It is already in `.gitignore`, but double-check before pushing to a public repository.
>
> Set `FADERR_PASSWORD`. Without it, anyone who can reach Faderr on your network can delete artists (Faderr logs a warning at startup). Audio and artwork are fetched from Plex by the server, so your Plex token is never sent to the browser.

### Run

```bash
venv/bin/uvicorn app:app --host 0.0.0.0 --port 8811
```

For persistent home server deployment, run under `systemd` or your container's process supervisor. Faderr finds its files, `.env` and the default `triage.db` next to `app.py`, so it doesn't matter which directory it's started from.

---

## Operational notes

- **First run:** Generation can take a few minutes on large libraries due to Last.fm rate limiting. Progress streams live in the header. Generation runs on the server, so closing the tab doesn't stop it; reopening the page picks the progress back up, and a second Generate click joins the run already in progress.
- **Regenerating:** Refreshes all undecided artists. Already-decided artists are not affected. Artists are tracked by their Plex ID, so two different artists with the same name are triaged separately. If Plex fails to load one artist's tracks, that artist is skipped (and listed when generation finishes) rather than failing the whole run.
- **Deletion flow:** Faderr calls Lidarr's delete endpoint. Lidarr handles file removal on the media server. Empty folders can be cleaned via Lidarr → System → Scheduled Tasks → "Clean Up Recycle Bin", or by enabling **Settings → Media Management → Delete Empty Folders**.
- **Artists not in Lidarr:** If an artist's files aren't in any Lidarr artist folder, deletion goes through the Plex API instead (Plex's "Allow media deletion" setting must be on).

---

## Troubleshooting

**Playlist generation fails immediately**
Check that `PLEX_URL` and `PLEX_TOKEN` are correct. Visit `http://YOUR_PLEX_URL/library/sections?X-Plex-Token=YOUR_TOKEN` in a browser — a valid token returns an XML response.

**Artists are missing after generation**
Artists with no tracks in Plex are skipped. Artists added manually to Plex (not managed by Lidarr) still appear and are deleted through Plex.

**"Not deleting …" when deleting an artist**
Faderr couldn't safely tell which Lidarr artist owns the files: Lidarr was unreachable, or a Lidarr artist has the same name but its folder isn't where Plex finds the files. Nothing was deleted. Retry once Lidarr is reachable, or delete the artist manually in Lidarr.

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
├── config.py               # Environment variable loading
├── models.py               # SQLite database model
├── services/
│   ├── plex_service.py     # Plex API interactions
│   ├── lastfm_service.py   # Last.fm API interactions
│   ├── lidarr_service.py   # Lidarr API interactions and artist matching
│   ├── triage_service.py   # Core triage logic
│   └── generation_runner.py # Background playlist generation
├── tests/                  # pytest suite
├── static/
│   ├── app.js
│   └── style.css
├── templates/
│   └── index.html
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
