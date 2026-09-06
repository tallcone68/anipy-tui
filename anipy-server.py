#!/usr/bin/env python3
"""
anipy-server — TCP socket server for anipy-tui.

Exposes the search / episodes / streams / watchlist / history features of
anipy-tui over a simple line-based TCP protocol so it can be driven from
plain `nc`, from scripts, or from another machine.

Start:

    python anipy-server.py                 # listen on 127.0.0.1:1337
    python anipy-server.py --host 0.0.0.0 --port 9000

Connect:

    nc 127.0.0.1 1337

Protocol (one command per line — plain text or JSON, responses are one JSON
object per line):

Plain text (easiest with `nc`):

    help
    search frieren
    open 0
    episodes / info / select 3 / streams
    play 3 / play best / next / prev
    download 1-5 / dlstop / status
    watchlist / wladd / wlremove 0 / wlcontinue 0
    history / provider / quit

The same commands as JSON (one object per line):

    {"cmd": "help"}
    {"cmd": "search", "query": "frieren"}              -> {"ok":..., "results":[...]}
    {"cmd": "open", "n": 0}                            -> open result #0
    {"cmd": "open", "name": "...", "identifier": "..."} -> open directly
    {"cmd": "episodes"}                                -> episode list + watched marks
    {"cmd": "info"}                                    -> synopsis/genres/year/status
    {"cmd": "select", "ep": 3}                         -> pick episode 3
    {"cmd": "streams"}                                 -> all streams for current ep
    {"cmd": "play", "ep": 3, "quality": "best"}        -> spawn player (mpv/vlc)
    {"cmd": "next"} / {"cmd": "prev"}                  -> play ep +/- 1
    {"cmd": "download", "range": "1-5"}                -> bulk download
    {"cmd": "dlstop"}                                  -> stop the download loop
    {"cmd": "status"}                                  -> current anime/ep/download
    {"cmd": "watchlist"}                               -> list watchlist entries
    {"cmd": "wladd"} / {"cmd": "wlremove", "n": 0}     -> add/remove watchlist
    {"cmd": "wlcontinue", "n": 0}                      -> continue entry #0
    {"cmd": "history"}                                 -> continue-watching list
    {"cmd": "provider"}                                -> list available providers
    {"cmd": "quit"}                                    -> close this connection

Notes:
  - with plain `nc`, just type `search frieren` and hit enter; quotes are not
    needed. One-shot form: echo 'search frieren' | nc 127.0.0.1 1337
  - server state is per-connection; watchlist/history/config are shared with
    anipy-tui via ~/.anipy-tui (override with $ANIPY_TUI_DIR).
  - playback spawns mpv/vlc on the machine running the server, it does not
    stream video over the socket.
  - stop with ctrl+c; each connection is handled on its own thread.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from anipy_api import __version__ as anipy_version
from anipy_api.anime import Anime
from anipy_api.download import Downloader
from anipy_api.locallist import LocalList, LocalListEntry
from anipy_api.player.players.mpv import Mpv
from anipy_api.player.players.vlc import Vlc
from anipy_api.provider import (
    BaseProvider,
    Episode,
    LanguageTypeEnum,
)

# Reuse helpers from anipy-tui.py (dashed filename -> importlib, not import).
# Only the pure-python helpers are imported; anipy-tui imports textual, which
# does not exist on a headless server, so fall back to local copies.
try:
    import importlib.util

    _tui_path = Path(__file__).resolve().parent / "anipy-tui.py"
    _spec = importlib.util.spec_from_file_location("anipy_tui_helpers", _tui_path)
    _tui = importlib.util.module_from_spec(_spec)
    sys.modules["anipy_tui_helpers"] = _tui
    _spec.loader.exec_module(_tui)

    CONFIG_DIR = _tui.CONFIG_DIR
    APP_NAME = _tui.APP_NAME
    QUALITIES = _tui.QUALITIES
    CONTAINERS = _tui.CONTAINERS
    available_providers = _tui.available_providers
    fmt_ep = _tui.fmt_ep
    safe_name = _tui.safe_name
    parse_ep_range = _tui.parse_ep_range
    Config = _tui.Config
except ImportError:
    # Minimal fallbacks (textual not installed / file missing).
    APP_NAME = "anipy-server"
    CONFIG_DIR = Path(os.environ.get("ANIPY_TUI_DIR", Path.home() / ".anipy-tui"))
    QUALITIES = ("best", "worst", "1080", "720", "480", "360")
    CONTAINERS = (".mkv", ".mp4", ".ts")

    def available_providers() -> Dict[str, type]:
        from anipy_api.provider.providers import (
            AniDBAppProvider,
            AnimeHubProvider,
            NativeProvider,
        )
        from anipy_api.provider.providers.allanime_provider import AllAnimeProvider

        providers: Dict[str, type] = {
            p.NAME: p for p in (AnimeHubProvider, AllAnimeProvider, AniDBAppProvider)
        }
        if Path(NativeProvider.BASE_URL).expanduser().is_dir():
            providers[NativeProvider.NAME] = NativeProvider
        return providers

    def fmt_ep(ep) -> str:
        return f"{float(ep):g}"

    def safe_name(name: str) -> str:
        name = "".join(c for c in name if c.isascii())
        name = re.sub(r"[^\w\s()\[\]-]", "", name).strip()
        return re.sub(r"\s+", " ", name) or "anime"

    def parse_ep_range(spec: str, eps: List[Episode]) -> List[Episode]:
        spec = (spec or "").strip()
        if not spec:
            return list(eps)
        picked: List[Episode] = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    lo, hi = float(a), float(b)
                except ValueError:
                    continue
                for e in eps:
                    if lo <= float(e) <= hi and e not in picked:
                        picked.append(e)
            else:
                try:
                    v = float(part)
                except ValueError:
                    continue
                for e in eps:
                    if abs(float(e) - v) < 1e-9 and e not in picked:
                        picked.append(e)
        return sorted(picked, key=float)

    class Config:  # type: ignore[no-redef]
        file = CONFIG_DIR / "config.json"

        def __init__(self) -> None:
            self.player = "mpv"
            self.player_path = ""
            self.mpv_args = ["--keep-open=no", "--really-quiet"]
            self.download_dir = str(Path.home() / "Videos" / "anipy-tui")
            self.container = ".mkv"
            self.use_ffmpeg = False
            self.quality = "best"
            self.language = "sub"
            self.provider = ""
            self.auto_next = False

        @classmethod
        def load(cls) -> "Config":
            cfg = cls()
            try:
                raw = json.loads(cls.file.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if hasattr(cfg, k) and not k.startswith("_"):
                        setattr(cfg, k, v)
            except Exception:
                pass
            return cfg

        def save(self) -> None:
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                self.file.write_text(
                    json.dumps(
                        {k: v for k, v in vars(self).items() if not k.startswith("_")},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass



# --------------------------------------------------------------------- helpers
def _lang(value: Union[str, LanguageTypeEnum]) -> LanguageTypeEnum:
    if isinstance(value, LanguageTypeEnum):
        return value
    return LanguageTypeEnum.DUB if str(value).lower() == "dub" else LanguageTypeEnum.SUB


def _now() -> str:
    return f"{datetime.now():%H:%M:%S}"


# =======================================================================
#                            SESSION (protocol logic)
# =======================================================================
class Session:
    """Holds all per-connection state and implements every command.

    The class is socket-agnostic: `handle_command` takes a parsed request
    dict and returns a response dict. A thin socket layer feeds lines in.
    Everything is synchronous on purpose — anipy-api calls are blocking, but
    each connection runs on its own thread so clients never block each other.
    """

    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or Config.load()
        self._providers: Dict[str, type] = available_providers()
        self._results: List[Anime] = []
        self._provider: Optional[BaseProvider] = None
        self._current: Optional[Anime] = None
        self._current_eps: List[Episode] = []
        self._current_ep: Optional[Episode] = None
        self._current_lang: LanguageTypeEnum = _lang(self.cfg.language)
        self._last_ep: Optional[Episode] = None
        self._last_stream: Optional[Any] = None
        self._player = None
        self._history = LocalList(CONFIG_DIR / "history.json")
        self._watchlist = LocalList(CONFIG_DIR / "watchlist.json")
        self._downloader: Optional[Downloader] = None
        self._dl_lock = threading.Lock()
        self._dl_running = False
        self._dl_stop = False
        self._dl_status = "idle"
        self._dl_log: List[str] = []
        self._dl_done = 0
        self._dl_total = 0

    # ------------------------------------------------------------- utilities
    @property
    def providers(self) -> Dict[str, type]:
        return self._providers

    def _make_provider(self, name: str) -> Optional[BaseProvider]:
        cls = self._providers.get(name)
        if cls is None:
            return None
        return cls(info_callback=lambda message, exc_info=None: None)

    def _need_anime(self) -> Tuple[Optional[Anime], str]:
        if self._current is None:
            return None, "no anime open — use: {\"cmd\":\"search\",...} then {\"cmd\":\"open\"}"
        return self._current, ""

    def _resolve_player(self) -> Optional[Tuple[type, str]]:
        exe = self.cfg.player_path.strip() or shutil.which(self.cfg.player)
        if not exe:
            return None
        return (Mpv if self.cfg.player == "mpv" else Vlc), exe

    def _stream_row(self, s: Any) -> Dict[str, Any]:
        return {
            "resolution": s.resolution,
            "language": str(s.language),
            "container": s.container,
            "url": s.url,
            "referrer": s.referrer,
            "subtitles": list(s.subtitle or {}),
        }

    def _history_mark(self, anime: Anime) -> Optional[float]:
        try:
            entry = self._history.get(anime)
            if entry is not None:
                return float(entry.episode)
        except Exception:
            pass
        return None

    # ---------------------------------------------------------------- search
    def cmd_search(self, req: Dict[str, Any]) -> Dict[str, Any]:
        query = str(req.get("query") or "")
        provider_name = str(req.get("provider") or self.cfg.provider or self._default_provider())
        if provider_name not in self._providers:
            return {
                "ok": False,
                "error": f"unknown provider '{provider_name}' (have: {', '.join(sorted(self._providers))})",
            }
        provider = self._make_provider(provider_name)
        if provider is None:
            return {"ok": False, "error": f"provider '{provider_name}' unavailable"}
        from anipy_api.provider import Filters, MediaType, Season

        year = req.get("year")
        season = req.get("season")
        media = req.get("media")
        # providers expect a real Filters instance (they read its dataclass
        # fields), never None — empty Filters means "no filtering"
        filters = Filters(
            year=int(year) if year else None,
            season=Season[season] if season else None,
            media_type=MediaType[media] if media else None,
        )
        results = provider.get_search(query, filters)
        self._results: List[Anime] = [Anime.from_search_result(provider, r) for r in results]
        self.cfg.provider = provider_name
        return {
            "ok": True,
            "provider": provider_name,
            "query": query,
            "count": len(self._results),
            "results": [
                {
                    "n": i,
                    "name": a.name,
                    "languages": sorted(str(l) for l in a.languages),
                }
                for i, a in enumerate(self._results)
            ],
        }

    def _default_provider(self) -> str:
        return next(iter(self._providers))

    def cmd_open(self, req: Dict[str, Any]) -> Dict[str, Any]:
        results = self._results
        anime: Optional[Anime] = None
        if "identifier" in req:
            prov_name = str(req.get("provider") or self.cfg.provider or self._default_provider())
            cls = self._providers.get(prov_name)
            if cls is None:
                return {"ok": False, "error": f"unknown provider '{prov_name}'"}
            provider = cls(info_callback=lambda m, exc_info=None: None)
            langs = {_lang(l) for l in (req.get("languages") or ["sub"])}
            anime = Anime(provider, str(req["name"]), str(req["identifier"]), langs)
        else:
            if not results:
                return {"ok": False, "error": "no search results — run search first"}
            try:
                idx = int(req.get("n", 0))
            except (TypeError, ValueError):
                return {"ok": False, "error": "invalid 'n'"}
            if not (0 <= idx < len(results)):
                return {"ok": False, "error": f"'n' out of range (0..{len(results) - 1})"}
            anime = results[idx]

        self._current = anime
        self._current_eps = []
        self._current_ep = None
        self._last_ep = None
        offered = [l for l in (LanguageTypeEnum.SUB, LanguageTypeEnum.DUB) if l in anime.languages]
        self._current_lang = (
            self._current_lang if self._current_lang in offered else (offered or [LanguageTypeEnum.SUB])[0]
        )
        info = self.cmd_info({})
        eps = self.cmd_episodes({})
        return {"ok": True, "anime": {"name": anime.name, "provider": str(anime.provider)}, "info": info, "episodes": eps}

    def cmd_info(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        try:
            info = anime.get_info()
        except Exception as exc:
            return {"ok": False, "error": f"info failed: {exc}"}
        return {
            "ok": True,
            "name": anime.name,
            "year": info.release_year,
            "status": str(info.status) if info.status else None,
            "genres": info.genres or [],
            "synopsis": (info.synopsis or "").strip(),
            "languages": sorted(str(l) for l in anime.languages),
        }

    def cmd_episodes(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        lang = _lang(req.get("lang") or self._current_lang)
        try:
            eps = sorted(set(anime.get_episodes(lang)), key=float)
        except Exception as exc:
            return {"ok": False, "error": f"episodes failed: {exc}"}
        self._current_eps = eps
        self._current_lang = lang
        watched = self._history_mark(anime)
        if self._current_ep not in eps and eps:
            after = [e for e in eps if watched is None or float(e) >= watched]
            self._current_ep = after[0] if after else eps[-1]
        return {
            "ok": True,
            "anime": anime.name,
            "lang": str(lang),
            "count": len(eps),
            "episodes": [
                {
                    "ep": fmt_ep(e),
                    "watched": bool(watched is not None and float(e) <= watched),
                    "current": e == self._current_ep,
                }
                for e in eps
            ],
            "selected": fmt_ep(self._current_ep) if self._current_ep is not None else None,
        }

    def cmd_select(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        if not self._current_eps:
            return {"ok": False, "error": "no episodes loaded — run episodes first"}
        try:
            wanted = float(req.get("ep"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "missing/invalid 'ep'"}
        for e in self._current_eps:
            if abs(float(e) - wanted) < 1e-9:
                self._current_ep = e
                return {"ok": True, "selected": fmt_ep(e)}
        return {"ok": False, "error": f"episode {fmt_ep(wanted)} not in list"}

    def cmd_streams(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        if self._current_ep is None:
            return {"ok": False, "error": "no episode selected — use select"}
        lang = _lang(req.get("lang") or self._current_lang)
        try:
            streams = anime.get_videos(self._current_ep, lang)
        except Exception as exc:
            return {"ok": False, "error": f"streams failed: {exc}"}
        self._current_lang = lang
        return {
            "ok": True,
            "anime": anime.name,
            "episode": fmt_ep(self._current_ep),
            "lang": str(lang),
            "count": len(streams),
            "streams": [self._stream_row(s) for s in streams],
        }

    def _play_stream(self, anime: Anime, stream: Any) -> Dict[str, Any]:
        resolved = self._resolve_player()
        if resolved is None:
            return {
                "ok": False,
                "error": f"player '{self.cfg.player}' not found on server (install mpv or vlc)",
            }
        player_cls, exe = resolved
        try:
            if self._player is not None:
                self._player.kill_player()
            extra = self.cfg.mpv_args if player_cls is Mpv else []
            player = player_cls(exe, extra_args=list(extra))
            if player_cls is Mpv and stream.container is None:
                player.player_args_template = [
                    a for a in player.player_args_template if "demuxer-lavf-format" not in a
                ]
            player.play_title(anime, stream)
        except Exception as exc:
            return {"ok": False, "error": f"failed to start player: {exc}"}
        self._player = player
        self._last_ep = stream.episode
        self._last_stream = stream
        self._current_ep = stream.episode
        try:
            self._history.update(anime, episode=stream.episode, language=stream.language)
        except Exception:
            pass
        return {
            "ok": True,
            "playing": {
                "anime": anime.name,
                "episode": fmt_ep(stream.episode),
                "language": str(stream.language),
                "resolution": stream.resolution,
                "player": self.cfg.player,
            },
        }

    def cmd_play(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        ep = self._current_ep
        if req.get("ep") is not None:
            sel = self.cmd_select({"ep": req.get("ep")})
            if not sel.get("ok"):
                return sel
        if ep is None:
            return {"ok": False, "error": "no episode selected — use select"}
        lang = _lang(req.get("lang") or self._current_lang)
        pref = req.get("quality") or self.cfg.quality or "best"
        try:
            stream = anime.get_video(self._current_ep, lang, preferred_quality=pref)
        except Exception as exc:
            return {"ok": False, "error": f"stream lookup failed: {exc}"}
        if stream is None:
            return {
                "ok": False,
                "error": f"no stream for EP {fmt_ep(self._current_ep)} ({lang}) — try dub or another provider",
            }
        resp = self._play_stream(anime, stream)
        if resp.get("ok"):
            resp["auto_next_hint"] = "use {\"cmd\":\"next\"} when the episode ends"
        return resp

    def _play_relative(self, req: Dict[str, Any], delta: int) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        anchor = self._last_ep if self._last_ep is not None else self._current_ep
        if anchor is None:
            return {"ok": False, "error": "nothing playing"}
        if not self._current_eps:
            sel = self.cmd_episodes({})
            if not sel.get("ok"):
                return sel
        idx = next(
            (i for i, e in enumerate(self._current_eps) if float(e) == float(anchor)),
            None,
        )
        if idx is None:
            idx = 0
        target = idx + delta
        if not (0 <= target < len(self._current_eps)):
            return {"ok": False, "error": "no episode in that direction"}
        return self.cmd_play({"ep": self._current_eps[target]})

    def cmd_next(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return self._play_relative(req, +1)

    def cmd_prev(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return self._play_relative(req, -1)

    # ------------------------------------------------------------- downloads
    def cmd_download(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        with self._dl_lock:
            if self._dl_running:
                return {"ok": False, "error": "a download is already running"}
            if not self._current_eps:
                return {"ok": False, "error": "no episodes loaded — run episodes first"}
            eps = parse_ep_range(str(req.get("range") or ""), self._current_eps)
            if not eps:
                return {"ok": False, "error": "no valid episodes in that range"}
            lang = _lang(req.get("lang") or self._current_lang)
            dl_dir = Path(
                str(req.get("dir") or self.cfg.download_dir)
            ).expanduser()
            container = str(req.get("container") or self.cfg.container or ".mkv")
            use_ff = bool(req.get("ffmpeg", self.cfg.use_ffmpeg))
            pref = req.get("quality") or self.cfg.quality or "best"

            self._dl_stop = False
            self._dl_running = True
            self._dl_done = 0
            self._dl_total = len(eps)
            self._dl_status = f"starting {len(eps)} episode(s)"
            self._dl_log = []

            t = threading.Thread(
                target=self._download_worker,
                args=(anime, lang, eps, dl_dir, container, use_ff, pref),
                daemon=True,
            )
            t.start()
        return {"ok": True, "started": len(eps), "lang": str(lang), "dir": str(dl_dir), "container": container}

    def _download_worker(
        self,
        anime: Anime,
        lang: LanguageTypeEnum,
        eps: List[Episode],
        dl_dir: Path,
        container: str,
        use_ff: bool,
        pref: Union[str, int],
    ) -> None:
        def log(msg: str) -> None:
            self._dl_log.append(f"{_now()}  {msg}")
            if len(self._dl_log) > 500:
                del self._dl_log[:-500]

        if self._downloader is None:
            self._downloader = Downloader(
                progress_callback=lambda pct: None,
                info_callback=lambda m, exc_info=None: log(m),
                soft_error_callback=lambda m, exc_info=None: log(m),
            )
        dl = self._downloader
        done = 0
        try:
            for i, ep in enumerate(eps, 1):
                if self._dl_stop:
                    break
                self._dl_status = f"downloading {anime.name} EP {fmt_ep(ep)} ({i}/{len(eps)})"
                log(f"EP {fmt_ep(ep)}: starting ({i}/{len(eps)})")
                try:
                    stream = anime.get_video(ep, lang, preferred_quality=pref)
                except Exception as exc:
                    log(f"EP {fmt_ep(ep)}: stream error: {exc}")
                    continue
                if stream is None:
                    log(f"EP {fmt_ep(ep)}: no stream, skipped")
                    continue
                fname = f"{safe_name(anime.name)} - EP {fmt_ep(ep)} [{lang}]"
                try:
                    path = dl.download(stream, dl_dir / fname, container=container, ffmpeg=use_ff)
                    done += 1
                    log(f"EP {fmt_ep(ep)}: saved -> {path}")
                except Exception as exc:
                    log(f"EP {fmt_ep(ep)}: download failed: {exc}")
            self._dl_done = done
            self._dl_status = (
                f"stopped ({done}/{len(eps)} saved)" if self._dl_stop else f"done ({done}/{len(eps)})"
            )
        except Exception as exc:
            self._dl_status = f"error: {exc}"
            log(f"fatal: {exc}")
        finally:
            self._dl_running = False

    def cmd_dlstop(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if not self._dl_running:
            return {"ok": False, "error": "no download running"}
        self._dl_stop = True
        return {"ok": True, "status": "stop requested — finishing current episode"}

    def cmd_status(self, req: Dict[str, Any]) -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "ok": True,
            "anipy_api": anipy_version,
            "providers": sorted(self._providers),
            "player": self.cfg.player,
            "player_found": bool(self._resolve_player()),
            "download": {
                "running": self._dl_running,
                "status": self._dl_status,
                "done": self._dl_done,
                "total": self._dl_total,
                "log_tail": self._dl_log[-10:],
            },
        }
        if self._current is not None:
            resp["current"] = {
                "name": self._current.name,
                "provider": str(self._current.provider),
                "languages": sorted(str(l) for l in self._current.languages),
                "lang": str(self._current_lang),
                "episodes_loaded": len(self._current_eps),
                "selected": fmt_ep(self._current_ep) if self._current_ep is not None else None,
                "last_played": fmt_ep(self._last_ep) if self._last_ep is not None else None,
            }
        else:
            resp["current"] = None
        return resp

    # ------------------------------------------------------------- watchlist
    @staticmethod
    def _entry_row(e: LocalListEntry) -> Dict[str, Any]:
        return {
            "n": None,
            "name": e.name,
            "provider": e.provider,
            "episode": fmt_ep(e.episode),
            "language": str(e.language),
            "identifier": e.identifier,
        }

    def cmd_watchlist(self, req: Dict[str, Any]) -> Dict[str, Any]:
        try:
            entries = sorted(self._watchlist.get_all(), key=lambda e: e.timestamp, reverse=True)
        except Exception:
            entries = []
        rows = []
        for i, e in enumerate(entries):
            row = self._entry_row(e)
            row["n"] = i
            rows.append(row)
        return {"ok": True, "count": len(rows), "watchlist": rows}

    def cmd_wladd(self, req: Dict[str, Any]) -> Dict[str, Any]:
        anime, err = self._need_anime()
        if err:
            return {"ok": False, "error": err}
        ep = self._last_ep or self._current_ep or (self._current_eps[0] if self._current_eps else 1)
        try:
            self._watchlist.update(anime, episode=ep, language=self._current_lang)
        except Exception as exc:
            return {"ok": False, "error": f"could not add: {exc}"}
        return {"ok": True, "added": anime.name, "episode": fmt_ep(ep)}

    def cmd_wlremove(self, req: Dict[str, Any]) -> Dict[str, Any]:
        try:
            entries = sorted(self._watchlist.get_all(), key=lambda e: e.timestamp, reverse=True)
        except Exception:
            entries = []
        try:
            idx = int(req.get("n", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid 'n'"}
        if not (0 <= idx < len(entries)):
            return {"ok": False, "error": f"'n' out of range (0..{len(entries) - 1})"}
        entry = entries[idx]
        try:
            self._watchlist.delete(entry)
        except Exception as exc:
            return {"ok": False, "error": f"could not remove: {exc}"}
        return {"ok": True, "removed": entry.name}

    def cmd_wlcontinue(self, req: Dict[str, Any]) -> Dict[str, Any]:
        try:
            entries = sorted(self._watchlist.get_all(), key=lambda e: e.timestamp, reverse=True)
        except Exception:
            entries = []
        try:
            idx = int(req.get("n", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid 'n'"}
        if not (0 <= idx < len(entries)):
            return {"ok": False, "error": f"'n' out of range (0..{len(entries) - 1})"}
        entry = entries[idx]
        cls = self._providers.get(entry.provider)
        if cls is None:
            return {"ok": False, "error": f"provider '{entry.provider}' is not available"}
        try:
            anime = Anime(
                cls(info_callback=lambda m, exc_info=None: None),
                entry.name,
                entry.identifier,
                set(entry.languages),
            )
        except Exception as exc:
            return {"ok": False, "error": f"could not restore entry: {exc}"}
        self._current = anime
        self._current_eps = []
        self._current_ep = None
        self._last_ep = None
        self._current_lang = _lang(entry.language)
        eps = self.cmd_episodes({})
        return {
            "ok": True,
            "continued": entry.name,
            "at_episode": fmt_ep(entry.episode),
            "episodes": eps,
        }

    def cmd_history(self, req: Dict[str, Any]) -> Dict[str, Any]:
        try:
            entries = sorted(self._history.get_all(), key=lambda e: e.timestamp, reverse=True)
        except Exception:
            entries = []
        rows = []
        for i, e in enumerate(entries):
            row = self._entry_row(e)
            row["n"] = i
            rows.append(row)
        return {"ok": True, "count": len(rows), "history": rows}

    # ------------------------------------------------------------- providers
    def cmd_provider(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "default": self._default_provider(),
            "providers": sorted(self._providers),
            "active": self.cfg.provider or self._default_provider(),
        }

    def cmd_config(self, req: Dict[str, Any]) -> Dict[str, Any]:
        for key in ("player", "quality", "language", "provider", "container", "download_dir"):
            if key in req:
                setattr(self.cfg, key, str(req[key]))
        if "use_ffmpeg" in req:
            self.cfg.use_ffmpeg = bool(req["use_ffmpeg"])
        self.cfg.save()
        return {
            "ok": True,
            "config": {
                "player": self.cfg.player,
                "quality": self.cfg.quality,
                "language": self.cfg.language,
                "provider": self.cfg.provider,
                "container": self.cfg.container,
                "download_dir": self.cfg.download_dir,
                "use_ffmpeg": self.cfg.use_ffmpeg,
            },
        }

    # --------------------------------------------------------------- dispatch
    def handle_command(self, req: Dict[str, Any]) -> Dict[str, Any]:
        cmd = str(req.get("cmd") or "").strip().lower()
        if not cmd:
            return {"ok": False, "error": "missing 'cmd' — try {\"cmd\":\"help\"}"}

        table = {
            "help": self.cmd_help,
            "search": self.cmd_search,
            "open": self.cmd_open,
            "info": self.cmd_info,
            "episodes": self.cmd_episodes,
            "select": self.cmd_select,
            "streams": self.cmd_streams,
            "play": self.cmd_play,
            "next": self.cmd_next,
            "prev": self.cmd_prev,
            "download": self.cmd_download,
            "dlstop": self.cmd_dlstop,
            "status": self.cmd_status,
            "watchlist": self.cmd_watchlist,
            "wladd": self.cmd_wladd,
            "wlremove": self.cmd_wlremove,
            "wlcontinue": self.cmd_wlcontinue,
            "history": self.cmd_history,
            "provider": self.cmd_provider,
            "config": self.cmd_config,
        }
        handler = table.get(cmd)
        if handler is None:
            return {"ok": False, "error": "unknown cmd '%s' — try {\"cmd\":\"help\"}" % cmd}
        try:
            return handler(req)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def cmd_help(self, req: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "protocol": "one command per line: plain text ('search frieren', 'open 0', 'play 1') or JSON",
            "plain_text": {
                "search frieren": "search; add 'provider <name>' to pick a provider",
                "open 0": "open search result #0",
                "episodes": "list episodes",
                "select 3": "select episode 3",
                "streams": "list streams for the selected episode",
                "play 3": "play ep 3 (also: 'play best', 'play 3 dub 1080', plain 'play')",
                "next / prev": "play next/previous episode",
                "download 1-5": "bulk download (also: 'dl', plain 'download' = all)",
                "dlstop": "stop the download",
                "status": "server + download state",
                "watchlist": "list watchlist (also: 'wl')",
                "wladd": "add current anime",
                "wlremove 0": "remove entry #0",
                "wlcontinue 0": "continue entry #0",
                "history": "continue-watching list",
                "provider": "list providers",
                "quit": "close connection (also: 'exit', 'q')",
            },
            "commands": {
                "help": "this message",
                "search": {"query": "frieren", "provider": "animehub", "year": 2024, "season": "WINTER", "media": "TV"},
                "open": {"n": 0},
                "info": "{}",
                "episodes": {"lang": "sub"},
                "select": {"ep": 3},
                "streams": "{}",
                "play": {"ep": 3, "quality": "best", "lang": "sub"},
                "next": "{}",
                "prev": "{}",
                "download": {"range": "1-5", "container": ".mkv", "dir": "/path"},
                "dlstop": "{}",
                "status": "{}",
                "watchlist": "{}",
                "wladd": "{}",
                "wlremove": {"n": 0},
                "wlcontinue": {"n": 0},
                "history": "{}",
                "provider": "{}",
                "config": {"player": "mpv", "quality": "best"},
                "quit": "close connection",
            },
        }


# =======================================================================
#                             SOCKET LAYER
# =======================================================================
class RequestHandler(socketserver.StreamRequestHandler):
    """One thread per connection; one JSON response per request line."""

    def setup(self) -> None:
        super().setup()
        # each connection gets its own Config instance but loads the same file
        self.session = Session(cfg=Config.load())

    # ------------------------------------------------------------- parsing
    @staticmethod
    def parse_line(line: str) -> Union[Dict[str, Any], str]:
        """Turn one input line into a request dict (or an error string).

        JSON lines win if they parse; otherwise the line is treated as
        plain-text commands like `search frieren` / `open 0` / `play best`.
        """
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            # looks like JSON -> parse strictly and report real JSON errors
            try:
                req = json.loads(stripped)
            except json.JSONDecodeError as exc:
                return f"bad json: {exc}"
            if isinstance(req, dict):
                return req
            return "expected a JSON object or a plain-text command"

        parts = stripped.split()
        if not parts:
            return "empty line"
        cmd, args = parts[0].lower(), parts[1:]

        def arg(i: int, default: str = "") -> str:
            return args[i] if len(args) > i else default

        if cmd == "search":
            if not args:
                return "usage: search <query> [provider <name>]"
            req: Dict[str, Any] = {"cmd": "search", "query": " ".join(args)}
            # allow: search <query> provider <name>
            if len(args) >= 2 and args[-2].lower() == "provider":
                req["query"] = " ".join(args[:-2])
                req["provider"] = args[-1]
            return req
        if cmd in ("open", "o"):
            if not args:
                return "usage: open <n>"
            return {"cmd": "open", "n": args[0]}
        if cmd in ("episodes", "ep", "eps"):
            return {"cmd": "episodes"}
        if cmd == "info":
            return {"cmd": "info"}
        if cmd in ("select", "sel"):
            if not args:
                return "usage: select <ep>"
            return {"cmd": "select", "ep": args[0]}
        if cmd in ("streams", "st"):
            return {"cmd": "streams"}
        if cmd == "play":
            # play / play 3 / play best / play 3 dub 1080
            req = {"cmd": "play"}
            for a in args:
                if a.lower() in ("sub", "dub"):
                    req["lang"] = a.lower()
                elif a.isdigit() or a.lower() in ("best", "worst"):
                    req["quality"] = int(a) if a.isdigit() else a.lower()
                else:
                    req.setdefault("ep", a)
            return req
        if cmd == "next":
            return {"cmd": "next"}
        if cmd == "prev":
            return {"cmd": "prev"}
        if cmd in ("download", "dl"):
            return {"cmd": "download", "range": " ".join(args)}
        if cmd == "dlstop":
            return {"cmd": "dlstop"}
        if cmd == "status":
            return {"cmd": "status"}
        if cmd in ("watchlist", "wl"):
            # 'watchlist add' / 'watchlist remove 0' / 'watchlist continue 0'
            sub = args[0].lower() if args else ""
            if sub == "add":
                return {"cmd": "wladd"}
            if sub in ("remove", "rm"):
                return {"cmd": "wlremove", "n": arg(1, "0")}
            if sub in ("continue", "cont"):
                return {"cmd": "wlcontinue", "n": arg(1, "0")}
            return {"cmd": "watchlist"}
        if cmd == "wladd":
            return {"cmd": "wladd"}
        if cmd == "wlremove":
            return {"cmd": "wlremove", "n": arg(0, "0")}
        if cmd == "wlcontinue":
            return {"cmd": "wlcontinue", "n": arg(0, "0")}
        if cmd == "history":
            return {"cmd": "history"}
        if cmd in ("provider", "providers"):
            return {"cmd": "provider"}
        if cmd == "config":
            return {"cmd": "config"}
        if cmd in ("quit", "exit", "q"):
            return {"cmd": "quit"}
        if cmd == "help":
            return {"cmd": "help"}
        return f"unknown command '{parts[0]}' — type 'help'"

    # -------------------------------------------------------------- handler
    def handle(self) -> None:
        self.wfile.write(
            (
                json.dumps(
                    {
                        "ok": True,
                        "banner": f"{APP_NAME} server",
                        "hint": "type 'help', or 'search frieren', or JSON lines",
                    }
                )
                + "\n"
            ).encode()
        )
        for raw in self.rfile:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parsed = self.parse_line(line)
            if isinstance(parsed, str):
                resp = {"ok": False, "error": parsed}
            elif str(parsed.get("cmd") or "").lower() == "quit":
                self.wfile.write((json.dumps({"ok": True, "bye": True}) + "\n").encode())
                break
            else:
                resp = self.session.handle_command(parsed)
            try:
                self.wfile.write((json.dumps(resp) + "\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host: str, port: int, cfg: Optional[Config] = None) -> None:
    with ThreadedTCPServer((host, port), RequestHandler) as server:
        print(f"{APP_NAME} server listening on {host}:{port} (anipy-api {anipy_version})")
        print(f"connect: nc {host} {port}")
        print(f"example: echo '{{\"cmd\":\"search\",\"query\":\"frieren\"}}' | nc {host} {port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down")
            server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anipy-server",
        description="TCP socket server for anipy — drive search/episodes/streams from nc.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=1337, help="tcp port (default 1337)")
    parser.add_argument("--version", action="version", version=f"anipy-server (anipy-api {anipy_version})")
    args = parser.parse_args()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
