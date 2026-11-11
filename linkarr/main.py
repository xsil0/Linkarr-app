"""Linkarr web app: JSON API plus the static front end. Every /api route except health and
sign-in needs a session; every change needs the X-Linkarr header (blocks cross-site posts)."""
import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, decode, files, jobs, logbuf, mediaserver, movietracker, post, prefs, roundup, tracker
from . import watch as folder_watch
from .config import Config, load
from .mediaserver import MediaServer
from .db import Database, now
from .jobs import Ctx, JobError
from .mega import Mega, MegaCmd, MegaError
from .notify import Notifier
from .plex import Plex
from .telegram import Telegram
from .tmdb import IMG, Tmdb, TmdbError
from .subs import Subs
from .vault import NAMES, Vault
from .worker import Worker

log = logging.getLogger("linkarr")
WEB = Path(__file__).parent / "web"
PUBLIC_API = {"/api/health", "/api/auth/state", "/api/auth/login", "/api/auth/setup"}


def create_app(cfg: Config | None = None, mega: Mega | None = None, tmdb_client: httpx.AsyncClient | None = None,
               telegram_client: httpx.AsyncClient | None = None, plex_client: httpx.AsyncClient | None = None,
               subs_client: httpx.AsyncClient | None = None, server_client: httpx.AsyncClient | None = None) -> FastAPI:
    cfg = cfg or load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cfg.data_dir, 0o700)
        logbuf.setup(cfg.log_dir)
        db = Database(cfg.db_path)
        vault = Vault(db, cfg.key_path)
        ctx = Ctx(cfg=cfg, db=db, vault=vault,
                  mega=mega or Mega(MegaCmd(cfg.mega_home, cfg.mega_bin)),
                  tmdb=Tmdb(lambda: vault.get("tmdb_key"), tmdb_client))
        tg = Telegram(ctx, telegram_client)
        plex = Plex(lambda: prefs.get(db, "plex_url"), lambda: vault.get("plex_token"), plex_client)
        servers = {k: MediaServer(k, lambda k=k: prefs.get(db, k + "_url"), lambda k=k: vault.get(k + "_key"), server_client)
                   for k in mediaserver.KINDS}
        ctx.notify = Notifier(ctx, tg, plex, servers.values())
        ctx.subs = Subs(ctx, subs_client)
        app.state.ctx, app.state.tg, app.state.plex, app.state.servers = ctx, tg, plex, servers
        app.state.http = httpx.AsyncClient(timeout=20)
        stop = asyncio.Event()
        tasks = []
        if not auth.has_user(db):
            log.warning("No account yet. Open Linkarr and use this setup code: %s", auth.setup_code(db))
        if cfg.worker:
            app.state.worker = Worker(ctx)
            tasks.append(asyncio.create_task(app.state.worker.run(stop)))
            tasks.append(asyncio.create_task(tg.run(stop)))
        log.info("Linkarr %s started", __version__)
        yield
        stop.set()
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        for c in (ctx.mega, ctx.tmdb, tg, plex, ctx.subs, *([] if server_client else servers.values())):
            try:
                await c.close()
            except Exception:
                pass
        await app.state.http.aclose()
        db.close()

    app = FastAPI(title="Linkarr", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg

    def ctx_of(request: Request) -> Ctx:
        return request.app.state.ctx

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_API:
            user = auth.session_user(request.app.state.ctx.db, request.cookies.get(auth.COOKIE))
            if user is None:
                return JSONResponse({"detail": "Sign in first."}, status_code=401)
            request.state.user = user
        if path.startswith("/api/") and request.method not in ("GET", "HEAD") and request.headers.get("x-linkarr") != "1":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"))
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if path.startswith("/api/") and not path.startswith("/api/poster/") or path == "/":
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def ip_of(request: Request) -> str:
        # The address uvicorn settled on. It already applies X-Forwarded-For, but only from the
        # proxies in LA_TRUSTED_PROXIES; the raw header is never read here.
        return (request.client.host if request.client else "")[:64]

    def fail(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    # ---- health and sign-in ---------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        db = ctx_of(request).db
        user = auth.session_user(db, request.cookies.get(auth.COOKIE))
        return {"setup_needed": not auth.has_user(db), "signed_in": user is not None,
                "username": user["username"] if user else None, "locked_for": auth.locked_for(db, ip_of(request)),
                "cookie_secure": cfg.cookie_secure, "theme": prefs.get(db, "theme")}

    class Setup(BaseModel):
        code: str
        username: str = Field(min_length=1, max_length=60)
        password: str = Field(min_length=10, max_length=200)

    def _set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(auth.COOKIE, token, max_age=cfg.session_hours * 3600, httponly=True,
                        secure=cfg.cookie_secure, samesite="lax", path="/")

    @app.post("/api/auth/setup")
    async def setup(body: Setup, request: Request):
        db = ctx_of(request).db
        if request.headers.get("x-linkarr") != "1":
            raise HTTPException(403, "Missing request header.")
        if auth.has_user(db):
            raise HTTPException(409, "An account already exists.")
        wait = auth.locked_for(db, ip_of(request))
        if wait:
            raise HTTPException(429, f"Too many wrong tries. Try again in {wait // 60 + 1} minutes.")
        if not hmac.compare_digest(body.code.strip().lower(), auth.setup_code(db)):
            db.audit("setup_failed", "", ip_of(request))
            await asyncio.sleep(1)
            raise HTTPException(403, "That setup code is wrong. It's in the container log.")
        auth.create_user(db, body.username.strip(), body.password)
        token = auth.login(db, body.username.strip(), body.password, cfg.session_hours, ip_of(request))
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, token)
        return resp

    class Login(BaseModel):
        username: str
        password: str

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request):
        db = ctx_of(request).db
        if request.headers.get("x-linkarr") != "1":
            raise HTTPException(403, "Missing request header.")
        wait = auth.locked_for(db, ip_of(request))
        if wait:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {wait // 60 + 1} minutes.")
        token = auth.login(db, body.username.strip()[:60], body.password[:200], cfg.session_hours, ip_of(request))
        if token is None:
            await asyncio.sleep(1)
            raise HTTPException(401, "Wrong username or password.")
        resp = JSONResponse({"ok": True})
        _set_cookie(resp, token)
        return resp

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        auth.logout(ctx_of(request).db, request.cookies.get(auth.COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    class NewPassword(BaseModel):
        current: str
        password: str = Field(min_length=10, max_length=200)

    @app.post("/api/auth/password")
    async def new_password(body: NewPassword, request: Request):
        db = ctx_of(request).db
        user = request.state.user
        row = db.one("SELECT pw_hash FROM users WHERE id = ?", (user["id"],))
        if not auth.verify_password(body.current, row["pw_hash"]):
            raise HTTPException(403, "Current password is wrong.")
        auth.change_password(db, user["id"], body.password)
        return {"ok": True}

    # ---- overview -------------------------------------------------------------------------

    def _free(path: Path) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def _space(n: int) -> str:
        return f"{n / 1024**4:.2f} TB" if n >= 1024**4 else f"{n / 1024**3:.0f} GB"

    @app.get("/api/overview")
    async def overview(request: Request):
        ctx = ctx_of(request)
        db = ctx.db
        active = [jobs.job_view(db, r["id"]) for r in db.query(
            "SELECT id FROM jobs WHERE status NOT IN ('done','canceled') ORDER BY CASE status WHEN 'needs_captcha' THEN 0 "
            "WHEN 'needs_answer' THEN 0 WHEN 'draft' THEN 1 WHEN 'downloading' THEN 2 ELSE 3 END, created_at")]
        recent = [jobs.job_view(db, r["id"]) for r in db.query(
            "SELECT id FROM jobs WHERE status = 'done' ORDER BY coalesce(finished_at, updated_at) DESC LIMIT 5")]
        speed = sum((ctx.status.get("speed") or {}).values())
        libs = jobs.libraries(db)
        counts = {}
        for lib in libs:
            try:
                counts[lib["id"]] = len(files.list_folders(jobs.lib_dir(cfg, lib)))
            except JobError:
                counts[lib["id"]] = 0
        return {
            "version": __version__, "active": active, "recent": recent, "alerts": jobs.open_alerts(db),
            "speed": speed, "free": _free(cfg.media_dir),
            "libraries": [dict(lib, count=counts.get(lib["id"], 0)) for lib in libs],
            "mega": ctx.status.get("mega_email", ""), "tmdb": ctx.tmdb.configured(),
            "quota": ctx.status.get("mega_quota"), "site": prefs.site(db), "setup_issues": quick_issues(ctx, libs),
            "extras": {"watch": prefs.get(db, "watch_enabled"), "captcha": prefs.get(db, "captcha_enabled")},
        }

    def quick_issues(ctx, libs) -> int:
        """How many basic setup checks fail, without asking MEGA or TMDB anything (for the dot on Settings)."""
        bad = 0
        bad += not (cfg.media_dir.exists() and os.access(cfg.media_dir, os.W_OK))
        bad += not libs
        bad += not ctx.tmdb.configured()
        return bad

    # ---- jobs -----------------------------------------------------------------------------

    class NewLink(BaseModel):
        url: str = Field(min_length=10, max_length=2000)
        job_id: int | None = None
        kind: str | None = None
        archive_password: str = Field(default="", max_length=100)

    @app.post("/api/jobs")
    async def new_job(body: NewLink, request: Request):
        ctx = ctx_of(request)
        try:
            job = await jobs.add_link(ctx, body.url, body.job_id, body.kind if body.kind in ("movie", "series") else None)
        except JobError as e:
            fail(e)
        if body.archive_password and not body.job_id and not job["has_password"]:
            ctx.db.run("UPDATE jobs SET archive_password=? WHERE id=?", (body.archive_password, job["id"]))
            job["has_password"] = True
        return job

    class Post(BaseModel):
        text: str = Field(min_length=1, max_length=50000)

    @app.post("/api/scan")
    async def scan_post(body: Post, request: Request):
        """Reads a pasted or shared post: every link in it, what each one is, and which to tick. Adds nothing."""
        ctx = ctx_of(request)
        return await post.scan(ctx, body.text, prefs.captcha_hosts(ctx.db) if prefs.get(ctx.db, "captcha_enabled") else [])

    class StartAll(BaseModel):
        when: str = "now"

    @app.post("/api/jobs/start-all")
    async def start_all(body: StartAll, request: Request):
        return jobs.start_all(ctx_of(request), "tonight" if body.when == "tonight" else "now")

    class Encoded(BaseModel):
        text: str = Field(max_length=20000)

    @app.post("/api/decode")
    async def decode_text(body: Encoded, request: Request):
        return decode.unwrap(body.text, hosts=prefs.captcha_hosts(ctx_of(request).db))

    # ---- captcha links: held until you solve the captcha and paste back the MEGA link --------

    class CaptchaLink(BaseModel):
        url: str = Field(min_length=8, max_length=2000)

    @app.post("/api/captcha")
    async def hold_captcha(body: CaptchaLink, request: Request):
        ctx = ctx_of(request)
        try:
            job = jobs.hold_captcha(ctx, body.url)
        except JobError as e:
            fail(e)
        if not job["held_before"] and ctx.notify:
            try:
                await ctx.notify.captcha(job["id"])  # so it can be finished on the phone too
            except Exception:
                log.exception("Couldn't send the captcha link to Telegram")
        return job

    class Solved(BaseModel):
        text: str = Field(min_length=8, max_length=20000)

    @app.post("/api/jobs/{job_id}/captcha")
    async def solve_captcha(job_id: int, body: Solved, request: Request):
        try:
            return {"jobs": await jobs.solve_captcha(ctx_of(request), job_id, body.text)}
        except JobError as e:
            fail(e)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: int, request: Request):
        job = jobs.job_view(ctx_of(request).db, job_id)
        if job is None:
            raise HTTPException(404, "Not found.")
        lib = job.get("library")
        if lib:
            try:
                job["target"] = str(jobs.target_path(ctx_of(request), job))
            except JobError:
                job["target"] = ""
        return job

    @app.get("/api/jobs/{job_id}/match-info")
    async def get_match_info(job_id: int, request: Request):
        return await jobs.match_info(ctx_of(request), job_id)

    @app.patch("/api/jobs/{job_id}")
    async def edit_job(job_id: int, body: dict, request: Request):
        try:
            await jobs.update_job(ctx_of(request), job_id, body)
            return await get_job(job_id, request)
        except (JobError, ValueError) as e:
            fail(e)

    @app.delete("/api/jobs/{job_id}")
    async def del_job(job_id: int, request: Request):
        jobs.delete_job(ctx_of(request), job_id)
        return {"ok": True}

    class Start(BaseModel):
        when: str = "now"

    @app.post("/api/jobs/{job_id}/start")
    async def start(job_id: int, body: Start, request: Request):
        try:
            return jobs.start_job(ctx_of(request), job_id, body.when)
        except JobError as e:
            fail(e)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: int, request: Request):
        try:
            return jobs.cancel_job(ctx_of(request), job_id)
        except JobError as e:
            fail(e)

    class Match(BaseModel):
        choice: str
        folder: str = ""

    @app.post("/api/jobs/{job_id}/match")
    async def match(job_id: int, body: Match, request: Request):
        try:
            return jobs.answer_match(ctx_of(request), job_id, body.choice, body.folder)
        except JobError as e:
            fail(e)

    class ShowChoice(BaseModel):
        library_id: int | None = None
        tmdb_id: int | None = None
        folder: str = Field(default="", max_length=200)
        remember: bool = False

    @app.post("/api/jobs/{job_id}/show")
    async def match_show(job_id: int, body: ShowChoice, request: Request):
        """Match to show: a TMDB show, a folder you have, or a name you type."""
        ctx = ctx_of(request)
        try:
            res = await jobs.match_show(ctx, job_id, body.model_dump(exclude={"remember"}), body.remember)
        except JobError as e:
            fail(e)
        for section in sorted(set(res.pop("sections"))):
            await request.app.state.plex.refresh(section)
        return res

    @app.get("/api/jobs/{job_id}/alike")
    async def job_alike(job_id: int, request: Request):
        try:
            return jobs.alike(ctx_of(request), job_id=job_id)
        except JobError as e:
            fail(e)

    class PlexAnswer(BaseModel):
        action: str = Field(pattern="^(check|found)$")

    @app.post("/api/jobs/{job_id}/plex")
    async def plex_answer(job_id: int, body: PlexAnswer, request: Request):
        """"Check Plex again" (look now, then ask Plex to scan and keep looking) or "It's in Plex" (you checked)."""
        ctx = ctx_of(request)
        if jobs.job_view(ctx.db, job_id) is None:
            raise HTTPException(404, "Not found.")
        worker = worker_of(request)
        if body.action == "found":
            worker._plex_found(job_id)
            return {"plex": "found"}
        try:
            return {"plex": await worker.recheck_plex(job_id)}
        except JobError as e:
            fail(e)

    @app.post("/api/links/{link_id}/retry")
    async def retry(link_id: int, request: Request):
        try:
            return {"ok": True, "mode": jobs.retry_link(ctx_of(request), link_id)}
        except JobError as e:
            fail(e)

    @app.post("/api/links/{link_id}/skip")
    async def skip(link_id: int, request: Request):
        try:
            jobs.skip_link(ctx_of(request), link_id)
            return {"ok": True}
        except JobError as e:
            fail(e)

    class Selected(BaseModel):
        selected: list[str]

    @app.post("/api/links/{link_id}/files")
    async def pick_files(link_id: int, body: Selected, request: Request):
        try:
            jobs.set_files(ctx_of(request), link_id, body.selected)
            return {"ok": True}
        except JobError as e:
            fail(e)

    @app.post("/api/alerts/{alert_id}/dismiss")
    async def dismiss(alert_id: int, request: Request):
        ctx_of(request).db.run("UPDATE alerts SET resolved_at=? WHERE id=?", (now(), alert_id))
        return {"ok": True}

    # ---- library and TMDB -----------------------------------------------------------------

    @app.get("/api/library/search")
    async def lib_search(q: str, request: Request, tmdb: int | None = None):
        try:
            return await jobs.search_library(ctx_of(request), q[:120], tmdb)
        except JobError as e:
            fail(e)

    @app.get("/api/libraries/{lib_id}/folders")
    async def lib_folders(lib_id: int, request: Request):
        ctx = ctx_of(request)
        lib = jobs.library(ctx.db, lib_id)
        if lib is None:
            raise HTTPException(404, "Not found.")
        return files.list_folders(jobs.lib_dir(cfg, lib))

    @app.get("/api/tmdb/search")
    async def tmdb_search(q: str, request: Request, kind: str | None = None):
        ctx = ctx_of(request)
        try:
            return await ctx.tmdb.search(q[:120], kind)
        except Exception as e:
            fail(e)

    @app.post("/api/shows/track-all")
    async def track_all(request: Request):
        """Starts adding every show you already have to the Series Tracker; GET reports progress."""
        st = request.app.state
        if getattr(st, "track_all", None) and st.track_all.get("running"):
            return st.track_all
        ctx = ctx_of(request)
        st.track_all = {"running": True, "total": 0, "done": 0, "added": 0, "unmatched": [], "error": ""}

        async def go():
            try:
                res = await jobs.track_existing(ctx, lambda p: st.track_all.update(p))
                st.track_all.update(res)
            except JobError as e:
                st.track_all["error"] = str(e)
            except Exception:
                log.exception("Series Tracker import failed")
                st.track_all["error"] = "Something went wrong. The log has the details."
            finally:
                st.track_all["running"] = False

        st.track_all_task = asyncio.create_task(go())
        return st.track_all

    @app.get("/api/shows/track-all")
    async def track_all_status(request: Request):
        return getattr(request.app.state, "track_all", None) or {"running": False, "total": 0}

    @app.get("/api/shows/{tmdb_id}")
    async def show(tmdb_id: int, request: Request):
        ctx = ctx_of(request)
        try:
            det = await jobs.show_details(ctx, tmdb_id)
        except JobError as e:
            fail(e)
        row = ctx.db.one("SELECT watching FROM shows WHERE tmdb_id=?", (tmdb_id,))
        det["watching"] = bool(row and row["watching"])
        return det

    class Watch(BaseModel):
        watching: bool
        name: str = ""
        folder: str = ""
        library_id: int | None = None

    @app.post("/api/shows/{tmdb_id}/watch")
    async def watch(tmdb_id: int, body: Watch, request: Request):
        db = ctx_of(request).db
        db.run("INSERT INTO shows (tmdb_id, name, folder, library_id, watching) VALUES (?,?,?,?,?) "
               "ON CONFLICT(tmdb_id) DO UPDATE SET watching=excluded.watching",
               (tmdb_id, body.name[:200] or str(tmdb_id), body.folder[:300], body.library_id, 1 if body.watching else 0))
        return {"ok": True}

    @app.get("/api/shows")
    async def shows(request: Request):
        out = []
        for r in ctx_of(request).db.query("SELECT * FROM shows WHERE watching=1 ORDER BY name"):
            row = dict(r)
            miss = json.loads(row.pop("missing") or "null")
            row["checked"] = miss is not None
            row["missing"] = tracker.describe(miss) if miss else ""
            out.append(row)
        return out

    _POSTER = re.compile(r"^/[A-Za-z0-9]+\.(jpg|png)$")

    # ---- movie tracker ------------------------------------------------------------------------

    @app.get("/api/movies")
    async def movies(request: Request):
        rows = ctx_of(request).db.query("SELECT * FROM movies ORDER BY CASE WHEN physical = '' THEN '9999' ELSE physical END, title")
        return [movietracker.view(r) for r in rows]

    class TrackMovie(BaseModel):
        tmdb_id: int

    @app.post("/api/movies")
    async def track_movie(body: TrackMovie, request: Request):
        """Add a movie and look it up straight away, so the Tracker shows its dates at once (no messages for those)."""
        ctx = ctx_of(request)
        try:
            info = await ctx.tmdb.release_info(body.tmdb_id, prefs.get(ctx.db, "release_country"))
        except TmdbError as e:
            fail(e)
        ctx.db.run("INSERT INTO movies (tmdb_id, title, year, added_at) VALUES (?,?,?,?) ON CONFLICT(tmdb_id) DO NOTHING",
                   (body.tmdb_id, info["title"], info["year"], now()))
        row = ctx.db.one("SELECT * FROM movies WHERE tmdb_id=?", (body.tmdb_id,))
        notified = json.loads(row["notified"] or "{}")
        movietracker.events(dict(row), info, notified, time.strftime("%Y-%m-%d"))
        movietracker.save(ctx.db, body.tmdb_id, info, notified)
        return movietracker.view(ctx.db.one("SELECT * FROM movies WHERE tmdb_id=?", (body.tmdb_id,)))

    @app.delete("/api/movies/{tmdb_id}")
    async def untrack_movie(tmdb_id: int, request: Request):
        ctx_of(request).db.run("DELETE FROM movies WHERE tmdb_id=?", (tmdb_id,))
        return {"ok": True}

    @app.get("/api/poster/{name}")
    async def poster(name: str, request: Request):
        path = "/" + name
        if not _POSTER.match(path):
            raise HTTPException(404, "Not found.")
        cache = cfg.poster_dir / name
        if not cache.exists():
            try:
                r = await request.app.state.http.get(IMG + path)
                r.raise_for_status()
            except httpx.HTTPError:
                raise HTTPException(404, "Not found.")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
        return FileResponse(cache, headers={"Cache-Control": "max-age=2592000"})

    # ---- folder watch ------------------------------------------------------------------------

    def worker_of(request: Request) -> Worker:
        worker = getattr(request.app.state, "worker", None)
        if worker is None:  # no background worker (tests)
            worker = request.app.state.worker = Worker(ctx_of(request))
        return worker

    @app.get("/api/watches")
    async def watches(request: Request):
        db = ctx_of(request).db
        return {"enabled": prefs.get(db, "watch_enabled"), "every": prefs.get(db, "watch_every"),
                "stop_days": prefs.get(db, "watch_stop_days"), "watches": folder_watch.all_watches(db)}

    class NewWatch(BaseModel):
        link_id: int

    @app.post("/api/watches")
    async def add_watch(body: NewWatch, request: Request):
        try:
            return folder_watch.add(ctx_of(request), body.link_id)
        except folder_watch.WatchError as e:
            fail(e)

    class WatchState(BaseModel):
        active: bool

    @app.post("/api/watches/{watch_id}")
    async def watch_state(watch_id: int, body: WatchState, request: Request):
        try:
            return folder_watch.set_active(ctx_of(request), watch_id, body.active)
        except folder_watch.WatchError as e:
            fail(e)

    @app.post("/api/watches/{watch_id}/check")
    async def watch_check(watch_id: int, request: Request):
        ctx = ctx_of(request)
        if not prefs.get(ctx.db, "watch_enabled"):
            raise HTTPException(400, "Monitored links are off. Turn them on in Settings → Automation.")
        if ctx.db.one("SELECT 1 FROM watches WHERE id = ?", (watch_id,)) is None:
            raise HTTPException(404, "Not found.")
        before = ctx.db.one("SELECT episodes FROM watches WHERE id = ?", (watch_id,))["episodes"]
        await worker_of(request).check_watches(only=watch_id)
        row = ctx.db.one("SELECT * FROM watches WHERE id = ?", (watch_id,))
        return {"new": row["episodes"] - before, "watch": folder_watch.view(row)}

    @app.delete("/api/watches/{watch_id}")
    async def del_watch(watch_id: int, request: Request):
        folder_watch.remove(ctx_of(request), watch_id)
        return {"ok": True}

    @app.post("/api/morning/send")
    async def morning_send(request: Request):
        if not request.app.state.tg.configured():
            raise HTTPException(400, "Set up Telegram first.")
        text = await worker_of(request).morning_summary(force=True)
        return {"sent": bool(text)}

    # ---- history --------------------------------------------------------------------------

    @app.get("/api/history")
    async def history(request: Request):
        rows = ctx_of(request).db.query(
            "SELECT h.*, j.title, j.poster, j.kind FROM history h LEFT JOIN jobs j ON j.id = h.job_id ORDER BY h.id DESC LIMIT 100")
        out = []
        for r in rows:
            d = dict(r)
            d["moves"] = json.loads(d["moves"])
            d["recycled"] = json.loads(d["recycled"])
            d.pop("created_dirs", None)
            if d["moves"]:
                d["where"] = str(files.filed_base(d["moves"], d["kind"] or "movie"))
            out.append(d)
        return out

    class Refile(BaseModel):
        library_id: int
        folder: str = Field(min_length=1, max_length=200)

    @app.post("/api/history/{hid}/move")
    async def refile(hid: int, body: Refile, request: Request):
        ctx = ctx_of(request)
        try:
            res = await asyncio.to_thread(jobs.refile, ctx, hid, body.library_id, body.folder)
        except (JobError, OSError) as e:
            fail(e)
        for section in res.pop("sections"):
            await request.app.state.plex.refresh(section)
        return res

    @app.post("/api/history/{hid}/show")
    async def fix_show(hid: int, body: ShowChoice, request: Request):
        """Fix show: moves something already filed into the right show folder and season."""
        ctx = ctx_of(request)
        try:
            res = await jobs.fix_show(ctx, hid, body.model_dump(exclude={"remember"}), body.remember)
        except (JobError, OSError) as e:
            fail(e)
        for section in res.pop("sections"):
            await request.app.state.plex.refresh(section)
        return res

    @app.get("/api/history/{hid}/alike")
    async def history_alike(hid: int, request: Request):
        try:
            return jobs.alike(ctx_of(request), hid=hid)
        except JobError as e:
            fail(e)

    @app.get("/api/remembered-shows")
    async def remembered(request: Request):
        return jobs.remembered_shows(ctx_of(request).db)

    @app.delete("/api/remembered-shows")
    async def forget(key: str, request: Request):
        jobs.forget_show(ctx_of(request).db, key)
        return {"ok": True}

    @app.post("/api/history/{hid}/undo")
    async def undo(hid: int, request: Request):
        ctx = ctx_of(request)
        row = ctx.db.one("SELECT * FROM history WHERE id = ?", (hid,))
        if row is None or row["undone_at"]:
            raise HTTPException(400, "Already undone.")
        record = {"moves": json.loads(row["moves"]), "recycled": json.loads(row["recycled"]),
                  "created_dirs": json.loads(row["created_dirs"])}
        undone_dir = jobs.work_dir(ctx) / "undone" / str(hid)
        problems = await asyncio.to_thread(files.undo, record, undone_dir)
        ctx.db.run("UPDATE history SET undone_at=? WHERE id=?", (now(), hid))
        if row["job_id"]:
            ctx.db.run("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (now(), row["job_id"]))
        return {"ok": True, "problems": problems, "kept_in": str(undone_dir)}

    # ---- settings -------------------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings(request: Request):
        ctx = ctx_of(request)
        return {"prefs": prefs.all_prefs(ctx.db), "secrets": ctx.vault.which_set(), "libraries": jobs.libraries(ctx.db),
                "media_dir": str(cfg.media_dir), "mega": {"installed": ctx.mega.cmd.installed() if hasattr(ctx.mega, "cmd") else True,
                                                          "email": ctx.status.get("mega_email", "")},
                "quota": ctx.status.get("mega_quota")}

    @app.put("/api/settings")
    async def put_settings(body: dict, request: Request):
        try:
            return prefs.set_many(ctx_of(request).db, body)
        except (ValueError, TypeError) as e:
            fail(e)

    @app.put("/api/secrets")
    async def put_secrets(body: dict, request: Request):
        ctx = ctx_of(request)
        for k, v in body.items():
            if k in NAMES:
                ctx.vault.set(k, str(v or ""))
                ctx.db.audit("secret_changed", k, ip_of(request))
        ctx.tmdb.cache.clear()
        return ctx.vault.which_set()

    @app.post("/api/subs/test")
    async def subs_test(request: Request):
        problem = await ctx_of(request).subs.check()
        if problem:
            raise HTTPException(400, problem)
        return {"ok": True}

    @app.post("/api/libraries")
    async def add_library(body: dict, request: Request):
        try:
            return jobs.save_library(ctx_of(request), body)
        except JobError as e:
            fail(e)

    @app.put("/api/libraries/{lib_id}")
    async def edit_library(lib_id: int, body: dict, request: Request):
        try:
            return jobs.save_library(ctx_of(request), body, lib_id)
        except JobError as e:
            fail(e)

    @app.delete("/api/libraries/{lib_id}")
    async def del_library(lib_id: int, request: Request):
        db = ctx_of(request).db
        busy = db.one("SELECT count(*) AS n FROM jobs WHERE library_id=? AND status NOT IN ('done','canceled','failed')", (lib_id,))["n"]
        if busy:
            raise HTTPException(400, "Jobs are still using this library.")
        db.run("DELETE FROM libraries WHERE id=?", (lib_id,))
        return {"ok": True}

    @app.post("/api/libraries/order")
    async def order_libraries(body: list[int], request: Request):
        db = ctx_of(request).db
        for i, lid in enumerate(body):
            db.run("UPDATE libraries SET position=? WHERE id=?", (i, lid))
        return jobs.libraries(db)

    class MegaLogin(BaseModel):
        email: str
        password: str
        code: str = ""

    @app.post("/api/mega/login")
    async def mega_login(body: MegaLogin, request: Request):
        ctx = ctx_of(request)
        try:
            await ctx.mega.cmd.login(body.email.strip(), body.password, body.code.strip())
        except MegaError as e:
            fail(e)
        ctx.status["mega_email"] = await ctx.mega.cmd.whoami()
        ctx.db.audit("mega_login", body.email.strip(), ip_of(request))
        return {"email": ctx.status["mega_email"]}

    @app.post("/api/mega/logout")
    async def mega_logout(request: Request):
        ctx = ctx_of(request)
        try:
            await ctx.mega.cmd.logout()
        except MegaError as e:
            fail(e)
        ctx.status["mega_email"] = ""
        return {"ok": True}

    @app.post("/api/roundup/send")
    async def roundup_send(request: Request):
        ctx = ctx_of(request)
        if not request.app.state.tg.configured():
            raise HTTPException(400, "Set up Telegram first.")
        text = roundup.text(ctx.db, ctx.status.get("mega_quota"))
        await ctx.notify.send_html(text)
        return {"ok": True}

    @app.post("/api/mega/quota")
    async def mega_quota(request: Request):
        ctx = ctx_of(request)
        q = await ctx.mega.quota() if hasattr(ctx.mega, "quota") else None
        if q:
            q["at"] = now()
        ctx.status["mega_quota"] = q
        return {"quota": q}

    @app.get("/api/plex/sections")
    async def plex_sections(request: Request):
        plex = request.app.state.plex
        if not plex.configured():
            raise HTTPException(400, "Add the Plex address and token first.")
        try:
            return await plex.sections()
        except httpx.HTTPError as e:
            fail(Exception(f"Couldn't reach Plex ({e.__class__.__name__})."))

    @app.post("/api/mediaserver/{kind}/test")
    async def server_test(kind: str, request: Request):
        server = request.app.state.servers.get(kind)
        if server is None:
            raise HTTPException(404, "Not found.")
        if not server.configured():
            raise HTTPException(400, f"Add the {server.label} address and API key first, then save.")
        try:
            return await server.info()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(400, mediaserver.explain(e, server.label))

    @app.post("/api/telegram/clear")
    async def telegram_clear(request: Request):
        tg = request.app.state.tg
        if not tg.configured():
            raise HTTPException(400, "Set up Telegram first.")
        return await tg.clear()

    @app.post("/api/telegram/test")
    async def telegram_test(request: Request):
        tg = request.app.state.tg
        me = await tg.me()
        if not me:
            raise HTTPException(400, "Telegram didn't accept the bot token.")
        if not tg.chats():
            return {"bot": me.get("username"), "sent": False}
        await tg.broadcast("👋 Linkarr is connected. Send me a MEGA link any time.")
        return {"bot": me.get("username"), "sent": True}

    # ---- setup check, logs, backup ---------------------------------------------------------

    @app.get("/api/check")
    async def check(request: Request):
        ctx = ctx_of(request)
        items = []

        def add(name, ok, detail, fix=""):
            items.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

        add("Data folder", os.access(cfg.data_dir, os.W_OK), str(cfg.data_dir))
        media_ok = cfg.media_dir.exists() and os.access(cfg.media_dir, os.W_OK)
        free = _free(cfg.media_dir)
        add("Media folder", media_ok, f"{cfg.media_dir}" + (f", {_space(free)} free" if free else ""),
            "" if media_ok else "Map your media share to /media in the container settings, read/write.")
        libs = jobs.libraries(ctx.db)
        if not libs:
            add("Libraries", False, "None yet.", "Add your Movies, TV and other folders in Settings → Libraries.")
        for lib in libs:
            p = jobs.lib_dir(cfg, lib)
            ok = p.exists() and os.access(p, os.W_OK)
            add(f"Library: {lib['name']}", ok, str(p), "" if ok else "The folder is missing or Linkarr can't write to it.")
        installed = ctx.mega.cmd.installed() if hasattr(ctx.mega, "cmd") else True
        add("MEGAcmd", installed, "installed" if installed else "not found", "" if installed else "Use the official Linkarr image.")
        email = await ctx.mega.cmd.whoami() if installed and hasattr(ctx.mega, "cmd") else ctx.status.get("mega_email", "")
        ctx.status["mega_email"] = email
        add("MEGA account", bool(email), email or "Not signed in.", "" if email else "Sign in under Settings → Accounts.")
        if ctx.tmdb.configured():
            try:
                await ctx.tmdb.check()
                add("TMDB", True, "Key works.")
            except Exception as e:
                add("TMDB", False, str(e), "Check the key in Settings → Accounts.")
        else:
            add("TMDB", False, "No key.", "Get a free API key at themoviedb.org and add it in Settings → Accounts.")
        for tool, why in (("unrar", "unpacking rar (also locked and over 2 GB)"), ("7z", "unpacking zip and 7z"),
                          ("ffprobe", "checking videos play")):
            ok = shutil.which(tool) is not None or (tool == "7z" and shutil.which("7zz") is not None)
            add(f"{tool}", ok, why, "" if ok else "Use the official Linkarr image.")
        plex = request.app.state.plex
        if plex.configured():
            try:
                secs = await plex.sections()
                add("Plex", True, f"{len(secs)} libraries")
            except Exception as e:
                add("Plex", False, e.__class__.__name__, "Check the Plex address and token.")
        for server in request.app.state.servers.values():
            if server.configured():
                try:
                    info = await server.info()
                    add(server.label, True, " ".join(x for x in (info["name"], info["version"]) if x) or "Connected")
                except (httpx.HTTPError, ValueError) as e:
                    add(server.label, False, mediaserver.explain(e, server.label), "Check Settings → Media servers.")
        tg = request.app.state.tg
        if tg.configured():
            me = await tg.me()
            add("Telegram", bool(me), f"@{me['username']}" if me else "Token not accepted",
                "" if tg.chats() else "Add your chat id in Settings → Telegram (send /start to the bot to see it).")
        return {"items": items, "ok": all(i["ok"] for i in items)}

    @app.get("/api/logs")
    async def logs(request: Request):
        return {"lines": list(logbuf.RING.lines)[-500:]}

    @app.get("/api/backup")
    async def backup(request: Request, secrets: int = 0):
        ctx = ctx_of(request)
        db = ctx.db
        data = {
            "linkarr": __version__, "made": now(),
            "prefs": prefs.all_prefs(db),
            "libraries": jobs.libraries(db),
            "shows": [dict(r) for r in db.query("SELECT * FROM shows")],
            "movies": [dict(r) for r in db.query("SELECT * FROM movies")],
            "watches": [dict(r) for r in db.query("SELECT * FROM watches")],
            "history": [dict(r) for r in db.query("SELECT * FROM history ORDER BY id DESC LIMIT 1000")],
        }
        if secrets:
            data["secrets"] = {n: ctx.vault.get(n) for n in NAMES if ctx.vault.get(n)}
        name = f"linkarr-backup-{time.strftime('%Y%m%d')}.json"
        return Response(json.dumps(data, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/restore")
    async def restore(request: Request, file: UploadFile = File(...)):
        ctx = ctx_of(request)
        db = ctx.db
        try:
            data = json.loads((await file.read(10_000_000)).decode("utf-8"))
            assert isinstance(data, dict) and "linkarr" in data
        except (ValueError, AssertionError, UnicodeDecodeError):
            raise HTTPException(400, "That isn't a Linkarr backup file.")
        prefs.set_many(db, data.get("prefs") or {})
        for n, v in (data.get("secrets") or {}).items():
            if n in NAMES:
                ctx.vault.set(n, v)
        existing = {lib["path"] for lib in jobs.libraries(db)}
        added = 0
        for lib in data.get("libraries") or []:
            if lib.get("path") not in existing:
                try:
                    jobs.save_library(ctx, lib)
                    added += 1
                except JobError:
                    pass
        for s in data.get("shows") or []:
            db.run("INSERT OR IGNORE INTO shows (tmdb_id, name, folder, library_id, watching) VALUES (?,?,?,?,?)",
                   (s.get("tmdb_id"), s.get("name", ""), s.get("folder", ""), None, s.get("watching", 1)))
        for m in data.get("movies") or []:
            db.run("INSERT OR IGNORE INTO movies (tmdb_id, title, year, added_at) VALUES (?,?,?,?)",
                   (m.get("tmdb_id"), m.get("title", ""), m.get("year"), m.get("added_at") or now()))
        for w in data.get("watches") or []:
            if w.get("url") and w.get("title"):
                db.run("INSERT OR IGNORE INTO watches (url, name, title, year, tmdb_id, poster, folder, seen, episodes, status, "
                       "last_check, last_new, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (w["url"], w.get("name", ""), w["title"], w.get("year"), w.get("tmdb_id"), w.get("poster", ""),
                        w.get("folder", ""), w.get("seen", "[]"), w.get("episodes", 0), w.get("status", "active"),
                        w.get("last_check", 0), w.get("last_new") or now(), w.get("created_at") or now()))
        if not db.one("SELECT 1 FROM history LIMIT 1"):
            for h in reversed(data.get("history") or []):
                db.run("INSERT INTO history (ts, summary, moves, recycled, created_dirs, undone_at) VALUES (?,?,?,?,?,?)",
                       (h.get("ts", now()), h.get("summary", ""), h.get("moves", "[]"), h.get("recycled", "[]"),
                        h.get("created_dirs", "[]"), h.get("undone_at")))
        db.audit("restore", file.filename or "", ip_of(request))
        return {"ok": True, "libraries_added": added}

    # ---- front end ------------------------------------------------------------------------

    @app.get("/share")
    async def share(text: str = "", url: str = "", title: str = ""):
        # The phone's Share menu and the Send to Linkarr bookmark land here. The app reads ?share=,
        # shows what it found and waits for you to pick: opening this address never adds anything.
        from urllib.parse import quote
        shared = "\n".join(x for x in (title, text, url) if x and x.strip())
        return RedirectResponse("/?share=" + quote(shared[:6000]), status_code=303)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/sw.js")
    async def sw():
        return FileResponse(WEB / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    return app


def main() -> None:
    import uvicorn

    cfg = load()
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips=cfg.trusted_proxies)
