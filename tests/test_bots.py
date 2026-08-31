import random
import unittest

from tichu.bot import Bot, run_bot_game
from tichu.game import Phase, TichuGame


class TestBotGames(unittest.TestCase):
    def play_full_game(self, seed, target=400):
        """Drive four bots through a whole game, checking invariants on the
        way: every hand's card points total 100 (or a 200 double win) before
        tichu bonuses, and the running scores always match the summaries."""
        rng = random.Random(seed)
        game = TichuGame(["N", "E", "S", "W"], target=target, rng=rng)
        bots = [Bot(s, rng) for s in range(4)]
        game.drain_events()
        expected_scores = [0, 0]
        hands_seen = 0
        for _ in range(200_000):
            if game.phase is Phase.GAME_OVER:
                break
            events = None
            for bot in bots:
                events = bot.act(game)
                if events is not None:
                    break
            self.assertIsNotNone(events, f"stalled in phase {game.phase}")
            for e in events:
                if e["type"] == "hand_end":
                    hands_seen += 1
                    raw = [
                        pts - sum(b["delta"] for b in e["bonuses"] if b["seat"] % 2 == t)
                        for t, pts in enumerate(e["team_points"])
                    ]
                    if e["double_win"]:
                        self.assertEqual(sorted(raw), [0, 200], e)
                    else:
                        self.assertEqual(sum(raw), 100, e)
                        self.assertTrue(all(-25 <= r <= 125 for r in raw), e)
                    expected_scores[0] += e["team_points"][0]
                    expected_scores[1] += e["team_points"][1]
                    self.assertEqual(e["scores"], expected_scores)
        else:
            self.fail("game did not finish")
        self.assertEqual(game.scores, expected_scores)
        self.assertGreaterEqual(max(game.scores), target)
        self.assertEqual(game.winner, 0 if game.scores[0] > game.scores[1] else 1)
        self.assertGreater(hands_seen, 0)
        return hands_seen

    def test_full_games_many_seeds(self):
        total_hands = 0
        for seed in (1, 7, 42, 1234, 987654):
            total_hands += self.play_full_game(seed)
        # sanity: a game to 400 takes a handful of hands
        self.assertGreater(total_hands, 10)

    def test_runner_helper(self):
        game = run_bot_game(target=200, seed=3)
        self.assertIs(game.phase, Phase.GAME_OVER)
        self.assertIn(game.winner, (0, 1))


if __name__ == "__main__":
    unittest.main()
