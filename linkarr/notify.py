"""Where the worker's news goes: Telegram (if set up) and a Plex rescan when something lands."""
import html
import logging

from . import jobs, prefs

log = logging.getLogger("linkarr.notify")


class Notifier:
    def __init__(self, ctx, telegram, plex, servers=()):
        self.ctx = ctx
        self.telegram = telegram
        self.plex = plex
        self.servers = list(servers)   # Jellyfin and Emby (mediaserver.MediaServer)

    async def refresh_servers(self) -> list[str]:
        """Asks Jellyfin and Emby (the ones set up) to scan. Returns the names that said yes."""
        done = []
        for s in self.servers:
            if s.configured() and await s.refresh():
                done.append(s.label)
        return done

    async def send(self, text: str, buttons=None) -> None:
        if self.telegram.configured():
            await self.telegram.broadcast(html.escape(text), buttons)

    async def send_html(self, text: str, buttons=None) -> None:
        if self.telegram.configured():
            await self.telegram.broadcast(text, buttons)

    async def captcha(self, job_id: int, reminder: bool = False) -> None:
        """A captcha link to solve (or, a day later, a reminder): sent to every chat, each one replyable."""
        job = jobs.job_view(self.ctx.db, job_id)
        if job and job["status"] == "needs_captcha" and self.telegram.configured():
            await self.telegram.broadcast_captcha(job, reminder)

    async def alert(self, job_id, link_id, kind: str) -> None:
        if not self.telegram.configured():
            return
        job = jobs.job_view(self.ctx.db, job_id) if job_id else None
        if kind in ("match", "conflict", "inbox", "recheck", "show") and job:
            text, rows = self.telegram.job_card(job)
            head = {"match": "❓ Same show?", "conflict": "❓ Already in your library", "inbox": "📥 New in the Inbox",
                    "recheck": "❓ Check the title", "show": "❓ Which show is this?"}[kind]
            await self.telegram.broadcast(f"<b>{head}</b>\n\n" + text, rows, job["id"])
            return
        link = self.ctx.db.one("SELECT * FROM links WHERE id = ?", (link_id,)) if link_id else None
        alert = self.ctx.db.one("SELECT * FROM alerts WHERE resolved_at IS NULL AND coalesce(link_id,0)=? AND coalesce(job_id,0)=? "
                                "ORDER BY id DESC", (link_id or 0, job_id or 0))
        title = alert["title"] if alert else "Something needs you"
        body = alert["message"] if alert else ""
        rows = None
        if link is not None and kind in ("integrity", "dead", "error", "unpack", "novideo"):
            rows = [[("🔧 Fix it", f"fix:{link['id']}"), ("Skip file", f"skip:{link['id']}")]]
        await self.telegram.broadcast(f"⚠️ <b>{html.escape(title)}</b>\n{html.escape(body)}", rows, job_id)

    async def refresh_plex(self, lib: dict, target: str) -> bool:
        """After a move: scan the Plex library, or with Settings → Extras → Plex refresh, only the folder that changed."""
        section = (lib or {}).get("plex_section", "")
        if not section:
            return False
        if prefs.get(self.ctx.db, "plex_partial") and target and self.plex.configured():
            from pathlib import Path
            try:
                base = jobs.lib_dir(self.ctx.cfg, lib)
            except jobs.JobError:
                return await self.plex.refresh(section)
            return bool(await self.plex.refresh_folder(section, base, Path(target)))
        return await self.plex.refresh(section)

    async def job_done(self, job_id: int, n_files: int, target: str, skipped=(), upgraded=()) -> None:
        job = jobs.job_view(self.ctx.db, job_id)
        if not job:
            return
        lib = job.get("library") or {}
        refreshed = await self.refresh_plex(lib, target) if lib and n_files and self.plex else False
        updating = (["Plex"] if refreshed else []) + (await self.refresh_servers() if n_files else [])
        if prefs.get(self.ctx.db, "notify_done") and self.telegram.configured():
            where = lib.get("name", "your library")
            title = f"{job['title']} ({job['year']})" if job.get("year") else job["title"]
            folder = target.rstrip("/").rsplit("/", 1)[-1]
            if not n_files:
                lines = [f"⏭ <b>{html.escape(title)}</b>", "You already had everything in it.", "Nothing was moved."]
            else:
                lines = [f"✅ <b>{html.escape(title)}</b> is in {html.escape(where)}: moved {n_files} "
                         f"file{'s' if n_files != 1 else ''} to {html.escape(folder)}." + _updating(updating)]
            if upgraded:
                lines.append(f"Upgraded {_eps(upgraded)} to a better copy.")
            if skipped:
                lines.append(f"Skipped {_eps(skipped)} you already have.")
            await self.telegram.broadcast("\n".join(lines))


def _updating(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f" {names[0]} is updating."
    return f" {', '.join(names[:-1])} and {names[-1]} are updating."


def _eps(labels) -> str:
    labels = list(labels)
    if len(labels) <= 3:
        return ", ".join(labels)
    return f"{len(labels)} episodes"
