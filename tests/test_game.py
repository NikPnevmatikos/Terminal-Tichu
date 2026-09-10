import random
import unittest

from tichu.bot import Bot
from tichu.cards import MAHJONG, full_deck, parse_card
from tichu.game import IllegalAction, Phase, TichuGame


def C(*codes):
    return [parse_card(code) for code in codes]


def rig_deal(game, south, west, north, east):
    """Overwrite the current (fresh) deal with small, fully controlled hands.

    Hands may be unequal; flow tests do not rely on the 56-card total."""
    hands = [C(*south), C(*west), C(*north), C(*east)]
    used = [c for h in hands for c in h]
    assert len(set(used)) == len(used), "duplicate card in rig"
    game.hands = hands
    game.first8 = [list(h[:8]) for h in hands]


def start_play(game, leader=None):
    """Skip the grand-tichu and exchange phases for trick-logic tests."""
    game.picked_up = [True] * 4
    game.exchanges = [(), (), (), ()]
    game.phase = Phase.PLAYING
    if leader is None:
        leader = next(
            s for s in range(4) if any(c.rank == MAHJONG for c in game.hands[s])
        )
    game.leader = leader
    game.turn = leader
    game._events.clear()


def new_game(**kw):
    return TichuGame(["S", "W", "N", "E"], rng=random.Random(1), **kw)


class TestPhases(unittest.TestCase):
    def test_grand_then_exchange_then_play(self):
        g = new_game()
        self.assertIs(g.phase, Phase.GRAND_TICHU)
        for s in range(4):
            self.assertEqual(len(g.view(s)["hand"]), 8)
        ev = g.decide_grand(0, True)
        self.assertIn({"type": "called", "seat": 0, "call": "grand"}, ev)
        self.assertEqual(len(g.view(0)["hand"]), 14)
        for s in (1, 2, 3):
            g.decide_grand(s, False)
        self.assertIs(g.phase, Phase.EXCHANGE)
        with self.assertRaises(IllegalAction):
            g.decide_grand(1, True)
        received_events = []
        for s in range(4):
            ev = g.submit_exchange(s, g.hands[s][:3])
            received_events += [e for e in ev if e["type"] == "received"]
        self.assertIs(g.phase, Phase.PLAYING)
        self.assertEqual(len(received_events), 4)
        for e in received_events:
            self.assertEqual(len(e["gifts"]), 3)
        for s in range(4):
            self.assertEqual(len(g.hands[s]), 14)
        mah_holder = next(
            s for s in range(4) if any(c.rank == MAHJONG for c in g.hands[s])
        )
        self.assertEqual(g.turn, mah_holder)

    def test_exchange_routing(self):
        g = new_game()
        for s in range(4):
            g.decide_grand(s, False)
        gifts = {}
        for s in range(4):
            gifts[s] = list(g.hands[s][:3])
            g.submit_exchange(s, gifts[s])
        for s in range(4):
            to_next, to_partner, to_prev = gifts[s]
            self.assertIn(to_next, g.hands[(s + 1) % 4])
            self.assertIn(to_partner, g.hands[(s + 2) % 4])
            self.assertIn(to_prev, g.hands[(s + 3) % 4])

    def test_tichu_call_window(self):
        g = new_game()
        with self.assertRaises(IllegalAction):
            g.call_tichu(0)  # has not seen all 14 yet
        g.decide_grand(0, False)
        g.call_tichu(0)
        self.assertEqual(g.calls[0], "tichu")
        with self.assertRaises(IllegalAction):
            g.call_tichu(0)
        g.decide_grand(1, True)
        with self.assertRaises(IllegalAction):
            g.call_tichu(1)  # grand callers cannot also call tichu


