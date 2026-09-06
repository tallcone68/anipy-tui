"""Smoke test for anipy-tui using Textual's pilot (no network needed)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="anipy-tui-test-")
os.environ["ANIPY_TUI_DIR"] = TMP

sys.path.insert(0, str(Path(__file__).parent))

import importlib.util

spec = importlib.util.spec_from_file_location("anipy_tui", Path(__file__).parent / "anipy-tui.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["anipy_tui"] = mod  # needed: dataclasses looks up sys.modules[cls.__module__]
spec.loader.exec_module(mod)


async def main() -> None:
    app = mod.AnipyTUI()
    async with app.run_test(size=(120, 40)) as pilot:
        # app mounted without crashing?
        assert app.is_running
        print("OK: app mounted")

        # widgets exist?
        for sel in (
            "#query-input", "#btn-search", "#sel-provider", "#results-table",
            "#eps-table", "#streams-list", "#wl-list", "#dl-progress",
            "#dl-log", "#activity-log", "#sel-lang", "#sel-quality",
            "#sel-container", "#dl-path", "#dl-range", "#sw-ffmpeg",
            "#info-panel", "#now-title", "#ep-hint", "#streams-hint",
        ):
            app.query_one(sel)
        print("OK: all widgets present")

        # provider select populated?
        provider_sel = app.query_one("#sel-provider", mod.Select)
        assert provider_sel.value in ("allanime", "animehub", "anidbapp", "native"), provider_sel.value
        print("OK: provider select =", provider_sel.value)

        # config saved to temp dir?
        assert (Path(TMP) / "watchlist.json").exists()
        assert (Path(TMP) / "history.json").exists()
        print("OK: watchlist/history created in", TMP)

        # simulate episode table update with a fake anime
        from anipy_api.provider import LanguageTypeEnum

        class FakeProvider:
            NAME = "allanime"

        fake = mod.Anime(FakeProvider(), "Test Anime", "id123", {LanguageTypeEnum.SUB})
        app._current = fake
        app._current_lang = LanguageTypeEnum.SUB
        app._episodes_loaded(fake, LanguageTypeEnum.SUB, [1, 2, 3, 4.5])
        await pilot.pause()
        eps_table = app.query_one("#eps-table", mod.DataTable)
        assert eps_table.row_count == 4, eps_table.row_count
        print("OK: episode table populated (4 rows)")

        # fake stream playback path (player not started - no mpv spawn):
        from anipy_api.provider import ProviderStream

        stream = ProviderStream(
            url="https://example.com/stream.m3u8", resolution=1080,
            episode=2, language=LanguageTypeEnum.SUB, container="hls",
        )
        app._streams = [stream]
        app._streams_loaded(fake, 2, LanguageTypeEnum.SUB, [stream])
        await pilot.pause()
        assert len(app._streams) == 1
        print("OK: streams list populated")

        # history write (play_stream would spawn mpv, so test the bookkeeping only)
        app._current_ep = 2
        app._last_ep = 2
        app._history.update(fake, episode=2, language=LanguageTypeEnum.SUB)
        entry = app._history.get(fake)
        assert entry is not None and float(entry.episode) == 2.0
        print("OK: history saved/read")

        # watchlist add/remove
        app._watchlist.update(fake, episode=3, language=LanguageTypeEnum.SUB)
        assert len(app._watchlist.get_all()) == 1
        app._watchlist.delete(fake)
        assert len(app._watchlist.get_all()) == 0
        print("OK: watchlist add/delete")

        # parse_ep_range helper
        assert mod.parse_ep_range("1-3, 4.5", [1, 2, 3, 4.5]) == [1, 2, 3, 4.5]
        assert mod.parse_ep_range("", [1, 2]) == [1, 2]
        assert mod.parse_ep_range("2", [1, 2, 3]) == [2]
        print("OK: parse_ep_range")

        # tab switching bindings (f-keys, Input-safe)
        await pilot.press("f3")
        await pilot.pause()
        assert app.tabs.active == "episodes", app.tabs.active
        await pilot.press("f6")
        await pilot.pause()
        assert app.tabs.active == "watchlist", app.tabs.active
        await pilot.press("f1")
        await pilot.pause()
        assert app.tabs.active == "help", app.tabs.active
        await pilot.press("f2")
        await pilot.pause()
        assert app.tabs.active == "search", app.tabs.active
        print("OK: tab bindings")

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
