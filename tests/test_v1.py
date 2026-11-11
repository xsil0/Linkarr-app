"""1.0: the link picker, saved archive passwords, Jellyfin and Emby, password reset, the lockout that
only locks out the guesser, and the safer defaults."""
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import KEY, FakeMega, Sent, run, tmdb_handler
from linkarr import auth, files, post, prefs
from linkarr.main import create_app
from linkarr.mediaserver import MediaServer
from linkarr.notify import Notifier
from test_v0101 import fake_tool, only

H = {"X-Linkarr": "1"}
S1_1080 = f"https://mega.nz/folder/Sev10801#{KEY}"
S1_720 = f"https://mega.nz/folder/Sev07201#{KEY}"
S2_1080 = f"https://mega.nz/folder/Sev10802#{KEY}"
DEAD = f"https://mega.nz/file/Gone0000#{KEY}"


# ---- the link picker ------------------------------------------------------------------------

def test_choose_ticks_best_quality_and_every_season():
    items = [
        {"url": "a", "title": "Severance", "seasons": [1], "resolution": "1080p"},
        {"url": "b", "title": "Severance", "seasons": [1], "resolution": "720p"},
        {"url": "c", "title": "Severance", "seasons": [2], "resolution": "1080p"},
        {"url": "d", "title": "Severance", "seasons": [2], "resolution": "2160p"},
        {"url": "e", "title": "Free Solo", "year": 2018, "seasons": [], "resolution": None},
        {"url": "f", "title": "", "seasons": [], "resolution": None, "error": "Link not found."},
    ]
    post.choose(items)
    sel = {i["url"]: i["selected"] for i in items}
    assert sel == {"a": True, "b": False, "c": False, "d": True, "e": True, "f": False}
    assert items[0]["group"] == items[1]["group"] != items[2]["group"] == items[3]["group"]
    assert items[4]["group"] == ""


def test_choose_keeps_everything_it_cant_tell_apart():
    # Two links for one season, both 1080p: probably parts, not alternatives. Both stay ticked.
    items = [{"url": "a", "title": "Show", "seasons": [1], "resolution": "1080p"},
             {"url": "b", "title": "Show", "seasons": [1], "resolution": "1080p"}]
    post.choose(items)
    assert [i["selected"] for i in items] == [True, True]


def test_password_and_codes_in_a_post():
    assert post.find_password("Great film!\nPassword: 2024\nEnjoy") == "2024"
    assert post.find_password("pw - movies4all") == "movies4all"
    assert post.find_password("no secret here") == ""
    import base64
    code = base64.b64encode(S1_1080.encode()).decode()
    mega, caps = post.links_in(f"Season 1\n{code}\nhttps://links.example/abc", ["links.example"])
    assert mega == [S1_1080] and caps == ["https://links.example/abc"]


def test_scan_reads_names_and_labels(ctx):
    m = ctx.mega
    m.add(S1_1080, "Severance.S01.1080p.WEB-DL", [("e1.mkv", 10)])
    m.add(S1_720, "Severance.S01.720p.WEB-DL", [("e1.mkv", 5)])
    m.add(S2_1080, "Sev2", [("Severance.S02E01.1080p.mkv", 10)])
    text = f"Severance\n1080p: {S1_1080}\n720p: {S1_720}\nSeason 2 1080p\n{S2_1080}\n{DEAD}\nPassword: abc"
    r = run(post.scan(ctx, text, []))
    by = {i["url"]: i for i in r["links"]}
    assert r["password"] == "abc"
    assert by[S1_1080]["selected"] and not by[S1_720]["selected"]
    assert by[S1_1080]["label"] == "1080p" and by[S2_1080]["label"] == "Season 2 1080p"
    assert by[S2_1080]["seasons"] == [2] and by[S2_1080]["resolution"] == "1080p" and by[S2_1080]["selected"]
    assert by[DEAD]["error"] and not by[DEAD]["selected"]


@pytest.fixture
def client(cfg):
    mega = FakeMega()
    mega.add(S1_1080, "Severance.S01.1080p", [("Severance.S01E01.mkv", 10)])
    mega.add(S1_720, "Severance.S01.720p", [("Severance.S01E01.mkv", 5)])
    sent = Sent()
    app = create_app(cfg, mega=mega, tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)),
                     telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)),
                     plex_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)),
                     server_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    with TestClient(app) as c:
        c.sent = sent
        yield c


def sign_up(c, password="correct horse battery"):
    code = auth.setup_code(c.app.state.ctx.db)
    r = c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": password}, headers=H)
    assert r.status_code == 200, r.text


def test_scan_adds_nothing_and_new_job_keeps_the_post_password(client):
    sign_up(client)
    r = client.post("/api/scan", json={"text": f"{S1_1080}\n{S1_720}\npass: s3cret"}, headers=H).json()
    assert len(r["links"]) == 2 and r["password"] == "s3cret"
    assert client.get("/api/overview").json()["active"] == []
    job = client.post("/api/jobs", json={"url": S1_1080, "archive_password": "s3cret"}, headers=H).json()
    assert job["has_password"] and "s3cret" not in str(job)
    again = client.post("/api/scan", json={"text": S1_1080}, headers=H).json()
    assert again["links"][0]["queued"] and not again["links"][0]["selected"]


