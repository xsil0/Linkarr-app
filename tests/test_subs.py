"""Subtitles from OpenSubtitles (a fake one: no test talks to the internet)."""
import json

import httpx

from conftest import run
from linkarr import prefs
from linkarr.subs import Subs, has_sub, os_hash


class FakeOS:
    def __init__(self, remaining=5):
        self.remaining = remaining
        self.searches = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if request.url.host == "dl.example":
            return httpx.Response(200, content=f"1\n00:00:01,000 --> 00:00:02,000\nfile {p}\n".encode())
        assert request.headers["Api-Key"] == "os-key"
        if p.endswith("/login"):
            return httpx.Response(200, json={"token": "tok", "base_url": "api.opensubtitles.com"})
        if p.endswith("/subtitles"):
            self.searches.append(dict(request.url.params))
            return httpx.Response(200, json={"data": [
                {"attributes": {"download_count": 900, "hearing_impaired": True, "files": [{"file_id": 1, "file_name": "a.srt"}]}},
                {"attributes": {"download_count": 50, "moviehash_match": True, "files": [{"file_id": 2, "file_name": "b.srt"}]}},
            ]})
        if p.endswith("/download"):
            if self.remaining <= 0:
                return httpx.Response(406, json={"message": "You have downloaded your allowed 20 subtitles"})
            self.remaining -= 1
            fid = json.loads(request.content)["file_id"]
            return httpx.Response(200, json={"link": f"https://dl.example/{fid}.srt", "remaining": self.remaining})
        return httpx.Response(404)


def setup(ctx, fake):
    ctx.vault.set("opensubtitles_key", "os-key")
    prefs.set_many(ctx.db, {"subs_enabled": True, "subs_languages": "en, it"})
    return Subs(ctx, httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))


def video(path, size=200_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


def test_hash_matches_the_reference_algorithm(tmp_path):
    f = video(tmp_path / "x.mkv", 256 * 1024)
    data = f.read_bytes()
    h = len(data)
    for chunk in (data[:65536], data[-65536:]):
        for i in range(0, 65536, 8):
            h = (h + int.from_bytes(chunk[i:i + 8], "little")) % 2 ** 64
    assert os_hash(f) == f"{h:016x}"
    assert os_hash(video(tmp_path / "small.mkv", 1000)) == ""


def test_movie_gets_a_subtitle_per_language(ctx, tmp_path):
    fake = FakeOS()
    subs = setup(ctx, fake)
    v = video(tmp_path / "Dune - Part Two (2024)" / "Dune - Part Two (2024).mkv")
    assert run(subs.for_files([v], "movie", 101)) == 2
    assert (v.parent / "Dune - Part Two (2024).en.srt").read_text().endswith("file /2.srt\n")  # the hash match wins
    assert (v.parent / "Dune - Part Two (2024).it.srt").exists()
    assert fake.searches[0]["tmdb_id"] == "101" and fake.searches[0]["type"] == "movie" and fake.searches[0]["moviehash"]


def test_existing_subtitle_is_kept(ctx, tmp_path):
    fake = FakeOS()
    subs = setup(ctx, fake)
    v = video(tmp_path / "Show" / "Season 1" / "Show - S01E02.mkv")
    (v.parent / "Show - S01E02.srt").write_text("mine")
    assert has_sub(v, "en") and not has_sub(v, "it")
    assert run(subs.for_files([v], "series", 201)) == 1
    assert (v.parent / "Show - S01E02.srt").read_text() == "mine"
    assert fake.searches[0]["parent_tmdb_id"] == "201" and fake.searches[0]["season_number"] == "1" \
        and fake.searches[0]["episode_number"] == "2"


def test_stops_for_the_day_when_the_allowance_runs_out(ctx, tmp_path):
    fake = FakeOS(remaining=1)
    subs = setup(ctx, fake)
    prefs.set_many(ctx.db, {"subs_languages": "en"})
    vids = [video(tmp_path / "S" / f"Show - S01E0{n}.mkv") for n in (1, 2, 3)]
    assert run(subs.for_files(vids, "series", 201)) == 1
    assert len(fake.searches) == 2
    assert run(subs.for_files(vids, "series", 201)) == 0 and len(fake.searches) == 2  # paused


def test_off_unless_switched_on_with_a_key(ctx):
    subs = Subs(ctx, httpx.AsyncClient(transport=httpx.MockTransport(FakeOS().handler)))
    assert not subs.enabled()
    prefs.set_many(ctx.db, {"subs_enabled": True})
    assert not subs.enabled()
    ctx.vault.set("opensubtitles_key", "os-key")
    assert subs.enabled()


def test_worker_fetches_subtitles_after_filing(ctx, libs, mega):
    import asyncio

    from conftest import MOVIE_URL
    from linkarr import jobs
    from test_flow import drive
    from linkarr.worker import Worker

    ctx.subs = setup(ctx, FakeOS())
    prefs.set_many(ctx.db, {"subs_languages": "en"})
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 200_000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        await asyncio.gather(*w.side_tasks)

    run(go())
    folder = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)"
    assert (folder / "Dune - Part Two (2024).en.srt").exists()
