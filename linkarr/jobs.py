"""Jobs and links: what the web app and the Telegram bot both call.

A job is one title (a movie, or a show with one or more links). It starts as a draft you can
check and edit, then gets queued. The worker (worker.py) downloads, unpacks and moves it.

A captcha link (captcha.py) is held as a job with status 'needs_captcha' and no links until you
paste back the MEGA link; then a normal draft takes its place.
"""
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import captcha, decode, files, naming, prefs, tracker
from .config import Config
from .db import Database, now
from .mega import Mega, MegaError, find_links
from .parse import is_sub, is_video, parse_name
from .tmdb import Tmdb, TmdbError
from .vault import Vault

log = logging.getLogger("linkarr.jobs")

ACTIVE = ("queued", "downloading", "processing", "waiting", "needs_answer", "moving")


@dataclass
class Ctx:
    cfg: Config
    db: Database
    vault: Vault
    mega: Mega
    tmdb: Tmdb
    notify: object = None  # notify.Notifier, set by main
    subs: object = None    # subs.Subs, set by main
    status: dict = field(default_factory=dict)


class JobError(Exception):
    pass


# ---- libraries --------------------------------------------------------------------------------

def libraries(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM libraries ORDER BY position, id")]


def library(db: Database, lib_id) -> dict | None:
    r = db.one("SELECT * FROM libraries WHERE id = ?", (lib_id,)) if lib_id else None
    return dict(r) if r else None


def lib_dir(cfg: Config, lib: dict) -> Path:
    p = (cfg.media_dir / lib["path"].strip("/")).resolve()
    root = cfg.media_dir.resolve()
    if p != root and root not in p.parents:
        raise JobError("A library folder has to be inside the media folder.")
    return p


def work_dir(ctx: Ctx) -> Path:
    return ctx.cfg.media_dir / prefs.get(ctx.db, "work_dir")


def save_library(ctx: Ctx, data: dict, lib_id: int | None = None) -> dict:
    name = (data.get("name") or "").strip()[:60]
    kind = data.get("kind")
    path = (data.get("path") or "").strip().strip("/")
    if not name or kind not in ("movie", "series") or not path:
        raise JobError("A library needs a name, a type and a folder.")
    if ".." in path.split("/"):
        raise JobError("The folder has to be inside the media folder.")
    lib = {"name": name, "kind": kind, "path": path, "documentary": 1 if data.get("documentary") else 0,
           "plex_section": str(data.get("plex_section") or "").strip()[:20]}
    lib_dir(ctx.cfg, lib)
    if lib_id:
        ctx.db.run("UPDATE libraries SET name=?, kind=?, path=?, documentary=?, plex_section=? WHERE id=?",
                   (lib["name"], kind, path, lib["documentary"], lib["plex_section"], lib_id))
    else:
        pos = (ctx.db.one("SELECT max(position) AS p FROM libraries")["p"] or 0) + 1
        lib_id = ctx.db.run(
            "INSERT INTO libraries (name, kind, path, documentary, plex_section, position, created_at) VALUES (?,?,?,?,?,?,?)",
            (lib["name"], kind, path, lib["documentary"], lib["plex_section"], pos, now())).lastrowid
    return library(ctx.db, lib_id)


def pick_library(db: Database, kind: str, documentary: bool) -> dict | None:
    libs = [lib for lib in libraries(db) if lib["kind"] == kind]
    if documentary:
        docs = [lib for lib in libs if lib["documentary"]]
        if docs:
            return docs[0]
    plain = [lib for lib in libs if not lib["documentary"]]
    return (plain or libs or [None])[0]


# ---- views ------------------------------------------------------------------------------------

def link_view(r) -> dict:
    d = dict(r)
    d["files"] = json.loads(d.get("files") or "[]")
    d.pop("staging", None)
    return d


def job_view(db: Database, job_id: int) -> dict | None:
    r = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if r is None:
        return None
    job = dict(r)
    job["genres"] = json.loads(job.get("genres") or "[]")
    job["quality"] = json.loads(job.get("quality") or "null")
    job["quality_text"] = quality_text(job["quality"]) if job["quality"] else ""
    job.pop("archive_password", None)
    job["has_password"] = bool(r["archive_password"])
    job["links"] = [link_view(x) for x in db.query("SELECT * FROM links WHERE job_id = ? ORDER BY position, id", (job_id,))]
    watched = {r["url"]: r["status"] for r in db.query("SELECT url, status FROM watches")} if job["kind"] == "series" else {}
    t = now()
    for link in job["links"]:
        link["watch"] = watched.get(link["url"], "")
        if link["status"] == "queued" and link["not_before"] <= t:
            link["window_at"] = window_wait(db, link["size"])
    lib = library(db, job["library_id"])
    job["library"] = lib
    job["size"] = sum(link["size"] for link in job["links"] if link["status"] != "skipped")
    job["done_bytes"] = sum(min(link["bytes_done"], link["size"] or link["bytes_done"]) for link in job["links"]
                            if link["status"] != "skipped")
    job["moved"] = moved_summary(db, job_id)
    job["alerts"] = [dict(a) for a in db.query(
        "SELECT * FROM alerts WHERE job_id = ? AND resolved_at IS NULL ORDER BY id", (job_id,))]
    return job


def moved_summary(db: Database, job_id: int) -> dict | None:
    """How many files went where, for 'Moved 8 files to …' on finished downloads. None before anything moved."""
    rows = db.query("SELECT id, ts, moves FROM history WHERE job_id = ? AND undone_at IS NULL ORDER BY id", (job_id,))
    tos = [m["to"] for r in rows for m in json.loads(r["moves"] or "[]")]
    if not tos:
        return None
    parents = sorted({str(Path(t).parent) for t in tos})
    try:
        where = os.path.commonpath(parents)
    except ValueError:
        where = parents[0]
    videos = [t for t in tos if is_video(t)]
    return {"files": len(tos), "videos": len(videos), "where": where, "file": Path((videos or tos)[0]).name,
            "ts": rows[-1]["ts"], "history_id": rows[-1]["id"]}


def target_path(ctx: Ctx, job: dict) -> Path | None:
    lib = library(ctx.db, job["library_id"])
    if not lib:
        return None
    base = lib_dir(ctx.cfg, lib)
    if job["match_state"] in ("exact", "confirmed") and job["match_folder"]:
        return base / job["match_folder"]
    return base / job["folder_name"]


# ---- creating and editing ---------------------------------------------------------------------

def _season_of(name: str, files_: list) -> int | None:
    info = parse_name(name)
    if info["season"] is not None and len(info["seasons"]) <= 1:
        return info["season"]
    seasons = {parse_name(f["path"].rsplit("/", 1)[-1])["season"] for f in files_ if is_video(f["path"])}
    seasons.discard(None)
    return seasons.pop() if len(seasons) == 1 else None


def _preselect(files_: list) -> list:
    out = []
    for f in files_:
        junk = files.is_junk(Path(f["path"]), int(f.get("size", 0)))
        item = {"path": f["path"], "size": int(f.get("size", 0)), "selected": not junk, "junk": junk}
        if f.get("h"):
            item["h"] = f["h"]  # MEGA's id for the file, so a watched folder knows it already has it
        out.append(item)
    return out


async def identify(ctx: Ctx, name: str, files_: list | None = None, kind_hint: str | None = None) -> dict:
    """Best guess at what a release is: parsed name, then the top TMDB hit."""
    info = parse_name(name)
    if info["kind"] == "movie" and files_:
        vids = [f for f in files_ if is_video(f["path"])]
        if len(vids) > 2 and any(parse_name(f["path"].rsplit("/", 1)[-1])["episode"] is not None for f in vids):
            info["kind"] = "series"
    kind = kind_hint or info["kind"]
    guess = {"kind": kind, "title": info["title"], "year": info["year"], "tmdb_id": None, "poster": "",
             "genres": [], "documentary": False, "resolution": info["resolution"],
             "sure": False, "note": "Linkarr couldn't look this up on TMDB. Check the title.", "show_unknown": False}
    if not info["title"] and kind == "series":
        # 'Season 200 - The Science of Connection': a season with no show name. Nothing to look up.
        guess.update({"show_unknown": True, "note": "Linkarr can't tell which show this is. Use Match to show."})
        return guess
    if ctx.tmdb.configured() and info["title"]:
        try:
            hits = await ctx.tmdb.search(info["title"], kind, info["year"])
            if not hits and kind_hint is None:
                other = "series" if kind == "movie" else "movie"
                hits = await ctx.tmdb.search(info["title"], other, info["year"])
            if hits:
                best = max(hits[:5], key=lambda h: naming.score(info["title"], h["title"], info["year"]) + (0.05 if h["year"] == info["year"] else 0))
                det = await ctx.tmdb.details(best["kind"], best["id"])
                guess.update({"kind": best["kind"], "title": det["title"], "year": det["year"], "tmdb_id": det["id"],
                              "poster": det["poster"], "genres": det.get("genres", []), "documentary": det["documentary"]})
                guess["sure"], guess["note"] = how_sure(info, best, hits)
        except TmdbError as e:
            log.warning("TMDB lookup failed: %s", e)
    return guess


def how_sure(info: dict, best: dict, hits: list[dict]) -> tuple[bool, str]:
    """Is the TMDB pick certainly the right title? Not when the name doesn't really match, the year
    disagrees, or (with no year in the release name) TMDB has several titles of that name:
    remakes like Dune (1984) and Dune (2021), or Shameless (UK) and Shameless (US)."""
    if naming.score(info["title"], best["title"]) < naming.EXACT:
        return False, f"The release name doesn't clearly match “{best['title']}”. Check it's the right one."
    if info["year"]:
        if best["year"] and abs(best["year"] - info["year"]) <= 1:
            return True, ""
        return False, f"The release says {info['year']} but TMDB has {best['year'] or 'no year'}. Check it's the right one."
    same = {h["year"] for h in hits if h["kind"] == best["kind"] and naming.norm(h["title"]) == naming.norm(best["title"])}
    if len(same) > 1:
        years = ", ".join(str(y) for y in sorted(y for y in same if y))
        return False, f"There's more than one “{best['title']}” ({years}). Check it's the right one."
    return True, ""


def can_auto_start(ctx: Ctx, job_id: int) -> bool:
    """Sure of the title, has a library, and nothing to ask (no close match, not already owned)."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return bool(job and job["library_id"] and job["tmdb_id"] and not job["note"] and not open_question(dict(job)))


def start_all(ctx: Ctx, when: str = "now") -> dict:
    started, skipped = 0, 0
    for r in ctx.db.query("SELECT id, library_id FROM jobs WHERE status = 'draft' ORDER BY created_at"):
        if not r["library_id"]:
            skipped += 1
            continue
        start_job(ctx, r["id"], when)
        started += 1
    return {"started": started, "skipped": skipped}


def refresh_target(ctx: Ctx, job_id: int, keep_folder: bool = False) -> None:
    """Works out the folder name, looks for the title already in the library, and flags a clash."""
    job = dict(ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
    lib = library(ctx.db, job["library_id"])
    pattern = prefs.get(ctx.db, "movie_pattern" if job["kind"] == "movie" else "show_pattern")
    folder = job["folder_name"] if keep_folder and job["folder_name"] else (
        naming.movie_folder(job["title"], job["year"], pattern) if job["kind"] == "movie"
        else naming.show_folder(job["title"], job["year"], pattern))
    match_folder, score, state, conflict = "", 0.0, "none", ""
    if lib:
        existing = files.list_folders(lib_dir(ctx.cfg, lib))
        index = plex_index(ctx.db, lib["id"])
        by_id = [f for f, v in index.items() if job["tmdb_id"] and v.get("tmdb") == job["tmdb_id"] and f in existing]
        if by_id:
            # Plex already has this exact title (same TMDB id), whatever its folder is called.
            match_folder, score = by_id[0], 1.0
        elif folder in existing:
            match_folder, score = folder, 1.0
        else:
            match_folder, score = naming.best_match(job["title"], existing, job["year"])
        known = index.get(match_folder, {}).get("tmdb")
        if match_folder and job["tmdb_id"] and known and known != job["tmdb_id"]:
            match_folder, score = "", 0.0  # Plex says that folder is a different title
        if score >= naming.EXACT:
            state = "exact"
        elif score >= naming.CLOSE:
            state = "close"
        else:
            match_folder, score = "", 0.0
        if job["match_state"] in ("confirmed", "new") and job["match_folder"] == match_folder:
            state = job["match_state"]
        if job["kind"] == "movie":
            target = lib_dir(ctx.cfg, lib) / (match_folder if state in ("exact", "confirmed") else folder)
            if files.has_video(target):
                conflict = "exists"
    ctx.db.run("UPDATE jobs SET folder_name=?, match_folder=?, match_score=?, match_state=?, conflict=?, updated_at=? WHERE id=?",
               (folder, match_folder, score, state, conflict, now(), job_id))
    compare_quality(ctx, job_id)


def plex_index(db: Database, lib_id) -> dict:
    """What Plex says each folder in a library is: {folder: {"tmdb", "title", "year", "count"}}. Kept fresh by
    the worker; empty when Plex isn't set up."""
    return (db.get_setting("plex_index", {}) or {}).get(str(lib_id), {})


async def match_info(ctx: Ctx, job_id: int) -> dict:
    """For the 'same show?' question: what the folder already in your library is, with its year and
    poster, so you can tell 'Resident Evil (2022)' from 'Evil (2019)'."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None or not job["match_folder"]:
        return {}
    folder = job["match_folder"]
    plex = plex_index(ctx.db, job["library_id"]).get(folder, {})
    out = {"folder": folder, "title": plex.get("title") or naming.norm_title(folder), "year": plex.get("year") or naming.year_of(folder),
           "poster": "", "tmdb_id": plex.get("tmdb"), "from_plex": bool(plex)}
    show = ctx.db.one("SELECT tmdb_id FROM shows WHERE folder = ? AND library_id = ?", (folder, job["library_id"]))
    if not out["tmdb_id"] and show:
        out["tmdb_id"] = show["tmdb_id"]
    if ctx.tmdb.configured():
        try:
            if out["tmdb_id"]:
                det = await ctx.tmdb.details(job["kind"], out["tmdb_id"])
                out.update({"title": det["title"] or out["title"], "year": det["year"] or out["year"], "poster": det["poster"]})
            else:
                hits = await ctx.tmdb.search(out["title"], job["kind"], out["year"])
                if out["year"]:
                    hits = [h for h in hits if h["year"] and abs(h["year"] - out["year"]) <= 1] or hits
                if hits:
                    out.update({"poster": hits[0]["poster"], "year": out["year"] or hits[0]["year"]})
        except TmdbError:
            pass
    return out


RANK = {"480p": 1, "576p": 2, "720p": 3, "1080p": 4, "2160p": 5}


def compare_quality(ctx: Ctx, job_id: int) -> None:
    """For a movie you already have: is this link an upgrade? Stored on the job as
    {have, have_size, new, new_size, verdict} where verdict is better, worse, same, bigger or unknown."""
    job = dict(ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
    q = ""
    if job["kind"] == "movie" and job["conflict"] == "exists":
        have = files.video_quality(target_path(ctx, job)) or {"res": "", "size": 0}
        new_res, new_size = "", 0
        for link in ctx.db.query("SELECT name, files FROM links WHERE job_id = ? AND status != 'skipped'", (job_id,)):
            for f in json.loads(link["files"] or "[]"):
                if f.get("selected", True) and is_video(f["path"]) and f["size"] > new_size:
                    new_size = f["size"]
                    new_res = parse_name(f["path"].rsplit("/", 1)[-1])["resolution"] or parse_name(link["name"])["resolution"] or ""
        a, b = RANK.get(have["res"], 0), RANK.get(new_res, 0)
        if a and b and a != b:
            verdict = "better" if b > a else "worse"
        elif a and b:
            verdict = "bigger" if new_size > have["size"] * 1.3 else "same"
        else:
            verdict = "unknown"
        q = json.dumps({"have": have["res"], "have_size": have["size"], "new": new_res, "new_size": new_size, "verdict": verdict})
        if not job["conflict_choice"]:
            # Extras: the quality upgrade replaces a worse copy, the duplicate guard skips a copy that's no better.
            auto = ("replace" if verdict == "better" and prefs.get(ctx.db, "auto_upgrade") else
                    "skip" if verdict in ("worse", "same") and prefs.get(ctx.db, "dup_guard") else "")
            if auto:
                ctx.db.run("UPDATE jobs SET conflict_choice=? WHERE id=?", (auto, job_id))
    if q != job["quality"]:
        ctx.db.run("UPDATE jobs SET quality=? WHERE id=?", (q, job_id))


def quality_text(q: dict) -> str:
    """One line for the 'you already have this' question."""
    have, new = q.get("have") or "", q.get("new") or ""
    v = q.get("verdict")
    if v == "better":
        return f"You have it in {have}. This link is {new}, an upgrade."
    if v == "worse":
        return f"You already have it in {have}. This link is only {new}."
    if v == "same":
        return f"You already have it in {have}, the same as this link."
    if v == "bigger":
        return f"You have it in {have}. This link is {have} too but a bigger file, likely better quality."
    return "You already have this movie."


async def add_link(ctx: Ctx, url: str, job_id: int | None = None, kind_hint: str | None = None) -> dict:
    """Reads a link and makes a draft job (or adds the link to an existing job)."""
    urls = find_links(url)
    if not urls:
        if captcha.find(url, prefs.captcha_hosts(ctx.db)):
            raise JobError("That's a captcha link. Open it, solve the captcha, then paste the MEGA link it shows.")
        raise JobError("That isn't a MEGA link I can read.")
    url = urls[0]
    if ctx.db.one("SELECT 1 FROM links l JOIN jobs j ON j.id = l.job_id WHERE l.url = ? AND j.status IN ('draft',%s)"
                  % ",".join("?" * len(ACTIVE)), (url, *ACTIVE)):
        raise JobError("That link is already in the queue.")
    try:
        pv = await ctx.mega.preview(url)
    except MegaError as e:
        raise JobError(str(e)) from e
    flist = _preselect(pv.files)
    season = _season_of(pv.name, flist)
    t = now()
    new_job = False
    if job_id:
        job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if job is None:
            raise JobError("That job is gone.")
        if job["status"] == "needs_captcha":
            raise JobError("Finish the captcha first, then paste the MEGA link in the captcha box.")
    else:
        guess = await identify(ctx, pv.name, flist, kind_hint)
        warn = preview_warning(pv, flist)
        if warn:
            guess["note"] = warn
        lib = pick_library(ctx.db, guess["kind"], guess["documentary"])
        job_id = ctx.db.run(
            "INSERT INTO jobs (kind, title, year, tmdb_id, poster, genres, library_id, status, source, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (guess["kind"], guess["title"] or pv.name, guess["year"], guess["tmdb_id"], guess["poster"],
             json.dumps(guess["genres"]), lib["id"] if lib else None, "draft", "link", guess["note"], t, t)).lastrowid
        if guess.get("show_unknown"):
            ctx.db.run("UPDATE jobs SET show_unknown=1 WHERE id=?", (job_id,))
        new_job = True
    pos = (ctx.db.one("SELECT max(position) AS p FROM links WHERE job_id = ?", (job_id,))["p"] or 0) + 1
    ctx.db.run(
        "INSERT INTO links (job_id, url, name, season, size, files, hidden, position, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, url, pv.name, season, pv.size, json.dumps(flist), pv.unreadable, pos, t))
    # A link added to a job that's already running or finished starts straight away;
    # a draft waits for Start like the rest of it.
    ctx.db.run("UPDATE jobs SET status='queued', finished_at=NULL, error='', updated_at=? "
               "WHERE id=? AND status NOT IN ('draft','moving','needs_answer')", (t, job_id))
    refresh_target(ctx, job_id, keep_folder=True)
    if new_job:
        use_remembered(ctx, job_id, pv.name)
    if new_job and prefs.get(ctx.db, "auto_start") and can_auto_start(ctx, job_id):
        start_job(ctx, job_id)
        log.info("Started %s automatically", guess["title"])
    return job_view(ctx.db, job_id)


EDITABLE = {"title", "year", "kind", "library_id", "folder_name", "tmdb_id", "wait_all", "archive_password", "conflict_choice", "recheck"}


def _size(n: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} bytes"


def preview_warning(pv, flist: list) -> str:
    """What to tell you about a link before it downloads: files MEGA hid from the preview, or no video."""
    if pv.unreadable:
        n = pv.unreadable
        return (f"MEGA didn't let Linkarr read {n} file{'s' if n != 1 else ''} ({_size(pv.unreadable_size)}) in this link "
                "before downloading, so the name here may be wrong. Linkarr looks again once it's downloaded.")
    names = [f["path"] for f in flist]
    if not any(is_video(x) for x in names) and not any(files.is_archive(x) for x in names):
        return "There's no video in this link, only " + (", ".join(Path(x).name for x in names[:3]) or "nothing") + \
            (" and more" if len(names) > 3 else "") + "."
    return ""


async def recheck_after_download(ctx: Ctx, link: dict, staging: Path) -> None:
    """For a link MEGA only partly showed before downloading: list what really arrived, look the title up
    again from the biggest video, and hold the job until you've checked it."""
    flist = [{"path": str(f.relative_to(staging)), "size": f.stat().st_size, "selected": True, "junk": False}
             for f in files.walk_files(staging)]
    ctx.db.run("UPDATE links SET files=?, hidden=0, size=? WHERE id=?",
               (json.dumps(flist), sum(f["size"] for f in flist), link["id"]))
    vids = [f for f in flist if is_video(f["path"])]
    job = dict(ctx.db.one("SELECT * FROM jobs WHERE id = ?", (link["job_id"],)))
    if not vids:
        return
    biggest = max(vids, key=lambda f: f["size"])["path"]
    name = Path(biggest).name if parse_name(Path(biggest).name)["title"] else biggest.split("/")[0]
    guess = await identify(ctx, name, flist)
    if guess["tmdb_id"] and guess["tmdb_id"] == job["tmdb_id"]:
        ctx.db.run("UPDATE jobs SET note='' WHERE id=?", (job["id"],))
        return
    sets = {"title": guess["title"] or name, "year": guess["year"], "tmdb_id": guess["tmdb_id"], "poster": guess["poster"],
            "genres": json.dumps(guess["genres"]), "kind": guess["kind"], "recheck": 1, "match_state": "none",
            "note": f"Linkarr couldn't read this link fully before downloading. From the files that arrived it looks like "
                    f"{guess['title'] or name}{' (' + str(guess['year']) + ')' if guess['year'] else ''}. Check it before it's filed."}
    if guess["kind"] != job["kind"]:
        lib = pick_library(ctx.db, guess["kind"], guess["documentary"])
        sets["library_id"] = lib["id"] if lib else None
    cols = ", ".join(f"{k} = ?" for k in sets)
    ctx.db.run(f"UPDATE jobs SET {cols}, updated_at = ? WHERE id = ?", (*sets.values(), now(), job["id"]))
    refresh_target(ctx, job["id"])


async def update_job(ctx: Ctx, job_id: int, data: dict) -> dict:
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise JobError("That job is gone.")
    if job["status"] in ("moving", "done"):
        raise JobError("It's already been moved.")
    sets = {k: v for k, v in data.items() if k in EDITABLE}
    if "kind" in sets and sets["kind"] not in ("movie", "series"):
        raise JobError("Type must be movie or series.")
    if "recheck" in sets:
        if sets["recheck"]:
            raise JobError("Only clearing the check is allowed.")
        sets["note"] = ""
    if "conflict_choice" in sets and sets["conflict_choice"] not in ("", "replace", "keep", "skip"):
        raise JobError("Choose replace, keep both or skip.")
    if "library_id" in sets:
        lib = library(ctx.db, sets["library_id"])
        if lib is None:
            raise JobError("Pick a library.")
        sets["kind"] = lib["kind"]
    if {"tmdb_id", "title", "year"} & set(sets):
        sets["note"] = ""
        sets["recheck"] = 0  # you've corrected it yourself
    if {"tmdb_id", "title"} & set(sets):
        sets["show_unknown"] = 0
    if sets.get("tmdb_id") and ctx.tmdb.configured() and sets.get("tmdb_id") != job["tmdb_id"]:
        try:
            det = await ctx.tmdb.details(sets.get("kind", job["kind"]), int(sets["tmdb_id"]))
            sets.update({"title": det["title"], "year": det["year"], "poster": det["poster"], "genres": json.dumps(det.get("genres", []))})
        except TmdbError as e:
            raise JobError(str(e)) from e
    if "folder_name" in sets:
        sets["folder_name"] = naming.safe(sets["folder_name"])
    if sets:
        cols = ", ".join(f"{k} = ?" for k in sets)
        ctx.db.run(f"UPDATE jobs SET {cols}, updated_at = ? WHERE id = ?", (*sets.values(), now(), job_id))
    keep = "folder_name" in sets and not ({"title", "year", "tmdb_id", "kind"} & set(sets))
    if {"title", "year", "tmdb_id", "kind", "library_id", "folder_name"} & set(sets):
        if not keep:
            ctx.db.run("UPDATE jobs SET match_state='none' WHERE id=? AND match_state IN ('confirmed','new')", (job_id,))
        refresh_target(ctx, job_id, keep_folder=keep)
    if {"conflict_choice", "recheck", "title", "year", "tmdb_id"} & set(sets) or "match" in data:
        _maybe_release(ctx, job_id)
    return job_view(ctx.db, job_id)


def answer_match(ctx: Ctx, job_id: int, choice: str, folder: str = "") -> dict:
    """choice: 'use' the close match, 'new' folder, or 'pick' a named folder in the library."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise JobError("That job is gone.")
    if choice == "use":
        ctx.db.run("UPDATE jobs SET match_state='confirmed' WHERE id=?", (job_id,))
    elif choice == "new":
        ctx.db.run("UPDATE jobs SET match_state='new', match_folder='' WHERE id=?", (job_id,))
    elif choice == "pick":
        lib = library(ctx.db, job["library_id"])
        if not lib or folder not in files.list_folders(lib_dir(ctx.cfg, lib)):
            raise JobError("That folder isn't in the library.")
        ctx.db.run("UPDATE jobs SET match_state='confirmed', match_folder=? WHERE id=?", (folder, job_id))
    else:
        raise JobError("Unknown choice.")
    if job["kind"] == "movie":
        j = dict(ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
        tgt = target_path(ctx, j)
        ctx.db.run("UPDATE jobs SET conflict=? WHERE id=?", ("exists" if tgt and files.has_video(tgt) else "", job_id))
        compare_quality(ctx, job_id)
    _maybe_release(ctx, job_id)
    return job_view(ctx.db, job_id)


def _maybe_release(ctx: Ctx, job_id: int) -> None:
    """A job held for a question goes back to the worker once the question is answered."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job and job["status"] == "needs_answer" and not open_question(dict(job)):
        ctx.db.run("UPDATE jobs SET status='waiting', updated_at=? WHERE id=?", (now(), job_id))
        resolve_alerts(ctx.db, job_id=job_id, kinds=("match", "conflict", "recheck"))


def open_question(job: dict) -> str:
    if job.get("show_unknown"):
        return "show"
    if job.get("recheck"):
        return "recheck"
    if job["match_state"] == "close":
        return "match"
    if job["kind"] == "movie" and job["conflict"] == "exists" and not job["conflict_choice"]:
        return "conflict"
    return ""


def start_job(ctx: Ctx, job_id: int, when: str = "now") -> dict:
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise JobError("That job is gone.")
    if job["status"] == "needs_captcha":
        raise JobError("Finish the captcha first. Then paste the MEGA link it gave you.")
    if not job["library_id"]:
        raise JobError("Pick a library first. Add one in Settings if the list is empty.")
    if job["conflict_choice"] == "skip":
        return cancel_job(ctx, job_id)
    ctx.db.run("UPDATE jobs SET note='' WHERE id=?", (job_id,))
    not_before = night_time(ctx) if when == "tonight" else 0
    ctx.db.run("UPDATE links SET not_before=? WHERE job_id=? AND status='queued'", (not_before, job_id))
    if job["status"] in ("draft", "failed", "canceled"):
        ctx.db.run("UPDATE jobs SET status='queued', error='', updated_at=? WHERE id=?", (now(), job_id))
        for link in ctx.db.query("SELECT * FROM links WHERE job_id=? AND status='error'", (job_id,)):
            if can_reunpack(link):  # the archive is already here: just unpack it again
                ctx.db.run("UPDATE links SET status='downloaded', error='', error_kind='' WHERE id=?", (link["id"],))
        ctx.db.run("UPDATE links SET status='queued', error='', error_kind='' WHERE job_id=? AND status IN ('error')", (job_id,))
    return job_view(ctx.db, job_id)


def _minutes(hhmm: str) -> int:
    hh, mm = (hhmm or "00:00").split(":")[:2]
    return int(hh) * 60 + int(mm)


def window_wait(db: Database, size: int, at: float | None = None) -> int:
    """With the download window on, when a download this big may start: 0 for now (inside the window, or
    small enough), otherwise the time the window next opens."""
    import datetime as dt
    if not prefs.get(db, "window_enabled") or size < int(prefs.get(db, "window_min_gb")) * 1024**3:
        return 0
    start, end = _minutes(prefs.get(db, "window_start")), _minutes(prefs.get(db, "window_end"))
    n = dt.datetime.fromtimestamp(at if at is not None else dt.datetime.now().timestamp())
    m = n.hour * 60 + n.minute
    inside = start <= m < end if start < end else (m >= start or m < end) if start != end else True
    if inside:
        return 0
    opens = n.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if opens <= n:
        opens += dt.timedelta(days=1)
    return int(opens.timestamp())


def night_time(ctx: Ctx) -> int:
    import datetime as dt
    hh, mm = (prefs.get(ctx.db, "night_start") or "01:00").split(":")[:2]
    n = dt.datetime.now()
    at = n.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if at <= n:
        at += dt.timedelta(days=1)
    return int(at.timestamp())


def cancel_job(ctx: Ctx, job_id: int) -> dict:
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise JobError("That job is gone.")
    if job["status"] == "moving":
        raise JobError("It's being moved right now.")
    ctx.db.run("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (now(), job_id))
    ctx.db.run("UPDATE links SET status='skipped' WHERE job_id=? AND status IN ('queued','error')", (job_id,))
    if ctx.status.get("cancel"):
        for link in ctx.db.query("SELECT id FROM links WHERE job_id = ?", (job_id,)):
            ev = ctx.status["cancel"].get(link["id"])
            if ev:
                ev.set()
    resolve_alerts(ctx.db, job_id=job_id)
    cleanup_staging(ctx, job_id)
    forget_captcha_messages(ctx.db, job_id)
    return job_view(ctx.db, job_id)


def delete_job(ctx: Ctx, job_id: int) -> None:
    job = ctx.db.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        return
    if job["status"] in ACTIVE:
        cancel_job(ctx, job_id)
    cleanup_staging(ctx, job_id)
    forget_captcha_messages(ctx.db, job_id)
    ctx.db.run("DELETE FROM jobs WHERE id = ?", (job_id,))


def cleanup_staging(ctx: Ctx, job_id: int) -> None:
    for link in ctx.db.query("SELECT id, staging FROM links WHERE job_id = ?", (job_id,)):
        if link["staging"]:
            p = Path(link["staging"])
            if p.exists() and work_dir(ctx).resolve() in p.resolve().parents:
                shutil.rmtree(p, ignore_errors=True)


def can_reunpack(link) -> bool:
    """True when a link only failed at unpacking and its download is still there, so trying again
    can unpack it without fetching gigabytes from MEGA again."""
    if link["error_kind"] != "unpack" or not link["staging"]:
        return False
    root = Path(link["staging"])
    return root.is_dir() and bool(files.archive_sets(root))


def retry_link(ctx: Ctx, link_id: int) -> str:
    """'Fix it': unpack again if only unpacking failed and the archive is still here; otherwise throw
    away the partial download and fetch the link again from scratch. Returns 'unpack' or 'download'."""
    link = ctx.db.one("SELECT * FROM links WHERE id = ?", (link_id,))
    if link is None:
        raise JobError("That link is gone.")
    if can_reunpack(link):
        ctx.db.run("UPDATE links SET status='downloaded', error='', error_kind='' WHERE id=?", (link_id,))
        ctx.db.run("UPDATE jobs SET status='queued', error='', updated_at=? WHERE id=? AND status IN ('failed','waiting','needs_answer')",
                   (now(), link["job_id"]))
        resolve_alerts(ctx.db, link_id=link_id)
        return "unpack"
    if link["staging"]:
        shutil.rmtree(link["staging"], ignore_errors=True)
    ctx.db.run("UPDATE links SET status='queued', error='', error_kind='', bytes_done=0, staging='', not_before=0 WHERE id=?", (link_id,))
    ctx.db.run("UPDATE jobs SET status='queued', error='', updated_at=? WHERE id=? AND status IN ('failed','waiting','needs_answer')",
               (now(), link["job_id"]))
    resolve_alerts(ctx.db, link_id=link_id)
    return "download"


def skip_link(ctx: Ctx, link_id: int) -> None:
    link = ctx.db.one("SELECT * FROM links WHERE id = ?", (link_id,))
    if link is None:
        raise JobError("That link is gone.")
    if link["staging"]:
        shutil.rmtree(link["staging"], ignore_errors=True)
    ctx.db.run("UPDATE links SET status='skipped' WHERE id=?", (link_id,))
    ctx.db.run("UPDATE jobs SET status='waiting', updated_at=? WHERE id=? AND status='failed'", (now(), link["job_id"]))
    resolve_alerts(ctx.db, link_id=link_id)


def set_files(ctx: Ctx, link_id: int, selected: list[str]) -> None:
    link = ctx.db.one("SELECT files FROM links WHERE id = ?", (link_id,))
    if link is None:
        raise JobError("That link is gone.")
    keep = set(selected)
    fl = json.loads(link["files"])
    for f in fl:
        f["selected"] = f["path"] in keep
    ctx.db.run("UPDATE links SET files=? WHERE id=?", (json.dumps(fl), link_id))


def refile(ctx: Ctx, hid: int, lib_id: int, folder: str) -> dict:
    """'Move to…' in History: puts something already filed into another folder or library."""
    row = ctx.db.one("SELECT * FROM history WHERE id = ?", (hid,))
    if row is None or row["undone_at"]:
        raise JobError("That move was undone, so there's nothing to move.")
    moves = json.loads(row["moves"] or "[]")
    if not moves:
        raise JobError("Nothing was moved that time.")
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)) if row["job_id"] else None
    kind = job["kind"] if job else ("series" if any(files._SEASON_DIR.match(Path(m["to"]).parent.name) for m in moves) else "movie")
    lib = library(ctx.db, lib_id)
    if lib is None:
        raise JobError("Pick a library.")
    if lib["kind"] != kind:
        raise JobError(f"That's a {'TV' if kind == 'series' else 'movie'} download; pick a {'TV' if kind == 'series' else 'movie'} library.")
    folder = naming.safe(folder or "")
    if not folder or folder in (".", ".."):
        raise JobError("Type a folder name.")
    old_base = files.filed_base(moves, kind)
    new_base = lib_dir(ctx.cfg, lib) / folder
    if new_base == old_base:
        raise JobError("It's already in that folder.")
    new_moves, created, problems = files.refile(moves, old_base, new_base)
    if not new_moves:
        raise JobError(" ".join(problems) or "Nothing could be moved.")
    title = job["title"] if job else old_base.name
    n = len(new_moves)
    dirs = json.loads(row["created_dirs"] or "[]") + created
    ctx.db.run("UPDATE history SET moves=?, created_dirs=?, summary=? WHERE id=?",
               (json.dumps(new_moves), json.dumps(dirs), f"{title}: {n} file{'s' if n != 1 else ''} to {new_base} (moved from {old_base.name})", hid))
    if job:
        ctx.db.run("UPDATE jobs SET library_id=?, folder_name=?, match_folder='', match_state='new', updated_at=? WHERE id=?",
                   (lib["id"], folder, now(), job["id"]))
        if kind == "series" and job["tmdb_id"]:
            ctx.db.run("UPDATE shows SET folder=?, library_id=?, checked_at=0 WHERE tmdb_id=?", (folder, lib["id"], job["tmdb_id"]))
    ctx.db.audit("refile", f"history {hid} to {lib['name']}/{folder}")
    old_lib = next((x for x in libraries(ctx.db) if old_base.parent == lib_dir(ctx.cfg, x)), None)
    return {"moved": n, "where": str(new_base), "problems": problems,
            "sections": [x["plex_section"] for x in (old_lib, lib) if x and x["plex_section"]]}


