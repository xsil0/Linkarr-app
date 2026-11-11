"""1.1: free space counts running downloads and unpacking, title cards, name from files, Library filter,
site logo, plain-http sign-in."""
import json
import shutil
from collections import namedtuple

from conftest import KEY, run
from linkarr import jobs, post
from linkarr.worker import Worker
from test_v1 import H, client, sign_up  # noqa: F401  (client is a fixture)

GB = 1024**3
Usage = namedtuple("Usage", "total used free")
BIG = f"https://mega.nz/file/Bigg1234#{KEY}"
RAR = f"https://mega.nz/folder/Rarr1234#{KEY}"


def _link(ctx, url):
    async def go():
        j = await jobs.add_link(ctx, url)
        jobs.start_job(ctx, j["id"])
        return j["id"]
    jid = run(go())
    return dict(ctx.db.one("SELECT * FROM links WHERE job_id = ?", (jid,)))


def test_space_counts_running_downloads_and_unpacking(ctx, libs, mega, monkeypatch):
    mega.add(BIG, "Dune Part Two 2024 1080p", [("Dune.Part.Two.2024.1080p.mkv", 6 * GB)], kind="file")
    mega.add(RAR, "Free Solo 2018 1080p", [("Free.Solo.2018.1080p.rar", 4 * GB)])
    first, second = _link(ctx, BIG), _link(ctx, RAR)
    w = Worker(ctx)
    staging = jobs.work_dir(ctx)
    staging.mkdir(parents=True, exist_ok=True)

    # 10 GB free: the archive alone needs 4 + 4 to unpack + 1 spare
    assert w._space_short(second, staging, 4 * GB, 10 * GB) is None
    assert w._space_short(second, staging, 4 * GB, 8 * GB)[0] == "download drive"
    assert Worker._unpack_room(second) == 4 * GB and Worker._unpack_room(first) == 0

    # with the 6 GB movie running, 10 GB is no longer enough
    ctx.db.run("UPDATE links SET status='downloading', bytes_done=0 WHERE id=?", (first["id"],))
    where, want, have = w._space_short(second, staging, 4 * GB, 10 * GB)
    assert where == "download drive" and want == 15 * GB and have == 10 * GB


def test_space_checks_the_library_drive_when_its_another_disk(ctx, libs, mega, monkeypatch):
    mega.add(BIG, "Dune Part Two 2024 1080p", [("Dune.Part.Two.2024.1080p.mkv", 6 * GB)], kind="file")
    link = _link(ctx, BIG)
    w = Worker(ctx)
    staging = jobs.work_dir(ctx)
    staging.mkdir(parents=True, exist_ok=True)
    real_stat = type(staging).stat

    def stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if "Movies" in str(p):
            return type("S", (), {"st_dev": st.st_dev + 1})()
        return st
    monkeypatch.setattr(type(staging), "stat", stat)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(0, 0, 2 * GB if "Movies" in str(p) else 100 * GB))
    assert w._space_short(link, staging, 6 * GB, 100 * GB)[0] == "library drive"
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(0, 0, 100 * GB))
    assert w._space_short(link, staging, 6 * GB, 100 * GB) is None


# ---- posters for tracked shows ----------------------------------------------------------------

def test_tracking_saves_the_poster_and_old_shows_get_theirs(client):
    c = client
    sign_up(c)
    ctx = c.app.state.ctx
    r = c.post("/api/shows/201/watch", json={"watching": True, "name": "Severance", "poster": "/sev.jpg"}, headers=H)
    assert r.status_code == 200
    assert ctx.db.one("SELECT poster FROM shows WHERE tmdb_id=201")["poster"] == "/sev.jpg"
    # a made-up path isn't stored
    c.post("/api/shows/201/watch", json={"watching": True, "name": "Severance", "poster": "../x"}, headers=H)
    assert ctx.db.one("SELECT poster FROM shows WHERE tmdb_id=201")["poster"] == "/sev.jpg"


def test_fill_posters_for_shows_tracked_before(ctx):
    ctx.db.run("INSERT INTO shows (tmdb_id, name, folder, watching) VALUES (201, 'Severance', 'Severance', 1)")
    w = Worker(ctx)
    assert run(w.fill_posters()) == 1
    assert ctx.db.one("SELECT poster FROM shows WHERE tmdb_id=201")["poster"] == "/sev.jpg"
    assert run(w.fill_posters()) == 0


