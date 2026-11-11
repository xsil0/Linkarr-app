"""Single-user login: scrypt password hash, random session tokens, lockout after repeated failures."""
import hashlib
import hmac
import secrets
import time

from .db import Database

COOKIE = "la_session"
MAX_FAILS = 5            # wrong passwords from one address before it has to wait
MAX_FAILS_ALL = 20       # wrong passwords from everywhere before everyone has to wait
LOCK_SECONDS = 15 * 60

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def has_user(db: Database) -> bool:
    return db.one("SELECT 1 FROM users LIMIT 1") is not None


def setup_code(db: Database) -> str:
    """A one-time code printed to the container log, needed to create the first account.

    Stops someone else on the network from claiming a fresh install before you do.
    """
    code = db.get_setting("setup_code")
    if not code:
        code = secrets.token_hex(4)
        db.set_setting("setup_code", code)
    return code


def create_user(db: Database, username: str, password: str) -> None:
    db.run(
        "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_password(password), int(time.time())),
    )
    db.run("DELETE FROM settings WHERE key = 'setup_code'")


def change_password(db: Database, user_id: int, password: str) -> None:
    db.run("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), user_id))
    db.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def reset(db: Database) -> str:
    """Forgotten password: removes the account and every session, and returns a new setup code.
    Libraries, settings, history and keys all stay. Run from the server: python -m linkarr reset-password"""
    db.run("DELETE FROM sessions")
    db.run("DELETE FROM users")
    db.run("DELETE FROM settings WHERE key = 'setup_code'")
    db.audit("password_reset", "", "console")
    return setup_code(db)


def _wait(fails: list[int], limit: int) -> int:
    if len(fails) < limit:
        return 0
    return max(0, fails[limit - 1] + LOCK_SECONDS - int(time.time()))


def locked_for(db: Database, ip: str = "") -> int:
    """Seconds left on the lockout, 0 when sign-in is allowed. Five wrong passwords lock out the
    address they came from, so someone else on the network guessing can't lock you out too. A
    much higher count from everywhere together locks everyone, in case the guesser keeps changing address."""
    since = int(time.time()) - LOCK_SECONDS
    # Failures since the last good sign-in, by log order (several can share one second).
    last_ok = db.one("SELECT max(id) AS id FROM audit_log WHERE event IN ('login', 'password_reset')")
    rows = db.query(
        "SELECT ts, ip FROM audit_log WHERE event IN ('login_failed', 'setup_failed') AND ts > ? AND id > ? ORDER BY id DESC",
        (since, (last_ok["id"] or 0) if last_ok else 0),
    )
    everyone = _wait([r["ts"] for r in rows], MAX_FAILS_ALL)
    mine = _wait([r["ts"] for r in rows if r["ip"] == ip], MAX_FAILS)
    return max(everyone, mine)


_DUMMY = hash_password(secrets.token_hex(8))


def login(db: Database, username: str, password: str, hours: int, ip: str = ""):
    """Returns a new session token, or None. Callers check locked_for() first."""
    user = db.one("SELECT id, pw_hash FROM users WHERE username = ?", (username,))
    # The same work whether or not the name exists, so timing doesn't give the username away.
    ok = verify_password(password, user["pw_hash"] if user else _DUMMY)
    if user is None or not ok:
        db.audit("login_failed", username, ip)
        return None
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.run(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), user["id"], now, now + hours * 3600),
    )
    db.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.audit("login", username, ip)
    return token


def session_user(db: Database, token: str | None):
    if not token:
        return None
    row = db.one(
        "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?",
        (_token_hash(token), int(time.time())),
    )
    return dict(row) if row else None


def logout(db: Database, token: str | None) -> None:
    if token:
        db.run("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