# ---- Match to show ----------------------------------------------------------------------------
# A link like 'Season 200 - The Science of Connection' has a season but no show name. You pick the show
# (from TMDB, a folder you have, or a name you type) and can have Linkarr remember it for links named
# the same way. Fix show does the same for something already filed: it moves it into the right show
# folder and season, and deletes the old folders once they're empty.

ALIASES = "show_aliases"


def show_key(name: str) -> str | None:
    """What 'named like this' means: the show part of a series link name. '' for links with no show
    name ('Season 200 - …'). None for a movie."""
    info = parse_name(name or "")
    if info["kind"] != "series":
        return None
    return naming.norm(info["title"]) if info["title"] else ""


def key_label(key: str) -> str:
    return "Links named “Season N - …”" if key == "" else f"Links named “{key}”"


def remembered_shows(db: Database) -> list[dict]:
    out = []
    for key, show in (db.get_setting(ALIASES, {}) or {}).items():
        lib = library(db, show.get("library_id"))
        out.append({"key": key, "label": key_label(key), "title": show.get("title", ""), "year": show.get("year"),
                    "folder": show.get("folder", ""), "library": lib["name"] if lib else ""})
    return out


def forget_show(db: Database, key: str) -> None:
    aliases = db.get_setting(ALIASES, {}) or {}
    aliases.pop(key, None)
    db.set_setting(ALIASES, aliases)


