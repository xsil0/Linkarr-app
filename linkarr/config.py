"""Settings from environment variables. Only what has to be known before the app starts lives
here; everything else (libraries, keys, naming) is set in the app and kept in the database."""
import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    media_dir: Path
    port: int
    worker: bool
    cookie_secure: bool | None  # None: HTTPS-only when the request came in over HTTPS
    session_hours: int
    default_tz: str
    mega_bin: str = ""  # folder holding the mega-* commands; empty = on PATH
    trusted_proxies: str = "*"  # whose X-Forwarded-For to believe; set to your reverse proxy's address

    @property
    def db_path(self) -> Path:
        return self.data_dir / "linkarr.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def poster_dir(self) -> Path:
        return self.data_dir / "posters"

    @property
    def mega_home(self) -> Path:
        # MEGAcmd keeps its sign-in session under $HOME/.megaCmd; keeping it in /data
        # means a container update doesn't sign you out.
        return self.data_dir / "megacmd"

    @property
    def site_logo_path(self) -> Path:
        return self.data_dir / "site-logo"


def _tri(v: str) -> bool | None:
    v = v.strip().lower()
    return True if v in ("1", "true", "yes") else False if v in ("0", "false", "no") else None


def load() -> Config:
    return Config(
        data_dir=Path(os.environ.get("LA_DATA", "/data")),
        media_dir=Path(os.environ.get("LA_MEDIA", "/media")),
        port=_int("LA_PORT", 9040),
        worker=os.environ.get("LA_WORKER", "1") != "0",
        cookie_secure=_tri(os.environ.get("LA_COOKIE_SECURE", "")),
        session_hours=max(1, _int("LA_SESSION_HOURS", 24 * 14)),
        default_tz=os.environ.get("TZ", "UTC"),
        mega_bin=os.environ.get("LA_MEGA_BIN", ""),
        trusted_proxies=os.environ.get("LA_TRUSTED_PROXIES", "*").strip() or "*",
    )
