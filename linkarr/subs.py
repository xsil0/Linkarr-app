"""Subtitles from OpenSubtitles.com for what Linkarr files: a .srt next to each movie or episode, in the
languages you pick, unless one is already there.

Needs a free API key (opensubtitles.com → your profile → API consumers). Without signing in,
OpenSubtitles allows a handful of downloads a day; with your OpenSubtitles username and password
it's 20 a day on a free account, more with VIP. When the day's allowance runs out Linkarr stops
asking until the next day.
"""
import logging
import struct
import time
from pathlib import Path

import httpx

from . import __version__, prefs
from .parse import parse_name

log = logging.getLogger("linkarr.subs")
API = "https://api.opensubtitles.com/api/v1"
_CHUNK = 64 * 1024


def os_hash(path: Path) -> str:
    """OpenSubtitles' file hash: the size plus the 64-bit words of the first and last 64 KB."""
    size = path.stat().st_size
    if size < _CHUNK * 2:
        return ""
    h = size
    with path.open("rb") as f:
        for offset in (0, size - _CHUNK):
            f.seek(offset)
            for (word,) in struct.iter_unpack("<Q", f.read(_CHUNK)):
                h = (h + word) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def has_sub(video: Path, lang: str) -> bool:
    """A sidecar in that language already sits next to the video ('Name.en.srt', or a plain
    'Name.srt' counted as English)."""
    stem = video.stem
    for f in video.parent.iterdir() if video.parent.exists() else []:
        if f.suffix.lower() not in (".srt", ".ass", ".ssa", ".vtt", ".sub") or not f.name.startswith(stem):
            continue
        tags = [t.lower() for t in f.name[len(stem):].split(".")[1:-1]]
        if lang in tags or (not tags and lang == "en"):
            return True
    return False


class Subs:
    def __init__(self, ctx, client: httpx.AsyncClient | None = None):
        self.ctx = ctx
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self.token, self.base, self.token_at = "", API, 0.0

    def enabled(self) -> bool:
        return bool(prefs.get(self.ctx.db, "subs_enabled") and self.ctx.vault.get("opensubtitles_key"))

    def _headers(self, auth: bool = False) -> dict:
        h = {"Api-Key": self.ctx.vault.get("opensubtitles_key") or "", "User-Agent": f"Linkarr v{__version__}",
             "Accept": "application/json"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _paused(self) -> bool:
        return int(self.ctx.db.get_setting("subs_paused_until", 0) or 0) > time.time()

    async def _login(self) -> None:
        user = prefs.get(self.ctx.db, "opensubtitles_user")
        pw = self.ctx.vault.get("opensubtitles_password")
        if not user or not pw or (self.token and time.time() - self.token_at < 20 * 3600):
            return
        r = await self.client.post(API + "/login", json={"username": user, "password": pw}, headers=self._headers())
        if r.status_code != 200:
            log.warning("OpenSubtitles sign-in failed (%s). Carrying on without it.", r.status_code)
            return
        data = r.json()
        self.token, self.token_at = data.get("token", ""), time.time()
        base = data.get("base_url") or ""
        self.base = f"https://{base}/api/v1" if base and "/" not in base else API

    async def check(self) -> str:
        """For the Settings test button: '' when the key works, else what's wrong."""
        try:
            r = await self.client.get(API + "/infos/formats", headers=self._headers())
        except httpx.HTTPError as e:
            return f"Couldn't reach OpenSubtitles: {e}"
        if r.status_code in (401, 403):
            return "OpenSubtitles didn't accept that API key."
        if r.status_code != 200:
            return f"OpenSubtitles answered {r.status_code}."
        self.token = ""
        await self._login()
        if prefs.get(self.ctx.db, "opensubtitles_user") and not self.token:
            return "The API key works, but the username or password didn't."
        return ""

    async def fetch(self, video: Path, kind: str, tmdb_id: int | None, lang: str) -> str:
        """Downloads one subtitle. Returns 'saved', 'have', 'none' or 'quota'."""
        if has_sub(video, lang):
            return "have"
        params = {"languages": lang, "order_by": "download_count"}
        vhash = os_hash(video)
        if vhash:
            params["moviehash"] = vhash
        if kind == "series":
            info = parse_name(video.name)
            if info["season"] is None or info["episode"] is None:
                return "none"
            params.update({"type": "episode", "season_number": info["season"], "episode_number": info["episode"]})
            if tmdb_id:
                params["parent_tmdb_id"] = tmdb_id
            else:
                params["query"] = info["title"]
        else:
            params["type"] = "movie"
            if tmdb_id:
                params["tmdb_id"] = tmdb_id
            else:
                params["query"] = parse_name(video.name)["title"]
        r = await self.client.get(self.base + "/subtitles", params=params, headers=self._headers(True))
        if r.status_code != 200:
            log.warning("Subtitle search for %s: OpenSubtitles answered %s", video.name, r.status_code)
            return "none"
        found = [d.get("attributes", {}) for d in r.json().get("data", [])]
        found = [a for a in found if a.get("files")]
        if not found:
            return "none"
        # A hash match was made for this exact file; otherwise the most downloaded, without SDH if possible.
        best = max(found, key=lambda a: (bool(a.get("moviehash_match")), not a.get("hearing_impaired"), a.get("download_count", 0)))
        r = await self.client.post(self.base + "/download", json={"file_id": best["files"][0]["file_id"]},
                                   headers=self._headers(True))
        if r.status_code in (406, 429) or (r.status_code == 200 and not r.json().get("link")):
            self.ctx.db.set_setting("subs_paused_until", int(time.time()) + 12 * 3600)
            log.info("OpenSubtitles download allowance used up for today; trying again later.")
            return "quota"
        if r.status_code != 200:
            log.warning("Subtitle download for %s: OpenSubtitles answered %s", video.name, r.status_code)
            return "none"
        body = await self.client.get(r.json()["link"])
        if body.status_code != 200 or not body.content:
            return "none"
        ext = Path(best["files"][0].get("file_name") or "x.srt").suffix.lower()
        dest = video.with_name(f"{video.stem}.{lang}{ext if ext in ('.srt', '.ass', '.vtt') else '.srt'}")
        dest.write_bytes(body.content)
        return "saved"

    async def for_files(self, videos: list[Path], kind: str, tmdb_id: int | None) -> int:
        """Subtitles for files that were just filed. Returns how many were saved. Never raises."""
        if not self.enabled() or self._paused():
            return 0
        langs = [x.strip().lower() for x in str(prefs.get(self.ctx.db, "subs_languages")).split(",") if x.strip()][:3]
        saved = 0
        try:
            await self._login()
            for video in videos:
                for lang in langs:
                    res = await self.fetch(video, kind, tmdb_id, lang)
                    if res == "saved":
                        saved += 1
                    elif res == "quota":
                        return saved
        except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
            log.warning("Subtitles: %s", e)
        if saved:
            log.info("Saved %d subtitle file%s", saved, "s" if saved != 1 else "")
        return saved

    async def close(self):
        await self.client.aclose()
