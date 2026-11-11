"""Fakes for everything outside the container: MEGA, TMDB, Telegram and Plex. No test talks to the
internet, and no real links, keys or media are in the repo."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from linkarr import prefs  # noqa: E402
from linkarr.config import Config  # noqa: E402
from linkarr.db import Database  # noqa: E402
from linkarr.jobs import Ctx  # noqa: E402
from linkarr.mega import MegaError, Preview  # noqa: E402
from linkarr.tmdb import Tmdb  # noqa: E402
from linkarr.vault import Vault  # noqa: E402

# Made-up link handles and keys in MEGA's format. They point at nothing.
KEY = "A" * 22
MOVIE_URL = f"https://mega.nz/file/Abc12345#{KEY}"
SHOW_S1 = f"https://mega.nz/folder/Fold1111#{KEY}"
SHOW_S2 = f"https://mega.nz/folder/Fold2222#{KEY}"
DOC_URL = f"https://mega.nz/file/Docu1234#{KEY}"


class FakeMega:
    """preview() answers from a table; download() writes the files it promised."""

    def __init__(self):
        self.previews: dict[str, Preview] = {}
        self.failures: dict[str, list[MegaError]] = {}
        self.downloads: list[str] = []

    def add(self, url: str, name: str, files: list[tuple[str, int]], kind: str = "folder"):
        fl = [{"path": p, "size": s} for p, s in files]
        self.previews[url] = Preview(name=name, size=sum(s for _, s in files), kind=kind, files=fl)

    async def preview(self, url: str) -> Preview:
        if url not in self.previews:
            raise MegaError("dead", "Link not found.")
        return self.previews[url]

    async def download(self, url: str, dest: Path, progress=None, cancel=None) -> None:
        self.downloads.append(url)
        fails = self.failures.get(url)
        if fails:
            raise fails.pop(0)
        for f in self.previews[url].files:
            p = dest / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * f["size"])
        if progress:
            progress(self.previews[url].size)

    async def close(self):
        pass


# Small fake TMDB catalogue.
TMDB_MOVIES = {
    101: {"id": 101, "title": "Dune: Part Two", "release_date": "2024-02-27", "poster_path": "/dune.jpg", "genre_ids": [878],
          "genres": [{"id": 878, "name": "Science Fiction"}]},
    102: {"id": 102, "title": "Free Solo", "release_date": "2018-09-28", "poster_path": "/solo.jpg", "genre_ids": [99],
          "genres": [{"id": 99, "name": "Documentary"}]},
}
TMDB_TV = {
    201: {"id": 201, "name": "Severance", "first_air_date": "2022-02-18", "poster_path": "/sev.jpg", "genre_ids": [18],
          "genres": [{"id": 18, "name": "Drama"}], "status": "Returning Series",
          "next_episode_to_air": None, "last_episode_to_air": {"air_date": "2025-03-21", "season_number": 2, "episode_type": "finale"},
          "seasons": [{"season_number": 1, "episode_count": 9, "air_date": "2022-02-18"},
                      {"season_number": 2, "episode_count": 10, "air_date": "2025-01-17"}]},
}

# Movie Tracker answers: release dates by country (type 3 theatrical, 4 digital, 5 physical) and streaming services.
TMDB_RELEASES = {
    101: [{"iso_3166_1": "US", "release_dates": [{"type": 3, "release_date": "2024-03-01T00:00:00.000Z"},
                                                  {"type": 4, "release_date": "2024-04-16T00:00:00.000Z"},
                                                  {"type": 5, "release_date": "2024-05-14T00:00:00.000Z"}]},
          {"iso_3166_1": "IT", "release_dates": [{"type": 5, "release_date": "2024-06-20T00:00:00.000Z"}]}],
}
TMDB_PROVIDERS = {101: {"US": {"flatrate": [{"provider_name": "Max"}], "rent": [{"provider_name": "Apple TV"}]}}}


def tmdb_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/3")
    q = request.url.params.get("query", "").lower()
    if path == "/configuration":
        return httpx.Response(200, json={})
    if path == "/search/movie":
        return httpx.Response(200, json={"results": [m for m in TMDB_MOVIES.values()
                                                     if q.replace(":", "") in m["title"].lower().replace(":", "")]})
    if path == "/search/tv":
        return httpx.Response(200, json={"results": [t for t in TMDB_TV.values() if q in t["name"].lower()]})
    if path.startswith("/movie/") and path.endswith("/release_dates"):
        return httpx.Response(200, json={"results": TMDB_RELEASES.get(int(path.split("/")[2]), [])})
    if path.startswith("/movie/") and path.endswith("/watch/providers"):
        return httpx.Response(200, json={"results": TMDB_PROVIDERS.get(int(path.split("/")[2]), {})})
    if path.startswith("/movie/"):
        m = TMDB_MOVIES.get(int(path.split("/")[-1]))
        return httpx.Response(200, json=m) if m else httpx.Response(404, json={})
    if path.startswith("/tv/"):
        t = TMDB_TV.get(int(path.split("/")[-1]))
        return httpx.Response(200, json=t) if t else httpx.Response(404, json={})
    return httpx.Response(404, json={})


class Sent:
    """Records what the bot or Plex would have sent."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json
        method = request.url.path.rsplit("/", 1)[-1]
        body = _json.loads(request.content or b"{}") if request.method == "POST" else dict(request.url.params)
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "test_bot"}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        if "/library/sections" in request.url.path:
            return httpx.Response(200, json={"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}]}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "media").mkdir()
    return Config(data_dir=tmp_path / "data", media_dir=tmp_path / "media", port=0, worker=False,
                  cookie_secure=False, session_hours=24, default_tz="UTC")


@pytest.fixture
def db(cfg):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    d = Database(cfg.db_path)
    d.set_setting("pref.captcha_hosts", "links.example")  # no captcha site is built in; tests use a made-up one
    yield d
    d.close()


@pytest.fixture
def mega():
    return FakeMega()


@pytest.fixture
def tmdb_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))


@pytest.fixture
def ctx(cfg, db, mega, tmdb_client):
    vault = Vault(db, cfg.key_path)
    vault.set("tmdb_key", "test-key")
    c = Ctx(cfg=cfg, db=db, vault=vault, mega=mega, tmdb=Tmdb(lambda: vault.get("tmdb_key"), tmdb_client))
    prefs.set_many(db, {"check_video": False})  # ffprobe isn't needed for fake files
    return c


@pytest.fixture
def libs(ctx):
    """The four libraries from the design: Movies, TV Shows, Documentaries, Docuseries."""
    from linkarr import jobs
    out = {}
    for name, kind, path, doc in (("Movies", "movie", "Movies", 0), ("TV Shows", "series", "TV Shows", 0),
                                  ("Documentaries", "movie", "Documentaries", 1), ("Docuseries", "series", "Docuseries", 1)):
        (ctx.cfg.media_dir / path).mkdir(parents=True, exist_ok=True)
        out[name] = jobs.save_library(ctx, {"name": name, "kind": kind, "path": path, "documentary": doc})
    return out


def run(coro):
    return asyncio.run(coro)


# Remakes: two "Dune" films a few decades apart.
TMDB_MOVIES[103] = {"id": 103, "title": "Dune", "release_date": "1984-12-14", "poster_path": "/d84.jpg", "genre_ids": [878],
                    "genres": [{"id": 878, "name": "Science Fiction"}]}
TMDB_MOVIES[104] = {"id": 104, "title": "Dune", "release_date": "2021-09-15", "poster_path": "/d21.jpg", "genre_ids": [878],
                    "genres": [{"id": 878, "name": "Science Fiction"}]}
