#!/usr/bin/env python3
"""Turn the decrypted snapshots into numbers and plan changes.

    python3 ig.py pull        # first: decrypt stats/snapshots/*.enc → staging/analytics/
    python3 analyze.py [--snapshots DIR] [--out DIR]

Writes (default) projects/mostik/social-media/analytics/insights.json + insights.md.
The social-analyst agent reads them, adds judgement (comments, competitors) and proposes
grid.next.json for the ig-week skill. Stdlib only; deterministic, so the same data
always gives the same recommendations.

Why these numbers: reach is decided by shares and saves (people sending and keeping a
post) and growth by follows, so the score is 3×share rate + 2×save rate + 5×follow rate,
per 100 accounts reached. Likes are shown but don't drive decisions.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOCIAL = (HERE / "../../mostik/social-media").resolve()
GRID = (HERE / "../../mostik/.claude/skills/ig-week/grid.json").resolve()
GOAL = {"followers": 10_000, "by": "2027-02-28",
        "checkpoints": [("2026-12-31", 2_500), ("2027-01-31", 5_500), ("2027-02-28", 10_000)]}
MIN_POSTS = 2          # a series needs this many posts before it is judged
WEAK = 0.6             # below 60 % of the median score = weak
LEVELS = ["A1", "A2", "B1", "B2", "C1"]


def rate(n, reach):
    return round(100 * (n or 0) / reach, 2) if reach else 0.0


def metrics(m: dict) -> dict:
    i = m.get("insights") or {}
    reach = i.get("reach") or 0
    out = {"reach": reach, "views": i.get("views"), "saved": i.get("saved") or 0, "shares": i.get("shares") or 0,
           "follows": i.get("follows") or 0, "likes": i.get("likes") or m.get("like_count") or 0,
           "comments": i.get("comments") or m.get("comments_count") or 0,
           "watch_s": round((i.get("ig_reels_avg_watch_time") or 0) / 1000, 1) or None}
    out["save_rate"], out["share_rate"], out["follow_rate"] = rate(out["saved"], reach), rate(out["shares"], reach), rate(out["follows"], reach)
    out["score"] = round(3 * out["share_rate"] + 2 * out["save_rate"] + 5 * out["follow_rate"], 2)
    return out


def group(rows, key):
    g = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            g[r[key]].append(r)
    return {k: {"posts": len(v), "score": round(statistics.mean(x["m"]["score"] for x in v), 2),
                "reach": round(statistics.mean(x["m"]["reach"] for x in v)),
                "save_rate": round(statistics.mean(x["m"]["save_rate"] for x in v), 2),
                "share_rate": round(statistics.mean(x["m"]["share_rate"] for x in v), 2)}
            for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def pace(snaps: list[dict]) -> dict:
    pts = [(s["date"], (s.get("account") or {}).get("followers_count")) for s in snaps]
    pts = [(d, n) for d, n in pts if n is not None]
    if not pts:
        return {}
    today, now = pts[-1]
    days_left = (dt.date.fromisoformat(GOAL["by"]) - dt.date.fromisoformat(today)).days
    need_week = round(7 * (GOAL["followers"] - now) / days_left) if days_left > 0 else None
    got_week = None
    if len(pts) >= 2:
        d0, n0 = pts[max(0, len(pts) - 5)]
        span = (dt.date.fromisoformat(today) - dt.date.fromisoformat(d0)).days
        got_week = round(7 * (now - n0) / span) if span else None
    nxt = next(((d, n) for d, n in GOAL["checkpoints"] if d >= today), None)
    out = {"date": today, "followers": now, "needed_per_week": need_week, "actual_per_week": got_week,
           "history": pts[-10:]}
    if nxt:
        out["next_checkpoint"] = {"date": nxt[0], "target": nxt[1]}
    if got_week and got_week > 0:
        out["projected_10k"] = str(dt.date.fromisoformat(today) + dt.timedelta(weeks=(GOAL["followers"] - now) / got_week))
    out["on_track"] = bool(got_week and need_week and got_week >= need_week)
    return out


QUESTION = re.compile(r"\?|^(how|what|why|can|is|does|which|как|что|почему|зачем|где|когда)\b", re.I)


def questions(rows):
    qs = []
    for r in rows:
        for c in r.get("comments") or []:
            t = (c.get("text") or "").strip()
            if len(t) > 8 and QUESTION.search(t):
                qs.append({"text": t[:200], "likes": c.get("like_count", 0), "on": r.get("title")})
    return sorted(qs, key=lambda q: -q["likes"])[:20]


def hour_slots(rows):
    g = defaultdict(list)
    for r in rows:
        try:
            h = dt.datetime.fromisoformat(r["timestamp"].replace("+0000", "+00:00")).hour
        except (KeyError, ValueError):
            continue
        g[h].append(r["m"]["score"])
    return {f"{h:02d}:00 UTC": {"posts": len(v), "score": round(statistics.mean(v), 2)} for h, v in sorted(g.items())}


def best_online_hours(online: dict) -> list[str]:
    hrs = {int(k): v for k, v in (online or {}).items() if str(k).isdigit()}
    return [f"{h:02d}:00 UTC" for h, _ in sorted(hrs.items(), key=lambda kv: -kv[1])[:3]]


def recommend(res: dict, grid: dict) -> tuple[list[str], list[dict]]:
    recs, changes = [], []
    series = {k: v for k, v in res["by_series"].items() if v["posts"] >= MIN_POSTS}
    if len(series) >= 3:
        med = statistics.median(v["score"] for v in series.values())
        ranked = sorted(series.items(), key=lambda kv: -kv[1]["score"])
        best, worst = ranked[0], ranked[-1]
        recs.append(f"Best series: {best[0]} (score {best[1]['score']}, {best[1]['posts']} posts). Weakest: {worst[0]} ({worst[1]['score']}).")
        if worst[1]["score"] < WEAK * med:
            # Swap one slot of the weakest series for the best series *of the same post type* (a Reel series can't
            # fill a carousel slot), keeping that slot's level so the A1–C1 mix holds.
            week = grid.get("week", {})
            series_of_type = lambda typ: {s.get("series") for d in week.values() for t, s in d.items() if t == typ}
            for day, slots in week.items():
                for typ, slot in slots.items():
                    if slot.get("series") != worst[0] or changes:
                        continue
                    pool = [(n, v) for n, v in ranked if n in series_of_type(typ) and n != worst[0]]
                    if pool and pool[0][1]["score"] > worst[1]["score"]:
                        changes.append({"day": day, "type": typ, "from": worst[0], "to": pool[0][0], "level": slot.get("level"),
                                        "why": f"{worst[0]} scores {worst[1]['score']} vs median {round(med, 2)}; "
                                               f"{pool[0][0]} is the best {typ} series ({pool[0][1]['score']})"})
    else:
        recs.append(f"Too early to judge series: only {len(series)} series have {MIN_POSTS}+ posts with numbers. Keep the grid.")
    k = res["by_kind"]
    if "reel" in k and "post" in k:
        hi = max(k, key=lambda x: k[x]["reach"])
        recs.append(f"Reach: reels {k['reel']['reach']} vs carousels {k['post']['reach']} average; {hi}s reach further. "
                    f"Saves: reels {k['reel']['save_rate']}% vs carousels {k['post']['save_rate']}%.")
    lv = {l: v for l, v in res["by_level"].items() if v["posts"] >= MIN_POSTS}
    if len(lv) >= 3:
        top = max(lv, key=lambda l: lv[l]["score"])
        recs.append(f"Level that performs best: {top} (score {lv[top]['score']}). Keep every level at least once a week (bio promise A1–C1).")
    if res.get("best_online_hours"):
        recs.append(f"Followers are most online at {', '.join(res['best_online_hours'])}. Current slots: carousel 12:00, reel 17:00 UTC.")
    p = res.get("pace") or {}
    if p.get("needed_per_week"):
        if p.get("actual_per_week") is None:
            recs.append(f"Need +{p['needed_per_week']} followers a week to reach 10k by {GOAL['by']}. Growth rate appears after two snapshots.")
        elif not p["on_track"]:
            recs.append(f"Behind: +{p['actual_per_week']}/week vs +{p['needed_per_week']} needed. Double the best series, push shareable hooks, "
                        "and consider Collab posts (plan, January lever).")
        else:
            recs.append(f"On track: +{p['actual_per_week']}/week vs +{p['needed_per_week']} needed.")
    if res.get("questions"):
        recs.append(f"{len(res['questions'])} follower questions in comments: turn the top ones into next week's posts.")
    return recs, changes


def analyze(snap_dir: Path) -> dict:
    snaps = [json.loads(f.read_text()) for f in sorted(snap_dir.glob("*.json"))]
    if not snaps:
        raise SystemExit(f"no snapshots in {snap_dir}: run `python3 ig.py pull` (needs a stats run with the token)")
    last = snaps[-1]
    rows = []
    for m in last.get("media", []):
        r = dict(m)
        r["m"] = metrics(m)
        r["title"] = (m.get("caption") or "").split("\n")[0][:80]
        r["from_plan"] = bool(m.get("queue_id"))
        rows.append(r)
    plan = [r for r in rows if r["from_plan"]]
    legacy = [r for r in rows if not r["from_plan"]]
    def top(rs, n=10):
        return [{"title": r["title"], "kind": r["kind"], "date": (r.get("timestamp") or "")[:10], "series": r.get("series"),
                 "level": r.get("level"), "link": r.get("permalink"), **r["m"]}
                for r in sorted(rs, key=lambda r: (-r["m"]["score"], -(r["m"]["views"] or 0)))[:n]]
    res = {"generated": str(dt.date.today()), "snapshot": last["date"], "posts_analysed": len(rows),
           "pace": pace(snaps), "by_kind": group(rows, "kind"), "by_series": group(plan, "series"),
           "by_level": group(plan, "level"), "by_hour": hour_slots(rows),
           "best_online_hours": best_online_hours(last.get("online_followers")),
           "audience": {k: dict(sorted(v.items(), key=lambda kv: -kv[1])[:8]) for k, v in (last.get("demographics") or {}).items()
                        if isinstance(v, dict) and not k.startswith("_")},
           "account_7d": {k: v for k, v in (last.get("account_insights_7d") or {}).items() if not k.startswith("_")},
           "top_plan_posts": top(plan), "top_older_posts": top(legacy),
           "weakest_plan_posts": top(plan, len(plan))[-5:],
           "questions": questions(rows),
           "api_gaps": {k: v for k, v in {"account": (last.get("account_insights_7d") or {}).get("_errors"),
                                          "demographics": (last.get("demographics") or {}).get("_errors"),
                                          "online": (last.get("online_followers") or {}).get("_error")}.items() if v}}
    grid = json.loads(GRID.read_text()) if GRID.exists() else {}
    res["recommendations"], res["grid_changes"] = recommend(res, grid)
    return res


def table(rows, cols):
    if not rows:
        return "_none yet_\n"
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n" for r in rows)


def to_md(res: dict) -> str:
    p = res.get("pace") or {}
    md = [f"# Instagram insights · snapshot {res['snapshot']}", "",
          f"Made by `projects/tools/ig-autopilot/analyze.py` on {res['generated']} from {res['posts_analysed']} posts. "
          "Score = 3×share rate + 2×save rate + 5×follow rate (per 100 reached).", "",
          "## Recommendations", *[f"- {r}" for r in res["recommendations"]], ""]
    if res["grid_changes"]:
        md += ["## Proposed grid change", *[f"- {c['day']} {c['type']}: {c['from']} → {c['to']} (level {c['level']}; {c['why']})" for c in res["grid_changes"]], ""]
    if p:
        md += ["## Pace to 10k", f"- Followers {p.get('followers')} · needed +{p.get('needed_per_week')}/week · actual "
               f"{('+' + str(p['actual_per_week'])) if p.get('actual_per_week') is not None else 'n/a'}/week"
               + (f" · projected 10k on {p['projected_10k']}" if p.get("projected_10k") else ""), ""]
    groups = [("By series", res["by_series"]), ("By level", res["by_level"]), ("Reels vs carousels", res["by_kind"]),
              ("By posting hour", res["by_hour"])]
    for title, g in groups:
        md += [f"## {title}", table([{"": k, **v} for k, v in g.items()], ["", "posts", "score", "reach", "save_rate", "share_rate"]
                                    if title != "By posting hour" else ["", "posts", "score"])]
    cols = ["date", "kind", "level", "series", "title", "views", "reach", "saved", "shares", "follows", "score", "link"]
    md += ["## Best posts from the plan", table(res["top_plan_posts"], cols),
           "## Best older posts (before the plan: what went viral)", table(res["top_older_posts"], cols)]
    if res["audience"]:
        md += ["## Audience"] + [f"- **{k}**: " + ", ".join(f"{a} {b}" for a, b in v.items()) for k, v in res["audience"].items()] + [""]
    if res["questions"]:
        md += ["## What followers ask (comments)"] + [f"- {q['text']} ({q['likes']} likes)" for q in res["questions"]] + [""]
    if res["api_gaps"]:
        md += ["## Data the API did not give", f"`{json.dumps(res['api_gaps'], ensure_ascii=False)[:600]}`", ""]
    return "\n".join(md) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default=str(HERE / "staging" / "analytics"))
    ap.add_argument("--out", default=str(SOCIAL / "analytics"))
    a = ap.parse_args(argv)
    res = analyze(Path(a.snapshots))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "insights.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    (out / "insights.md").write_text(to_md(res))
    print(out / "insights.md")


if __name__ == "__main__":
    main()
