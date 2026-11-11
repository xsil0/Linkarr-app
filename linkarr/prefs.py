"""Settings changed in the app (not secrets: those are in vault.py). Each has a safe default."""
from .db import Database
from . import captcha, naming

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULTS = {
    "movie_pattern": naming.MOVIE_PATTERN,
    "show_pattern": naming.MOVIE_PATTERN,
    "season_pattern": naming.SEASON_PATTERN,
    "episode_pattern": naming.EPISODE_PATTERN,
    "rename_episodes": True,
    # 'ask': nothing is moved without your OK on close matches and conflicts (exact matches move).
    "unpack": True,
    "clean_junk": True,
    "check_video": True,
    "recycle_days": 7,
    "work_dir": ".linkarr",          # inside the media mount, so moves into libraries are instant
    "inbox_enabled": True,
    "inbox_dir": "Linkarr Inbox",
    "inbox_auto_start": False,
    "auto_start": False,             # start a new link straight away when Linkarr is sure what it is
    "max_parallel": 1,
    "night_start": "01:00",          # 'Tonight' downloads wait until this time
    "plex_url": "",
    "jellyfin_url": "",              # Jellyfin and Emby are asked to scan after something is filed
    "emby_url": "",
    # Passwords tried on locked archives when the job has none of its own. One per entry. {year} and
    # {title} are swapped for the job's year and title before trying.
    "archive_passwords": [],
    "telegram_chats": [],             # chat ids allowed to use the bot and get messages
    "notify_done": True,
    "notify_series": True,
    "subs_enabled": False,           # subtitles from OpenSubtitles.com after filing
    "subs_languages": "en",          # up to three, comma separated: en, it, es…
    "opensubtitles_user": "",
    "roundup_enabled": True,         # a Telegram message once a week: what was added, what starts soon
    "roundup_day": "sun",
    "quota_guard": True,             # hold a download that would go over what's left of the MEGA transfer quota
    "theme": "dark",
    "site_name": "",                 # your download site: a tab in the menu that opens it
    "site_url": "",
    "site_badge": "",                # up to 3 letters shown as its icon; blank uses the name's capitals
    "notify_movies": True,           # Movie Tracker news: out on Blu-ray, digital, streaming
    "release_country": "US",         # whose release dates and streaming services the Movie Tracker uses
    # Extras (0.7.0). Every one is off until you turn it on in Settings → Downloads.
    "watch_enabled": False,          # folder watch: check watched MEGA folder links for new episodes
    "watch_every": 24,               # hours between checks of each watched folder
    "watch_stop_days": 30,           # stop watching after this many days with nothing new (0 = never)
    "gap_check": False,              # after filing, say which episodes are missing between the ones you have
    "dup_guard": False,              # skip episodes and movies you already have, whichever link they came from
    "auto_upgrade": False,           # swap in a better copy (1080p over 720p) without asking
    "plex_partial": False,           # scan only the folder that changed, not the whole Plex library
    "window_enabled": False,         # big downloads wait for the download window
    "window_start": "01:00",
    "window_end": "07:00",
    "window_min_gb": 5,              # what counts as big; 0 = every download waits
    "morning_summary": False,        # one Telegram message in the morning with what came in overnight
    "morning_time": "08:00",
    # Captcha links (0.8.0). On from the start: a link on one of your captcha sites is held as 'Needs captcha' and sent
    # to you, instead of being turned away as "not a MEGA link". Linkarr never tries to solve the captcha.
    "captcha_enabled": True,
    "captcha_hosts": captcha.DEFAULT_HOSTS,   # comma separated; subdomains count too
    # Nightly chat clear: off until you turn it on. Every night the bot deletes its chat so each day starts clean.
    # Pinned messages stay. Cards still waiting on an answer stay unless you turn that off.
    "chat_clear": False,
    "chat_clear_time": "04:00",
    "chat_clear_keep_open": True,
}

WATCH_EVERY = (6, 12, 24, 48)

THEMES = ("dark", "light", "auto")
MAX_PASSWORDS = 30

KINDS = {
    "movie_pattern": str, "show_pattern": str, "season_pattern": str, "episode_pattern": str,
    "rename_episodes": bool, "unpack": bool, "clean_junk": bool, "check_video": bool,
    "recycle_days": int, "work_dir": str, "inbox_enabled": bool, "inbox_dir": str, "inbox_auto_start": bool, "auto_start": bool,
    "max_parallel": int, "night_start": str, "plex_url": str, "jellyfin_url": str, "emby_url": str, "archive_passwords": list, "telegram_chats": list,
    "notify_done": bool, "notify_series": bool, "subs_enabled": bool, "subs_languages": str, "opensubtitles_user": str,
    "roundup_enabled": bool, "roundup_day": str, "quota_guard": bool, "theme": str,
    "site_name": str, "site_url": str, "site_badge": str, "notify_movies": bool, "release_country": str,
    "watch_enabled": bool, "watch_every": int, "watch_stop_days": int, "gap_check": bool, "dup_guard": bool,
    "auto_upgrade": bool, "plex_partial": bool, "window_enabled": bool, "window_start": str, "window_end": str,
    "window_min_gb": int, "morning_summary": bool, "morning_time": str, "captcha_enabled": bool, "captcha_hosts": str,
    "chat_clear": bool, "chat_clear_time": str, "chat_clear_keep_open": bool,
}


