"""Tests for the fit scorer. No network -- the LLM call is monkeypatched.

Run:  ./venv/bin/python3 -m unittest test_scorer
"""

import unittest

import scorer
from agent_kit import LLMUnavailable


def fake_fit(score=8, reason="Strong data overlap.", flags=None,
             seniority="match"):
    return {
        "fit_score": score,
        "reason": reason,
        "red_flags": flags if flags is not None else [],
        "seniority_match": seniority,
    }


LONG_DESC = "Supply chain analyst role. " * 20  # comfortably over the 80-char floor


class CleanTextTest(unittest.TestCase):
    def test_strips_tags_and_unescapes(self):
        out = scorer.clean_text("<p>Hello &amp; welcome</p><li>SQL</li>")
        self.assertNotIn("<", out)
        self.assertIn("Hello & welcome", out)
        self.assertIn("SQL", out)

    def test_collapses_whitespace_and_blank_lines(self):
        out = scorer.clean_text("<p>a</p>\n\n\n\n<p>b</p>")
        self.assertNotIn("\n\n\n", out)

    def test_plain_text_passes_through(self):
        # Lever descriptions arrive as plain text, not HTML.
        self.assertEqual(scorer.clean_text("Just plain text."), "Just plain text.")

    def test_none_is_empty(self):
        self.assertEqual(scorer.clean_text(None), "")


class ScoreJobTest(unittest.TestCase):
    def setUp(self):
        self._real = scorer.ask_json
        self.calls = []

    def tearDown(self):
        scorer.ask_json = self._real

    def _stub(self, result):
        def fn(**kwargs):
            self.calls.append(kwargs)
            return result
        scorer.ask_json = fn

    def test_returns_schema_dict(self):
        self._stub(fake_fit())
        fit = scorer.score_job(
            {"title": "Analyst", "description": LONG_DESC}, "resume")
        self.assertEqual(fit["fit_score"], 8)
        self.assertEqual(len(self.calls), 1)

    def test_skips_short_description_without_calling_api(self):
        self._stub(fake_fit())
        fit = scorer.score_job({"title": "Analyst", "description": "Too short"},
                               "resume")
        self.assertIsNone(fit)
        self.assertEqual(self.calls, [], "should not pay for an empty posting")

    def test_missing_description_returns_none(self):
        self._stub(fake_fit())
        self.assertIsNone(
            scorer.score_job({"title": "Analyst", "description": None}, "resume"))

    def test_truncates_very_long_descriptions(self):
        self._stub(fake_fit())
        scorer.score_job(
            {"title": "Analyst", "description": "x " * 20000}, "resume")
        sent = self.calls[0]["user"]
        self.assertIn("[...truncated]", sent)
        self.assertLess(len(sent), scorer.MAX_DESCRIPTION_CHARS + 2000)

    def test_clamps_out_of_range_scores(self):
        self._stub(fake_fit(score=47))
        fit = scorer.score_job(
            {"title": "Analyst", "description": LONG_DESC}, "resume")
        self.assertEqual(fit["fit_score"], 10)

    def test_fails_open_when_api_unavailable(self):
        def boom(**kwargs):
            raise LLMUnavailable("no key")
        scorer.ask_json = boom
        fit = scorer.score_job(
            {"title": "Analyst", "description": LONG_DESC}, "resume")
        self.assertIsNone(fit, "a dead API must not break the alert pipeline")

    def test_fails_open_on_unexpected_error(self):
        def boom(**kwargs):
            raise ValueError("something weird")
        scorer.ask_json = boom
        self.assertIsNone(scorer.score_job(
            {"title": "Analyst", "description": LONG_DESC}, "resume"))


class EmbedColorTest(unittest.TestCase):
    def test_high_scores_are_green(self):
        for s in (8, 9, 10):
            self.assertEqual(scorer.embed_color(fake_fit(s)), scorer.GREEN)

    def test_middling_scores_are_amber(self):
        for s in (5, 6, 7):
            self.assertEqual(scorer.embed_color(fake_fit(s)), scorer.AMBER)

    def test_low_scores_are_red(self):
        for s in (1, 3, 4):
            self.assertEqual(scorer.embed_color(fake_fit(s)), scorer.RED)

    def test_unscored_keeps_the_original_blurple(self):
        self.assertEqual(scorer.embed_color(None), scorer.DEFAULT_COLOR)


class FieldRenderingTest(unittest.TestCase):
    def test_fit_field_includes_score_and_reason(self):
        value = scorer.fit_field_value(fake_fit(7, "Good SQL overlap."))
        self.assertIn("7/10", value)
        self.assertIn("Good SQL overlap.", value)

    def test_seniority_note_appears_only_when_mismatched(self):
        self.assertIn("wants more experience",
                      scorer.fit_field_value(fake_fit(seniority="under")))
        self.assertIn("below your level",
                      scorer.fit_field_value(fake_fit(seniority="over")))
        self.assertNotIn("(", scorer.fit_field_value(
            fake_fit(reason="Clean match.", seniority="match")))

    def test_fit_field_respects_discord_1024_limit(self):
        value = scorer.fit_field_value(fake_fit(reason="x" * 5000))
        self.assertLessEqual(len(value), 1024)

    def test_no_fit_means_no_field(self):
        self.assertIsNone(scorer.fit_field_value(None))
        self.assertIsNone(scorer.red_flag_text(None))

    def test_red_flags_joined(self):
        text = scorer.red_flag_text(
            fake_fit(flags=["No visa sponsorship", "Wants 5+ years"]))
        self.assertIn("No visa sponsorship", text)
        self.assertIn("Wants 5+ years", text)

    def test_empty_red_flags_render_nothing(self):
        self.assertIsNone(scorer.red_flag_text(fake_fit(flags=[])))

    def test_red_flags_respect_discord_4096_limit(self):
        text = scorer.red_flag_text(fake_fit(flags=["y" * 9000]))
        self.assertLessEqual(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
