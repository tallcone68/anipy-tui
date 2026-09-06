#!/usr/bin/env python3
"""
anipy-tui — a full-featured Terminal UI to watch & download anime.

Built on top of:
  - anipy-api   (https://github.com/sdaqo/anipy-cli)  >= 3.8
  - Textual     (https://textual.textualize.io)

Install:
    pip install anipy-api textual

Optional system dependencies:
    mpv (recommended) or vlc  -> playback
    ffmpeg + ffprobe          -> "use ffmpeg" downloads / remux

Run:
    python anipy-tui.py

Features:
    - Search anime on multiple providers (allanime / animehub / anidbapp / native)
    - Browse anime by year / season / type (no query needed)
    - Show anime details (synopsis, genres, year, status)
    - Episode list with "watched" markers, sub & dub support
    - Stream picker (all qualities) or quick "best quality" play
    - Watchlist + watch history (auto continue where you left off)
    - Auto-next episode when the player closes at the end
    - Bulk episode downloads with progress bar (mkv/mp4/ts, optional ffmpeg remux)
    - Activity log, help screen, persisted settings

Data & settings live in ~/.anipy-tui (override with $ANIPY_TUI_DIR).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Tuple, Type, Union

# ---------------------------------------------------------------- dependencies
try:
    from rich.table import Table as RichTable
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widget import Widget
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        ProgressBar,
        RichLog,
        Select,
        Static,
        Switch,
        TabbedContent,
        TabPane,
    )
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Missing dependency: {exc.name}\n"
        "Install with:\n    pip install anipy-api textual"
    )

from anipy_api import __version__ as anipy_version
from anipy_api.anime import Anime
from anipy_api.download import Downloader
from anipy_api.error import PlayerError
from anipy_api.locallist import LocalList, LocalListEntry
from anipy_api.player.players.mpv import Mpv
from anipy_api.player.players.vlc import Vlc
from anipy_api.provider import (
    BaseProvider,
    Episode,
    Filters,
    LanguageTypeEnum,
    MediaType,
    ProviderInfoResult,
    ProviderSearchResult,
    ProviderStream,
    Season,
)
from anipy_api.provider.providers import (
    AniDBAppProvider,
    AnimeHubProvider,
    NativeProvider,
)
from anipy_api.provider.providers.allanime_provider import AllAnimeProvider

# ------------------------------------------------------------------- constants
APP_NAME = "anipy-tui"
CONFIG_DIR = Path(os.environ.get("ANIPY_TUI_DIR", Path.home() / ".anipy-tui"))
DEFAULT_DL_DIR = Path.home() / "Videos" / "anipy-tui"

LANGS = (LanguageTypeEnum.SUB, LanguageTypeEnum.DUB)
QUALITIES = ("best", "worst", "1080", "720", "480", "360")
CONTAINERS = (".mkv", ".mp4", ".ts")

SUPPORT_LANG_TIP = (
    "Tip: if a stream fails, try another provider from the Search tab."
)


def available_providers() -> Dict[str, Type[BaseProvider]]:
    """Provider classes that are usable right now.

    Order matters: the first entry is the default provider. animehub is
    currently the most reliable one (allanime's stream endpoint depends on
    upstream crypto keys that rotate frequently).
    """
    providers: Dict[str, Type[BaseProvider]] = {
        p.NAME: p for p in (AnimeHubProvider, AllAnimeProvider, AniDBAppProvider)
    }
    if Path(NativeProvider.BASE_URL).expanduser().is_dir():
        providers[NativeProvider.NAME] = NativeProvider
    return providers


def fmt_ep(ep: Episode) -> str:
    """'12.0' -> '12', keep 12.5 as '12.5'."""
    return f"{float(ep):g}"


def safe_name(name: str) -> str:
    """Filesystem-friendly anime name."""
    name = "".join(c for c in name if c.isascii())
    name = re.sub(r"[^\w\s()\[\]-]", "", name).strip()
    return re.sub(r"\s+", " ", name) or "anime"


def parse_ep_range(spec: str, eps: List[Episode]) -> List[Episode]:
    """Parse '1-3, 5' against the available episode list (empty = all)."""
    spec = (spec or "").strip()
    if not spec:
        return list(eps)

    picked: List[Episode] = []

    def take(v: float) -> None:
        for e in eps:
            if abs(float(e) - v) < 1e-9 and e not in picked:
                picked.append(e)

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
                take(float(part))
            except ValueError:
                continue
    return sorted(picked, key=float)


# ---------------------------------------------------------------------- config
@dataclass
class Config:
    player: str = "mpv"  # "mpv" | "vlc"
    player_path: str = ""  # empty = auto-detect on PATH
    mpv_args: List[str] = field(
        default_factory=lambda: ["--keep-open=no", "--really-quiet"]
    )
    download_dir: str = str(DEFAULT_DL_DIR)
    container: str = ".mkv"
    use_ffmpeg: bool = False
    quality: str = "best"
    language: str = "sub"
    provider: str = ""
    auto_next: bool = True

    file: ClassVar[Path] = CONFIG_DIR / "config.json"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            raw = json.loads(cls.file.read_text(encoding="utf-8"))
            for k, v in raw.items():
                if hasattr(cfg, k) and not k.startswith("_") and k != "file":
                    setattr(cfg, k, v)
        except FileNotFoundError:
            pass
        except Exception:
            pass  # corrupted config -> fall back to defaults
        return cfg

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.file.write_text(
                json.dumps(asdict(self), indent=2), encoding="utf-8"
            )
        except Exception:
            pass


# -------------------------------------------------------------- confirm dialog
class ConfirmScreen(ModalScreen[bool]):
    """Simple yes/no modal."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(Text(self._message), id="confirm-msg")
            with Horizontal(id="confirm-actions"):
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no", variant="default")

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------- help text
def build_help_text() -> Text:
    lines = [
        f"  {APP_NAME}  —  watch & download anime in the terminal",
        f"  powered by anipy-api {anipy_version}",
        "",
        "  GLOBAL KEYS",
        "    f2 ........... search box                f3 ......... episodes tab",
        "    f4 ........... streams tab               f5 ......... downloads tab",
        "    f6 ........... watchlist tab             f7 ......... activity log",
        "    f1 ........... this help                 ctrl+q ..... quit & save",
        "    ctrl+p ....... command palette",
        "",
        "  WATCHING",
        "    p ............ play selected episode     n .......... next episode",
        "    b ............ previous episode          s .......... show streams",
        "    enter ........ on a search result / episode = play it",
        "    d ............ download selected episode",
        "",
        "  NOTES",
        "    - Playback uses mpv (preferred) or VLC; both are auto-detected.",
        "    - Shortcuts like p/n/d are ignored while typing in a text box.",
        "    - Downloads go to ~/Videos/anipy-tui by default (configurable).",
        "      Remuxing to .mkv/.mp4 requires ffmpeg on your system.",
        "    - Watchlist & history are saved in ~/.anipy-tui so you can continue",
        "      any anime where you left off.",
        "    - 'Empty query + season filters' lets you browse what is airing.",
        "    - If something fails to play, try: another quality (Streams tab),",
        "      another audio language, or another provider.",
    ]
    return Text("\n".join(lines), no_wrap=False)


