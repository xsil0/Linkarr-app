"""Folder watch: a MEGA folder link that keeps getting new episodes (an uploader adding each week's
episode to the same folder) is checked on a schedule. Only files Linkarr hasn't seen before are
downloaded, one small link per file, and filed into the show's folder like any other download.

Off until you turn it on in Settings → Extras. A watch stops by itself when the link dies or after a
set number of days with nothing new, and Linkarr tells you either way.
"""
import json
import logging
from pathlib import Path

from . import files, jobs, prefs
from .db import now
from .mega import MegaError, file_in_folder, is_folder
from .parse import is_sub, is_video, parse_name

log = logging.getLogger("linkarr.watch")


class WatchError(Exception):
    pass


def _wanted(f: dict) -> bool:
    """Files worth downloading from a watched folder: videos, subtitles and archives that aren't junk."""
    p = f["path"]
    return (is_video(p) or is_sub(p) or files.is_archive(p)) and not files.is_junk(Path(p), int(f.get("size", 0)))


def view(r) -> dict:
    d = dict(r)
    d.pop("seen", None)
    return d


def all_watches(db) -> list[dict]:
    return [view(r) for r in db.query("SELECT * FROM watches ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, title")]


def add(ctx, link_id: int) -> dict:
    """Watch the folder link of a show you've added. What's in the folder now belongs to that download;
    anything that turns up later is new."""
    if not prefs.get(ctx.db, "watch_enabled"):
        raise WatchError("Monitored links are off. Turn them on in Settings → Extras.")
    link = ctx.db.one("SELECT * FROM links WHERE id = ?", (link_id,))
    if link is None:
        raise WatchError("That link is gone.")
    if not is_folder(link["url"]):
        raise WatchError("Only folder links can be monitored. A link to one file never changes.")
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (link["job_id"],))
    if job["kind"] != "series":
        raise WatchError("Only shows can be monitored. Change the type to Series first.")
    flist = json.loads(link["files"] or "[]")
    seen = sorted({f["path"] for f in flist} | {f["h"] for f in flist if f.get("h")})
    folder = job["match_folder"] if job["match_state"] in ("exact", "confirmed") and job["match_folder"] else job["folder_name"]
    eps = sum(1 for f in flist if is_video(f["path"]) and f.get("selected", True))
    t = now()
    ctx.db.run(
        "INSERT INTO watches (url, name, job_id, title, year, tmdb_id, poster, library_id, folder, seen, episodes, last_check, last_new, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET status='active', stop_reason='', error='', "
        "job_id=excluded.job_id, last_new=excluded.last_new",
        (link["url"], link["name"], job["id"], job["title"], job["year"], job["tmdb_id"], job["poster"], job["library_id"],
         folder, json.dumps(seen), eps, t, t, t))
    return view(ctx.db.one("SELECT * FROM watches WHERE url = ?", (link["url"],)))


def set_active(ctx, watch_id: int, active: bool) -> dict:
    row = ctx.db.one("SELECT * FROM watches WHERE id = ?", (watch_id,))
    if row is None:
        raise WatchError("That monitored link is gone.")
    if active and row["status"] == "dead":
        raise WatchError("This link is gone. Send the new link with /monitor, or add it and press Monitor this link.")
    # Resuming restarts the idle clock, so it isn't stopped again on the next check.
    ctx.db.run("UPDATE watches SET status=?, stop_reason=?, last_new=CASE WHEN ? THEN ? ELSE last_new END WHERE id=?",
               ("active" if active else "stopped", "" if active else "you", 1 if active else 0, now(), watch_id))
    return view(ctx.db.one("SELECT * FROM watches WHERE id = ?", (watch_id,)))


def remove(ctx, watch_id: int) -> None:
    ctx.db.run("DELETE FROM watches WHERE id = ?", (watch_id,))


def due(db, limit: int = 5) -> list[dict]:
    every = int(prefs.get(db, "watch_every")) * 3600
    return [dict(r) for r in db.query("SELECT * FROM watches WHERE status='active' AND last_check <= ? ORDER BY last_check LIMIT ?",
                                      (now() - every, limit))]


