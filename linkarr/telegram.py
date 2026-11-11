"""Telegram bot: send it a MEGA link, answer its questions with buttons, get told when things land.

Polling (no webhook), so it works behind CGNAT with no open ports. Only chats listed in Settings
can use it; anyone else who writes /start is told their chat id and nothing more.

Captcha links (captcha.py) come back as a message with the link; you reply to it with the MEGA link.

Every message in the bot's chats is noted (tg_messages) so the nightly clear can delete it. Telegram only lets a
bot delete messages under 48 hours old, so that's how far back the clear reaches.
"""
import asyncio
import html
import json
import logging
import secrets
import time

import httpx

from . import captcha, decode, jobs, prefs, roundup, tracker, watch
from .mega import find_links, is_folder

log = logging.getLogger("linkarr.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

# The bot's command menu (the "/" button in Telegram), also what /help lists.
COMMANDS = [
    ("site", "Open your download site"),
    ("status", "What's downloading now"),
    ("startall", "Start everything waiting"),
    ("missing", "Episodes you don't have"),
    ("monitor", "Monitor a folder link for new episodes"),
    ("roundup", "This week's roundup"),
    ("clear", "Clear this chat now"),
    ("help", "What I can do"),
]


def gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"


class Telegram:
    def __init__(self, ctx, client: httpx.AsyncClient | None = None):
        self.ctx = ctx
        self.client = client or httpx.AsyncClient(timeout=40)
        self._menu_for = ""              # the token whose command menu is already set

    def token(self) -> str:
        return self.ctx.vault.get("telegram_token")

    def chats(self) -> list[str]:
        return [str(c) for c in prefs.get(self.ctx.db, "telegram_chats")]

    def configured(self) -> bool:
        return bool(self.token())

    async def api(self, method: str, **payload):
        token = self.token()
        if not token:
            return None
        try:
            r = await self.client.post(API.format(token=token, method=method), json=payload)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Telegram %s failed: %s", method, e.__class__.__name__)
            return None
        if not data.get("ok"):
            log.warning("Telegram %s: %s", method, data.get("description", "error"))
            return None
        return data.get("result")

    async def me(self):
        return await self.api("getMe")

    @staticmethod
    def _markup(buttons):
        if not buttons:
            return None
        def button(t, d):
            return {"text": t, "url": d} if d.startswith(("http://", "https://")) else {"text": t, "callback_data": d}
        return {"inline_keyboard": [[button(t, d) for t, d in row] for row in buttons if row]}

    async def send(self, chat: str, text: str, buttons=None, job_id: int | None = None):
        payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = self._markup(buttons)
        sent = await self.api("sendMessage", **payload)
        if isinstance(sent, dict):
            self.note(chat, sent.get("message_id"), job_id)
        return sent

    async def edit(self, chat: str, message_id: int, text: str, buttons=None):
        payload = {"chat_id": chat, "message_id": message_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True, "reply_markup": self._markup(buttons) or {"inline_keyboard": []}}
        return await self.api("editMessageText", **payload)

    async def broadcast(self, text: str, buttons=None, job_id: int | None = None):
        for chat in self.chats():
            await self.send(chat, text, buttons, job_id)

    # ---- nightly clear ------------------------------------------------------------------------

    def note(self, chat: str, message_id, job_id: int | None = None) -> None:
        """Remember a message in one of the bot's chats so the clear can delete it. A card edited into a
        different job's card takes that job; the time stays the first one seen."""
        if not message_id or str(chat) not in self.chats():
            return
        self.ctx.db.run("INSERT INTO tg_messages (chat, message_id, job_id, sent_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(chat, message_id) DO UPDATE SET job_id = coalesce(excluded.job_id, tg_messages.job_id)",
                        (str(chat), int(message_id), job_id, int(time.time())))

    def needs_you(self, job_id: int | None) -> bool:
        """A card worth keeping through the clear: its job is a draft, a captcha to solve, has a question, or an alert."""
        if not job_id:
            return False
        job = self.ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            return False
        if job["status"] in ("draft", "needs_captcha") or jobs.open_question(dict(job)):
            return True
        return bool(self.ctx.db.one("SELECT 1 FROM alerts WHERE job_id = ? AND resolved_at IS NULL", (job_id,)))

    async def clear(self, chats: list[str] | None = None) -> dict:
        """Delete what's in the bot's chats from the last 48 hours. Pinned messages stay, and so do cards that still
        need an answer (Settings → Telegram). Returns {'deleted': n, 'kept': n, 'failed': n}."""
        db = self.ctx.db
        db.run("DELETE FROM tg_messages WHERE sent_at < ?", (int(time.time()) - 47 * 3600,))  # too old to delete
        keep_open = prefs.get(db, "chat_clear_keep_open")
        out = {"deleted": 0, "kept": 0, "failed": 0}
        for chat in chats or self.chats():
            rows = db.query("SELECT message_id, job_id, pinned FROM tg_messages WHERE chat = ? ORDER BY message_id", (chat,))
            if not rows:
                continue
            info = await self.api("getChat", chat_id=chat) or {}
            pinned = (info.get("pinned_message") or {}).get("message_id")
            keep = {r["message_id"] for r in rows if r["pinned"] or r["message_id"] == pinned
                    or (keep_open and self.needs_you(r["job_id"]))}
            ids = [r["message_id"] for r in rows if r["message_id"] not in keep]
            for i in range(0, len(ids), 100):          # deleteMessages takes up to 100 at a time
                part = ids[i:i + 100]
                if await self.api("deleteMessages", chat_id=chat, message_ids=part) is not None:
                    out["deleted"] += len(part)
                else:                                    # one at a time, so one stuck message doesn't stop the rest
                    for mid in part:
                        ok = await self.api("deleteMessage", chat_id=chat, message_id=mid) is not None
                        out["deleted" if ok else "failed"] += 1
            # Deleted or not deletable (gone already, or not the bot's to delete in a group): either way, done with it.
            if ids:
                db.run(f"DELETE FROM tg_messages WHERE chat = ? AND message_id IN ({','.join('?' * len(ids))})", (chat, *ids))
            out["kept"] += len(keep)
        if out["deleted"] or out["failed"]:
            log.info("Cleared Telegram: %d deleted, %d kept, %d couldn't be deleted", out["deleted"], out["kept"], out["failed"])
        return out

    # ---- cards --------------------------------------------------------------------------------

    def job_card(self, job: dict) -> tuple[str, list]:
        e = html.escape
        icon = "🎬" if job["kind"] == "movie" else "📺"
        title = f"{job['title']} ({job['year']})" if job.get("year") else job["title"]
        lib = job.get("library") or {}
        lines = [f"{icon} <b>{e(title)}</b>", f"{'Movie' if job['kind'] == 'movie' else 'Series'} · {gb(job['size'])} · "
                 f"{len(job['links'])} link{'s' if len(job['links']) != 1 else ''}"]
        if lib:
            lines.append(f"Library: <b>{e(lib['name'])}</b>")
            folder = job["match_folder"] if job["match_state"] in ("exact", "confirmed") else job["folder_name"]
            lines.append(f"Folder: {e(folder)}")
        else:
            lines.append("Library: <i>none yet, add one in Settings</i>")
        if "Documentary" in job.get("genres", []):
            lines.append("TMDB lists it as a documentary.")
        if job.get("note") and (job["status"] == "draft" or job.get("recheck")):
            lines.append(f"\n⚠️ {e(job['note'])}")
        if job.get("show_unknown") and job["status"] != "draft":
            lines.append("\nLinkarr can't tell which show this is.")
            lines.append("Open it in Linkarr and use Match to show.")
        if job["match_state"] == "close":
            this = f"{e(job['title'])} ({job['year']})" if job.get("year") else e(job["title"])
            lines.append(f"\nLooks like <b>{e(job['match_folder'])}</b> in your library. This link is {this}. Same one?")
        if job["kind"] == "movie" and job["conflict"] == "exists" and not job["conflict_choice"]:
            lines.append(f"\n{e(job.get('quality_text') or 'You already have this.')} Replace, keep both, or skip?")
        lines.append(f"\nStatus: {job['status'].replace('_', ' ')}")
        jid = job["id"]
        rows = []
        libs = [x for x in jobs.libraries(self.ctx.db)]
        if job["status"] in ("draft", "queued", "downloading", "waiting", "needs_answer") and libs:
            rows += [[("✓ " + x["name"] if lib and x["id"] == lib["id"] else x["name"], f"lib:{jid}:{x['id']}") for x in libs[i:i + 2]]
                     for i in range(0, len(libs), 2)]
        if job.get("recheck"):
            rows.append([("✓ Looks right, file it", f"ok:{jid}")])
        if job["match_state"] == "close":
            rows.append([("✓ Yes, same", f"mu:{jid}"), ("New folder", f"mn:{jid}")])
        if job["kind"] == "movie" and job["conflict"] == "exists" and not job["conflict_choice"]:
            rows.append([("Replace", f"cr:{jid}"), ("Keep both", f"ck:{jid}"), ("Skip", f"cs:{jid}")])
        if job["status"] == "draft":
            rows.append([("▶ Start", f"go:{jid}"), ("🌙 Tonight", f"night:{jid}"), ("✖ Cancel", f"x:{jid}")])
        return "\n".join(lines), rows

    @staticmethod
    def captcha_card(job: dict, reminder: bool = False) -> tuple[str, list]:
        """The captcha link, what to do with it, and buttons to open it or drop it. One fact per line."""
        e = html.escape
        url = job["hold_url"]
        head = "⏰ <b>Still needs a captcha</b>" if reminder else "🔒 <b>Needs captcha</b>"
        lines = [head, f"<code>{e(url)}</code>"]
        if reminder:
            lines.append("Waiting since yesterday.")
        lines += ["", "Linkarr can't solve captchas for you.", "1. Open the link and solve the captcha.",
                  "2. Reply to this message with the MEGA link."]
        return "\n".join(lines), [[("🔓 Open link", url)], [("✖ Drop it", f"cx:{job['id']}")]]

    async def send_captcha(self, chat: str, job: dict, reminder: bool = False):
        text, rows = self.captcha_card(job, reminder)
        sent = await self.send(chat, text, rows, job["id"])
        if sent:
            jobs.remember_captcha_message(self.ctx.db, chat, sent.get("message_id"), job["id"])
        return sent

    async def broadcast_captcha(self, job: dict, reminder: bool = False) -> None:
        for chat in self.chats():
            await self.send_captcha(chat, job, reminder)

    async def on_captcha_reply(self, chat: str, msg: dict, job_id: int, text: str) -> None:
        """A reply to a captcha message: the MEGA link the page gave you."""
        e = html.escape
        hold = jobs.job_view(self.ctx.db, job_id)
        try:
            added = await jobs.solve_captcha(self.ctx, job_id, text)
        except jobs.JobError as err:
            await self.send(chat, f"⚠️ {e(str(err))}")
            return
        first = msg.get("reply_to_message", {}).get("message_id")
        if first and hold:
            await self.edit(chat, first, f"🔓 <b>Captcha done</b>\n<code>{e(hold['hold_url'])}</code>")
        for job in added:
            text_, rows = self.job_card(job)
            await self.send(chat, text_, rows, job["id"])

    async def on_captcha_links(self, chat: str, urls: list[str]) -> None:
        if not prefs.get(self.ctx.db, "captcha_enabled"):
            await self.send(chat, "🔒 That's a captcha link.\nCaptcha links are off.\nTurn them on in Linkarr → Settings → Extras.")
            return
        for url in urls[:5]:
            try:
                job = jobs.hold_captcha(self.ctx, url)
            except jobs.JobError as err:
                await self.send(chat, f"⚠️ {html.escape(str(err))}")
                continue
            await self.send_captcha(chat, job)

    def missing_text(self) -> str:
        rows = []
        for r in self.ctx.db.query("SELECT name, missing FROM shows WHERE watching=1 AND missing NOT IN ('', 'null') ORDER BY name"):
            text = tracker.describe(json.loads(r["missing"]))
            if text:
                rows.append(f"• <b>{html.escape(r['name'])}</b>: {html.escape(text.removeprefix("Missing "))}")
        if not rows:
            return "No gaps in the shows you track."
        more = f"\n…and {len(rows) - 40} more in the Tracker." if len(rows) > 40 else ""
        return "Missing episodes:\n" + "\n".join(rows[:40]) + more

    def site_reply(self) -> tuple[str, list]:
        site = prefs.site(self.ctx.db)
        if not site:
            return "No download site set yet.\nAdd it in Settings → Connections.", []
        e = html.escape
        return f"🔗 <b>{e(site['name'])}</b>\n{e(site['url'])}", [[(f"Open {site['name']}", site["url"])]]

    @staticmethod
    def help_text() -> str:
        return ("Send me a MEGA link (or a Base64 code with one inside) and I'll set it up.\n"
                "A captcha link comes back to you to solve. Reply with the MEGA link it gives you.\n\n"
                + "\n".join(f"/{c} · {d}" for c, d in COMMANDS))

    async def set_menu(self) -> None:
        """Fill Telegram's "/" menu once per bot token."""
        token = self.token()
        if token == self._menu_for:
            return
        if await self.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS]) is not None:
            self._menu_for = token

    # ---- polling ------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.configured():
                await _sleep(stop, 30)
                continue
            await self.set_menu()
            offset = int(self.ctx.db.get_setting("telegram_offset", 0) or 0)
            updates = await self.api("getUpdates", offset=offset, timeout=25, allowed_updates=["message", "callback_query"])
            if updates is None:
                await _sleep(stop, 15)
                continue
            for u in updates:
                self.ctx.db.set_setting("telegram_offset", u["update_id"] + 1)
                try:
                    if "callback_query" in u:
                        await self.on_button(u["callback_query"])
                    elif "message" in u:
                        await self.on_message(u["message"])
                except Exception:
                    log.exception("Telegram update failed")

    async def on_message(self, msg: dict) -> None:
        chat = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text") or msg.get("caption") or ""
        if chat not in self.chats():
            if text.startswith("/start"):
                await self.send(chat, f"This chat's id is <code>{html.escape(chat)}</code>. Add it in Linkarr → Settings → "
                                      "Telegram to use this bot.")
            return
        self.note(chat, msg.get("message_id"))           # your messages are cleared too
        if "pinned_message" in msg:                       # "You pinned …": the pinned one stays through the clear
            pin = msg["pinned_message"].get("message_id")
            if pin:
                self.note(chat, pin)
                self.ctx.db.run("UPDATE tg_messages SET pinned = 1 WHERE chat = ? AND message_id = ?", (chat, pin))
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        replied = msg.get("reply_to_message") or {}
        hold_id = jobs.captcha_job_for(self.ctx.db, chat, replied.get("message_id")) if not command else None
        if hold_id and self.ctx.db.one("SELECT 1 FROM jobs WHERE id = ? AND status = 'needs_captcha'", (hold_id,)):
            await self.on_captcha_reply(chat, msg, hold_id, text)
            return
        if command in ("/site", "/link"):
            await self.send(chat, *self.site_reply())
            return
        if command == "/clear":
            await self.clear([chat])
            return
        if command in ("/help", "/start"):
            await self.send(chat, self.help_text())
            return
        if text.startswith("/status"):
            await self.send(chat, self.status_text())
            return
        if text.startswith("/startall"):
            r = jobs.start_all(self.ctx)
            extra = f" {r['skipped']} still need a library." if r["skipped"] else ""
            await self.send(chat, f"Started {r['started']}.{extra}" if r["started"] else "Nothing waiting to start." + extra)
            return
        if text.startswith("/roundup"):
            await self.send(chat, roundup.text(self.ctx.db, self.ctx.status.get("mega_quota")))
            return
        if text.startswith("/missing"):
            await self.send(chat, self.missing_text())
            return
        if command in ("/monitor", "/watch"):  # /watch still works, from 0.7.0
            await self.on_watch(chat, text)
            return
        hosts = prefs.captcha_hosts(self.ctx.db)
        links = find_links(text)
        caps = captcha.find(text, hosts)
        if not links and not caps:
            found = decode.unwrap(text, hosts=hosts)
            if found["links"]:
                await self.offer_decoded(chat, found)
                return
            if not found["captcha"]:
                await self.send(chat, self.help_text())
                return
            caps = found["captcha"]
        if caps:
            await self.on_captcha_links(chat, caps)
        for url in links:
            try:
                job = await jobs.add_link(self.ctx, url)
            except jobs.JobError as e:
                await self.send(chat, f"⚠️ {html.escape(str(e))}")
                continue
            text_, rows = self.job_card(job)
            await self.send(chat, text_, rows, job["id"])

    def watched_text(self) -> str:
        rows = watch.all_watches(self.ctx.db)
        if not rows:
            return "No monitored links yet.\nSend /monitor and a MEGA folder link to start."
        label = {"active": "monitoring", "stopped": "stopped", "dead": "link gone"}
        out = ["👁 <b>Monitored links</b>", ""]
        out += [f"• {html.escape(w['title'])}: {label.get(w['status'], w['status'])}" for w in rows[:30]]
        return "\n".join(out)

    async def on_watch(self, chat: str, text: str) -> None:
        """/watch LINK: add a show's folder link (or use the one already added) and watch it for new episodes.
        /watch on its own lists what's watched."""
        e = html.escape
        if not prefs.get(self.ctx.db, "watch_enabled"):
            await self.send(chat, "Monitored links are off.\nTurn them on in Linkarr → Settings → Extras.")
            return
        links = find_links(text)
        if not links:
            await self.send(chat, self.watched_text())
            return
        url = links[0]
        if not is_folder(url):
            await self.send(chat, "Only folder links can be monitored.\nA link to one file never changes.")
            return
        row = self.ctx.db.one("SELECT l.id, l.job_id FROM links l JOIN jobs j ON j.id = l.job_id WHERE l.url = ? AND j.status != 'canceled' "
                              "ORDER BY l.id DESC", (url,))
        job = None
        if row is None:
            try:
                job = await jobs.add_link(self.ctx, url, kind_hint="series")
            except jobs.JobError as err:
                await self.send(chat, f"⚠️ {e(str(err))}")
                return
            link_id = job["links"][-1]["id"]
        else:
            link_id = row["id"]
        try:
            w = watch.add(self.ctx, link_id)
        except watch.WatchError as err:
            await self.send(chat, f"⚠️ {e(str(err))}")
            return
        every = int(prefs.get(self.ctx.db, "watch_every"))
        often = "Every day" if every == 24 else "Every 2 days" if every == 48 else f"Every {every} hours"
        await self.send(chat, "\n".join([f"👁 <b>Monitoring {e(w['title'])}</b>", f"Checks: {often.lower()}",
                                         "New episodes download by themselves."]))
        if job and job["status"] == "draft":
            text_, rows = self.job_card(job)
            await self.send(chat, text_, rows, job["id"])

    async def offer_decoded(self, chat: str, found: dict) -> None:
        """A Base64 code turned into a MEGA link: ask before adding it."""
        n = len(found["layers"])
        rows, shown = [], []
        for url in found["links"][:3]:
            pid = secrets.token_hex(4)
            self.ctx.db.set_setting("tg_pending." + pid, url)
            shown.append(f"<code>{html.escape(url.split('#')[0])}#…</code>")
            rows.append([("➕ Add it" if len(found["links"]) == 1 else f"➕ Add {len(rows) + 1}", f"add:{pid}")])
        rows.append([("No thanks", "add:no")])
        await self.send(chat, f"Decoded {n} time{'s' if n != 1 else ''} and found a MEGA link:\n" + "\n".join(shown)
                        + "\n\nAdd it?", rows)

    def status_text(self) -> str:
        rows = self.ctx.db.query("SELECT id FROM jobs WHERE status NOT IN ('done','canceled','draft') ORDER BY created_at LIMIT 10")
        if not rows:
            return "Nothing running right now."
        out = []
        for r in rows:
            j = jobs.job_view(self.ctx.db, r["id"])
            pct = int(100 * j["done_bytes"] / j["size"]) if j["size"] else 0
            out.append(f"• <b>{html.escape(j['title'])}</b>: {j['status'].replace('_', ' ')} {pct}%")
        return "\n".join(out)

    async def on_button(self, q: dict) -> None:
        chat = str(q.get("message", {}).get("chat", {}).get("id", ""))
        if chat not in self.chats():
            await self.api("answerCallbackQuery", callback_query_id=q["id"])
            return
        data = q.get("data", "")
        parts = data.split(":")
        note = ""
        job_id = None
        if parts[0] == "add":
            await self.on_add_button(q, chat, parts[1] if len(parts) > 1 else "no")
            return
        if parts[0] == "cx":
            await self.on_drop_captcha(q, chat, parts[1] if len(parts) > 1 else "")
            return
        try:
            act, arg = parts[0], int(parts[1])
            if act == "lib":
                job_id = arg
                await jobs.update_job(self.ctx, arg, {"library_id": int(parts[2])})
            elif act in ("go", "night"):
                job_id = arg
                jobs.start_job(self.ctx, arg, "tonight" if act == "night" else "now")
                note = "Starting." if act == "go" else "It'll start tonight."
            elif act == "x":
                job_id = arg
                jobs.cancel_job(self.ctx, arg)
                note = "Canceled."
            elif act == "ok":
                job_id = arg
                await jobs.update_job(self.ctx, arg, {"recheck": 0})
                note = "Thanks. Filing it."
            elif act in ("mu", "mn"):
                job_id = arg
                jobs.answer_match(self.ctx, arg, "use" if act == "mu" else "new")
            elif act in ("cr", "ck", "cs"):
                job_id = arg
                choice = {"cr": "replace", "ck": "keep", "cs": "skip"}[act]
                await jobs.update_job(self.ctx, arg, {"conflict_choice": choice})
                if choice == "skip":
                    jobs.cancel_job(self.ctx, arg)
            elif act == "fix":
                mode = jobs.retry_link(self.ctx, arg)
                note = "Unpacking it again." if mode == "unpack" else "Downloading it again."
            elif act == "skip":
                jobs.skip_link(self.ctx, arg)
                note = "Skipped."
        except (ValueError, IndexError, jobs.JobError) as e:
            note = str(e) or "That didn't work."
        await self.api("answerCallbackQuery", callback_query_id=q["id"], text=note[:190])
        msg = q.get("message", {})
        if job_id and msg.get("message_id"):
            job = jobs.job_view(self.ctx.db, job_id)
            if job:
                text_, rows = self.job_card(job)
                await self.edit(chat, msg["message_id"], text_, rows)
                self.note(chat, msg["message_id"], job_id)
        elif note and msg.get("message_id"):
            await self.edit(chat, msg["message_id"], html.escape(msg.get("text", "")) + f"\n\n<i>{html.escape(note)}</i>")

    async def on_add_button(self, q: dict, chat: str, pid: str) -> None:
        msg = q.get("message", {})
        url = self.ctx.db.get_setting("tg_pending." + pid) if pid != "no" else None
        await self.api("answerCallbackQuery", callback_query_id=q["id"])
        if not url:
            if msg.get("message_id"):
                await self.edit(chat, msg["message_id"], "Okay, not added." if pid == "no" else "That one's gone. Send it again.")
            return
        self.ctx.db.run("DELETE FROM settings WHERE key = ?", ("tg_pending." + pid,))
        try:
            job = await jobs.add_link(self.ctx, url)
        except jobs.JobError as e:
            await self.send(chat, f"⚠️ {html.escape(str(e))}")
            return
        text_, rows = self.job_card(job)
        if msg.get("message_id"):
            await self.edit(chat, msg["message_id"], text_, rows)
            self.note(chat, msg["message_id"], job["id"])
        else:
            await self.send(chat, text_, rows, job["id"])

    async def on_drop_captcha(self, q: dict, chat: str, arg: str) -> None:
        msg = q.get("message", {})
        job = self.ctx.db.one("SELECT id, status FROM jobs WHERE id = ?", (int(arg),)) if arg.isdigit() else None
        await self.api("answerCallbackQuery", callback_query_id=q["id"])
        if job and job["status"] == "needs_captcha":
            jobs.delete_job(self.ctx, job["id"])
            note = "🗑 <b>Dropped</b>\nNothing was downloaded."
        else:
            note = "That captcha link isn't waiting any more."
        if msg.get("message_id"):
            await self.edit(chat, msg["message_id"], note)

    async def close(self):
        await self.client.aclose()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
