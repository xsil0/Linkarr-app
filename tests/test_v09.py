"""0.9.0: links with no show name ('Season 200 - The Science of Connection'), seasons past 99, Match to show
(TMDB, a folder you have, or a name you type), remembering it for links named the same way, and Fix show
for downloads already filed in the wrong place."""
import json
import time

import httpx
from fastapi.testclient import TestClient

from conftest import SHOW_S1, SHOW_S2, FakeMega, run, tmdb_handler
from linkarr import auth, jobs
from linkarr.db import now
from linkarr.parse import parse_name
from linkarr.worker import Worker

KEY = "B" * 22
CLASS_200 = "Season 200 - The Science of Connection with Dr. Amir Levine"
CLASS_201 = "Season 201 - The Conflict Playbook with Amanda Ripley"
MC = "MasterClass {tvdb-362158}"
H = {"X-Linkarr": "1"}


async def drive(w: Worker, ticks: int = 8):
    import asyncio
    w.last_inbox = w.last_shows = time.time()
    for _ in range(ticks):
        await w.tick()
        pending = list(w.downloads.values()) + list(w.processing.values())
        if pending:
            await asyncio.gather(*pending)


def add_class(mega, url, name, n=2):
    mega.add(url, name, [(f"{name}/{i:02d} - Lesson {i}.mp4", 100) for i in range(1, n + 1)])


# ---- reading the names ----------------------------------------------------------------------------

def test_season_only_names_have_no_show():
    p = parse_name(CLASS_200)
    assert p["title"] == "" and p["season"] == 200 and p["seasons"] == [200] and p["kind"] == "series"
    assert p["label"] == "The Science of Connection with Dr. Amir Levine"
    p = parse_name("Season 206 - Redefining Grief: Building Resilience Through Loss - Featuring Prince Harry")
    assert p["title"] == "" and p["season"] == 206


def test_long_season_numbers():
    assert parse_name("Show.S2019E05.1080p.mkv")[("season")] == 2019
    assert parse_name("Show.S2019E05.1080p.mkv")["episode"] == 5
    assert parse_name("MasterClass S200E03 Intro.mp4")["season"] == 200
    assert parse_name("The Bear Season 2")["title"] == "The Bear"
    assert parse_name("Severance S02 1080p")["season"] == 2
    assert parse_name("Show Season 1-3")["seasons"] == [1, 2, 3]
    assert parse_name("Show 1920x1080")["season"] is None


# ---- Match to show ----------------------------------------------------------------------------------

def test_no_show_name_waits_then_match_and_remember(ctx, libs, mega):
    add_class(mega, SHOW_S1, CLASS_200)
    add_class(mega, SHOW_S2, CLASS_201)

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        assert j["show_unknown"] == 1 and j["kind"] == "series" and j["tmdb_id"] is None
        assert j["links"][0]["season"] == 200
        assert "which show" in j["note"]
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        v = jobs.job_view(ctx.db, j["id"])
        assert v["status"] == "needs_answer" and v["alerts"][0]["kind"] == "show"
        assert not any((ctx.cfg.media_dir / "TV Shows").iterdir())   # nothing filed while it waits

        assert jobs.alike(ctx, job_id=j["id"])["key"] == ""
        r = await jobs.match_show(ctx, j["id"], {"folder": MC}, remember=True)
        assert r["job"]["title"] == "MasterClass" and r["job"]["folder_name"] == MC and r["job"]["show_unknown"] == 0
        await drive(w)
        assert jobs.job_view(ctx.db, j["id"])["status"] == "done"

        # The next class goes to the same show by itself.
        j2 = await jobs.add_link(ctx, SHOW_S2)
        assert j2["show_unknown"] == 0 and j2["folder_name"] == MC and j2["match_state"] == "confirmed"
        jobs.start_job(ctx, j2["id"])
        await drive(w)
        assert jobs.job_view(ctx.db, j2["id"])["status"] == "done"

    run(go())
    show = ctx.cfg.media_dir / "TV Shows" / MC
    assert sorted(p.name for p in show.iterdir()) == ["Season 200", "Season 201"]
    assert (show / "Season 200" / "01 - Lesson 1.mp4").exists()
    assert not jobs.open_alerts(ctx.db)
    assert [s["label"] for s in jobs.remembered_shows(ctx.db)] == ["Links named “Season N - …”"]
    jobs.forget_show(ctx.db, "")
    assert jobs.remembered_shows(ctx.db) == []


def test_match_to_a_tmdb_show(ctx, libs, mega):
    add_class(mega, SHOW_S1, "Season 3 - Something")

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        r = await jobs.match_show(ctx, j["id"], {"tmdb_id": 201})
        return r["job"]

    j = run(go())
    assert j["title"] == "Severance" and j["year"] == 2022 and j["tmdb_id"] == 201
    assert j["folder_name"] == "Severance (2022)" and j["library"]["name"] == "TV Shows"


def test_match_needs_a_tv_library(ctx, libs, mega):
    add_class(mega, SHOW_S1, CLASS_200)

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        try:
            await jobs.match_show(ctx, j["id"], {"folder": MC, "library_id": libs["Movies"]["id"]})
        except jobs.JobError as e:
            return str(e)

    assert run(go()) == "Pick a TV library."


# ---- Fix show: what was filed before 0.9.0 -----------------------------------------------------------

