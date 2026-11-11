"""0.7.0 extras, each off until turned on: folder watch, missing episode check, duplicate guard, quality
upgrade, Plex folder scans, the download window and the morning summary."""
import asyncio
import datetime as dt
import json
import time
from pathlib import Path

import httpx
from conftest import KEY, MOVIE_URL, SHOW_S1, Sent, run

from linkarr import files, jobs, prefs, roundup, tracker, watch
from linkarr.mega import Preview, file_in_folder, is_folder
from linkarr.plex import Plex
from linkarr.telegram import Telegram
from linkarr.worker import Worker

EXTRAS = ("watch_enabled", "gap_check", "dup_guard", "auto_upgrade", "plex_partial", "window_enabled", "morning_summary")


class Notes:
    """Collects what would have gone to Telegram."""

    def __init__(self):
        self.texts, self.alerts, self.done = [], [], []
        self.plex = None

    async def send(self, text, buttons=None):
        self.texts.append(text)

    async def send_html(self, text, buttons=None):
        self.texts.append(text)

    async def alert(self, job_id, link_id, kind):
        self.alerts.append(kind)

    async def job_done(self, job_id, n, target, skipped=(), upgraded=()):
        self.done.append({"n": n, "skipped": list(skipped), "upgraded": list(upgraded)})


async def drive(w: Worker, ticks: int = 10):
    w.last_inbox = w.last_shows = w.last_minute = time.time()
    for _ in range(ticks):
        await w.tick()
        pending = list(w.downloads.values()) + list(w.processing.values())
        if pending:
            await asyncio.gather(*pending)


def folder(mega, url, name, entries):
    """A folder link whose files carry MEGA ids, like the real API gives."""
    fl = [{"path": p, "size": s, "h": h} for p, s, h in entries]
    mega.previews[url] = Preview(name=name, size=sum(s for _, s, _ in entries), kind="folder", files=fl)
    for p, s, h in entries:  # each file on its own, as the watch downloads them
        sub = file_in_folder(url, h)
        mega.previews[sub] = Preview(name=Path(p).name, size=s, kind="file", files=[{"path": Path(p).name, "size": s}])


def show_dir(ctx):
    return ctx.cfg.media_dir / "TV Shows" / "Severance (2022)"


def test_every_extra_is_off_by_default(db):
    assert not any(prefs.get(db, k) for k in EXTRAS)
    assert prefs.get(db, "watch_every") == 24
    assert prefs.set_many(db, {"watch_every": 5, "window_start": "7:5", "morning_time": "late"})["watch_every"] == 24
    assert prefs.get(db, "window_start") == "07:05" and prefs.get(db, "morning_time") == "08:00"


# ---- folder watch -------------------------------------------------------------------------------

def test_watch_needs_the_switch_and_a_folder(ctx, libs, mega):
    folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001")])
    j = run(jobs.add_link(ctx, SHOW_S1))
    try:
        watch.add(ctx, j["links"][0]["id"])
        raise AssertionError("watch added while switched off")
    except watch.WatchError as e:
        assert "Settings → Extras" in str(e)
    prefs.set_many(ctx.db, {"watch_enabled": True})
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.mkv", [("Dune.Part.Two.2024.mkv", 100)], kind="file")
    m = run(jobs.add_link(ctx, MOVIE_URL))
    try:
        watch.add(ctx, m["links"][0]["id"])
        raise AssertionError("file link watched")
    except watch.WatchError as e:
        assert "folder links" in str(e)
    assert is_folder(SHOW_S1) and not is_folder(MOVIE_URL) and not is_folder(file_in_folder(SHOW_S1, "h0000001"))