def _first_link_name(db: Database, job_id) -> str:
    row = db.one("SELECT name FROM links WHERE job_id = ? ORDER BY position, id LIMIT 1", (job_id,)) if job_id else None
    return row["name"] if row else ""


def _series_library(db: Database, lib_id=None, current=None) -> dict:
    lib = library(db, lib_id) if lib_id else None
    if lib_id and (lib is None or lib["kind"] != "series"):
        raise JobError("Pick a TV library.")
    if lib is None and current:
        cur = library(db, current)
        lib = cur if cur and cur["kind"] == "series" else None
    lib = lib or pick_library(db, "series", False)
    if lib is None:
        raise JobError("Add a TV library in Settings first.")
    return lib


def _existing_folder(ctx: Ctx, lib: dict, title: str, year, tmdb_id) -> str:
    """The folder this show already has in the library: by Plex's TMDB id, the Series Tracker, or an exact name."""
    existing = files.list_folders(lib_dir(ctx.cfg, lib))
    if tmdb_id:
        by_id = [f for f, v in plex_index(ctx.db, lib["id"]).items() if v.get("tmdb") == tmdb_id and f in existing]
        if by_id:
            return by_id[0]
        row = ctx.db.one("SELECT folder FROM shows WHERE tmdb_id = ? AND library_id = ?", (tmdb_id, lib["id"]))
        if row and row["folder"] in existing:
            return row["folder"]
    name = naming.show_folder(title, year, prefs.get(ctx.db, "show_pattern"))
    if name in existing:
        return name
    best, score = naming.best_match(title, existing, year)
    return best if score >= naming.EXACT else ""


