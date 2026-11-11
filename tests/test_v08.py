"""0.8.0: captcha links (held as 'Needs captcha', on by default; Linkarr never tries the captcha itself) and
the library search putting the show you have first, with other titles by that name below."""
import base64
import json
import time

import httpx
from fastapi.testclient import TestClient

from conftest import MOVIE_URL, SHOW_S1, FakeMega, Sent, TMDB_TV, run, tmdb_handler
from linkarr import auth, captcha, decode, jobs, prefs, roundup
from linkarr.main import create_app
from linkarr.telegram import Telegram
from linkarr.worker import Worker

CAP = "https://links.example/AbCd1234"   # made up; points at nothing
H = {"X-Linkarr": "1"}


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def bot(ctx):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    return Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler))), sent


def add_movie(mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")


# ---- spotting captcha links -----------------------------------------------------------------------

def test_captcha_links_are_on_by_default(db):
    assert prefs.get(db, "captcha_enabled") is True
    assert prefs.captcha_hosts(db) == ["links.example"]
    assert prefs.set_many(db, {"captcha_hosts": "https://Other.example/x, junk, links.example"})["captcha_hosts"] == "other.example, links.example"
    assert prefs.set_many(db, {"captcha_hosts": ""})["captcha_hosts"] == ""
    assert prefs.captcha_hosts(db) == []          # no captcha site is built in


def test_find_captcha_links():
    hosts = ["links.example"]
    assert captcha.find(f"grab it: {CAP}.", hosts) == [CAP]
    assert captcha.find("links.example/AbCd1234", hosts) == [CAP]
    assert captcha.find("https://www.links.example/x1", hosts) == ["https://www.links.example/x1"]
    assert captcha.find("https://links.example", hosts) == []           # nothing after the host
    assert captcha.find("https://xlinks.example/a https://links.example.evil.com/a", hosts) == []


def test_decoder_finds_a_captcha_link_inside_base64():
    r = decode.unwrap(b64(b64(CAP)), hosts=["links.example"])
    assert r["done"] == "captcha" and r["captcha"] == [CAP] and r["links"] == [] and len(r["layers"]) == 2
    assert decode.unwrap(CAP, hosts=["links.example"])["captcha"] == [CAP]
    assert decode.unwrap(b64(MOVIE_URL), hosts=["links.example"])["captcha"] == []
    assert decode.unwrap(CAP)["captcha"] == []    # none built in


# ---- holding and solving --------------------------------------------------------------------------

def test_hold_then_paste_the_mega_link(ctx, libs, mega):
    add_movie(mega)
    hold = jobs.hold_captcha(ctx, "here " + CAP)
    assert hold["status"] == "needs_captcha" and hold["hold_url"] == CAP and not hold["held_before"]
    assert jobs.hold_captcha(ctx, CAP)["id"] == hold["id"]                # same link, same hold
    for bad, why in ((CAP, "captcha link again"), ("solved it!", "no MEGA link")):
        try:
            run(jobs.solve_captcha(ctx, hold["id"], bad))
            raise AssertionError("accepted " + bad)
        except jobs.JobError as e:
            assert why in str(e)
    try:
        jobs.start_job(ctx, hold["id"])
        raise AssertionError("started a hold")
    except jobs.JobError as e:
        assert "captcha" in str(e)
    added = run(jobs.solve_captcha(ctx, hold["id"], b64(MOVIE_URL)))       # a code works too
    assert [j["title"] for j in added] == ["Dune: Part Two"] and added[0]["status"] == "draft"
    assert jobs.job_view(ctx.db, hold["id"]) is None


def test_switch_off_and_wrong_box(ctx, libs):
    try:
        run(jobs.add_link(ctx, CAP))
        raise AssertionError("added a captcha link as MEGA")
    except jobs.JobError as e:
        assert "captcha" in str(e)
    prefs.set_many(ctx.db, {"captcha_enabled": False})
    try:
        jobs.hold_captcha(ctx, CAP)
        raise AssertionError("held while off")
    except jobs.JobError as e:
        assert "Settings → Extras" in str(e)


class Notes:
    def __init__(self):
        self.captchas = []

    async def captcha(self, job_id, reminder=False):
        self.captchas.append((job_id, reminder))

    async def send_html(self, text, buttons=None):
        pass


def test_one_reminder_after_a_day(ctx):
    hold = jobs.hold_captcha(ctx, CAP)
    ctx.notify = Notes()
    w = Worker(ctx)
    assert run(w.captcha_reminders()) == 0
    ctx.db.run("UPDATE jobs SET created_at = ? WHERE id = ?", (int(time.time()) - 25 * 3600, hold["id"]))
    assert run(w.captcha_reminders()) == 1
    assert run(w.captcha_reminders()) == 0
    assert ctx.notify.captchas == [(hold["id"], True)]
    assert "🔒 1 needs a captcha" in roundup.morning_text(ctx.db, 0)


# ---- Telegram -------------------------------------------------------------------------------------

def test_telegram_hold_reply_and_drop(ctx, libs, mega):
    add_movie(mega)
    tg, sent = bot(ctx)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": b64(CAP)}))
    msg = sent.calls[-1][1]
    assert msg["text"].splitlines()[:2] == ["🔒 <b>Needs captcha</b>", f"<code>{CAP}</code>"]
    assert "Reply to this message with the MEGA link." in msg["text"]
    rows = msg["reply_markup"]["inline_keyboard"]
    assert rows[0][0] == {"text": "🔓 Open link", "url": CAP}
    hold_id = int(rows[1][0]["callback_data"].split(":")[1])
    assert jobs.captcha_job_for(ctx.db, "42", 1) == hold_id                 # the fake API answers message_id 1

    run(tg.on_message({"chat": {"id": 42}, "text": MOVIE_URL, "reply_to_message": {"message_id": 1}}))
    methods = [m for m, _ in sent.calls]
    assert "editMessageText" in methods
    assert "Dune: Part Two" in sent.calls[-1][1]["text"]
    assert jobs.job_view(ctx.db, hold_id) is None and jobs.captcha_job_for(ctx.db, "42", 1) is None

    run(tg.on_message({"chat": {"id": 42}, "text": "look " + CAP}))
    again = ctx.db.one("SELECT id FROM jobs WHERE status = 'needs_captcha'")["id"]
    run(tg.on_button({"id": "q", "data": f"cx:{again}", "message": {"chat": {"id": 42}, "message_id": 1}}))
    assert jobs.job_view(ctx.db, again) is None
    assert sent.calls[-1][1]["text"] == "🗑 <b>Dropped</b>\nNothing was downloaded."


def test_telegram_says_when_captcha_links_are_off(ctx):
    tg, sent = bot(ctx)
    prefs.set_many(ctx.db, {"captcha_enabled": False})
    run(tg.on_message({"chat": {"id": 42}, "text": CAP}))
    assert sent.calls[-1][1]["text"].splitlines() == ["🔒 That's a captcha link.", "Captcha links are off.",
                                                      "Turn them on in Linkarr → Settings → Extras."]


# ---- API --------------------------------------------------------------------------------------------

def test_api_hold_and_solve(cfg):
    mega = FakeMega()
    add_movie(mega)
    sent = Sent()
    app = create_app(cfg, mega=mega, tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)),
                     telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    with TestClient(app) as c:
        code = auth.setup_code(c.app.state.ctx.db)
        c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"}, headers=H)
        c.app.state.ctx.vault.set("telegram_token", "123456:" + "T" * 35)
        prefs.set_many(c.app.state.ctx.db, {"telegram_chats": ["42"], "captcha_hosts": "links.example"})
        assert c.post("/api/decode", json={"text": b64(CAP)}, headers=H).json()["captcha"] == [CAP]
        job = c.post("/api/captcha", json={"url": CAP}, headers=H).json()
        assert job["status"] == "needs_captcha"
        assert any(m == "sendMessage" and CAP in b["text"] for m, b in sent.calls)   # sent to the phone too
        ov = c.get("/api/overview").json()
        assert ov["active"][0]["id"] == job["id"] and ov["extras"]["captcha"] is True
        assert c.post(f"/api/jobs/{job['id']}/captcha", json={"text": "nothing here"}, headers=H).status_code == 400
        r = c.post(f"/api/jobs/{job['id']}/captcha", json={"text": MOVIE_URL}, headers=H).json()
        assert r["jobs"][0]["status"] == "draft" and "Dune" in r["jobs"][0]["title"]


