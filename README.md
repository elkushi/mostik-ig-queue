# ig-autopilot

Queues, checks and publishes the daily Instagram posts for @russianinuse (→ Mostik at 10k).
Plan: `projects/mostik/social-media/INSTAGRAM-10K-PLAN.md`. Posts are made by the `ig-week` skill
(`projects/mostik/.claude/skills/ig-week/`).

**This folder is the public queue repo** (`elkushi/mostik-ig-queue`): code + `queue/*/post.json` on `main`,
pending media on the `media` branch served by GitHub Pages (Instagram only fetches media from a public URL).
Nothing secret is ever committed: the token lives in `.env` (git-ignored) and in GitHub secrets.

## How it runs
| When | Where | What |
|---|---|---|
| Sunday | laptop, `ig-week` skill | make 14 posts → `ig.py add` each (runs the check gate) → `ig.py push` |
| 12:05 / 17:05 / 20:35 UTC daily | GitHub Actions `publish.yml` | `ig.py publish-due`: publishes what's due, approved and passed; at most one per type per run; 3 failed attempts → `failed`; any failure → GitHub emails |
| Sunday 06:00 UTC | GitHub Actions `maintenance.yml` | refresh the 60-day token (saved back with `GH_PAT`), pull numbers into `queue/*/post.json` + `stats/followers.csv` |
| any time | laptop | `ig.py report` → `projects/mostik/social-media/instagram-stats.md` |

Approval: posts dated before `autopilot_from` (2026-12-14, `config.json`) need `ig.py approve`; later posts that pass the gate are approved automatically.

## The check gate (`checks.py`)
Holds a post back if: level not A1–C1 · caption over 2,200 chars, hashtags not 3–8, Phase 1 words (Mostik, NoTranslate, our app, prices…), Russian without stress marks, Latin transliteration · carousel not 2–10 JPEG slides at 1080×1350, or `render-report.json` not clean · Reel not 1080×1920 h264 + aac, 5–90 s, under 95 MB, or `qa.json` from `review.mjs` is a draft / has fails / Whisper couldn't confirm the voice · Natasha review not PASS.

## Commands
```
python3 ig.py whoami                 # test the token, saves IG_USER_ID in .env
python3 ig.py add --source DIR --type reel|carousel --date 2026-11-30 --level A1 --series cheat-sheet --title "…" --natasha PASS
python3 ig.py check --week 2026-11-30
python3 ig.py sheet --week 2026-11-30   # review page (supervised weeks)
python3 ig.py approve --week 2026-11-30
python3 ig.py status
python3 ig.py push
python3 ig.py report
python3 ig.py publish-due --dry-run --now 2026-11-30T17:10
python3 -m unittest discover -s tests   # 13 tests, no network
```
Stdlib only (Python 3.12+), plus `ffmpeg` / `ffprobe` on the laptop.

## One-time setup
1. **Token (Moustafa, VPN on):** developers.facebook.com → My Apps → Create app → use case **Other** → type **Business** → add product **Instagram** → **API setup with Instagram login** → *Generate access tokens* → add @russianinuse (account must be public) and log in to Instagram → copy the token into `.env` as `IG_TOKEN=…`. Then `python3 ig.py whoami`.
2. **Queue repo (Claude, after OK):** `gh repo create elkushi/mostik-ig-queue --public --source . --push`, then Pages from branch `media`, then the secrets `IG_TOKEN`, `IG_USER_ID`.
3. **GH_PAT (Moustafa, 2 min):** GitHub → Settings → Developer settings → Fine-grained tokens → only this repo → permission *Secrets: read and write* → save as repo secret `GH_PAT`. Without it the weekly refresh can't save the new token and posting stops after 60 days.
4. Dry run: Actions → publish → Run workflow → dry run ✓.

## Self-improvement
Log each publishing failure, API change or gate false-alarm here with the date and the fix; turn repeats into code or a rule.
- 2026-11-23: built; 13 tests pass; end-to-end add → check → sheet → dry-run publish verified on a real carousel and a real 9:16 Reel. Live publishing not yet tested (no token).
