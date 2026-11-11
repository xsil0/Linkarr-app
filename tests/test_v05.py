"""0.5.0: MEGA folders with several keys per file, links with no video, re-checking a link MEGA only
partly showed, matching through Plex, Plex pick-up checks, the transfer quota, Move to…, the roundup."""
import asyncio
import json
import os
import time

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from conftest import MOVIE_URL, SHOW_S1, run
from linkarr import files, jobs, naming, prefs, roundup
from linkarr.mega import Preview, PublicApi, _fold, b64e, classify, encrypt_attr, folder_preview
from linkarr.plex import Plex
from linkarr.worker import Worker


async def drive(w: Worker, ticks: int = 8):
    w.last_inbox = w.last_shows = time.time()
    for _ in range(ticks):
        await w.tick()
        pending = list(w.downloads.values()) + list(w.processing.values())
        if pending:
            await asyncio.gather(*pending)


def _ecb(k, d):
    e = Cipher(algorithms.AES(k), modes.ECB()).encryptor()
    return e.update(d) + e.finalize()


# ---- MEGA ---------------------------------------------------------------------------------------

def test_folder_preview_tries_every_key_and_counts_unreadable():
    folder_key, other_key = os.urandom(16), os.urandom(16)
    nodes = []

    def node(h, parent, name, t, size=0, keys=None):
        key = os.urandom(16) if t == 1 else os.urandom(32)
        k = keys(key) if keys else "owner:" + b64e(_ecb(folder_key, key))
        nodes.append({"h": h, "p": parent, "t": t, "s": size, "a": encrypt_attr({"n": name}, _fold(key)), "k": k})

    node("root0001", "", "Movie Night", 1)
    # The pair for another share comes first; the second one is this link's.
    node("file0001", "root0001", "Dune.Part.Two.2024.mkv", 0, 4000,
         keys=lambda key: "other:" + b64e(_ecb(other_key, key)) + "/owner:" + b64e(_ecb(folder_key, key)))
    # Only a key for a share this link doesn't open.
    node("file0002", "root0001", "Hidden.mkv", 0, 3000, keys=lambda key: "other:" + b64e(_ecb(other_key, key)))
    pv = folder_preview(nodes, folder_key)
    assert [f["path"] for f in pv.files] == ["Movie Night/Dune.Part.Two.2024.mkv"]
    assert pv.unreadable == 1 and pv.unreadable_size == 3000


def test_quota_messages_count_as_quota():
    assert classify("Transfer not started: proceeding will exceed transfer quota. Use --ignore-quota-warn") == "quota"


def test_transfer_quota_reads_used_and_max():
    def handler(request):
        assert request.url.params["sid"] == "secret-session"
        return httpx.Response(200, json=[{"mxfer": 1000, "caxfer": 300, "csxfer": 50}])

    api = PublicApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert run(api.transfer_quota("secret-session")) == {"used": 350, "max": 1000}


# ---- links with no video, and links MEGA only partly showed -----------------------------------