# ---- saved archive passwords ----------------------------------------------------------------

def test_saved_passwords_fill_in_year_and_title(db):
    prefs.set_many(db, {"archive_passwords": "{year}\nwww.site.example\n{title}{year}\n{year}\n  \nwith space "})
    assert prefs.get(db, "archive_passwords") == ["{year}", "www.site.example", "{title}{year}", "with space "]
    assert prefs.archive_passwords(db, "Dune", 2021) == ["2021", "www.site.example", "Dune2021", "with space "]
    assert prefs.archive_passwords(db, "Dune", None) == ["www.site.example", "with space "]


def test_unpack_tries_saved_passwords_in_order(tmp_path, monkeypatch):
    log = tmp_path / "tries.txt"
    unrar = fake_tool(tmp_path, "unrar", f"""
        pw = next(a for a in sys.argv if a.startswith("-p"))[2:]
        open({str(log)!r}, "a").write(pw + "\\n")
        if pw != "2021":
            print("Incorrect password for Film.rar"); sys.exit(11)
        dest = pathlib.Path(sys.argv[-1]); (dest / "Film.mkv").write_bytes(b"v")
    """)
    only(monkeypatch, unrar=unrar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Film.rar").write_bytes(b"rar")
    assert run(files.unpack(stage, "", ["site.example", "2021", "never"])) == []
    assert log.read_text().split() == ["-", "site.example", "2021"]
    assert (stage / "Film.mkv").exists()


def test_unpack_says_when_no_saved_password_works(tmp_path, monkeypatch):
    unrar = fake_tool(tmp_path, "unrar", "print('Incorrect password for Film.rar'); sys.exit(11)")
    only(monkeypatch, unrar=unrar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Film.rar").write_bytes(b"rar")
    assert run(files.unpack(stage, "", ["a", "b"])) == [
        "Couldn't unpack Film.rar: none of the saved passwords opened it. Add the right one on the job and try again."]


def test_unpack_stops_trying_on_a_missing_part(tmp_path, monkeypatch):
    log = tmp_path / "tries.txt"
    unrar = fake_tool(tmp_path, "unrar", f"""
        open({str(log)!r}, "a").write("x\\n"); print("Cannot find volume Film.part2.rar"); sys.exit(10)
    """)
    only(monkeypatch, unrar=unrar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Film.rar").write_bytes(b"rar")
    problems = run(files.unpack(stage, "", ["a", "b", "c"]))
    assert "missing" in problems[0] and log.read_text().count("x") == 1


# ---- Jellyfin and Emby -----------------------------------------------------------------------

def test_jellyfin_refresh_and_test(client):
    sign_up(client)
    assert client.post("/api/mediaserver/jellyfin/test", headers=H).status_code == 400   # not set up yet
    client.put("/api/settings", json={"jellyfin_url": "jf.lan:8096/"}, headers=H)
    client.put("/api/secrets", json={"jellyfin_key": "jfkey"}, headers=H)
    assert client.get("/api/settings").json()["prefs"]["jellyfin_url"] == "http://jf.lan:8096"
    assert client.post("/api/mediaserver/jellyfin/test", headers=H).status_code == 200
    assert client.post("/api/mediaserver/plexish/test", headers=H).status_code == 404
    ok = run(client.app.state.servers["jellyfin"].refresh())
    assert ok and ("Refresh", {}) in client.sent.calls
    assert "jfkey" not in str(client.get("/api/settings").json())


def test_job_done_names_every_server_that_updates(ctx):
    sent = Sent()
    c = httpx.AsyncClient(transport=httpx.MockTransport(sent.handler))
    jf = MediaServer("jellyfin", lambda: "http://jf", lambda: "k", c)
    emby = MediaServer("emby", lambda: "", lambda: "", c)   # not set up: never asked
    n = Notifier(ctx, None, None, [jf, emby])
    assert run(n.refresh_servers()) == ["Jellyfin"]


def test_bad_key_is_explained():
    from linkarr import mediaserver
    req = httpx.Request("GET", "http://x")
    e = httpx.HTTPStatusError("no", request=req, response=httpx.Response(401, request=req))
    assert mediaserver.explain(e, "Emby") == "Emby didn't accept the API key."
    assert "Check the address" in mediaserver.explain(httpx.ConnectError("x"), "Emby")


# ---- sign-in -------------------------------------------------------------------------------

def test_lockout_is_per_address_with_an_overall_cap(db):
    auth.create_user(db, "admin", "correct horse battery")
    for _ in range(auth.MAX_FAILS):
        assert auth.login(db, "admin", "wrong", 24, "10.0.0.66") is None
    assert auth.locked_for(db, "10.0.0.66") > 0
    assert auth.locked_for(db, "10.0.0.2") == 0          # you can still get in from your own device
    for i in range(auth.MAX_FAILS_ALL):
        auth.login(db, "admin", "wrong", 24, f"10.1.0.{i}")
    assert auth.locked_for(db, "10.0.0.2") > 0           # someone hopping addresses locks everyone for a while


def test_password_reset_keeps_everything_else(client):
    sign_up(client)
    db = client.app.state.ctx.db
    client.put("/api/settings", json={"recycle_days": 5}, headers=H)
    code = auth.reset(db)
    assert client.get("/api/overview").status_code == 401           # old sessions are gone
    st = client.get("/api/auth/state").json()
    assert st["setup_needed"] and not st["signed_in"]
    r = client.post("/api/auth/setup", json={"code": code.upper(), "username": "nick2", "password": "another long one"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/settings").json()["prefs"]["recycle_days"] == 5


def test_reset_command(cfg, capsys, monkeypatch):
    from linkarr.db import Database
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    d = Database(cfg.db_path)
    auth.create_user(d, "admin", "correct horse battery")
    d.close()
    monkeypatch.setenv("LA_DATA", str(cfg.data_dir))
    monkeypatch.setattr("sys.argv", ["linkarr", "reset-password"])
    import runpy
    runpy.run_module("linkarr", run_name="__main__")
    out = capsys.readouterr().out
    assert "setup code" in out
    d = Database(cfg.db_path)
    assert not auth.has_user(d) and auth.setup_code(d) in out
    d.close()


def test_setup_code_guessing_is_locked_out(client):
    for _ in range(auth.MAX_FAILS):
        r = client.post("/api/auth/setup", json={"code": "nope", "username": "x", "password": "0123456789"}, headers=H)
        assert r.status_code == 403
    r = client.post("/api/auth/setup", json={"code": "nope", "username": "x", "password": "0123456789"}, headers=H)
    assert r.status_code == 429


def test_forwarded_for_is_not_trusted_from_the_page(client):
    # A made-up X-Forwarded-For can't dodge the per-address lockout: the app uses the address uvicorn settled on.
    sign_up(client)
    client.post("/api/auth/logout", headers=H)
    for i in range(auth.MAX_FAILS):
        client.post("/api/auth/login", json={"username": "admin", "password": "bad"}, headers={**H, "X-Forwarded-For": f"1.2.3.{i}"})
    r = client.post("/api/auth/login", json={"username": "admin", "password": "bad"}, headers={**H, "X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 429


# ---- defaults ------------------------------------------------------------------------------

def test_new_defaults(db):
    assert prefs.get(db, "chat_clear") is False
    assert prefs.DEFAULTS["captcha_hosts"] == ""   # no captcha site is built in
    db.set_setting("pref.theme", "terminal")   # a theme from before 1.0
    assert prefs.get(db, "theme") == "dark"
    assert prefs.set_many(db, {"theme": "poster"})["theme"] == "dark"
    assert prefs.set_many(db, {"theme": "light"})["theme"] == "light"


def test_saved_passwords_only_go_in_the_backup_with_keys(client):
    sign_up(client)
    client.put("/api/settings", json={"archive_passwords": "{year}\nsecret.example"}, headers=H)
    plain = client.get("/api/backup").json()
    assert "archive_passwords" not in plain["prefs"] and "secrets" not in plain
    full = client.get("/api/backup?secrets=1").json()
    assert full["prefs"]["archive_passwords"]


def test_show_folder_queries():
    from linkarr.jobs import show_query
    assert show_query("Anatomy of a Scandal 2022") == "Anatomy of a Scandal"
    assert show_query("Family Plan (Korean)") == "Family Plan"
    assert show_query("Evil (2019) [tvdb-1]") == "Evil"
    assert show_query("Blade Runner 2049") == "Blade Runner 2049"
    assert show_query("1923") == "1923"


def test_session_ids_cut_the_id_out_of_the_saved_session():
    from linkarr.mega import b64e, session_ids
    sid = bytes(range(43))
    plain = b64e(bytes(16) + sid)
    assert session_ids(plain) == [plain, b64e(sid)]
    versioned = b64e(b"\x01" + bytes(16) + sid)
    assert b64e(sid) in session_ids(versioned)
    assert session_ids("not base64 !!")[0] == "not base64 !!"


def test_picking_an_unmatched_folder_by_hand_drops_it_from_the_list(client):
    sign_up(client)
    client.app.state.track_all = {"running": False, "total": 2, "done": 2, "added": 0, "error": "",
                                  "unmatched": [{"folder": "Eric", "library": "TV", "library_id": 1, "query": "Eric"},
                                                {"folder": "Gannibal 2022", "library": "TV", "library_id": 1, "query": "Gannibal"}]}
    r = client.post("/api/shows/42/watch", json={"watching": True, "name": "Eric", "folder": "Eric", "library_id": 1}, headers=H)
    assert r.status_code == 200
    left = client.get("/api/shows/track-all").json()["unmatched"]
    assert [u["folder"] for u in left] == ["Gannibal 2022"]
    assert [s["name"] for s in client.get("/api/shows").json()] == ["Eric"]