async def resolve_show(ctx: Ctx, choice: dict, current_lib=None) -> dict:
    """choice: {"tmdb_id"} or {"folder"} (a folder you have, or a new name), with an optional "library_id".
    Returns the show: title, year, tmdb_id, poster, genres, library_id and the folder it goes in."""
    lib = _series_library(ctx.db, choice.get("library_id"), current_lib)
    show = {"title": "", "year": None, "tmdb_id": None, "poster": "", "genres": [], "library_id": lib["id"], "folder": ""}
    if choice.get("tmdb_id"):
        if not ctx.tmdb.configured():
            raise JobError("Add a TMDB key in Settings first.")
        try:
            det = await ctx.tmdb.details("series", int(choice["tmdb_id"]))
        except TmdbError as e:
            raise JobError(str(e)) from e
        show.update({"title": det["title"], "year": det["year"], "tmdb_id": det["id"], "poster": det["poster"],
                     "genres": det.get("genres", [])})
        show["folder"] = _existing_folder(ctx, lib, det["title"], det["year"], det["id"]) or \
            naming.show_folder(det["title"], det["year"], prefs.get(ctx.db, "show_pattern"))
        return show
    raw = (choice.get("folder") or "").strip()
    folder = naming.safe(raw) if raw else ""
    if not folder or folder in (".", ".."):
        raise JobError("Pick a show or type a name.")
    show["folder"] = folder
    show["title"] = naming.norm_title(folder)
    show["year"] = naming.year_of(folder)
    if folder in files.list_folders(lib_dir(ctx.cfg, lib)):
        known = plex_index(ctx.db, lib["id"]).get(folder, {})
        row = ctx.db.one("SELECT tmdb_id FROM shows WHERE folder = ? AND library_id = ?", (folder, lib["id"]))
        show["tmdb_id"] = known.get("tmdb") or (row["tmdb_id"] if row else None)
        show["title"] = known.get("title") or show["title"]
        show["year"] = known.get("year") or show["year"]
    return show