def test_link_without_video_warns_and_waits(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    mega.add(MOVIE_URL, "Dune.Part.Two.2024", [("Dune.Part.Two.2024/readme.txt", 10), ("Dune.Part.Two.2024/cover.jpg", 20)])
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["status"] == "draft" and "no video" in j["note"]


def test_partly_hidden_link_is_checked_after_download(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    # MEGA showed only the folder; the movie inside couldn't be read before downloading.
    mega.previews[SHOW_S1] = Preview(name="Folder", size=0, kind="folder", files=[], unreadable=1, unreadable_size=2000)

    async def fake_download(url, dest, progress=None, cancel=None):
        p = dest / "Folder" / "Dune.Part.Two.2024.1080p.mkv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 2000)

    mega.download = fake_download

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        assert j["status"] == "draft" and "didn't let Linkarr read 1 file" in j["note"]
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        v = jobs.job_view(ctx.db, j["id"])
        assert v["status"] == "needs_answer" and v["recheck"] and v["title"] == "Dune: Part Two" and v["kind"] == "movie"
        assert v["library"]["name"] == "Movies" and v["alerts"][0]["kind"] == "recheck"
        assert v["links"][0]["files"][0]["path"].endswith(".mkv")
        await jobs.update_job(ctx, j["id"], {"recheck": 0})
        await drive(w)
        return j["id"]

    jid = run(go())
    v = jobs.job_view(ctx.db, jid)
    assert v["status"] == "done" and not v["note"]
    assert (ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)" / "Dune - Part Two (2024).mkv").exists()
    assert v["moved"]["files"] == 1 and v["moved"]["where"].endswith("Movies/Dune - Part Two (2024)")


# ---- matching -----------------------------------------------------------------------------------

def test_one_word_folder_doesnt_match_a_longer_title():
    assert naming.score("Resident Evil", "Evil") < naming.CLOSE
    assert naming.score("Dark", "Dark Matter") < naming.CLOSE
    assert naming.score("The Office", "The Office (US) (2005)") >= naming.CLOSE
    assert naming.norm_title("Resident Evil (2022) {tmdb-1}") == "Resident Evil"


def test_plex_ids_beat_folder_names(ctx, libs, mega):
    tv = ctx.cfg.media_dir / "TV Shows"
    (tv / "Sev").mkdir()
    (tv / "Severance (2019)").mkdir()  # a same-name folder Plex says is something else
    ctx.db.set_setting("plex_index", {str(libs["TV Shows"]["id"]): {
        "Sev": {"tmdb": 201, "title": "Severance", "year": 2022, "count": 9},
        "Severance (2019)": {"tmdb": 999, "title": "Severance", "year": 2019, "count": 3}}})
    mega.add(SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100)])
    j = run(jobs.add_link(ctx, SHOW_S1))
    assert j["match_state"] == "exact" and j["match_folder"] == "Sev"

    ctx.db.set_setting("plex_index", {str(libs["TV Shows"]["id"]): {"Sev": {"tmdb": 555, "title": "Other", "year": 2001, "count": 1}}})
    (tv / "Severance (2019)").rmdir()
    (tv / "Severance").mkdir()
    ctx.db.set_setting("plex_index", {str(libs["TV Shows"]["id"]): {"Severance": {"tmdb": 555, "title": "Other", "year": 2001, "count": 1}}})
    jobs.refresh_target(ctx, j["id"])
    assert jobs.job_view(ctx.db, j["id"])["match_state"] == "none"


def test_same_show_question_gets_years_and_posters(ctx, libs, mega):
    (ctx.cfg.media_dir / "TV Shows" / "Severence (2022)").mkdir()
    mega.add(SHOW_S1, "Severance S02", [("Severance S02/Severance.S02E01.mkv", 100)])
    j = run(jobs.add_link(ctx, SHOW_S1))
    assert j["match_state"] == "close"
    info = run(jobs.match_info(ctx, j["id"]))
    assert info["folder"] == "Severence (2022)" and info["year"] == 2022


