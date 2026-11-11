import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import MOVIE_URL, FakeMega, Sent, tmdb_handler
from linkarr import auth
from linkarr.main import create_app

H = {"X-Linkarr": "1"}


@pytest.fixture
def client(cfg):
    mega = FakeMega()
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    sent = Sent()
    app = create_app(cfg, mega=mega, tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)),
                     telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)),
                     plex_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    with TestClient(app) as c:
        c.sent = sent
        yield c


def sign_up(c):
    code = auth.setup_code(c.app.state.ctx.db)
    r = c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"}, headers=H)
    assert r.status_code == 200, r.text


def test_needs_sign_in_and_header(client):
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/auth/state").json()["setup_needed"]
    bad = client.post("/api/auth/setup", json={"code": "nope", "username": "x", "password": "0123456789"}, headers=H)
    assert bad.status_code == 403
    sign_up(client)
    assert client.get("/api/overview").status_code == 200
    assert client.post("/api/libraries", json={"name": "Movies", "kind": "movie", "path": "Movies"}).status_code == 403
    r = client.get("/")
    assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_library_link_and_settings(client, cfg):
    sign_up(client)
    (cfg.media_dir / "Movies").mkdir()
    r = client.post("/api/libraries", json={"name": "Movies", "kind": "movie", "path": "../etc"}, headers=H)
    assert r.status_code == 400
    lib = client.post("/api/libraries", json={"name": "Movies", "kind": "movie", "path": "Movies"}, headers=H).json()
    client.put("/api/secrets", json={"tmdb_key": "test-key"}, headers=H)
    j = client.post("/api/jobs", json={"url": "grab " + MOVIE_URL}, headers=H).json()
    assert j["title"] == "Dune: Part Two" and j["library_id"] == lib["id"]
    again = client.post("/api/jobs", json={"url": MOVIE_URL}, headers=H)
    assert again.status_code == 400 and "already" in again.json()["detail"]
    j = client.patch(f"/api/jobs/{j['id']}", json={"folder_name": "Dune 2 (2024)"}, headers=H).json()
    assert j["target"].endswith("Movies/Dune 2 (2024)")
    assert client.post(f"/api/jobs/{j['id']}/start", json={"when": "tonight"}, headers=H).json()["status"] == "queued"
    ov = client.get("/api/overview").json()
    assert ov["libraries"][0]["name"] == "Movies" and ov["active"][0]["id"] == j["id"]


def test_secrets_never_come_back_out(client):
    sign_up(client)
    token = "123456:" + "T" * 35
    client.put("/api/secrets", json={"telegram_token": token, "plex_token": "plexsecret"}, headers=H)
    s = client.get("/api/settings").json()
    assert s["secrets"] == {"tmdb_key": False, "plex_token": True, "jellyfin_key": False, "emby_key": False,
                            "telegram_token": True, "opensubtitles_key": False, "opensubtitles_password": False}
    assert token not in str(s) and "plexsecret" not in str(s)
    assert token not in client.get("/api/backup").text
    assert token in client.get("/api/backup?secrets=1").text
    raw = (client.app.state.cfg.db_path).read_bytes()
    assert token.encode() not in raw and b"plexsecret" not in raw


def test_check_page_and_logs(client, cfg):
    sign_up(client)
    r = client.get("/api/check").json()
    names = [i["name"] for i in r["items"]]
    assert "Media folder" in names and "Libraries" in names and not r["ok"]
    assert "lines" in client.get("/api/logs").json()


def test_share_target_redirect(client):
    r = client.get("/share", params={"text": "look " + MOVIE_URL, "title": "A post"}, follow_redirects=False)
    # The whole shared text goes to the app's picker; nothing is added by opening the address.
    assert r.status_code == 303 and r.headers["location"].startswith("/?share=")
    assert "A%20post" in r.headers["location"] and "mega.nz" in r.headers["location"]


def test_backup_restore_round_trip(client, cfg):
    sign_up(client)
    (cfg.media_dir / "TV").mkdir()
    client.post("/api/libraries", json={"name": "TV", "kind": "series", "path": "TV"}, headers=H)
    client.put("/api/settings", json={"theme": "light", "recycle_days": 3}, headers=H)
    data = client.get("/api/backup").content
    client.delete("/api/libraries/1", headers=H)
    client.put("/api/settings", json={"theme": "dark"}, headers=H)
    r = client.post("/api/restore", files={"file": ("b.json", data, "application/json")}, headers=H)
    assert r.json()["libraries_added"] == 1
    s = client.get("/api/settings").json()
    assert s["prefs"]["theme"] == "light" and s["prefs"]["recycle_days"] == 3
