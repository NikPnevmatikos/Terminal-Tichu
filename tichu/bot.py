"""A simple rules-abiding bot.

Not a strong player — it exists so a table can be filled to four, and so the
whole engine can be exercised end-to-end in tests. It always produces legal
actions (it draws from the same `legal_plays` enumeration the rules engine
uses for wish enforcement).
"""

from __future__ import annotations

import random
from typing import Optional

from .cards import ACE, Card, DOG, DRAGON, KING, MAHJONG, PHOENIX, sort_hand
from .combos import Combo, ComboKind, legal_plays, wish_plays
from .game import Phase, TichuGame, opponents


class Bot:
    def __init__(self, seat: int, rng: Optional[random.Random] = None):
        self.seat = seat
        self.rng = rng or random.Random()

    # -- individual decisions ------------------------------------------ #

    def wants_grand(self, first8: list[Card]) -> bool:
        ranks = [c.rank for c in first8]
        strength = (
            2 * (DRAGON in ranks)
            + 2 * (PHOENIX in ranks)
            + sum(1 for r in ranks if r == ACE)
            + 0.5 * sum(1 for r in ranks if r == KING)
        )
        return strength >= 4

    def wants_tichu(self, hand: list[Card]) -> bool:
        ranks = [c.rank for c in hand]
        counts: dict[int, int] = {}
        for r in ranks:
            if 2 <= r <= ACE:
                counts[r] = counts.get(r, 0) + 1
        has_bomb = any(v == 4 for v in counts.values())
        strength = (
            2 * (DRAGON in ranks)
            + 2 * (PHOENIX in ranks)
            + sum(1 for r in ranks if r == ACE)
            + 2 * has_bomb
        )
        return strength >= 6

    def choose_exchange(self, hand: list[Card]) -> list[Card]:
        """Returns [to next player, to partner, to previous player]."""
        plain = sort_hand([c for c in hand if not c.is_special])
        aces = [c for c in plain if c.rank == ACE]
        to_partner = aces[0] if len(aces) >= 2 else plain[2]
        rest = [c for c in plain if c is not to_partner]
        return [rest[0], to_partner, rest[1]]

    def choose_dragon_gift(self, game: TichuGame) -> int:
        a, b = opponents(self.seat)
        return a if len(game.hands[a]) >= len(game.hands[b]) else b

    def choose_play(self, game: TichuGame) -> Optional[Combo]:
        """A combo to play, or None to pass (never None when leading)."""
        hand = game.hands[self.seat]
        top = game.top
        plays = legal_plays(hand, top)
        if game.wish is not None:
            forced = [p for p in plays if p.contains_rank(game.wish)]
            if forced:
                return min(forced, key=lambda p: (p.is_bomb, p.power, p.size))
        if top is None:
            # Leading: shed the Dog immediately, otherwise the longest,
            # lowest combination; keep bombs and big singles for later.
            dog = next((p for p in plays if p.kind is ComboKind.DOG), None)
            if dog is not None:
                return dog
            normal = [
                p for p in plays
                if not p.is_bomb and p.kind is not ComboKind.DOG
                and not (p.kind is ComboKind.SINGLE and p.cards[0].rank in (DRAGON, PHOENIX))
            ]
            pool = normal or [p for p in plays if p.kind is not ComboKind.DOG]
            return max(pool, key=lambda p: (p.size, -p.power))
        # Following: cheapest non-bomb that beats; bomb only over a juicy
        # trick that an opponent currently owns.
        non_bombs = [p for p in plays if not p.is_bomb]
        if non_bombs:
            return min(non_bombs, key=lambda p: p.power)
        bombs = [p for p in plays if p.is_bomb]
        trick_points = game.view(self.seat)["trick_points"]
        opp_owns = game.last_play_seat in opponents(self.seat)
        if bombs and opp_owns and trick_points >= 10:
            return min(bombs, key=lambda p: (p.kind is ComboKind.STRAIGHTFLUSH, p.size, p.power))
        return None

    def choose_wish(self, hand_after: list[Card]) -> int:
        """Wish the highest rank the bot does not hold itself."""
        mine = {c.rank for c in hand_after}
        for r in range(ACE, 1, -1):
            if r not in mine:
                return r
        return ACE

    # -- one step of the game loop -------------------------------------- #

    def has_action(self, game: TichuGame) -> bool:
        """True when `act` would do something (no mutation)."""
        s = self.seat
        if game.phase is Phase.GRAND_TICHU:
            return not game.picked_up[s]
        if game.phase is Phase.EXCHANGE:
            return game.exchanges[s] is None
        if game.phase is Phase.DRAGON_GIFT:
            return game.dragon_chooser == s
        if game.phase is Phase.PLAYING:
            return game.turn == s
        return False

    def act(self, game: TichuGame) -> Optional[list[dict]]:
        """Perform this bot's next pending action, if any. Returns the
        emitted events, or None when it is not this bot's move."""
        s = self.seat
        if game.phase is Phase.GRAND_TICHU:
            if not game.picked_up[s]:
                return game.decide_grand(s, self.wants_grand(game.first8[s]))
            return None
        if game.phase is Phase.EXCHANGE:
            if game.exchanges[s] is None:
                events = []
                if game.calls[s] is None and self.wants_tichu(game.hands[s]):
                    events += game.call_tichu(s)
                events += game.submit_exchange(s, self.choose_exchange(game.hands[s]))
                return events
            return None
        if game.phase is Phase.DRAGON_GIFT:
            if game.dragon_chooser == s:
                return game.give_dragon(s, self.choose_dragon_gift(game))
            return None
        if game.phase is Phase.PLAYING and game.turn == s:
            events = []
            if (
                game.calls[s] is None
                and not game.played_any[s]
                and self.wants_tichu(game.hands[s])
            ):
                events += game.call_tichu(s)
            combo = self.choose_play(game)
            if combo is None:
                events += game.pass_turn(s)
                return events
            wish = None
            if combo.contains_rank(MAHJONG):
                remaining = [c for c in game.hands[s] if c not in combo.cards]
                wish = self.choose_wish(remaining)
            events += game.play(s, list(combo.cards), wish=wish, phoenix_as=combo.phoenix_as)
            return events
        return None


def run_bot_game(
    target: int = 1000, seed: Optional[int] = None, max_steps: int = 200_000
) -> TichuGame:
    """Play a complete bots-only game; used by the test-suite."""
    rng = random.Random(seed)
    game = TichuGame(["North", "East", "South", "West"], target=target, rng=rng)
    bots = [Bot(s, rng) for s in range(4)]
    game.drain_events()
    for _ in range(max_steps):
        if game.phase is Phase.GAME_OVER:
            return game
        acted = False
        for bot in bots:
            if bot.act(game) is not None:
                acted = True
                break
        if not acted:
            raise RuntimeError(f"stalled in phase {game.phase}")
    raise RuntimeError("game did not finish within step limit")