def clock(v: str, default: str) -> str:
    """'7:5' or '07:05:00' → '07:05'; anything that isn't a time of day gives the default."""
    try:
        hh, mm = (int(x) for x in str(v).split(":")[:2])
    except ValueError:
        return default
    return f"{hh:02d}:{mm:02d}" if 0 <= hh < 24 and 0 <= mm < 60 else default


def get(db: Database, key: str):
    v = db.get_setting("pref." + key, DEFAULTS[key])
    if key == "theme" and v not in THEMES:
        return "dark"  # the older extra themes are gone; they all were dark
    return v


def all_prefs(db: Database) -> dict:
    return {k: get(db, k) for k in DEFAULTS}


def set_many(db: Database, values: dict) -> dict:
    for k, v in values.items():
        if k not in DEFAULTS:
            continue
        kind = KINDS[k]
        if kind is bool:
            v = bool(v)
        elif kind is int:
            v = int(v)
        elif kind is list and k == "archive_passwords":
            # One password per line. Spaces inside a password are kept; commas are part of it.
            seen: list[str] = []
            for x in (v if isinstance(v, list) else str(v).splitlines()):
                x = str(x).strip("\r\n")[:100]
                if x.strip() and x not in seen:
                    seen.append(x)
            v = seen[:MAX_PASSWORDS]
        elif kind is list:
            v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
        else:
            v = str(v).strip()
        if k == "recycle_days":
            v = max(1, min(90, v))
        if k == "max_parallel":
            v = max(1, min(4, v))
        if k in ("work_dir", "inbox_dir"):
            v = v.strip("/") or DEFAULTS[k]
            if ".." in v.split("/"):
                raise ValueError("Folders must stay inside the media folder.")
        if k == "roundup_day" and v not in DAYS:
            v = "sun"
        if k == "theme" and v not in THEMES:
            v = "dark"
        if k == "site_url" and v and not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        if k in ("plex_url", "jellyfin_url", "emby_url") and v:
            if not v.lower().startswith(("http://", "https://")):
                v = "http://" + v
            v = v.rstrip("/")[:200]
        if k == "site_name":
            v = v[:40]
        if k == "site_badge":
            v = "".join(c for c in v if c.isalnum())[:3].upper()
        if k == "release_country":
            v = v.upper()[:2] if len(v) >= 2 and v[:2].isalpha() else "US"
        if k == "watch_every" and v not in WATCH_EVERY:
            v = 24
        if k == "watch_stop_days":
            v = max(0, min(365, v))
        if k == "window_min_gb":
            v = max(0, min(500, v))
        if k == "captcha_hosts":
            v = ", ".join(captcha.parse_hosts(v)[:10]) or captcha.DEFAULT_HOSTS
        if k in ("window_start", "window_end", "morning_time", "night_start", "chat_clear_time"):
            v = clock(v, DEFAULTS[k])
        db.set_setting("pref." + k, v)
    return all_prefs(db)


def captcha_hosts(db: Database) -> list[str]:
    """The captcha hosts to look for (Settings → Downloads). Spotted even with the switch off, so Linkarr can say why it
    isn't holding one."""
    return captcha.parse_hosts(get(db, "captcha_hosts")) or captcha.parse_hosts(captcha.DEFAULT_HOSTS)


def site(db: Database) -> dict | None:
    """Your download site: its name, address and a 1-3 letter badge (the name's capitals if not set)."""
    url = get(db, "site_url")
    if not url:
        return None
    name = get(db, "site_name") or "Site"
    badge = get(db, "site_badge") or "".join(c for c in name if c.isupper())[:3] or name[:1].upper()
    logo = db.get_setting("site_logo")
    return {"name": name, "url": url, "badge": badge, "logo": f"/api/site-logo?v={logo['v']}" if logo else ""}


def archive_passwords(db: Database, title: str = "", year=None) -> list[str]:
    """The saved archive passwords with {year} and {title} filled in for one job. An entry that needs
    a year is left out when the job has none."""
    out: list[str] = []
    for raw in get(db, "archive_passwords") or []:
        if "{year}" in raw and not year:
            continue
        pw = raw.replace("{year}", str(year or "")).replace("{title}", title or "")
        pw = pw.replace("{Title}", title or "").replace("{title_nospace}", (title or "").replace(" ", ""))
        if pw and pw not in out:
            out.append(pw)
    return out