async def check(ctx, w: dict) -> list[str]:
    """Look at one watched folder. Returns the messages to send (one per thing that happened)."""
    db = ctx.db
    t = now()
    stop_days = int(prefs.get(db, "watch_stop_days"))
    try:
        pv = await ctx.mega.preview(w["url"])
    except MegaError as e:
        if e.kind == "dead":
            db.run("UPDATE watches SET status='dead', stop_reason='dead', error=?, last_check=? WHERE id=?", (str(e)[:300], t, w["id"]))
            log.info("Watched folder for %s is gone", w["title"])
            return [f"⚠️ <b>Stopped monitoring {_e(w['title'])}</b>\nThe MEGA folder link is gone.\nSend the new link with /monitor."]
        # MEGA busy or unreachable: try again in an hour, not at the next full interval.
        retry = t - int(prefs.get(db, "watch_every")) * 3600 + 3600
        db.run("UPDATE watches SET error=?, last_check=? WHERE id=?", (str(e)[:300], retry, w["id"]))
        log.info("Couldn't check the watched folder for %s: %s", w["title"], e)
        return []
    seen = set(json.loads(w["seen"] or "[]"))
    new = [f for f in pv.files if f["path"] not in seen and not (f.get("h") and f["h"] in seen)]
    for f in new:
        seen.add(f["path"])
        if f.get("h"):
            seen.add(f["h"])
    wanted = [f for f in new if _wanted(f)]
    busy = {r["url"] for r in db.query(
        "SELECT l.url FROM links l JOIN jobs j ON j.id = l.job_id WHERE j.status IN ('draft',%s)" % ",".join("?" * len(jobs.ACTIVE)),
        jobs.ACTIVE)}
    if not wanted:
        db.run("UPDATE watches SET seen=?, error='', last_check=? WHERE id=?", (json.dumps(sorted(seen)), t, w["id"]))
        if stop_days and w["last_new"] and t - w["last_new"] > stop_days * 86400:
            db.run("UPDATE watches SET status='stopped', stop_reason='idle' WHERE id=?", (w["id"],))
            return [f"💤 <b>Stopped monitoring {_e(w['title'])}</b>\nNothing new for {stop_days} days.\nResume it in Tracker → Monitored links."]
        return []
    job_id = queue(ctx, w, wanted, pv.name, busy)
    eps = [f for f in wanted if is_video(f["path"])]
    if job_id is None:
        if not all(f.get("h") for f in wanted):
            # The whole folder is still downloading in another job: look again next time.
            db.run("UPDATE watches SET error='', last_check=? WHERE id=?", (t, w["id"]))
        else:  # every new file is already in the queue
            db.run("UPDATE watches SET seen=?, error='', last_check=? WHERE id=?", (json.dumps(sorted(seen)), t, w["id"]))
        return []
    db.run("UPDATE watches SET seen=?, error='', last_check=?, last_new=?, episodes=episodes+? WHERE id=?",
           (json.dumps(sorted(seen)), t, t, len(eps), w["id"]))
    seasons = sorted({parse_name(Path(f["path"]).name)["season"] for f in eps} - {None})
    job = db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    lines = [f"📺 <b>{_e(w['title'])}</b>",
             f"{len(eps) or len(wanted)} new {'episode' if len(eps) == 1 else 'episodes' if eps else 'files'}"
             + (f" in season {seasons[0]}" if len(seasons) == 1 else "")]
    lines.append("Downloading now." if job and job["status"] != "draft" else "Check it in Linkarr before it starts.")
    return ["\n".join(lines)]


def queue(ctx, w: dict, wanted: list[dict], folder_name: str, busy: set) -> int | None:
    """One job for what's new: a link per file when MEGA gave file ids (only those files download), or the
    whole folder link with just the new files ticked."""
    db = ctx.db
    t = now()
    if all(f.get("h") for f in wanted):
        links = [(file_in_folder(w["url"], f["h"]), Path(f["path"]).name, int(f["size"]),
                  [{"path": Path(f["path"]).name, "size": int(f["size"]), "selected": True, "junk": False}]) for f in wanted]
    else:
        pv_paths = {f["path"] for f in wanted}
        flist = json.loads(db.one("SELECT files FROM links WHERE url = ? ORDER BY id DESC", (w["url"],))["files"] or "[]") \
            if db.one("SELECT 1 FROM links WHERE url = ?", (w["url"],)) else []
        allf = [{"path": f["path"], "size": int(f.get("size", 0)), "selected": False, "junk": False} for f in flist
                if f["path"] not in pv_paths]
        allf += [{"path": f["path"], "size": int(f["size"]), "selected": True, "junk": False} for f in wanted]
        links = [(w["url"], folder_name, sum(f["size"] for f in allf), allf)]
    links = [x for x in links if x[0] not in busy]
    if not links:
        return None
    lib = jobs.library(db, w["library_id"]) or jobs.pick_library(db, "series", False)
    job_id = db.run(
        "INSERT INTO jobs (kind, title, year, tmdb_id, poster, library_id, folder_name, status, source, created_at, updated_at) "
        "VALUES ('series',?,?,?,?,?,?,'draft','watch',?,?)",
        (w["title"], w["year"], w["tmdb_id"], w["poster"], lib["id"] if lib else None, w["folder"], t, t)).lastrowid
    for pos, (url, name, size, flist) in enumerate(links, 1):
        season = jobs._season_of(name, flist)
        db.run("INSERT INTO links (job_id, url, name, season, size, files, position, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (job_id, url, name, season, size, json.dumps(flist), pos, t))
    jobs.refresh_target(ctx, job_id, keep_folder=True)
    if lib and w["folder"]:
        try:
            if (jobs.lib_dir(ctx.cfg, lib) / w["folder"]).is_dir():
                # The watch knows where the show lives: no "same show?" question for it.
                db.run("UPDATE jobs SET match_folder=?, match_state='confirmed', match_score=1 WHERE id=?", (w["folder"], job_id))
        except jobs.JobError:
            pass
    if lib and not jobs.open_question(dict(db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))):
        jobs.start_job(ctx, job_id)
    else:
        jobs.add_alert(db, "inbox", "New episodes in a monitored link", f"{w['title']}: check the details and start it.", job_id)
    db.run("UPDATE watches SET job_id=? WHERE id=?", (job_id, w["id"]))
    log.info("Watched folder for %s: %d new link(s)", w["title"], len(links))
    return job_id


def after_move(db, job: dict, target: Path) -> None:
    """Once a watched show is filed, remember its folder so new episodes go to the same place."""
    db.run("UPDATE watches SET folder=?, library_id=? WHERE job_id=?", (target.name, job["library_id"], job["id"]))


def _e(s) -> str:
    import html
    return html.escape(str(s or ""))
