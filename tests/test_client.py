"""Exercises the terminal client's rendering and command parser offline by
replaying a full bots game's event/state stream into it."""

import argparse
import contextlib
import io
import random
import unittest

from tichu import ansi
from tichu.bot import Bot
from tichu.client import Client
from tichu.game import Phase, TichuGame


def make_client(seat=0):
    args = argparse.Namespace(
        host="x", port=1, name="Tester", spectate=False,
        token=None, fresh=True, no_color=True,
    )
    c = Client(args)
    c.seat = seat
    return c


class TestClientRendering(unittest.TestCase):
    def test_replay_full_game_through_renderer(self):
        ansi.set_enabled(False)
        rng = random.Random(5)
        game = TichuGame(["Tester", "Bot-A", "Bot-B", "Bot-C"], target=250, rng=rng)
        bots = [Bot(s, rng) for s in range(4)]
        client = make_client(seat=0)
        client.send = lambda obj: None

        out = io.StringIO()

        def push(events):
            for e in events:
                to = e.get("to")
                if to is None:
                    client.on_message({"event": "game", "data": e})
                elif to == 0:
                    client.on_message({"event": "game", "data": e})
            view = game.view(0)
            view["connected"] = [True] * 4
            client.on_message({"event": "state", "data": view})

        with contextlib.redirect_stdout(out):
            client.on_message({
                "event": "welcome", "seat": 0, "token": None,
                "name": "Tester", "target": 250,
            })
            client.on_message({
                "event": "started", "names": game.names, "target": 250,
            })
            push(game.drain_events())
            for _ in range(100_000):
                if game.phase is Phase.GAME_OVER:
                    break
                for bot in bots:
                    ev = bot.act(game)
                    if ev is not None:
                        push(ev)
                        break
            else:
                self.fail("game did not finish")
            # a couple of interactive commands against the final state
            client.handle_line("board")
            client.handle_line("hand")
            client.handle_line("score")
            client.handle_line("")

        text = out.getvalue()
        self.assertIn("GAME OVER", text)
        self.assertIn("hand result", text)
        self.assertIn("takes the trick", text)
        self.assertNotIn("Traceback", text)

    def test_command_parsing(self):
        ansi.set_enabled(False)
        client = make_client(seat=0)
        sent = []
        client.send = sent.append
        client.state = {
            "names": ["Tester", "Left", "Part", "Right"],
            "scores": [0, 0], "hand_no": 1, "target": 1000,
            "hand": ["2h", "5s"], "calls": [None] * 4, "out_order": [],
            "hand_counts": [14] * 4, "picked_up": [True] * 4,
            "phase": "playing", "turn": 0, "top": None, "wish": None,
            "trick_points": 0, "exchange_submitted": True,
        }
        with contextlib.redirect_stdout(io.StringIO()):
            client.handle_line("p 1 wish K")
            client.handle_line("play 8s 8h phx as 8")
            client.handle_line("5s 5d")  # bare cards = a play
            client.handle_line("pass")
            client.handle_line("t")
            client.handle_line("gt")
            client.handle_line("take")
            client.handle_line("x 2s 3h 4d")
            client.handle_line("dragon next")
            client.handle_line("dragon rig")
            client.handle_line("say hello table")
            client.handle_line("sit 2")
            client.handle_line("rematch")
            client.handle_line("p zzz")  # bad card: nothing sent
            client.handle_line("frobnicate")  # unknown: nothing sent
        self.assertEqual(sent, [
            {"cmd": "play", "cards": ["1"], "wish": "K"},
            {"cmd": "play", "cards": ["8s", "8h", "phx"], "phoenix_as": "8"},
            {"cmd": "play", "cards": ["5s", "5d"]},
            {"cmd": "pass"},
            {"cmd": "tichu"},
            {"cmd": "grand", "call": True},
            {"cmd": "grand", "call": False},
            {"cmd": "exchange", "cards": ["2s", "3h", "4d"]},
            {"cmd": "dragon", "to": 1},
            {"cmd": "dragon", "to": 3},
            {"cmd": "chat", "text": "hello table"},
            {"cmd": "sit", "seat": 2},
            {"cmd": "rematch"},
        ])

    def test_spectator_render(self):
        ansi.set_enabled(False)
        client = make_client(seat=None)
        client.send = lambda obj: None
        game = TichuGame(["A", "B", "C", "D"], target=100, rng=random.Random(2))
        view = game.view(None)
        view["connected"] = [True] * 4
        with contextlib.redirect_stdout(io.StringIO()) as out:
            client.on_message({"event": "state", "data": view})
            client.handle_line("board")
        self.assertIn("spectating", out.getvalue())


if __name__ == "__main__":
    unittest.main()