def test_watch_downloads_only_the_new_episode(ctx, libs, mega):
    prefs.set_many(ctx.db, {"watch_enabled": True})
    ctx.notify = Notes()
    folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001"),
                                            ("Severance S02/Severance.S02E02.mkv", 100, "h0000002")])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        w = watch.add(ctx, j["links"][0]["id"])
        assert w["episodes"] == 2 and w["status"] == "active"
        jobs.start_job(ctx, j["id"])
        worker = Worker(ctx)
        await drive(worker)
        assert jobs.job_view(ctx.db, j["id"])["status"] == "done"
        # The uploader adds episode 3 (and a junk file) to the same folder.
        folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001"),
                                                ("Severance S02/Severance.S02E02.mkv", 100, "h0000002"),
                                                ("Severance S02/Severance.S02E03.mkv", 120, "h0000003"),
                                                ("Severance S02/RARBG.txt", 5, "h0000004")])
        mega.downloads.clear()
        assert watch.due(ctx.db) == []  # checked just now, next look in 24 hours
        ctx.db.run("UPDATE watches SET last_check=0")
        await worker.check_watches()
        await drive(worker)
        return j["id"]

    run(go())
    assert mega.downloads == [file_in_folder(SHOW_S1, "h0000003")]  # only the new file, not the whole folder
    eps = sorted(p.name for p in (show_dir(ctx) / "Season 2").iterdir())
    assert eps == ["Severance - S02E01.mkv", "Severance - S02E02.mkv", "Severance - S02E03.mkv"]
    assert ctx.notify.texts[-1] == "📺 <b>Severance</b>\n1 new episode in season 2\nDownloading now."
    w = watch.all_watches(ctx.db)[0]
    assert w["episodes"] == 3 and w["last_new"] > 0 and w["folder"] == "Severance (2022)"
    newest = ctx.db.one("SELECT * FROM jobs ORDER BY id DESC")
    assert newest["source"] == "watch" and newest["status"] == "done"
    # Nothing new on the next look: no job, no message.
    n = len(ctx.notify.texts)
    ctx.db.run("UPDATE watches SET last_check=0")
    run(Worker(ctx).check_watches())
    assert len(ctx.notify.texts) == n and ctx.db.one("SELECT count(*) AS c FROM jobs")["c"] == 2


def test_watch_stops_when_the_link_dies_or_goes_quiet(ctx, libs, mega):
    prefs.set_many(ctx.db, {"watch_enabled": True, "watch_stop_days": 30})
    ctx.notify = Notes()
    folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001")])
    j = run(jobs.add_link(ctx, SHOW_S1))
    w = watch.add(ctx, j["links"][0]["id"])
    # Quiet for 31 days.
    ctx.db.run("UPDATE watches SET last_check=0, last_new=?", (int(time.time()) - 31 * 86400,))
    run(Worker(ctx).check_watches())
    row = ctx.db.one("SELECT * FROM watches WHERE id=?", (w["id"],))
    assert row["status"] == "stopped" and row["stop_reason"] == "idle"
    assert ctx.notify.texts[-1].split("\n") == ["💤 <b>Stopped monitoring Severance</b>", "Nothing new for 30 days.",
                                                "Resume it in Tracker → Monitored links."]
    watch.set_active(ctx, w["id"], True)
    # The uploader deletes the folder.
    del mega.previews[SHOW_S1]
    ctx.db.run("UPDATE watches SET last_check=0")
    run(Worker(ctx).check_watches())
    assert ctx.db.one("SELECT status FROM watches WHERE id=?", (w["id"],))["status"] == "dead"
    assert ctx.notify.texts[-1].split("\n")[1:] == ["The MEGA folder link is gone.", "Send the new link with /monitor."]
    try:
        watch.set_active(ctx, w["id"], True)
        raise AssertionError("dead link resumed")
    except watch.WatchError:
        pass


def test_watch_api_and_bot(ctx, libs, mega):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001")])

    run(tg.on_message({"chat": {"id": 42}, "text": f"/watch {SHOW_S1}"}))
    assert sent.calls[-1][1]["text"] == "Monitored links are off.\nTurn them on in Linkarr → Settings → Extras."
    prefs.set_many(ctx.db, {"watch_enabled": True})
    run(tg.on_message({"chat": {"id": 42}, "text": "/watch https://mega.nz/file/Abc12345#" + KEY}))
    assert sent.calls[-1][1]["text"].startswith("Only folder links can be monitored.")
    run(tg.on_message({"chat": {"id": 42}, "text": f"/monitor {SHOW_S1}"}))
    texts = [b["text"] for m, b in sent.calls if m == "sendMessage"]
    assert texts[-2].split("\n") == ["👁 <b>Monitoring Severance</b>", "Checks: every day", "New episodes download by themselves."]
    assert "Severance" in texts[-1] and "Status: draft" in texts[-1]  # the job card, to check and start
    run(tg.on_message({"chat": {"id": 42}, "text": "/watch"}))
    assert sent.calls[-1][1]["text"] == "👁 <b>Monitored links</b>\n\n• Severance: monitoring"


