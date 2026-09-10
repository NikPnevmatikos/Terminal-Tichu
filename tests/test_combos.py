import itertools
import random
import unittest

from tichu.cards import Card, DOG, DRAGON, MAHJONG, PHOENIX, Suit, full_deck, parse_card
from tichu.combos import (
    Combo,
    ComboKind,
    beats,
    finalize,
    find_play,
    interpretations,
    legal_plays,
    wish_plays,
)


def C(*codes):
    return [parse_card(code) for code in codes]


def kinds(cards):
    return {c.kind for c in interpretations(cards)}


class TestIdentification(unittest.TestCase):
    def test_singles(self):
        self.assertEqual(kinds(C("5h")), {ComboKind.SINGLE})
        self.assertEqual(kinds(C("drg")), {ComboKind.SINGLE})
        self.assertEqual(kinds(C("1")), {ComboKind.SINGLE})
        self.assertEqual(kinds(C("dog")), {ComboKind.DOG})
        phx = interpretations(C("phx"))
        self.assertEqual(len(phx), 1)
        self.assertIsNone(phx[0].power)

    def test_pairs_triples(self):
        self.assertEqual(kinds(C("5h", "5s")), {ComboKind.PAIR})
        self.assertEqual(kinds(C("5h", "6s")), set())
        self.assertEqual(kinds(C("5h", "phx")), {ComboKind.PAIR})
        self.assertEqual(kinds(C("5h", "5s", "5d")), {ComboKind.TRIPLE})
        self.assertEqual(kinds(C("5h", "5s", "phx")), {ComboKind.TRIPLE})
        self.assertEqual(kinds(C("1", "phx")), set())  # no pairing the Mah Jong
        self.assertEqual(kinds(C("drg", "phx")), set())

    def test_full_house(self):
        self.assertEqual(kinds(C("5h", "5s", "5d", "9c", "9h")), {ComboKind.FULLHOUSE})
        fh = interpretations(C("5h", "5s", "5d", "9c", "phx"))
        self.assertEqual({c.kind for c in fh}, {ComboKind.FULLHOUSE})
        self.assertEqual([c.power for c in fh], [5.0])  # Phoenix completes the pair
        both = interpretations(C("5h", "5s", "9c", "9h", "phx"))
        self.assertEqual(sorted(c.power for c in both), [5.0, 9.0])

    def test_straights(self):
        self.assertEqual(kinds(C("3h", "4s", "5d", "6c", "7h")), {ComboKind.STRAIGHT})
        self.assertEqual(kinds(C("1", "2s", "3d", "4c", "5h")), {ComboKind.STRAIGHT})
        self.assertEqual(kinds(C("3h", "4s", "5d", "6c")), set())  # too short
        self.assertEqual(kinds(C("3h", "4s", "5d", "6c", "8h")), set())  # gap
        # Phoenix can fill a gap or extend either end.
        gap = interpretations(C("3h", "4s", "6c", "7h", "phx"))
        self.assertEqual([(c.kind, c.power, c.phoenix_as) for c in gap],
                         [(ComboKind.STRAIGHT, 7.0, 5)])
        ends = interpretations(C("3h", "4s", "5d", "6c", "phx"))
        self.assertEqual(sorted((c.power, c.phoenix_as) for c in ends),
                         [(6.0, 2), (7.0, 7)])
        # ...but never stand in for the Mah Jong or go past the Ace.
        low = interpretations(C("2h", "3s", "4d", "5c", "phx"))
        self.assertEqual(sorted((c.power, c.phoenix_as) for c in low), [(6.0, 6)])
        high = interpretations(C("Jh", "Qs", "Kd", "Ac", "phx"))
        self.assertEqual(sorted((c.power, c.phoenix_as) for c in high), [(14.0, 10)])

    def test_pair_sequences(self):
        self.assertEqual(kinds(C("5h", "5s", "6d", "6c")), {ComboKind.PAIRSEQ})
        self.assertEqual(kinds(C("5h", "5s", "7d", "7c")), set())
        self.assertEqual(kinds(C("5h", "5s", "6d", "phx")), {ComboKind.PAIRSEQ})
        three = interpretations(C("5h", "5s", "6d", "6c", "7h", "phx"))
        self.assertEqual([(c.kind, c.power, c.phoenix_as) for c in three],
                         [(ComboKind.PAIRSEQ, 7.0, 7)])

    def test_bombs(self):
        self.assertEqual(kinds(C("5h", "5s", "5d", "5c")), {ComboKind.BOMB4})
        self.assertEqual(kinds(C("5h", "5s", "5d", "phx")), set())  # no Phoenix bombs
        self.assertEqual(kinds(C("3h", "4h", "5h", "6h", "7h")), {ComboKind.STRAIGHTFLUSH})
        # A same-suit run is a bomb, never a plain straight.
        self.assertNotIn(ComboKind.STRAIGHT, kinds(C("3h", "4h", "5h", "6h", "7h")))
        # With the Phoenix it is a plain straight, not a bomb.
        self.assertEqual(kinds(C("3h", "4h", "5h", "6h", "phx")), {ComboKind.STRAIGHT})

    def test_specials_in_combos(self):
        self.assertEqual(kinds(C("drg", "ah")), set())
        self.assertEqual(kinds(C("dog", "2h")), set())
        self.assertEqual(kinds(C("1", "2s", "3d", "4c", "5h", "6h")), {ComboKind.STRAIGHT})


