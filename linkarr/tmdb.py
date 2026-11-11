"""TMDB lookups: the real title and year, posters, the Documentary genre, and a show's status
(returning, ended, canceled, next episode). Needs a free TMDB API key, set in Settings."""
import logging
import time

import httpx

log = logging.getLogger("linkarr.tmdb")
BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w342"
DOCUMENTARY = 99  # the same genre id for movies and TV


class TmdbError(Exception):
    pass


def _year(date: str | None) -> int | None:
    try:
        return int((date or "")[:4]) or None
    except ValueError:
        return None


class Tmdb:
    def __init__(self, key_getter, client: httpx.AsyncClient | None = None):
        self.key_getter = key_getter  # a function, so a key changed in Settings applies at once
        self.client = client or httpx.AsyncClient(timeout=20)
        self.cache: dict = {}

    def configured(self) -> bool:
        return bool(self.key_getter())

    async def _get(self, path: str, **params):
        key = self.key_getter()
        if not key:
            raise TmdbError("Add a TMDB API key in Settings first.")
        headers = {"Accept": "application/json"}
        # A v4 'read access token' is a long JWT; the short v3 key goes in the query string.
        if key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + key
        else:
            params["api_key"] = key
        cache_key = (path, tuple(sorted((k, v) for k, v in params.items() if k != "api_key")))
        hit = self.cache.get(cache_key)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            r = await self.client.get(BASE + path, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise TmdbError(f"Couldn't reach TMDB ({e.__class__.__name__}).") from e
        if r.status_code == 401:
            raise TmdbError("TMDB didn't accept the API key.")
        if r.status_code == 404:
            raise TmdbError("TMDB doesn't know that title.")
        if r.status_code >= 400:
            raise TmdbError(f"TMDB answered {r.status_code}.")
        data = r.json()
        self.cache[cache_key] = (time.time() + 3600, data)
        if len(self.cache) > 500:
            self.cache.clear()
        return data

    async def check(self) -> None:
        await self._get("/configuration")

    @staticmethod
    def _item(r: dict, kind: str) -> dict:
        title = r.get("title") or r.get("name") or ""
        date = r.get("release_date") or r.get("first_air_date")
        return {
            "id": r.get("id"), "kind": kind, "title": title, "year": _year(date),
            "poster": r.get("poster_path") or "", "overview": (r.get("overview") or "")[:400],
            "documentary": DOCUMENTARY in (r.get("genre_ids") or []),
        }

    async def search(self, query: str, kind: str | None = None, year: int | None = None) -> list[dict]:
        out = []
        kinds = [kind] if kind in ("movie", "series") else ["movie", "series"]
        for k in kinds:
            path = "/search/movie" if k == "movie" else "/search/tv"
            params = {"query": query, "include_adult": "false"}
            if year:
                params["year" if k == "movie" else "first_air_date_year"] = year
            data = await self._get(path, **params)
            out += [self._item(r, k) for r in data.get("results", [])[:8]]
            if year and not data.get("results"):
                params.pop("year", None)
                params.pop("first_air_date_year", None)
                data = await self._get(path, **params)
                out += [self._item(r, k) for r in data.get("results", [])[:8]]
        return out

    async def details(self, kind: str, tmdb_id: int) -> dict:
        data = await self._get(("/movie/" if kind == "movie" else "/tv/") + str(int(tmdb_id)))
        item = self._item(data, kind)
        genres = [g.get("id") for g in data.get("genres", [])]
        item["documentary"] = DOCUMENTARY in genres
        item["genres"] = [g.get("name", "") for g in data.get("genres", [])]
        if kind == "series":
            nxt = data.get("next_episode_to_air") or {}
            last = data.get("last_episode_to_air") or {}
            item.update({
                "status": data.get("status", ""),  # Returning Series, Ended, Canceled, In Production
                "next_air": nxt.get("air_date") or "",
                "next_season": nxt.get("season_number"),
                "next_episode": nxt.get("episode_number"),
                "last_air": last.get("air_date") or "",
                "last_season": last.get("season_number"),
                "last_episode": last.get("episode_number"),
                "last_episode_type": last.get("episode_type") or "",
                "seasons": [
                    {"season": s.get("season_number"), "episodes": s.get("episode_count", 0), "air": s.get("air_date") or ""}
                    for s in data.get("seasons", []) if s.get("season_number")
                ],
            })
        return item

    async def release_info(self, tmdb_id: int, country: str = "US") -> dict:
        """When a movie is out where you live: the earliest theatrical, digital (buy or rent) and physical
        (Blu-ray, DVD) dates, and the streaming services that include it ("flatrate" in TMDB's terms)."""
        det = await self.details("movie", tmdb_id)
        dates = await self._get(f"/movie/{int(tmdb_id)}/release_dates")
        out = {"title": det["title"], "year": det["year"], "poster": det["poster"], "theatrical": "", "digital": "", "physical": ""}
        wanted = {2: "theatrical", 3: "theatrical", 4: "digital", 5: "physical"}
        for block in dates.get("results", []):
            if block.get("iso_3166_1") != country:
                continue
            for r in block.get("release_dates", []):
                key, day = wanted.get(r.get("type")), (r.get("release_date") or "")[:10]
                if key and day and (not out[key] or day < out[key]):
                    out[key] = day
        prov = await self._get(f"/movie/{int(tmdb_id)}/watch/providers")
        here = (prov.get("results") or {}).get(country) or {}
        out["streaming"] = sorted({p.get("provider_name", "") for p in here.get("flatrate", []) if p.get("provider_name")})
        out["rent_buy"] = bool(here.get("rent") or here.get("buy"))
        return out

    async def close(self):
        await self.client.aclose()