def test_plex_items_reads_folders_and_ids():
    body = {"MediaContainer": {"Metadata": [
        {"title": "Resident Evil", "year": 2022, "leafCount": 8, "Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://8899"}],
         "Location": [{"path": "/tv/Resident Evil (2022)"}]},
        {"title": "Dune", "year": 2021, "Guid": [{"id": "tmdb://104"}],
         "Media": [{"Part": [{"file": "/movies/Dune (2021)/Dune (2021).mkv"}]}]},
        {"title": "Home Video", "Location": [{"path": "/tv/Home Video"}]}]}}
    plex = Plex(lambda: "http://plex:32400", lambda: "tok",
                httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    items = run(plex.items("2"))
    assert items["Resident Evil (2022)"] == {"tmdb": 8899, "title": "Resident Evil", "year": 2022, "count": 8}
    assert items["Dune (2021)"]["tmdb"] == 104 and items["Home Video"]["tmdb"] is None


# ---- Plex pick-up check -------------------------------------------------------------------------

class FakePlex:
    def __init__(self, answers):
        self.answers = list(answers)  # one dict per look
        self.refreshed = []

    def configured(self):
        return True

    async def items(self, section):
        return self.answers.pop(0) if self.answers else {}

    async def refresh(self, section):
        self.refreshed.append(section)
        return True


class FakeNotify:
    def __init__(self, plex):
        self.plex = plex
        self.alerts, self.texts = [], []

    async def job_done(self, *a):
        pass

    async def alert(self, job_id, link_id, kind):
        self.alerts.append(kind)

    async def send_html(self, text, buttons=None):
        self.texts.append(text)


def _filed_movie(ctx, libs, mega, plex):
    jobs.save_library(ctx, {"name": "Movies", "kind": "movie", "path": "Movies", "plex_section": "1"}, libs["Movies"]["id"])
    ctx.notify = FakeNotify(plex)
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        w.plex_waits = (0, 0)
        await drive(w)
        await asyncio.gather(*w.side_tasks)
        return j["id"]

    return run(go())


def test_plex_check_finds_the_movie(ctx, libs, mega):
    plex = FakePlex([{}, {}, {}, {"Dune - Part Two (2024)": {"tmdb": 101, "title": "Dune", "year": 2024, "count": 1}}])
    jid = _filed_movie(ctx, libs, mega, plex)
    assert jobs.job_view(ctx.db, jid)["plex"] == "found" and not ctx.notify.alerts


def test_plex_check_tells_you_when_plex_never_shows_it(ctx, libs, mega):
    jid = _filed_movie(ctx, libs, mega, FakePlex([]))
    v = jobs.job_view(ctx.db, jid)
    assert v["plex"] == "missing" and ctx.notify.alerts == ["plex"]
    assert jobs.open_alerts(ctx.db)[0]["kind"] == "plex"


# ---- MEGA transfer quota ------------------------------------------------------------------------

def test_download_waits_when_quota_is_short(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    ctx.status["mega_quota"] = {"used": 9000, "max": 10000}

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        w.last_quota = time.time()
        await drive(w, 2)

    run(go())
    assert not mega.downloads
    link = ctx.db.one("SELECT * FROM links")
    assert link["status"] == "queued" and link["not_before"] > time.time() + 3000
    a = jobs.open_alerts(ctx.db)[0]
    assert a["kind"] == "quota" and "transfer left" in a["message"]

    prefs.set_many(ctx.db, {"quota_guard": False})
    ctx.db.run("UPDATE links SET not_before=0")
    w = Worker(ctx)
    w.last_quota = time.time()
    run(drive(w))
    assert mega.downloads == [MOVIE_URL]


# ---- Move to… -----------------------------------------------------------------------------------

def test_move_to_refiles_a_show_and_undo_still_works(ctx, libs, mega):
    tv = ctx.cfg.media_dir / "TV Shows"
    (tv / "Evil").mkdir()
    mega.add(SHOW_S1, "Severance S01", [("Severance S01/Severance.S01E01.mkv", 100), ("Severance S01/Severance.S01E02.mkv", 100)])

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        jobs.answer_match(ctx, j["id"], "pick", "Evil")  # filed in the wrong show by mistake
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))
        return j["id"]

    jid = run(go())
    wrong = tv / "Evil" / "Season 1"
    assert (wrong / "Severance - S01E01.mkv").exists()
    (wrong / "Severance - S01E01.en.srt").write_text("subs")  # saved later by the subtitle step
    hid = ctx.db.one("SELECT id FROM history WHERE job_id=?", (jid,))["id"]

    res = jobs.refile(ctx, hid, libs["TV Shows"]["id"], "Severance (2022)")
    right = tv / "Severance (2022)" / "Season 1"
    assert res["moved"] == 3 and not res["problems"]
    assert (right / "Severance - S01E01.mkv").exists() and (right / "Severance - S01E01.en.srt").exists()
    assert not (tv / "Evil").exists()  # emptied, so removed
    assert ctx.db.one("SELECT folder FROM shows WHERE tmdb_id=201")["folder"] == "Severance (2022)"
    assert jobs.job_view(ctx.db, jid)["moved"]["where"].endswith("Severance (2022)/Season 1")

    row = ctx.db.one("SELECT * FROM history WHERE id=?", (hid,))
    record = {"moves": json.loads(row["moves"]), "recycled": [], "created_dirs": json.loads(row["created_dirs"])}
    assert files.undo(record, ctx.cfg.media_dir / ".linkarr" / "undone" / "1") == []
    assert not (tv / "Severance (2022)").exists()


def test_move_to_refuses_the_wrong_kind_of_library(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))

    run(go())
    hid = ctx.db.one("SELECT id FROM history")["id"]
    try:
        jobs.refile(ctx, hid, libs["TV Shows"]["id"], "Dune")
        raise AssertionError("should refuse")
    except jobs.JobError as e:
        assert "movie library" in str(e)
    res = jobs.refile(ctx, hid, libs["Documentaries"]["id"], "Dune - Part Two (2024)")
    assert (ctx.cfg.media_dir / "Documentaries" / "Dune - Part Two (2024)" / "Dune - Part Two (2024).mkv").exists()
    assert res["moved"] == 1


# ---- weekly roundup -----------------------------------------------------------------------------

