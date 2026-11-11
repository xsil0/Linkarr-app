"""Missing episodes in the Series Tracker, and upgrade offers for movies you already have."""
import json

from conftest import MOVIE_URL, run
from linkarr import files, jobs, tracker
from linkarr.worker import show_events

TODAY = "2026-10-19"
DET = {"title": "Severance", "status": "Returning Series", "last_season": 2, "last_episode": 10,
       "last_air": "2025-03-21", "last_episode_type": "finale",
       "seasons": [{"season": 1, "episodes": 9, "air": "2022-02-18"}, {"season": 2, "episodes": 10, "air": "2025-01-17"},
                   {"season": 3, "episodes": 10, "air": "2027-01-01"}]}


def touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_episodes_on_disk(tmp_path):
    show = tmp_path / "Severance (2022)"
    for n in range(1, 10):
        touch(show / "Season 1" / f"Severance - S01E{n:02d}.mkv")
    touch(show / "Season 2" / "Severance.S02E01E02.1080p.mkv")
    touch(show / "Season 2" / "Severance.S02E03-05.mkv")
    touch(show / "Season 2" / "extra.mkv")
    touch(show / "Season 2" / "Severance - S02E06.srt")
    have = files.episodes_in(show)
    assert have[1]["eps"] == set(range(1, 10))
    assert have[2]["eps"] == {1, 2, 3, 4, 5} and have[2]["unknown"] == 1


def test_missing_only_counts_aired_episodes():
    have = {1: {"eps": set(range(1, 10)), "unknown": 0}, 2: {"eps": {1, 2, 3, 4, 5, 9}, "unknown": 0}}
    miss = tracker.missing(DET, have, TODAY)
    assert miss == {"seasons": [], "episodes": {2: [6, 7, 8, 10]}, "counts": {}}  # season 3 hasn't aired
    assert tracker.describe(miss) == "Missing S2 episodes 6–8, 10"
    assert tracker.describe(tracker.missing(DET, {1: have[1]}, TODAY)) == "Missing season 2"
    assert tracker.missing(DET, {1: have[1], 2: {"eps": set(range(1, 11)), "unknown": 0}}, TODAY) == \
        {"seasons": [], "episodes": {}, "counts": {}}


def test_unnumbered_files_count_toward_a_season():
    have = {1: {"eps": set(range(1, 10)), "unknown": 0}, 2: {"eps": set(range(1, 8)), "unknown": 3}}
    assert tracker.describe(tracker.missing(DET, have, TODAY)) == ""
    have[2]["unknown"] = 1
    assert tracker.describe(tracker.missing(DET, have, TODAY)) == "Missing 2 episodes of S2"


def test_finale_message_says_what_you_have():
    det = dict(DET, last_air="2026-10-15")
    msgs = show_events({"name": "Severance"}, det, {}, TODAY, {1: {"eps": set(range(1, 10)), "unknown": 0}})
    assert msgs == ["✅ Severance season 2 finale aired on 2026-10-15. The full season is out. You have season 1."]


def test_stored_gaps_read_back(ctx, libs):
    ctx.db.run("INSERT INTO shows (tmdb_id, name, missing) VALUES (201, 'Severance', ?)",
               ('{"seasons": [3], "episodes": {"2": [4, 5]}, "counts": {}}',))
    row = ctx.db.one("SELECT missing FROM shows WHERE tmdb_id=201")
    assert tracker.describe(json.loads(row["missing"])) == "Missing season 3; S2 episodes 4–5"


def _owned(ctx, name):
    old = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)"
    old.mkdir(parents=True, exist_ok=True)
    (old / name).write_bytes(b"old" * 10)


def test_4k_link_is_an_upgrade(ctx, libs, mega):
    _owned(ctx, "Dune - Part Two (2024) 1080p.mkv")
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["quality"]["verdict"] == "better"
    assert j["quality_text"] == "You have it in 1080p. This link is 2160p, an upgrade."


def test_worse_link_says_so(ctx, libs, mega):
    _owned(ctx, "Dune.Part.Two.2024.2160p.mkv")
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.720p.mkv", [("Dune.Part.Two.2024.720p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["quality"]["verdict"] == "worse" and "only 720p" in j["quality_text"]


def test_no_quality_without_a_copy(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["quality"] is None and j["quality_text"] == ""


def test_telegram_missing_command(ctx, libs):
    import httpx

    from conftest import Sent
    from linkarr.telegram import Telegram
    ctx.db.run("INSERT INTO shows (tmdb_id, name, missing) VALUES (201, 'Severance', ?)",
               (json.dumps({"seasons": [], "episodes": {"2": [4]}, "counts": {}}),))
    ctx.db.run("INSERT INTO shows (tmdb_id, name, missing) VALUES (202, 'Dark', ?)",
               (json.dumps({"seasons": [], "episodes": {}, "counts": {}}),))
    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(Sent().handler)))
    assert tg.missing_text() == "Missing episodes:\n• <b>Severance</b>: S2 episode 4"