def _fix_link_seasons(db: Database, job_id: int) -> None:
    """Season numbers read from the link names again (links added before 0.9.0 cut 'Season 200' to 20)."""
    for link in db.query("SELECT id, name, files, season FROM links WHERE job_id = ?", (job_id,)):
        s = _season_of(link["name"], json.loads(link["files"] or "[]"))
        if s is not None and s != link["season"]:
            db.run("UPDATE links SET season=? WHERE id=?", (s, link["id"]))


def apply_show(ctx: Ctx, job_id: int, show: dict) -> None:
    """Puts a job that hasn't been filed yet into a show."""
    ctx.db.run("UPDATE jobs SET kind='series', title=?, year=?, tmdb_id=?, poster=?, genres=?, library_id=?, folder_name=?, "
               "show_unknown=0, note='', recheck=0, match_state='none', match_folder='', conflict='', updated_at=? WHERE id=?",
               (show["title"] or show["folder"], show["year"], show["tmdb_id"], show["poster"], json.dumps(show.get("genres", [])),
                show["library_id"], show["folder"], now(), job_id))
    _fix_link_seasons(ctx.db, job_id)
    refresh_target(ctx, job_id, keep_folder=True)
    # You picked the folder, so there's nothing to ask about it.
    lib = library(ctx.db, show["library_id"])
    if lib and show["folder"] in files.list_folders(lib_dir(ctx.cfg, lib)):
        ctx.db.run("UPDATE jobs SET match_state='confirmed', match_folder=? WHERE id=?", (show["folder"], job_id))
    else:
        ctx.db.run("UPDATE jobs SET match_state='new', match_folder='' WHERE id=?", (job_id,))
    resolve_alerts(ctx.db, job_id=job_id, kinds=("show", "match"))
    _maybe_release(ctx, job_id)