# ---- library search: yours first --------------------------------------------------------------------

def test_search_puts_your_show_first_then_similar(ctx, libs, monkeypatch):
    monkeypatch.setitem(TMDB_TV, 202, {"id": 202, "name": "Severance: Outie", "first_air_date": "2026-01-01", "poster_path": "",
                                       "genre_ids": [18], "genres": [], "seasons": []})
    show = ctx.cfg.media_dir / "TV Shows" / "Severance (2022)" / "Season 1"
    show.mkdir(parents=True)
    (show / "Severance - S01E01.mkv").write_bytes(b"x")
    missing = json.dumps({"episodes": {"1": [2, 3]}})
    ctx.db.run("INSERT INTO shows (tmdb_id, name, folder, library_id, missing) VALUES (201, 'Severance', 'Severance (2022)', ?, ?)",
               (libs["TV Shows"]["id"], missing))
    rows = run(jobs.search_library(ctx, "Severance", 201))
    assert [r["title"] for r in rows if r["kind"] == "series"] == ["Severance", "Severance: Outie"]
    first = rows[0]
    assert first["pinned"] and first["group"] == "mine" and first["have"]["folder"] == "Severance (2022)"
    assert first["missing"] == "Missing S1 episodes 2–3"
    spin = next(r for r in rows if r["id"] == 202)
    assert spin["group"] == "similar" and spin["have"] is None              # that folder is a different show