def test_watch_page_api(cfg, mega, tmdb_client):
    from fastapi.testclient import TestClient
    from linkarr.main import create_app
    folder(mega, SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100, "h0000001")])
    app = create_app(cfg, mega=mega, tmdb_client=tmdb_client)
    with TestClient(app) as c:
        from linkarr import auth
        db = app.state.ctx.db
        auth.create_user(db, "admin", "a-long-password")
        h = {"X-Linkarr": "1"}
        assert c.post("/api/auth/login", json={"username": "admin", "password": "a-long-password"}, headers=h).status_code == 200
        (cfg.media_dir / "TV").mkdir()
        c.post("/api/libraries", json={"name": "TV", "kind": "series", "path": "TV"}, headers=h)
        j = c.post("/api/jobs", json={"url": SHOW_S1, "kind": "series"}, headers=h).json()
        assert c.get("/api/overview").json()["extras"] == {"watch": False, "captcha": True}
        r = c.post("/api/watches", json={"link_id": j["links"][0]["id"]}, headers=h)
        assert r.status_code == 400 and "Extras" in r.json()["detail"]
        c.put("/api/settings", json={"watch_enabled": True}, headers=h)
        w = c.post("/api/watches", json={"link_id": j["links"][0]["id"]}, headers=h).json()
        assert "seen" not in w
        assert c.get(f"/api/jobs/{j['id']}").json()["links"][0]["watch"] == "active"
        listed = c.get("/api/watches").json()
        assert listed["enabled"] and listed["every"] == 24 and [x["id"] for x in listed["watches"]] == [w["id"]]
        assert c.post(f"/api/watches/{w['id']}/check", headers=h).json()["new"] == 0
        assert c.post(f"/api/watches/{w['id']}", json={"active": False}, headers=h).json()["status"] == "stopped"
        assert c.delete(f"/api/watches/{w['id']}", headers=h).json() == {"ok": True}
        assert c.get("/api/watches").json()["watches"] == []


# ---- duplicate guard and quality upgrade ----------------------------------------------------------

def _have(ctx, *names):
    d = show_dir(ctx) / "Season 1"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"old")
    return d


def test_duplicate_guard_skips_episodes_you_have(ctx, libs, mega):
    prefs.set_many(ctx.db, {"dup_guard": True})
    ctx.notify = Notes()
    season = _have(ctx, "Severance - S01E01.mkv")
    mega.add(SHOW_S1, "Severance S01", [("Severance S01/Severance.S01E01.mkv", 100), ("Severance S01/Severance.S01E02.mkv", 100),
                                        ("Severance S01/Severance.S01E01.en.srt", 3)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))

    run(go())
    assert sorted(p.name for p in season.iterdir()) == ["Severance - S01E01.mkv", "Severance - S01E02.mkv"]
    assert (season / "Severance - S01E01.mkv").read_bytes() == b"old"  # no "(2)" copy, the old one untouched
    assert ctx.notify.done[-1] == {"n": 1, "skipped": ["S01E01"], "upgraded": []}


def test_duplicate_guard_doesnt_download_a_link_you_already_have(ctx, libs, mega):
    prefs.set_many(ctx.db, {"dup_guard": True})
    ctx.notify = Notes()
    _have(ctx, "Severance - S01E01.mkv")
    mega.add(SHOW_S1, "Severance S01", [("Severance S01/Severance.S01E01.mkv", 100)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))
        return j["id"]

    jid = run(go())
    assert mega.downloads == []
    assert jobs.job_view(ctx.db, jid)["status"] == "canceled"
    assert ctx.notify.texts[-1].split("\n")[1:] == ["You already have every episode in this link.", "It wasn't downloaded."]