def filed_the_old_way(ctx, libs, name, staging_id, files=("01 - Lesson 1.mp4", "01 - Lesson 1.en.srt")):
    """What 0.8 did with 'Season 200 - …': the link name as the show, and 'Season 20' inside it."""
    t = now()
    tv = ctx.cfg.media_dir / "TV Shows"
    staging = ctx.cfg.media_dir / ".linkarr" / "links" / staging_id
    jid = ctx.db.run("INSERT INTO jobs (kind, title, library_id, status, folder_name, created_at, updated_at) VALUES "
                     "('series', ?, ?, 'done', ?, ?, ?)", (name, libs["TV Shows"]["id"], name, t, t)).lastrowid
    season = parse_name(name)["season"]
    ctx.db.run("INSERT INTO links (job_id, url, name, season, status, staging, files, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (jid, f"https://mega.nz/folder/{staging_id}#{KEY}", name, int(str(season)[:2]), "moved", str(staging), "[]", t))
    moves, dirs = [], [str(tv / name), str(tv / name / "Season 20")]
    for f in files:
        dst = tv / name / "Season 20" / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"v")
        if not f.endswith(".srt"):
            moves.append({"from": str(staging / name / f), "to": str(dst)})
    hid = ctx.db.run("INSERT INTO history (job_id, ts, summary, moves, recycled, created_dirs) VALUES (?,?,?,?,?,?)",
                     (jid, t, name, json.dumps(moves), "[]", json.dumps(dirs))).lastrowid
    return jid, hid


def test_fix_show_moves_seasons_and_cleans_up(ctx, libs):
    tv = ctx.cfg.media_dir / "TV Shows"
    (tv / "Severance (2022)").mkdir()  # an unrelated show stays put
    jid, hid = filed_the_old_way(ctx, libs, CLASS_200, "aaa")
    jid2, hid2 = filed_the_old_way(ctx, libs, CLASS_201, "bbb")
    assert jobs.alike(ctx, hid=hid) == {"key": "", "label": "Links named “Season N - …”", "filed": 1, "waiting": 0}

    r = run(jobs.fix_show(ctx, hid, {"folder": MC}, remember=True))
    assert r["moved"] == 2 and r["filed"] == 1 and not r["problems"]
    show = tv / MC
    assert (show / "Season 200" / "01 - Lesson 1.mp4").exists()
    assert (show / "Season 200" / "01 - Lesson 1.en.srt").exists()   # the subtitle came along
    assert (show / "Season 201" / "01 - Lesson 1.mp4").exists()
    assert sorted(p.name for p in tv.iterdir()) == [MC, "Severance (2022)"]   # no empty folders left
    job = jobs.job_view(ctx.db, jid)
    assert job["title"] == "MasterClass" and job["folder_name"] == MC and job["links"][0]["season"] == 200
    h = ctx.db.one("SELECT * FROM history WHERE id=?", (hid,))
    assert json.loads(h["moves"])[0]["to"] == str(show / "Season 200" / "01 - Lesson 1.mp4")
    assert "fixed from" in h["summary"]


def test_fix_show_keeps_a_folder_with_your_own_files(ctx, libs):
    tv = ctx.cfg.media_dir / "TV Shows"
    _jid, hid = filed_the_old_way(ctx, libs, CLASS_200, "aaa")
    (tv / CLASS_200 / "notes.txt").write_text("mine")
    r = run(jobs.fix_show(ctx, hid, {"folder": MC}))
    assert r["moved"] == 2
    assert (tv / CLASS_200 / "notes.txt").exists()
    assert not (tv / CLASS_200 / "Season 20").exists()


def test_fix_show_renames_episodes_when_renaming_is_on(ctx, libs):
    from linkarr import prefs
    prefs.set_many(ctx.db, {"rename_episodes": True})
    _jid, hid = filed_the_old_way(ctx, libs, CLASS_200, "aaa", files=("S200E01 Lesson.mp4",))
    run(jobs.fix_show(ctx, hid, {"folder": MC}))
    assert (ctx.cfg.media_dir / "TV Shows" / MC / "Season 200" / "MasterClass - S200E01.mp4").exists()


def test_remember_also_fixes_waiting_jobs(ctx, libs, mega):
    add_class(mega, SHOW_S1, CLASS_201)
    _jid, hid = filed_the_old_way(ctx, libs, CLASS_200, "aaa")

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        assert jobs.alike(ctx, hid=hid)["waiting"] == 1
        r = await jobs.fix_show(ctx, hid, {"folder": MC}, remember=True)
        assert r["waiting"] == 1
        return jobs.job_view(ctx.db, j["id"])

    j = run(go())
    assert j["show_unknown"] == 0 and j["folder_name"] == MC


def test_api_match_and_fix(cfg):
    mega = FakeMega()
    add_class(mega, SHOW_S1, CLASS_200)
    from linkarr.main import create_app
    app = create_app(cfg, mega=mega, tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)))
    with TestClient(app) as c:
        code = auth.setup_code(c.app.state.ctx.db)
        c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"}, headers=H)
        ctx = c.app.state.ctx
        tv = ctx.cfg.media_dir / "TV Shows"
        tv.mkdir()
        lib = jobs.save_library(ctx, {"name": "TV Shows", "kind": "series", "path": "TV Shows"})
        job = c.post("/api/jobs", json={"url": SHOW_S1}, headers=H).json()
        assert job["show_unknown"] == 1
        assert c.get(f"/api/jobs/{job['id']}/alike").json()["key"] == ""
        r = c.post(f"/api/jobs/{job['id']}/show", json={"folder": MC, "library_id": lib["id"], "remember": True}, headers=H).json()
        assert r["job"]["folder_name"] == MC
        assert c.get("/api/remembered-shows").json()[0]["title"] == "MasterClass"
        assert c.delete("/api/remembered-shows?key=", headers=H).json() == {"ok": True}
        assert c.get("/api/remembered-shows").json() == []
        assert c.post("/api/history/999/show", json={"folder": MC}, headers=H).status_code == 400
