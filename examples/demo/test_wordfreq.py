"""Tests du compteur de frequence de mots (demo AgentWatch)."""

from __future__ import annotations

import unittest

from wordfreq import tokenize, top_words


class WordFreqTests(unittest.TestCase):
    def test_tokenize_lowercases_and_keeps_accents(self) -> None:
        self.assertEqual(tokenize("Été, ÉTÉ été! l'ami"), ["été", "été", "été", "l'ami"])

    def test_min_len_filters_short_words(self) -> None:
        self.assertEqual(tokenize("a bb ccc", min_len=2), ["bb", "ccc"])

    def test_top_words_orders_by_count_then_alpha(self) -> None:
        text = "b a b c a c c"
        self.assertEqual(top_words(text, top=2), [("c", 3), ("a", 2)])

    def test_top_limits_result(self) -> None:
        self.assertEqual(len(top_words("x y z x y z w", top=3)), 3)


if __name__ == "__main__":
    unittest.main()