def test_quality_upgrade_swaps_in_the_better_episode(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_upgrade": True})
    ctx.notify = Notes()
    season = _have(ctx, "Severance - S01E01.720p.mkv", "Severance - S01E01.720p.en.srt")
    mega.add(SHOW_S1, "Severance S01 1080p", [("Severance S01 1080p/Severance.S01E01.1080p.mkv", 300)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))
        return j["id"]

    jid = run(go())
    assert sorted(p.name for p in season.iterdir()) == ["Severance - S01E01.mkv"]
    assert (season / "Severance - S01E01.mkv").stat().st_size == 300
    h = ctx.db.one("SELECT recycled FROM history WHERE job_id=?", (jid,))
    assert len(json.loads(h["recycled"])) == 2  # the old video and its subtitle, kept in the recycle folder
    assert ctx.notify.done[-1]["upgraded"] == ["S01E01"]


def test_movies_upgrade_or_skip_without_asking(ctx, libs, mega):
    have = ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)"
    have.mkdir(parents=True)
    (have / "Dune - Part Two (2024) 1080p.mkv").write_bytes(b"x" * 100)
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 900)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert jobs.open_question(j) == "conflict"  # off: it asks, as before
    jobs.delete_job(ctx, j["id"])
    prefs.set_many(ctx.db, {"auto_upgrade": True})
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["conflict_choice"] == "replace" and not jobs.open_question(j)
    jobs.delete_job(ctx, j["id"])
    (have / "Dune - Part Two (2024) 1080p.mkv").unlink()
    (have / "Dune - Part Two (2024) 2160p.mkv").write_bytes(b"x" * 900)
    prefs.set_many(ctx.db, {"dup_guard": True})
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["quality"]["verdict"] == "same" and j["conflict_choice"] == "skip"
    assert jobs.start_job(ctx, j["id"])["status"] == "canceled" and mega.downloads == []


# ---- missing episode check -------------------------------------------------------------------------

def test_gap_check_names_the_missing_episode_once(ctx, libs, mega):
    prefs.set_many(ctx.db, {"gap_check": True})
    ctx.notify = Notes()
    _have(ctx, "Severance - S01E01.mkv", "Severance - S01E02.mkv")
    mega.add(SHOW_S1, "Severance S01", [("Severance S01/Severance.S01E04.mkv", 100), ("Severance S01/Severance.S01E05.mkv", 100)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))

    run(go())
    assert ctx.notify.texts[-1].split("\n") == ["🧩 <b>Severance</b>", "Season 1 is missing episode 3.", "You have episodes 1–2, 4–5."]
    alert = ctx.db.one("SELECT * FROM alerts WHERE kind='gap'")
    assert alert["message"] == "Season 1 is missing episode 3. You have episodes 1–2, 4–5."
    assert tracker.gaps({1: {"eps": {1, 3}, "unknown": 1}}, [1]) == {}  # unnumbered files may be the gap


# ---- download window -----------------------------------------------------------------------------

def test_download_window(db):
    at = dt.datetime(2026, 10, 31, 14, 0).timestamp()
    assert jobs.window_wait(db, 50 * 1024**3, at) == 0  # off
    prefs.set_many(db, {"window_enabled": True, "window_start": "01:00", "window_end": "07:00", "window_min_gb": 5})
    assert jobs.window_wait(db, 1024**3, at) == 0  # small downloads don't wait
    opens = jobs.window_wait(db, 6 * 1024**3, at)
    assert dt.datetime.fromtimestamp(opens) == dt.datetime(2026, 11, 1, 1, 0)
    assert jobs.window_wait(db, 6 * 1024**3, dt.datetime(2026, 11, 1, 3, 0).timestamp()) == 0
    prefs.set_many(db, {"window_start": "23:00", "window_end": "06:00"})  # across midnight
    assert jobs.window_wait(db, 6 * 1024**3, dt.datetime(2026, 11, 1, 2, 0).timestamp()) == 0
    assert dt.datetime.fromtimestamp(jobs.window_wait(db, 6 * 1024**3, at)) == dt.datetime(2026, 10, 31, 23, 0)


def test_big_download_waits_for_the_window(ctx, libs, mega):
    prefs.set_many(ctx.db, {"window_enabled": True, "window_min_gb": 0})
    hour = time.localtime().tm_hour
    start = f"{(hour + 2) % 24:02d}:00"
    end = f"{(hour + 3) % 24:02d}:00"
    prefs.set_many(ctx.db, {"window_start": start, "window_end": end})
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.mkv", [("Dune.Part.Two.2024.mkv", 100)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    jobs.start_job(ctx, j["id"])
    run(drive(Worker(ctx), 3))
    assert mega.downloads == []
    assert jobs.job_view(ctx.db, j["id"])["links"][0]["window_at"] > time.time()
    prefs.set_many(ctx.db, {"window_enabled": False})
    run(drive(Worker(ctx), 3))
    assert mega.downloads == [MOVIE_URL]