# ---- mislabeled series ------------------------------------------------------------------------

def test_name_from_the_files_inside(tmp_path):
    d = tmp_path / "Cannibal Show"
    (d / "Season 1").mkdir(parents=True)
    for n in ("Gannibal.S01E01.1080p.mkv", "Gannibal.S01E02.1080p.mkv", "sample.txt"):
        (d / "Season 1" / n).write_bytes(b"x")
    assert jobs.name_from_files(d) == "Gannibal"
    (tmp_path / "Plain").mkdir()
    (tmp_path / "Plain" / "Episode 1.mkv").write_bytes(b"x")
    assert jobs.name_from_files(tmp_path / "Plain") == ""


def test_track_with_rename(client):
    c = client
    sign_up(c)
    ctx = c.app.state.ctx
    lib = jobs.save_library(ctx, {"name": "TV Shows", "kind": "series", "path": "TV Shows"})
    base = ctx.cfg.media_dir / "TV Shows"
    (base / "sev stuff").mkdir(parents=True)
    r = c.post("/api/shows/201/watch", json={"watching": True, "name": "Severance", "year": 2022, "folder": "sev stuff",
                                             "library_id": lib["id"], "rename": True}, headers=H).json()
    assert r["folder"] == "Severance (2022)" and not r["note"]
    assert (base / "Severance (2022)").is_dir() and not (base / "sev stuff").exists()
    assert ctx.db.one("SELECT folder FROM shows WHERE tmdb_id=201")["folder"] == "Severance (2022)"
    # a folder with that name already there: tracked under the old name, and it says why
    (base / "other").mkdir()
    r = c.post("/api/shows/201/watch", json={"watching": True, "name": "Severance", "year": 2022, "folder": "other",
                                             "library_id": lib["id"], "rename": True}, headers=H).json()
    assert r["folder"] == "other" and "already a folder" in r["note"]


# ---- site logo, library list, plain-http sign-in ---------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def test_site_logo_upload_serve_and_remove(client):
    c = client
    sign_up(c)
    ctx = c.app.state.ctx
    from linkarr import prefs
    prefs.set_many(ctx.db, {"site_name": "My Site", "site_url": "https://example.org"})
    r = c.post("/api/site-logo", files={"file": ("logo.svg", b"<svg onload=alert(1)>", "image/svg+xml")}, headers=H)
    assert r.status_code == 400 and "PNG" in r.json()["detail"]
    r = c.post("/api/site-logo", files={"file": ("logo.png", PNG, "image/png")}, headers=H)
    assert r.status_code == 200 and r.json()["site"]["logo"].startswith("/api/site-logo?v=")
    g = c.get("/api/site-logo")
    assert g.status_code == 200 and g.content == PNG and g.headers["content-type"] == "image/png"
    assert g.headers["x-content-type-options"] == "nosniff"
    assert c.delete("/api/site-logo", headers=H).status_code == 200
    assert c.get("/api/site-logo").status_code == 404 and prefs.site(ctx.db)["logo"] == ""


def test_library_list_shows_seasons_tracked_and_missing(client):
    c = client
    sign_up(c)
    ctx = c.app.state.ctx
    lib = jobs.save_library(ctx, {"name": "TV Shows", "kind": "series", "path": "TV Shows"})
    base = ctx.cfg.media_dir / "TV Shows"
    for s in ("Season 1", "Season 2", "Extras"):
        (base / "Severance (2022)" / s).mkdir(parents=True)
    (base / "Severance (2022)" / "Season 1" / "Severance.S01E01.mkv").write_bytes(b"x")
    (base / "Other Show").mkdir()
    ctx.db.run("INSERT INTO shows (tmdb_id, name, folder, library_id, watching, missing) VALUES (201,'Severance','Severance (2022)',?,1,?)",
               (lib["id"], json.dumps({"seasons": [], "episodes": {"2": [3, 4]}, "counts": {}})))
    rows = {r["folder"]: r for r in c.get("/api/library/folders").json()}
    sev = rows["Severance (2022)"]
    assert sev["seasons"] == 2 and sev["tracked"] and sev["tmdb_id"] == 201 and "S2" in sev["missing"].replace("Season ", "S")
    assert not rows["Other Show"]["tracked"] and rows["Other Show"]["seasons"] == 0
    one = c.get("/api/library/folder", params={"lib": lib["id"], "name": "Severance (2022)"}).json()
    assert one["seasons"] == {"1": 1}
    assert c.get("/api/library/folder", params={"lib": lib["id"], "name": ".."}).status_code == 404


