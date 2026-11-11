"""0.6.1: /site sends your download site, /help and the bot's command menu."""
import httpx
from conftest import Sent, run

from linkarr import prefs
from linkarr.telegram import COMMANDS, Telegram


def bot(ctx):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    return Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler))), sent


def test_site_command_sends_the_link_from_settings(ctx):
    tg, sent = bot(ctx)
    run(tg.on_message({"chat": {"id": 42}, "text": "/site"}))
    assert sent.calls[-1][1]["text"] == "No download site set yet.\nAdd it in Settings → Connections."
    assert "reply_markup" not in sent.calls[-1][1]

    prefs.set_many(ctx.db, {"site_name": "My Site & Co", "site_url": "example.org/forum"})
    run(tg.on_message({"chat": {"id": 42}, "text": "/site@test_bot"}))
    msg = sent.calls[-1][1]
    assert msg["text"] == "🔗 <b>My Site &amp; Co</b>\nhttps://example.org/forum"
    assert msg["reply_markup"]["inline_keyboard"] == [[{"text": "Open My Site & Co", "url": "https://example.org/forum"}]]

    # The address changes: the next /site follows Settings, nothing is cached.
    prefs.set_many(ctx.db, {"site_url": "https://new.example.org"})
    run(tg.on_message({"chat": {"id": 42}, "text": "/link"}))
    assert sent.calls[-1][1]["text"].endswith("\nhttps://new.example.org")


def test_strangers_dont_get_the_site(ctx):
    tg, sent = bot(ctx)
    prefs.set_many(ctx.db, {"site_url": "https://example.org"})
    run(tg.on_message({"chat": {"id": 7}, "text": "/site"}))
    assert not sent.calls


def test_help_lists_one_command_per_line(ctx):
    tg, sent = bot(ctx)
    run(tg.on_message({"chat": {"id": 42}, "text": "/help"}))
    lines = sent.calls[-1][1]["text"].split("\n")
    assert [f"/{c} · {d}" for c, d in COMMANDS] == lines[3:]
    run(tg.on_message({"chat": {"id": 42}, "text": "hello"}))
    assert sent.calls[-1][1]["text"] == "\n".join(lines)


def test_command_menu_set_once_per_token(ctx):
    tg, sent = bot(ctx)
    run(tg.set_menu())
    run(tg.set_menu())
    menus = [body for method, body in sent.calls if method == "setMyCommands"]
    assert len(menus) == 1 and menus[0]["commands"][0] == {"command": "site", "description": "Open your download site"}
    ctx.vault.set("telegram_token", "654321:" + "U" * 35)
    run(tg.set_menu())
    assert len([m for m, _ in sent.calls if m == "setMyCommands"]) == 2
