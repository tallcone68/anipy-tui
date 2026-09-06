# anipy-tui

Watch and download anime from your terminal — a full-featured Textual TUI built
on [anipy-api](https://github.com/sdaqo/anipy-cli).

## Features

- **Search** across providers: `animehub`, `allanime`, `anidbapp`, and `native`
  (local files in `~/Videos`)
- **Browse by season/year/type** — leave the search box empty and use the filters
- **Anime details** (synopsis, genres, year, status)
- **Episode list** with watched markers, sub & dub support
- **Stream picker** (all qualities) or quick "best quality" play
- **Watchlist + history** — continue any anime exactly where you left off
- **Auto-next episode** when the player closes
- **Bulk downloads** with progress bar (`1-12, 5` style ranges), optional
  ffmpeg remux to `.mkv` / `.mp4`
- **Activity log, help tab, persisted settings**
- **Socket server** — drive the whole thing from `nc` or scripts (`anipy-server.py`)

## Install

```bash
pip install -r requirements.txt
# or manually:
pip install "anipy-api>=3.9" textual
```

System dependencies (optional but recommended):

| Tool | Purpose |
|---|---|
| `mpv` | playback (preferred) |
| `vlc` | playback (alternative) |
| `ffmpeg` + `ffprobe` | downloads & remuxing |

```bash
# Debian/Ubuntu
sudo apt install mpv ffmpeg
# Arch
sudo pacman -S mpv ffmpeg
# macOS
brew install mpv ffmpeg
```

## Run

```bash
python anipy-tui.py
```

## Keys

| Key | Action |
|---|---|
| `f2` | search box |
| `f3` / `f4` / `f5` / `f6` / `f7` | episodes / streams / downloads / watchlist / log tab |
| `f1` | help |
| `p` | play selected episode |
| `n` / `b` | next / previous episode |
| `s` | list all streams for the episode |
| `d` | download selected episode |
| `enter` | open selected search result / play selected stream |
| `ctrl+q` | quit (settings are saved) |
| `ctrl+p` | command palette |

## How to watch

1. `f2` → type an anime name → **Search**
2. Move to the results table, hit **enter** on an anime
3. Episodes tab opens automatically — pick an episode, choose sub/dub & quality
4. `p` to play (mpv window opens)
5. When the episode ends, the next one starts automatically (toggleable)

## Downloads

Downloads tab (`f5`):

- **Range**: `1-12`, `5`, or empty for all loaded episodes
- **Container**: `.mkv` / `.mp4` / `.ts` (remuxing needs ffmpeg)
- **use ffmpeg**: streams non-HLS sources through ffmpeg directly
- Files land in `~/Videos/anipy-tui/` by default, named
  `Anime Name - EP 12 [sub].mkv`

## Socket server (`anipy-server.py`)

The same features as the TUI — search, episodes, streams, playback, downloads,
watchlist — exposed over a line-based TCP protocol you can drive with plain
`nc`. No extra dependencies.

Start:

```bash
python anipy-server.py                    # listens on 127.0.0.1:7531
python anipy-server.py --host 0.0.0.0 --port 9000
```

Connect and send one command per line — **plain text** or JSON; every request
gets one JSON response line. Plain text is easiest with `nc`:

```bash
nc 127.0.0.1 7531
help
search frieren
open 0
play 1
quit
```

Or one-shot with `echo`:

```bash
echo 'search frieren' | nc 127.0.0.1 7531
```

The same commands also work as JSON (handy from scripts):

```bash
echo '{"cmd":"search","query":"frieren"}' | nc 127.0.0.1 7531
```

Commands (type `help` for the full list):

| Plain text | JSON | Purpose |
|---|---|---|
| `search frieren` | `{"cmd": "search", "query": "...", "provider": "animehub", "year": 2024, "season": "WINTER", "media": "TV"}` | search |
| `open 0` | `{"cmd": "open", "n": 0}` or by `name`/`identifier` | open result from last search |
| `info` / `episodes` | `info` / `episodes` | synopsis, genres / episode list with watched marks |
| `select 3` | `{"cmd": "select", "ep": 3}` | pick episode |
| `streams` | `streams` | all streams for the selected episode |
| `play 3` / `play best` / `next` / `prev` | `play` (with optional `ep`, `quality`, `lang`) | spawn mpv/vlc on the server machine |
| `download 1-5` / `dlstop` | `download` (with optional `range`, `dir`, `container`, `ffmpeg`) / `dlstop` | bulk download / stop it |
| `status` | `status` | server + download state |
| `watchlist` / `watchlist add` / `watchlist remove 0` / `watchlist continue 0` | `watchlist` / `wladd` / `wlremove` / `wlcontinue` | watchlist management |
| `history` | `history` | continue-watching list |
| `provider` / `config` | `provider` / `config` | list providers / change settings |
| `quit` (also `exit`, `q`) | `quit` | close the connection |

Notes:

- One connection = one session (own current anime/episode); each connection
  runs on its own thread.
- Playback spawns the player **on the machine running the server** — the video
  is not streamed over the socket.
- Watchlist, history and config are shared with the TUI via `~/.anipy-tui`
  (override with `$ANIPY_TUI_DIR`).

## Data & config

Everything lives in `~/.anipy-tui/` (override with `$ANIPY_TUI_DIR`):

- `config.json` — player, quality, language, download dir, ...
- `watchlist.json` — your saved anime
- `history.json` — continue-watching state

## Provider notes

Providers are scraped from streaming sites and break from time to time —
this is inherent to the upstream project:

- `animehub` — currently the most reliable; sub + dub, up to 1080p
- `allanime` — big catalog; its stream endpoint depends on upstream crypto
  keys that rotate frequently (`AA_CRYPTO_STALE` / `PersistedQueryNotFound`
  means sdaqo's keygen is stale — search/episodes still work, or update
  anipy-api and wait for the keygen refresh)
- `anidbapp` — was returning 503 at the time of writing
- `native` — appears automatically when `~/Videos` exists; plays local files

If one provider fails, switch with the **Provider** dropdown on the Search tab
(or the `provider` field on `search` over the socket).

## Development

```bash
python smoke_test.py   # UI harness test (no network)
python test_server.py  # socket server test (no network)
python e2e_test.py     # real provider search -> episodes -> streams
```
