"""End to end through the worker: link → draft → download → unpack/clean → move, with fakes."""
import asyncio
import json
import time

import httpx

from conftest import DOC_URL, MOVIE_URL, SHOW_S1, SHOW_S2, Sent, run
from linkarr import jobs, prefs
from linkarr.mega import MegaError
from linkarr.telegram import Telegram
from linkarr.worker import Worker, show_events


async def drive(w: Worker, ticks: int = 8):
    w.last_inbox = w.last_shows = time.time()
    for _ in range(ticks):
        await w.tick()
        pending = list(w.downloads.values()) + list(w.processing.values())
        if pending:
            await asyncio.gather(*pending)


def job(ctx, jid):
    return jobs.job_view(ctx.db, jid)


def test_movie_lands_named_and_cleaned(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        assert j["status"] == "draft" and j["title"] == "Dune: Part Two" and j["year"] == 2024
        assert j["library"]["name"] == "Movies" and j["poster"] == "/dune.jpg"
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    dest = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)" / "Dune - Part Two (2024).mkv"
    assert dest.exists() and dest.stat().st_size == 2000
    assert not any((ctx.cfg.media_dir / ".linkarr" / "links").glob("*/*"))
    h = ctx.db.one("SELECT * FROM history WHERE job_id=?", (jid,))
    assert len(json.loads(h["moves"])) == 1


def test_unticked_and_junk_files_are_dropped(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p", [("Dune.Part.Two.2024.2160p/movie.mkv", 2000),
                                                     ("Dune.Part.Two.2024.2160p/RARBG.txt", 10),
                                                     ("Dune.Part.Two.2024.2160p/extras.mkv", 900)])

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        link = j["links"][0]
        picked = {f["path"].rsplit("/", 1)[-1]: f["selected"] for f in link["files"]}
        assert picked == {"movie.mkv": True, "RARBG.txt": False, "extras.mkv": True}
        jobs.set_files(ctx, link["id"], ["Dune.Part.Two.2024.2160p/movie.mkv"])
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))

    run(go())
    folder = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)"
    assert sorted(p.name for p in folder.iterdir()) == ["Dune - Part Two (2024).mkv"]


def test_documentary_goes_to_documentaries(ctx, libs, mega):
    mega.add(DOC_URL, "Free.Solo.2018.1080p.mkv", [("Free.Solo.2018.1080p.mkv", 500)], kind="file")
    j = run(jobs.add_link(ctx, DOC_URL))
    assert j["library"]["name"] == "Documentaries"
    assert "Documentary" in j["genres"]


def test_two_season_links_join_existing_show(ctx, libs, mega):
    show = ctx.cfg.media_dir / "TV Shows" / "Severance (2022)" / "Season 1"
    show.mkdir(parents=True)
    (show / "Severance - S01E01.mkv").write_bytes(b"old")
    mega.add(SHOW_S1, "Severance S01", [("Severance S01/Severance.S01E02.mkv", 100)])
    mega.add(SHOW_S2, "Severance S02 1080p", [("Severance S02 1080p/Severance.S02E01.1080p.mkv", 100),
                                             ("Severance S02 1080p/Severance.S02E02.1080p.mkv", 100)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        assert j["kind"] == "series" and j["library"]["name"] == "TV Shows"
        assert j["match_state"] == "exact" and j["match_folder"] == "Severance (2022)"
        j = await jobs.add_link(ctx, SHOW_S2, job_id=j["id"])
        assert [x["season"] for x in j["links"]] == [1, 2]
        await jobs.update_job(ctx, j["id"], {"wait_all": 1})
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx), 12)
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    root = ctx.cfg.media_dir / "TV Shows" / "Severance (2022)"
    got = sorted(str(p.relative_to(root)) for p in root.rglob("*.mkv"))
    assert got == ["Season 1/Severance - S01E01.mkv", "Season 1/Severance - S01E02.mkv",
                   "Season 2/Severance - S02E01.mkv", "Season 2/Severance - S02E02.mkv"]
    assert ctx.db.one("SELECT count(*) AS n FROM history WHERE job_id=?", (jid,))["n"] == 1
    assert ctx.db.one("SELECT name FROM shows WHERE tmdb_id=201")["name"] == "Severance"


