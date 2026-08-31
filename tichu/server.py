"""The Tichu game server.

Authoritative asyncio TCP server for one table of four. Clients connect with
the terminal client (``python -m tichu.client``); empty seats can be filled
with bots (``--bots N``). Players get a reconnect token on join, so a dropped
connection can resume mid-hand with full state.

Run:  python -m tichu.server --host 0.0.0.0 --port 4271 [--bots 3]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import secrets
import socket
from dataclasses import dataclass, field
from typing import Optional

from .bot import Bot
from .cards import ACE, parse_cards, parse_rank
from .game import IllegalAction, Phase, TichuGame
from .protocol import DEFAULT_PORT, encode, read_message

BOT_NAMES = ["Bot-Ada", "Bot-Bo", "Bot-Cy", "Bot-Dot"]
BOT_SEAT_ORDER = [1, 3, 2, 0]  # so human partners end up opposite each other


@dataclass
class SeatState:
    name: str
    token: str
    bot: Optional[Bot] = None
    writer: Optional[asyncio.StreamWriter] = None
    rematch_vote: bool = False

    @property
    def connected(self) -> bool:
        return self.bot is not None or self.writer is not None


@dataclass
class Client:
    writer: asyncio.StreamWriter
    seat: Optional[int] = None  # None = spectator
    name: str = "?"


class Table:
    def __init__(self, target: int, bots: int, bot_delay: float, seed: Optional[int]):
        self.target = target
        self.bot_delay = bot_delay
        self.rng = random.Random(seed)
        self.seats: list[Optional[SeatState]] = [None] * 4
        self.clients: list[Client] = []
        self.game: Optional[TichuGame] = None
        self.lock = asyncio.Lock()
        self._bot_task: Optional[asyncio.Task] = None
        for i in range(max(0, min(4, bots))):
            seat = BOT_SEAT_ORDER[i]
            self.seats[seat] = SeatState(
                name=BOT_NAMES[i], token=secrets.token_hex(8), bot=Bot(seat, self.rng)
            )

    # ---------------------------------------------------------------- #
    # plumbing

    def _send(self, writer: asyncio.StreamWriter, obj: dict) -> None:
        if writer.is_closing():
            return
        try:
            writer.write(encode(obj))
        except (ConnectionError, RuntimeError):
            pass

    def _broadcast(self, obj: dict) -> None:
        for c in self.clients:
            self._send(c.writer, obj)

    def _seat_names(self) -> list[Optional[dict]]:
        out = []
        for s in self.seats:
            if s is None:
                out.append(None)
            else:
                out.append({"name": s.name, "connected": s.connected, "bot": s.bot is not None})
        return out

    def _send_lobby(self) -> None:
        self._broadcast({"event": "lobby", "seats": self._seat_names(),
                         "started": self.game is not None, "target": self.target})

    def _send_states(self) -> None:
        if self.game is None:
            return
        conn = [s is not None and s.connected for s in self.seats]
        for c in self.clients:
            view = self.game.view(c.seat)
            view["connected"] = conn
            self._send(c.writer, {"event": "state", "data": view})

    def _dispatch_events(self, events: list[dict]) -> None:
        for ev in events:
            target = ev.get("to")
            if target is None:
                self._broadcast({"event": "game", "data": ev})
            else:
                for c in self.clients:
                    if c.seat == target:
                        self._send(c.writer, {"event": "game", "data": ev})

    def _after_action(self, events: list[dict]) -> None:
        self._dispatch_events(events)
        self._send_states()
        self._kick_bots()

    # ---------------------------------------------------------------- #
    # game lifecycle

    def _maybe_start(self) -> None:
        if self.game is None and all(s is not None for s in self.seats):
            names = [s.name for s in self.seats]
            self.game = TichuGame(names, target=self.target, rng=self.rng)
            self._broadcast({"event": "started", "names": names, "target": self.target})
            self._after_action(self.game.drain_events())

    def _start_rematch(self) -> None:
        names = [s.name for s in self.seats]
        for s in self.seats:
            s.rematch_vote = False
        self.game = TichuGame(names, target=self.target, rng=self.rng)
        self._broadcast({"event": "started", "names": names, "target": self.target,
                         "rematch": True})
        self._after_action(self.game.drain_events())

    def _kick_bots(self) -> None:
        if self.game is None or self.game.phase is Phase.GAME_OVER:
            return
        if any(
            s and s.bot and s.bot.has_action(self.game) for s in self.seats
        ) and (self._bot_task is None or self._bot_task.done()):
            self._bot_task = asyncio.create_task(self._bot_pump())

    async def _bot_pump(self) -> None:
        while True:
            await asyncio.sleep(self.bot_delay)
            async with self.lock:
                if self.game is None or self.game.phase is Phase.GAME_OVER:
                    return
                actor = next(
                    (s.bot for s in self.seats if s and s.bot and s.bot.has_action(self.game)),
                    None,
                )
                if actor is None:
                    return
                try:
                    events = actor.act(self.game) or []
                except IllegalAction as exc:  # a bot bug; keep the table alive
                    events = [{"type": "chat", "seat": actor.seat,
                               "text": f"(bot error: {exc})"}]
                self._dispatch_events(events)
                self._send_states()

    # ---------------------------------------------------------------- #
    # client handling

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client: Optional[Client] = None
        try:
            hello = await asyncio.wait_for(read_message(reader), timeout=30)
            if not hello or hello.get("cmd") != "hello":
                self._send(writer, {"event": "error", "msg": "expected a hello"})
                return
            async with self.lock:
                client = self._admit(writer, hello)
            while True:
                msg = await read_message(reader)
                if msg is None:
                    break
                if not msg:
                    self._send(writer, {"event": "error", "msg": "bad message"})
                    continue
                async with self.lock:
                    self._handle_cmd(client, msg)
                try:
                    await writer.drain()
                except (ConnectionError, RuntimeError):
                    break
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            if client is not None:
                async with self.lock:
                    self._drop(client)
            try:
                writer.close()
            except RuntimeError:
                pass

    def _admit(self, writer: asyncio.StreamWriter, hello: dict) -> Client:
        name = str(hello.get("name") or "player").strip()[:16] or "player"
        token = hello.get("token")
        client = Client(writer=writer, name=name)
        self.clients.append(client)

        # resume a seat by token
        if isinstance(token, str):
            for i, s in enumerate(self.seats):
                if s is not None and s.bot is None and s.token == token:
                    if s.writer is not None:
                        self._send(s.writer, {"event": "error",
                                              "msg": "another connection took this seat"})
                        for c in self.clients:
                            if c.writer is s.writer:
                                c.seat = None
                    s.writer = writer
                    s.name = name if name != "player" else s.name
                    client.seat = i
                    client.name = s.name
                    self._send(writer, self._welcome(client, s))
                    self._broadcast({"event": "rejoined", "seat": i, "name": s.name})
                    self._send_lobby()
                    self._send_states()
                    return client

        # a fresh seat, if the game has not started and one is free
        if self.game is None and not hello.get("spectate"):
            free = [i for i, s in enumerate(self.seats) if s is None]
            if free:
                names = {s.name for s in self.seats if s}
                base, n = name, 2
                while name in names:
                    name = f"{base}{n}"
                    n += 1
                seat = free[0]
                state = SeatState(name=name, token=secrets.token_hex(8), writer=writer)
                self.seats[seat] = state
                client.seat = seat
                client.name = name
                self._send(writer, self._welcome(client, state))
                self._broadcast({"event": "joined", "seat": seat, "name": name})
                self._send_lobby()
                self._maybe_start()
                return client

        # otherwise: spectator
        self._send(writer, {"event": "welcome", "seat": None, "token": None,
                            "name": name, "target": self.target})
        self._send_lobby()
        if self.game is not None:
            self._send(writer, {"event": "state", "data": self.game.view(None)})
        return client

    def _welcome(self, client: Client, seat_state: SeatState) -> dict:
        return {
            "event": "welcome",
            "seat": client.seat,
            "token": seat_state.token,
            "name": seat_state.name,
            "target": self.target,
        }

    def _drop(self, client: Client) -> None:
        if client in self.clients:
            self.clients.remove(client)
        if client.seat is not None:
            s = self.seats[client.seat]
            if s is not None and s.writer is client.writer:
                s.writer = None
                if self.game is None:
                    self.seats[client.seat] = None  # free the seat in the lobby
                self._broadcast({"event": "left", "seat": client.seat, "name": s.name,
                                 "in_game": self.game is not None})
                self._send_lobby()

    # ---------------------------------------------------------------- #
    # commands

    def _handle_cmd(self, client: Client, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "ping":
            self._send(client.writer, {"event": "pong"})
            return
        if cmd == "chat":
            text = str(msg.get("text", ""))[:400]
            if text:
                self._broadcast({"event": "chat", "seat": client.seat,
                                 "name": client.name, "text": text})
            return
        if cmd == "who":
            self._send_lobby()
            return
        if cmd == "sit":
            self._cmd_sit(client, msg)
            return
        if cmd == "rematch":
            self._cmd_rematch(client)
            return
        if client.seat is None:
            self._send(client.writer, {"event": "error", "msg": "you are spectating"})
            return
        if self.game is None:
            self._send(client.writer, {"event": "error", "msg": "the game has not started"})
            return
        try:
            events = self._game_cmd(client.seat, cmd, msg)
        except IllegalAction as exc:
            self._send(client.writer, {"event": "error", "msg": str(exc)})
            return
        if events is None:
            self._send(client.writer, {"event": "error", "msg": f"unknown command: {cmd}"})
            return
        self._after_action(events)

    def _game_cmd(self, seat: int, cmd: str, msg: dict) -> Optional[list[dict]]:
        g = self.game
        if cmd == "grand":
            return g.decide_grand(seat, bool(msg.get("call")))
        if cmd == "tichu":
            return g.call_tichu(seat)
        if cmd == "exchange":
            cards = parse_cards(msg.get("cards") or [])
            if cards is None:
                raise IllegalAction("unrecognised card in exchange")
            return g.submit_exchange(seat, cards)
        if cmd == "play":
            cards = parse_cards(msg.get("cards") or [])
            if cards is None:
                raise IllegalAction("unrecognised card")
            wish = phoenix_as = None
            if msg.get("wish") is not None:
                wish = parse_rank(str(msg["wish"]))
                if wish is None:
                    raise IllegalAction("wish a rank from 2 to A")
            if msg.get("phoenix_as") is not None:
                phoenix_as = parse_rank(str(msg["phoenix_as"]))
                if phoenix_as is None:
                    raise IllegalAction("the Phoenix stands for a rank from 2 to A")
            return g.play(seat, cards, wish=wish, phoenix_as=phoenix_as)
        if cmd == "pass":
            return g.pass_turn(seat)
        if cmd == "dragon":
            to = msg.get("to")
            if not isinstance(to, int):
                raise IllegalAction("dragon needs a seat number")
            return g.give_dragon(seat, to % 4)
        return None

    def _cmd_sit(self, client: Client, msg: dict) -> None:
        if self.game is not None:
            self._send(client.writer, {"event": "error", "msg": "the game already started"})
            return
        seat = msg.get("seat")
        if not isinstance(seat, int) or not 0 <= seat <= 3 or self.seats[seat] is not None:
            self._send(client.writer, {"event": "error", "msg": "that seat is taken"})
            return
        if client.seat is None:
            self._send(client.writer, {"event": "error", "msg": "join a seat first"})
            return
        self.seats[seat] = self.seats[client.seat]
        self.seats[client.seat] = None
        client.seat = seat
        self._send(client.writer, {"event": "moved", "seat": seat})
        self._send_lobby()

    def _cmd_rematch(self, client: Client) -> None:
        if self.game is None or self.game.phase is not Phase.GAME_OVER:
            self._send(client.writer, {"event": "error", "msg": "no finished game to rematch"})
            return
        if client.seat is None:
            self._send(client.writer, {"event": "error", "msg": "you are spectating"})
            return
        self.seats[client.seat].rematch_vote = True
        waiting = [
            s.name
            for s in self.seats
            if s.bot is None and s.connected and not s.rematch_vote
        ]
        self._broadcast({"event": "rematch_vote", "name": client.name, "waiting": waiting})
        if not waiting:
            self._start_rematch()


async def _amain(args: argparse.Namespace) -> None:
    table = Table(args.target, args.bots, args.bot_delay, args.seed)
    server = await asyncio.start_server(table.handle, args.host, args.port)
    async with table.lock:
        table._maybe_start()  # a table of four bots starts on its own
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"Terminal Tichu server listening on {addrs}")
    print(f"  target score: {args.target}   bots: {args.bots}")
    try:
        hostname = socket.gethostname()
        lan = socket.gethostbyname(hostname)
        print(f"  players connect with:  python -m tichu.client --host {lan} --port {args.port} --name YOU")
    except OSError:
        pass
    async with server:
        await server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal Tichu server (one table)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--target", type=int, default=1000, help="score to win (default 1000)")
    ap.add_argument("--bots", type=int, default=0, help="fill this many seats with bots")
    ap.add_argument("--bot-delay", type=float, default=0.8, help="seconds between bot moves")
    ap.add_argument("--seed", type=int, default=None, help="deterministic shuffles (testing)")
    args = ap.parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nserver stopped")


if __name__ == "__main__":
    main()