# =========================================================================
#                                  APP
# =========================================================================
class AnipyTUI(App[None]):
    TITLE = APP_NAME
    SUB_TITLE = "watch anime in the terminal"

    CSS = """
    Screen { layout: vertical; }
    .fill { height: 1fr; }
    Select { width: 1fr; }
    #search-row { height: 3; }
    #query-input { width: 1fr; }
    #filters-row { height: 3; }
    #results-table { height: 1fr; }
    #info-panel {
        height: auto; max-height: 14; padding: 0 1;
        border-top: solid $accent; background: $surface;
    }
    #ep-hint { height: 1; color: $text-muted; padding: 0 1; }
    #ep-actions, #streams-actions, #wl-actions, #dl-opt-row, #dl-row { height: 3; }
    #eps-table, #streams-list, #wl-list, #dl-log, #activity-log { height: 1fr; }
    #dl-status { height: 1; padding: 0 1; color: $warning; }
    #dl-progress { margin: 0 1; }
    #lbl-ffmpeg { width: auto; padding: 0 1; color: $text-muted; }
    #dl-range { width: 1fr; }
    #dl-path { width: 2fr; }
    #streams-hint { width: 1fr; padding: 0 1; color: $text-muted; }
    #now-title { padding: 0 1; }
    #help { padding: 1 2; }
    #help-table { width: 1fr; }
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 64; height: auto; padding: 1 2;
        border: thick $error; background: $surface;
    }
    #confirm-msg { margin-bottom: 1; }
    #confirm-actions { height: 3; align-horizontal: right; }
    """

    BINDINGS = [
        Binding("f2", "focus_search", "Search", show=True),
        Binding("f3", "tab('episodes')", "Episodes", show=True),
        Binding("f4", "tab('streams')", "Streams", show=True),
        Binding("f5", "tab('downloads')", "Downloads", show=True),
        Binding("f6", "tab('watchlist')", "Watchlist", show=True),
        Binding("f7", "tab('activity')", "Log", show=True),
        Binding("f1", "tab('help')", "Help", show=True),
        Binding("p", "play_selected", "Play", show=True),
        Binding("n", "play_next", "Next ep", show=True),
        Binding("b", "play_prev", "Prev ep", show=False),
        Binding("d", "download_selected", "Download", show=True),
        Binding("s", "show_streams", "Streams", show=False),
    ]

    # ------------------------------------------------------------- lifecycle
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()

        self._providers: Dict[str, Type[BaseProvider]] = {}
        self._provider: Optional[BaseProvider] = None
        self._default_provider = next(iter(available_providers()))

        self._results: List[Anime] = []
        self._current: Optional[Anime] = None
        self._current_eps: List[Episode] = []
        self._current_ep: Optional[Episode] = None
        self._current_lang = (
            LanguageTypeEnum.DUB
            if self.cfg.language == "dub"
            else LanguageTypeEnum.SUB
        )
        self._streams: List[ProviderStream] = []
        self._jump_ep: Optional[Episode] = None

        self._ep_row_keys: Dict[str, object] = {}
        self._col_ep = None
        self._col_state = None

        self._wl_entries: List[LocalListEntry] = []
        self._wl_sel: Optional[int] = None

        self._watchlist = LocalList(CONFIG_DIR / "watchlist.json")
        self._history = LocalList(CONFIG_DIR / "history.json")

        self._player = None
        self._play_token = 0
        self._last_ep: Optional[Episode] = None
        self._last_stream: Optional[ProviderStream] = None

        self._downloader: Optional[Downloader] = None
        self._dl_running = False
        self._dl_stop = False

        self._suppress_lang_changed = False
        self._help_text = build_help_text()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs"):
            # ------------------------------------------------------ search tab
            with TabPane("Search", id="search"):
                with Vertical(classes="fill"):
                    with Horizontal(id="search-row"):
                        yield Input(
                            placeholder="Search anime... (leave empty to browse by filters)",
                            id="query-input",
                        )
                        yield Button("Search", id="btn-search", variant="primary")
                    with Horizontal(id="filters-row"):
                        yield Select(options=[], id="sel-provider", prompt="Provider")
                        yield Select(options=[], id="sel-year", prompt="Year")
                        yield Select(options=[], id="sel-season", prompt="Season")
                        yield Select(options=[], id="sel-media", prompt="Type")
                    yield DataTable(id="results-table", cursor_type="row", zebra_stripes=True)
            # ---------------------------------------------------- episodes tab
            with TabPane("Episodes", id="episodes"):
                with Vertical(classes="fill"):
                    yield Static(Text("Pick an anime from the Search tab (f2)."), id="info-panel")
                    yield Label("", id="now-title")
                    yield Label("", id="ep-hint")
                    with Horizontal(id="ep-actions"):
                        yield Select(options=[], id="sel-lang", prompt="Audio")
                        yield Select(options=[], id="sel-quality", prompt="Quality")
                        yield Button("Play", id="btn-play", variant="primary")
                        yield Button("Download", id="btn-dl-quick", variant="warning")
                        yield Button("Streams", id="btn-streams")
                        yield Button("+ Watchlist", id="btn-add-wl")
                    yield DataTable(id="eps-table", cursor_type="row", zebra_stripes=True)
            # ----------------------------------------------------- streams tab
            with TabPane("Streams", id="streams"):
                with Vertical(classes="fill"):
                    with Horizontal(id="streams-actions"):
                        yield Label("", id="streams-hint")
                        yield Button("Best quality", id="btn-streams-best", variant="primary")
                        yield Button("Refresh", id="btn-streams-refresh")
                    yield ListView(id="streams-list")
            # -------------------------------------------------- downloads tab
            with TabPane("Downloads", id="downloads"):
                with Vertical(classes="fill"):
                    with Horizontal(id="dl-opt-row"):
                        yield Select(options=[], id="sel-dl-quality", prompt="Quality")
                        yield Select(options=[], id="sel-container", prompt="Container")
                        yield Switch(id="sw-ffmpeg", value=self.cfg.use_ffmpeg)
                        yield Label("use ffmpeg (needed for remux)", id="lbl-ffmpeg")
                    with Horizontal(id="dl-row"):
                        yield Input(
                            placeholder="Episodes e.g. 1-12 or 5 (empty = all)",
                            id="dl-range",
                        )
                        yield Input(placeholder="Download folder", id="dl-path")
                        yield Button("Start", id="btn-dl-start", variant="success")
                        yield Button("Stop", id="btn-dl-stop", variant="error")
                    yield Label("idle", id="dl-status")
                    yield ProgressBar(id="dl-progress", total=100.0, show_eta=False)
                    yield RichLog(id="dl-log", markup=False, wrap=True)
            # -------------------------------------------------- watchlist tab
            with TabPane("Watchlist", id="watchlist"):
                with Vertical(classes="fill"):
                    with Horizontal(id="wl-actions"):
                        yield Button("Continue", id="btn-wl-continue", variant="primary")
                        yield Button("Remove", id="btn-wl-remove", variant="error")
                        yield Button("Refresh", id="btn-wl-refresh")
                    yield ListView(id="wl-list")
            # ---------------------------------------------------- activity tab
            with TabPane("Activity", id="activity"):
                yield RichLog(id="activity-log", markup=False, wrap=True)
            # -------------------------------------------------------- help tab
            with TabPane("Help", id="help"):
                yield Static(self._help_text, id="help")
        yield Footer()

    def on_mount(self) -> None:
        # providers ---------------------------------------------------------
        self._providers = available_providers()
        default_provider = (
            self.cfg.provider
            if self.cfg.provider in self._providers
            else self._default_provider
        )
        self.query_one("#sel-provider", Select).set_options(
            [(name, name) for name in sorted(self._providers)]
        )
        self.query_one("#sel-provider", Select).value = default_provider
        self._provider = self._make_provider(default_provider)

        # search filters ----------------------------------------------------
        this_year = datetime.now().year
        self.query_one("#sel-year", Select).set_options(
            [("Any", None)] + [(str(y), y) for y in range(this_year + 1, 1969, -1)]
        )
        self.query_one("#sel-season", Select).set_options(
            [("Any", None)] + [(s.name.capitalize(), s) for s in Season]
        )
        self.query_one("#sel-media", Select).set_options(
            [("Any", None)] + [(m.name.capitalize(), m) for m in MediaType]
        )

        # episodes tab selects ----------------------------------------------
        self.query_one("#sel-lang", Select).set_options(
            [(lang.value, lang) for lang in LANGS]
        )
        self._suppress_lang_changed = True
        self.query_one("#sel-lang", Select).value = (
            LanguageTypeEnum.DUB if self.cfg.language == "dub" else LanguageTypeEnum.SUB
        )
        self._suppress_lang_changed = False
        self.query_one("#sel-quality", Select).set_options(
            [(q, q) for q in QUALITIES]
        )
        self.query_one("#sel-quality", Select).value = (
            self.cfg.quality if self.cfg.quality in QUALITIES else "best"
        )

        # downloads tab ------------------------------------------------------
        self.query_one("#sel-dl-quality", Select).set_options(
            [(q, q) for q in QUALITIES]
        )
        self.query_one("#sel-dl-quality", Select).value = (
            self.cfg.quality if self.cfg.quality in QUALITIES else "best"
        )
        self.query_one("#sel-container", Select).set_options(
            [(c, c) for c in CONTAINERS]
        )
        self.query_one("#sel-container", Select).value = (
            self.cfg.container if self.cfg.container in CONTAINERS else ".mkv"
        )
        self.query_one("#dl-path", Input).value = self.cfg.download_dir

        # tables -------------------------------------------------------------
        results = self.query_one("#results-table", DataTable)
        results.add_column("Provider", key="prov")
        results.add_column("Name", key="name")
        results.add_column("Audio", key="audio")

        eps = self.query_one("#eps-table", DataTable)
        self._col_ep = eps.add_column("Episode", key="ep")
        self._col_state = eps.add_column("State", key="state")

        # misc ---------------------------------------------------------------
        self.query_one("#streams-hint", Label).update(
            Text("Select an episode first (p / enter on Episodes tab).")
        )
        self.refresh_watchlist()
        self.log_line(f"{APP_NAME} ready — anipy-api {anipy_version}")
        self.log_line(f"config dir: {CONFIG_DIR}")
        if shutil.which("mpv"):
            self.log_line("player: mpv found")
        elif shutil.which("vlc"):
            self.log_line("player: vlc found")
        else:
            self.log_line("player: neither mpv nor vlc found - install one!")
        self.log_line(SUPPORT_LANG_TIP)
        self.query_one("#query-input", Input).focus()

    # ------------------------------------------------------------ UI helpers
    @property
    def tabs(self) -> TabbedContent:
        return self.query_one("#tabs", TabbedContent)

    def log_line(self, message: str) -> None:
        try:
            self.query_one("#activity-log", RichLog).write(
                f"{datetime.now():%H:%M:%S}  {message}"
            )
        except Exception:
            pass

    def _make_provider(self, name: str) -> Optional[BaseProvider]:
        cls = self._providers.get(name)
        if cls is None:
            return None
        return cls(info_callback=self._provider_info_cb)

    def _provider_info_cb(self, message: str, exc_info=None) -> None:
        try:
            self.call_from_thread(self.log_line, f"[provider] {message}")
        except Exception:
            pass

    def _set_info_panel(self, text: Union[str, Text, RichTable]) -> None:
        self.query_one("#info-panel", Static).update(text)

    def _set_now_title(self, anime: Optional[Anime]) -> None:
        label = self.query_one("#now-title", Label)
        if anime is None:
            label.update("")
        else:
            langs = "/".join(l.value for l in sorted(anime.languages, key=str))
            label.update(Text(f"{anime.name}   [{anime.provider} | {langs}]"))

    def _quality_pref(self, select_id: str) -> Union[str, int]:
        value = self.query_one(select_id, Select).value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value if isinstance(value, str) else "best"

    # -------------------------------------------------------------- search --
    @on(Button.Pressed, "#btn-search")
    def on_search_pressed(self) -> None:
        self.start_search()

    @on(Input.Submitted, "#query-input")
    def on_search_submitted(self) -> None:
        self.start_search()

    def start_search(self) -> None:
        query = self.query_one("#query-input", Input).value.strip()
        provider_name = self.query_one("#sel-provider", Select).value
        if provider_name is None or provider_name not in self._providers:
            self.notify("Pick a provider first", severity="warning")
            return

        # switch provider if needed
        if self._provider is None or str(self._provider) != provider_name:
            self._provider = self._make_provider(provider_name)
            self.cfg.provider = provider_name

        year = self.query_one("#sel-year", Select).value
        season = self.query_one("#sel-season", Select).value
        media = self.query_one("#sel-media", Select).value
        filters = Filters(year=year, season=season, media_type=media)

        self._set_info_panel(
            Text(f"Searching '{provider_name}' for '{query or 'anything'}'...")
        )
        self.run_search(query, provider_name, filters)

    @work(thread=True, group="search", exclusive=True)
    def run_search(
        self, query: str, provider_name: str, filters: Filters
    ) -> None:
        try:
            provider = self._make_provider(provider_name)
            if provider is None:
                raise RuntimeError(f"provider '{provider_name}' unavailable")
            results = provider.get_search(query, filters)
            animes = [Anime.from_search_result(provider, r) for r in results]
            self.call_from_thread(self._search_done, animes, provider_name)
        except Exception as exc:
            self.call_from_thread(self._search_failed, str(exc))

    def _search_done(self, animes: List[Anime], provider_name: str) -> None:
        self._results = animes
        table = self.query_one("#results-table", DataTable)
        table.clear()
        for anime in animes:
            table.add_row(
                Text(str(anime.provider)),
                Text(anime.name),
                Text("/".join(l.value for l in sorted(anime.languages, key=str))),
            )
        if not animes:
            self.notify("No results found", severity="warning")
            self._set_info_panel(Text("No results found."))
        else:
            self.notify(f"{len(animes)} results — enter to open")
            self._set_info_panel(
                Text(f"{len(animes)} results from {provider_name}. Enter = open.")
            )
        self.log_line(f"search done: {len(animes)} result(s) on {provider_name}")
        # only steal focus if the user is still looking at the search tab
        if self.tabs.active == "search":
            table.focus()

    def _search_failed(self, error: str) -> None:
        self.notify(f"Search failed: {error}", severity="error", timeout=8)
        self._set_info_panel(Text(f"Search failed: {error}"))
        self.log_line(f"search failed: {error}")

    # ------------------------------------------------- anime / episodes -----
    @on(DataTable.RowSelected, "#results-table")
    def on_result_selected(self, event: DataTable.RowSelected) -> None:
        index = event.cursor_row
        if 0 <= index < len(self._results):
            self.select_anime(self._results[index])

    def select_anime(self, anime: Anime, jump_ep: Optional[Episode] = None) -> None:
        self._current = anime
        self._jump_ep = jump_ep
        self._streams = []
        self.query_one("#streams-list", ListView).clear()
        self._set_now_title(anime)
        self._activate_tab("episodes")

        # rebuild the language select according to what this anime offers
        lang_select = self.query_one("#sel-lang", Select)
        offered = [l for l in LANGS if l in anime.languages] or [LANGS[0]]
        self._suppress_lang_changed = True
        lang_select.set_options([(l.value, l) for l in offered])
        if self._current_lang not in offered:
            self._current_lang = offered[0]
        lang_select.value = self._current_lang
        self._suppress_lang_changed = False

        self.load_info(anime)
        self.load_episodes(anime, self._current_lang)

    @work(thread=True, group="info", exclusive=True)
    def load_info(self, anime: Anime) -> None:
        try:
            info = anime.get_info()
            self.call_from_thread(self._info_loaded, anime, info)
        except Exception as exc:
            self.call_from_thread(self.log_line, f"info failed: {exc}")

    def _info_loaded(self, anime: Anime, info: ProviderInfoResult) -> None:
        if self._current is not anime:
            return
        table = RichTable(show_header=False, box=None, pad_edge=False)
        table.add_row(Text("Title", style="bold"), Text(anime.name))
        if info.release_year:
            table.add_row(Text("Year", style="bold"), Text(str(info.release_year)))
        if info.status:
            table.add_row(Text("Status", style="bold"), Text(str(info.status)))
        if info.genres:
            table.add_row(Text("Genres", style="bold"), Text(", ".join(info.genres)))
        langs = "/".join(l.value for l in sorted(anime.languages, key=str))
        table.add_row(Text("Audio", style="bold"), Text(langs))
        if info.synopsis:
            table.add_row(Text("Synopsis", style="bold"), Text(info.synopsis.strip()))
        self._set_info_panel(table)
        self.log_line(f"info loaded for '{anime.name}'")

    @work(thread=True, group="episodes", exclusive=True)
    def load_episodes(self, anime: Anime, lang: LanguageTypeEnum) -> None:
        try:
            eps = sorted(set(anime.get_episodes(lang)), key=float)
            self.call_from_thread(self._episodes_loaded, anime, lang, eps)
        except Exception as exc:
            self.call_from_thread(self._episodes_failed, str(exc))

    def _episodes_failed(self, error: str) -> None:
        self.notify(f"Failed to load episodes: {error}", severity="error", timeout=8)
        self.log_line(f"episodes failed: {error}")

    def _episodes_loaded(
        self, anime: Anime, lang: LanguageTypeEnum, eps: List[Episode]
    ) -> None:
        if self._current is not anime or lang != self._current_lang:
            return
        self._current_eps = eps
        table = self.query_one("#eps-table", DataTable)
        table.clear()
        self._ep_row_keys.clear()

        watched: Optional[float] = None
        try:
            entry = self._history.get(anime)
            if entry is not None:
                watched = float(entry.episode)
        except Exception:
            watched = None

        for ep in eps:
            mark = ""
            if watched is not None and float(ep) <= watched:
                mark = "watched"
            row_key = table.add_row(Text(fmt_ep(ep)), Text(mark), key=str(ep))
            self._ep_row_keys[str(ep)] = row_key

        # where to put the cursor: explicit jump > history > first episode
        start = eps[0] if eps else None
        if eps:
            if self._jump_ep is not None and any(
                abs(float(e) - float(self._jump_ep)) < 1e-9 for e in eps  # type: ignore[arg-type]
            ):
                start = next(
                    e for e in eps if abs(float(e) - float(self._jump_ep)) < 1e-9  # type: ignore[arg-type]
                )
            elif watched is not None:
                after = [e for e in eps if float(e) >= watched]
                start = after[0] if after else eps[-1]
        self._jump_ep = None
        self._current_ep = start

        if start is not None:
            try:
                table.move_cursor(row=eps.index(start))
            except Exception:
                pass

        count = len(eps)
        self.log_line(f"{count} episode(s) loaded for '{anime.name}' ({lang})")
        self.notify(f"{count} episode(s) loaded — enter/p to play")

    @on(DataTable.RowHighlighted, "#eps-table")
    def on_ep_cursor_moved(self, event: DataTable.RowHighlighted) -> None:
        ep = self._ep_from_key(getattr(event, "row_key", None))
        if ep is not None:
            self._current_ep = ep
            self.query_one("#ep-hint", Label).update(
                Text(f"Selected: EP {fmt_ep(ep)}    (enter/p = play, d = download)")
            )

    def _ep_from_key(self, row_key) -> Optional[Episode]:
        if row_key is None:
            return None
        value = getattr(row_key, "value", row_key)
        try:
            wanted = float(value)
        except (TypeError, ValueError):
            return None
        for e in self._current_eps:
            if abs(float(e) - wanted) < 1e-9:
                return e
        return None

    @on(Select.Changed, "#sel-lang")
    def on_lang_changed(self, event: Select.Changed) -> None:
        if self._suppress_lang_changed or event.value is Select.NULL:
            return
        if event.value == self._current_lang:
            return  # echo of a programmatic set_options/value assignment
        self._current_lang = event.value
        self.cfg.language = str(event.value)
        if self._current is not None:
            self.load_episodes(self._current, self._current_lang)

    @on(Select.Changed, "#sel-provider")
    def on_provider_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        self._provider = self._make_provider(event.value)
        self.cfg.provider = event.value
        self.log_line(f"provider switched to {event.value}")

    # ------------------------------------------------------------ playback --
    def _resolve_player(self) -> Optional[Tuple[Type, str]]:
        exe = self.cfg.player_path.strip() or shutil.which(self.cfg.player)
        if not exe:
            return None
        return (Mpv if self.cfg.player == "mpv" else Vlc), exe

    @on(Button.Pressed, "#btn-play")
    def on_play_pressed(self) -> None:
        self.action_play_selected()

    def action_play_selected(self) -> None:
        if self._current is None or self._current_ep is None:
            self.notify("Pick an anime + episode first", severity="warning")
            return
        self.play_best(
            self._current,
            self._current_ep,
            self._current_lang,
            self._quality_pref("#sel-quality"),
        )

    def action_play_next(self) -> None:
        self._play_relative(+1)

    def action_play_prev(self) -> None:
        self._play_relative(-1)

    def _play_relative(self, delta: int) -> None:
        if self._current is None or not self._current_eps:
            self.notify("Nothing playing", severity="warning")
            return
        anchor = self._last_ep if self._last_ep is not None else self._current_ep
        if anchor is None:
            return
        idx = next(
            (i for i, e in enumerate(self._current_eps) if float(e) == float(anchor)),
            None,
        )
        if idx is None:
            idx = 0
        target = idx + delta
        if not (0 <= target < len(self._current_eps)):
            self.notify("No episode in that direction", severity="warning")
            return
        ep = self._current_eps[target]
        self.play_best(
            self._current,
            ep,
            self._current_lang,
            self._quality_pref("#sel-quality"),
        )

    @work(thread=True, group="fetch-stream", exclusive=True)
    def play_best(
        self, anime: Anime, ep: Episode, lang: LanguageTypeEnum, pref: Union[str, int]
    ) -> None:
        try:
            stream = anime.get_video(ep, lang, preferred_quality=pref)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Could not get stream: {exc}", severity="error", timeout=8
            )
            return
        if stream is None:
            self.call_from_thread(
                self.notify,
                f"No stream found for EP {fmt_ep(ep)} ({lang}) — try dub or another provider",
                severity="warning",
                timeout=8,
            )
            return
        self.call_from_thread(self.play_stream, anime, stream)

    def play_stream(self, anime: Anime, stream: ProviderStream) -> None:
        resolved = self._resolve_player()
        if resolved is None:
            self.notify(
                f"Player '{self.cfg.player}' not found — install mpv (or vlc)",
                severity="error",
                timeout=8,
            )
            return
        player_cls, exe = resolved

        try:
            if self._player is not None:
                self._player.kill_player()
            extra = self.cfg.mpv_args if player_cls is Mpv else []
            player = player_cls(exe, extra_args=list(extra))
            if player_cls is Mpv and stream.container is None:
                # drop "--demuxer-lavf-format={container}" (would format to "None")
                player.player_args_template = [
                    a for a in player.player_args_template
                    if "demuxer-lavf-format" not in a
                ]
            player.play_title(anime, stream)
        except PlayerError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            return
        except Exception as exc:
            self.notify(f"Failed to start player: {exc}", severity="error", timeout=8)
            return

        self._player = player
        self._play_token += 1
        token = self._play_token
        self._last_ep = stream.episode
        self._last_stream = stream
        self._current_ep = stream.episode

        try:
            self._history.update(
                anime, episode=stream.episode, language=stream.language
            )
        except Exception:
            pass

        self._refresh_ep_marks()
        self._set_now_title(anime)
        self.query_one("#streams-hint", Label).update(
            Text(
                f"Now: EP {fmt_ep(stream.episode)}  {stream.language} "
                f"{stream.resolution}p"
            )
        )
        self.log_line(
            f"playing '{anime.name}' EP {fmt_ep(stream.episode)} "
            f"[{stream.language}/{stream.resolution}p] via {self.cfg.player}"
        )
        threading.Thread(
            target=self._watch_player, args=(token, player), daemon=True
        ).start()

    def _watch_player(self, token: int, player) -> None:
        proc = getattr(player, "_sub_proc", None)
        if proc is None:
            return
        try:
            proc.wait()
        except Exception:
            return
        try:
            self.call_from_thread(self._player_exited, token)
        except Exception:
            pass

    def _player_exited(self, token: int) -> None:
        if token != self._play_token:
            return  # a newer playback already started
        self.log_line("player closed")
        if not self.cfg.auto_next:
            return
        if self._current is None or self._last_ep is None:
            return
        idx = next(
            (
                i
                for i, e in enumerate(self._current_eps)
                if float(e) == float(self._last_ep)
            ),
            None,
        )
        if idx is None or idx + 1 >= len(self._current_eps):
            self.notify("End of episode list", severity="information")
            return
        nxt = self._current_eps[idx + 1]
        self.notify(f"Auto-next: EP {fmt_ep(nxt)}")
        self.play_best(
            self._current,
            nxt,
            self._current_lang,
            self._quality_pref("#sel-quality"),
        )

    def _refresh_ep_marks(self) -> None:
        if self._current is None:
            return
        table = self.query_one("#eps-table", DataTable)
        watched: Optional[float] = None
        try:
            entry = self._history.get(self._current)
            if entry is not None:
                watched = float(entry.episode)
        except Exception:
            watched = None
        for ep in self._current_eps:
            key = self._ep_row_keys.get(str(ep))
            if key is None or self._col_state is None:
                continue
            if self._last_ep is not None and float(ep) == float(self._last_ep):
                mark = "NOW"
            elif watched is not None and float(ep) <= watched:
                mark = "watched"
            else:
                mark = ""
            try:
                table.update_cell(key, self._col_state, Text(mark))
            except Exception:
                pass

    # -------------------------------------------------------------- streams -
    @on(Button.Pressed, "#btn-streams")
    def on_streams_btn(self) -> None:
        self.action_show_streams()

    def action_show_streams(self) -> None:
        if self._current is None or self._current_ep is None:
            self.notify("Pick an episode first", severity="warning")
            return
        self._activate_tab("streams")
        self.refresh_streams()

    @on(Button.Pressed, "#btn-streams-refresh")
    def on_streams_refresh(self) -> None:
        self.refresh_streams()

    @on(Button.Pressed, "#btn-streams-best")
    def on_streams_best(self) -> None:
        self.action_play_selected()

    def refresh_streams(self) -> None:
        if self._current is None or self._current_ep is None:
            self.notify("Pick an episode first", severity="warning")
            return
        self.query_one("#streams-hint", Label).update(Text("Loading streams..."))
        self.run_get_streams(self._current, self._current_ep, self._current_lang)

    @work(thread=True, group="streams", exclusive=True)
    def run_get_streams(
        self, anime: Anime, ep: Episode, lang: LanguageTypeEnum
    ) -> None:
        try:
            streams = anime.get_videos(ep, lang)
            self.call_from_thread(self._streams_loaded, anime, ep, lang, streams)
        except Exception as exc:
            self.call_from_thread(self._streams_failed, str(exc))

    def _streams_loaded(
        self,
        anime: Anime,
        ep: Episode,
        lang: LanguageTypeEnum,
        streams: List[ProviderStream],
    ) -> None:
        if self._current is not anime:
            return
        self._streams = streams
        lv = self.query_one("#streams-list", ListView)
        lv.clear()
        for s in streams:
            subs = f" | {len(s.subtitle)} sub track(s)" if s.subtitle else ""
            lv.append(
                ListItem(
                    Label(
                        Text(
                            f"{s.resolution:>4}p  {s.language}  "
                            f"{s.container or 'unknown'}{subs}"
                        )
                    )
                )
            )
        self.query_one("#streams-hint", Label).update(
            Text(
                f"EP {fmt_ep(ep)} ({lang}) - {len(streams)} stream(s). "
                "Enter = play selected."
            )
        )
        self.log_line(f"{len(streams)} stream(s) for EP {fmt_ep(ep)}")
        if not streams:
            self.notify("No streams found", severity="warning")

    def _streams_failed(self, error: str) -> None:
        self.query_one("#streams-hint", Label).update(Text("Failed to load streams."))
        self.notify(f"Failed to get streams: {error}", severity="error", timeout=8)

    @on(ListView.Selected, "#streams-list")
    def on_stream_selected(self, event: ListView.Selected) -> None:
        if self._current is None or not (0 <= event.index < len(self._streams)):
            return
        self.play_stream(self._current, self._streams[event.index])

    # ------------------------------------------------------------ downloads -
    @on(Select.Changed, "#sel-dl-quality")
    def on_dl_quality_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self.cfg.quality = event.value

    @on(Select.Changed, "#sel-container")
    def on_container_changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self.cfg.container = event.value

    @on(Switch.Changed, "#sw-ffmpeg")
    def on_ffmpeg_switched(self, event: Switch.Changed) -> None:
        self.cfg.use_ffmpeg = event.value

    @on(Button.Pressed, "#btn-dl-quick")
    def on_dl_quick(self) -> None:
        if self._current is None or self._current_ep is None:
            self.notify("Pick an episode first", severity="warning")
            return
        self.query_one("#dl-range", Input).value = fmt_ep(self._current_ep)
        self._activate_tab("downloads")
        self.start_download()

    @on(Button.Pressed, "#btn-dl-start")
    def on_dl_start(self) -> None:
        self.start_download()

    @on(Button.Pressed, "#btn-dl-stop")
    def on_dl_stop(self) -> None:
        if self._dl_running:
            self._dl_stop = True
            self._dl_log_line("Stop requested — finishing current episode first...")
            self.query_one("#dl-status", Label).update("stopping...")

    def start_download(self) -> None:
        if self._dl_running:
            self.notify("A download is already running", severity="warning")
            return
        if self._current is None or self._current_lang is None:
            self.notify("Pick an anime first", severity="warning")
            return
        if not self._current_eps:
            self.notify("No episodes loaded yet", severity="warning")
            return

        eps = parse_ep_range(
            self.query_one("#dl-range", Input).value, self._current_eps
        )
        if not eps:
            self.notify("No valid episodes in that range", severity="warning")
            return

        dl_dir = Path(
            self.query_one("#dl-path", Input).value.strip() or self.cfg.download_dir
        ).expanduser()
        self.cfg.download_dir = str(dl_dir)

        container = self.query_one("#sel-container", Select).value
        use_ff = self.query_one("#sw-ffmpeg", Switch).value
        if use_ff and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            self.notify(
                "ffmpeg/ffprobe not found — download may fail for some streams",
                severity="warning",
                timeout=8,
            )

        self._dl_stop = False
        self._dl_running = True
        self.run_download(
            self._current,
            self._current_lang,
            eps,
            dl_dir,
            container,
            use_ff,
            self._quality_pref("#sel-dl-quality"),
        )

    def _get_downloader(self) -> Downloader:
        if self._downloader is None:
            self._downloader = Downloader(
                progress_callback=self._dl_progress_cb,
                info_callback=self._dl_info_cb,
                soft_error_callback=self._dl_info_cb,
            )
        return self._downloader

    def _dl_progress_cb(self, percentage: float) -> None:
        try:
            self.call_from_thread(
                self._dl_set_progress, max(0.0, min(100.0, percentage))
            )
        except Exception:
            pass

    def _dl_set_progress(self, value: float) -> None:
        try:
            self.query_one("#dl-progress", ProgressBar).update(progress=value)
        except Exception:
            pass

    def _dl_info_cb(self, message: str, exc_info=None) -> None:
        try:
            self.call_from_thread(self._dl_log_line, message)
        except Exception:
            pass

    def _dl_log_line(self, message: str) -> None:
        try:
            self.query_one("#dl-log", RichLog).write(
                f"{datetime.now():%H:%M:%S}  {message}"
            )
        except Exception:
            pass

    @work(thread=True, group="download", exclusive=False)
    def run_download(
        self,
        anime: Anime,
        lang: LanguageTypeEnum,
        eps: List[Episode],
        dl_dir: Path,
        container: str,
        use_ff: bool,
        pref: Union[str, int],
    ) -> None:
        try:
            dl = self._get_downloader()
            total = len(eps)
            done = 0
            for i, ep in enumerate(eps, 1):
                if self._dl_stop:
                    break
                self.call_from_thread(
                    self._dl_begin_ep, anime, ep, i, total
                )
                try:
                    stream = anime.get_video(ep, lang, preferred_quality=pref)
                except Exception as exc:
                    self.call_from_thread(
                        self._dl_log_line, f"EP {fmt_ep(ep)}: stream error: {exc}"
                    )
                    continue
                if stream is None:
                    self.call_from_thread(
                        self._dl_log_line, f"EP {fmt_ep(ep)}: no stream, skipped"
                    )
                    continue
                fname = f"{safe_name(anime.name)} - EP {fmt_ep(ep)} [{lang}]"
                try:
                    path = dl.download(
                        stream,
                        dl_dir / fname,
                        container=container,
                        ffmpeg=use_ff,
                    )
                    done += 1
                    self.call_from_thread(
                        self._dl_ep_done, ep, path
                    )
                except Exception as exc:
                    self.call_from_thread(
                        self._dl_log_line, f"EP {fmt_ep(ep)}: download failed: {exc}"
                    )
            self.call_from_thread(self._dl_finished, done, total, self._dl_stop)
        except Exception as exc:
            self.call_from_thread(self._dl_finished, 0, len(eps), False, str(exc))

    def _dl_begin_ep(
        self, anime: Anime, ep: Episode, index: int, total: int
    ) -> None:
        self.query_one("#dl-progress", ProgressBar).update(progress=0.0)
        self.query_one("#dl-status", Label).update(
            f"downloading {anime.name} EP {fmt_ep(ep)}  ({index}/{total})"
        )
        self._dl_log_line(f"EP {fmt_ep(ep)}: starting ({index}/{total})")

    def _dl_ep_done(self, ep: Episode, path: Path) -> None:
        self._dl_log_line(f"EP {fmt_ep(ep)}: saved -> {path}")

    def _dl_finished(
        self, done: int, total: int, stopped: bool, error: Optional[str] = None
    ) -> None:
        self._dl_running = False
        progress = self.query_one("#dl-progress", ProgressBar)
        progress.update(progress=100.0 if done == total else 0.0)
        if error:
            self.query_one("#dl-status", Label).update(f"error: {error}")
            self.notify(f"Download error: {error}", severity="error", timeout=8)
        elif stopped:
            self.query_one("#dl-status", Label).update(
                f"stopped ({done}/{total} saved)"
            )
            self.notify(f"Stopped — {done}/{total} saved", severity="warning")
        else:
            self.query_one("#dl-status", Label).update(f"done ({done}/{total})")
            self.notify(f"Downloaded {done}/{total} episode(s)")
        self.log_line(f"download finished: {done}/{total} saved")

    # ------------------------------------------------------------ watchlist -
    def refresh_watchlist(self) -> None:
        try:
            entries = sorted(
                self._watchlist.get_all(), key=lambda e: e.timestamp, reverse=True
            )
        except Exception:
            entries = []
        self._wl_entries = entries
        self._wl_sel = None
        lv = self.query_one("#wl-list", ListView)
        lv.clear()
        for e in entries:
            lv.append(
                ListItem(
                    Label(
                        Text(
                            f"{e.name}   [ {e.language} | EP {fmt_ep(e.episode)} | "
                            f"{e.provider} ]"
                        )
                    )
                )
            )
        if not entries:
            lv.append(ListItem(Label(Text("(watchlist empty — add from Episodes tab)"))))

    @on(ListView.Selected, "#wl-list")
    def on_wl_selected(self, event: ListView.Selected) -> None:
        self._wl_sel = event.index

    @on(Button.Pressed, "#btn-wl-refresh")
    def on_wl_refresh(self) -> None:
        self.refresh_watchlist()

    @on(Button.Pressed, "#btn-add-wl")
    def on_add_wl(self) -> None:
        if self._current is None:
            self.notify("Pick an anime first", severity="warning")
            return
        ep = self._last_ep or self._current_ep or (
            self._current_eps[0] if self._current_eps else 1
        )
        try:
            self._watchlist.update(
                self._current, episode=ep, language=self._current_lang
            )
        except Exception as exc:
            self.notify(f"Could not add: {exc}", severity="error")
            return
        self.refresh_watchlist()
        self.notify(f"Added '{self._current.name}' (EP {fmt_ep(ep)}) to watchlist")
        self.log_line(f"watchlist + {self._current.name}")

    @on(Button.Pressed, "#btn-wl-remove")
    def on_wl_remove(self) -> None:
        idx = self._wl_sel
        if idx is None or not (0 <= idx < len(self._wl_entries)):
            self.notify("Select a watchlist entry first", severity="warning")
            return
        entry = self._wl_entries[idx]
        self.push_screen(
            ConfirmScreen(f"Remove '{entry.name}' from the watchlist?"),
            self._wl_remove_confirmed,
        )

    def _wl_remove_confirmed(self, confirmed: Optional[bool]) -> None:
        if not confirmed or self._wl_sel is None:
            return
        if 0 <= self._wl_sel < len(self._wl_entries):
            entry = self._wl_entries[self._wl_sel]
            try:
                self._watchlist.delete(entry)
            except Exception as exc:
                self.notify(f"Could not remove: {exc}", severity="error")
                return
            self.log_line(f"watchlist - {entry.name}")
            self.refresh_watchlist()
            self.notify("Removed from watchlist")

    @on(Button.Pressed, "#btn-wl-continue")
    def on_wl_continue(self) -> None:
        if not self._wl_entries:
            self.notify("Watchlist is empty", severity="warning")
            return
        idx = self._wl_sel if self._wl_sel is not None else 0
        idx = min(idx, len(self._wl_entries) - 1)
        entry = self._wl_entries[idx]

        provider_cls = self._providers.get(entry.provider)
        if provider_cls is None:
            self.notify(
                f"Provider '{entry.provider}' is not available anymore",
                severity="error",
            )
            return
        try:
            anime = Anime(
                provider_cls(info_callback=self._provider_info_cb),
                entry.name,
                entry.identifier,
                set(entry.languages),
            )
        except Exception as exc:
            self.notify(f"Could not restore entry: {exc}", severity="error")
            return

        self._current_lang = entry.language
        self.notify(f"Continuing {entry.name} at EP {fmt_ep(entry.episode)}")
        self.log_line(f"continue {entry.name} at EP {fmt_ep(entry.episode)}")
        self.select_anime(anime, jump_ep=entry.episode)

    # -------------------------------------------------------------- quit etc
    def _activate_tab(self, tab_id: str) -> None:
        """Switch tab AND move keyboard focus into it.

        Textual 8's TabbedContent follows pane focus: if focus stays behind
        in the old (now hidden) pane, the tab snaps right back. So always
        steer focus: into the new pane's first focusable widget, or nowhere
        (set_focus(None)) when the pane has none (e.g. Help/Activity).
        """
        self.tabs.active = tab_id
        try:
            pane = self.query_one(f"#{tab_id}", TabPane)
            focusable = [w for w in pane.walk_children(Widget) if w.focusable]
            if focusable:
                focusable[0].focus()
            else:
                self.set_focus(None)
        except Exception:
            pass

    def action_focus_search(self) -> None:
        self.tabs.active = "search"
        self.query_one("#query-input", Input).focus()

    def action_tab(self, tab_id: str) -> None:
        self._activate_tab(tab_id)

    def action_download_selected(self) -> None:
        self.on_dl_quick()

    def action_quit(self) -> None:
        # persist current UI settings
        try:
            q = self.query_one("#sel-quality", Select).value
            if isinstance(q, str):
                self.cfg.quality = q
            lang = self.query_one("#sel-lang", Select).value
            if isinstance(lang, LanguageTypeEnum):
                self.cfg.language = str(lang)
            cont = self.query_one("#sel-container", Select).value
            if isinstance(cont, str):
                self.cfg.container = cont
            path = self.query_one("#dl-path", Input).value.strip()
            if path:
                self.cfg.download_dir = path
        except Exception:
            pass
        self.cfg.save()
        try:
            if self._player is not None:
                self._player.kill_player()
        except Exception:
            pass
        self.exit()


# ------------------------------------------------------------------------ main
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Watch & download anime in a terminal TUI (anipy-api + Textual).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} (anipy-api {anipy_version})",
    )
    parser.parse_args()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AnipyTUI().run()


if __name__ == "__main__":
    main()
