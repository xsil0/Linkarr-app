"""API keys and tokens you type into Settings, stored encrypted in the database.

The key lives in its own file next to the database (data/secret.key, readable only by the app),
so a copy of the database or a settings backup on its own doesn't give the tokens away.
Nothing secret ever comes from the image, the repo or an environment variable.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .db import Database

# Everything stored through this module. The API only ever says whether each one is set.
NAMES = ("tmdb_key", "plex_token", "jellyfin_key", "emby_key", "telegram_token", "opensubtitles_key", "opensubtitles_password")


class Vault:
    def __init__(self, db: Database, key_path: Path):
        self.db = db
        if not key_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.fernet = Fernet(key_path.read_bytes().strip())

    def get(self, name: str) -> str:
        token = self.db.get_setting("secret." + name)
        if not token:
            return ""
        try:
            return self.fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return ""

    def set(self, name: str, value: str) -> None:
        if name not in NAMES:
            raise KeyError(name)
        value = (value or "").strip()
        if not value:
            self.db.run("DELETE FROM settings WHERE key = ?", ("secret." + name,))
            return
        self.db.set_setting("secret." + name, self.fernet.encrypt(value.encode()).decode())

    def which_set(self) -> dict:
        return {n: bool(self.get(n)) for n in NAMES}