def test_close_match_asks_first(ctx, libs, mega):
    (ctx.cfg.media_dir / "TV Shows" / "The Office (US) (2005)").mkdir()
    mega.add(SHOW_S1, "The Office S09", [("The Office S09/The.Office.S09E01.mkv", 100)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        assert j["match_state"] == "close" and j["match_folder"] == "The Office (US) (2005)"
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        v = job(ctx, j["id"])
        assert v["status"] == "needs_answer" and v["alerts"][0]["kind"] == "match"
        assert not any((ctx.cfg.media_dir / "TV Shows" / "The Office (US) (2005)").iterdir())
        jobs.answer_match(ctx, j["id"], "use")
        await drive(w)
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    assert (ctx.cfg.media_dir / "TV Shows" / "The Office (US) (2005)" / "Season 9" / "The Office - S09E01.mkv").exists()
    assert not jobs.open_alerts(ctx.db)


def test_movie_you_already_have_asks_then_replaces(ctx, libs, mega):
    old = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)"
    old.mkdir()
    (old / "Dune - Part Two (2024).mkv").write_bytes(b"old")
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        assert j["conflict"] == "exists"
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        assert job(ctx, j["id"])["status"] == "needs_answer"
        await jobs.update_job(ctx, j["id"], {"conflict_choice": "replace"})
        await drive(w)
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    assert (old / "Dune - Part Two (2024).mkv").stat().st_size == 2000
    recycled = list((ctx.cfg.media_dir / ".linkarr-recycle").rglob("*.mkv"))
    assert len(recycled) == 1 and recycled[0].read_bytes() == b"old"


def test_stuck_download_retries_once_then_offers_fix_it(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    mega.failures[MOVIE_URL] = [MegaError("integrity", "MAC verification failed"), MegaError("integrity", "MAC verification failed")]

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w, 2)
        link = ctx.db.one("SELECT * FROM links WHERE job_id=?", (j["id"],))
        assert link["status"] == "queued" and link["not_before"] > 0  # the silent retry
        assert not jobs.open_alerts(ctx.db)
        ctx.db.run("UPDATE links SET not_before=0")
        await drive(w, 3)
        v = job(ctx, j["id"])
        assert v["links"][0]["status"] == "error" and v["status"] == "failed"
        alerts = jobs.open_alerts(ctx.db)
        assert alerts[0]["kind"] == "integrity" and alerts[0]["link_id"] == v["links"][0]["id"]
        jobs.retry_link(ctx, v["links"][0]["id"])  # Fix it
        await drive(w)
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    assert mega.downloads.count(MOVIE_URL) == 3
    assert not jobs.open_alerts(ctx.db)


def test_quota_waits_and_tells_you(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    mega.failures[MOVIE_URL] = [MegaError("quota", "Transfer quota exceeded")]

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx), 2)

    run(go())
    link = ctx.db.one("SELECT * FROM links")
    assert link["status"] == "queued" and link["not_before"] > time.time() + 1000
    assert jobs.open_alerts(ctx.db)[0]["kind"] == "quota"


def test_inbox_item_becomes_a_draft(ctx, libs):
    inbox = ctx.cfg.media_dir / "Linkarr Inbox" / "Free.Solo.2018.1080p"
    inbox.mkdir(parents=True)
    (inbox / "free.solo.2018.1080p.mkv").write_bytes(b"x" * 300)
    (inbox / "readme.nfo").write_bytes(b"x")

    async def go():
        w = Worker(ctx)
        for _ in range(3):
            await w.scan_inbox()
        row = ctx.db.one("SELECT id FROM jobs")
        j = job(ctx, row["id"])
        assert j["source"] == "inbox" and j["status"] == "draft" and j["library"]["name"] == "Documentaries"
        assert jobs.open_alerts(ctx.db)[0]["kind"] == "inbox"
        jobs.start_job(ctx, j["id"])
        await drive(w)
        return j["id"]

    jid = run(go())
    assert job(ctx, jid)["status"] == "done"
    assert (ctx.cfg.media_dir / "Documentaries" / "Free Solo (2018)" / "Free Solo (2018).mkv").exists()
    assert not (ctx.cfg.media_dir / "Linkarr Inbox" / "Free.Solo.2018.1080p").exists()


def test_restart_requeues_interrupted_download(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    jobs.start_job(ctx, j["id"])
    ctx.db.run("UPDATE links SET status='downloading'")
    ctx.db.run("UPDATE jobs SET status='downloading'")
    w = Worker(ctx)
    w.recover()
    assert ctx.db.one("SELECT status FROM links")["status"] == "queued"
    run(drive(w))
    assert job(ctx, j["id"])["status"] == "done"


def test_series_news():
    show = {"name": "Severance"}
    notified = {}
    det = {"title": "Severance", "status": "Returning Series", "next_season": 3, "next_episode": 1, "next_air": "2026-11-01",
           "last_episode_type": "finale", "last_season": 2, "last_air": "2026-10-10"}
    out = show_events(show, det, notified, "2026-10-18")
    assert any("season 3 starts" in x for x in out) and any("finale" in x for x in out)
    assert show_events(show, det, notified, "2026-10-18") == []
    det = dict(det, status="Ended", last_air="2025-01-01")
    out = show_events(show, det, notified, "2026-10-18")
    assert len(out) == 1 and "ended" in out[0]


def test_telegram_link_and_buttons(ctx, libs, mega):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        await tg.on_message({"chat": {"id": 7}, "text": "/start"})
        assert "<code>7</code>" in sent.calls[-1][1]["text"]
        await tg.on_message({"chat": {"id": 7}, "text": MOVIE_URL})
        assert ctx.db.one("SELECT count(*) AS n FROM jobs")["n"] == 0  # strangers can't add links
        await tg.on_message({"chat": {"id": 42}, "text": "grab this " + MOVIE_URL})
        msg = sent.calls[-1][1]
        assert "Dune: Part Two" in msg["text"]
        buttons = [b["callback_data"] for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        jid = ctx.db.one("SELECT id FROM jobs")["id"]
        assert f"go:{jid}" in buttons and f"lib:{jid}:{libs['Documentaries']['id']}" in buttons
        await tg.on_button({"id": "q1", "data": f"lib:{jid}:{libs['Documentaries']['id']}", "message": {"chat": {"id": 42}, "message_id": 5}})
        assert job(ctx, jid)["library"]["name"] == "Documentaries"
        await tg.on_button({"id": "q2", "data": f"go:{jid}", "message": {"chat": {"id": 42}, "message_id": 5}})
        assert job(ctx, jid)["status"] == "queued"

    run(go())
