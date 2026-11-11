"""Jellyfin and Emby: ask the server to scan its libraries after something lands. Both speak the
same API here, so one class covers them. Optional: needs the server address (Settings) and an API
key (stored encrypted). Linkarr only ever asks for a scan and reads the server's name; it changes
nothing on the server."""
import logging

import httpx

log = logging.getLogger("linkarr.mediaserver")

KINDS = {"jellyfin": "Jellyfin", "emby": "Emby"}


class MediaServer:
    def __init__(self, kind: str, url_getter, key_getter, client: httpx.AsyncClient | None = None):
        self.kind = kind
        self.label = KINDS[kind]
        self.url_getter = url_getter
        self.key_getter = key_getter
        self.client = client or httpx.AsyncClient(timeout=15)

    def configured(self) -> bool:
        return bool(self.url_getter() and self.key_getter())

    def _headers(self) -> dict:
        return {"X-Emby-Token": self.key_getter(), "Accept": "application/json"}

    async def info(self) -> dict:
        """{'name', 'version'} of the server. Raises httpx.HTTPError when it can't be reached or the key is wrong."""
        r = await self.client.get(self.url_getter().rstrip("/") + "/System/Info", headers=self._headers())
        r.raise_for_status()
        data = r.json() if r.content else {}
        return {"name": str(data.get("ServerName", ""))[:80], "version": str(data.get("Version", ""))[:20]}

    async def refresh(self) -> bool:
        if not self.configured():
            return False
        try:
            r = await self.client.post(self.url_getter().rstrip("/") + "/Library/Refresh", headers=self._headers())
            r.raise_for_status()
            return True
        except (httpx.HTTPError, ValueError) as e:
            log.warning("%s refresh failed: %s", self.label, e.__class__.__name__)
            return False

    async def close(self):
        await self.client.aclose()


def explain(e: Exception, label: str) -> str:
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
        return f"{label} didn't accept the API key."
    return f"Couldn't reach {label} ({e.__class__.__name__}). Check the address."
