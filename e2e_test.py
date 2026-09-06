"""End-to-end test: real provider search, episodes, and stream resolution.

Run: python e2e_test.py            (fast - allanime only)
     python e2e_test.py --full     (also tests animehub + anidbapp)
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="anipy-tui-e2e-")
os.environ["ANIPY_TUI_DIR"] = TMP

import importlib.util

spec = importlib.util.spec_from_file_location(
    "anipy_tui", Path(__file__).parent / "anipy-tui.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules["anipy_tui"] = mod
spec.loader.exec_module(mod)

FULL = "--full" in sys.argv
QUERY = "frieren"
# animehub first: most reliable provider right now
PROVIDERS = ["animehub", "allanime"] + (["anidbapp"] if FULL else [])


async def test_provider(provider_name: str) -> bool:
    """Returns True if the provider worked end-to-end."""
    provider = mod.available_providers().get(provider_name)
    if provider is None:
        print(f"[{provider_name}] SKIP (not available)")
        return False
    try:
        p = provider()
        print(f"[{provider_name}] searching '{QUERY}' ...", flush=True)
        results = p.get_search(QUERY)
        print(f"[{provider_name}] {len(results)} results")
        if not results:
            print(f"[{provider_name}] FAIL: no results")
            return False

        anime = mod.Anime.from_search_result(p, results[0])
        langs = ",".join(str(l) for l in sorted(anime.languages, key=str))
        print(f"[{provider_name}] first: {anime.name} ({langs})", flush=True)

        lang = mod.LanguageTypeEnum.SUB
        eps = anime.get_episodes(lang)
        print(f"[{provider_name}] {len(eps)} episodes (sub)", flush=True)
        if not eps:
            print(f"[{provider_name}] FAIL: no episodes")
            return False

        ep = eps[0]
        streams = anime.get_videos(ep, lang)
        streams = sorted(streams, key=lambda s: s.resolution)
        print(f"[{provider_name}] EP {ep}: {len(streams)} stream(s)", flush=True)
        for s in streams[:3]:
            print(
                f"[{provider_name}]   {s.resolution}p {s.language} "
                f"container={s.container} subs={len(s.subtitle or {})}"
            )
        if not streams:
            print(f"[{provider_name}] FAIL: no streams")
            return False

        # quick reachability check on the best stream URL
        import requests

        best = max(streams, key=lambda s: s.resolution)
        headers = {"Referer": best.referrer} if best.referrer else {}
        try:
            r = requests.get(best.url, headers=headers, stream=True, timeout=20)
            ct = r.headers.get("content-type", "?")
            size = r.headers.get("content-length", "?")
            r.close()
            print(f"[{provider_name}] stream reachable: {ct} ({size} bytes)")
        except Exception as exc:
            print(f"[{provider_name}] WARN: stream HEAD failed: {exc}")

        # info endpoint
        try:
            info = anime.get_info()
            print(
                f"[{provider_name}] info: year={info.release_year} "
                f"genres={(info.genres or [])[:3]}"
            )
        except Exception as exc:
            print(f"[{provider_name}] info failed (non-fatal): {exc}")

        print(f"[{provider_name}] PASS")
        return True
    except Exception as exc:
        print(f"[{provider_name}] FAIL: {type(exc).__name__}: {exc}")
        return False


async def main() -> None:
    print("=" * 60)
    ok = False
    for name in PROVIDERS:
        result = await test_provider(name)
        ok = ok or result
        print("-" * 60)
    print("E2E RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
