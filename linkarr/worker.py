"""The background worker: downloads, unpacks, checks and moves jobs; watches the Inbox; checks
show status once a day; purges the recycle folder and backs up the database nightly.

Everything it does is written to the database first, so a restart (container update, Unraid
reboot) picks up where it stopped: interrupted downloads start again, finished ones aren't lost.
"""
import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

import httpx

from . import files, jobs, movietracker, naming, prefs, roundup, tracker, watch
from .db import now
from .jobs import Ctx, add_alert, resolve_alerts
from .mega import MegaError
from .parse import is_sub, is_video, parse_name

log = logging.getLogger("linkarr.worker")
RETRY_GENERIC = 3       # silent retries for a generic failure before asking you
RETRY_INTEGRITY = 1     # one silent re-download when MEGA's file check fails ('Fix issues')
QUOTA_WAIT = 30 * 60


class Worker:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.downloads: dict[int, asyncio.Task] = {}
        self.processing: dict[int, asyncio.Task] = {}
        self.cancel: dict[int, asyncio.Event] = {}
        self.moving = False
        self.last_inbox = 0.0
        self.last_shows = 0.0
        self.last_quota = 0.0
        self.last_plex = 0.0
        self.last_minute = 0.0
        self.poster_tried: set[int] = set()  # shows TMDB had no poster for, not asked again until a restart
        self.plex_waits = (45, 90, 180, 300)  # seconds between looks for a new download in Plex
        self.side_tasks: set = set()  # subtitle lookups running after a move
        self.speed: dict[int, tuple[float, int, float]] = {}
        ctx.status["cancel"] = self.cancel
        ctx.status["speed"] = {}

    # ---- lifecycle ---------------------------------------------------------------------------

    def recover(self) -> None:
        """After a restart: unfinished downloads go back in the queue; nothing is lost."""
        n = self.db.run("UPDATE links SET status='queued' WHERE status='downloading'").rowcount
        self.db.run("UPDATE jobs SET status='waiting' WHERE status IN ('moving','processing')")
        if n:
            log.info("Restarting %d interrupted download(s).", n)

    async def run(self, stop: asyncio.Event) -> None:
        self.recover()
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # keep going whatever happens; the log has the details
                log.exception("Worker tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        for ev in self.cancel.values():
            ev.set()
        pending = list(self.downloads.values()) + list(self.processing.values())
        if pending:
            await asyncio.wait(pending, timeout=8)
        self.db.run("UPDATE links SET status='queued' WHERE status='downloading'")

    async def tick(self) -> None:
        self._start_downloads()
        self._start_processing()
        await self._advance_jobs()
        t = time.time()
        if t - self.last_inbox > 30:
            self.last_inbox = t
            await self.scan_inbox()
        if t - self.last_quota > 900:
            self.last_quota = t
            await self.refresh_quota()
        if t - self.last_minute > 60:
            self.last_minute = t
            await self.check_watches()
            await self.chat_clear()
            await self.morning_summary()
            await self.captcha_reminders()
            await self.fill_folder_posters()
        if t - self.last_plex > 6 * 3600:
            self.last_plex = t
            await self.refresh_plex_index()
        if t - self.last_shows > 3600:
            self.last_shows = t
            await self.check_shows()
            await self.fill_posters()
            await self.check_movies()
            await asyncio.to_thread(self.maintenance)
            await self.weekly_roundup()

    # ---- MEGA transfer quota -------------------------------------------------------------------

    async def refresh_quota(self) -> None:
        get = getattr(self.ctx.mega, "quota", None)
        if get is None:
            return
        try:
            q = await get()
        except Exception as e:  # a quota check must never stop downloads
            log.info("Quota check failed: %s", e)
            return
        if q:
            q["at"] = now()
        self.ctx.status["mega_quota"] = q

    def _quota_short(self, need: int) -> tuple[bool, int]:
        """Would this download go over what's left of the transfer quota (counting downloads already running)?"""
        q = self.ctx.status.get("mega_quota")
        if not need or not q or not q.get("max") or not prefs.get(self.db, "quota_guard"):
            return False, 0
        running = self.db.one("SELECT coalesce(sum(max(size - bytes_done, 0)), 0) AS n FROM links WHERE status = 'downloading'")["n"]
        left = q["max"] - q["used"] - running
        return left < need, max(0, left)

    # ---- downloads ---------------------------------------------------------------------------

    def _start_downloads(self) -> None:
        self.downloads = {k: v for k, v in self.downloads.items() if not v.done()}
        room = int(prefs.get(self.db, "max_parallel")) - len(self.downloads)
        if room <= 0:
            return
        rows = self.db.query(
            "SELECT l.* FROM links l JOIN jobs j ON j.id = l.job_id "
            "WHERE l.status = 'queued' AND l.not_before <= ? AND j.status IN ('queued','downloading','waiting') "
            "ORDER BY j.created_at, l.position, l.id LIMIT ?", (now(), room + len(self.downloads)))
        for link in rows:
            if link["id"] in self.downloads or room <= 0:
                continue
            if jobs.window_wait(self.db, max(0, link["size"] - link["bytes_done"])):
                continue  # big download, outside the download window (Settings → Downloads)
            room -= 1
            self.downloads[link["id"]] = asyncio.create_task(self.download(dict(link)))

    @staticmethod
    def _unpack_room(link: dict) -> int:
        """Extra room an archive needs while it unpacks: the archive and what comes out of it sit side by side."""
        return sum(int(f.get("size", 0)) for f in json.loads(link.get("files") or "[]")
                   if f.get("selected", True) and files.is_archive(f["path"]))

    def _space_short(self, link: dict, staging: Path, need: int, free: int) -> tuple[str, int, int] | None:
        """Is there room for this download, counting the downloads already running and room to unpack?
        Returns (which drive, bytes wanted, bytes free) when there isn't."""
        spare = 1024**3
        others = [dict(r) for r in self.db.query("SELECT * FROM links WHERE status = 'downloading' AND id != ?", (link["id"],))]
        running = sum(max(0, r["size"] - r["bytes_done"]) + self._unpack_room(r) for r in others)
        want = need + self._unpack_room(link) + running + spare
        if free < want:
            return "download drive", want, free
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", (link["job_id"],))
        try:
            target = jobs.target_path(self.ctx, dict(job)) if job else None
        except jobs.JobError:
            target = None
        if not target:
            return None
        probe = target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            if probe.stat().st_dev == staging.stat().st_dev:
                return None  # same drive, already counted
            lib_free = shutil.disk_usage(probe).free
        except OSError:
            return None
        lib_want = link["size"] + spare
        return ("library drive", lib_want, lib_free) if lib_free < lib_want else None

    async def download(self, link: dict) -> None:
        lid, job_id = link["id"], link["job_id"]
        staging = jobs.work_dir(self.ctx) / "links" / str(lid)
        need = max(0, link["size"] - files.tree_size(staging) if staging.exists() else link["size"])
        try:
            staging.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(staging).free
        except OSError as e:
            self._link_failed(link, "error", f"Can't write to the work folder: {e}")
            return
        if need:
            short = self._space_short(link, staging, need, free)
            if short:
                where, want, have = short
                self.db.run("UPDATE links SET not_before=? WHERE id=?", (now() + 1800, lid))
                if add_alert(self.db, "space", "Not enough free space",
                             f"{link['name']} needs {want / 1024**3:.1f} GB on the {where} and it has "
                             f"{have / 1024**3:.1f} GB free. It will try again every 30 minutes.", job_id, lid):
                    await self._notify_alert(job_id, lid, "space")
                return
        short, left = self._quota_short(need)
        if short:
            self.db.run("UPDATE links SET not_before=? WHERE id=?", (now() + 3600, lid))
            self.last_quota = 0.0  # look again on the next tick
            if add_alert(self.db, "quota", "Waiting for MEGA transfer quota",
                         f"{link['name']} needs {need / 1024**3:.1f} GB and your MEGA account has {left / 1024**3:.1f} GB of "
                         "transfer left. It starts by itself once there's room; Linkarr checks every hour.", job_id, lid):
                await self._notify_alert(job_id, lid, "quota")
            return
        if self._all_had(link):
            self.db.run("UPDATE links SET status='skipped', error='You already have every episode in it.' WHERE id=?", (lid,))
            log.info("Skipped link %d: every episode in it is already in the library", lid)
            if self.ctx.notify and prefs.get(self.db, "notify_done"):
                job = self.db.one("SELECT title FROM jobs WHERE id = ?", (job_id,))
                await self.ctx.notify.send("\n".join([f"⏭ {job['title'] if job else link['name']}",
                                                       "You already have every episode in this link.", "It wasn't downloaded."]))
            return
        resolve_alerts(self.db, link_id=lid, kinds=("space", "quota", "auth"))
        self.db.run("UPDATE links SET status='downloading', attempts=attempts+1, staging=?, started_at=?, error='' WHERE id=?",
                    (str(staging), now(), lid))
        self.db.run("UPDATE jobs SET status='downloading', updated_at=? WHERE id=? AND status IN ('queued','waiting')", (now(), job_id))
        ev = self.cancel[lid] = asyncio.Event()
        last = [0.0]

        def progress(nbytes: int) -> None:
            t = time.time()
            prev = self.speed.get(lid)
            if prev:
                dt = t - prev[0]
                if dt > 0:
                    rate = max(0.0, (nbytes - prev[1]) / dt)
                    self.ctx.status["speed"][lid] = 0.7 * prev[2] + 0.3 * rate if prev[2] else rate
            self.speed[lid] = (t, nbytes, self.ctx.status["speed"].get(lid, 0.0))
            if t - last[0] >= 2:
                last[0] = t
                self.db.run("UPDATE links SET bytes_done=? WHERE id=?", (nbytes, lid))

        log.info("Downloading %s", link["url"])
        try:
            await self.ctx.mega.download(link["url"], staging, progress, ev)
        except MegaError as e:
            if ev.is_set():
                return
            await self._download_error(link, e)
            return
        except asyncio.CancelledError:
            self.db.run("UPDATE links SET status='queued' WHERE id=?", (lid,))
            raise
        finally:
            self.cancel.pop(lid, None)
            self.speed.pop(lid, None)
            self.ctx.status["speed"].pop(lid, None)
        self.last_quota = 0.0  # the allowance just changed
        size = files.tree_size(staging)
        self.db.run("UPDATE links SET status='downloaded', bytes_done=?, finished_at=? WHERE id=?",
                    (max(size, link["size"]), now(), lid))
        log.info("Downloaded link %d (%s)", lid, link["name"])

    def _all_had(self, link: dict) -> bool:
        """Duplicate guard: a show's link whose every episode is already in the show's folder isn't downloaded
        at all (no transfer quota spent). Only before the first try, and only when every video has an episode number."""
        if not prefs.get(self.db, "dup_guard") or link["attempts"] or link["bytes_done"]:
            return False
        job = self.db.one("SELECT * FROM jobs WHERE id = ?", (link["job_id"],))
        if not job or job["kind"] != "series":
            return False
        vids = [f for f in json.loads(link["files"] or "[]") if f.get("selected", True) and is_video(f["path"])]
        if not vids or any(files.is_archive(f["path"]) for f in json.loads(link["files"] or "[]")):
            return False
        try:
            target = jobs.target_path(self.ctx, dict(job))
        except jobs.JobError:
            return False
        if not target or not target.is_dir():
            return False
        have = files.episode_files(target)
        upgrade = prefs.get(self.db, "auto_upgrade")
        for f in vids:
            p = Path(f["path"])
            key = files.episode_of(p)
            if key is None and link["season"] is not None:
                ep = jobs.parse_name(p.name)["episode"]
                key = (link["season"], ep) if ep is not None else None
            if not key or key not in have:
                return False
            if upgrade:
                new = files.RANK.get(jobs.parse_name(p.name)["resolution"] or jobs.parse_name(link["name"])["resolution"] or "", 0)
                if new > max(files.RANK.get(files.file_res(x), 0) for x in have[key]):
                    return False
        return True

    async def _download_error(self, link: dict, e: MegaError) -> None:
        lid, attempts = link["id"], (link["attempts"] or 0) + 1
        log.warning("Download of link %d failed (%s): %s", lid, e.kind, e)
        if e.kind == "quota":
            self.db.run("UPDATE links SET status='queued', not_before=?, error=? WHERE id=?", (now() + QUOTA_WAIT, str(e), lid))
            if add_alert(self.db, "quota", "MEGA transfer quota reached",
                         "Downloads pause and try again every 30 minutes until the quota resets.", link["job_id"], lid):
                await self._notify_alert(link["job_id"], lid, "quota")
            return
        if e.kind == "auth":
            self.db.run("UPDATE links SET status='queued', not_before=?, error=? WHERE id=?", (now() + 600, str(e), lid))
            if add_alert(self.db, "auth", "Sign in to MEGA", "Open Settings and sign in to your MEGA account.", link["job_id"], lid):
                await self._notify_alert(link["job_id"], lid, "auth")
            return
        if e.kind == "integrity" and attempts <= RETRY_INTEGRITY:
            # What 'Fix issues' does in the desktop app: drop the bad copy and fetch it again.
            shutil.rmtree(jobs.work_dir(self.ctx) / "links" / str(lid), ignore_errors=True)
            self.db.run("UPDATE links SET status='queued', bytes_done=0, not_before=? WHERE id=?", (now() + 30, lid))
            return
        if e.kind == "error" and attempts < RETRY_GENERIC:
            self.db.run("UPDATE links SET status='queued', not_before=?, error=? WHERE id=?", (now() + 120 * attempts, str(e), lid))
            return
        self._link_failed(link, e.kind, str(e))
        await self._notify_alert(link["job_id"], lid, e.kind)

    def _link_failed(self, link: dict, kind: str, message: str, title: str = "") -> None:
        self.db.run("UPDATE links SET status='error', error_kind=?, error=? WHERE id=?", (kind, message[:500], link["id"]))
        titles = {
            "integrity": "A download kept failing MEGA's file check",
            "dead": "This link is dead",
            "missing": "MEGAcmd is missing",
            "unpack": "Couldn't unpack it",
            "novideo": "No video in this download",
        }
        add_alert(self.db, kind if kind in titles else "error", title or titles.get(kind, "A download failed"),
                  message if link["name"] and link["name"] in message else f"{link['name']}: {message}",
                  link["job_id"], link["id"])

    # ---- after download: unpack, clean, check --------------------------------------------------

    def _start_processing(self) -> None:
        self.processing = {k: v for k, v in self.processing.items() if not v.done()}
        for link in self.db.query(
                "SELECT l.* FROM links l JOIN jobs j ON j.id = l.job_id WHERE l.status = 'downloaded' AND j.status != 'canceled'"):
            if link["id"] not in self.processing and len(self.processing) < 2:
                self.processing[link["id"]] = asyncio.create_task(self.process(dict(link)))

    async def process(self, link: dict) -> None:
        lid = link["id"]
        root = Path(link["staging"])
        job = self.db.one("SELECT archive_password, title, year FROM jobs WHERE id = ?", (link["job_id"],))
        try:
            if prefs.get(self.db, "unpack"):
                saved = prefs.archive_passwords(self.db, job["title"], job["year"]) if job else []
                problems = await files.unpack(root, (job["archive_password"] or "") if job else "", saved)
                if problems:
                    self._link_failed(link, "unpack", " ".join(problems))
                    await self._notify_alert(link["job_id"], lid, "unpack")
                    return
            for f in json.loads(link["files"] or "[]"):
                if not f.get("selected", True):
                    (root / f["path"]).unlink(missing_ok=True)
            if prefs.get(self.db, "clean_junk"):
                removed = await asyncio.to_thread(files.clean, root)
                if removed:
                    log.info("Removed %d junk file(s) from link %d", len(removed), lid)
            if link.get("hidden"):
                await jobs.recheck_after_download(self.ctx, link, root)
            media = [f for f in files.walk_files(root) if is_video(f.name)]
            if not media and not any(is_sub(f.name) for f in files.walk_files(root)):
                self._link_failed(link, "novideo", "There's no video file left after unpacking and cleaning.")
                await self._notify_alert(link["job_id"], lid, "novideo")
                return
            if prefs.get(self.db, "check_video"):
                for f in media:
                    try:
                        await files.probe(f)
                    except ValueError as e:
                        self._link_failed(link, "integrity", str(e), "This video won't play")
                        await self._notify_alert(link["job_id"], lid, "integrity")
                        return
        except OSError as e:
            self._link_failed(link, "error", f"File problem: {e}", "Couldn't read the downloaded files")
            return
        self.db.run("UPDATE links SET status='ready' WHERE id=?", (lid,))

    # ---- moving into the library --------------------------------------------------------------

    async def _advance_jobs(self) -> None:
        if self.moving:
            return
        for job in self.db.query("SELECT * FROM jobs WHERE status IN ('queued','downloading','waiting','processing') ORDER BY created_at"):
            job = dict(job)
            links = [dict(x) for x in self.db.query("SELECT * FROM links WHERE job_id = ? ORDER BY position", (job["id"],))]
            pending = [x for x in links if x["status"] in ("queued", "downloading", "downloaded")]
            ready = [x for x in links if x["status"] == "ready"]
            errors = [x for x in links if x["status"] == "error"]
            if not ready:
                if not pending:
                    if errors:
                        self.db.run("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                                    (errors[0]["error"][:300], now(), job["id"]))
                    elif all(x["status"] in ("moved", "skipped") for x in links) and any(x["status"] == "moved" for x in links):
                        self.db.run("UPDATE jobs SET status='done', finished_at=coalesce(finished_at, ?) WHERE id=?", (now(), job["id"]))
                    elif all(x["status"] == "skipped" for x in links):
                        self.db.run("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (now(), job["id"]))
                continue
            if (pending or errors) and job["wait_all"]:
                if not any(x["status"] == "downloading" for x in pending) and job["status"] != "waiting":
                    self.db.run("UPDATE jobs SET status='waiting', updated_at=? WHERE id=?", (now(), job["id"]))
                continue
            if job["kind"] == "movie" and job["conflict"] == "exists" and job["conflict_choice"] == "skip":
                # Duplicate guard picked skip (or you did): nothing is filed over what you have.
                jobs.cancel_job(self.ctx, job["id"])
                if self.ctx.notify and prefs.get(self.db, "notify_done"):
                    q = json.loads(job["quality"]) if job.get("quality") else {}
                    title = f"{job['title']} ({job['year']})" if job.get("year") else job["title"]
                    await self.ctx.notify.send("\n".join([f"⏭ Skipped {title}", jobs.quality_text(q) if q else "You already have it."]))
                continue
            question = jobs.open_question(job)
            if question:
                self.db.run("UPDATE jobs SET status='needs_answer', updated_at=? WHERE id=?", (now(), job["id"]))
                if question == "recheck":
                    new = add_alert(self.db, "recheck", "Check this before it's filed", job["note"] or job["title"], job["id"])
                elif question == "show":
                    new = add_alert(self.db, "show", "Which show is this?",
                                    f"{job['title']}: open it and use Match to show.", job["id"])
                elif question == "match":
                    new = add_alert(self.db, "match", "Is this the same show?",
                                    f"{job['title']} looks like “{job['match_folder']}” in your library.", job["id"])
                else:
                    new = add_alert(self.db, "conflict", "You already have this",
                                    f"{job['title']}: {jobs.quality_text(json.loads(job['quality'])) if job.get('quality') else 'already in your library.'} "
                                    "Replace it, keep both, or skip?", job["id"])
                if new:
                    await self._notify_alert(job["id"], None, question)
                continue
            self.moving = True
            try:
                await self.move_job(job, ready)
            finally:
                self.moving = False
            return

    async def move_job(self, job: dict, ready: list[dict]) -> None:
        self.db.run("UPDATE jobs SET status='moving', updated_at=? WHERE id=?", (now(), job["id"]))
        before = await self._plex_count(job)
        try:
            record, target = await asyncio.to_thread(self._do_move, job, ready)
        except (OSError, jobs.JobError) as e:
            log.exception("Move of job %d failed", job["id"])
            self.db.run("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?", (f"Moving failed: {e}"[:300], now(), job["id"]))
            add_alert(self.db, "error", "Moving failed", f"{job['title']}: {e}", job["id"])
            await self._notify_alert(job["id"], None, "error")
            return
        n_files = len(record["moves"])
        summary = f"{job['title']}: {n_files} file{'s' if n_files != 1 else ''} to {target}"
        if record.get("skipped"):
            summary += f" ({len(record['skipped'])} already there, skipped)"
        if record.get("upgraded"):
            summary += f" ({len(record['upgraded'])} upgraded)"
        self.db.run("INSERT INTO history (job_id, ts, summary, moves, recycled, created_dirs) VALUES (?,?,?,?,?,?)",
                    (job["id"], now(), summary, json.dumps(record["moves"]), json.dumps(record["recycled"]),
                     json.dumps(record["created_dirs"])))
        for x in ready:
            self.db.run("UPDATE links SET status='moved' WHERE id=?", (x["id"],))
            if x["staging"]:
                shutil.rmtree(x["staging"], ignore_errors=True)
        still = self.db.one("SELECT count(*) AS n FROM links WHERE job_id=? AND status IN ('queued','downloading','downloaded','ready')",
                            (job["id"],))["n"]
        self.db.run("UPDATE jobs SET status=?, finished_at=?, updated_at=? WHERE id=?",
                    ("downloading" if still else "done", None if still else now(), now(), job["id"]))
        resolve_alerts(self.db, job_id=job["id"])
        log.info("Moved %s", summary)
        if job["kind"] == "series" and job["tmdb_id"]:
            self.db.run("INSERT INTO shows (tmdb_id, name, folder, library_id, poster) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(tmdb_id) DO UPDATE SET folder=excluded.folder, library_id=excluded.library_id, checked_at=0, "
                        "poster=CASE WHEN shows.poster = '' THEN excluded.poster ELSE shows.poster END",
                        (job["tmdb_id"], job["title"], target.name, job["library_id"], job["poster"] or ""))
        if job["kind"] == "series":
            watch.after_move(self.db, job, target)
            if prefs.get(self.db, "gap_check"):
                await self._gap_check(job, target, record)
        if job["kind"] == "movie" and job["tmdb_id"]:
            # It's in your library now, so the Movie Tracker has nothing left to wait for.
            self.db.run("DELETE FROM movies WHERE tmdb_id=?", (job["tmdb_id"],))
        if self.ctx.notify:
            await self.ctx.notify.job_done(job["id"], n_files, str(target), record.get("skipped", []), record.get("upgraded", []))
        n_videos = sum(1 for m in record["moves"] if is_video(m["to"]))
        if before is not None and n_videos:
            self._side(self.check_plex(job["id"], job["library_id"], target.name, before, job["kind"]))
        if self.ctx.subs and self.ctx.subs.enabled():
            videos = [Path(m["to"]) for m in record["moves"] if is_video(m["to"])]
            task = asyncio.create_task(self.ctx.subs.for_files(videos, job["kind"], job["tmdb_id"]))
            self.side_tasks.add(task)
            task.add_done_callback(self.side_tasks.discard)

    def _side(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.side_tasks.add(task)
        task.add_done_callback(self.side_tasks.discard)

    # ---- Plex ---------------------------------------------------------------------------------

    def _plex(self):
        plex = getattr(self.ctx.notify, "plex", None)
        return plex if plex is not None and plex.configured() else None

    async def _plex_items(self, lib: dict) -> dict | None:
        plex = self._plex()
        if not plex or not lib or not lib["plex_section"]:
            return None
        try:
            items = await plex.items(lib["plex_section"])
        except (httpx.HTTPError, ValueError) as e:
            log.info("Couldn't read the %s library from Plex: %s", lib["name"], e.__class__.__name__)
            return None
        index = self.db.get_setting("plex_index", {}) or {}
        index[str(lib["id"])] = items
        self.db.set_setting("plex_index", index)
        return items

    async def refresh_plex_index(self) -> None:
        """What Plex says each library folder is (its TMDB id), so new links match shows by id, not by name.
        Anything marked "Not in Plex yet" that Plex has since picked up flips to "In Plex"."""
        index = {}
        for lib in jobs.libraries(self.db):
            index[lib["id"]] = await self._plex_items(lib)
        for job in self.db.query("SELECT * FROM jobs WHERE plex='missing'"):
            items = index.get(job["library_id"])
            if items is not None and self._plex_has(dict(job), items):
                self._plex_found(job["id"])

    async def _plex_count(self, job: dict) -> int | None:
        """Episodes (or 1 for a movie) Plex has in the job's folder before the move; None when Plex isn't set up."""
        lib = jobs.library(self.db, job["library_id"])
        items = await self._plex_items(lib) if lib else None
        if items is None:
            return None
        target = jobs.target_path(self.ctx, job)
        return (items.get(target.name) or {}).get("count", 0) if target else 0

    def _plex_folder(self, job: dict) -> str:
        try:
            target = jobs.target_path(self.ctx, job)
        except jobs.JobError:
            return ""
        return target.name if target else ""

    def _plex_has(self, job: dict, items: dict, folder: str = "", before: int = 0) -> bool:
        """Plex has the job: its folder shows up (with more episodes than before, for a show), or, for a movie,
        Plex has a title with the same TMDB id, whatever folder Plex thinks it's in."""
        got = items.get(folder or self._plex_folder(job))
        if got and (job["kind"] == "movie" or got["count"] > before):
            return True
        return job["kind"] == "movie" and bool(job.get("tmdb_id")) and any(
            i.get("tmdb") == job["tmdb_id"] for i in items.values())

    def _plex_found(self, job_id: int) -> None:
        self.db.run("UPDATE jobs SET plex='found' WHERE id=?", (job_id,))
        resolve_alerts(self.db, job_id=job_id, kinds=("plex",))

    async def check_plex(self, job_id: int, lib_id: int, folder: str, before: int, kind: str, waits=None) -> None:
        """After a move: look for it in Plex a few times, and tell you if it never shows up."""
        self.db.run("UPDATE jobs SET plex='checking' WHERE id=?", (job_id,))
        lib = jobs.library(self.db, lib_id)
        job = dict(self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {"kind": kind, "tmdb_id": None, "title": folder})
        job["kind"] = kind
        for wait in waits or self.plex_waits:
            await asyncio.sleep(wait)
            items = await self._plex_items(lib)
            if items is not None and self._plex_has(job, items, folder, before):
                self._plex_found(job_id)
                return
        self.db.run("UPDATE jobs SET plex='missing' WHERE id=?", (job_id,))
        name = lib["name"] if lib else "the library"
        if add_alert(self.db, "plex", "Plex hasn't picked this up",
                     f"{job['title']} is in “{folder}” but Plex doesn't show it in {name} yet. Check that the "
                     f"{name} library in Plex points at that folder, or scan it in Plex.", job_id):
            await self._notify_alert(job_id, None, "plex")

    async def recheck_plex(self, job_id: int) -> str:
        """The "Check Plex again" button: look now, and if it isn't there, ask Plex to scan and keep looking."""
        job = jobs.job_view(self.db, job_id)
        lib = job.get("library") if job else None
        if not job or not job.get("moved") or not lib:
            raise jobs.JobError("Nothing from this download has been filed yet.")
        plex = self._plex()
        if not plex or not lib["plex_section"]:
            raise jobs.JobError(f"Plex isn't set up for {lib['name']}. Add the Plex address, token and library id in Settings.")
        folder = self._plex_folder(job)
        items = await self._plex_items(lib)
        if items is None:
            raise jobs.JobError("Couldn't reach Plex. Check the Plex address and token in Settings.")
        if self._plex_has(job, items, folder):
            self._plex_found(job_id)
            return "found"
        await plex.refresh(lib["plex_section"])
        self.db.run("UPDATE jobs SET plex='checking' WHERE id=?", (job_id,))
        self._side(self.check_plex(job_id, lib["id"], folder, 0, job["kind"], waits=(20, 60, 120)))
        return "checking"

    # ---- missing episode check, folder watch, morning summary ---------------------------------

    async def _gap_check(self, job: dict, target: Path, record: dict) -> None:
        """After filing a show: episodes missing between the ones you have in the seasons this download touched.
        Each gap is told once (a watched show filling in week by week doesn't repeat itself)."""
        seasons = {k[0] for k in (files.episode_of(Path(m["to"]), target) for m in record["moves"] if is_video(m["to"])) if k}
        if not seasons:
            return
        have = await asyncio.to_thread(files.episodes_in, target)
        found = tracker.gaps(have, seasons)
        told = self.db.get_setting("gaps_told", {}) or {}
        for s, miss in found.items():
            key = f"{target}|{s}"
            if told.get(key) == miss:
                continue
            told[key] = miss
            text = tracker.gap_message(job["title"], s, miss, have[s]["eps"])
            plain = text.replace("<b>", "").replace("</b>", "").replace("🧩 ", "")
            add_alert(self.db, "gap", f"{job['title']}: missing episodes", plain.split("\n", 1)[1].replace("\n", " "), job["id"])
            if self.ctx.notify:
                await self.ctx.notify.send_html(text)
        for s in seasons - set(found):
            told.pop(f"{target}|{s}", None)
        self.db.set_setting("gaps_told", told)

    async def check_watches(self, only: int | None = None) -> int:
        """Folder watch: look at the watched folders that are due (or one, for Check now)."""
        if not prefs.get(self.db, "watch_enabled"):
            return 0
        rows = [dict(r) for r in self.db.query("SELECT * FROM watches WHERE id=?", (only,))] if only else watch.due(self.db)
        n = 0
        for w in rows:
            try:
                for text in await watch.check(self.ctx, w):
                    n += 1
                    if self.ctx.notify:
                        await self.ctx.notify.send_html(text)
            except Exception:  # one bad folder shouldn't stop the others
                log.exception("Checking the watched folder for %s failed", w["title"])
        return n

    async def captcha_reminders(self) -> int:
        """One nudge for a captcha link that's waited a day. Only one, so it never nags."""
        if not prefs.get(self.db, "captcha_enabled"):
            return 0
        due = jobs.captcha_due(self.db)
        for job in due:
            self.db.run("UPDATE jobs SET reminded_at = ? WHERE id = ?", (now(), job["id"]))
            if self.ctx.notify:
                try:
                    await self.ctx.notify.captcha(job["id"], reminder=True)
                except Exception:
                    log.exception("Captcha reminder failed")
        return len(due)

    async def morning_summary(self, force: bool = False) -> str:
        """Once a day at the time in Settings → Downloads: what came in since the last one (or the last 24 hours)."""
        today = time.strftime("%Y-%m-%d")
        if not force:
            if not prefs.get(self.db, "morning_summary") or self.db.get_setting("last_morning") == today:
                return ""
            if time.strftime("%H:%M") < prefs.get(self.db, "morning_time"):
                return ""
            self.db.set_setting("last_morning", today)
        since = max(int(self.db.get_setting("last_morning_ts", 0) or 0), now() - 24 * 3600)
        text = roundup.morning_text(self.db, since)
        if not force:
            self.db.set_setting("last_morning_ts", now())
        if text and self.ctx.notify:
            await self.ctx.notify.send_html(text)
        return text

    # ---- nightly chat clear -------------------------------------------------------------------

    async def chat_clear(self, at: float | None = None) -> dict | None:
        """Once a night at the time in Settings → Telegram: delete the bot's chat so the day starts clean.
        If Linkarr was off at that time it still runs up to 3 hours late, never in the middle of the day."""
        tg = getattr(self.ctx.notify, "telegram", None)
        if not prefs.get(self.db, "chat_clear") or tg is None or not tg.configured():
            return None
        t = time.time() if at is None else at
        clock = prefs.get(self.db, "chat_clear_time")
        hh, mm = (int(x) for x in clock.split(":"))
        lt = time.localtime(t)
        late = (lt.tm_hour * 60 + lt.tm_min - (hh * 60 + mm)) % 1440      # minutes since today's (or last night's) time
        if late >= 180:
            return None
        night = time.strftime("%Y-%m-%d", time.localtime(t - late * 60)) + " " + clock   # the night it belongs to
        if self.db.get_setting("last_chat_clear") == night:
            return None
        self.db.set_setting("last_chat_clear", night)
        return await tg.clear()

    # ---- weekly roundup -----------------------------------------------------------------------

    async def weekly_roundup(self, force: bool = False) -> str:
        today = time.strftime("%Y-%m-%d")
        if not force:
            if not prefs.get(self.db, "roundup_enabled") or time.strftime("%a").lower()[:3] != prefs.get(self.db, "roundup_day"):
                return ""
            if time.localtime().tm_hour < 10 or self.db.get_setting("last_roundup") == today:
                return ""
            self.db.set_setting("last_roundup", today)
        text = roundup.text(self.db, self.ctx.status.get("mega_quota"), today)
        if self.ctx.notify:
            await self.ctx.notify.send_html(text)
        return text

    def _do_move(self, job: dict, ready: list[dict]):
        lib = jobs.library(self.db, job["library_id"])
        if lib is None:
            raise jobs.JobError("The job's library was deleted.")
        target = jobs.target_path(self.ctx, job)
        media_root = self.ctx.cfg.media_dir
        choice = job["conflict_choice"]
        record = {"moves": [], "recycled": [], "created_dirs": []}
        if job["kind"] == "movie":
            staged = [(f, Path(x["staging"])) for x in ready for f in files.walk_files(Path(x["staging"]))]
            if choice == "replace":
                files.recycle_old_videos(target, media_root, job["id"], record)
            info = None
            if choice == "keep":
                from .parse import parse_name
                info = parse_name(ready[0]["name"])["resolution"] or "copy"
            plan = files.plan_movie(staged, target, target.name, keep_suffix=info or "")
        else:
            # The season is read from the link name again: links added before 0.9.0 cut 'Season 200' to 20.
            seasons = {x["id"]: jobs._season_of(x["name"], json.loads(x["files"] or "[]")) for x in ready}
            staged = [(f, Path(x["staging"]), seasons[x["id"]] if seasons[x["id"]] is not None else x["season"])
                      for x in ready for f in files.walk_files(Path(x["staging"]))]
            plan = files.plan_series(staged, target, job["title"], prefs.get(self.db, "season_pattern"),
                                     prefs.get(self.db, "episode_pattern"), prefs.get(self.db, "rename_episodes"))
            dupes, upgrade = prefs.get(self.db, "dup_guard"), prefs.get(self.db, "auto_upgrade")
            if dupes or upgrade:
                plan = files.guard_episodes(plan, target, dupes, upgrade, media_root, job["id"], record)
        moved = files.apply_plan(plan, media_root, job["id"], replace=(choice == "replace"))
        record["moves"] = moved["moves"]
        record["recycled"] += moved["recycled"]
        record.setdefault("skipped", [])
        record.setdefault("upgraded", [])
        record["created_dirs"] = moved["created_dirs"]
        return record, target

    # ---- Inbox --------------------------------------------------------------------------------

    async def scan_inbox(self) -> None:
        if not prefs.get(self.db, "inbox_enabled"):
            return
        inbox = self.ctx.cfg.media_dir / prefs.get(self.db, "inbox_dir")
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            entries = [p for p in inbox.iterdir() if not p.name.startswith(".")]
        except OSError as e:
            log.warning("Can't read the Inbox folder: %s", e)
            return
        names = {p.name for p in entries}
        for row in self.db.query("SELECT name FROM inbox_seen"):
            if row["name"] not in names:
                self.db.run("DELETE FROM inbox_seen WHERE name=?", (row["name"],))
        for p in entries:
            if p.suffix.lower() in (".part", ".tmp", ".crdownload", ".!qb", ".mega") or p.name.endswith(".linkarr-partial"):
                continue
            size = await asyncio.to_thread(files.tree_size, p) if p.is_dir() else p.stat().st_size
            row = self.db.one("SELECT * FROM inbox_seen WHERE name=?", (p.name,))
            if row is None:
                self.db.run("INSERT INTO inbox_seen (name, size, stable, first_seen) VALUES (?,?,0,?)", (p.name, size, now()))
                continue
            if row["size"] != size:
                self.db.run("UPDATE inbox_seen SET size=?, stable=0 WHERE name=?", (size, p.name))
                continue
            if row["stable"] < 1:
                self.db.run("UPDATE inbox_seen SET stable=stable+1 WHERE name=?", (p.name,))
                continue
            await self.take_from_inbox(p, size)

    async def take_from_inbox(self, p: Path, size: int) -> None:
        staging = jobs.work_dir(self.ctx) / "inbox" / uuid.uuid4().hex[:12]
        staging.mkdir(parents=True, exist_ok=True)
        try:
            files.move_file(p, staging / p.name) if p.is_file() else p.rename(staging / p.name)
        except OSError as e:
            log.warning("Couldn't take %s from the Inbox: %s", p.name, e)
            return
        flist = [{"path": str(f.relative_to(staging)), "size": f.stat().st_size, "selected": True, "junk": False}
                 for f in files.walk_files(staging)]
        guess = await jobs.identify(self.ctx, p.name, flist)
        lib = jobs.pick_library(self.db, guess["kind"], guess["documentary"])
        auto = False
        t = now()
        job_id = self.db.run(
            "INSERT INTO jobs (kind, title, year, tmdb_id, poster, genres, library_id, status, source, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (guess["kind"], guess["title"] or p.name, guess["year"], guess["tmdb_id"], guess["poster"], json.dumps(guess["genres"]),
             lib["id"] if lib else None, "draft", "inbox", guess["note"], t, t)).lastrowid
        season = jobs._season_of(p.name, flist)
        self.db.run("INSERT INTO links (job_id, url, name, season, size, files, status, bytes_done, staging, position, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, "inbox:" + p.name, p.name, season, size, json.dumps(flist), "downloaded", size, str(staging), 1, t))
        self.db.run("UPDATE inbox_seen SET job_id=? WHERE name=?", (job_id, p.name))
        if guess.get("show_unknown"):
            self.db.run("UPDATE jobs SET show_unknown=1 WHERE id=?", (job_id,))
        jobs.refresh_target(self.ctx, job_id)
        jobs.use_remembered(self.ctx, job_id, p.name)
        log.info("Picked up %s from the Inbox", p.name)
        if prefs.get(self.db, "inbox_auto_start") and jobs.can_auto_start(self.ctx, job_id):
            jobs.start_job(self.ctx, job_id)
            auto = True
        if not auto:
            add_alert(self.db, "inbox", "New in the Inbox", f"{guess['title'] or p.name}: check the details and start it.", job_id)
            await self._notify_alert(job_id, None, "inbox")

    # ---- show status --------------------------------------------------------------------------

    async def check_shows(self) -> None:
        if not self.ctx.tmdb.configured():
            return
        for show in self.db.query("SELECT * FROM shows WHERE watching=1 AND checked_at < ? LIMIT 40", (now() - 20 * 3600,)):
            show = dict(show)
            try:
                det = await self.ctx.tmdb.details("series", show["tmdb_id"])
            except Exception as e:  # one bad lookup shouldn't stop the others
                log.warning("Show check for %s failed: %s", show["name"], e)
                continue
            notified = json.loads(show["notified"] or "{}")
            quiet = notified.pop("_quiet", False)  # imported in bulk: note what's already true, say nothing
            today = time.strftime("%Y-%m-%d")
            have = await asyncio.to_thread(self._episodes_of, show)
            events = show_events(show, det, notified, today, have)
            if quiet:
                events = []
            miss = json.dumps(tracker.missing(det, have, today)) if have is not None else ""
            self.db.run("UPDATE shows SET status=?, next_air=?, last_air=?, seasons=?, notified=?, missing=?, poster=?, checked_at=? "
                        "WHERE tmdb_id=?",
                        (det.get("status", ""), det.get("next_air", ""), det.get("last_air", ""), len(det.get("seasons", [])),
                         json.dumps(notified), miss, det.get("poster", "") or "", now(), show["tmdb_id"]))
            for text in events:
                log.info("Show update: %s", text)
                if self.ctx.notify and prefs.get(self.db, "notify_series"):
                    await self.ctx.notify.send(text)

    async def fill_posters(self, limit: int = 200) -> int:
        """Tracked shows without a poster (tracked before 1.1, or in bulk) get theirs, a couple of hundred an hour.
        Coming up shows a title card until then."""
        if not self.ctx.tmdb.configured():
            return 0
        n = 0
        for show in self.db.query("SELECT tmdb_id, name FROM shows WHERE poster = '' ORDER BY watching DESC, next_air LIMIT ?",
                                  (limit + len(self.poster_tried),)):
            if show["tmdb_id"] in self.poster_tried or n >= limit:
                continue
            self.poster_tried.add(show["tmdb_id"])
            try:
                det = await self.ctx.tmdb.details("series", show["tmdb_id"])
            except Exception as e:  # one bad lookup shouldn't stop the others
                log.info("Poster for %s: %s", show["name"], e)
                continue
            if det.get("poster"):
                self.db.run("UPDATE shows SET poster=? WHERE tmdb_id=? AND poster=''", (det["poster"], show["tmdb_id"]))
                n += 1
            await asyncio.sleep(0.05)
        if n:
            log.info("Found posters for %d tracked shows", n)
        return n

    def _posterless_folders(self) -> list[tuple[dict, str]]:
        """Library folders with no poster yet: not a tracked show with one, and not looked up in the last month."""
        have = {(r["library_id"], r["folder"]) for r in self.db.query("SELECT library_id, folder FROM shows WHERE poster != ''")}
        month = now() - 30 * 86400
        have |= {(r["library_id"], r["folder"]) for r in self.db.query(
            "SELECT library_id, folder FROM folder_posters WHERE poster != '' OR tried_at > ?", (month,))}
        out = []
        for lib in jobs.libraries(self.db):
            try:
                base = jobs.lib_dir(self.ctx.cfg, lib)
            except jobs.JobError:
                continue
            out += [(lib, f) for f in files.list_folders(base) if (lib["id"], f) not in have]
        return out

    async def fill_folder_posters(self, limit: int = 40) -> int:
        """Posters for the Library list: every folder (movies, untracked shows) gets its TMDB poster, a few
        dozen a minute, so a big library is done in about an hour. Only a confident name match is used."""
        if not self.ctx.tmdb.configured():
            return 0
        todo = (await asyncio.to_thread(self._posterless_folders))[:limit]
        n = 0
        for lib, folder in todo:
            info = parse_name(folder)
            title = info["title"] or naming.norm_title(folder)
            kind = "series" if lib["kind"] == "series" else "movie"
            poster, tmdb_id = "", None
            try:
                hits = await self.ctx.tmdb.search(title, kind, info["year"] or naming.year_of(folder))
            except Exception as e:  # one bad lookup shouldn't stop the others
                log.info("Poster for %s: %s", folder, e)
                continue
            best = 0.0
            for h in hits[:8]:
                s = naming.score(h["title"], folder, h["year"])
                if s > best and h["poster"]:
                    best, poster, tmdb_id = s, h["poster"], h["id"]
            if best < 0.86:
                poster, tmdb_id = "", None
            self.db.run("INSERT INTO folder_posters (library_id, folder, tmdb_id, poster, tried_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(library_id, folder) DO UPDATE SET tmdb_id=excluded.tmdb_id, poster=excluded.poster, "
                        "tried_at=excluded.tried_at", (lib["id"], folder, tmdb_id, poster, now()))
            n += bool(poster)
            await asyncio.sleep(0.1)
        if n:
            log.info("Found posters for %d library folders", n)
        return n

    async def check_movies(self) -> None:
        """Movie Tracker: new Blu-ray, digital and streaming news, once a day per movie. The first check after
        adding a movie only notes what's already true; the Tracker page shows it."""
        if not self.ctx.tmdb.configured():
            return
        since = now() - 20 * 3600
        country = prefs.get(self.db, "release_country")
        for m in self.db.query("SELECT * FROM movies WHERE checked_at < ? LIMIT 40", (since,)):
            m = dict(m)
            try:
                info = await self.ctx.tmdb.release_info(m["tmdb_id"], country)
            except Exception as e:  # one bad lookup shouldn't stop the others
                log.warning("Movie check for %s failed: %s", m["title"], e)
                continue
            notified = json.loads(m["notified"] or "{}")
            news = movietracker.events(m, info, notified, time.strftime("%Y-%m-%d"))
            movietracker.save(self.db, m["tmdb_id"], info, notified)
            if not m["checked_at"]:
                continue
            for text in news:
                log.info("Movie update: %s", text.replace("\n", ": "))
                if self.ctx.notify and prefs.get(self.db, "notify_movies"):
                    await self.ctx.notify.send(text)

    def _episodes_of(self, show: dict) -> dict | None:
        """What's in the show's folder, or None when Linkarr doesn't know where the show lives."""
        lib = jobs.library(self.db, show["library_id"]) if show["library_id"] else None
        if not lib or not show["folder"]:
            return None
        try:
            return files.episodes_in(jobs.lib_dir(self.ctx.cfg, lib) / show["folder"])
        except (OSError, jobs.JobError):
            return None

    # ---- housekeeping -------------------------------------------------------------------------

    def maintenance(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self.db.get_setting("last_maintenance") == today:
            return
        self.db.set_setting("last_maintenance", today)
        n = files.purge_recycle(self.ctx.cfg.media_dir, int(prefs.get(self.db, "recycle_days")))
        if n:
            log.info("Emptied %d day(s) of the recycle folder", n)
        bdir = self.ctx.cfg.backup_dir
        self.db.backup_to(bdir / f"linkarr-{today}.db")
        for old in sorted(bdir.glob("linkarr-*.db"))[:-7]:
            old.unlink(missing_ok=True)
        self.db.run("DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?", (now() - 30 * 86400,))

    async def _notify_alert(self, job_id, link_id, kind: str) -> None:
        if self.ctx.notify:
            try:
                await self.ctx.notify.alert(job_id, link_id, kind)
            except Exception:
                log.exception("Notification failed")


def show_events(show: dict, det: dict, notified: dict, today: str, have: dict | None = None) -> list[str]:
    """What's new about a show since the last check. notified is updated so each fires once."""
    out = []
    name = det.get("title") or show["name"]
    status = det.get("status", "")
    if status in ("Ended", "Canceled") and notified.get("status") != status:
        out.append(f"📺 {name} has {'ended' if status == 'Ended' else 'been canceled'}. What you have is the complete series"
                   " if no seasons are missing.")
        notified["status"] = status
    nxt_season, nxt_ep, nxt_air = det.get("next_season"), det.get("next_episode"), det.get("next_air")
    if nxt_season and nxt_ep == 1 and nxt_air:
        key = f"premiere_{nxt_season}"
        if not notified.get(key):
            out.append(f"📅 {name} season {nxt_season} starts on {nxt_air}.")
            notified[key] = nxt_air
    last_type, last_season, last_air = det.get("last_episode_type", ""), det.get("last_season"), det.get("last_air", "")
    recent = last_air and _days_between(last_air, today) <= 14
    if last_type == "finale" and last_season and recent and last_air <= today:
        key = f"finale_{last_season}"
        if not notified.get(key):
            text = f"✅ {name} season {last_season} finale aired on {last_air}. The full season is out."
            if have is not None:
                got = have.get(last_season)
                if not got or not (got["eps"] or got["unknown"]):
                    text += f" You have {tracker.owned_seasons(have)}."
                elif tracker.missing(det, {last_season: got}, today)["episodes"].get(last_season):
                    text += " You're missing some episodes of it."
            out.append(text)
            notified[key] = last_air
    return out


def _days_between(a: str, b: str) -> int:
    import datetime as dt
    try:
        return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
    except ValueError:
        return 9999