def use_remembered(ctx: Ctx, job_id: int, name: str) -> bool:
    """A new link named like one you matched before ('Season N - …') goes to the same show."""
    key = show_key(name)
    show = (ctx.db.get_setting(ALIASES, {}) or {}).get(key) if key is not None else None
    if not show or not library(ctx.db, show.get("library_id")):
        return False
    apply_show(ctx, job_id, show)
    log.info("Matched %s to %s (remembered)", name, show["title"] or show["folder"])
    return True


def _waiting_alike(db: Database, key: str, skip_job=None) -> list[int]:
    out = []
    for r in db.query("SELECT id FROM jobs WHERE show_unknown=1 AND status NOT IN ('moving','done','canceled')"):
        if r["id"] != skip_job and show_key(_first_link_name(db, r["id"])) == key:
            out.append(r["id"])
    return out


def _history_key(row) -> str | None:
    """A filed download still sitting in a folder named like its link ('Season 200 - …')."""
    moves = json.loads(row["moves"] or "[]")
    if not moves or row["undone_at"]:
        return None
    return show_key(files.filed_base(moves, "series").name)


def _filed_alike(db: Database, key: str, skip_hid=None) -> list[int]:
    return [r["id"] for r in db.query("SELECT * FROM history WHERE undone_at IS NULL ORDER BY id")
            if r["id"] != skip_hid and _history_key(r) == key]


def alike(ctx: Ctx, job_id: int | None = None, hid: int | None = None) -> dict:
    """For the 'Do the same for every link named like this' box: how many more it would fix."""
    if hid is not None:
        row = ctx.db.one("SELECT * FROM history WHERE id = ?", (hid,))
        if row is None:
            raise JobError("That move is gone.")
        key = show_key(_first_link_name(ctx.db, row["job_id"])) if row["job_id"] else None
        if key is None:
            key = _history_key(row)
    else:
        key = show_key(_first_link_name(ctx.db, job_id))
    if key is None:
        return {"key": None, "label": "", "filed": 0, "waiting": 0}
    return {"key": key, "label": key_label(key), "filed": len(_filed_alike(ctx.db, key, hid)),
            "waiting": len(_waiting_alike(ctx.db, key, job_id))}


def _remember(ctx: Ctx, key: str, show: dict) -> None:
    aliases = ctx.db.get_setting(ALIASES, {}) or {}
    aliases[key] = {k: show.get(k) for k in ("title", "year", "tmdb_id", "poster", "genres", "library_id", "folder")}
    ctx.db.set_setting(ALIASES, aliases)


