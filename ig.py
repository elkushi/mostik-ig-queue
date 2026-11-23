#!/usr/bin/env python3
"""ig-autopilot: queue, check and publish the daily Instagram posts.

Laptop (Sunday, after /ig-week made the posts):
  python3 ig.py whoami                      test the token, save the account id
  python3 ig.py add --source DIR --type reel|carousel --date YYYY-MM-DD --level A1 --series S --title T
                    [--natasha PASS] [--natasha-notes TEXT]
  python3 ig.py review ID --natasha PASS|FIX [--notes TEXT]
  python3 ig.py check [--week YYYY-MM-DD | ID]
  python3 ig.py sheet --week YYYY-MM-DD     review page for the supervised weeks
  python3 ig.py approve --week YYYY-MM-DD | ID
  python3 ig.py status [--week YYYY-MM-DD]
  python3 ig.py push                        queue → GitHub, media → the Pages branch
  python3 ig.py report                      numbers → projects/mostik/social-media/instagram-stats.md

GitHub Actions:
  python3 ig.py publish-due [--dry-run] [--now 2026-11-30T17:05]
  python3 ig.py refresh-token
  python3 ig.py stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from checks import check_post
from igapi import IgError, Instagram

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
QUEUE = HERE / "queue"
STAGING = HERE / "staging"
STATS = HERE / "stats"
UTC = dt.timezone.utc


# ---------------------------------------------------------------- helpers
def load_env() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip() and not os.environ.get(k.strip()):
                    os.environ[k.strip()] = v.strip()


def set_env_value(key: str, value: str) -> None:
    env = HERE / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out) + "\n")
    env.chmod(0o600)


def api() -> Instagram:
    load_env()
    return Instagram(os.environ.get("IG_TOKEN", ""), os.environ.get("IG_USER_ID", "me"),
                     CFG["api_host"], CFG.get("api_version", ""))


def posts() -> list[dict]:
    out = []
    for f in sorted(QUEUE.glob("*/post.json")):
        out.append(json.loads(f.read_text(encoding="utf-8")))
    return sorted(out, key=lambda p: (p["date"], p["time_utc"]))


def save(post: dict) -> None:
    d = QUEUE / post["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(pid: str) -> dict:
    f = QUEUE / pid / "post.json"
    if not f.exists():
        sys.exit(f"no post {pid}")
    return json.loads(f.read_text(encoding="utf-8"))


def week_of(day: str) -> str:
    d = dt.date.fromisoformat(day)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def selected(args) -> list[dict]:
    if getattr(args, "id", None):
        return [get(args.id)]
    if getattr(args, "week", None):
        w = week_of(args.week)
        return [p for p in posts() if week_of(p["date"]) == w]
    return posts()


def due_at(p: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{p['date']}T{p['time_utc']}").replace(tzinfo=UTC)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "post"


def caption_from(md: Path) -> str:
    text = md.read_text(encoding="utf-8")
    m = re.search(r"^## Instagram\s*\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if m:
        text = m.group(1)
    else:
        text = re.sub(r"\A# .*\n", "", text)  # deliver.mjs puts "# <title>" on top
    return text.strip()


def run(cmd: list[str], cwd: Path | None = None, check=True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def public_url(p: dict, name: str) -> str:
    return f"{CFG['pages_base'].rstrip('/')}/{p['id']}/{name}"


# ---------------------------------------------------------------- laptop commands
def cmd_whoami(args):
    try:
        me = api().whoami()
    except IgError as e:
        sys.exit(f"Token check failed: {e}\n(From Russia graph.instagram.com may need the VPN on.)")
    print(json.dumps(me, ensure_ascii=False, indent=1))
    if me.get("user_id"):
        set_env_value("IG_USER_ID", str(me["user_id"]))
        print(f"Saved IG_USER_ID={me['user_id']} in .env")


def cmd_add(args):
    src = Path(args.source).resolve()
    if not src.is_dir():
        sys.exit(f"{src} is not a folder")
    pid = f"{args.date}-{args.type}-{slugify(args.slug or args.title)}"
    stage = STAGING / pid
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    caption_md = src / "caption.md"
    if not caption_md.exists():
        sys.exit(f"no caption.md in {src}")
    media = []
    if args.type == "carousel":
        slides = sorted(src.glob("slide-*.png")) or sorted(src.glob("slide-*.jpg"))
        for i, s in enumerate(slides, 1):
            out = stage / f"slide-{i:02d}.jpg"
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(s), "-q:v", "2", str(out)])
            media.append(out.name)
        rep = src / "render-report.json"
        if rep.exists():
            shutil.copy2(rep, stage / rep.name)
    else:
        video = src / "video.mp4"
        if not video.exists():
            vids = sorted(src.glob("*.mp4"))
            video = vids[0] if vids else video
        if video.exists():
            shutil.copy2(video, stage / "video.mp4")
            media.append("video.mp4")
        if (src / "cover.jpg").exists():
            shutil.copy2(src / "cover.jpg", stage / "cover.jpg")
        qa = Path(args.qa) if args.qa else src / "qa.json"
        if qa.exists():
            shutil.copy2(qa, stage / "qa.json")
    post = {
        "id": pid, "type": args.type, "date": args.date,
        "time_utc": args.time or CFG["times_utc"][args.type],
        "level": args.level.upper(), "series": args.series, "title": args.title,
        "caption": caption_from(caption_md), "media": media,
        "cover": "cover.jpg" if (stage / "cover.jpg").exists() else None,
        "source": os.path.relpath(src, HERE),
        "review": {"natasha": (args.natasha or "").upper(), "notes": args.natasha_notes or ""},
        "approved": False, "status": "queued", "attempts": 0, "error": None, "published": None, "stats": None,
    }
    finish_check(post)
    print(json.dumps({"id": pid, "check": post["checks"], "approved": post["approved"]}, ensure_ascii=False, indent=1))


def finish_check(post: dict) -> None:
    res = check_post(post, STAGING / post["id"])
    post["checks"] = {**res, "at": dt.datetime.now(UTC).isoformat(timespec="seconds")}
    if post["status"] in ("queued", "held"):
        post["status"] = "queued" if res["passed"] else "held"
    # From autopilot_from on, a post that passes the gate needs no human approval.
    post["approved"] = bool(res["passed"] and (post["approved"] or post["date"] >= CFG["autopilot_from"]))
    save(post)


def cmd_review(args):
    p = get(args.id)
    p["review"] = {"natasha": args.natasha.upper(), "notes": args.notes or ""}
    finish_check(p)
    print(p["id"], "→", p["status"], "approved" if p["approved"] else "not approved")


def cmd_check(args):
    bad = 0
    for p in selected(args):
        if p["status"] == "published":
            continue
        if not (STAGING / p["id"]).exists():
            print(f"{p['id']}: no staged media on this laptop, skipped")
            continue
        finish_check(p)
        c = p["checks"]
        mark = "PASS" if c["passed"] else "HELD"
        bad += not c["passed"]
        print(f"{mark}  {p['id']}")
        for x in c["problems"]:
            print(f"      ✗ {x}")
        for x in c["warnings"]:
            print(f"      ! {x}")
    sys.exit(1 if bad else 0)


def cmd_approve(args):
    for p in selected(args):
        if p["status"] == "published":
            continue
        if not p.get("checks", {}).get("passed"):
            print(f"NOT approved (held by checks): {p['id']}")
            continue
        p["approved"] = True
        save(p)
        print(f"approved {p['id']}")


def cmd_status(args):
    rows = selected(args)
    if not rows:
        print("queue is empty")
    for p in rows:
        flag = "✓" if p["approved"] else "·"
        extra = (p.get("published") or {}).get("permalink") or p.get("error") or ""
        print(f"{p['date']} {p['time_utc']}  {p['type']:<8} {p['level']}  {p['status']:<9} {flag}  {p['title'][:40]:<40} {extra}")


def cmd_sheet(args):
    rows = selected(args)
    if not rows:
        sys.exit("nothing for that week")
    cards = []
    for p in rows:
        d = STAGING / p["id"]
        if p["type"] == "reel":
            media = f'<video src="{d / "video.mp4"}" controls preload="metadata"></video>'
        else:
            media = '<div class="strip">' + "".join(f'<img src="{d / m}">' for m in p["media"]) + "</div>"
        c = p.get("checks", {})
        problems = "".join(f"<li>{html.escape(x)}</li>" for x in c.get("problems", []))
        cards.append(f"""<section class="{'ok' if c.get('passed') else 'held'}">
