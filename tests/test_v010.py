"""0.10.0: the bot's chat is cleared every night (pinned messages and cards that still need you stay)."""
import time

import httpx
from fastapi.testclient import TestClient

from conftest import MOVIE_URL, run
from linkarr import auth, jobs, prefs
from linkarr.main import create_app
from linkarr.notify import Notifier
from linkarr.telegram import Telegram
from linkarr.worker import Worker

CAP = "https://links.example/AbCd1234"   # made up; points at nothing
H = {"X-Linkarr": "1"}


class Chat:
    """A fake Telegram: hands out message ids in order, remembers what's deleted, can refuse deletes."""

    def __init__(self, pinned=None, refuse=()):
        self.next_id = 100
        self.calls = []
        self.deleted: list[int] = []
        self.pinned = pinned
        self.refuse = set(refuse)

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "sendMessage":
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getChat":
            pin = {"pinned_message": {"message_id": self.pinned}} if self.pinned else {}
            return httpx.Response(200, json={"ok": True, "result": {"id": body["chat_id"], **pin}})
        if method == "deleteMessages":
            if self.refuse & set(body["message_ids"]):
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: message can't be deleted"})
            self.deleted += body["message_ids"]
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "deleteMessage":
            if body["message_id"] in self.refuse:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: message can't be deleted"})
            self.deleted.append(body["message_id"])
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(200, json={"ok": True, "result": True})


def bot(ctx, **kw):
    chat = Chat(**kw)
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    return Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(chat.handler))), chat


def logged(ctx):
    return [r["message_id"] for r in ctx.db.query("SELECT message_id FROM tg_messages ORDER BY message_id")]


def test_defaults(db):
    assert prefs.get(db, "chat_clear") is False   # off until you turn it on
    assert prefs.get(db, "chat_clear_time") == "04:00"
    assert prefs.get(db, "chat_clear_keep_open") is True
    assert prefs.set_many(db, {"chat_clear_time": "3:5"})["chat_clear_time"] == "03:05"
    assert prefs.set_many(db, {"chat_clear_time": "late"})["chat_clear_time"] == "04:00"


def test_clear_deletes_yours_and_the_bots_and_keeps_pinned(ctx):
    tg, chat = bot(ctx, pinned=7)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": "/status"}))      # yours, and the reply (101)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 7, "text": "/help"}))        # you pinned this one later
    run(tg.on_message({"chat": {"id": 42}, "message_id": 103, "pinned_message": {"message_id": 7}}))
    assert logged(ctx) == [5, 7, 101, 102, 103]
    before = len(chat.calls)
    r = run(tg.clear())
    assert sorted(chat.deleted) == [5, 101, 102, 103]
    assert r == {"deleted": 4, "kept": 1, "failed": 0}
    assert logged(ctx) == [7]                                     # the pinned one, kept for next time
    assert [m for m, _ in chat.calls[before:]] == ["getChat", "deleteMessages"]   # nothing is posted after a clear


def test_the_chat_pin_from_telegram_is_kept_too(ctx):
    tg, chat = bot(ctx, pinned=101)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": "/status"}))
    run(tg.clear())
    assert chat.deleted == [5]


def test_cards_that_need_you_stay_until_answered(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    tg, chat = bot(ctx)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": MOVIE_URL}))      # a draft card: 101
    run(tg.on_message({"chat": {"id": 42}, "message_id": 6, "text": CAP}))            # a captcha card: 102
    draft = ctx.db.one("SELECT id FROM jobs WHERE status = 'draft'")["id"]
    run(tg.clear())
    assert sorted(chat.deleted) == [5, 6]
    assert logged(ctx) == [101, 102]
    # The captcha still answers a reply after the clear.
    assert jobs.captcha_job_for(ctx.db, "42", 102)

    jobs.start_job(ctx, draft)                                    # answered: it goes next time
    run(tg.clear())
    assert chat.deleted[-1] == 101 and logged(ctx) == [102]

    prefs.set_many(ctx.db, {"chat_clear_keep_open": False})       # or clear everything
    run(tg.clear())
    assert chat.deleted[-1] == 102 and logged(ctx) == []


def test_one_stuck_message_doesnt_stop_the_rest(ctx):
    tg, chat = bot(ctx, refuse={6})
    for mid in (5, 6, 7):
        tg.note("42", mid)
    r = run(tg.clear())
    assert sorted(chat.deleted) == [5, 7] and r["failed"] == 1
    assert logged(ctx) == []                                      # not tried again: it can't be deleted


def test_too_old_to_delete_is_dropped_not_tried(ctx):
    tg, chat = bot(ctx)
    tg.note("42", 5)
    ctx.db.run("UPDATE tg_messages SET sent_at = ?", (int(time.time()) - 49 * 3600,))
    tg.note("42", 6)
    run(tg.clear())
    assert chat.deleted == [6]


def test_clear_command(ctx):
    tg, chat = bot(ctx)
    run(tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": "/status"}))
    run(tg.on_message({"chat": {"id": 42}, "message_id": 8, "text": "/clear"}))
    assert sorted(chat.deleted) == [5, 8, 101]
    assert "clear" in tg.help_text()


def test_strangers_messages_are_not_logged(ctx):
    tg, _ = bot(ctx)
    run(tg.on_message({"chat": {"id": 99}, "message_id": 5, "text": "/start"}))
    assert logged(ctx) == []


def test_nightly_schedule(ctx):
    tg, chat = bot(ctx)
    ctx.notify = Notifier(ctx, tg, None)
    prefs.set_many(ctx.db, {"chat_clear": True})
    w = Worker(ctx)

    def at(day, hhmm):
        return time.mktime(time.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M"))

    tg.note("42", 5)
    assert run(w.chat_clear(at("2026-11-05", "03:59"))) is None   # not yet
    assert run(w.chat_clear(at("2026-11-05", "04:00")))["deleted"] == 1
    tg.note("42", 6)
    assert run(w.chat_clear(at("2026-11-05", "04:30"))) is None   # once a night
    assert run(w.chat_clear(at("2026-11-05", "15:00"))) is None   # never in the middle of the day
    assert run(w.chat_clear(at("2026-11-06", "06:30")))["deleted"] == 1   # Linkarr was off at 4: runs late
    prefs.set_many(ctx.db, {"chat_clear_time": "23:30"})
    tg.note("42", 7)
    assert run(w.chat_clear(at("2026-11-07", "00:15")))["deleted"] == 1   # late past midnight counts for the 6th
    assert run(w.chat_clear(at("2026-11-07", "01:00"))) is None
    prefs.set_many(ctx.db, {"chat_clear": False})
    assert run(w.chat_clear(at("2026-11-07", "23:30"))) is None


def test_clear_now_api(cfg):
    chat = Chat()
    app = create_app(cfg, telegram_client=httpx.AsyncClient(transport=httpx.MockTransport(chat.handler)))
    with TestClient(app) as c:
        db = app.state.ctx.db
        code = auth.setup_code(db)
        c.post("/api/auth/setup", json={"code": code, "username": "admin", "password": "correct horse battery"}, headers=H)
        assert c.post("/api/telegram/clear", headers=H).status_code == 400
        app.state.ctx.vault.set("telegram_token", "123456:" + "T" * 35)
        prefs.set_many(db, {"telegram_chats": ["42"]})
        app.state.tg.note("42", 9)
        assert c.post("/api/telegram/clear", headers=H).json() == {"deleted": 1, "kept": 0, "failed": 0}
        assert chat.deleted == [9]
