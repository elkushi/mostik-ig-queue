"""The check gate: a post that fails any check is held back and never published.

Each check returns problems (block) or warnings (shown, don't block).
Why each rule exists is written next to it.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

LEVELS = {"A1", "A2", "B1", "B2", "C1"}
STRESS = "́"
VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
# Phase 1 rule (until 10k + rename): no app, course, prices or dates in posts.
BANNED = [r"mostik", r"мостик", r"no\s?translate", r"our app", r"our course", r"app store", r"google play",
          r"download (the|our)", r"\b\d+\s?(₽|rub|руб)", r"\$\s?\d"]
# Crude transliteration catcher: Latin syllables in quotes or parentheses right after Cyrillic,
# e.g. «привет (pree-VYET)». Founder rule: never explain Russian through Latin letters.
TRANSLIT = re.compile(r"[А-Яа-яЁё́]+\s*[\(\[«\"']\s*[a-z]+(-[a-zA-Z]+)+\s*[\)\]»\"']")
MAX_VIDEO_MB = 95      # GitHub refuses files over 100 MB
MAX_IMAGE_MB = 8       # Instagram's image limit


def words_missing_stress(text: str) -> list[str]:
    """Russian words of 2+ syllables with no stress mark (ё carries its own stress)."""
    out = []
    for w in re.findall(r"[А-Яа-яЁё́-]+", text):
        for part in w.split("-"):
            vowels = sum(ch in VOWELS for ch in part)
            if vowels >= 2 and STRESS not in part and "ё" not in part.lower():
                out.append(part)
    return out


def caption_problems(caption: str) -> tuple[list[str], list[str]]:
    probs, warns = [], []
    if not caption.strip():
        probs.append("caption is empty")
    if len(caption) > 2200:
        probs.append(f"caption is {len(caption)} characters (Instagram max 2200)")
    tags = re.findall(r"(?<!\w)#\w+", caption)
    if not 3 <= len(tags) <= 8:
        probs.append(f"{len(tags)} hashtags (plan: 3–6, never more than 8)")
    low = caption.lower()
    for pat in BANNED:
        if re.search(pat, low):
            probs.append(f"caption mentions something Phase 1 forbids: /{pat}/")
    missing = words_missing_stress(re.sub(r"(?<!\w)#\w+", " ", caption))  # hashtags can't carry stress marks
    if missing:
        probs.append("Russian without stress mark in caption: " + ", ".join(sorted(set(missing))[:8]))
    if TRANSLIT.search(caption):
        probs.append("caption seems to spell Russian in Latin letters (no transliteration)")
    if "?" not in caption:
        warns.append("no question in the caption (the plan ends each caption with one)")
    return probs, warns


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        return {}
    return json.loads(r.stdout or "{}")


def image_problems(files: list[Path]) -> list[str]:
    probs = []
    if not 2 <= len(files) <= 10:
        probs.append(f"carousel has {len(files)} slides (2–10)")
    for f in files:
        if f.suffix.lower() not in (".jpg", ".jpeg"):
            probs.append(f"{f.name}: Instagram only takes JPEG")
            continue
        if f.stat().st_size > MAX_IMAGE_MB * 1024 * 1024:
            probs.append(f"{f.name}: over {MAX_IMAGE_MB} MB")
        st = (probe(f).get("streams") or [{}])[0]
        w, h = st.get("width"), st.get("height")
        if (w, h) != (1080, 1350):
            probs.append(f"{f.name}: {w}×{h}, expected 1080×1350")
    return probs


def video_problems(f: Path) -> list[str]:
    if not f.exists():
        return [f"{f.name} missing"]
    probs = []
    if f.stat().st_size > MAX_VIDEO_MB * 1024 * 1024:
        probs.append(f"{f.name}: over {MAX_VIDEO_MB} MB")
    info = probe(f)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        return probs + [f"{f.name}: no video stream"]
    if (v.get("width"), v.get("height")) != (1080, 1920):
        probs.append(f"{f.name}: {v.get('width')}×{v.get('height')}, expected 1080×1920 (9:16)")
    if v.get("codec_name") != "h264":
        probs.append(f"{f.name}: video codec {v.get('codec_name')}, Instagram wants h264")
    if not a:
        probs.append(f"{f.name}: no sound (the voice is half the lesson)")
    elif a.get("codec_name") != "aac":
        probs.append(f"{f.name}: audio codec {a.get('codec_name')}, Instagram wants aac")
    dur = float(info.get("format", {}).get("duration", 0))
    if not 5 <= dur <= 90:
        probs.append(f"{f.name}: {dur:.1f} s long (plan 20–45 s, allowed 5–90 s)")
    return probs


def render_report_problems(path: Path | None) -> list[str]:
    """mostik-infographic render.py prints a JSON report; layout and content must be clean."""
    if not path or not path.exists():
        return ["no render-report.json (save render.py's output next to the slides)"]
    rep = json.loads(path.read_text(encoding="utf-8"))
    probs = []
    if rep.get("layout_check") in (None, "not run") or rep.get("layout_check"):
        probs.append(f"layout_check not clean: {json.dumps(rep.get('layout_check'), ensure_ascii=False)[:200]}")
    if rep.get("content_warnings"):
        probs.append(f"content_warnings: {json.dumps(rep['content_warnings'], ensure_ascii=False)[:200]}")
    return probs


def qa_problems(path: Path | None) -> list[str]:
    """animation-creator review.mjs writes review/qa.json; it must be a final render with no fails."""
    if not path or not path.exists():
        return ["no qa.json from animation-creator review.mjs"]
    qa = json.loads(path.read_text(encoding="utf-8"))
    probs = []
    if qa.get("draft"):
        probs.append("qa.json is from a draft render, not the final")
    if qa.get("fails"):
        probs.append("review.mjs fails: " + "; ".join(qa["fails"])[:200])
    # Russian parts read by the native voice must be heard right (review.mjs "ruOk"): a wrong Russian
    # sound is a wrong lesson. English mismatches are usually Whisper, so they only warn in review.mjs.
    bad = [v for v in (qa.get("voice") or []) if v.get("ruOk") is False]
    if bad:
        probs.append("Russian voice not confirmed by Whisper in scene(s) " + ", ".join(str(v.get("scene")) for v in bad))
    return probs


def check_post(post: dict, folder: Path) -> dict:
    """Full gate for one staged post. Returns {passed, problems, warnings}."""
    probs, warns = [], []
    if post.get("level") not in LEVELS:
        probs.append(f"level {post.get('level')!r} not one of {sorted(LEVELS)}")
    p, w = caption_problems(post.get("caption", ""))
    probs += p
    warns += w
    media = [folder / m for m in post.get("media", [])]
    missing = [m.name for m in media if not m.exists()]
    if missing:
        probs.append("missing media: " + ", ".join(missing))
    elif post["type"] == "carousel":
        probs += image_problems(media)
        probs += render_report_problems(folder / "render-report.json")
    elif post["type"] == "reel":
        probs += video_problems(media[0]) if media else ["reel has no video"]
        probs += qa_problems(folder / "qa.json")
    else:
        probs.append(f"unknown type {post['type']}")
    review = (post.get("review") or {}).get("natasha", "")
    if review.upper() != "PASS":
        probs.append(f"Natasha review is {review or 'missing'} (needs PASS)")
    return {"passed": not probs, "problems": probs, "warnings": warns}
