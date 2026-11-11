"""Reads a whole post you paste or share: every MEGA link in it, what each one is, and which ones
to tick by default. Linkarr never opens the page the text came from; it only reads what you give it.

Posts usually come in one of two shapes: one link per season, or the same thing in several
qualities. So links are grouped by title and season, and in each group only the best quality is
ticked. Links Linkarr can't tell apart stay ticked, so "Add" on an ordinary post takes everything.
"""
import asyncio
import re

from . import captcha, decode
from .mega import MegaError, find_links
from .parse import parse_name

MAX_LINKS = 40
_RES_ORDER = {"2160p": 4, "1080p": 3, "720p": 2, "576p": 1, "480p": 0}
_RES = re.compile(r"\b(2160p|1080p|720p|576p|480p|4k|uhd)\b", re.I)
_PASSWORD = re.compile(r"\b(?:password|passwd|pass|pw|pwd)\b\s*(?:is\b|[:=\-])\s*([^\s<>\"]{1,100})", re.I)
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_=-])")
_URL = re.compile(r"https?://\S+", re.I)


def _norm_res(text: str) -> str | None:
    m = _RES.search(text or "")
    if not m:
        return None
    r = m.group(1).lower()
    return "2160p" if r in ("4k", "uhd") else r


def find_password(text: str) -> str:
    """The archive password a post mentions ('Password: abc'), or ''. Only ever a suggestion."""
    m = _PASSWORD.search(text or "")
    return m.group(1).strip(".,;") if m else ""


def _labels(text: str, urls: list[str]) -> dict[str, str]:
    """What the post says next to each link: the words before it on its line, or the line above."""
    out: dict[str, str] = {}
    lines = (text or "").splitlines()
    prev = ""
    for line in lines:
        hits = find_links(decode._with_scheme(line))
        words = _URL.sub(" ", line)
        words = re.sub(r"[#\w/.-]*mega(?:\.co)?\.nz\S*", " ", words, flags=re.I)
        words = re.sub(r"\s+", " ", words).strip(" :-–|>*[]")
        for u in hits:
            if u in urls and u not in out:
                out[u] = (words or prev)[:80]
        if not hits and line.strip():
            prev = re.sub(r"\s+", " ", line).strip(" :-–|>*[]")
        elif not line.strip():
            prev = ""
    return out


def links_in(text: str, hosts: list[str]) -> tuple[list[str], list[str]]:
    """(MEGA links, captcha links) in a post, in order. Base64 codes in the text are decoded too."""
    text = (text or "")[:50000]
    mega = find_links(decode._with_scheme(text))
    caps = captcha.find(text, hosts)
    for tok in _TOKEN.findall(text)[:20]:
        if "mega" in tok.lower() and "/" in tok:
            continue  # part of a link, not a code
        got = decode.unwrap(tok, hosts=hosts)
        for u in got["links"]:
            if u not in mega:
                mega.append(u)
        for u in got["captcha"]:
            if u not in caps:
                caps.append(u)
    return mega, caps


def choose(items: list[dict]) -> None:
    """Ticks the links to add: everything, except that where one title and season comes in several
    qualities only the best is ticked. Sets 'group' (rows that are alternatives share one) and 'selected'."""
    groups: dict[tuple, list[dict]] = {}
    for it in items:
        if it.get("error") or it.get("queued"):
            it["selected"] = False
            it["group"] = ""
            continue
        seasons = ",".join(str(s) for s in it.get("seasons") or [])
        key = (re.sub(r"[^a-z0-9]+", "", (it.get("title") or "").lower()), it.get("year") if not seasons else None, seasons)
        groups.setdefault(key, []).append(it)
    n = 0
    for key, rows in groups.items():
        quals = {r.get("resolution") for r in rows}
        alternatives = len(rows) > 1 and key[0] and len(quals) == len(rows) and None not in quals
        if alternatives:
            n += 1
            best = max(rows, key=lambda r: _RES_ORDER.get(r["resolution"], -1))
            for r in rows:
                r["group"] = f"g{n}"
                r["selected"] = r is best
        else:
            for r in rows:
                r["group"] = ""
                r["selected"] = True


async def scan(ctx, text: str, hosts: list[str]) -> dict:
    """{'links': [...], 'captcha': [...], 'password': '...', 'more': n}: what a pasted post holds."""
    mega, caps = links_in(text, hosts)
    more = max(0, len(mega) - MAX_LINKS)
    mega = mega[:MAX_LINKS]
    labels = _labels(text, mega)
    queued = {r["url"] for r in ctx.db.query(
        "SELECT l.url FROM links l JOIN jobs j ON j.id = l.job_id WHERE j.status NOT IN ('done','canceled')")}
    gate = asyncio.Semaphore(4)

    async def one(url: str) -> dict:
        label = labels.get(url, "")
        item = {"url": url, "label": label, "name": "", "size": 0, "files": 0, "kind": "movie", "title": "", "year": None,
                "seasons": [], "resolution": _norm_res(label), "error": "", "queued": url in queued}
        if item["queued"]:
            item["error"] = "Already in the queue."
            return item
        try:
            async with gate:
                pv = await ctx.mega.preview(url)
        except MegaError as e:
            item["error"] = str(e)
            return item
        info = parse_name(pv.name)
        from_label = parse_name(label) if label else None
        item.update({"name": pv.name, "size": pv.size, "files": len(pv.files), "kind": info["kind"], "title": info["title"],
                     "year": info["year"], "seasons": info["seasons"] or (from_label["seasons"] if from_label else []),
                     "resolution": info["resolution"] or item["resolution"]})
        if not item["resolution"]:
            for f in pv.files:
                item["resolution"] = _norm_res(f["path"])
                if item["resolution"]:
                    break
        if item["seasons"]:
            item["kind"] = "series"
        return item

    items = list(await asyncio.gather(*(one(u) for u in mega)))
    choose(items)
    return {"links": items, "captcha": caps[:10], "password": find_password(text), "more": more}