async def match_show(ctx: Ctx, job_id: int, choice: dict, remember: bool = False) -> dict:
    """Match to show on a job that hasn't been filed yet."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None:
        raise JobError("That job is gone.")
    if job["status"] in ("moving", "done", "canceled"):
        raise JobError("It's already been filed. Use Fix show in History.")
    show = await resolve_show(ctx, choice, job["library_id"])
    apply_show(ctx, job_id, show)
    out = {"job": job_view(ctx.db, job_id), "waiting": 0, "filed": 0, "sections": []}
    key = show_key(_first_link_name(ctx.db, job_id))
    if remember and key is not None:
        _remember(ctx, key, show)
        for other in _waiting_alike(ctx.db, key, job_id):
            apply_show(ctx, other, show)
            out["waiting"] += 1
        for hid in _filed_alike(ctx.db, key):
            res = _fix_filed(ctx, hid, show)
            out["filed"] += 1 if res["moved"] else 0
            out["sections"] += res["sections"]
    ctx.db.audit("match_show", f"job {job_id} to {show['folder']}")
    return out


def _move_seasons(ctx: Ctx, row, job) -> list:
    """The real season of each filed file: from its own name, then its link's name, then the folder it was filed in
    (which, for these, is the link name), then the season folder it sits in."""
    moves = json.loads(row["moves"] or "[]")
    old_base = files.filed_base(moves, "series")
    links = [dict(x) for x in ctx.db.query("SELECT * FROM links WHERE job_id = ?", (job["id"],))] if job else []
    base_season = parse_name(old_base.name)["season"]
    out = []
    for m in moves:
        s = parse_name(Path(m["to"]).name)["season"]
        if s is None:
            link = next((x for x in links if x["staging"] and m["from"].startswith(x["staging"].rstrip("/") + "/")), None)
            if link is None and len(links) == 1:
                link = links[0]
            if link:
                s = _season_of(link["name"], json.loads(link["files"] or "[]"))
        if s is None:
            s = base_season
        if s is None:
            s = parse_name(Path(m["to"]).parent.name)["season"]
        out.append(s)
    return out


def _fix_filed(ctx: Ctx, hid: int, show: dict) -> dict:
    row = ctx.db.one("SELECT * FROM history WHERE id = ?", (hid,))
    if row is None or row["undone_at"]:
        raise JobError("That move was undone, so there's nothing to fix.")
    moves = json.loads(row["moves"] or "[]")
    if not moves:
        raise JobError("Nothing was moved that time.")
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)) if row["job_id"] else None
    lib = library(ctx.db, show["library_id"])
    old_base = files.filed_base(moves, "series")
    new_base = lib_dir(ctx.cfg, lib) / show["folder"]
    keep = {ctx.cfg.media_dir} | {lib_dir(ctx.cfg, x) for x in libraries(ctx.db)}
    seasons = _move_seasons(ctx, row, job)
    new_moves, created, problems = files.refile_show(
        moves, seasons, old_base, new_base, show["title"] or show["folder"], prefs.get(ctx.db, "season_pattern"),
        prefs.get(ctx.db, "episode_pattern"), prefs.get(ctx.db, "rename_episodes"), keep)
    if not new_moves:
        return {"moved": 0, "where": str(new_base), "problems": problems or ["Nothing could be moved."], "sections": []}
    n = len(new_moves)
    dirs = json.loads(row["created_dirs"] or "[]") + created
    title = show["title"] or show["folder"]
    ctx.db.run("UPDATE history SET moves=?, created_dirs=?, summary=? WHERE id=?",
               (json.dumps(new_moves), json.dumps(dirs), f"{title}: {n} file{'s' if n != 1 else ''} to {new_base} (fixed from {old_base.name})", hid))
    if job:
        ctx.db.run("UPDATE jobs SET kind='series', title=?, year=?, tmdb_id=?, poster=?, genres=?, library_id=?, folder_name=?, "
                   "match_folder='', match_state='new', show_unknown=0, updated_at=? WHERE id=?",
                   (title, show["year"], show["tmdb_id"], show["poster"], json.dumps(show.get("genres", [])), lib["id"],
                    show["folder"], now(), job["id"]))
        _fix_link_seasons(ctx.db, job["id"])
    if show["tmdb_id"]:
        ctx.db.run("INSERT INTO shows (tmdb_id, name, folder, library_id) VALUES (?,?,?,?) "
                   "ON CONFLICT(tmdb_id) DO UPDATE SET folder=excluded.folder, library_id=excluded.library_id, checked_at=0",
                   (show["tmdb_id"], title, show["folder"], lib["id"]))
    old_lib = next((x for x in libraries(ctx.db) if old_base.parent == lib_dir(ctx.cfg, x)), None)
    return {"moved": n, "where": str(new_base), "problems": problems,
            "sections": [x["plex_section"] for x in (old_lib, lib) if x and x["plex_section"]]}


async def fix_show(ctx: Ctx, hid: int, choice: dict, remember: bool = False) -> dict:
    """Fix show in History: moves something already filed into the right show folder and season."""
    row = ctx.db.one("SELECT * FROM history WHERE id = ?", (hid,))
    if row is None or row["undone_at"]:
        raise JobError("That move was undone, so there's nothing to fix.")
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)) if row["job_id"] else None
    current = job["library_id"] if job else None
    show = await resolve_show(ctx, choice, current)
    info = alike(ctx, hid=hid)
    res = _fix_filed(ctx, hid, show)
    res.update({"filed": 0, "waiting": 0})
    if remember and info["key"] is not None:
        _remember(ctx, info["key"], show)
        for other in _filed_alike(ctx.db, info["key"], hid):
            r = _fix_filed(ctx, other, show)
            res["filed"] += 1 if r["moved"] else 0
            res["problems"] += r["problems"]
            res["sections"] += r["sections"]
        for jid in _waiting_alike(ctx.db, info["key"]):
            apply_show(ctx, jid, show)
            res["waiting"] += 1
    res["sections"] = sorted(set(res["sections"]))
    ctx.db.audit("fix_show", f"history {hid} to {show['folder']}")
    return res


# ---- captcha links ----------------------------------------------------------------------------

def hold_captcha(ctx: Ctx, text: str) -> dict:
    """A captcha link becomes a job on hold ('needs_captcha') until you paste back the MEGA link.
    The same link sent twice gives back the job already waiting for it."""
    if not prefs.get(ctx.db, "captcha_enabled"):
        raise JobError("Captcha links are off. Turn them on in Settings → Extras.")
    found = captcha.find(text, prefs.captcha_hosts(ctx.db))
    if not found:
        raise JobError("That isn't a captcha link Linkarr knows. Add its site in Settings → Extras.")
    url = found[0]
    old = ctx.db.one("SELECT id FROM jobs WHERE status = 'needs_captcha' AND hold_url = ?", (url,))
    if old:
        return dict(job_view(ctx.db, old["id"]), held_before=True)
    t = now()
    job_id = ctx.db.run(
        "INSERT INTO jobs (kind, title, status, source, hold_url, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("movie", "Captcha link", "needs_captcha", "captcha", url, t, t)).lastrowid
    log.info("Holding a captcha link from %s until you solve it (job %d)", captcha.host_of(url), job_id)
    return dict(job_view(ctx.db, job_id), held_before=False)


async def solve_captcha(ctx: Ctx, job_id: int, text: str) -> list[dict]:
    """You solved the captcha and pasted what the page gave you: a MEGA link (or a Base64 code with one in
    it). Each MEGA link becomes a normal draft, and the job on hold goes away."""
    job = ctx.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if job is None or job["status"] != "needs_captcha":
        raise JobError("That captcha link isn't waiting any more.")
    hosts = prefs.captcha_hosts(ctx.db)
    urls = find_links(text) or decode.unwrap(text, hosts=hosts)["links"]
    if not urls:
        if captcha.find(text, hosts):
            raise JobError("That's the captcha link again. Open it, solve the captcha, then paste the MEGA link it shows.")
        raise JobError("There's no MEGA link in that. Paste the MEGA link the page showed after the captcha.")
    added, problems = [], []
    for url in urls[:10]:
        try:
            added.append(await add_link(ctx, url))
        except JobError as e:
            problems.append(str(e))
    if not added:
        raise JobError(problems[0])
    forget_captcha_messages(ctx.db, job_id)
    ctx.db.run("DELETE FROM jobs WHERE id = ? AND status = 'needs_captcha'", (job_id,))
    log.info("Captcha link (job %d) solved: %d MEGA link%s added", job_id, len(added), "s" if len(added) != 1 else "")
    return added


def captcha_due(db: Database, at: int | None = None) -> list[dict]:
    """Captcha links that have waited a day and haven't had their reminder yet."""
    cutoff = (at if at is not None else now()) - captcha.REMIND_AFTER
    return [dict(r) for r in db.query(
        "SELECT * FROM jobs WHERE status = 'needs_captcha' AND reminded_at = 0 AND created_at <= ? ORDER BY created_at", (cutoff,))]


def remember_captcha_message(db: Database, chat: str, message_id, job_id: int) -> None:
    """So a Telegram reply to the captcha message is matched to its job."""
    if message_id:
        db.set_setting(f"tg_captcha.{chat}.{message_id}", job_id)


def captcha_job_for(db: Database, chat: str, message_id) -> int | None:
    job_id = db.get_setting(f"tg_captcha.{chat}.{message_id}") if message_id else None
    return int(job_id) if job_id else None


def forget_captcha_messages(db: Database, job_id: int) -> None:
    db.run("DELETE FROM settings WHERE key LIKE 'tg_captcha.%' AND value = ?", (json.dumps(job_id),))


# ---- alerts -----------------------------------------------------------------------------------

