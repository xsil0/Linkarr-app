"""0.6.0: Movie Tracker, download site link, new themes, Recently finished."""
import json

import httpx
import pytest
from conftest import TMDB_PROVIDERS, FakeMega, Sent, run, tmdb_handler
from fastapi.testclient import TestClient
from test_api import H, sign_up

from linkarr import movietracker, prefs, roundup
from linkarr.main import create_app
from linkarr.worker import Worker


class Texts:
    def __init__(self):
        self.texts = []

    async def send(self, text, buttons=None):
        self.texts.append(text)


def test_release_info_picks_your_country(ctx):
    info = run(ctx.tmdb.release_info(101, "US"))
    assert (info["theatrical"], info["digital"], info["physical"]) == ("2024-03-01", "2024-04-16", "2024-05-14")
    assert info["streaming"] == ["Max"] and info["rent_buy"]
    assert run(ctx.tmdb.release_info(101, "IT"))["physical"] == "2024-06-20"


def test_events_fire_once_each():
    m = {"title": "Dune: Part Two", "year": 2024}
    info = {"title": "Dune: Part Two", "year": 2024, "digital": "2024-04-16", "physical": "2024-05-14", "streaming": []}
    seen = {}
    first = movietracker.events(m, info, seen, "2024-04-20")
    assert first == ["📅 Dune: Part Two (2024)\nBlu-ray and DVD on 14 May 2024",
                     "🛒 Dune: Part Two (2024)\nOut on Digital (buy or rent): 16 Apr 2024"]
    assert movietracker.events(m, info, seen, "2024-04-21") == []
    info["streaming"] = ["Max"]
    later = movietracker.events(m, info, seen, "2024-05-14")
    assert later == ["💿 Dune: Part Two (2024)\nOut on Blu-ray and DVD: 14 May 2024", "📺 Dune: Part Two (2024)\nNow streaming on Max"]
    assert movietracker.events(m, info, seen, "2024-05-15") == []


def test_daily_check_is_quiet_first_then_tells_you(ctx):
    ctx.notify = Texts()
    ctx.db.run("INSERT INTO movies (tmdb_id, title, year) VALUES (101, 'Dune: Part Two', 2024)")
    w = Worker(ctx)
    run(w.check_movies())
    assert ctx.notify.texts == []  # first look only notes what's already true
    row = ctx.db.one("SELECT * FROM movies WHERE tmdb_id=101")
    assert row["physical"] == "2024-05-14" and json.loads(row["streaming"]) == ["Max"]
    ctx.db.run("UPDATE movies SET checked_at=0 + 1 WHERE tmdb_id=101")
    TMDB_PROVIDERS[101]["US"]["flatrate"].append({"provider_name": "Netflix"})
    ctx.tmdb.cache.clear()
    try:
        run(w.check_movies())
    finally:
        TMDB_PROVIDERS[101]["US"]["flatrate"].pop()
    assert ctx.notify.texts == ["📺 Dune: Part Two (2024)\nNow streaming on Netflix"]


def test_roundup_has_movie_dates(ctx):
    ctx.db.run("INSERT INTO movies (tmdb_id, title, year, physical, digital) VALUES (101, 'Dune: Part Two', 2024, '2024-05-14', '2024-04-16')")
    text = roundup.text(ctx.db, None, "2024-05-10")
    assert "<b>Movie Tracker</b>" in text and "• Dune: Part Two (2024): Blu-ray on 14 May 2024" in text
    assert "digital" not in text.split("Movie Tracker")[1]  # 16 Apr is more than a week ago


def test_settings_tidy_site_and_theme(db):
    p = prefs.set_many(db, {"site_name": "My Site", "site_url": "forum.example.org/links", "site_badge": "m-l!x9", "theme": "light",
                            "release_country": "it"})
    assert p["site_url"] == "https://forum.example.org/links" and p["site_badge"] == "MLX" and p["theme"] == "light"
    assert p["release_country"] == "IT"
    assert prefs.set_many(db, {"theme": "matrix"})["theme"] == "dark"


@pytest.fixture
def client(cfg):
    app = create_app(cfg, mega=FakeMega(), tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)),
                     telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(Sent().handler)),
                     plex_client=httpx.AsyncClient(transport=httpx.MockTransport(Sent().handler)))
    with TestClient(app) as c:
        sign_up(c)
        c.app.state.ctx.vault.set("tmdb_key", "test-key")
        yield c


def test_movie_tracker_api(client):
    r = client.post("/api/movies", json={"tmdb_id": 101}, headers=H)
    assert r.status_code == 200, r.text
    m = r.json()
    assert [x["label"] for x in m["lines"]] == ["Blu-ray", "Digital", "Theaters", "Streaming"]
    assert m["out"] and m["lines"][0]["value"] == "14 May 2024"
    assert [x["tmdb_id"] for x in client.get("/api/movies").json()] == [101]
    assert client.delete("/api/movies/101", headers=H).json()["ok"]
    assert client.get("/api/movies").json() == []


def test_overview_has_site_and_only_finished(client):
    assert client.get("/api/overview").json()["site"] is None
    client.put("/api/settings", json={"site_name": "Site", "site_url": "https://example.org"}, headers=H)
    ov = client.get("/api/overview").json()
    assert ov["site"] == {"name": "Site", "url": "https://example.org", "badge": "S"}
    assert ov["setup_issues"] >= 1  # no libraries yet
    db = client.app.state.ctx.db
    for st in ("done", "canceled"):
        db.run("INSERT INTO jobs (title, kind, status, created_at, updated_at) VALUES (?, 'movie', ?, 1, 1)", (st, st))
    assert [j["title"] for j in client.get("/api/overview").json()["recent"]] == ["done"]


def test_filing_a_movie_takes_it_off_the_tracker(ctx, libs, mega):
    from test_v05 import FakePlex, _filed_movie
    ctx.db.run("INSERT INTO movies (tmdb_id, title, year) VALUES (101, 'Dune: Part Two', 2024)")
    _filed_movie(ctx, libs, mega, FakePlex([{}] * 6))
    assert ctx.db.one("SELECT 1 FROM movies WHERE tmdb_id=101") is None