<h2>{p['date']} · {p['time_utc']} UTC · {p['type']} · <b>{p['level']}</b> · {html.escape(p['series'])}</h2>
<h3>{html.escape(p['title'])}</h3>{media}
<pre>{html.escape(p['caption'])}</pre>
<p>Checks: {'PASS' if c.get('passed') else 'HELD'} · Natasha: {html.escape(p['review'].get('natasha') or '-')} · ID <code>{p['id']}</code></p>
<ul>{problems}</ul></section>""")
    out = STAGING / f"review-{week_of(args.week)}.html"
    out.write_text(f"""<!doctype html><meta charset="utf-8"><title>Instagram week {week_of(args.week)}</title>
<style>body{{font:15px system-ui;margin:16px;background:#f6f3ee;color:#222}}section{{background:#fff;border-radius:10px;padding:12px 16px;margin:0 0 18px;border-left:6px solid #3a7}}
section.held{{border-color:#c33}}h2{{font-size:14px;margin:0;color:#666}}h3{{margin:4px 0 10px}}.strip{{display:flex;gap:8px;overflow-x:auto}}
.strip img{{height:420px}}video{{height:560px}}pre{{white-space:pre-wrap;background:#f3f3f3;padding:10px;border-radius:6px}}li{{color:#c33}}</style>
<h1>Instagram week of {week_of(args.week)}: {len(rows)} posts</h1>
<p>Approve all: <code>python3 ig.py approve --week {week_of(args.week)}</code> · one: <code>python3 ig.py approve ID</code></p>
{''.join(cards)}""", encoding="utf-8")
    print(out)


def cmd_push(args):
    if run(["git", "remote"], HERE) == "":
        sys.exit("no git remote yet: create the queue repo first (see README)")
    run(["git", "pull", "--rebase", "--autostash", "-q", "origin", "main"], HERE, check=False)
    run(["git", "add", "queue"], HERE)
    if run(["git", "status", "--porcelain", "queue"], HERE):
        run(["git", "commit", "-q", "-m", f"queue: update {dt.date.today()}"], HERE)
    run(["git", "push", "-q", "origin", "HEAD:main"], HERE)
    # Media branch: only posts not yet published, rebuilt from scratch each time,
    # so the public repo never piles up old videos.
    origin = run(["git", "remote", "get-url", "origin"], HERE)
    pending = [p for p in posts() if p["status"] != "published"]
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / ".nojekyll").write_text("")
        (t / "index.html").write_text("<!doctype html><title>media</title>Media queue for scheduled posts.")
        n = 0
        for p in pending:
            src = STAGING / p["id"]
            if not src.exists():
                print(f"warning: {p['id']} has no staged media on this laptop; it will fail to publish")
                continue
            dst = t / p["id"]
            dst.mkdir()
            for name in p["media"] + ([p["cover"]] if p.get("cover") else []):
                shutil.copy2(src / name, dst / name)
                n += 1
        run(["git", "init", "-q", "-b", "media"], t)
        run(["git", "add", "-A"], t)
        run(["git", "-c", "user.name=ig-autopilot", "-c", "user.email=ig-autopilot@users.noreply.github.com",
             "commit", "-q", "-m", f"media {dt.datetime.now(UTC).isoformat(timespec='minutes')}"], t)
        run(["git", "push", "-q", "--force", origin, "media:media"], t)
    print(f"pushed queue ({len(pending)} pending posts) and {n} media files")


def cmd_report(args):
    rows = [p for p in posts() if p["status"] == "published"]
    fol = STATS / "followers.csv"
    lines = ["# Instagram numbers", "",
             f"Updated {dt.date.today()} from `projects/tools/ig-autopilot` (`python3 ig.py report`). "
             "The Sunday `/ig-week` reads this before picking ideas.", ""]
    if fol.exists():
        pts = [l.split(",") for l in fol.read_text().splitlines()[1:] if l]
        if pts:
            lines += ["## Followers", "", "| Date | Followers |", "|---|---|"] + [f"| {d} | {n} |" for d, n, *_ in pts[-10:]] + [""]
    def score(p):
        s = p.get("stats") or {}
        return (s.get("shares") or 0) * 3 + (s.get("saved") or 0) * 2 + (s.get("follows") or 0) * 5
    lines += ["## Posts, best first (score = 3×shares + 2×saves + 5×follows)", "",
              "| Date | Type | Level | Series | Title | Views | Reach | Saves | Shares | Follows | Score | Link |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(rows, key=score, reverse=True):
        s = p.get("stats") or {}
        link = (p.get("published") or {}).get("permalink") or ""
        lines.append(f"| {p['date']} | {p['type']} | {p['level']} | {p['series']} | {p['title']} | {s.get('views', '')} | "
                     f"{s.get('reach', '')} | {s.get('saved', '')} | {s.get('shares', '')} | {s.get('follows', '')} | {score(p)} | {link} |")
    series: dict[str, list[int]] = {}
    for p in rows:
        series.setdefault(p["series"], []).append(score(p))
    lines += ["", "## Series average score", "", "| Series | Posts | Avg score |", "|---|---|---|"]
    for k, v in sorted(series.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        lines.append(f"| {k} | {len(v)} | {sum(v) / len(v):.1f} |")
    out = (HERE / CFG["local"]["stats_report"]).resolve()
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)


# ---------------------------------------------------------------- GitHub Actions commands
def cmd_publish_due(args):
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=UTC) if args.now else dt.datetime.now(UTC)
    ig = None  # made on first use, so an empty queue never needs a token
    failed, done_types = [], set()
    for p in posts():
        if p["status"] != "queued" or not p["approved"] or not p.get("checks", {}).get("passed"):
            continue
        if due_at(p) > now or p["type"] in done_types:
            continue
        done_types.add(p["type"])  # at most one post per type per run: a backlog drains slowly, never floods
        urls = [public_url(p, m) for m in p["media"]]
        if args.dry_run:
            print(f"[dry-run] would publish {p['id']}: {urls}")
            continue
        try:
            ig = ig or api()
            if p["type"] == "carousel":
                res = ig.publish_carousel(urls, p["caption"])
            else:
                cover = public_url(p, p["cover"]) if p.get("cover") else None
                res = ig.publish_reel(urls[0], p["caption"], cover)
            p.update(status="published", error=None,
                     published={**res, "at": dt.datetime.now(UTC).isoformat(timespec="seconds")})
            print(f"published {p['id']} → {res.get('permalink')}")
        except IgError as e:
            p["attempts"] = p.get("attempts", 0) + 1
            p["error"] = str(e)[:500]
            if p["attempts"] >= CFG["max_attempts"]:
                p["status"] = "failed"
            failed.append(p["id"])
            print(f"FAILED {p['id']} (attempt {p['attempts']}): {e}", file=sys.stderr)
        save(p)
    if failed:
        sys.exit(1)  # GitHub emails the failure


def no_token_yet() -> bool:
    load_env()
    if os.environ.get("IG_TOKEN"):
        return False
    print("No IG_TOKEN yet: nothing to do (set the secret once the Meta app token exists).")
    return True


def cmd_refresh_token(args):
    if no_token_yet():
        return
    res = api().refresh_token()
    tok = res.get("access_token")
    if not tok:
        sys.exit(f"refresh returned no token: {res}")
    print(f"::add-mask::{tok}")
    if os.environ.get("GITHUB_ACTIONS"):
        r = subprocess.run(["gh", "secret", "set", "IG_TOKEN", "--repo", CFG["repo"]], input=tok, text=True,
                           capture_output=True)
        if r.returncode:
            sys.exit(f"could not save the new token (is the GH_PAT secret set?): {r.stderr.strip()}")
    else:
        set_env_value("IG_TOKEN", tok)
    days = int(res.get("expires_in", 0)) // 86400
    print(f"token refreshed, valid {days} more days")


def cmd_stats(args):
    if no_token_yet():
        return
    ig = api()
    STATS.mkdir(exist_ok=True)
    me = ig.whoami()
    fol = STATS / "followers.csv"
    if not fol.exists():
        fol.write_text("date,followers,media_count\n")
    with fol.open("a") as f:
        f.write(f"{dt.date.today()},{me.get('followers_count', '')},{me.get('media_count', '')}\n")
    since = dt.date.today() - dt.timedelta(days=CFG["stats_days"])
    n = 0
    for p in posts():
        mid = (p.get("published") or {}).get("media_id")
        if p["status"] != "published" or not mid or dt.date.fromisoformat(p["date"]) < since:
            continue
        p["stats"] = {**ig.media_insights(mid, p["type"]), "at": str(dt.date.today())}
        save(p)
        n += 1
    print(f"followers {me.get('followers_count')} · stats for {n} posts")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")
    a = sub.add_parser("add")
    a.add_argument("--source", required=True)
    a.add_argument("--type", choices=["reel", "carousel"], required=True)
    a.add_argument("--date", required=True)
    a.add_argument("--time")
    a.add_argument("--level", required=True)
    a.add_argument("--series", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--slug")
    a.add_argument("--qa")
    a.add_argument("--natasha")
    a.add_argument("--natasha-notes")
    r = sub.add_parser("review")
    r.add_argument("id")
    r.add_argument("--natasha", required=True, choices=["PASS", "FIX", "pass", "fix"])
    r.add_argument("--notes")
    for name in ("check", "approve", "status"):
        s = sub.add_parser(name)
        s.add_argument("id", nargs="?")
        s.add_argument("--week")
    s = sub.add_parser("sheet")
    s.add_argument("--week", required=True)
    sub.add_parser("push")
    sub.add_parser("report")
    s = sub.add_parser("publish-due")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--now")
    sub.add_parser("refresh-token")
    sub.add_parser("stats")
    args = ap.parse_args(argv)
    {"whoami": cmd_whoami, "add": cmd_add, "review": cmd_review, "check": cmd_check, "approve": cmd_approve,
     "status": cmd_status, "sheet": cmd_sheet, "push": cmd_push, "report": cmd_report,
     "publish-due": cmd_publish_due, "refresh-token": cmd_refresh_token, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
