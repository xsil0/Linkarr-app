"""Captcha links: link-protector pages that only show the real MEGA link after a person solves a captcha.

Linkarr never tries to get past the captcha. It spots these links (pasted, or found inside a Base64
code), holds the job as 'Needs captcha' and hands the link to you on Telegram and in the app. Once you
paste back the MEGA link the page gave you, the job carries on like any other (jobs.solve_captcha).

Only the matching lives here, so decode, prefs and jobs can all use it without importing each other.
"""
import re

DEFAULT_HOSTS = ""             # none built in: add the sites you use in Settings → Downloads
REMIND_AFTER = 24 * 3600       # one reminder when a link has been waiting this long


def parse_hosts(value) -> list[str]:
    """'links.example, https://other.example/' → ['links.example', 'other.example']. Junk is dropped."""
    out: list[str] = []
    for h in re.split(r"[\s,]+", str(value or "").lower()):
        h = re.sub(r"^[a-z]+://", "", h).split("/")[0].strip(".")
        if h and "." in h and re.fullmatch(r"[a-z0-9.-]+", h) and h not in out:
            out.append(h)
    return out


def find(text: str, hosts: list[str]) -> list[str]:
    """Links on a captcha host (or one of its subdomains), with https:// added when it was left off.
    A bare host with nothing after it isn't a link to anything, so it doesn't count."""
    out: list[str] = []
    for h in hosts:
        pat = re.compile(r"(?<![\w.-])(?:https?://)?(?:[a-z0-9-]+\.)*" + re.escape(h) + r"/[^\s<>\"']+", re.I)
        for m in pat.finditer(text or ""):
            url = m.group(0).rstrip(").,;!?]")
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            if url not in out:
                out.append(url)
    return out


def host_of(url: str) -> str:
    return re.sub(r"^[a-z]+://", "", url or "", flags=re.I).split("/")[0].lower()
