"""Plex: rescans a library after something lands in it, reads which title each folder is (Plex's own
TMDB match, so Linkarr matches shows by id instead of by folder name), and checks Plex picked up what
was filed. Optional: needs the Plex URL (Settings) and a Plex token (stored encrypted)."""
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("linkarr.plex")


class Plex:
    def __init__(self, url_getter, token_getter, client: httpx.AsyncClient | None = None):
        self.url_getter = url_getter
        self.token_getter = token_getter
        self.client = client or httpx.AsyncClient(timeout=15)

    def configured(self) -> bool:
        return bool(self.url_getter() and self.token_getter())

    async def _get(self, path: str, params: dict | None = None):
        r = await self.client.get(self.url_getter().rstrip("/") + path, params=params,
                                  headers={"X-Plex-Token": self.token_getter(), "Accept": "application/json"})
        r.raise_for_status()
        return r

    async def sections(self) -> list[dict]:
        r = await self._get("/library/sections")
        dirs = r.json().get("MediaContainer", {}).get("Directory", [])
        return [{"id": str(d.get("key")), "title": d.get("title", ""), "type": d.get("type", ""),
                 "paths": [loc.get("path", "") for loc in d.get("Location", []) or [] if loc.get("path")]} for d in dirs]

    async def items(self, section: str) -> dict:
        """{folder name: {"tmdb", "title", "year", "count"}} for one library section. count is the number of
        episodes for a show, 1 for a movie. Folders without a TMDB match in Plex have tmdb None."""
        r = await self._get(f"/library/sections/{section}/all?includeGuids=1")
        out = {}
        for m in r.json().get("MediaContainer", {}).get("Metadata", []) or []:
            tmdb = None
            for g in m.get("Guid", []) or []:
                hit = re.match(r"tmdb://(\d+)", str(g.get("id", "")))
                if hit:
                    tmdb = int(hit.group(1))
            info = {"tmdb": tmdb, "title": m.get("title", ""), "year": m.get("year"),
                    "count": int(m.get("leafCount") or 1)}
            paths = [loc.get("path", "") for loc in m.get("Location", []) or []]
            if not paths:  # a movie: the folder its file sits in. Plex on Windows gives back\slashes.
                paths = [p.get("file", "").replace("\\", "/").rsplit("/", 1)[0]
                         for md in m.get("Media", []) or [] for p in md.get("Part", []) or []]
            for path in paths:
                name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                if name:
                    out[name] = info
        return out

    async def refresh(self, section: str, path: str = "") -> bool:
        """Scan a library. With path, Plex scans only that folder (path as Plex sees it)."""
        if not self.configured() or not section:
            return False
        try:
            await self._get(f"/library/sections/{section}/refresh", {"path": path} if path else None)
            return True
        except httpx.HTTPError as e:
            log.warning("Plex refresh failed: %s", e.__class__.__name__)
            return False

    async def refresh_folder(self, section: str, lib_dir: Path, folder: Path) -> str:
        """Scan only `folder`: Linkarr's path is turned into Plex's by putting the part under the library
        folder onto the section's own folder (Plex can see the share under another name, even on Windows).
        Falls back to scanning the whole library when that can't be worked out. Returns 'folder', 'library' or ''."""
        plex_path = ""
        try:
            rel = folder.relative_to(lib_dir).parts
            secs = await self.sections()
            paths = next((s["paths"] for s in secs if s["id"] == str(section)), [])
            base = next((p for p in paths if p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] == lib_dir.name), paths[0] if paths else "")
            if base and rel:
                sep = "\\" if "\\" in base and "/" not in base else "/"
                plex_path = base.rstrip("/\\") + sep + sep.join(rel)
        except (ValueError, httpx.HTTPError) as e:
            log.info("Couldn't work out the Plex folder, scanning the whole library: %s", e.__class__.__name__)
        if plex_path and await self.refresh(section, plex_path):
            return "folder"
        return "library" if await self.refresh(section) else ""

    async def close(self):
        await self.client.aclose()
