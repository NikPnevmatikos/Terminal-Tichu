"""Exercises the terminal client's rendering and command parser offline by
replaying a full bots game's event/state stream into it."""

import argparse
import contextlib
import io
import random
import re
import unittest

from tichu import ansi
from tichu.bot import Bot
from tichu.client import Client, vis
from tichu.game import Phase, TichuGame


def make_client(seat=0):
    args = argparse.Namespace(
        host="x", port=1, name="Tester", spectate=False,
        token=None, fresh=True, no_color=True,
    )
    c = Client(args)
    c.seat = seat
    return c


def sample_state(**over):
    """Mid-trick snapshot as seat 0: partner called Tichu, Left is out,
    Right is offline and holds the table with a single Ace."""
    st = {
        "names": ["Tester", "Left", "Part", "Right"],
        "scores": [240, 185], "hand_no": 3, "target": 1000,
        "hand": ["2h", "5s"], "calls": [None, None, "tichu", None], "out_order": [1],
        "hand_counts": [11, 0, 11, 14], "picked_up": [True] * 4,
        "phase": "playing", "turn": 0,
        "top": {"seat": 3, "cards": ["Ad"], "combo": "single A♦", "kind": "single",
                "power": 14, "size": 1, "bomb": False},
        "wish": None, "trick_points": 0, "exchange_submitted": True,
        "connected": [True, True, True, False],
    }
    st.update(over)
    return st


DRAGON_PLAY = {"type": "played", "seat": 3, "cards": ["Drg"], "combo": "single Drg",
               "kind": "single", "bomb": False, "out_of_turn": False}
