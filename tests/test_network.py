"""End-to-end test over real sockets: a scripted client joins a table with
three server-side bots and plays a whole game through the wire protocol."""

import asyncio
import unittest

from tichu.cards import parse_card
from tichu.combos import interpretations, legal_plays
from tichu.protocol import encode, read_message
from tichu.server import Table


class ScriptedPlayer:
    """Plays legal moves straight off the state snapshots the server sends."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.seat = None
        self.last_sig = None
        self.game_over = None
        self.errors = []

    def send(self, obj):
        self.writer.write(encode(obj))

    def top_combo(self, st):
        top = st.get("top")
        if not top:
            return None
        cards = [parse_card(c) for c in top["cards"]]
        for interp in interpretations(cards):
            if interp.kind.value == top["kind"]:
                if interp.power is None:
                    from dataclasses import replace
                    return replace(interp, power=top["power"])
                if interp.power == top["power"]:
                    return interp
        return None

    def act_on(self, st):
        me = self.seat
        phase = st["phase"]
        sig = (
            phase, st["turn"], tuple(st.get("hand") or ()),
            tuple((st.get("top") or {}).get("cards", ())),
            st.get("wish"), st.get("exchange_submitted"),
            st["picked_up"][me], st.get("dragon_chooser"),
        )
        if sig == self.last_sig:
            return
        self.last_sig = sig
        if phase == "grand_tichu" and not st["picked_up"][me]:
            self.send({"cmd": "grand", "call": False})
        elif phase == "exchange" and not st.get("exchange_submitted"):
            self.send({"cmd": "exchange", "cards": st["hand"][:3]})
        elif phase == "dragon_gift" and st.get("dragon_chooser") == me:
            self.send({"cmd": "dragon", "to": (me + 1) % 4})
        elif phase == "playing" and st["turn"] == me:
            hand = [parse_card(c) for c in st["hand"]]
            top = self.top_combo(st)
            plays = legal_plays(hand, top)
            if st.get("wish") is not None:
                forced = [p for p in plays if p.contains_rank(st["wish"])]
                if forced:
                    plays = forced
            if not plays and top is not None:
                self.send({"cmd": "pass"})
                return
            combo = min(plays, key=lambda p: (p.is_bomb, p.power or 0, -p.size))
            msg = {"cmd": "play", "cards": [c.code for c in combo.cards]}
            if combo.phoenix_as is not None:
                msg["phoenix_as"] = str(combo.phoenix_as)
            if any(c.rank == 1 for c in combo.cards):
                msg["wish"] = "A"
            self.send(msg)

    async def run(self):
        self.send({"cmd": "hello", "name": "Tester"})
        while True:
            msg = await read_message(self.reader)
            if msg is None:
                return
            ev = msg.get("event")
            if ev == "welcome":
                self.seat = msg["seat"]
            elif ev == "error":
                self.errors.append(msg["msg"])
            elif ev == "state":
                st = msg["data"]
                if st.get("winner") is not None:
                    self.game_over = st
                    return
                if self.seat is not None:
                    self.act_on(st)
            elif ev == "game" and msg["data"].get("type") == "game_over":
                pass  # the final state snapshot ends the loop
            await self.writer.drain()


class TestNetworkGame(unittest.TestCase):
    def test_full_game_over_sockets(self):
        async def scenario():
            table = Table(target=200, bots=3, bot_delay=0, seed=99)
            server = await asyncio.start_server(table.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            player = ScriptedPlayer(reader, writer)
            try:
                await asyncio.wait_for(player.run(), timeout=90)
            finally:
                writer.close()
                server.close()
                await server.wait_closed()
            return player

        player = asyncio.run(scenario())
        self.assertIsNotNone(player.game_over, f"errors: {player.errors[:5]}")
        st = player.game_over
        self.assertIn(st["winner"], (0, 1))
        self.assertGreaterEqual(max(st["scores"]), 200)
        # a rules-abiding scripted player should never be rejected
        self.assertEqual(player.errors, [])


if __name__ == "__main__":
    unittest.main()
