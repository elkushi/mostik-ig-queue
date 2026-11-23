"""Run: python3 -m unittest discover -s tests   (from projects/tools/ig-autopilot)"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import ig  # noqa: E402
from igapi import IgError, Instagram  # noqa: E402


class FakeIG:
    """Records calls and answers like graph.instagram.com."""

    def __init__(self, statuses=None, fail_metric=None):
        self.calls, self.n = [], 0
        self.statuses = statuses or {}
        self.fail_metric = fail_metric

    def __call__(self, method, url, data):
        self.calls.append((method, url, dict(data or {})))
        path = url.split("graph.instagram.com/")[1].split("?")[0]
        if method == "POST" and path.endswith("/media"):
            self.n += 1
            return {"id": f"c{self.n}"}
        if method == "POST" and path.endswith("/media_publish"):
            return {"id": "m1"}
        if "fields=status_code" in url:
            cid = path
            seq = self.statuses.get(cid, ["FINISHED"])
            return {"status_code": seq.pop(0) if len(seq) > 1 else seq[0]}
        if "fields=status" in url:
            return {"status": "bad video"}
        if "fields=permalink" in url:
            return {"permalink": "https://www.instagram.com/p/XYZ/"}
        if path.endswith("/insights"):
            if self.fail_metric and self.fail_metric in url:
                raise IgError("invalid metric")
            metric = url.split("metric=")[1].split("&")[0].split("%2C")
            return {"data": [{"name": m, "values": [{"value": 7}]} for m in metric]}
        if path.startswith("refresh_access_token"):
            return {"access_token": "NEW", "expires_in": 5184000}
        return {}


def client(fake):
    return Instagram("TOKEN", "17841", transport=fake, sleep=lambda s: None)


class ApiTests(unittest.TestCase):
    def test_carousel_flow(self):
        fake = FakeIG()
        res = client(fake).publish_carousel(["https://x/a.jpg", "https://x/b.jpg"], "cap")
        self.assertEqual(res, {"media_id": "m1", "permalink": "https://www.instagram.com/p/XYZ/"})
        posts = [c for c in fake.calls if c[0] == "POST"]
        self.assertEqual(posts[0][2]["is_carousel_item"], "true")
        self.assertEqual(posts[2][2]["media_type"], "CAROUSEL")
        self.assertEqual(posts[2][2]["children"], "c1,c2")
        self.assertEqual(posts[3][2]["creation_id"], "c3")
        self.assertTrue(all(c[2].get("access_token") == "TOKEN" for c in posts))

    def test_reel_waits_until_finished(self):
        fake = FakeIG(statuses={"c1": ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]})
        res = client(fake).publish_reel("https://x/v.mp4", "cap", "https://x/c.jpg")
        self.assertEqual(res["media_id"], "m1")
        post = [c for c in fake.calls if c[0] == "POST"][0][2]
        self.assertEqual((post["media_type"], post["share_to_feed"], post["cover_url"]), ("REELS", "true", "https://x/c.jpg"))
        self.assertEqual(sum("status_code" in c[1] for c in fake.calls), 3)

    def test_container_error_raises(self):
        fake = FakeIG(statuses={"c1": ["ERROR"]})
        with self.assertRaises(IgError) as e:
            client(fake).publish_reel("https://x/v.mp4", "cap")
        self.assertIn("bad video", str(e.exception))
        self.assertFalse(any("media_publish" in c[1] for c in fake.calls))

    def test_carousel_size_limits(self):
        with self.assertRaises(IgError):
            client(FakeIG()).publish_carousel(["one"], "cap")

    def test_insights_fall_back_per_metric(self):
        out = client(FakeIG(fail_metric="ig_reels_avg_watch_time")).media_insights("m1", "reel")
        self.assertEqual(out["views"], 7)
        self.assertEqual(out["shares"], 7)

    def test_refresh(self):
        self.assertEqual(client(FakeIG()).refresh_token()["access_token"], "NEW")


class CheckTests(unittest.TestCase):
    def test_stress(self):
        self.assertEqual(checks.words_missing_stress("Приве́т, как дела"), ["дела"])
        self.assertEqual(checks.words_missing_stress("ещё дом"), [])
        self.assertEqual(checks.words_missing_stress("кто-то"), [])  # one vowel each side

    def test_caption(self):
        good = "Two hellos: приве́т and здра́вствуйте. Which one do you use? #learnrussian #russianlanguage #русский"
        self.assertEqual(checks.caption_problems(good)[0], [])
        bad = "Learn with Mostik! привет (pree-vyet) #a"
        probs = " ".join(checks.caption_problems(bad)[0])
        for word in ("hashtags", "mostik", "stress", "Latin"):
            self.assertIn(word, probs)

    def test_review_required(self):
        with tempfile.TemporaryDirectory() as t:
            post = {"type": "carousel", "level": "A1", "media": [], "caption": "Which? #a #b #c", "review": {}}
            res = checks.check_post(post, Path(t))
            self.assertFalse(res["passed"])
            self.assertTrue(any("Natasha" in p for p in res["problems"]))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ig.QUEUE = Path(self.tmp.name) / "queue"
        ig.STAGING = Path(self.tmp.name) / "staging"

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, pid, typ, date, time, approved=True, passed=True, status="queued"):
        ig.save({"id": pid, "type": typ, "date": date, "time_utc": time, "level": "A1", "series": "s", "title": pid,
                 "caption": "c", "media": ["a.jpg", "b.jpg"] if typ == "carousel" else ["video.mp4"], "cover": None,
                 "approved": approved, "status": status, "checks": {"passed": passed}, "attempts": 0})

    def test_only_due_approved_passed_one_per_type(self):
        self.mk("p1", "carousel", "2026-11-30", "12:00")
        self.mk("p2", "carousel", "2026-11-30", "12:00")              # second carousel waits for the next run
        self.mk("p3", "reel", "2026-11-30", "17:00")                  # not due yet
        self.mk("p4", "reel", "2026-11-29", "17:00", approved=False)  # not approved
        self.mk("p5", "reel", "2026-11-29", "17:00", passed=False)    # held by checks
        published = []

        class Fake:
            def publish_carousel(self, urls, cap):
                published.append(urls)
                return {"media_id": "m", "permalink": "L"}

        orig = ig.api
        ig.api = lambda: Fake()
        try:
            ig.main(["publish-due", "--now", "2026-11-30T12:05"])
        finally:
            ig.api = orig
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0][0].endswith("/p1/a.jpg"))
        self.assertEqual(ig.get("p1")["status"], "published")
        self.assertEqual(ig.get("p2")["status"], "queued")

    def test_failure_counts_attempts_then_fails(self):
        self.mk("r1", "reel", "2026-11-30", "17:00")

        class Boom:
            def publish_reel(self, *a):
                raise IgError("nope")

        orig = ig.api
        ig.api = lambda: Boom()
        try:
            for _ in range(3):
                with self.assertRaises(SystemExit):
                    ig.main(["publish-due", "--now", "2026-11-30T17:05"])
        finally:
            ig.api = orig
        p = ig.get("r1")
        self.assertEqual((p["attempts"], p["status"]), (3, "failed"))

    def test_autopilot_date_approves_passing_posts(self):
        post = {"id": "x", "type": "carousel", "date": "2026-12-20", "status": "queued", "approved": False}
        orig = ig.check_post
        ig.check_post = lambda p, f: {"passed": True, "problems": [], "warnings": []}
        try:
            ig.finish_check(post)
            self.assertTrue(post["approved"])
            post.update(date="2026-12-01", approved=False)
            ig.finish_check(post)
            self.assertFalse(post["approved"])  # supervised weeks: needs `approve`
        finally:
            ig.check_post = orig

    def test_caption_from_section(self):
        f = Path(self.tmp.name) / "c.md"
        f.write_text("# Title\n\n## Instagram\nHello?\n#a\n\n## Facebook\nother\n")
        self.assertEqual(ig.caption_from(f), "Hello?\n#a")
        f.write_text("# Title\n\nJust this?\n")
        self.assertEqual(ig.caption_from(f), "Just this?")


if __name__ == "__main__":
    unittest.main()
