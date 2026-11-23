"""1.2: the Cerberus gateway as one more chat. What the bot sends goes there, and what you type there is answered there."""
import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from conftest import MOVIE_URL, FakeMega, Sent, run, tmdb_handler
from linkarr import prefs
from linkarr.main import create_app
from linkarr.telegram import GATEWAY, Telegram, gateway_message

SECRET = "s" * 24


def setup_gateway(ctx):
    prefs.set_many(ctx.db, {"gateway_url": "http://cerberus.test:9065/"})
    ctx.vault.set("gateway_token", "cb-" + "t" * 30)
    ctx.vault.set("gateway_secret", SECRET)


def ingests(sent):
    return [body for method, body in sent.calls if method == "ingest"]


def test_message_shape():
    text, actions, taps = gateway_message('<b>🎬 Dune</b>\nSee <a href="https://example.org/x">the page</a> &amp; more',
                                          [[{"text": "▶️ Start", "callback_data": "go:4"}, {"text": "Site", "url": "https://example.org"}]])
    assert text == "**🎬 Dune**\nSee the page & more"
    assert actions[0] == {"label": "▶️ Start", "reply": "▶️ Start"} and actions[1]["url"] == "https://example.org"
    assert actions[2]["url"] == "https://example.org/x" and taps == {"▶️ Start": "go:4"}


def test_gateway_is_one_more_chat(ctx, libs, mega):
    sent = Sent()
    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    assert not tg.configured() and GATEWAY not in tg.chats()
    setup_gateway(ctx)
    assert prefs.get(ctx.db, "gateway_url") == "http://cerberus.test:9065"
    assert tg.configured() and GATEWAY in tg.chats() and not tg.token()
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        # A link typed in Cerberus: the job card comes back there, answering your message, with its buttons.
        await tg.on_gateway({"channel": "linkarr", "messageId": "m1", "text": "grab this " + MOVIE_URL})
        card = ingests(sent)[-1]
        assert card["channel"] == "linkarr" and card["replyTo"] == "m1" and "Dune: Part Two" in card["text"]
        assert not [c for c in sent.calls if c[0] == "sendMessage"]          # nothing went to Telegram
        jid = ctx.db.one("SELECT id FROM jobs")["id"]
        start = next(a for a in card["actions"] if a.get("reply") and "start" in a["label"].lower())
        # Tapping a button: its label comes back, with the message it was on; the card changes in place.
        await tg.on_gateway({"channel": "linkarr", "messageId": "m2", "text": start["reply"], "about": {"key": card["key"]}})
        assert ctx.db.one("SELECT status FROM jobs WHERE id = ?", (jid,))["status"] in ("queued", "downloading", "waiting")
        assert ingests(sent)[-1]["key"] == card["key"]
        # Broadcasts (news, alerts) go to the gateway too.
        await tg.broadcast("✅ <b>Landed</b>")
        assert ingests(sent)[-1]["text"] == "✅ **Landed**" and "replyTo" not in ingests(sent)[-1]
        assert sent.calls[-1][0] == "ingest"

    run(go())


def test_gateway_endpoint_needs_the_secret(cfg):
    sent = Sent()
    app = create_app(cfg, mega=FakeMega(), tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)),
                     telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)),
                     plex_client=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    with TestClient(app) as c:
        body = {"channel": "linkarr", "messageId": "m1", "text": "/help"}
        assert c.post("/api/gateway/message", json=body, headers={"X-Cerberus-Secret": SECRET}).status_code == 403   # not set up
        setup_gateway(c.app.state.ctx)
        assert c.post("/api/gateway/message", json=body, headers={"X-Cerberus-Secret": "wrong"}).status_code == 403
        assert c.post("/api/gateway/message", json=body, headers={"X-Cerberus-Secret": SECRET}).json() == {"ok": True}
        for _ in range(50):
            if ingests(sent):
                break
            time.sleep(0.02)
        assert ingests(sent) and ingests(sent)[0]["replyTo"] == "m1" and "/status" in ingests(sent)[0]["text"]
        assert json.dumps(ingests(sent)).count("Authorization") == 0


def test_home_line_in_cerberus(ctx, libs, mega):
    puts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append((request.url.path, json.loads(request.content), request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert tg.home_line() == {"text": "Nothing downloading"}
    setup_gateway(ctx)
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        await tg.on_gateway({"channel": "linkarr", "messageId": "m1", "text": "grab this " + MOVIE_URL})
        jid = ctx.db.one("SELECT id FROM jobs")["id"]
        ctx.db.run("UPDATE jobs SET status = 'downloading' WHERE id = ?", (jid,))
        ctx.db.run("UPDATE links SET bytes_done = 500 WHERE job_id = ?", (jid,))
        line = tg.home_line()
        assert line["text"] == "Downloading 1 · 25%" and line["progress"] == 0.25 and line["tone"] == "info"
        ctx.db.run("UPDATE jobs SET status = 'needs_answer' WHERE id = ?", (jid,))
        assert tg.home_line() == {"text": "🔒 1 waiting for you", "tone": "warn"}
        ctx.db.run("UPDATE jobs SET status = 'done', finished_at = 1 WHERE id = ?", (jid,))
        assert tg.home_line()["text"].startswith("Last added: Dune")
        # The loop sends it once and stops when told to.
        stop = asyncio.Event()
        task = asyncio.create_task(tg.run_line(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert len(puts) == 1 and puts[0][0] == "/api/lines/linkarr" and puts[0][1]["text"].startswith("Last added: Dune")
        assert puts[0][2] == "Bearer cb-" + "t" * 30

    run(go())


def test_cerberus_settings_hidden_until_a_gateway_is_set(cfg):
    """1.2.2: Cerberus is a private gateway, so its settings stay out of the way until one is set (or LA_GATEWAY=1)."""
    from dataclasses import replace

    from linkarr import auth

    def settings(c):
        return c.get("/api/settings").json()["gateway"]

    for env_on in (False, True):
        app = create_app(replace(cfg, gateway=env_on), mega=FakeMega(),
                         tmdb_client=httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler)))
        with TestClient(app) as c:
            if env_on is False:
                code = auth.setup_code(c.app.state.ctx.db)
                assert c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"},
                              headers={"X-Linkarr": "1"}).status_code == 200
                assert settings(c) is False
                setup_gateway(c.app.state.ctx)
                assert settings(c) is True
                prefs.set_many(c.app.state.ctx.db, {"gateway_url": ""})
                c.app.state.ctx.vault.set("gateway_token", "")
                c.app.state.ctx.vault.set("gateway_secret", "")
                assert settings(c) is False
            else:
                c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"}, headers={"X-Linkarr": "1"})
                assert settings(c) is True
