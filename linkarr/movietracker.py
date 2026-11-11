"""Movie Tracker: movies you want, and when they're out on Blu-ray, to buy or rent, or on a streaming service.
TMDB has the dates (by country) and the streaming services; Linkarr checks once a day and tells you once."""
import json
import time

from .db import Database, now


def day(date: str) -> str:
    """'2024-05-14' as '14 May 2024'."""
    try:
        return time.strftime("%d %b %Y", time.strptime(date, "%Y-%m-%d")).lstrip("0")
    except ValueError:
        return date


def name(m: dict) -> str:
    return f"{m['title']} ({m['year']})" if m.get("year") else m["title"]


def events(movie: dict, info: dict, notified: dict, today: str) -> list[str]:
    """What's new since the last check, one message each. notified is updated so each fires once."""
    out = []
    head = name(info if info.get("title") else movie)
    for key, label, icon in (("physical", "Blu-ray and DVD", "💿"), ("digital", "Digital (buy or rent)", "🛒")):
        date = info.get(key) or ""
        if not date:
            continue
        if date <= today and notified.get(key) != "out":
            out.append(f"{icon} {head}\nOut on {label}: {day(date)}")
            notified[key] = "out"
        elif date > today and notified.get(key) != date:
            out.append(f"📅 {head}\n{label} on {day(date)}")
            notified[key] = date
    told = set(notified.get("streaming", []))
    new = [s for s in info.get("streaming", []) if s not in told]
    if new:
        out.append(f"📺 {head}\nNow streaming on {', '.join(new)}")
    notified["streaming"] = sorted(told | set(info.get("streaming", [])))
    return out


def save(db: Database, tmdb_id: int, info: dict, notified: dict) -> None:
    db.run("UPDATE movies SET title=?, year=?, poster=?, theatrical=?, digital=?, physical=?, streaming=?, notified=?, checked_at=? "
           "WHERE tmdb_id=?", (info["title"], info["year"], info["poster"], info["theatrical"], info["digital"], info["physical"],
                               json.dumps(info.get("streaming", [])), json.dumps(notified), now(), tmdb_id))


def view(r) -> dict:
    """A row for the app: one line per fact, ready to show."""
    m = dict(r)
    m["streaming"] = json.loads(m["streaming"] or "[]")
    m.pop("notified", None)
    today = time.strftime("%Y-%m-%d")
    lines = []
    for key, label in (("physical", "Blu-ray"), ("digital", "Digital"), ("theatrical", "Theaters")):
        if m[key]:
            lines.append({"label": label, "value": day(m[key]), "out": m[key] <= today})
    if m["streaming"]:
        lines.append({"label": "Streaming", "value": ", ".join(m["streaming"]), "out": True})
    m["lines"] = lines
    m["out"] = any(x["out"] and x["label"] != "Theaters" for x in lines)
    m["checked"] = bool(m["checked_at"])
    return m


def roundup_lines(db: Database, today: str) -> list[str]:
    """For the weekly roundup: tracked movies that came out in the past week or come out in the next two."""
    t = time.mktime(time.strptime(today, "%Y-%m-%d"))
    week_ago = time.strftime("%Y-%m-%d", time.localtime(t - 7 * 86400))
    soon = time.strftime("%Y-%m-%d", time.localtime(t + 14 * 86400))
    out = []
    for r in db.query("SELECT * FROM movies ORDER BY title"):
        for key, label in (("physical", "Blu-ray"), ("digital", "digital")):
            d = r[key]
            if d and week_ago <= d <= soon:
                out.append(f"• {name(dict(r))}: {label} {'out' if d <= today else 'on'} {day(d)}")
    return out
