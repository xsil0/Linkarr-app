import zipfile
from pathlib import Path

from conftest import run
from linkarr import files


def touch(p: Path, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_clean_removes_junk_keeps_media(tmp_path):
    touch(tmp_path / "Movie.mkv", 1000)
    touch(tmp_path / "Movie.en.srt")
    touch(tmp_path / "info.nfo")
    touch(tmp_path / "RARBG.txt")
    touch(tmp_path / "Sample" / "sample.mkv", 100)
    removed = files.clean(tmp_path)
    left = sorted(str(p.relative_to(tmp_path)) for p in files.walk_files(tmp_path))
    assert left == ["Movie.en.srt", "Movie.mkv"]
    assert len(removed) == 3


def test_unpack_zip_and_block_path_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(files.shutil, "which", lambda name: None)  # use the built-in zip reader
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Film (2020)/Film.mkv", b"x" * 50)
    problems = run(files.unpack(tmp_path))
    assert problems == []
    assert (tmp_path / "Film (2020)" / "Film.mkv").exists()
    assert not z.exists()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../../escape.mkv", b"x")
    problems = run(files.unpack(tmp_path))
    assert problems and not (tmp_path.parent / "escape.mkv").exists()


def test_plan_movie_renames_video_and_subs(tmp_path):
    root = tmp_path / "stage"
    v = touch(root / "Dune.Part.Two.2024.2160p.mkv", 1000)
    s = touch(root / "Subs" / "English.srt")
    target = tmp_path / "Movies" / "Dune Part Two (2024)"
    plan = files.plan_movie([(v, root), (s, root)], target, target.name)
    dsts = sorted(d.name for _, d in plan)
    assert dsts == ["Dune Part Two (2024).en.srt", "Dune Part Two (2024).mkv"]


def test_plan_series_puts_episodes_in_season_folders(tmp_path):
    root = tmp_path / "stage"
    e1 = touch(root / "Show.S02E01.1080p.mkv")
    e2 = touch(root / "Show.S02E02.1080p.mkv")
    show = tmp_path / "TV" / "Severance (2022)"
    plan = files.plan_series([(e1, root, 2), (e2, root, 2)], show, "Severance", "Season {season}",
                             "{show} - S{season:00}E{episode:00}", True)
    assert sorted(str(d.relative_to(show)) for _, d in plan) == ["Season 2/Severance - S02E01.mkv", "Season 2/Severance - S02E02.mkv"]


def test_apply_plan_never_overwrites_and_undo_restores(tmp_path):
    media = tmp_path / "media"
    target = media / "Movies" / "Film (2020)"
    old = touch(target / "Film (2020).mkv", 5)
    new = touch(tmp_path / "stage" / "x.mkv", 9)
    rec = files.apply_plan([(new, target / "Film (2020).mkv")], media, 1, replace=False)
    names = sorted(p.name for p in target.iterdir())
    assert names == ["Film (2020) (2).mkv", "Film (2020).mkv"]
    assert old.stat().st_size == 5
    problems = files.undo({"moves": rec["moves"], "recycled": rec["recycled"], "created_dirs": rec["created_dirs"]}, tmp_path / "undone")
    assert problems == []
    assert sorted(p.name for p in target.iterdir()) == ["Film (2020).mkv"]


def test_replace_recycles_old_copy(tmp_path):
    media = tmp_path / "media"
    target = media / "Movies" / "Film (2020)"
    touch(target / "Film (2020).mkv", 5)
    new = touch(tmp_path / "stage" / "x.mkv", 9)
    rec = {"moves": [], "recycled": [], "created_dirs": []}
    files.recycle_old_videos(target, media, 7, rec)
    out = files.apply_plan([(new, target / "Film (2020).mkv")], media, 7, replace=True)
    assert (target / "Film (2020).mkv").stat().st_size == 9
    recycled = rec["recycled"] + out["recycled"]
    assert recycled and all(".linkarr-recycle" in r["to"] for r in recycled)
    assert Path(recycled[0]["to"]).exists()


def test_seasons_on_disk(tmp_path):
    show = tmp_path / "Severance (2022)"
    touch(show / "Season 1" / "a.mkv")
    touch(show / "Season 02" / "b.mkv")
    (show / "Season 3").mkdir()
    s = files.seasons_in(show)
    assert s == {1: 1, 2: 1}
