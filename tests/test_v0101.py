"""0.10.1: rar files over 2 GB (and locked ones) unpack with unrar and 7-Zip instead of unar, errors say
what's actually wrong, and Fix it after an unpack failure unpacks again instead of downloading again."""
import asyncio
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import MOVIE_URL, run
from linkarr import files, jobs
from linkarr.worker import Worker


def fake_tool(tmp_path, name, body):
    """A stand-in for an unpacker: a small Python script that gets the same arguments."""
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!{sys.executable}\nimport sys, pathlib\n" + textwrap.dedent(body))
    p.chmod(0o755)
    return str(p)


def only(monkeypatch, **tools):
    monkeypatch.setattr(files.shutil, "which", lambda name: tools.get(name))


def test_reasons_are_plain():
    assert "password is wrong" in files._why("Incorrect password for a.rar\nTotal errors: 1", "2024")
    assert "needs a password" in files._why("Incorrect password for a.rar", "")
    assert "part of this archive is missing" in files._why("Cannot find volume a.part2.rar", "x")
    assert "incomplete" in files._why("a.mkv - checksum error\nUnexpected end of archive", "x")
    assert "incomplete" in files._why("Archive parsing failed! (Attempted to read more data than was available.)", "")
    assert files._why("something odd\nlast line", "") == "last line"


def test_rar_uses_unrar_and_moves_files_up(tmp_path, monkeypatch):
    unrar = fake_tool(tmp_path, "unrar", """
        args = sys.argv[1:]
        assert args[0] == "x" and "-p2024" in args, args
        dest = pathlib.Path(args[-1])
        (dest / "Film (2020)").mkdir(parents=True)
        (dest / "Film (2020)" / "Film.mkv").write_bytes(b"v" * 50)
    """)
    only(monkeypatch, unrar=unrar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Bibi.Guns.rar").write_bytes(b"rar")
    assert run(files.unpack(stage, "2024")) == []
    assert (stage / "Film (2020)" / "Film.mkv").read_bytes() == b"v" * 50
    assert not (stage / "Bibi.Guns.rar").exists()
    assert not (stage / files.UNPACK_DIR).exists()


def test_wrong_password_keeps_archive_and_leaves_nothing_half_written(tmp_path, monkeypatch):
    unrar = fake_tool(tmp_path, "unrar", """
        dest = pathlib.Path(sys.argv[-1])
        (dest / "half.mkv").write_bytes(b"x")
        print("Incorrect password for Bibi.Guns.rar")
        sys.exit(11)
    """)
    unar = fake_tool(tmp_path, "unar", "raise SystemExit('should not be tried after a clear answer')")
    only(monkeypatch, unrar=unrar, unar=unar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Bibi.Guns.rar").write_bytes(b"rar")
    problems = run(files.unpack(stage, "nope"))
    assert problems == ["Couldn't unpack Bibi.Guns.rar: the saved password is wrong. Check it on the job and try again."]
    assert sorted(p.name for p in files.walk_files(stage)) == ["Bibi.Guns.rar"]


def test_nothing_extracted_is_a_failure_not_a_delete(tmp_path, monkeypatch):
    # unar says "No files extracted." and exits 0 when it's given only the first part of a set.
    unar = fake_tool(tmp_path, "unar", "print('No files extracted.')")
    only(monkeypatch, unar=unar)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Movie.part1.rar").write_bytes(b"rar")
    problems = run(files.unpack(stage))
    assert problems and "part of this archive is missing" in problems[0]
    assert (stage / "Movie.part1.rar").exists()


def test_falls_back_when_the_first_tool_breaks(tmp_path, monkeypatch):
    unrar = fake_tool(tmp_path, "unrar", "sys.exit('Segmentation fault')")
    seven = fake_tool(tmp_path, "7z", """
        dest = pathlib.Path(next(a[2:] for a in sys.argv if a.startswith("-o")))
        (dest / "Movie.mkv").write_bytes(b"v")
    """)
    only(monkeypatch, unrar=unrar, **{"7z": seven})
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "Movie.rar").write_bytes(b"rar")
    assert run(files.unpack(stage)) == []
    assert (stage / "Movie.mkv").exists()


@pytest.mark.skipif(not (shutil.which("rar") and shutil.which("unrar")), reason="needs rar and unrar installed")
def test_real_locked_rar5_with_encrypted_headers(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "Bibi.mkv").write_bytes(b"v" * 100_000)
    stage = tmp_path / "stage"
    stage.mkdir()
    subprocess.run(["rar", "a", "-ma5", "-idq", "-hp2024", str(stage / "Bibi.Guns.rar"), "Bibi.mkv"], cwd=src, check=True)
    assert "password is wrong" in run(files.unpack(stage, "nope"))[0]
    assert run(files.unpack(stage, "2024")) == []
    assert (stage / "Bibi.mkv").read_bytes() == b"v" * 100_000


async def drive(w: Worker, ticks: int = 8):
    w.last_inbox = w.last_shows = time.time()
    for _ in range(ticks):
        await w.tick()
        pending = list(w.downloads.values()) + list(w.processing.values())
        if pending:
            await asyncio.gather(*pending)


def test_fix_it_after_unpack_failure_unpacks_again_without_downloading(ctx, libs, mega, tmp_path, monkeypatch):
    mega.add(MOVIE_URL, "Dune.Part.Two.2024.2160p.rar", [("Dune.Part.Two.2024.2160p.rar", 2000)], kind="file")
    unrar = fake_tool(tmp_path, "unrar", """
        args = sys.argv[1:]
        if "-p2024" not in args:
            print("Incorrect password for Dune.Part.Two.2024.2160p.rar")
            sys.exit(11)
        (pathlib.Path(args[-1]) / "Dune.Part.Two.2024.2160p.mkv").write_bytes(b"v" * 3000)
    """)
    only(monkeypatch, unrar=unrar)
    monkeypatch.setattr(files, "probe", lambda f: asyncio.sleep(0))

    async def go():
        j = await jobs.add_link(ctx, MOVIE_URL)
        await jobs.update_job(ctx, j["id"], {"archive_password": "wrong"})
        jobs.start_job(ctx, j["id"])
        w = Worker(ctx)
        await drive(w)
        v = jobs.job_view(ctx.db, j["id"])
        link = v["links"][0]
        assert link["status"] == "error" and "password is wrong" in link["error"]
        await jobs.update_job(ctx, j["id"], {"archive_password": "2024"})
        assert jobs.retry_link(ctx, link["id"]) == "unpack"
        await drive(w)
        return j["id"]

    jid = run(go())
    assert jobs.job_view(ctx.db, jid)["status"] == "done"
    assert mega.downloads.count(MOVIE_URL) == 1