class TestTricks(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        rig_deal(
            self.g,
            south=["1", "2h", "5h", "5s"],
            west=["3h", "7h", "7s", "8c"],
            north=["4h", "9h", "9s", "kc"],
            east=["6h", "jh", "js", "dog"],
        )
        start_play(self.g)

    def test_singles_trick(self):
        g = self.g
        self.assertEqual(g.turn, 0)
        g.play(0, C("2h"))
        g.play(1, C("3h"))
        g.play(2, C("4h"))
        g.play(3, C("6h"))
        g.pass_turn(0)
        g.pass_turn(1)
        ev = g.pass_turn(2)
        won = [e for e in ev if e["type"] == "trick_won"]
        self.assertEqual(won, [{"type": "trick_won", "seat": 3, "points": 0, "cards": 4}])
        self.assertEqual(g.turn, 3)  # winner leads
        self.assertIsNone(g.top)

    def test_leader_cannot_pass_and_turn_enforced(self):
        g = self.g
        with self.assertRaises(IllegalAction):
            g.pass_turn(0)
        with self.assertRaises(IllegalAction):
            g.play(1, C("3h"))  # not their turn, not a bomb

    def test_pair_trick_and_wrong_size(self):
        g = self.g
        g.play(0, C("5h", "5s"))
        with self.assertRaises(IllegalAction):
            g.play(1, C("8c"))  # single on a pair
        g.play(1, C("7h", "7s"))
        g.play(2, C("9h", "9s"))
        g.play(3, C("jh", "js"))
        g.pass_turn(0)
        g.pass_turn(1)
        g.pass_turn(2)
        self.assertEqual(g.turn, 3)

    def test_tichu_blocked_after_first_play(self):
        g = self.g
        g.play(0, C("2h"))
        with self.assertRaises(IllegalAction):
            g.call_tichu(0)
        g.call_tichu(1)  # has not played yet


class TestDog(unittest.TestCase):
    def test_dog_passes_lead_to_partner(self):
        g = new_game()
        rig_deal(
            g,
            south=["dog", "2h", "3h"],
            west=["5h", "6h", "7h"],
            north=["8h", "9h", "th"],
            east=["jh", "qh", "kh"],
        )
        start_play(g, leader=0)
        ev = g.play(0, C("dog"))
        self.assertIn({"type": "dog", "seat": 0, "to": 2}, ev)
        self.assertEqual(g.turn, 2)
        self.assertIsNone(g.top)
        g.play(2, C("8h"))

    def test_dog_cannot_follow_and_cannot_be_bombed(self):
        g = new_game()
        rig_deal(
            g,
            south=["2h", "3h"],
            west=["dog", "5h"],
            north=["8h", "8s", "8d", "8c"],
            east=["jh", "qh"],
        )
        start_play(g, leader=0)
        g.play(0, C("2h"))
        with self.assertRaises(IllegalAction):
            g.play(1, C("dog"))
        g.play(1, C("5h"))
        g.pass_turn(2)
        g.pass_turn(3)
        g.pass_turn(0)
        # west won and leads the Dog: nothing may be played on it, the lead
        # moves instantly, so an interjected bomb hits an empty trick.
        g.play(1, C("dog"))
        self.assertEqual(g.turn, 3)  # west's partner
        with self.assertRaises(IllegalAction):
            g.play(2, C("8h", "8s", "8d", "8c"))

    def test_dog_skips_out_partner(self):
        g = new_game()
        rig_deal(
            g,
            south=["dog", "2h", "3h"],
            west=["5h", "6h"],
            north=["9h"],
            east=["jh", "qh"],
        )
        start_play(g, leader=2)
        g.play(2, C("9h"))  # north goes out immediately
        self.assertEqual(g.out_order, [2])
        g.play(3, C("jh"))
        g.pass_turn(0)
        g.pass_turn(1)
        self.assertEqual(g.turn, 3)  # east won (north is out and is skipped)
        g.play(3, C("qh"))  # east's last card, trick still open
        self.assertEqual(g.out_order, [2, 3])
        g.pass_turn(0)
        g.pass_turn(1)
        # east won while out: the lead goes to the next player with cards
        self.assertEqual(g.turn, 0)
        self.assertIsNone(g.top)
        # south's partner (north) is out, and so is east: the Dog hands the
        # lead to the next active player after the partner - south itself.
        ev = g.play(0, C("dog"))
        self.assertIn({"type": "dog", "seat": 0, "to": 0}, ev)
        self.assertEqual(g.turn, 0)


class TestWish(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        rig_deal(
            self.g,
            south=["1", "4h"],
            west=["kh", "2h"],
            north=["3h", "qh"],
            east=["6h", "7h"],
        )
        start_play(self.g, leader=0)

    def test_wish_forces_play(self):
        g = self.g
        ev = g.play(0, C("1"), wish=13)
        self.assertIn({"type": "wish_set", "seat": 0, "rank": 13}, ev)
        with self.assertRaises(IllegalAction):
            g.pass_turn(1)  # west holds the king: must play it
        with self.assertRaises(IllegalAction):
            g.play(1, C("2h"))
        ev = g.play(1, C("kh"))
        self.assertIn({"type": "wish_fulfilled", "seat": 1}, ev)
        self.assertIsNone(g.wish)
        g.pass_turn(2)  # north has no king: free to pass

    def test_wish_persists_until_fulfilled(self):
        g = self.g
        g.play(0, C("1"), wish=12)  # wish a queen
        g.play(1, C("2h"))  # west has no queen: any play is fine
        with self.assertRaises(IllegalAction):
            g.pass_turn(2)  # north holds the queen and Q beats 2
        g.play(2, C("qh"))
        self.assertIsNone(g.wish)

    def test_wish_not_forced_when_it_cannot_beat(self):
        g = self.g
        g.play(0, C("1"), wish=12)
        g.play(1, C("kh"))  # west has no queen: free play
        # north holds the queen, but Q does not beat K and there is no other
        # legal play containing it: north may pass, the wish stays alive.
        g.pass_turn(2)
        self.assertEqual(g.wish, 12)

    def test_wish_requires_mahjong(self):
        g = self.g
        with self.assertRaises(IllegalAction):
            g.play(0, C("4h"), wish=13)


class TestWishCannotBeat(unittest.TestCase):
    def test_unbeatable_wish_frees_player(self):
        g = new_game()
        rig_deal(
            g,
            south=["1", "ah"],
            west=["5h", "2h"],
            north=["3h"],
            east=["6h"],
        )
        start_play(g, leader=0)
        g.play(0, C("ah"))
        g.pass_turn(1)
        g.pass_turn(2)
        g.pass_turn(3)
        # south won; now leads Mah Jong wishing for a 5
        g.play(0, C("1"), wish=5)
        # west's 5 cannot beat... the Mah Jong is a 1, the 5 CAN beat it.
        with self.assertRaises(IllegalAction):
            g.play(1, C("2h"))
        g.play(1, C("5h"))
        self.assertIsNone(g.wish)


class TestBombs(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        rig_deal(
            self.g,
            south=["2h", "3h", "4h"],
            west=["9h", "9c", "kh"],
            north=["5h", "5s", "5d", "5c", "2c"],
            east=["jh", "th", "3c"],
        )
        start_play(self.g, leader=0)

    def test_out_of_turn_bomb(self):
        g = self.g
        g.play(0, C("3h"))
        self.assertEqual(g.turn, 1)
        ev = g.play(2, C("5h", "5s", "5d", "5c"))  # north interjects
        played = [e for e in ev if e["type"] == "played"][0]
        self.assertTrue(played["bomb"])
        self.assertTrue(played["out_of_turn"])
        self.assertEqual(g.turn, 3)  # play continues after the bomber
        with self.assertRaises(IllegalAction):
            g.play(3, C("jh"))  # nothing but a bigger bomb beats a bomb
        g.pass_turn(3)
        g.pass_turn(0)
        g.pass_turn(1)
        self.assertEqual(g.turn, 2)  # bomber takes the trick and leads

    def test_no_out_of_turn_bomb_on_empty_trick(self):
        g = self.g
        with self.assertRaises(IllegalAction):
            g.play(2, C("5h", "5s", "5d", "5c"))

    def test_out_of_turn_non_bomb_rejected(self):
        g = self.g
        g.play(0, C("3h"))
        with self.assertRaises(IllegalAction):
            g.play(3, C("jh"))


class TestDragon(unittest.TestCase):
    def test_dragon_trick_is_given_away(self):
        g = new_game()
        rig_deal(
            g,
            south=["2h", "drg"],
            west=["3h", "6h"],
            north=["4h", "7h"],
            east=["5h", "8h"],
        )
        start_play(g, leader=0)
        g.play(0, C("2h"))
        g.play(1, C("3h"))
        g.play(2, C("4h"))
        g.play(3, C("5h"))
        g.play(0, C("drg"))  # south's last card - south is out but wins
        self.assertEqual(g.out_order, [0])
        g.pass_turn(1)
        g.pass_turn(2)
        ev = g.pass_turn(3)
        self.assertIn("dragon_pending", [e["type"] for e in ev])
        self.assertIs(g.phase, Phase.DRAGON_GIFT)
        with self.assertRaises(IllegalAction):
            g.give_dragon(0, 2)  # partner is not an opponent
        with self.assertRaises(IllegalAction):
            g.give_dragon(1, 0)  # only the winner chooses
        ev = g.give_dragon(0, 1)
        gift = [e for e in ev if e["type"] == "dragon_given"][0]
        self.assertEqual(gift["to"], 1)
        self.assertEqual(gift["points"], 30)  # 5 + Dragon 25
        self.assertEqual(g.view(0)["pile_points"][1], 30)
        self.assertIs(g.phase, Phase.PLAYING)
        self.assertEqual(g.turn, 1)  # south is out; next active leads

    def test_dragon_cannot_go_to_an_opponent_who_is_out(self):
        g = new_game()
        rig_deal(
            g,
            south=["2h", "drg"],
            west=["3h"],           # west sheds their last card in trick one
            north=["4h", "7h"],
            east=["5h", "8h"],
        )
        start_play(g, leader=0)
        g.play(0, C("2h"))
        g.play(1, C("3h"))         # west goes out
        g.play(2, C("4h"))
        g.play(3, C("5h"))
        self.assertEqual(g.out_order, [1])
        g.play(0, C("drg"))
        g.pass_turn(2)
        g.pass_turn(3)
        self.assertIs(g.phase, Phase.DRAGON_GIFT)
        self.assertEqual(g.dragon_targets(0), (3,))
        self.assertEqual(g.view(0)["dragon_targets"], [3])
        with self.assertRaises(IllegalAction):
            g.give_dragon(0, 1)    # west is already out
        self.assertIs(g.phase, Phase.DRAGON_GIFT)
        self.assertEqual(g.view(0)["pile_points"], [0, 0, 0, 0])
        ev = g.give_dragon(0, 3)
        gift = [e for e in ev if e["type"] == "dragon_given"][0]
        self.assertEqual(gift["to"], 3)
        self.assertEqual(g.view(0)["pile_points"][3], 30)  # 5 + Dragon 25

    def test_bot_never_gifts_the_dragon_to_a_finished_opponent(self):
        g = new_game()
        rig_deal(
            g,
            south=["2h", "drg"],
            west=["3h"],
            north=["4h", "7h"],
            east=["5h", "8h"],
        )
        start_play(g, leader=0)
        g.play(0, C("2h"))
        g.play(1, C("3h"))
        g.play(2, C("4h"))
        g.play(3, C("5h"))
        g.play(0, C("drg"))
        g.pass_turn(2)
        g.pass_turn(3)
        self.assertEqual(Bot(0).choose_dragon_gift(g), 3)


class TestScoring(unittest.TestCase):
    def test_normal_hand_scoring_and_transfers(self):
        g = new_game(target=10)
        rig_deal(
            g,
            south=["2h", "3s"],
            west=["5h"],
            north=["9h"],
            east=["kh", "ks"],
        )
        start_play(g, leader=0)
        g.call_tichu(1)  # west calls tichu and will go out first
        g.play(0, C("2h"))
        g.play(1, C("5h"))
        self.assertEqual(g.out_order, [1])
        g.play(2, C("9h"))
        self.assertEqual(g.out_order, [1, 2])  # opponents: no double win
        g.play(3, C("kh"))
        g.pass_turn(0)
        # trick closes at east: 2h+5h+9h+kh = 15 points to east's pile
        g.play(3, C("ks"))  # east out - three out, but south may still beat
        g.pass_turn(0)
        summary = g.last_hand_summary
        self.assertIsNotNone(summary)
        self.assertFalse(summary["double_win"])
        self.assertEqual(summary["first_out"], 1)
        # south was last: pile (empty) -> west; hand 3s (0 pts) -> team 1.
        # team 1: east pile 15 + ks 10 + hand 0 + tichu bonus 100 = 125
        self.assertEqual(summary["team_points"], [0, 125])
        self.assertEqual(g.scores, [0, 125])
        # target 10 reached: game over
        self.assertIs(g.phase, Phase.GAME_OVER)
        self.assertEqual(g.winner, 1)

    def test_double_win(self):
        g = new_game()
        rig_deal(
            g,
            south=["2h"],
            west=["5h", "6h"],
            north=["3h"],
            east=["kh", "qh"],
        )
        start_play(g, leader=0)
        g.calls[3] = "grand"  # east had called grand tichu
        g.play(0, C("2h"))
        self.assertEqual(g.out_order, [0])
        g.pass_turn(1)
        ev = g.play(2, C("3h"))
        types = [e["type"] for e in ev]
        self.assertIn("hand_end", types)
        summary = [e for e in ev if e["type"] == "hand_end"][0]
        self.assertTrue(summary["double_win"])
        # 200 for the 1-2 finish, east's failed grand costs 200
        self.assertEqual(summary["team_points"], [200, -200])
        self.assertEqual(g.scores, [200, -200])
        # a new hand was dealt automatically
        self.assertIn("hand_start", types)
        self.assertIs(g.phase, Phase.GRAND_TICHU)
        self.assertEqual(g.hand_no, 2)
        for s in range(4):
            self.assertEqual(len(g.hands[s]), 14)

    def test_full_deck_deal_is_consistent(self):
        g = new_game()
        dealt = [c for h in g.hands for c in h]
        self.assertEqual(len(dealt), 56)
        self.assertEqual(set(dealt), set(full_deck()))


if __name__ == "__main__":
    unittest.main()
