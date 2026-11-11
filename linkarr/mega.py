"""MEGA links: read what's inside before downloading (MEGA's public API, no account needed),
then download with MEGAcmd, MEGA's own command-line client, signed in to your account so your
plan's transfer quota applies.

Link keys never leave this module in logs: logbuf masks everything after the '#'.
"""
import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("linkarr.mega")
API = "https://g.api.mega.co.nz/cs"

# https://mega.nz/file/HANDLE#KEY, /folder/HANDLE#KEY (optionally /file/SUB or /folder/SUB after the key),
# and the old #!HANDLE!KEY / #F!HANDLE!KEY forms.
_NEW = re.compile(r"https?://(?:www\.)?mega(?:\.co)?\.nz/(file|folder)/([A-Za-z0-9_-]{8})#([A-Za-z0-9_-]{22,43})"
                  r"(?:/(file|folder)/([A-Za-z0-9_-]{8}))?")
_OLD = re.compile(r"https?://(?:www\.)?mega(?:\.co)?\.nz/#(F?)!([A-Za-z0-9_-]{8})!([A-Za-z0-9_-]{22,43})")
_ANY = re.compile(r"https?://(?:www\.)?mega(?:\.co)?\.nz/[^\s<>\"']+")


class MegaError(Exception):
    """kind: dead (link gone), integrity (file check failed), quota, auth, missing, error."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Link:
    kind: str  # 'file' or 'folder'
    handle: str
    key: str
    sub_kind: str = ""
    sub_handle: str = ""

    @property
    def url(self) -> str:
        u = f"https://mega.nz/{self.kind}/{self.handle}#{self.key}"
        if self.sub_handle:
            u += f"/{self.sub_kind}/{self.sub_handle}"
        return u


@dataclass
class Preview:
    name: str
    size: int
    kind: str
    files: list = field(default_factory=list)  # [{"path": "Folder/a.mkv", "size": 123}]
    unreadable: int = 0       # files in the link Linkarr couldn't read the names of
    unreadable_size: int = 0


def parse_link(text: str) -> Link | None:
    text = (text or "").strip()
    m = _NEW.search(text)
    if m:
        return Link(m.group(1), m.group(2), m.group(3), m.group(4) or "", m.group(5) or "")
    m = _OLD.search(text)
    if m:
        return Link("folder" if m.group(1) else "file", m.group(2), m.group(3))
    return None


def is_folder(url: str) -> bool:
    link = parse_link(url)
    return bool(link) and link.kind == "folder" and link.sub_kind != "file"


def file_in_folder(url: str, handle: str) -> str:
    """A link to one file inside a folder link (folder/HANDLE#KEY/file/SUB), so only that file downloads."""
    link = parse_link(url)
    return Link(link.kind, link.handle, link.key, "file", handle).url


def find_links(text: str) -> list[str]:
    """Every MEGA link in a block of pasted text, normalised, in order, without repeats."""
    out = []
    for raw in _ANY.findall(text or ""):
        link = parse_link(raw)
        if link and link.url not in out:
            out.append(link.url)
    return out


# ---- MEGA's crypto, just enough to read names and sizes ----------------------------------

def b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode().replace("+", "-").replace("/", "_").rstrip("=")


def _a32(b: bytes) -> list[int]:
    b = b + b"\0" * (-len(b) % 4)
    return list(struct.unpack(">%dI" % (len(b) // 4), b))


def _bytes(a: list[int]) -> bytes:
    return struct.pack(">%dI" % len(a), *a)


def _fold(key: bytes) -> bytes:
    """A file key is 32 bytes; the AES key is its two halves XORed together."""
    if len(key) < 32:
        return key[:16]
    k = _a32(key)
    return _bytes([k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7]])


def _ecb_decrypt(key: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def _cbc(key: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def decrypt_attr(attr: str, aes_key: bytes) -> dict:
    raw = _cbc(aes_key, b64d(attr), False)
    if not raw.startswith(b"MEGA"):
        raise MegaError("error", "The link's key doesn't match (wrong or incomplete link).")
    text = raw[4:].rstrip(b"\0").decode("utf-8", "replace")
    return json.loads(text)


def encrypt_attr(attrs: dict, aes_key: bytes) -> str:
    """Only used by the tests, to build a fake MEGA answer."""
    raw = b"MEGA" + json.dumps(attrs).encode()
    raw += b"\0" * (-len(raw) % 16)
    return b64e(_cbc(aes_key, raw, True))


class PublicApi:
    """Reads names and sizes of a public file or folder link. No sign-in, no download."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=30)
        self.seq = random.randint(0, 0xFFFFFFFF)

    async def _call(self, payload, params=None):
        self.seq += 1
        try:
            r = await self.client.post(API, params={"id": self.seq, **(params or {})}, json=[payload])
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MegaError("error", f"Couldn't reach MEGA: {e.__class__.__name__}") from e
        res = data[0] if isinstance(data, list) and data else data
        if isinstance(res, int) and res < 0:
            if res in (-9, -16, -11, -14):
                raise MegaError("dead", "MEGA says this link no longer exists or was taken down.")
            if res in (-17, -18):
                raise MegaError("quota", "MEGA is busy or over quota right now. Try again later.")
            raise MegaError("error", f"MEGA answered with error {res}.")
        return res

    async def transfer_quota(self, sid: str) -> dict | None:
        """{"used", "max"} in bytes for a signed-in account, or None when MEGA doesn't say. sid is MEGAcmd's
        session; it's only sent to MEGA, never logged or stored."""
        res = await self._call({"a": "uq", "xfer": 1, "pro": 1}, {"sid": sid})
        if not isinstance(res, dict) or not res.get("mxfer"):
            return None
        used = int(res.get("caxfer") or 0) + int(res.get("csxfer") or 0)
        return {"used": used, "max": int(res["mxfer"])}

    async def preview(self, url: str) -> Preview:
        link = parse_link(url)
        if link is None:
            raise MegaError("error", "That doesn't look like a MEGA file or folder link.")
        key = b64d(link.key)
        if link.kind == "file":
            res = await self._call({"a": "g", "p": link.handle})
            attrs = decrypt_attr(res["at"], _fold(key))
            name = attrs.get("n", "file")
            return Preview(name=name, size=int(res.get("s", 0)), kind="file", files=[{"path": name, "size": int(res.get("s", 0))}])
        res = await self._call({"a": "f", "c": 1, "ca": 1, "r": 1}, params={"n": link.handle})
        return folder_preview(res.get("f", []), key, link.sub_handle)

    async def close(self):
        await self.client.aclose()


def folder_preview(nodes: list, folder_key: bytes, only: str = "") -> Preview:
    """Builds the file tree of a folder link from MEGA's node list."""
    by_handle = {}
    unreadable = []
    for n in nodes:
        attrs = _node_attrs(n, folder_key)
        if attrs is None:
            if n.get("t", 0) == 0:
                unreadable.append(int(n.get("s", 0)))
            continue
        by_handle[n["h"]] = {"name": attrs.get("n", ""), "parent": n.get("p", ""), "type": n.get("t", 0), "size": int(n.get("s", 0))}

    def path_of(h: str) -> str:
        parts, seen = [], set()
        while h in by_handle and h not in seen:
            seen.add(h)
            parts.append(by_handle[h]["name"])
            h = by_handle[h]["parent"]
        return "/".join(reversed(parts))

    def under(h: str, root: str) -> bool:
        seen = set()
        while h in by_handle and h not in seen:
            if h == root:
                return True
            seen.add(h)
            h = by_handle[h]["parent"]
        return False

    roots = [h for h, n in by_handle.items() if n["parent"] not in by_handle]
    root = only if only in by_handle else (roots[0] if roots else "")
    files = []
    for h, n in by_handle.items():
        if n["type"] == 0 and (not root or under(h, root)):
            files.append({"path": path_of(h), "size": n["size"], "h": h})
    files.sort(key=lambda f: f["path"].lower())
    name = by_handle[root]["name"] if root in by_handle else "folder"
    if root in by_handle and by_handle[root]["type"] == 0:
        return Preview(name=name, size=by_handle[root]["size"], kind="file", files=files)
    return Preview(name=name, size=sum(f["size"] for f in files), kind="folder", files=files,
                   unreadable=len(unreadable), unreadable_size=sum(unreadable))


def _node_attrs(n: dict, folder_key: bytes) -> dict | None:
    """A node's name. Its 'k' can hold several 'handle:key' pairs (one per share it's in, so a file in
    a folder inside another shared folder has two); only the pair for this link's share opens it,
    and it isn't always the first."""
    for piece in str(n.get("k", "")).split("/"):
        if ":" not in piece:
            continue
        try:
            node_key = _ecb_decrypt(folder_key, b64d(piece.split(":", 1)[1]))
            return decrypt_attr(n["a"], _fold(node_key))
        except (KeyError, ValueError, MegaError):
            continue
    return None


# ---- MEGAcmd ----------------------------------------------------------------------------------

def classify(output: str) -> str:
    t = output.lower()
    if re.search(r"over ?quota|transfer quota|bandwidth|quota exceeded|transfer limit", t):
        return "quota"
    if re.search(r"\bmac\b|fingerprint|integrity|checksum|corrupt", t):
        return "integrity"
    if re.search(r"not found|enoent|eblocked|taken down|no longer available|invalid link|access denied", t):
        return "dead"
    if re.search(r"not logged in|login required|log in first", t):
        return "auth"
    return "error"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class MegaCmd:
    def __init__(self, home: Path, bin_dir: str = ""):
        self.home = home
        self.bin_dir = bin_dir

    def _exe(self, name: str) -> str | None:
        if self.bin_dir:
            p = Path(self.bin_dir) / name
            return str(p) if p.exists() else None
        return shutil.which(name)

    def installed(self) -> bool:
        return self._exe("mega-get") is not None

    def _env(self) -> dict:
        self.home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        return env

    async def run(self, name: str, *args: str, timeout: float = 60) -> tuple[int, str]:
        exe = self._exe(name)
        if exe is None:
            raise MegaError("missing", "MEGAcmd isn't installed in this container.")
        proc = await asyncio.create_subprocess_exec(
            exe, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self._env())
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise MegaError("error", f"MEGAcmd didn't answer within {int(timeout)} seconds.")
        return proc.returncode, out.decode("utf-8", "replace")

    async def whoami(self) -> str:
        """The signed-in e-mail, or '' when not signed in."""
        try:
            rc, out = await self.run("mega-whoami", timeout=30)
        except MegaError:
            return ""
        m = re.search(r"e-?mail:\s*(\S+)", out, re.I)
        return m.group(1) if rc == 0 and m else ""

    async def details(self) -> str:
        try:
            rc, out = await self.run("mega-whoami", "-l", timeout=30)
        except MegaError:
            return ""
        keep = [ln.strip() for ln in out.splitlines() if re.search(r"quota|storage|pro|account type|transfer", ln, re.I)]
        return "\n".join(keep[:6])

    async def session(self) -> str:
        """MEGAcmd's (secret) session id, for asking MEGA how much transfer allowance is left."""
        try:
            rc, out = await self.run("mega-session", timeout=30)
        except MegaError:
            return ""
        m = re.search(r"session is:\s*(\S+)", out)
        return m.group(1) if rc == 0 and m else ""

    async def login(self, email: str, password: str, code: str = "") -> None:
        args = [email, password] + ([f"--auth-code={code}"] if code else [])
        rc, out = await self.run("mega-login", *args, timeout=90)
        if rc != 0 and "already logged in" not in out.lower():
            raise MegaError("auth", out.strip().splitlines()[-1] if out.strip() else "MEGA sign-in failed.")

    async def logout(self) -> None:
        await self.run("mega-logout", timeout=30)

    async def download(self, url: str, dest: Path, progress=None, cancel: asyncio.Event | None = None) -> None:
        """Downloads a public link into dest, calling progress(bytes_on_disk) every couple of seconds."""
        exe = self._exe("mega-get")
        if exe is None:
            raise MegaError("missing", "MEGAcmd isn't installed in this container.")
        dest.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            exe, url, str(dest) + "/", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=self._env())
        reader = asyncio.create_task(proc.stdout.read())
        try:
            while True:
                done, _ = await asyncio.wait([reader], timeout=2)
                if progress:
                    progress(dir_size(dest))
                if done:
                    break
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    raise MegaError("error", "Stopped.")
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
        out = reader.result().decode("utf-8", "replace") if reader.done() and not reader.cancelled() else ""
        # mega-get checks the quota before it starts and can stop without an error code.
        if proc.returncode != 0 or "transfer not started" in out.lower():
            last = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
            raise MegaError(classify(out), " ".join(last) or f"mega-get stopped with code {proc.returncode}.")


class Mega:
    """What the rest of the app uses: preview through the public API, download through MEGAcmd."""

    def __init__(self, cmd: MegaCmd, api: PublicApi | None = None):
        self.cmd = cmd
        self.api = api or PublicApi()

    async def preview(self, url: str) -> Preview:
        return await self.api.preview(url)

    async def download(self, url: str, dest: Path, progress=None, cancel=None) -> None:
        await self.cmd.download(url, dest, progress, cancel)

    async def quota(self) -> dict | None:
        """Transfer allowance used and total for the signed-in account, or None when not known."""
        sid = await self.cmd.session()
        if not sid:
            return None
        try:
            return await self.api.transfer_quota(sid)
        except MegaError as e:
            log.info("Couldn't read the MEGA transfer quota: %s", e)
            return None

    async def close(self):
        await self.api.close()