def add_alert(db: Database, kind: str, title: str, message: str = "", job_id=None, link_id=None) -> int | None:
    dup = db.one("SELECT id FROM alerts WHERE kind=? AND resolved_at IS NULL AND coalesce(job_id,0)=? AND coalesce(link_id,0)=?",
                 (kind, job_id or 0, link_id or 0))
    if dup:
        return None
    return db.run("INSERT INTO alerts (kind, job_id, link_id, title, message, created_at) VALUES (?,?,?,?,?,?)",
                  (kind, job_id, link_id, title, message, now())).lastrowid


def resolve_alerts(db: Database, job_id=None, link_id=None, kinds=None) -> None:
    sql, params = "UPDATE alerts SET resolved_at=? WHERE resolved_at IS NULL", [now()]
    if job_id:
        sql += " AND job_id=?"
        params.append(job_id)
    if link_id:
        sql += " AND link_id=?"
        params.append(link_id)
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params += list(kinds)
    db.run(sql, params)


def open_alerts(db: Database) -> list[dict]:
    return [dict(a) for a in db.query("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id DESC LIMIT 50")]


# ---- library search ---------------------------------------------------------------------------

async def search_library(ctx: Ctx, query: str, tmdb_id: int | None = None) -> list[dict]:
    """TMDB results with what you already have: the folder, and for shows the seasons on disk and the episodes
    you're missing. What you have comes first (tmdb_id, the show you came from in the Tracker, at the very top),
    then everything else by that name, like Dexter's spinoffs. Each result says which group it's in: 'mine' or
    'similar'."""
    q = query.strip()
    if not q:
        return []
    libs = libraries(ctx.db)
    folders = {lib["id"]: files.list_folders(lib_dir(ctx.cfg, lib)) for lib in libs}
    # Folders already known to be a particular title (Series Tracker, Plex), so "Dexter (2006)" isn't shown as
    # a maybe for "Dexter: New Blood".
    known = {(r["library_id"], r["folder"]): r["tmdb_id"] for r in ctx.db.query("SELECT tmdb_id, library_id, folder FROM shows WHERE folder != ''")}
    for lib in libs:
        for f, v in plex_index(ctx.db, lib["id"]).items():
            if v.get("tmdb"):
                known.setdefault((lib["id"], f), v["tmdb"])
    tracked = {r["tmdb_id"]: dict(r) for r in ctx.db.query("SELECT tmdb_id, folder, library_id, missing FROM shows")}
    results = []
    hits = []
    if ctx.tmdb.configured():
        try:
            hits = await ctx.tmdb.search(q)
        except TmdbError as e:
            log.warning("Search: %s", e)
    if tmdb_id and ctx.tmdb.configured() and not any(h["id"] == tmdb_id and h["kind"] == "series" for h in hits):
        try:  # the show you came from, even when TMDB's search lists it further down
            det = await ctx.tmdb.details("series", tmdb_id)
            hits.insert(0, {"id": det["id"], "kind": "series", "title": det["title"], "year": det["year"], "poster": det["poster"],
                            "overview": det.get("overview", ""), "documentary": det.get("documentary", False)})
        except TmdbError:
            pass
    if not hits:
        hits = [{"id": None, "kind": None, "title": q, "year": None, "poster": "", "overview": "", "documentary": False}]
    for h in hits[:12]:
        best = None
        show = tracked.get(h["id"]) if h["id"] and h["kind"] == "series" else None
        for lib in libs:
            if h["kind"] and lib["kind"] != h["kind"]:
                continue
            if show and show["library_id"] == lib["id"] and show["folder"] in folders[lib["id"]]:
                best = {"library_id": lib["id"], "library": lib["name"], "folder": show["folder"], "score": 1.0, "kind": lib["kind"]}
                break
            mine = [f for f in folders[lib["id"]] if known.get((lib["id"], f)) in (None, h["id"])]
            f, s = naming.best_match(h["title"], mine, h["year"])
            if s >= naming.CLOSE and (best is None or s > best["score"]):
                best = {"library_id": lib["id"], "library": lib["name"], "folder": f, "score": s, "kind": lib["kind"]}
        item = dict(h)
        item["have"] = best
        if best and best["kind"] == "series":
            lib = library(ctx.db, best["library_id"])
            item["seasons_on_disk"] = files.seasons_in(lib_dir(ctx.cfg, lib) / best["folder"])
        miss = (show or {}).get("missing") or ""
        item["missing"] = tracker.describe(json.loads(miss)) if best and miss not in ("", "null") else ""
        item["pinned"] = bool(tmdb_id and h["id"] == tmdb_id)
        results.append(item)
    if hits[0]["id"] is not None:
        # Yours first (the one you came from on top), then the rest in TMDB's order.
        order = {id(r): i for i, r in enumerate(results)}
        results.sort(key=lambda r: (not r["pinned"], not (r["have"] and r["have"]["score"] >= naming.EXACT),
                                    not r["have"], order[id(r)]))
        for r in results:
            r["group"] = "mine" if r["pinned"] or r["have"] else "similar"
    if hits and hits[0]["id"] is None:
        # No TMDB: also list folders whose names look like the query.
        results = []
        for lib in libs:
            for f in folders[lib["id"]]:
                s = naming.score(q, f)
                if s >= 0.6:
                    item = {"id": None, "kind": lib["kind"], "title": f, "year": naming.year_of(f), "poster": "", "overview": "",
                            "have": {"library_id": lib["id"], "library": lib["name"], "folder": f, "score": s, "kind": lib["kind"]}}
                    if lib["kind"] == "series":
                        item["seasons_on_disk"] = files.seasons_in(lib_dir(ctx.cfg, lib) / f)
                    item["group"] = "mine"
                    results.append(item)
        results.sort(key=lambda r: -r["have"]["score"])
    return results


# ---- series tracker import ---------------------------------------------------------------------

_TAGS = re.compile(r"\s*[\[{][^\]}]*[\]}]")  # '[tvdbid-123]', '{imdb-tt...}'


async def track_existing(ctx: Ctx, progress=None) -> dict:
    """Adds every show folder in your series libraries to the Series Tracker. A folder is only added when
    TMDB has a near-exact match; the rest come back in 'unmatched' so you can pick them by hand.
    Imported shows are marked so the first daily check stays quiet (no flood of 'has ended' messages)."""
    if not ctx.tmdb.configured():
        raise JobError("Add a TMDB key in Settings first.")
    known = {(r["library_id"], r["folder"]) for r in ctx.db.query("SELECT library_id, folder FROM shows")}
    todo = [(lib, f) for lib in libraries(ctx.db) if lib["kind"] == "series"
            for f in files.list_folders(lib_dir(ctx.cfg, lib)) if (lib["id"], f) not in known]
    out = {"total": len(todo), "done": 0, "added": 0, "unmatched": []}
    for lib, folder in todo:
        name = _TAGS.sub("", folder).strip()
        year = naming.year_of(name)
        title = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", name).strip() or name
        hit = None
        try:
            hits = await ctx.tmdb.search(title, "series", year)
            if hits:
                top = hits[0]
                sc = naming.score(top["title"], name, top["year"])
                # Same name, or a close name whose year matches the folder's year.
                if sc >= naming.EXACT or (sc >= 0.86 and year and top["year"] and abs(top["year"] - year) <= 1):
                    hit = top
        except TmdbError as e:
            log.warning("Series Tracker: %s: %s", folder, e)
        if hit:
            # INSERT OR IGNORE: a show you stopped tracking stays stopped.
            n = ctx.db.run("INSERT OR IGNORE INTO shows (tmdb_id, name, folder, library_id, watching, notified) VALUES (?,?,?,?,1,?)",
                           (hit["id"], hit["title"], folder, lib["id"], '{"_quiet": 1}')).rowcount
            out["added"] += n
        else:
            out["unmatched"].append({"folder": folder, "library": lib["name"]})
        out["done"] += 1
        if progress:
            progress(out)
    return out


async def show_details(ctx: Ctx, tmdb_id: int) -> dict:
    try:
        return await ctx.tmdb.details("series", tmdb_id)
    except TmdbError as e:
        raise JobError(str(e)) from e


def media_files(paths: list[str]) -> list[str]:
    return [p for p in paths if is_video(p) or is_sub(p)]
