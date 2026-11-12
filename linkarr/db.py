"""SQLite storage: one file in the data folder, WAL mode, one shared connection."""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  event TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT ''
);

-- Where finished downloads go. kind is 'movie' (one folder per title) or 'series'
-- (show folder with season folders inside). path is relative to the media mount.
CREATE TABLE IF NOT EXISTS libraries (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
  path TEXT NOT NULL,
  documentary INTEGER NOT NULL DEFAULT 0,
  plex_section TEXT NOT NULL DEFAULT '',
  position INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

-- One title: a movie, or a show with one or more links (seasons, episodes).
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'movie',
  title TEXT NOT NULL,
  year INTEGER,
  tmdb_id INTEGER,
  poster TEXT NOT NULL DEFAULT '',
  genres TEXT NOT NULL DEFAULT '[]',
  library_id INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
  folder_name TEXT NOT NULL DEFAULT '',
  match_folder TEXT NOT NULL DEFAULT '',
  match_score REAL NOT NULL DEFAULT 0,
  match_state TEXT NOT NULL DEFAULT 'none',
  conflict TEXT NOT NULL DEFAULT '',
  conflict_choice TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  source TEXT NOT NULL DEFAULT 'link',
  wait_all INTEGER NOT NULL DEFAULT 1,
  archive_password TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  quality TEXT NOT NULL DEFAULT '',
  recheck INTEGER NOT NULL DEFAULT 0,
  plex TEXT NOT NULL DEFAULT '',
  hold_url TEXT NOT NULL DEFAULT '',
  reminded_at INTEGER NOT NULL DEFAULT 0,
  show_unknown INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, updated_at);

CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  season INTEGER,
  size INTEGER NOT NULL DEFAULT 0,
  files TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'queued',
  bytes_done INTEGER NOT NULL DEFAULT 0,
  staging TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  error_kind TEXT NOT NULL DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0,
  not_before INTEGER NOT NULL DEFAULT 0,
  position INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  started_at INTEGER,
  finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS links_job ON links (job_id, position);

-- Things that need you: a stuck file, a close match, a quota wait.
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
  link_id INTEGER REFERENCES links(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  resolved_at INTEGER
);

-- Every move into a library, so it can be undone.
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  ts INTEGER NOT NULL,
  summary TEXT NOT NULL,
  moves TEXT NOT NULL DEFAULT '[]',
  recycled TEXT NOT NULL DEFAULT '[]',
  created_dirs TEXT NOT NULL DEFAULT '[]',
  undone_at INTEGER
);

-- Shows whose status (ended, new season, finale) is checked once a day.
CREATE TABLE IF NOT EXISTS shows (
  tmdb_id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  folder TEXT NOT NULL DEFAULT '',
  library_id INTEGER,
  status TEXT NOT NULL DEFAULT '',
  next_air TEXT NOT NULL DEFAULT '',
  last_air TEXT NOT NULL DEFAULT '',
  seasons INTEGER NOT NULL DEFAULT 0,
  notified TEXT NOT NULL DEFAULT '{}',
  watching INTEGER NOT NULL DEFAULT 1,
  missing TEXT NOT NULL DEFAULT '',
  poster TEXT NOT NULL DEFAULT '',
  checked_at INTEGER NOT NULL DEFAULT 0
);

-- Posters for library folders that aren't tracked (movies, mostly), looked up on TMDB a few at a time.
-- tried_at stops a folder TMDB had nothing for from being asked about again for a month.
CREATE TABLE IF NOT EXISTS folder_posters (
  library_id INTEGER NOT NULL,
  folder TEXT NOT NULL,
  tmdb_id INTEGER,
  poster TEXT NOT NULL DEFAULT '',
  tried_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (library_id, folder)
);

