"""1.2.3: whole Reddit posts with links hidden behind words (Telegram and paste), several links in one Telegram
message, and a remembered 'Season N - …' show no longer claiming every link called just 'Season 1'."""
import httpx

from conftest import KEY, SHOW_S1, SHOW_S2, Sent, run
from linkarr import jobs, post, prefs
from linkarr.telegram import Telegram, with_hidden_links
from test_v09 import CLASS_200, MC, add_class

S3 = f"https://mega.nz/folder/Fold3333#{KEY}"


def sev_season(mega, url, n, name=None):
    mega.add(url, name or f"Season {n}", [(f"Season {n}/Severance.S0{n}E0{e}.1080p.WEB-DL.mkv", 1000) for e in (1, 2, 3)])


def remember_masterclass(ctx, mega):
    add_class(mega, SHOW_S1, CLASS_200)

    async def go():
        j = await jobs.add_link(ctx, SHOW_S1)
        await jobs.match_show(ctx, j["id"], {"folder": MC}, remember=True)
    run(go())


# ---- the wrong title: 'Season 1' links went to the remembered MasterClass -----------------------

def test_plain_season_link_takes_the_show_from_its_files(ctx, libs, mega):
    remember_masterclass(ctx, mega)
    sev_season(mega, SHOW_S2, 1)
    j = run(jobs.add_link(ctx, SHOW_S2))
    assert j["title"] == "Severance" and j["tmdb_id"] == 201 and j["show_unknown"] == 0
    assert j["status"] == "draft" and "Check it's right" in j["note"]


def test_plain_season_link_with_no_show_anywhere_asks_instead_of_guessing(ctx, libs, mega):
    remember_masterclass(ctx, mega)
    mega.add(SHOW_S2, "Season 1", [("Season 1/01.mkv", 1000), ("Season 1/02.mkv", 1000)])
    j = run(jobs.add_link(ctx, SHOW_S2))
    assert j["title"] != "MasterClass" and j["show_unknown"] == 1


def test_lesson_named_links_still_use_the_remembered_show(ctx, libs, mega):
    remember_masterclass(ctx, mega)
    add_class(mega, SHOW_S2, "Season 201 - The Conflict Playbook with Amanda Ripley")
    j = run(jobs.add_link(ctx, SHOW_S2))
    assert j["folder_name"] == MC
    assert not jobs._lesson_name("Season 1") and not jobs._lesson_name("Season 2 - 1080p WEB-DL")
    assert jobs._lesson_name(CLASS_200)


def test_post_label_names_the_show(ctx, libs, mega):
    mega.add(SHOW_S2, "Season 2", [("Season 2/02x01.mkv", 1000), ("Season 2/02x02.mkv", 1000)])
    j = run(jobs.add_link(ctx, SHOW_S2, hint="Severance Season 2 (1080p)"))
    assert j["title"] == "Severance"
    mega.add(S3, "Season 2", [("Season 2/02x01.mkv", 1000), ("Season 2/02x02.mkv", 1000)])
    r = run(post.scan(ctx, f"Severance Season 2: {S3}", []))
    assert r["links"][0]["title"] == "Severance" and r["links"][0]["kind"] == "series"


# ---- links hidden behind words --------------------------------------------------------------------

def test_telegram_entities_give_back_hidden_links():
    text = "🎬 Entourage\nSeason 1 | Season 2"
    s1 = text.index("Season 1")
    # Telegram counts in UTF-16 code units: the emoji is two of them.
    msg = {"text": text, "entities": [{"type": "bold", "offset": 3, "length": 9},
                                      {"type": "text_link", "offset": s1 + 1, "length": 8, "url": SHOW_S1},
                                      {"type": "text_link", "offset": s1 + 1 + 11, "length": 8, "url": SHOW_S2}]}
    out = with_hidden_links(msg)
    assert f"Season 1 {SHOW_S1}" in out and f"Season 2 {SHOW_S2}" in out
    assert with_hidden_links({"caption": "x"}) == "x"


def test_reddit_backslashes_are_dropped():
    url = f"https://mega.nz/folder/ab_cdefg#{KEY}"
    assert post.links_in(url.replace("_", "\\_"), [])[0] == [url]


# ---- several links in one Telegram message ---------------------------------------------------------

def make_tg(ctx):
    sent = Sent()
    ctx.vault.set("telegram_token", "123456:" + "T" * 35)
    prefs.set_many(ctx.db, {"telegram_chats": ["42"]})
    return Telegram(ctx, httpx.AsyncClient(transport=httpx.MockTransport(sent.handler))), sent


def test_telegram_post_with_hidden_links_lists_then_adds_one_job_per_show(ctx, libs, mega):
    sev_season(mega, SHOW_S1, 1, "Severance.S01.1080p.WEB-DL")
    sev_season(mega, SHOW_S2, 2, "Severance.S02.1080p.WEB-DL")
    mega.add(S3, "Severance.S02.720p.WEB-DL", [("Severance.S02E01.720p.mkv", 500)])
    tg, sent = make_tg(ctx)
    text = "Severance, all seasons\nS1 1080p\nS2 1080p\nS2 720p\nPassword: tvforall"
    ents = [{"type": "text_link", "offset": text.index(w), "length": len(w), "url": u}
            for w, u in (("S1 1080p", SHOW_S1), ("S2 1080p", SHOW_S2), ("S2 720p", S3))]

    async def go():
        await tg.on_message({"chat": {"id": 42}, "message_id": 5, "text": text, "entities": ents})
        msg = sent.calls[-1][1]
        assert "3 links in this post" in msg["text"] and KEY not in msg["text"]
        assert "Another quality is ticked." in msg["text"]
        assert ctx.db.one("SELECT count(*) AS n FROM jobs")["n"] == 0      # nothing added until you tap
        data = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert msg["reply_markup"]["inline_keyboard"][0][0]["text"] == "➕ Add 2 ticked"
        await tg.on_button({"id": "q", "data": data, "message": {"chat": {"id": 42}, "message_id": 9}})
    run(go())
    rows = ctx.db.query("SELECT id, title, archive_password FROM jobs")
    assert len(rows) == 1 and rows[0]["title"] == "Severance"            # both seasons, one job
    assert ctx.db.one("SELECT count(*) AS n FROM links")["n"] == 2
    assert not ctx.db.query("SELECT key FROM settings WHERE key LIKE 'tg_post.%'")


def test_telegram_page_link_explains_why(ctx):
    tg, sent = make_tg(ctx)
    run(tg.on_message({"chat": {"id": 42}, "text": "https://www.reddit.com/r/something/comments/abc/entourage/"}))
    assert "doesn't open pages" in sent.calls[-1][1]["text"] and "reddit.com" in sent.calls[-1][1]["text"]