BOMB_PLAY = {"type": "played", "seat": 1, "cards": ["5s", "5h", "5d", "5c"],
             "combo": "bomb (5♠ 5♥ 5♦ 5♣)", "kind": "bomb", "bomb": True, "out_of_turn": True}


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
        text = out.getvalue()
        self.assertIn("spectating", text)
        # seats are numbered as in the lobby; the game is in its first phase
        self.assertIn("[0] A", text)
        self.assertIn("[2] C", text)
        self.assertIn("Grand Tichu?", text)

    def test_table_and_log_show_the_cards_once(self):
        ansi.set_enabled(False)
        client = make_client(seat=0)
        client.state = sample_state()
        straight = {"type": "played", "seat": 1, "cards": ["3s", "4h", "5d", "6c", "7s"],
                    "combo": "straight to 7 (3♠ 4♥ 5♦ 6♣ 7♠)", "kind": "straight",
                    "bomb": False, "out_of_turn": False}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            client.render_board()
            client.on_game_event(dict(DRAGON_PLAY, cards=["Ad"], combo="single A♦"))
            client.on_game_event(straight)
        text = out.getvalue()
        self.assertIn("│ single [A♦]          │", text)   # on the table
        self.assertIn("│ by Right · 0 pts     │", text)
        self.assertIn("  Right: single [A♦]", text)        # in the log
        self.assertIn("  Left: straight to 7 [3♠ 4♥ 5♦ 6♣ 7♠]", text)
        self.assertNotIn("single A♦ [", text)
        self.assertNotIn("single A♦]", text)

    def test_seating_puts_partner_across_and_opponents_beside(self):
        ansi.set_enabled(False)
        client = make_client(seat=0)
        client.state = sample_state()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            client.render_board()
        lines = out.getvalue().splitlines()
        first = next(i for i, l in enumerate(lines) if l.strip() == "Part")
        self.assertEqual(lines[first:first + 10], [
            "                       Part",
            "                       partner · 11 cards",
            "                       tichu!",
            "                    ┌──────────────────────┐",
            "    Left            │ single [A♦]          │   Right",
            "    next · 0 cards  │ by Right · 0 pts     │   prev · 14 cards",
            "    out#1           │                      │   offline",
            "                    └──────────────────────┘",
            "                          ▸ you",
            "                            11 cards",
        ])
        self.assertTrue(all(len(l) <= 62 for l in lines), [len(l) for l in lines])

    def test_seating_stays_aligned_in_color(self):
        ansi.set_enabled(True)
        try:
            client = make_client(seat=2)
            client.state = sample_state(turn=3, calls=["grand", None, None, None],
                                        connected=[False, True, True, True])
            lines = client.seating()
        finally:
            ansi.set_enabled(False)
        plain = [vis(l) for l in lines]
        self.assertTrue(all(len(l) <= 62 for l in plain), plain)
        # the box borders sit in the same columns on every row
        self.assertEqual({(l.find("│"), l.rfind("│")) for l in plain if "│" in l}, {(20, 43)})
        self.assertIn("                       Tester", plain)  # partner across
        self.assertIn("                       GRAND!", plain)
        self.assertIn("  ▸ Right           │ single [A♦]          │   Left", plain)
        self.assertIn("\x1b[31m", next(l for l in lines if "Right" in vis(l)))  # opponents red
        self.assertIn("\x1b[32m", next(l for l in lines if vis(l).strip() == "you"))  # you green

    def test_table_box_wraps_long_combinations(self):
        ansi.set_enabled(False)
        client = make_client(seat=0)
        cards = ["1", "2s", "3h", "4d", "5c", "6s", "7h", "8d", "9c", "10s", "Jh", "Qd", "Kc", "As"]
        client.state = sample_state(wish=13, top={
            "seat": 3, "cards": cards, "combo": "straight to A (…)", "kind": "straight",
            "power": 14, "size": 14, "bomb": False})
        box = [l[20:44] for l in client.seating() if "│" in l]
        self.assertEqual(box, [
            "│ straight to A        │",
            "│ [1 2♠ 3♥ 4♦ 5♣ 6♠ 7♥ │",
            "│ 8♦ 9♣ 10♠ J♥ Q♦ K♣   │",
            "│ A♠]                  │",
            "│ by Right · 0 pts     │",
            "│ wish: K              │",
        ])

    def test_bomb_and_dragon_flash_the_window(self):
        ansi.set_enabled(True)
        try:
            client = make_client(seat=0)
            client.state = sample_state()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                client.on_game_event(DRAGON_PLAY)
            text = out.getvalue()
            self.assertIn("\x1b]11;#2ea043\x07", text)  # the window goes green...
            self.assertIn("\x1b]111\x07", text)          # ...and back
            self.assertIn("\x1b[1;97;42m", text)         # plus a green bar in the log
            self.assertIn("DRAGON  played by Right", text)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                client.on_game_event(BOMB_PLAY)
            text = out.getvalue()
            self.assertIn("\x1b]11;#da3633\x07", text)
            self.assertIn("\x1b[1;97;41m", text)
            self.assertIn("BOMB  by Left — out of turn!", text)
            # --no-flash keeps the bar but leaves the window alone
            client.no_flash = True
            with contextlib.redirect_stdout(io.StringIO()) as out:
                client.on_game_event(BOMB_PLAY)
            self.assertNotIn("\x1b]11;", out.getvalue())
            self.assertIn("\x1b[1;97;41m", out.getvalue())
        finally:
            ansi.set_enabled(False)
        # without colors there is no flash, but the shout is still there
        with contextlib.redirect_stdout(io.StringIO()) as out:
            client.no_flash = False
            client.on_game_event(DRAGON_PLAY)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn("DRAGON  played by Right", out.getvalue())


class TestAnsi(unittest.TestCase):
    def test_nested_style_survives_the_inner_reset(self):
        ansi.set_enabled(True)
        try:
            s = ansi.dim("a " + ansi.green("b") + " c")
        finally:
            ansi.set_enabled(False)
        self.assertEqual(s, "\x1b[2ma \x1b[32mb\x1b[0m\x1b[2m c\x1b[0m")

    def test_disabled_means_plain_text(self):
        ansi.set_enabled(False)
        self.assertEqual(ansi.on_red("x"), "x")
        self.assertEqual(ansi.flash_on("red"), "")
        self.assertEqual(ansi.flash_off(), "")


if __name__ == "__main__":
    unittest.main()
