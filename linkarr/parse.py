"""Reads a release name like 'The.Bear.S03.1080p.WEB-DL' or 'tvshowname season 2' into a title,
year, season and episode. TMDB then confirms the real title; this only has to get close."""
import re

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}
_ALL_EXT = VIDEO_EXT | SUB_EXT | ARCHIVE_EXT | {".nfo", ".txt"}

# Words that end the title: quality, source, codec, language and release tags.
_STOP = re.compile(
    r"\b(2160p|1080[pi]|720p|576p|480p|4k|uhd|hdr10?\+?|dv|dolby|bluray|blu-ray|bdrip|brrip|bdremux|remux|"
    r"web-?dl|webrip|web|hdtv|hdrip|dvdrip|dvd|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac\d?|ac3|eac3|ddp?5|dts|truehd|atmos|"
    r"10bit|8bit|proper|repack|extended|unrated|remastered|imax|internal|limited|multi|dual|ita|eng|sub|subs|"
    r"amzn|nf|dsnp|hmax|atvp|hulu|pcok|complete|integrale|completa)\b",
    re.I,
)
# Seasons go up to 4 digits: some shows number them 199, 200… (MasterClass on TVDB) or by year (S2019).
_SXE = re.compile(r"\bs(\d{1,4})[ ._-]?e(\d{1,3})(?:[ ._-]?e?(\d{1,3}))?\b", re.I)
_X = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.I)
_SRANGE = re.compile(r"\bs(\d{1,4})\s*-\s*s?(\d{1,4})\b", re.I)
_S = re.compile(r"\bs(\d{1,4})\b", re.I)
_SEASON = re.compile(r"\b(?:season|stagione|saison|temporada|staffel)\s*(\d{1,4})(?!\d)(?:\s*(?:-|to|a)\s*(\d{1,4})(?!\d))?", re.I)
_YEAR = re.compile(r"(?<![0-9])(19[2-9]\d|20[0-4]\d)(?![0-9p])")
_COMPLETE = re.compile(r"\b(complete series|complete|the complete|all seasons|serie completa|integrale)\b", re.I)


def strip_ext(name: str) -> str:
    for ext in sorted(_ALL_EXT, key=len, reverse=True):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return re.sub(r"\.part\d+$", "", name, flags=re.I)


def _clean_title(t: str) -> str:
    t = re.sub(r"[\[\(\{][^\]\)\}]*$", "", t)  # an unclosed bracket at the end
    t = re.sub(r"\s+", " ", t).strip(" -_.,")
    t = re.sub(r"^[\[\(\{]\s*|\s*[\]\}]$", "", t).strip(" -_.,")
    if t and (t.islower() or t.isupper()):
        t = " ".join(w if w.isdigit() else (w[0] + w[1:2].upper() + w[2:].lower() if w[:1] in "([" else w[:1].upper() + w[1:].lower()) for w in t.split(" "))
        t = re.sub(r"\b(And|Of|The|A|An|In|On|At|To|For)\b", lambda m: m.group(1).lower(), t)
        t = t[:1].upper() + t[1:]
    return t


def parse_name(name: str) -> dict:
    base = strip_ext(name.rsplit("/", 1)[-1])
    # Release-group tags in square brackets at the start ('[YTS.MX] Movie') say nothing about the title.
    base = re.sub(r"^\s*\[[^\]]*\]\s*", "", base)
    text = re.sub(r"[._]+", " ", base)
    text = re.sub(r"(?<=[a-z])(?=season\s?\d)", " ", text, flags=re.I)
    out = {"title": "", "year": None, "season": None, "episode": None, "seasons": [], "complete": False,
           "resolution": None, "kind": "movie", "label": ""}

    res = re.search(r"\b(2160p|1080p|720p|576p|480p|4k|uhd)\b", text, re.I)
    if res:
        r = res.group(1).lower()
        out["resolution"] = "2160p" if r in ("4k", "uhd") else r
    if _COMPLETE.search(text):
        out["complete"] = True

    cut = len(text)
    m = _SXE.search(text)
    if m:
        out["season"], out["episode"] = int(m.group(1)), int(m.group(2))
        cut = min(cut, m.start())
    else:
        m = _X.search(text)
        if m and not _YEAR.fullmatch(m.group(0)):
            out["season"], out["episode"] = int(m.group(1)), int(m.group(2))
            cut = min(cut, m.start())
    for rx in (_SRANGE, _SEASON, _S):
        m = rx.search(text)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else a
            if out["season"] is None:
                out["season"] = a
            if b >= a and not out["seasons"]:
                out["seasons"] = list(range(a, b + 1))
            cut = min(cut, m.start())
            break
    if out["season"] is not None and not out["seasons"]:
        out["seasons"] = [out["season"]]
    if out["season"] is not None or out["complete"] and re.search(r"series|serie|seasons", text, re.I):
        out["kind"] = "series"

    years = [y for y in _YEAR.finditer(text) if y.start() > 0]
    if years:
        y = years[-1] if out["kind"] == "movie" else years[0]
        if y.start() < cut or out["kind"] == "movie":
            out["year"] = int(y.group(1))
            if y.start() > 0:
                cut = min(cut, y.start())
    stop = _STOP.search(text)
    if stop and stop.start() > 0:
        cut = min(cut, stop.start())
    if cut == 0 and out["kind"] == "series":
        # 'Season 200 - The Science of Connection': no show name at all, only a season and what it's about.
        # The title stays empty so nothing guesses a show from the episode name; Match to show fills it in.
        out["title"] = ""
        m = _SEASON.match(base.strip())
        if m:
            out["label"] = re.sub(r"\s+", " ", base.strip()[m.end():]).strip(" -:–_.")
        return out
    out["title"] = _clean_title(text[:cut]) or _clean_title(text)
    return out


def is_video(path: str) -> bool:
    return "." + path.rsplit(".", 1)[-1].lower() in VIDEO_EXT if "." in path else False


def is_sub(path: str) -> bool:
    return "." + path.rsplit(".", 1)[-1].lower() in SUB_EXT if "." in path else False
