"""The weekly Telegram roundup: what Linkarr filed in the past seven days, which tracked shows have
new episodes coming in the next two weeks, and how much MEGA transfer allowance is left."""
import html
import json
import time

from .db import Database, now
from .tracker import describe
from .movietracker import roundup_lines


def gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n < 1024**4 else f"{n / 1024**4:.2f} TB"


def _seasons(db: Database, job_id: int) -> str:
    ss = sorted({r["season"] for r in db.query("SELECT season FROM links WHERE job_id = ? AND status = 'moved'", (job_id,))
                 if r["season"] is not None})
    if not ss:
        return ""
    return f" season {ss[0]}" if len(ss) == 1 else f" seasons {ss[0]}–{ss[-1]}"


def _day(date: str) -> str:
    try:
        t = time.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    return time.strftime("%a %d %b", t).replace(" 0", " ")


def text(db: Database, quota: dict | None = None, today: str | None = None) -> str:
    e = html.escape
    today = today or time.strftime("%Y-%m-%d")
    soon = time.strftime("%Y-%m-%d", time.localtime(time.mktime(time.strptime(today, "%Y-%m-%d")) + 14 * 86400))
    lines = ["🗞 <b>Your week in Linkarr</b>"]
    done = db.query("SELECT id, title, year, kind FROM jobs WHERE status = 'done' AND finished_at >= ? ORDER BY finished_at",
                    (now() - 7 * 86400,))
    movies = [r for r in done if r["kind"] == "movie"]
    shows = [r for r in done if r["kind"] == "series"]
    if movies:
        lines.append("\n<b>Movies added</b>")
        lines += ["• " + e(r["title"]) + (f" ({r['year']})" if r["year"] else "") for r in movies[:20]]
        if len(movies) > 20:
            lines.append(f"…and {len(movies) - 20} more")
    if shows:
        lines.append("\n<b>TV added</b>")
        lines += [f"• {e(r['title'])}{_seasons(db, r['id'])}" for r in shows[:20]]
        if len(shows) > 20:
            lines.append(f"…and {len(shows) - 20} more")
    if not done:
        lines.append("\nNothing new was filed this week.")
    coming = db.query("SELECT name, next_air FROM shows WHERE watching = 1 AND next_air >= ? AND next_air <= ? ORDER BY next_air",
                      (today, soon))
    if coming:
        lines.append("\n<b>Coming up</b>")
        lines += [f"• {e(r['name'])}: new episode {_day(r['next_air'])}" for r in coming[:15]]
    films = roundup_lines(db, today)
    if films:
        lines.append("\n<b>Movie Tracker</b>")
        lines += [e(x) for x in films[:15]]
    gaps = 0
    for r in db.query("SELECT missing FROM shows WHERE watching = 1 AND missing NOT IN ('', 'null')"):
        try:
            gaps += bool(describe(json.loads(r["missing"])))
        except ValueError:
            pass
    if gaps:
        lines.append(f"\n{gaps} tracked show{'s have' if gaps != 1 else ' has'} missing episodes. /missing lists them.")
    if quota and quota.get("max"):
        left = max(0, quota["max"] - quota["used"])
        lines.append(f"\nMEGA transfer left: {gb(left)} of {gb(quota['max'])}.")
    return "\n".join(lines)


def morning_text(db: Database, since: int) -> str:
    """The morning summary: what was filed since `since`, one title per line, then what's still going and
    what needs you. '' when nothing happened, so a quiet night sends nothing."""
    from .parse import is_video, parse_name
    e = html.escape
    rows = db.query("SELECT h.moves, h.job_id, j.title, j.year, j.kind FROM history h LEFT JOIN jobs j ON j.id = h.job_id "
                    "WHERE h.ts >= ? AND h.undone_at IS NULL ORDER BY h.ts", (since,))
    per: dict = {}
    for r in rows:
        key = r["job_id"] if r["kind"] == "movie" else (r["title"] or "")
        item = per.setdefault(key, {"title": r["title"] or "Something", "year": r["year"], "kind": r["kind"] or "movie",
                                    "eps": 0, "seasons": set()})
        for m in json.loads(r["moves"] or "[]"):
            if is_video(m["to"]):
                item["eps"] += 1
                s = parse_name(m["to"].rsplit("/", 1)[-1])["season"]
                if s is not None:
                    item["seasons"].add(s)
    lines = []
    for it in per.values():
        if it["kind"] == "movie":
            lines.append(f"🎬 {e(it['title'])}" + (f" ({it['year']})" if it["year"] else ""))
        elif it["eps"]:
            ss = sorted(it["seasons"])
            where = f" S{ss[0]:02d}" if len(ss) == 1 else ""
            lines.append(f"📺 {e(it['title'])}{where}: {it['eps']} episode{'s' if it['eps'] != 1 else ''}")
    running = db.one("SELECT count(*) AS n FROM jobs WHERE status IN ('queued','downloading','waiting','processing','moving')")["n"]
    waiting = db.one("SELECT count(*) AS n FROM alerts WHERE resolved_at IS NULL")["n"]
    caps = db.one("SELECT count(*) AS n FROM jobs WHERE status = 'needs_captcha'")["n"]
    if not lines and not waiting and not caps:
        return ""
    out = ["☀️ <b>Overnight in Linkarr</b>", ""]
    out += lines or ["Nothing new came in."]
    if running or waiting or caps:
        out.append("")
    if running:
        out.append(f"⏳ {running} still downloading")
    if waiting:
        out.append(f"⚠️ {waiting} need{'s' if waiting == 1 else ''} you")
    if caps:
        out.append(f"🔒 {caps} need{'s' if caps == 1 else ''} a captcha")
    return "\n".join(out)
