"""Folder and file names. Patterns are set in Settings; these are the defaults."""
import difflib
import re

MOVIE_PATTERN = "{title} ({year})"
SEASON_PATTERN = "Season {season}"
EPISODE_PATTERN = "{show} - S{season:00}E{episode:00}"
_TOKEN = re.compile(r"\{(\w+)(?::(0+))?\}")


def safe(name: str) -> str:
    """A name that works on Unraid shares, Windows (SMB) and Plex."""
    name = name.replace(": ", " - ").replace(":", "-")
    name = re.sub(r'[<>"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:180] or "Untitled"


def fill(pattern: str, **values) -> str:
    def sub(m):
        v = values.get(m.group(1))
        if v is None or v == "":
            return ""
        if m.group(2) and str(v).isdigit():
            return str(v).zfill(len(m.group(2)))
        return str(v)
    out = _TOKEN.sub(sub, pattern)
    out = re.sub(r"\(\s*\)|\[\s*\]", "", out)  # '(year)' with no year
    return safe(out)


def movie_folder(title: str, year: int | None, pattern: str = MOVIE_PATTERN) -> str:
    return fill(pattern or MOVIE_PATTERN, title=title, year=year or "")


def show_folder(title: str, year: int | None, pattern: str = MOVIE_PATTERN) -> str:
    return fill(pattern or MOVIE_PATTERN, title=title, year=year or "", show=title)


def season_folder(season: int, pattern: str = SEASON_PATTERN) -> str:
    return fill(pattern or SEASON_PATTERN, season=season)


def episode_file(show: str, season: int, episode: int, ext: str, pattern: str = EPISODE_PATTERN) -> str:
    return fill(pattern or EPISODE_PATTERN, show=show, title=show, season=season, episode=episode) + ext


# ---- matching against folders already in a library ------------------------------------------

_YEAR_IN = re.compile(r"\(?\b(19|20)\d{2}\b\)?")


def norm(name: str) -> str:
    n = name.lower()
    n = _YEAR_IN.sub(" ", n)
    n = n.replace("&", " and ")
    n = re.sub(r"[^a-z0-9]+", " ", n)
    n = re.sub(r"^(the|a|an) ", "", n.strip())
    return re.sub(r"\s+", " ", n).strip()


def year_of(name: str) -> int | None:
    m = re.search(r"\((19\d{2}|20\d{2})\)", name) or re.search(r"\b(19\d{2}|20\d{2})\b", name)
    return int(m.group(1)) if m else None


def norm_title(folder: str) -> str:
    """'Resident Evil (2022) {tmdb-123}' → 'Resident Evil'."""
    t = re.sub(r"\s*[\[{][^\]}]*[\]}]", "", folder)
    t = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", t.strip())
    return t.strip() or folder


_QUALIFIERS = {"us", "uk", "au", "nz", "ca", "ie", "gb"}


def score(wanted: str, folder: str, year: int | None = None) -> float:
    a, b = norm(wanted), norm(folder)
    if not a or not b:
        return 0.0
    if a == b:
        s = 1.0
    else:
        s = difflib.SequenceMatcher(None, a, b).ratio()
        ta, tb = set(a.split()), set(b.split())
        if ta and tb and (ta <= tb or tb <= ta):
            small, extra = (ta, tb - ta) if ta <= tb else (tb, ta - tb)
            # 'The Office' vs 'The Office (US)' is close; 'Evil' vs 'Resident Evil' isn't: one word inside a
            # longer title only counts when what's added is a country tag.
            if len(small) >= 2 or extra <= _QUALIFIERS:
                s = max(s, 0.86)
    fy = year_of(folder)
    if year and fy and abs(fy - year) > 1:
        s = min(s, 0.7)  # a remake or a different show with the same name
    return round(s, 3)


def best_match(wanted: str, folders: list[str], year: int | None = None) -> tuple[str, float]:
    best, best_s = "", 0.0
    for f in folders:
        s = score(wanted, f, year)
        if s > best_s:
            best, best_s = f, s
    return best, best_s


EXACT = 0.97
CLOSE = 0.75