def test_roundup_lists_the_week(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        jobs.start_job(ctx, j["id"])
        await drive(Worker(ctx))

    run(go())
    today = time.strftime("%Y-%m-%d")
    soon = time.strftime("%Y-%m-%d", time.localtime(time.time() + 3 * 86400))
    ctx.db.run("INSERT INTO shows (tmdb_id, name, next_air) VALUES (201, 'Severance', ?)", (soon,))
    text = roundup.text(ctx.db, {"used": 0, "max": 1024**4}, today)
    assert "Dune: Part Two (2024)" in text and "Severance: new episode" in text and "1.00 TB of 1.00 TB" in text


def test_roundup_goes_out_once_on_its_day(ctx, libs):
    plex = FakePlex([])
    ctx.notify = FakeNotify(plex)
    prefs.set_many(ctx.db, {"roundup_day": time.strftime("%a").lower()[:3]})
    w = Worker(ctx)
    sent = run(w.weekly_roundup())
    if time.localtime().tm_hour >= 10:
        assert sent and len(ctx.notify.texts) == 1
        assert run(w.weekly_roundup()) == "" and len(ctx.notify.texts) == 1
    assert run(w.weekly_roundup(force=True))
    prefs.set_many(ctx.db, {"roundup_enabled": False})
    ctx.db.set_setting("last_roundup", "")
    assert run(w.weekly_roundup()) == ""


def test_new_columns_added_to_an_old_database(tmp_path):
    from linkarr.db import Database
    p = tmp_path / "old.db"
    d = Database(p)
    for table, col in (("jobs", "recheck"), ("jobs", "plex"), ("links", "hidden")):
        d.run(f"ALTER TABLE {table} DROP COLUMN {col}")  # what a 0.4 database looks like
    d.close()
    d = Database(p)
    assert {"recheck", "plex"} <= {r["name"] for r in d.query("PRAGMA table_info(jobs)")}
    assert "hidden" in {r["name"] for r in d.query("PRAGMA table_info(links)")}
    d.close()


# ---- Plex: Windows paths, TMDB fallback, check again / mark it ---------------------------------

def test_plex_items_reads_windows_movie_paths():
    body = {"MediaContainer": {"Metadata": [
        {"title": "Hellraiser: Judgment", "year": 2018, "Guid": [{"id": "tmdb://424139"}],
         "Media": [{"Part": [{"file": "\\\\NAS\\Library\\Movies\\Hellraiser - Judgment (2018)\\Hellraiser.Judgment.2018.mkv"}]}]}]}}
    plex = Plex(lambda: "http://plex:32400", lambda: "tok",
                httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    items = run(plex.items("1"))
    assert items["Hellraiser - Judgment (2018)"]["tmdb"] == 424139


def test_plex_check_finds_the_movie_by_tmdb_id_in_another_folder(ctx, libs, mega):
    plex = FakePlex([{}, {}, {}, {"Dune Part Two": {"tmdb": 101, "title": "Dune: Part Two", "year": 2024, "count": 1}}])
    jid = _filed_movie(ctx, libs, mega, plex)
    assert jobs.job_view(ctx.db, jid)["plex"] == "found"


def test_check_plex_again_and_mark_it(ctx, libs, mega):
    jid = _filed_movie(ctx, libs, mega, FakePlex([]))
    assert jobs.job_view(ctx.db, jid)["plex"] == "missing"
    w = Worker(ctx)
    # Not there yet: Plex is asked to scan and Linkarr keeps looking.
    ctx.notify.plex.answers = [{}, {}, {}, {}]
    w.plex_waits = (0,)

    async def again():
        res = await w.recheck_plex(jid)
        await asyncio.gather(*w.side_tasks)
        return res

    async def no_sleep(_):
        pass

    real_sleep, asyncio.sleep = asyncio.sleep, no_sleep
    try:
        assert run(again()) == "checking"
    finally:
        asyncio.sleep = real_sleep
    assert ctx.notify.plex.refreshed[-1] == "1"
    # Plex has it now: found straight away, alert cleared.
    ctx.notify.plex.answers = [{"Dune - Part Two (2024)": {"tmdb": 101, "title": "Dune", "year": 2024, "count": 1}}]
    assert run(w.recheck_plex(jid)) == "found"
    assert jobs.job_view(ctx.db, jid)["plex"] == "found" and not jobs.open_alerts(ctx.db)


def test_mark_in_plex_and_the_sweep_flips_missing(ctx, libs, mega):
    jid = _filed_movie(ctx, libs, mega, FakePlex([]))
    w = Worker(ctx)
    ctx.notify.plex.answers = [{"Dune - Part Two (2024)": {"tmdb": 101, "title": "Dune", "year": 2024, "count": 1}}]
    run(w.refresh_plex_index())
    assert jobs.job_view(ctx.db, jid)["plex"] == "found" and not jobs.open_alerts(ctx.db)
    ctx.db.run("UPDATE jobs SET plex='missing' WHERE id=?", (jid,))
    w._plex_found(jid)
    assert jobs.job_view(ctx.db, jid)["plex"] == "found"
