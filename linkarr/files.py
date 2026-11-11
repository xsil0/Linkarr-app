"""Everything that touches files on the media share: unpacking, removing junk, checking a video
plays, and moving into a library without ever overwriting anything.

Rules: a file in a library is never replaced in place. When you choose to replace something,
the old copy goes to a recycle folder on the same share (kept 7 days by default), and every move
is recorded so it can be undone.
"""
import asyncio
import errno
import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import naming
from .parse import _SXE, ARCHIVE_EXT, is_sub, is_video, parse_name

log = logging.getLogger("linkarr.files")

JUNK_EXT = {".txt", ".nfo", ".url", ".lnk", ".exe", ".bat", ".cmd", ".scr", ".website", ".html", ".htm",
            ".sfv", ".md5", ".torrent", ".db", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ini", ".DS_Store"}
SAMPLE_MAX = 400 * 1024 * 1024
RECYCLE = ".linkarr-recycle"
_PART = re.compile(r"\.part(\d+)\.rar$", re.I)
_RNN = re.compile(r"\.r\d{2}$", re.I)
_LANG = {"english": "en", "eng": "en", "en": "en", "italian": "it", "ita": "it", "it": "it", "spanish": "es",
         "spa": "es", "es": "es", "french": "fr", "fre": "fr", "fra": "fr", "fr": "fr", "german": "de", "ger": "de", "de": "de",
         "forced": "forced", "sdh": "sdh"}


def walk_files(root: Path) -> list[Path]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            out.append(Path(dirpath) / f)
    return sorted(out)


def prune_empty(root: Path) -> None:
    for dirpath, _dirs, _files in sorted(os.walk(root), key=lambda t: -len(t[0])):
        p = Path(dirpath)
        if p != root:
            try:
                p.rmdir()
            except OSError:
                pass


# ---- archives ---------------------------------------------------------------------------------

def archive_sets(root: Path) -> list[tuple[Path, list[Path]]]:
    """(the file to open, every file belonging to it) for each archive in the folder."""
    files = walk_files(root)
    sets = []
    for f in files:
        name = f.name.lower()
        m = _PART.search(name)
        if m:
            if int(m.group(1)) != 1:
                continue
            stem = f.name[: m.start()]
            parts = [p for p in files if p.parent == f.parent and p.name.lower().startswith(stem.lower() + ".part") and p.name.lower().endswith(".rar")]
            sets.append((f, parts))
        elif name.endswith(".rar"):
            stem = f.name[:-4]
            parts = [f] + [p for p in files if p.parent == f.parent and p.name.startswith(stem) and _RNN.search(p.name)]
            sets.append((f, parts))
        elif f.suffix.lower() in (".zip", ".7z"):
            sets.append((f, [f]))
    return sets


async def _run(*args: str, timeout: float = 6 * 3600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode, out.decode("utf-8", "replace")


UNPACK_DIR = ".linkarr-unpacking"


def _commands(first: Path, dest: Path, password: str) -> list[list[str]]:
    """The tools to try, best first. unrar (RARLAB's own) for rar, 7-Zip for zip and 7z, unar as a
    last resort: unar can't read files over 2 GB inside a rar or AES-locked zips."""
    unrar, sevenzip, unar = shutil.which("unrar"), shutil.which("7z") or shutil.which("7zz"), shutil.which("unar")
    cmds = []
    if first.suffix.lower() == ".rar" and unrar:
        cmds.append([unrar, "x", "-y", "-o+", "-idc", "-idp", "-p" + (password or "-"), str(first), str(dest) + os.sep])
    if sevenzip:
        cmds.append([sevenzip, "x", "-y", "-aoa", "-bd", "-p" + password, "-o" + str(dest), str(first)])
    if unar:
        cmds.append([unar, "-q", "-f", "-D", "-o", str(dest)] + (["-p", password] if password else []) + [str(first)])
    return cmds


def _why(out: str, password: str) -> str:
    """Turns the unpacker's output into a plain reason."""
    low = out.lower()
    if "cannot find volume" in low or "missing volume" in low:
        return "a part of this archive is missing. Add the other parts to the job."
    if "incorrect password" in low or "wrong password" in low or "password" in low or "encrypted" in low:
        if password:
            return "the saved password is wrong. Check it on the job and try again."
        return "it needs a password. Add it on the job and try again."
    if "unexpected end of archive" in low or "more data than was available" in low or "truncated" in low:
        return "the download looks incomplete. Use Fix it to download it again."
    if "checksum error" in low or "crc failed" in low or "data error" in low:
        return ("the archive is damaged or the password is wrong." if password
                else "the archive is damaged. Use Fix it to download it again.")
    if "no space" in low or "disk full" in low:
        return "the disk is full."
    return _last_line(out)


def _last_line(out: str) -> str:
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "unknown error"


def _move_up(tmp: Path, dest: Path) -> None:
    """Moves what came out of an archive next to it. Same-named files are replaced, folders merged."""
    for item in sorted(tmp.iterdir()):
        target = dest / item.name
        if item.is_dir() and target.is_dir():
            _move_up(item, target)
            item.rmdir()
            continue
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        os.replace(item, target)


def _locked(out: str, password: str) -> bool:
    """Did the unpacker stop because of the password (missing or wrong)? A wrong password on a rar
    shows up as a checksum error, so with a password that counts too."""
    low = out.lower()
    if "password" in low or "encrypted" in low:
        return True
    return bool(password) and any(w in low for w in ("checksum error", "crc failed", "data error"))


async def unpack(root: Path, password: str = "", saved: list[str] | tuple = ()) -> list[str]:
    """Unpacks every archive in root next to itself and deletes the archive. Returns problems.
    `password` is the job's own; `saved` are the passwords from Settings, tried in order when the
    archive turns out to be locked and the job's own (or none) didn't open it."""
    tries = [password] + [p for p in saved if p and p != password]
    problems = []
    for first, parts in archive_sets(root):
        problem = ""
        for i, pw in enumerate(tries):
            problem, out = await _unpack_one(first, parts, pw)
            if not problem:
                if i:
                    log.info("Unpacked %s with saved password %d of %d", first.name, i, len(tries) - 1)
                    tries = [password, pw] + [p for p in tries[1:] if p != pw]  # the one that worked goes first next time
                break
            if not _locked(out, pw):
                break
        else:
            if len(tries) > 1:
                problem = (f"Couldn't unpack {first.name}: none of the saved passwords opened it. "
                           "Add the right one on the job and try again.")
        if problem:
            problems.append(problem)
    return problems


async def _unpack_one(first: Path, parts: list[Path], password: str) -> tuple[str, str]:
    """One archive with one password: ('', output) when it worked, else (the problem in plain words, output).
    Each archive goes into a scratch folder first, so a failed attempt leaves nothing half-written
    and the archive stays for the next try."""
    tmp = first.parent / UNPACK_DIR
    cmds = _commands(first, tmp, password)
    rc, out = 1, "No unpacker is installed. Use the official Linkarr image."
    failed = ""
    for cmd in cmds:
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        rc, out = await _run(*cmd)
        if rc == 0 and not any(tmp.iterdir()):
            rc, out = 1, out + "\nNo files extracted."
        if rc == 0:
            break
        failed = failed or out  # the best tool's answer is the one worth showing
        log.info("Unpacking %s with %s failed (exit %s)", first.name, Path(cmd[0]).name, rc)
        if _why(out, password) != _last_line(out):
            break  # a clear answer (password, missing part, incomplete): another tool won't do better
    out = (failed or out) if rc else out
    if not cmds and first.suffix.lower() == ".zip":
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        rc, out = await asyncio.to_thread(_unzip, first, password, tmp)
    if rc == 0:
        await asyncio.to_thread(_move_up, tmp, first.parent)
        shutil.rmtree(tmp, ignore_errors=True)
        for p in parts:
            p.unlink(missing_ok=True)
        log.info("Unpacked %s", first.name)
        return "", out
    shutil.rmtree(tmp, ignore_errors=True)
    if "no files extracted" in out.lower() and len(parts) == 1 and _PART.search(first.name):
        return f"Couldn't unpack {first.name}: a part of this archive is missing. Add the other parts to the job.", ""
    return f"Couldn't unpack {first.name}: {_why(out, password)}", out


def _unzip(path: Path, password: str, dest: Path) -> tuple[int, str]:
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            dest = dest.resolve()
            for info in z.infolist():
                target = (dest / info.filename).resolve()
                if dest not in target.parents and target != dest:
                    return 1, "unsafe path inside the zip"
            z.extractall(dest, pwd=password.encode() if password else None)
        return 0, ""
    except (zipfile.BadZipFile, RuntimeError, OSError) as e:
        return 1, str(e)


# ---- junk -------------------------------------------------------------------------------------

def is_junk(path: Path, size: int) -> bool:
    name = path.name.lower()
    if path.suffix.lower() in JUNK_EXT or name in ("thumbs.db", ".ds_store"):
        return True
    if is_video(name) and size < SAMPLE_MAX and (re.search(r"\bsample\b", name.replace(".", " ")) or "sample" in [p.lower() for p in path.parts[:-1]]):
        return True
    return False


def clean(root: Path) -> list[str]:
    removed = []
    for f in walk_files(root):
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if is_junk(f, size):
            f.unlink(missing_ok=True)
            removed.append(str(f.relative_to(root)))
    prune_empty(root)
    return removed


# ---- does it play? ----------------------------------------------------------------------------

async def probe(path: Path) -> float | None:
    """Length in seconds, None when ffprobe isn't available. Raises ValueError for a broken file."""
    tool = shutil.which("ffprobe")
    if not tool:
        return None
    rc, out = await _run(tool, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path), timeout=120)
    try:
        dur = float(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        dur = 0.0
    if rc != 0 or dur <= 0:
        raise ValueError(f"{path.name} doesn't look like a playable video.")
    return dur


_HEIGHTS = ((2000, "2160p"), (1000, "1080p"), (700, "720p"), (540, "576p"), (0, "480p"))


def video_quality(folder: Path) -> dict | None:
    """Resolution and size of the main (largest) video in a movie folder. The resolution comes from the
    file name, or ffprobe when the name doesn't say; '' when neither knows."""
    vids = [f for f in walk_files(folder) if is_video(f.name)] if folder.exists() else []
    if not vids:
        return None
    main = max(vids, key=lambda f: f.stat().st_size)
    res = parse_name(main.name)["resolution"] or parse_name(folder.name)["resolution"] or ""
    tool = shutil.which("ffprobe")
    if not res and tool:
        import subprocess
        try:
            out = subprocess.run([tool, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                                  "-of", "csv=p=0", str(main)], capture_output=True, text=True, timeout=20).stdout
            w, h = (int(x) for x in out.strip().split(",")[:2])
            # Wide films are letterboxed: 3840x1600 is still 4K, so go by width as well.
            h = max(h, w * 9 // 16)
            res = next(r for floor, r in _HEIGHTS if h >= floor)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {"res": res, "size": main.stat().st_size}


RANK = {"480p": 1, "576p": 2, "720p": 3, "1080p": 4, "2160p": 5}


def file_res(path: Path) -> str:
    """Resolution of one video: from its name, or ffprobe when the name doesn't say; '' when neither knows."""
    res = parse_name(path.name)["resolution"] or ""
    tool = shutil.which("ffprobe")
    if res or not tool or not path.exists():
        return res
    import subprocess
    try:
        out = subprocess.run([tool, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=20).stdout
        w, h = (int(x) for x in out.strip().split(",")[:2])
        h = max(h, w * 9 // 16)
        return next(r for floor, r in _HEIGHTS if h >= floor)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def episode_of(path: Path, base: Path | None = None) -> tuple[int, int] | None:
    """(season, episode) of a video, using a 'Season 2' folder above it when the name has no season."""
    info = parse_name(path.name)
    s, e = info["season"], info["episode"]
    if e is None:
        return None
    if s is None:
        parts = path.relative_to(base).parts[:-1] if base and base in path.parents else path.parts[:-1]
        for part in reversed(parts):
            s = parse_name(part)["season"]
            if s is not None:
                break
    return (s, e) if s is not None else None


def episode_files(show_dir: Path) -> dict:
    """{(season, episode): [video paths]} for a show folder."""
    out: dict = {}
    for f in walk_files(show_dir) if show_dir.exists() else []:
        if is_video(f.name):
            key = episode_of(f, show_dir)
            if key:
                out.setdefault(key, []).append(f)
    return out


def guard_episodes(plan: list[tuple[Path, Path]], show_dir: Path, skip_dupes: bool, upgrade: bool,
                   media_root: Path, job_id: int, record: dict) -> list[tuple[Path, Path]]:
    """Duplicate guard and quality upgrade for a show. An episode you already have is left out of the plan
    (its subtitles too), unless upgrade is on and the new copy has a higher resolution: then the old copy and
    its subtitles go to the recycle folder first. Adds 'skipped' and 'upgraded' (episode labels) to record."""
    have = episode_files(show_dir)
    if not have or not (skip_dupes or upgrade):
        return plan
    drop, out = set(), []
    for src, dst in plan:
        if not is_video(src.name):
            continue
        key = episode_of(src) or episode_of(dst, show_dir)
        if not key or key not in have:
            continue
        label = f"S{key[0]:02d}E{key[1]:02d}"
        old = have[key]
        new_rank = RANK.get(file_res(src), 0)
        old_rank = max(RANK.get(file_res(p), 0) for p in old)
        if upgrade and new_rank and new_rank > old_rank:
            for p in old:
                for f in [p] + [x for x in p.parent.iterdir() if x != p and is_sub(x.name) and x.name.startswith(p.stem + ".")]:
                    if f.exists():
                        dest = recycle_path(media_root, f, job_id)
                        move_file(f, dest)
                        record["recycled"].append({"from": str(f), "to": str(dest)})
            record.setdefault("upgraded", []).append(label)
            del have[key]
        elif skip_dupes:
            drop.add(src)
            record.setdefault("skipped", []).append(label)
    for src, dst in plan:
        if src in drop:
            continue
        if is_sub(src.name):
            twin = next((v for v in drop if v.parent == src.parent and src.name.startswith(v.stem)), None)
            key = episode_of(src)
            if twin or (key and any(episode_of(v) == key for v in drop)):
                continue
        out.append((src, dst))
    return out


# ---- planning where things go ---------------------------------------------------------------

def _sub_suffix(name: str) -> str:
    bits = [b.lower() for b in re.split(r"[ ._\-\[\]()]+", Path(name).stem) if b]
    tags = []
    for b in reversed(bits[-3:]):
        code = _LANG.get(b)
        if code and code not in tags:
            tags.insert(0, code)
    return "".join("." + t for t in tags)


def plan_movie(staged: list[tuple[Path, Path]], target: Path, base: str, keep_suffix: str = "") -> list[tuple[Path, Path]]:
    """staged: (file, its staging root). One main video is renamed to 'Title (Year).ext' and its
    subtitles follow it; with several videos (parts, extras) names are kept."""
    videos = [f for f, _ in staged if is_video(f.name)]
    out = []
    if len(videos) == 1:
        stem = base + (f" - {keep_suffix}" if keep_suffix else "")
        used = set()
        for f, _root in staged:
            if f == videos[0]:
                out.append((f, target / (stem + f.suffix.lower())))
            elif is_sub(f.name):
                name = stem + _sub_suffix(f.name)
                n, cand = 1, name + f.suffix.lower()
                while cand in used:
                    n += 1
                    cand = f"{name}.{n}{f.suffix.lower()}"
                used.add(cand)
                out.append((f, target / cand))
            else:
                out.append((f, target / f.name))
        return out
    for f, root in staged:
        rel = f.relative_to(root)
        # Drop the release's own top folder: the library folder replaces it.
        parts = rel.parts[1:] if len(rel.parts) > 1 else rel.parts
        out.append((f, target.joinpath(*parts)))
    return out


def plan_series(staged: list[tuple[Path, Path, int | None]], show_dir: Path, show_name: str, season_pattern: str,
                episode_pattern: str, rename_episodes: bool) -> list[tuple[Path, Path]]:
    """staged: (file, staging root, the link's season). Each video goes to Show/Season N/; with
    an episode number it's renamed 'Show - S01E02.ext' when renaming is on."""
    out = []
    for f, _root, link_season in staged:
        info = parse_name(f.name)
        season = info["season"] if info["season"] is not None else link_season
        if season is None:
            # A folder named 'Season 2' or 'S02' above the file.
            for part in reversed(f.parts[:-1]):
                p = parse_name(part)
                if p["season"] is not None:
                    season = p["season"]
                    break
        dest_dir = show_dir / naming.season_folder(season, season_pattern) if season is not None else show_dir
        if (is_video(f.name) or is_sub(f.name)) and rename_episodes and season is not None and info["episode"] is not None:
            extra = _sub_suffix(f.name) if is_sub(f.name) else ""
            stem = naming.fill(episode_pattern or naming.EPISODE_PATTERN, show=show_name, title=show_name,
                               season=season, episode=info["episode"])
            out.append((f, dest_dir / (stem + extra + f.suffix.lower())))
        else:
            out.append((f, dest_dir / f.name))
    return out


# ---- moving -----------------------------------------------------------------------------------

def _free_name(dst: Path, taken: set) -> Path:
    if not dst.exists() and dst not in taken:
        return dst
    n = 2
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists() and cand not in taken:
            return cand
        n += 1


def move_file(src: Path, dst: Path) -> None:
    """Rename when on the same disk/share, otherwise copy, check the size, then delete."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(str(dst))
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    tmp = dst.with_name(dst.name + ".linkarr-partial")
    shutil.copy2(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise OSError(f"Copy of {src.name} came out the wrong size.")
    os.rename(tmp, dst)
    src.unlink()


def recycle_path(media_root: Path, original: Path, job_id: int) -> Path:
    day = time.strftime("%Y-%m-%d")
    try:
        rel = original.relative_to(media_root)
    except ValueError:
        rel = Path(original.name)
    return media_root / RECYCLE / day / str(job_id) / rel


def apply_plan(plan: list[tuple[Path, Path]], media_root: Path, job_id: int, replace: bool) -> dict:
    """Moves everything. Returns {'moves': [...], 'recycled': [...], 'created_dirs': [...]} for undo."""
    record = {"moves": [], "recycled": [], "created_dirs": []}
    taken: set = set()
    for src, dst in plan:
        for parent in reversed(dst.parents):
            if not parent.exists() and str(parent) not in record["created_dirs"]:
                record["created_dirs"].append(str(parent))
        if dst.exists():
            if replace and (is_video(dst.name) or is_sub(dst.name)):
                old = recycle_path(media_root, dst, job_id)
                move_file(dst, old)
                record["recycled"].append({"from": str(dst), "to": str(old)})
            else:
                dst = _free_name(dst, taken)
        taken.add(dst)
        move_file(src, dst)
        record["moves"].append({"from": str(src), "to": str(dst)})
    return record


def recycle_old_videos(folder: Path, media_root: Path, job_id: int, record: dict) -> None:
    """For 'replace': the old movie files in the folder go to the recycle bin first."""
    if not folder.exists():
        return
    for f in walk_files(folder):
        if is_video(f.name) or is_sub(f.name):
            old = recycle_path(media_root, f, job_id)
            move_file(f, old)
            record["recycled"].append({"from": str(f), "to": str(old)})


def undo(record: dict, undone_dir: Path) -> list[str]:
    """Moves the new files out of the library (into undone_dir) and puts recycled files back."""
    problems = []
    for m in reversed(record.get("moves", [])):
        src = Path(m["to"])
        if not src.exists():
            problems.append(f"{src.name} isn't there any more.")
            continue
        dst = _free_name(undone_dir / src.name, set())
        try:
            move_file(src, dst)
        except OSError as e:
            problems.append(f"{src.name}: {e}")
    for r in record.get("recycled", []):
        src, dst = Path(r["to"]), Path(r["from"])
        if src.exists() and not dst.exists():
            try:
                move_file(src, dst)
            except OSError as e:
                problems.append(f"{src.name}: {e}")
    for d in sorted(record.get("created_dirs", []), key=len, reverse=True):
        try:
            Path(d).rmdir()
        except OSError:
            pass
    return problems


_SEASON_DIR = re.compile(r"^(season\s*\d+|specials)$", re.I)


def filed_base(moves: list[dict], kind: str) -> Path:
    """The folder something was filed into: the movie folder, or the show folder above its season folders."""
    parents = {Path(m["to"]).parent for m in moves}
    if kind == "series":
        parents = {p.parent if _SEASON_DIR.match(p.name) else p for p in parents}
    return Path(os.path.commonpath([str(p) for p in parents]))


def refile(moves: list[dict], old_base: Path, new_base: Path) -> tuple[list[dict], list[str], list[str]]:
    """Moves filed files from old_base to new_base, keeping where they sit under it (Season 1/…), along with
    subtitles saved next to them. Returns (new moves for the history row, folders created, problems)."""
    new_moves, created, problems, taken = [], [], [], set()
    for m in moves:
        src = Path(m["to"])
        if not src.exists():
            problems.append(f"{src.name} isn't there any more.")
            continue
        extra = [f for f in src.parent.iterdir() if f != src and f.is_file() and is_sub(f.name) and f.name.startswith(src.stem + ".")]
        for f in [src] + extra:
            if any(n["to"] == str(f) for n in moves if n is not m):
                continue  # filed in its own right; moved on its own turn
            try:
                rel = f.relative_to(old_base)
            except ValueError:
                rel = Path(f.name)
            dst = new_base / rel
            for parent in reversed(dst.parents):
                if not parent.exists() and str(parent) not in created:
                    created.append(str(parent))
            dst = _free_name(dst, taken)
            taken.add(dst)
            try:
                move_file(f, dst)
            except OSError as e:
                problems.append(f"{f.name}: {e}")
                continue
            new_moves.append({"from": m["from"] if f == src else str(f), "to": str(dst)})
    if old_base.exists():
        prune_empty(old_base)
        try:
            old_base.rmdir()
        except OSError:
            pass
    return new_moves, created, problems


def refile_show(moves: list[dict], seasons: list, old_base: Path, new_base: Path, show_name: str, season_pattern: str,
                episode_pattern: str, rename: bool, keep: set) -> tuple[list[dict], list[str], list[str]]:
    """Fix show: moves filed files into new_base/Season N/ using the real season of each (seasons, one per move),
    renamed 'Show - S200E01' when episode renaming is on and the file has an episode number. Subtitles next to a
    video follow it. Afterwards the old folders are deleted, but only the ones left empty, and never a folder in
    keep (the media folder and the library folders). Returns (new moves, folders created, problems)."""
    new_moves, created, problems, taken = [], [], [], set()
    filed = {m["to"] for m in moves}
    for m, season in zip(moves, seasons):
        src = Path(m["to"])
        if not src.exists():
            problems.append(f"{src.name} isn't there any more.")
            continue
        if season is not None:
            dest_dir = new_base / naming.season_folder(season, season_pattern)
        else:
            try:
                dest_dir = new_base / src.relative_to(old_base).parent
            except ValueError:
                dest_dir = new_base
        ep = parse_name(src.name)["episode"]
        stem = src.stem
        if rename and season is not None and ep is not None and (is_video(src.name) or is_sub(src.name)):
            stem = naming.fill(episode_pattern or naming.EPISODE_PATTERN, show=show_name, title=show_name, season=season, episode=ep)
            if is_sub(src.name):
                stem += _sub_suffix(src.name)
        pairs = [(src, dest_dir / (stem + src.suffix.lower() if stem != src.stem else src.name))]
        if is_video(src.name):
            for f in src.parent.iterdir():
                if f != src and f.is_file() and is_sub(f.name) and f.name.startswith(src.stem + ".") and str(f) not in filed:
                    pairs.append((f, dest_dir / (stem + f.name[len(src.stem):])))
        for f, dst in pairs:
            if f.parent == dst.parent and f.name == dst.name:
                new_moves.append({"from": m["from"] if f == src else str(f), "to": str(f)})
                continue
            for parent in reversed(dst.parents):
                if not parent.exists() and str(parent) not in created:
                    created.append(str(parent))
            dst = _free_name(dst, taken)
            taken.add(dst)
            try:
                move_file(f, dst)
            except OSError as e:
                problems.append(f"{f.name}: {e}")
                continue
            new_moves.append({"from": m["from"] if f == src else str(f), "to": str(dst)})
    if old_base != new_base and old_base not in keep and old_base.exists():
        prune_empty(old_base)
        try:
            old_base.rmdir()
        except OSError:
            pass  # something else is still in it, so it stays
    elif old_base == new_base:
        prune_empty(old_base)  # the old 'Season 20' folder
    return new_moves, created, problems


def purge_recycle(media_root: Path, days: int) -> int:
    root = media_root / RECYCLE
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for day in root.iterdir():
        try:
            when = time.mktime(time.strptime(day.name, "%Y-%m-%d"))
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(day, ignore_errors=True)
            n += 1
    return n


def list_folders(path: Path) -> list[str]:
    try:
        return sorted(p.name for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def seasons_in(show_dir: Path) -> dict:
    """{season number: number of video files} for a show folder."""
    out: dict = {}
    for f in walk_files(show_dir) if show_dir.exists() else []:
        if not is_video(f.name):
            continue
        info = parse_name(f.name)
        s = info["season"]
        if s is None:
            for part in reversed(f.relative_to(show_dir).parts[:-1]):
                p = parse_name(part)
                if p["season"] is not None:
                    s = p["season"]
                    break
        if s is not None:
            out[s] = out.get(s, 0) + 1
    return dict(sorted(out.items()))


def episodes_in(show_dir: Path) -> dict:
    """{season: {"eps": set of episode numbers, "unknown": videos with no episode number}}.
    'Show - S01E01E02' and 'S01E01-02' count as both episodes."""
    out: dict = {}
    for f in walk_files(show_dir) if show_dir.exists() else []:
        if not is_video(f.name):
            continue
        info = parse_name(f.name)
        s, e = info["season"], info["episode"]
        if s is None:
            for part in reversed(f.relative_to(show_dir).parts[:-1]):
                s = parse_name(part)["season"]
                if s is not None:
                    break
        if s is None:
            continue
        slot = out.setdefault(s, {"eps": set(), "unknown": 0})
        if e is None:
            slot["unknown"] += 1
            continue
        slot["eps"].add(e)
        m = _SXE.search(re.sub(r"[._]+", " ", f.name))
        if m and m.group(3):
            last = int(m.group(3))
            if e < last <= e + 5:
                slot["eps"].update(range(e + 1, last + 1))
    return dict(sorted(out.items()))


def has_video(folder: Path) -> list[str]:
    return [f.name for f in walk_files(folder)] if folder.exists() and any(is_video(f.name) for f in walk_files(folder)) else []


def tree_size(path: Path) -> int:
    return sum(f.stat().st_size for f in walk_files(path) if f.exists())


def is_archive(name: str) -> bool:
    return Path(name).suffix.lower() in ARCHIVE_EXT
