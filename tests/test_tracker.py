"""Series Tracker: adding the shows you already have, without a flood of messages."""
import json

from conftest import TMDB_TV, run
from linkarr import jobs
from linkarr.worker import Worker

TMDB_TV[202] = {"id": 202, "name": "Dark", "first_air_date": "2017-12-01", "poster_path": "", "genre_ids": [18],
                "genres": [{"id": 18, "name": "Drama"}], "status": "Ended", "next_episode_to_air": None,
                "last_episode_to_air": {"air_date": "2020-06-27", "season_number": 3, "episode_type": "finale"},
                "seasons": [{"season_number": 1, "episode_count": 10, "air_date": "2017-12-01"}]}


class Notes:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


def test_track_all_adds_matches_and_lists_the_rest(ctx, libs):
    tv = ctx.cfg.media_dir / "TV Shows"
    for f in ("Severance (2022)", "Dark [tvdbid-334824]", "Some Show Nobody Made"):
        (tv / f).mkdir()
    res = run(jobs.track_existing(ctx))
    assert res["total"] == 3 and res["added"] == 2
    assert [u["folder"] for u in res["unmatched"]] == ["Some Show Nobody Made"]
    rows = {r["tmdb_id"]: dict(r) for r in ctx.db.query("SELECT * FROM shows")}
    assert set(rows) == {201, 202} and rows[202]["folder"] == "Dark [tvdbid-334824]"
    # Running it again only looks at folders it hasn't added.
    assert run(jobs.track_existing(ctx))["total"] == 1


def test_stopped_show_stays_stopped(ctx, libs):
    ctx.db.run("INSERT INTO shows (tmdb_id, name, watching) VALUES (202, 'Dark', 0)")
    (ctx.cfg.media_dir / "TV Shows" / "Dark (2017)").mkdir()
    run(jobs.track_existing(ctx))
    assert ctx.db.one("SELECT watching FROM shows WHERE tmdb_id=202")["watching"] == 0


def test_first_check_after_import_is_quiet(ctx, libs):
    (ctx.cfg.media_dir / "TV Shows" / "Dark (2017)").mkdir()
    run(jobs.track_existing(ctx))
    ctx.notify = Notes()
    w = Worker(ctx)
    run(w.check_shows())
    assert ctx.notify.sent == []
    row = ctx.db.one("SELECT * FROM shows WHERE tmdb_id=202")
    assert row["status"] == "Ended" and json.loads(row["notified"]).get("status") == "Ended"


def test_shows_need_tmdb(ctx, libs):
    ctx.vault.set("tmdb_key", "")
    try:
        run(jobs.track_existing(ctx))
        raise AssertionError("expected an error")
    except jobs.JobError as e:
        assert "TMDB" in str(e)
