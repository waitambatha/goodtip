"""Tests for MatchReader's card copy — MatchReader_Copy_Update_Erick.docx.

Mirrors orgs.tests.RecapWriterTests: explain() is pure enough to drive
directly with hand-built facts, so none of this needs a database.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from matchreader.algorithm import Prediction
from matchreader.services import MIN_GAMES_FOR_SEASON_RATE, explain

# Words the brief retires from this component (§2, §3): "form" and
# "prediction" sit too close to betting/tipster language once they sit next
# to "edge" and "pick" elsewhere on the card, and "not a tip" is the kind of
# negation the product avoids everywhere else. Checked as bare words against
# what explain() actually emits — internal names like Prediction/form_coef
# are a different thing and are not in scope here.
BANNED = ("form", "prediction", "not a tip")

TIERS = ["Even Contest", "Slight Edge", "Clear Edge", "Strong Position", "Dominant Position"]


def _row(name, *, wins, of, draws=0, is_leader=False, is_home=True):
    return {
        "name": name, "wins": wins, "draws": draws,
        "losses": of - wins - draws, "of": of,
        "is_leader": is_leader, "is_home": is_home,
    }


class MatchReaderCopyTests(SimpleTestCase):
    def _explain(self, *, tier, leader_is_home=True, match_id=1,
                 season_record=(12, 22), probability=0.6):
        home = _row("Alpha", wins=4, of=5, is_leader=leader_is_home, is_home=True)
        away = _row("Beta", wins=2, of=5, is_leader=not leader_is_home, is_home=False)
        prediction = Prediction(
            probability=probability,
            leader=home["name"] if leader_is_home else away["name"],
            tier=tier, home_leads=leader_is_home,
        )
        version = SimpleNamespace(baseline_accuracy=0.58)
        match = SimpleNamespace(id=match_id)
        return explain(prediction, home, away, version, match, season_record)

    # -- §2/§3: the retired words -------------------------------------------

    def test_no_banned_words_on_any_tier_or_seed(self):
        for tier in TIERS:
            for match_id in range(20):
                result = self._explain(tier=tier, match_id=match_id)
                text = " ".join(result["sentences"]).lower()
                for word in BANNED:
                    self.assertNotIn(word, text, f"{tier}/{match_id}: {text!r}")

    def test_favourite_playing_away_avoids_banned_words_too(self):
        for tier in TIERS[:-1]:  # Even Contest has no "favourite"
            result = self._explain(tier=tier, leader_is_home=False, season_record=(11, 22))
            text = " ".join(result["sentences"]).lower()
            for word in BANNED:
                self.assertNotIn(word, text)

    # -- §5: variation so same-tier cards stop reading identically ----------

    def test_headline_varies_across_matches_in_the_same_tier(self):
        headlines = {
            self._explain(tier="Slight Edge", match_id=i)["headline"]
            for i in range(10)
        }
        self.assertGreater(len(headlines), 1, "same tier should not always read identically")

    def test_headline_is_stable_for_the_same_match(self):
        first = self._explain(tier="Clear Edge", match_id=42)["headline"]
        second = self._explain(tier="Clear Edge", match_id=42)["headline"]
        self.assertEqual(first, second)

    def test_every_tier_has_three_approved_variants(self):
        from matchreader.services import HEADLINE_VARIANTS

        for tier in TIERS:
            self.assertEqual(len(HEADLINE_VARIANTS[tier]), 3, tier)

    # -- §4: small-sample framing ---------------------------------------------

    def test_small_sample_uses_a_raw_count_not_a_percentage(self):
        result = self._explain(tier="Slight Edge", season_record=(3, 5))
        self.assertIn("3 of 5", result["ground"])
        self.assertNotIn("%", result["ground"])
        self.assertIn("Early in the season", result["ground"])

    def test_sample_right_at_the_threshold_uses_the_percentage(self):
        result = self._explain(
            tier="Slight Edge", season_record=(11, MIN_GAMES_FOR_SEASON_RATE),
        )
        self.assertIn("%", result["ground"])
        self.assertNotIn("Early in the season", result["ground"])

    def test_sample_just_under_the_threshold_uses_the_raw_count(self):
        result = self._explain(
            tier="Slight Edge", season_record=(10, MIN_GAMES_FOR_SEASON_RATE - 1),
        )
        self.assertNotIn("%", result["ground"])
        self.assertIn("Early in the season", result["ground"])

    def test_no_games_played_yet_means_no_ground_line_at_all(self):
        result = self._explain(tier="Slight Edge", season_record=(0, 0))
        self.assertEqual(result["ground"], "")

    def test_small_sample_framing_holds_for_even_contest_and_away_favourite(self):
        even = self._explain(tier="Even Contest", season_record=(3, 5))
        self.assertIn("3 of 5", even["ground"])
        away_fav = self._explain(tier="Clear Edge", leader_is_home=False, season_record=(3, 5))
        self.assertIn("3 of 5", away_fav["ground"])

    # -- form comparison line, shortened --------------------------------------

    def test_form_line_is_two_short_sentences(self):
        result = self._explain(tier="Clear Edge")
        self.assertEqual(result["form"].count("."), 2)
        self.assertNotIn("while", result["form"])
