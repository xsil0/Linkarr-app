import os

from linkarr import logbuf, naming
from linkarr.mega import (_fold, b64e, classify, decrypt_attr, encrypt_attr, find_links, folder_preview,
                          parse_link)
from linkarr.parse import parse_name
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def test_parse_movie_and_series_names():
    m = parse_name("Dune.Part.Two.2024.2160p.WEB-DL.mkv")
    assert (m["title"], m["year"], m["kind"], m["resolution"]) == ("Dune Part Two", 2024, "movie", "2160p")
    s = parse_name("Severance S02 1080p")
    assert (s["title"], s["season"], s["kind"]) == ("Severance", 2, "series")
    assert parse_name("Severance.S02E03.mkv")["episode"] == 3
    f = parse_name("Friends S01-S10 Complete")
    assert f["seasons"] == list(range(1, 11)) and f["complete"]
    assert parse_name("tvshowname season1")["season"] == 1
    assert parse_name("the office (us) - stagione 9")["title"] == "The Office (Us)"


def test_naming_patterns():
    assert naming.movie_folder("Dune: Part Two", 2024) == "Dune - Part Two (2024)"
    assert naming.fill("{show} - S{season:00}E{episode:00}", show="X", season=2, episode=3) == "X - S02E03"
    assert naming.season_folder(4) == "Season 4"
    assert "/" not in naming.safe("AC/DC: Live") and ":" not in naming.safe("AC/DC: Live")


def test_matching_exact_close_and_none():
    f, s = naming.best_match("Severance", ["Severance (2022)", "Seven (1995)"], 2022)
    assert f == "Severance (2022)" and s >= naming.EXACT
    f, s = naming.best_match("The Office", ["The Office (US) (2005)"], 2005)
    assert naming.CLOSE <= s < naming.EXACT
    _, s = naming.best_match("Dune", ["Severance (2022)"], 2021)
    assert s < naming.CLOSE
    # Same name, years far apart: a remake, not the same title.
    _, s = naming.best_match("Dune", ["Dune (1984)"], 2021)
    assert s < naming.EXACT


def test_link_parsing():
    key = "B" * 43
    text = f"look https://mega.nz/folder/AbCd1234#{key} and https://mega.nz/#!XyZ98765!{key} and https://mega.nz/folder/AbCd1234#{key}"
    links = find_links(text)
    assert links == [f"https://mega.nz/folder/AbCd1234#{key}", f"https://mega.nz/file/XyZ98765#{key}"]
    sub = parse_link(f"https://mega.nz/folder/AbCd1234#{key}/folder/Sub12345")
    assert sub.sub_handle == "Sub12345"
    assert find_links("not a link") == []


def test_folder_preview_decrypts_names():
    folder_key = os.urandom(16)
    ecb = lambda k, d: (lambda e: e.update(d) + e.finalize())(Cipher(algorithms.AES(k), modes.ECB()).encryptor())  # noqa: E731
    nodes = []

    def node(h, parent, name, t, size=0):
        key = os.urandom(16) if t == 1 else os.urandom(32)
        nodes.append({"h": h, "p": parent, "t": t, "s": size, "a": encrypt_attr({"n": name}, _fold(key)),
                      "k": "owner:" + b64e(ecb(folder_key, key))})

    node("root0001", "", "Severance S01", 1)
    node("file0001", "root0001", "Severance.S01E01.mkv", 0, 100)
    node("subd0001", "root0001", "Subs", 1)
    node("file0002", "subd0001", "eng.srt", 0, 5)
    pv = folder_preview(nodes, folder_key)
    assert pv.name == "Severance S01" and pv.size == 105
    assert [f["path"] for f in pv.files] == ["Severance S01/Severance.S01E01.mkv", "Severance S01/Subs/eng.srt"]


def test_wrong_key_is_reported():
    attr = encrypt_attr({"n": "x"}, b"k" * 16)
    try:
        decrypt_attr(attr, b"j" * 16)
        raise AssertionError("should fail")
    except Exception as e:
        assert "key" in str(e)


def test_classify_megacmd_errors():
    assert classify("ERR: Transfer quota exceeded") == "quota"
    assert classify("Transfer failed: MAC verification failed") == "integrity"
    assert classify("ERR: Not found: link taken down") == "dead"
    assert classify("Not logged in") == "auth"
    assert classify("something else") == "error"


def test_logs_hide_keys_and_tokens():
    line = logbuf.mask(f"Downloading https://mega.nz/file/Abc12345#{'K' * 43} via bot123456:{'T' * 35} X-Plex-Token=secret1")
    assert "K" * 10 not in line and "T" * 10 not in line and "secret1" not in line
    assert "Abc12345" in line

