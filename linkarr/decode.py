"""Base64 unwrapping for links people share encoded (sometimes several times over).

Decodes up to MAX_LAYERS times, stopping as soon as a MEGA link (or a captcha link, see captcha.py)
shows up or the text stops being valid Base64 that turns into readable text.
"""
import base64
import binascii
import re

from . import captcha
from .mega import find_links

MAX_LAYERS = 4
_B64 = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
# A MEGA address with the https:// left off, as encoded links often have.
_BARE = re.compile(r"(?<![/\w.])((?:www\.)?mega(?:\.co)?\.nz/)", re.I)


def _with_scheme(text: str) -> str:
    return _BARE.sub(r"https://\1", text)


def decode_once(text: str) -> str | None:
    """One layer of Base64 (standard or URL-safe, padding optional), or None if it isn't."""
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 8 or not _B64.match(s):
        return None
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        raw = base64.b64decode(s, validate=True)
        out = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in out)
    if not out or printable < 0.95 * len(out):
        return None
    return out.strip()


def unwrap(text: str, max_layers: int = MAX_LAYERS, hosts: list[str] | None = None) -> dict:
    """{'layers': [each decoded text], 'links': [MEGA links found], 'captcha': [captcha links found],
    'done': why it stopped}. hosts are the captcha hosts (Settings → Extras); None means the default."""
    hosts = captcha.parse_hosts(captcha.DEFAULT_HOSTS) if hosts is None else hosts
    current = (text or "").strip()
    layers: list[str] = []

    def found(t: str):
        links = find_links(_with_scheme(t))
        caps = captcha.find(t, hosts)
        if links or caps:
            return {"layers": layers, "links": links, "captcha": caps, "done": "link" if links else "captcha"}
        return None

    hit = found(current)
    if hit:
        return hit
    for i in range(max_layers):
        nxt = decode_once(current)
        if nxt is None and i == 0:
            # Pasted with other words around it ("link: aHR0cHM6..."): try the long Base64-looking bits.
            for tok in sorted(_TOKEN.findall(current), key=len, reverse=True):
                nxt = decode_once(tok)
                if nxt is not None:
                    break
        if nxt is None:
            return {"layers": layers, "links": [], "captcha": [], "done": "not_base64" if not layers else "no_link"}
        layers.append(nxt)
        current = nxt
        hit = found(current)
        if hit:
            return hit
    return {"layers": layers, "links": [], "captcha": [], "done": "max_layers"}