# ---- morning summary --------------------------------------------------------------------------------

def test_morning_summary_one_title_per_line(ctx, libs, mega):
    since = int(time.time()) - 60
    assert roundup.morning_text(ctx.db, since) == ""  # a quiet night sends nothing
    t = int(time.time())
    a = ctx.db.run("INSERT INTO jobs (kind, title, year, status, created_at, updated_at) VALUES ('movie','Dune: Part Two',2024,'done',?,?)", (t, t)).lastrowid
    b = ctx.db.run("INSERT INTO jobs (kind, title, status, created_at, updated_at) VALUES ('series','Severance','done',?,?)", (t, t)).lastrowid
    moves = [{"from": "x", "to": f"/media/TV Shows/Severance (2022)/Season 2/Severance - S02E0{i}.mkv"} for i in (3, 4)]
    ctx.db.run("INSERT INTO history (job_id, ts, summary, moves) VALUES (?,?,?,?)",
               (a, t, "", json.dumps([{"from": "x", "to": "/media/Movies/Dune/Dune.mkv"}])))
    ctx.db.run("INSERT INTO history (job_id, ts, summary, moves) VALUES (?,?,?,?)", (b, t, "", json.dumps(moves)))
    ctx.db.run("INSERT INTO jobs (kind, title, status, created_at, updated_at) VALUES ('movie','Other','downloading',?,?)", (t, t))
    assert roundup.morning_text(ctx.db, since).split("\n") == [
        "☀️ <b>Overnight in Linkarr</b>", "", "🎬 Dune: Part Two (2024)", "📺 Severance S02: 2 episodes", "", "⏳ 1 still downloading"]


def test_morning_summary_goes_out_once_after_its_time(ctx):
    ctx.notify = Notes()
    w = Worker(ctx)
    t = int(time.time())
    jid = ctx.db.run("INSERT INTO jobs (kind, title, status, created_at, updated_at) VALUES ('movie','Dune','done',?,?)", (t, t)).lastrowid
    ctx.db.run("INSERT INTO history (job_id, ts, summary, moves) VALUES (?,?,?,?)", (jid, t, "", '[{"from":"a","to":"/m/Dune.mkv"}]'))
    assert run(w.morning_summary()) == ""  # off
    prefs.set_many(ctx.db, {"morning_summary": True, "morning_time": "00:00"})
    assert run(w.morning_summary()).startswith("☀️")
    assert run(w.morning_summary()) == "" and len(ctx.notify.texts) == 1


# ---- Plex: scan only the folder that changed ----------------------------------------------------------

def test_plex_scans_just_the_folder_with_plexs_own_path(tmp_path):
    seen = []

    def handler(request):
        seen.append((request.url.path, request.url.params.get("path")))
        if request.url.path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "2", "title": "TV Shows", "type": "show", "Location": [{"path": "D:\\Media\\TV Shows"}]}]}})
        return httpx.Response(200, json={})

    plex = Plex(lambda: "http://plex:32400", lambda: "tok", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    lib = tmp_path / "TV Shows"
    got = run(plex.refresh_folder("2", lib, lib / "Severance (2022)"))
    assert got == "folder" and seen[-1] == ("/library/sections/2/refresh", "D:\\Media\\TV Shows\\Severance (2022)")
    # A section Linkarr can't place: the whole library is scanned instead.
    got = run(plex.refresh_folder("9", lib, lib / "Severance (2022)"))
    assert got == "library" and seen[-1] == ("/library/sections/9/refresh", None)


def test_episode_files_reads_season_folders(tmp_path):
    d = tmp_path / "Show" / "Season 3"
    d.mkdir(parents=True)
    (d / "Show - E05.mkv").write_bytes(b"")
    (d / "Show - S03E06.mkv").write_bytes(b"")
    assert sorted(files.episode_files(tmp_path / "Show")) == [(3, 6)]  # "E05" alone isn't parsed as an episode
