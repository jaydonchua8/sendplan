import unittest
from sendplan import Book, Step, canonical

CAPS = {"dm": 100, "collab": 100, "email": 100}


class Canonical(unittest.TestCase):
    def test_variants_collapse(self):
        for v in ["@Glow.Kate", "glow.kate", "  GLOW.KATE ", "tiktok.com/@glow.kate",
                  "https://www.tiktok.com/@Glow.Kate?lang=en",
                  "https://m.tiktok.com/@glow.kate/video/123", "glow.kate.",
                  "ｇlow.kate", "glow.​kate"]:
            self.assertEqual(canonical(v), "glow.kate", v)

    def test_distinct_stay_distinct(self):
        self.assertNotEqual(canonical("glow_kate"), canonical("glow.kate"))
        self.assertNotEqual(canonical("glowkate"), canonical("glowkate1"))

    def test_rejects_junk(self):
        for v in ["", "@", "N/A", "kate smith!", "x", "a" * 25]:
            self.assertEqual(canonical(v), "", v)


class Planning(unittest.TestCase):
    def book(self):
        b = Book()
        b.add("@a_one", 0.9, "a@x.test")
        b.add("b_two", 0.5)
        b.add("c_three", 0.7, "c@x.test")
        return b

    def test_duplicate_rows_merge(self):
        b = self.book()
        b.add("https://www.tiktok.com/@A_One", 0.2)
        self.assertEqual(len(b.creators), 3)
        self.assertEqual(b.creators["a_one"]["score"], 0.9)

    def test_junk_is_rejected_not_planned(self):
        b = self.book()
        self.assertIsNone(b.add("n/a"))
        self.assertEqual(b.rejected, ["n/a"])

    def test_new_contacts_by_score(self):
        p = self.book().plan(0, CAPS, 2)
        self.assertEqual([s.key for s in p], ["a_one", "c_three"])
        self.assertTrue(all(s.channel == "dm" and s.kind == "new" for s in p))

    def test_followup_waits_then_goes_first(self):
        b = self.book()
        b.apply(b.plan(0, CAPS, 1), 0)                 # a_one gets the DM
        self.assertEqual([s.key for s in b.plan(2, CAPS, 1)], ["c_three"])
        p = b.plan(3, CAPS, 1)
        self.assertEqual((p[0].key, p[0].channel, p[0].kind), ("a_one", "collab", "followup"))

    def test_email_step_skipped_without_address(self):
        b = self.book()
        for day in (0, 3):
            b.apply(b.plan(day, CAPS, 10), day)
        p = b.plan(7, CAPS, 10)
        self.assertEqual(sorted(s.key for s in p), ["a_one", "c_three"])
        self.assertTrue(all(s.channel == "email" for s in p))
        b.apply(p, 7)
        self.assertEqual(b.plan(30, CAPS, 10), [])     # everyone is finished

    def test_reply_under_other_spelling_stops(self):
        b = self.book()
        b.apply(b.plan(0, CAPS, 10), 0)
        b.stop("TikTok.com/@A_ONE")
        self.assertNotIn("a_one", [s.key for s in b.plan(3, CAPS, 10)])

    def test_stop_before_import_is_remembered(self):
        b = Book()
        b.stop("@later_on", "opted_out")
        b.add("later_on", 0.99)
        self.assertEqual(b.plan(0, CAPS, 10), [])

    def test_caps_hold(self):
        b = Book()
        for i in range(50):
            b.add("creator_%02d" % i, i / 50)
        self.assertEqual(len(b.plan(0, {"dm": 7}, 20)), 7)
        self.assertEqual(len(b.plan(0, {"dm": 40}, 20)), 20)
        self.assertEqual(b.plan(0, {"collab": 40}, 20), [])

    def test_blocked_channel_does_not_block_others(self):
        b = Book()
        for i in range(6):
            b.add("creator_%02d" % i, 0.5)
        b.apply(b.plan(0, CAPS, 4), 0)
        p = b.plan(3, {"dm": 5, "collab": 1}, 10)
        self.assertEqual([s.kind for s in p], ["followup", "new", "new"])

    def test_new_share_reserves_room(self):
        b = Book()
        for i in range(20):
            b.add("creator_%02d" % i, 0.5)
        b.apply(b.plan(0, CAPS, 10), 0)
        kinds = lambda p: sorted(s.kind for s in p)
        self.assertEqual(kinds(b.plan(3, CAPS, 10)), ["followup"] * 10)
        self.assertEqual(kinds(b.plan(3, CAPS, 10, new_share=0.3)),
                         ["followup"] * 7 + ["new"] * 3)

    def test_apply_twice_is_a_noop(self):
        b = self.book()
        p = b.plan(0, CAPS, 10)
        self.assertEqual(b.apply(p, 0), 3)
        self.assertEqual(b.apply(p, 0), 0)
        self.assertEqual(b.plan(0, CAPS, 10), [])      # one message per creator per day

    def test_plan_is_deterministic_and_pure(self):
        b = self.book()
        before = b.dumps()
        self.assertEqual(b.plan(0, CAPS, 10), b.plan(0, CAPS, 10))
        self.assertEqual(b.dumps(), before)

    def test_round_trip(self):
        b = self.book()
        b.apply(b.plan(0, CAPS, 2), 0)
        b.stop("b_two")
        c = Book.loads(b.dumps())
        self.assertEqual(c.plan(3, CAPS, 10), b.plan(3, CAPS, 10))

    def test_custom_sequence(self):
        b = Book(sequence=[Step("email", 0), Step("dm", 1)])
        b.add("has_mail", 0.1, "h@x.test")
        b.add("no_mail", 0.9)
        self.assertEqual({s.key: s.channel for s in b.plan(0, CAPS, 10)},
                         {"has_mail": "email", "no_mail": "dm"})


if __name__ == "__main__":
    unittest.main()