class TestBeats(unittest.TestCase):
    def one(self, *codes):
        interps = interpretations(C(*codes))
        assert len(interps) == 1, interps
        return interps[0]

    def test_single_chain(self):
        mah = self.one("1")
        five = self.one("5h")
        ace = self.one("as")
        drg = self.one("drg")
        self.assertTrue(beats(five, mah))
        self.assertTrue(beats(ace, five))
        self.assertTrue(beats(drg, ace))
        self.assertFalse(beats(ace, drg))

    def test_phoenix_single(self):
        five = self.one("5h")
        phx = finalize(interpretations(C("phx"))[0], five)
        self.assertEqual(phx.power, 5.5)
        self.assertTrue(beats(phx, five))
        six = self.one("6c")
        self.assertTrue(beats(six, phx))
        self.assertFalse(beats(self.one("5s"), phx))
        # Phoenix never beats the Dragon.
        self.assertIsNone(finalize(interpretations(C("phx"))[0], self.one("drg")))
        # Led Phoenix is 1.5: beats the Mah Jong, is beaten by a 2.
        led = finalize(interpretations(C("phx"))[0], None)
        self.assertEqual(led.power, 1.5)
        self.assertTrue(beats(self.one("2s"), led))

    def test_type_and_size_discipline(self):
        pair9 = self.one("9h", "9s")
        pairj = self.one("jh", "js")
        self.assertTrue(beats(pairj, pair9))
        self.assertFalse(beats(self.one("ah"), pair9))
        s5 = self.one("3h", "4s", "5d", "6c", "7h")
        s6 = self.one("2h", "3s", "4d", "5c", "6h", "7s")
        self.assertFalse(beats(s6, s5))  # straights must match length
        s5hi = self.one("4h", "5s", "6d", "7c", "8h")
        self.assertTrue(beats(s5hi, s5))

    def test_full_house_by_triple(self):
        low = self.one("5h", "5s", "5d", "ac", "ah")
        high = self.one("6h", "6s", "6d", "2c", "2h")
        self.assertTrue(beats(high, low))

    def test_bomb_hierarchy(self):
        pair = self.one("ah", "as")
        b5 = self.one("5h", "5s", "5d", "5c")
        b9 = self.one("9h", "9s", "9d", "9c")
        sf5 = self.one("3h", "4h", "5h", "6h", "7h")
        sf6 = self.one("3d", "4d", "5d", "6d", "7d", "8d")
        sf5hi = self.one("9s", "10s", "js", "qs", "ks")
        self.assertTrue(beats(b5, pair))
        self.assertTrue(beats(b9, b5))
        self.assertFalse(beats(b5, b9))
        self.assertTrue(beats(sf5, b9))  # any straight flush beats any 4-bomb
        self.assertFalse(beats(b9, sf5))
        self.assertTrue(beats(sf6, sf5hi))  # longer beats higher-but-shorter
        self.assertTrue(beats(sf5hi, sf5))
        drg = self.one("drg")
        self.assertTrue(beats(b5, drg))  # only bombs beat the Dragon


class TestFindPlay(unittest.TestCase):
    def test_hint_selects_interpretation(self):
        cards = C("3h", "4s", "5d", "6c", "phx")
        combo, _ = find_play(cards, None)
        self.assertEqual(combo.power, 7.0)  # strongest reading by default
        combo, _ = find_play(cards, None, phoenix_hint=2)
        self.assertEqual(combo.power, 6.0)
        combo, err = find_play(cards, None, phoenix_hint=9)
        self.assertIsNone(combo)
        self.assertIn("Phoenix", err)

    def test_following(self):
        top, _ = find_play(C("8h"), None)
        combo, _ = find_play(C("9h"), top)
        self.assertEqual(combo.power, 9.0)
        combo, err = find_play(C("7h"), top)
        self.assertIsNone(combo)


