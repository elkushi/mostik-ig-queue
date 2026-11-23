import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze  # noqa: E402


def media(i, series, level, kind, reach, saved, shares, follows=0, comments=None, queue=True, hour=17):
    return {"id": str(i), "caption": f"Post {i}\nmore", "kind": kind, "timestamp": f"2026-12-0{1 + i % 7}T{hour:02d}:05:00+0000",
            "permalink": f"https://instagram.com/p/{i}", "queue_id": f"q{i}" if queue else None,
            "series": series if queue else None, "level": level if queue else None, "comments": comments or [],
            "insights": {"reach": reach, "views": reach * 2, "saved": saved, "shares": shares, "follows": follows, "likes": 5}}


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        rows = [media(1, "quiz", "B1", "post", 1000, 80, 40), media(2, "quiz", "B1", "post", 1200, 90, 50),
                media(3, "one-word-five-levels", "A1", "reel", 5000, 400, 400, 60), media(4, "one-word-five-levels", "A1", "reel", 4000, 320, 330, 50),
                media(5, "nuance", "C1", "post", 800, 4, 1), media(6, "nuance", "C1", "post", 700, 3, 0),
                media(7, None, None, "reel", 90000, 2000, 5000, queue=False,
                      comments=[{"text": "How do you say thank you?", "like_count": 9}, {"text": "nice", "like_count": 1}])]
        for date, n in [("2026-12-06", 1500), ("2026-12-13", 2300)]:
            (d / f"{date}.json").write_text(json.dumps({"date": date, "account": {"followers_count": n}, "media": rows,
                                                        "demographics": {"country": {"US": 50, "IN": 80}},
                                                        "online_followers": {"17": 900, "12": 300, "3": 50}}))
        self.res = analyze.analyze(d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scores_and_groups(self):
        self.assertEqual(self.res["by_series"]["quiz"]["posts"], 2)
        self.assertGreater(self.res["by_series"]["one-word-five-levels"]["score"], self.res["by_series"]["quiz"]["score"])
        self.assertEqual(self.res["top_older_posts"][0]["title"], "Post 7")

    def test_pace(self):
        p = self.res["pace"]
        self.assertEqual(p["followers"], 2300)
        self.assertEqual(p["actual_per_week"], 800)
        self.assertTrue(p["needed_per_week"] > 0)

    def test_grid_change_swaps_weak_for_best_same_type(self):
        ch = self.res["grid_changes"]
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["from"], "nuance")
        self.assertEqual(ch[0]["type"], "carousel")
        self.assertEqual(ch[0]["to"], "quiz")  # the best *carousel* series (reels can't replace a carousel slot)

    def test_questions_and_hours(self):
        self.assertEqual(self.res["questions"][0]["text"], "How do you say thank you?")
        self.assertEqual(self.res["best_online_hours"][0], "17:00 UTC")
        self.assertIn("Recommendations", analyze.to_md(self.res))