def test_cookie_follows_the_address_unless_set(cfg):
    from dataclasses import replace

    import httpx
    from fastapi.testclient import TestClient

    from conftest import FakeMega, tmdb_handler
    from linkarr.config import _tri
    from linkarr.main import create_app
    assert _tri("") is None and _tri("1") is True and _tri("0") is False
    app = create_app(replace(cfg, cookie_secure=None), mega=FakeMega(),
                     tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)))
    for base, secure in (("http://192.168.1.10:9040", False), ("https://linkarr.example", True)):
        with TestClient(app, base_url=base) as c:
            code = __import__("linkarr.auth", fromlist=["x"]).setup_code(c.app.state.ctx.db)
            if not c.app.state.ctx.db.one("SELECT 1 FROM users"):
                r = c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"}, headers=H)
            else:
                r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers=H)
            assert r.status_code == 200, r.text
            assert ("secure" in r.headers["set-cookie"].lower()) is secure


# ---- back to the same thread: skip what you have ----------------------------------------------

def test_picker_skips_what_you_have_and_what_you_downloaded(ctx, libs):
    from linkarr import post
    m = ctx.mega
    A = f"https://mega.nz/file/Mclass01#{KEY}"
    B = f"https://mega.nz/file/Mclass02#{KEY}"
    C = f"https://mega.nz/file/Mclass03#{KEY}"
    D = f"https://mega.nz/folder/Sev20001#{KEY}"
    m.add(A, "Free.Solo.2018.1080p.mkv", [("Free.Solo.2018.1080p.mkv", 10)], kind="file")
    m.add(B, "Dune.Part.Two.2024.1080p.mkv", [("Dune.Part.Two.2024.1080p.mkv", 10)], kind="file")
    m.add(C, "Night.of.the.Living.Dead.1968.mkv", [("Night.of.the.Living.Dead.1968.mkv", 10)], kind="file")
    m.add(D, "Severance.S02.1080p", [("Severance.S02E01.1080p.mkv", 10)])
    (ctx.cfg.media_dir / "Documentaries" / "Free Solo (2018)").mkdir(parents=True)
    (ctx.cfg.media_dir / "TV Shows" / "Severance (2022)" / "Season 1").mkdir(parents=True)
    # B was downloaded before
    jid = ctx.db.run("INSERT INTO jobs (kind, title, status, created_at, updated_at) VALUES ('movie','Dune: Part Two','done',0,0)").lastrowid
    ctx.db.run("INSERT INTO links (job_id, url, name, status, created_at) VALUES (?,?,?,'moved',0)", (jid, B, "Dune"))
    r = run(post.scan(ctx, f"{A}\n{B}\n{C}\n{D}", []))
    by = {i["url"]: i for i in r["links"]}
    assert "You have it in Documentaries" in by[A]["have"] and not by[A]["selected"]
    assert by[B]["have"].startswith("Downloaded before") and not by[B]["selected"]
    assert not by[C]["have"] and by[C]["selected"]
    assert not by[D]["have"] and by[D]["selected"]  # Season 2 isn't on disk yet
    (ctx.cfg.media_dir / "TV Shows" / "Severance (2022)" / "Season 2").mkdir()
    r = run(post.scan(ctx, f"{D}", []))
    assert "this season" in r["links"][0]["have"] and not r["links"][0]["selected"]


def test_a_long_post_reads_every_link_and_every_code(ctx):
    # A thread with 120 links, most of them as Base64 codes, with forum chatter around each one.
    import base64
    urls = [f"https://mega.nz/folder/Mc{i:06d}#{KEY}" for i in range(120)]
    lines = []
    for i, u in enumerate(urls):
        ctx.mega.add(u, f"MasterClass.Lesson.{i:03d}", [("lesson.mp4", 10)])
        shown = u if i % 4 == 0 else base64.b64encode(u.encode()).decode()
        lines.append(f"Lesson {i}: some chatter about this one, thanks to the uploader " * 3 + "\n" + shown)
    r = run(post.scan(ctx, "\n\n".join(lines), []))
    assert len(r["links"]) == 120 and r["more"] == 0
    assert {i["url"] for i in r["links"]} == set(urls)