-- Movie Tracker: movies you're waiting for, checked once a day for Blu-ray, digital and streaming dates.
CREATE TABLE IF NOT EXISTS movies (
  tmdb_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  year INTEGER,
  poster TEXT NOT NULL DEFAULT '',
  theatrical TEXT NOT NULL DEFAULT '',
  digital TEXT NOT NULL DEFAULT '',
  physical TEXT NOT NULL DEFAULT '',
  streaming TEXT NOT NULL DEFAULT '[]',
  notified TEXT NOT NULL DEFAULT '{}',
  have INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL DEFAULT 0,
  checked_at INTEGER NOT NULL DEFAULT 0
);

-- Folder watch: MEGA folder links checked on a schedule; only files not seen before are downloaded.
-- seen holds the MEGA node handles and paths already handled. status: active, stopped (by you, or
-- nothing new for a while), dead (the link is gone).
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  year INTEGER,
  tmdb_id INTEGER,
  poster TEXT NOT NULL DEFAULT '',
  library_id INTEGER,
  folder TEXT NOT NULL DEFAULT '',
  seen TEXT NOT NULL DEFAULT '[]',
  episodes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  stop_reason TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  last_check INTEGER NOT NULL DEFAULT 0,
  last_new INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

-- Files and folders seen in the Inbox, picked up once their size stops changing.
CREATE TABLE IF NOT EXISTS inbox_seen (
  name TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  stable INTEGER NOT NULL DEFAULT 0,
  job_id INTEGER,
  first_seen INTEGER NOT NULL
);

-- Telegram messages in the bot's chats (sent by Linkarr, or sent to it), so the nightly clear can delete
-- them. Telegram only lets a bot delete messages under 48 hours old, so older rows are dropped.
-- job_id: the job a card is about (kept while it still needs you). pinned: you pinned it (always kept).
CREATE TABLE IF NOT EXISTS tg_messages (
  chat TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  job_id INTEGER,
  pinned INTEGER NOT NULL DEFAULT 0,
  sent_at INTEGER NOT NULL,
  PRIMARY KEY (chat, message_id)
);
"""


class Database:
    """One connection shared by the app and the worker, serialised with a lock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        # Columns added after 0.1.0. CREATE TABLE above already has them on a new database.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "note" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "quality" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN quality TEXT NOT NULL DEFAULT ''")
        if "recheck" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN recheck INTEGER NOT NULL DEFAULT 0")
        if "plex" not in cols:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN plex TEXT NOT NULL DEFAULT ''")
        if "hold_url" not in cols:  # 0.8.0: captcha links held as 'needs_captcha'
            self.conn.execute("ALTER TABLE jobs ADD COLUMN hold_url TEXT NOT NULL DEFAULT ''")
            self.conn.execute("ALTER TABLE jobs ADD COLUMN reminded_at INTEGER NOT NULL DEFAULT 0")
        if "show_unknown" not in cols:  # 0.9.0: links with no show name ('Season 200 - …') wait for Match to show
            self.conn.execute("ALTER TABLE jobs ADD COLUMN show_unknown INTEGER NOT NULL DEFAULT 0")
        if "hidden" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(links)")}:
            self.conn.execute("ALTER TABLE links ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        show_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(shows)")}
        if "missing" not in show_cols:
            self.conn.execute("ALTER TABLE shows ADD COLUMN missing TEXT NOT NULL DEFAULT ''")
        if "poster" not in show_cols:  # 1.0: the small posters in Tracker → Coming up
            self.conn.execute("ALTER TABLE shows ADD COLUMN poster TEXT NOT NULL DEFAULT ''")

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key: str, value) -> None:
        self.run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def audit(self, event: str, detail: str = "", ip: str = "") -> None:
        self.run(
            "INSERT INTO audit_log (ts, event, detail, ip) VALUES (?, ?, ?, ?)",
            (int(time.time()), event, detail[:500], ip[:64]),
        )

    def backup_to(self, dest: Path) -> None:
        """A consistent copy of the live database (SQLite's online backup)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            with self.lock:
                self.conn.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def now() -> int:
    return int(time.time())
