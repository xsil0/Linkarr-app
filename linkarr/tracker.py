"""What's missing from a show you track: aired episodes (by TMDB) that aren't in its folder."""


def aired(det: dict, today: str) -> dict:
    """{season: episodes aired so far}. Specials (season 0) are left out."""
    out = {}
    last_s, last_e = det.get("last_season"), det.get("last_episode")
    for s in det.get("seasons", []):
        n, count, air = s.get("season"), s.get("episodes") or 0, s.get("air") or ""
        if not n or not count or not air or air > today:
            continue
        if last_s and n > last_s:
            continue
        if n == last_s and last_e:
            count = min(count, last_e)
        out[n] = count
    return out


def missing(det: dict, have: dict, today: str) -> dict:
    """{"seasons": [seasons with nothing on disk], "episodes": {season: [missing numbers]},
    "counts": {season: n missing when the files don't say which}}. Empty lists mean complete."""
    out = {"seasons": [], "episodes": {}, "counts": {}}
    for n, count in aired(det, today).items():
        slot = have.get(n)
        if not slot or (not slot["eps"] and not slot["unknown"]):
            out["seasons"].append(n)
            continue
        gone = [e for e in range(1, count + 1) if e not in slot["eps"]]
        if not gone:
            continue
        if slot["unknown"] >= len(gone):
            continue  # files without episode numbers probably fill the gaps
        if slot["unknown"]:
            out["counts"][n] = len(gone) - slot["unknown"]
        else:
            out["episodes"][n] = gone
    return out


def _ranges(nums: list[int]) -> str:
    parts, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i + 1 == len(nums) or nums[i + 1] != n + 1:
            parts.append(f"{start}" if start == n else f"{start}–{n}")
            start = None
    return ", ".join(parts)


def describe(miss: dict) -> str:
    """'Missing season 3; S2 episodes 4–5' or '' when nothing is missing."""
    if not miss:
        return ""
    bits = []
    if miss.get("seasons"):
        ss = miss["seasons"]
        bits.append(("season " if len(ss) == 1 else "seasons ") + _ranges(sorted(ss)))
    for s, eps in sorted((int(k), v) for k, v in (miss.get("episodes") or {}).items()):
        bits.append(f"S{s} episode{'s' if len(eps) > 1 else ''} {_ranges(eps)}")
    for s, n in sorted((int(k), v) for k, v in (miss.get("counts") or {}).items()):
        bits.append(f"{n} episode{'s' if n > 1 else ''} of S{s}")
    return "Missing " + "; ".join(bits) if bits else ""


def owned_seasons(have: dict) -> str:
    """'seasons 1–2' for the message that a new season is out."""
    ss = sorted(n for n, v in have.items() if v["eps"] or v["unknown"])
    if not ss:
        return "none of it"
    return ("season " if len(ss) == 1 else "seasons ") + _ranges(ss)


def gaps(have: dict, seasons) -> dict:
    """Episodes missing between the ones you have, without asking TMDB: S02 with E01, E02 and E04 is
    missing E03. {season: [missing numbers]} for the seasons given; a season with files that have no
    episode number is left out (they may be the missing ones)."""
    out = {}
    for s in sorted(set(seasons)):
        slot = have.get(s)
        if not slot or slot["unknown"] or not slot["eps"]:
            continue
        miss = [e for e in range(1, max(slot["eps"])) if e not in slot["eps"]]
        if miss:
            out[s] = miss
    return out


def gap_message(show: str, season: int, miss: list[int], eps: set) -> str:
    """The Telegram message for one season with gaps, one fact per line."""
    import html
    one = len(miss) == 1
    return (f"🧩 <b>{html.escape(show)}</b>\nSeason {season} is missing episode{'' if one else 's'} {_ranges(miss)}.\n"
            f"You have episode{'s' if len(eps) != 1 else ''} {_ranges(sorted(eps))}.")
