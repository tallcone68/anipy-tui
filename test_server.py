"""Offline test for anipy-server.py — no network, no real providers.

Runs Session.handle_command() directly with a fake provider, then does a
real loopback socket round-trip through the actual server code path.

Run: python test_server.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="anipy-server-test-")
os.environ["ANIPY_TUI_DIR"] = TMP

import importlib.util

spec = importlib.util.spec_from_file_location(
    "anipy_server", Path(__file__).parent / "anipy-server.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules["anipy_server"] = mod
spec.loader.exec_module(mod)

from anipy_api.provider import LanguageTypeEnum, ProviderStream


# ---------------------------------------------------------------- fake provider
class FakeProvider:
    """Mimics a BaseProvider well enough for Anime.get_* delegation."""

    NAME = "fake"

    def __str__(self) -> str:
        return self.NAME  # real providers do this; LocalList stores str(provider)

    def __init__(self, info_callback=None) -> None:
        self.info_callback = info_callback

    def get_search(self, query, filters=None):
        class R:
            pass

        r1 = R()
        r1.name = "Fake Anime"
        r1.identifier = "fake-1"
        r1.languages = {LanguageTypeEnum.SUB, LanguageTypeEnum.DUB}
        return [r1]

    def get_info(self, identifier):
        class I:
            release_year = 2024
            status = "FINISHED"
            genres = ["Fantasy"]
            synopsis = "  A test anime.  "

        return I()

    def get_episodes(self, identifier, lang):
        return [1, 2, 3, 4.5]

    def get_video(self, identifier, episode, lang):
        return [
            ProviderStream(
                url=f"https://example.com/{episode}-360.m3u8",
                resolution=360,
                episode=episode,
                language=lang,
                container="m3u8",
            ),
            ProviderStream(
                url=f"https://example.com/{episode}-1080.m3u8",
                resolution=1080,
                episode=episode,
                language=lang,
                container="m3u8",
            ),
        ]


def fresh_session() -> mod.Session:
    s = mod.Session()
    s._providers = {"fake": FakeProvider}
    s.cfg.provider = "fake"
    # no real player binary: keeps the test offline and deterministic
    s.cfg.player = "no-such-player-exe"
    return s


def must(condition, label):
    if not condition:
        print(f"FAIL: {label}")
        sys.exit(1)
    print(f"OK: {label}")


# ------------------------------------------------------------------ session tests
s = fresh_session()

r = s.handle_command({"cmd": "status"})
must(r["ok"] and r["current"] is None, "status with no anime open")

r = s.handle_command({"cmd": "search", "query": "fake"})
must(r["ok"] and r["count"] == 1 and r["results"][0]["name"] == "Fake Anime", "search returns results")

r = s.handle_command({"cmd": "search", "query": "x", "provider": "nope"})
must(not r["ok"] and "unknown provider" in r["error"], "search rejects unknown provider")

r = s.handle_command({"cmd": "open", "n": 0})
must(r["ok"] and r["episodes"]["count"] == 4, "open loads episodes via cmd_info+cmd_episodes")
must(r["info"]["synopsis"] == "A test anime.", "info synopsis stripped")

r = s.handle_command({"cmd": "episodes", "lang": "dub"})
must(r["ok"] and r["count"] == 4 and r["selected"] == "1", "episodes with lang switch")

r = s.handle_command({"cmd": "select", "ep": 2})
must(r["ok"] and r["selected"] == "2", "select episode 2")

r = s.handle_command({"cmd": "select", "ep": 99})
must(not r["ok"], "select rejects missing episode")

r = s.handle_command({"cmd": "streams"})
must(
    r["ok"] and r["count"] == 2 and r["streams"][-1]["resolution"] == 1080,
    "streams lists both qualities",
)

r = s.handle_command({"cmd": "play", "ep": 3, "quality": 720})
must(not r["ok"] and "not found on server" in r["error"], "play without player fails cleanly (no mpv here)")

# watchlist round-trip
r = s.handle_command({"cmd": "wladd"})
must(r["ok"] and r["added"] == "Fake Anime", "wladd")

# history round-trip (seed directly — play never got far enough to record)
must(s._current is not None, "current anime set after open")
s._history.update(s._current, episode=2, language=LanguageTypeEnum.SUB)
r = s.handle_command({"cmd": "history"})
must(r["ok"] and r["count"] >= 1 and r["history"][0]["episode"] == "2", "history lists seeded entry")

r = s.handle_command({"cmd": "watchlist"})
must(r["ok"] and r["count"] == 1 and r["watchlist"][0]["provider"] == "fake", "watchlist lists entry")

r = s.handle_command({"cmd": "wlcontinue", "n": 0})
must(r["ok"] and r["continued"] == "Fake Anime" and r["episodes"]["count"] == 4, "wlcontinue restores anime")

r = s.handle_command({"cmd": "wlremove", "n": 0})
must(r["ok"] and r["removed"] == "Fake Anime", "wlremove")

r = s.handle_command({"cmd": "watchlist"})
must(r["ok"] and r["count"] == 0, "watchlist empty after remove")

# deterministic stop test: fake Downloader that blocks mid-download.
# Patch BEFORE starting so the worker picks up the fake.
class FakeDownloader:
    def __init__(self, progress_callback=None, info_callback=None, soft_error_callback=None):
        self.block = threading.Event()
        self.calls = []

    def download(self, stream, path, container=None, ffmpeg=False):
        self.calls.append(path.name)
        self.block.wait(timeout=5)  # hold until the test releases
        return Path(str(path) + container)

real_downloader = mod.Downloader
mod.Downloader = FakeDownloader
try:
    r = s.handle_command({"cmd": "download", "range": "1-2"})
    must(r["ok"] and r["started"] == 2, "download starts (fails per-ep without network, but runs)")

    deadline = time.time() + 3
    while not getattr(s._downloader, "calls", None) and time.time() < deadline:
        time.sleep(0.02)
    must(len(s._downloader.calls) == 1, "download worker started episode 1")

    s._dl_stop = True
    s._downloader.block.set()  # release the in-flight episode
    deadline = time.time() + 3
    while s._dl_running and time.time() < deadline:
        time.sleep(0.02)
    must(not s._dl_running, "download worker exits after stop")
    must(s._dl_status.startswith("stopped"), f"download status stopped ({s._dl_status})")
    must(len(s._downloader.calls) == 1, "stop finishes current episode only")
finally:
    mod.Downloader = real_downloader

r = s.handle_command({"cmd": "nonsense"})
must(not r["ok"] and "unknown cmd" in r["error"], "unknown command rejected")

r = s.handle_command({"cmd": ""})
must(not r["ok"] and "missing 'cmd'" in r["error"], "empty command rejected")

must(mod.parse_ep_range("1-3, 4.5", [1, 2, 3, 4.5]) == [1, 2, 3, 4.5], "parse_ep_range helper")

print("-" * 60)

# ------------------------------------------------------------------ socket test
# Real loopback round-trip through RequestHandler: banner + requests + quit.
# Inject the fake provider so the whole socket flow is offline & deterministic.
# NOTE: keep the patch active for the whole test — Session is constructed per
# connection in setup(), not at server-creation time.
real_avail = mod.available_providers
mod.available_providers = lambda: {"fake": FakeProvider}
# no player binary for the socket session (mpv exists on this machine)
mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
(mod.CONFIG_DIR / "config.json").write_text(json.dumps({"player": "no-such-player-exe"}))
srv = mod.ThreadedTCPServer(("127.0.0.1", 0), mod.RequestHandler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
    f = sock.makefile("rwb")

    def send(obj):
        f.write((json.dumps(obj) + "\n").encode())
        f.flush()
        return json.loads(f.readline().decode())

    banner = json.loads(f.readline().decode())
    must(banner["ok"] and "banner" in banner, "socket: banner on connect")

    def send_raw(line: str):
        f.write((line + "\n").encode())
        f.flush()
        return json.loads(f.readline().decode())

    r = send({"cmd": "status"})
    must(r["ok"] and "providers" in r, "socket: status")

    r = send({"cmd": "help"})
    must(r["ok"] and "search" in r["commands"], "socket: help")

    r = send({"cmd": "episodes"})
    must(not r["ok"] and "no anime open" in r["error"], "socket: error envelope")

    # ---- plain-text mode (what you get typing directly in nc) ----
    r = send_raw("help")
    must(r["ok"] and "plain_text" in r, "text: help works without JSON")

    r = send_raw("status")
    must(r["ok"] and "providers" in r, "text: status")

    r = send_raw("episodes")
    must(not r["ok"] and "no anime open" in r["error"], "text: error envelope")

    r = send_raw("search frieren provider animehub")
    must(not r["ok"] and "unknown provider" in r["error"], "text: search parses provider flag")

    r = send_raw("search frieren")
    must(r["ok"] and r["count"] == 1 and r["results"][0]["name"] == "Fake Anime", "text: search end-to-end")

    r = send_raw("open 0")
    must(r["ok"] and r["episodes"]["count"] == 4, "text: open 0 loads episodes")

    r = send_raw("select 2")
    must(r["ok"] and r["selected"] == "2", "text: select 2")

    r = send_raw("streams")
    must(r["ok"] and r["count"] == 2, "text: streams")

    r = send_raw("play 2")
    must(not r["ok"] and "not found on server" in r["error"], "text: play fails cleanly (no player)")

    r = send_raw("watchlist add")
    must(r["ok"] and r["added"] == "Fake Anime", "text: 'watchlist add' == wladd")

    r = send_raw("watchlist")
    must(r["ok"] and r["count"] == 1, "text: watchlist lists entry")

    r = send_raw("history")
    must(r["ok"] and r["count"] >= 1, "text: history")

    r = send_raw("open")
    must(not r["ok"] and "usage" in r["error"], "text: usage hints")

    r = send_raw("bogus stuff")
    must(not r["ok"] and "unknown command" in r["error"], "text: unknown command rejected")

    r = send_raw("this is not json")
    must(not r["ok"] and "unknown command" in r["error"], "text: non-json prose no longer errors as bad json")

with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
    f = sock.makefile("rwb")
    json.loads(f.readline().decode())  # banner

    # malformed JSON that *looks* like JSON still gets a clear error
    f.write(b"{broken json\n")
    f.flush()
    r = json.loads(f.readline().decode())
    must(not r["ok"] and "bad json" in r["error"], "socket: bad json rejected")

    # plain-text quit aliases
    f.write(b"q\n")
    f.flush()
    r = json.loads(f.readline().decode())
    must(r.get("bye") is True, "socket: plain 'q' closes connection")

srv.shutdown()
srv.server_close()
mod.available_providers = real_avail

print("-" * 60)
print("ALL SERVER TESTS PASSED")