class TestDisplayOrder(unittest.TestCase):
    """The Phoenix is shown in the slot it fills, not trailing the combo."""

    def shown(self, codes, hint=None):
        combo, reason = find_play(C(*codes), None, hint)
        self.assertIsNotNone(combo, reason)
        return [c.code for c in combo.ordered_cards]

    def test_phoenix_sits_where_it_completes_a_straight(self):
        self.assertEqual(
            self.shown(["2s", "3h", "4d", "phx", "6c", "7s"]),
            ["2s", "3h", "4d", "Phx", "6c", "7s"],
        )
        self.assertEqual(  # standing in below the run
            self.shown(["phx", "3h", "4d", "5c", "6s"], hint=2),
            ["Phx", "3h", "4d", "5c", "6s"],
        )
        self.assertEqual(  # standing in above it: last is right
            self.shown(["10s", "Jh", "Qd", "Kc", "phx"], hint=14),
            ["10s", "Jh", "Qd", "Kc", "Phx"],
        )

    def test_phoenix_sits_with_the_rank_it_doubles(self):
        self.assertEqual(
            self.shown(["8s", "8h", "Kd", "Kc", "phx"], hint=8),
            ["8s", "8h", "Phx", "Kd", "Kc"],
        )
        self.assertEqual(
            self.shown(["5s", "5h", "6d", "phx", "7c", "7s"], hint=6),
            ["5s", "5h", "6d", "Phx", "7s", "7c"],
        )
        # after the real card of the same rank, never before it
        self.assertEqual(self.shown(["9s", "phx"], hint=9), ["9s", "Phx"])

    def test_a_combo_without_a_phoenix_is_untouched(self):
        self.assertEqual(
            self.shown(["3h", "4s", "5d", "6c", "7s"]),
            ["3h", "4s", "5d", "6c", "7s"],
        )

    def test_a_lone_phoenix_has_no_rank_to_sit_at(self):
        self.assertEqual(self.shown(["phx"]), ["Phx"])


class TestLegalPlays(unittest.TestCase):
    def brute_force(self, hand, top):
        """Reference implementation: full subset enumeration."""
        found = set()
        for size in range(1, len(hand) + 1):
            for subset in itertools.combinations(hand, size):
                for interp in interpretations(subset):
                    fin = finalize(interp, top)
                    if fin is None:
                        continue
                    if top is None or beats(fin, top):
                        found.add(self.signature(fin))
        return found

    @staticmethod
    def signature(combo):
        return (
            combo.kind,
            combo.size,
            combo.power,
            frozenset(c.rank for c in combo.cards),
        )

    def test_matches_brute_force(self):
        rng = random.Random(42)
        deck = full_deck()
        tops = [
            None,
            interpretations(C("8h"))[0],
            interpretations(C("8h", "8s"))[0],
            interpretations(C("4h", "4s", "4d", "9c", "9h"))[0],
            interpretations(C("3h", "4s", "5d", "6c", "7h"))[0],
            interpretations(C("5h", "5s", "6d", "6c"))[0],
            interpretations(C("drg"))[0],
        ]
        for trial in range(30):
            hand = rng.sample(deck, 9)
            for top in tops:
                fast = {self.signature(p) for p in legal_plays(hand, top)}
                slow = self.brute_force(hand, top)
                self.assertEqual(fast, slow, f"hand={hand} top={top}")

    def test_wish_plays(self):
        # Single 8 on the table, wish is K: a lone king must appear.
        top = interpretations(C("8h"))[0]
        hand = C("kh", "5s", "3d")
        plays = wish_plays(hand, top, 13)
        self.assertTrue(plays)
        self.assertTrue(all(p.contains_rank(13) for p in plays))
        # A king that cannot beat the top cannot fulfil the wish...
        top_a = interpretations(C("ah"))[0]
        self.assertEqual(wish_plays(hand, top_a, 13), [])
        # ...unless it sits inside a bomb.
        bomb_hand = C("kh", "ks", "kd", "kc", "3d")
        self.assertTrue(wish_plays(bomb_hand, top_a, 13))
        # The Phoenix standing in for the rank does not fulfil the wish.
        phx_hand = C("phx", "5s")
        self.assertEqual(wish_plays(phx_hand, top, 13), [])


if __name__ == "__main__":
    unittest.main()
