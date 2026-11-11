"""Auto-start when sure, ask on remakes and same-name titles, Start all, and the Base64 decoder."""
import base64

import httpx

from conftest import DOC_URL, KEY, MOVIE_URL, Sent, run
from linkarr import decode, jobs, prefs
from linkarr.telegram import Telegram

DUNE_URL = f"https://mega.nz/file/Dune1984#{KEY}"


def b64(text: str, times: int) -> str:
    for _ in range(times):
        text = base64.b64encode(text.encode()).decode()
    return text


def test_sure_link_starts_by_itself(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, MOVIE_URL))
    assert j["status"] == "queued" and j["note"] == ""


def test_off_by_default(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    assert run(jobs.add_link(ctx, MOVIE_URL))["status"] == "draft"


def test_remake_without_year_asks(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    mega.add(DUNE_URL, "Dune.1080p.BluRay.mkv", [("Dune.1080p.BluRay.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, DUNE_URL))
    assert j["status"] == "draft"
    assert "more than one" in j["note"] and "1984" in j["note"] and "2021" in j["note"]
    # Picking the right one settles it.
    j = run(jobs.update_job(ctx, j["id"], {"tmdb_id": 103}))
    assert j["year"] == 1984 and j["note"] == ""


def test_remake_with_year_is_sure(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    mega.add(DUNE_URL, "Dune.2021.2160p.mkv", [("Dune.2021.2160p.mkv", 2000)], kind="file")
    j = run(jobs.add_link(ctx, DUNE_URL))
    assert j["status"] == "queued" and j["year"] == 2021


def test_already_have_it_still_asks(ctx, libs, mega):
    prefs.set_many(ctx.db, {"auto_start": True})
    (ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)").mkdir()
    (ctx.cfg.media_dir / "Movies" / "Dune - Part Two (2024)" / "old.mkv").write_bytes(b"x")
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    assert run(jobs.add_link(ctx, MOVIE_URL))["status"] == "draft"


def test_start_all(ctx, libs, mega):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")
    mega.add(DOC_URL, "Free.Solo.2018.1080p.mkv", [("Free.Solo.2018.1080p.mkv", 500)], kind="file")
    a = run(jobs.add_link(ctx, MOVIE_URL))
    b = run(jobs.add_link(ctx, DOC_URL))
    assert jobs.start_all(ctx) == {"started": 2, "skipped": 0}
    assert {jobs.job_view(ctx.db, x["id"])["status"] for x in (a, b)} == {"queued"}


def test_decode_layers():
    url = f"https://mega.nz/file/Abc12345#{'K' * 43}"
    for n in (1, 2, 4):
        r = decode.unwrap(b64(url, n))
        assert r["links"] == [url] and len(r["layers"]) == n
    assert decode.unwrap(b64(url, 5))["done"] == "max_layers"
    assert decode.unwrap("check this: " + b64(url, 2) + " thanks")["links"] == [url]
    bare = decode.unwrap(b64(url.removeprefix("https://"), 1))
    assert bare["links"] == [url]
    assert decode.unwrap("hello there")["done"] == "not_base64"
    assert decode.unwrap(b64("just some words, no link", 2))["done"] == "no_link"


def test_telegram_decodes_and_asks(ctx, libs, mega):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    tg = Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.mkv", [("Dune.Part.Two.2024.2160p.mkv", 2000)], kind="file")

    async def go():
        await tg.on_message({"chat": {"id": 42}, "text": b64(MOVIE_URL, 3)})
        msg = sent.calls[-1][1]
        assert "Decoded 3 times" in msg["text"] and KEY not in msg["text"]
        assert ctx.db.one("SELECT count(*) AS n FROM jobs")["n"] == 0  # asks first
        data = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await tg.on_button({"id": "q", "data": data, "message": {"chat": {"id": 42}, "message_id": 9}})
        assert ctx.db.one("SELECT title FROM jobs")["title"] == "Dune: Part Two"
        await tg.on_message({"chat": {"id": 42}, "text": "/startall"})
        assert "Started 1" in sent.calls[-1][1]["text"]

    run(go())
