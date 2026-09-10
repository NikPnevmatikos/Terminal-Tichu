"""The Tichu rules state machine.

One `TichuGame` plays a whole match (hands until a team reaches the target
score). It is deliberately synchronous, deterministic and transport-free:
every player action is a method call that either raises `IllegalAction` or
mutates the state and returns a list of event dicts for the caller (the
server) to broadcast. Events carrying a "to" key are private to that seat.

Seats are 0..3 in play order; seats 0+2 form team 0, seats 1+3 team 1.

Hand lifecycle:
  GRAND_TICHU  - everyone sees their first 8 cards, calls grand tichu or takes
  EXCHANGE     - everyone passes one card to each other player
  PLAYING      - tricks; the Mah Jong holder leads the first one
  DRAGON_GIFT  - a trick won with the Dragon must be given to an opponent
                 who is still holding cards
  GAME_OVER    - a team reached the target with the higher score
"""

from __future__ import annotations

import random
from enum import Enum
from typing import Optional, Sequence

from .cards import (
    ACE,
    Card,
    DRAGON,
    MAHJONG,
    cards_points,
    rank_to_str,
    shuffled_deck,
    sort_hand,
)
from .combos import Combo, ComboKind, find_play, wish_plays

TEAM_OF = (0, 1, 0, 1)
GRAND_BONUS = 200
TICHU_BONUS = 100
DOUBLE_WIN_BONUS = 200


class Phase(Enum):
    GRAND_TICHU = "grand_tichu"
    EXCHANGE = "exchange"
    PLAYING = "playing"
    DRAGON_GIFT = "dragon_gift"
    GAME_OVER = "game_over"


class IllegalAction(Exception):
    pass


def partner(seat: int) -> int:
    return (seat + 2) % 4


def opponents(seat: int) -> tuple[int, int]:
    return ((seat + 1) % 4, (seat + 3) % 4)


class TichuGame:
    def __init__(
        self,
        names: Sequence[str],
        target: int = 1000,
        rng: Optional[random.Random] = None,
    ):
        assert len(names) == 4
        self.names = list(names)
        self.target = target
        self.rng = rng or random.Random()
        self.scores = [0, 0]
        self.hand_no = 0
        self.phase: Phase = Phase.GRAND_TICHU
        self.winner: Optional[int] = None
        self.last_hand_summary: Optional[dict] = None
        self._events: list[dict] = []
        self._deal_hand()

    # ------------------------------------------------------------------ #
    # dealing / per-hand state

    def _deal_hand(self) -> None:
        self.hand_no += 1
        deck = shuffled_deck(self.rng)
        self.hands: list[list[Card]] = [deck[i * 14:(i + 1) * 14] for i in range(4)]
        self.first8: list[list[Card]] = [h[:8] for h in self.hands]
        self.picked_up = [False] * 4
        self.calls: list[Optional[str]] = [None] * 4
        self.played_any = [False] * 4
        self.exchanges: list[Optional[tuple[Card, Card, Card]]] = [None] * 4
        self.received: list[list[tuple[int, Card]]] = [[] for _ in range(4)]
        self.out_order: list[int] = []
        self.piles: list[list[Card]] = [[] for _ in range(4)]
        self.trick_plays: list[tuple[int, Combo]] = []
        self.top: Optional[Combo] = None
        self.turn: Optional[int] = None
        self.leader: Optional[int] = None
        self.last_play_seat: Optional[int] = None
        self.wish: Optional[int] = None
        self.dragon_chooser: Optional[int] = None
        self._dragon_trick: list[Card] = []
        self.phase = Phase.GRAND_TICHU
        self._emit(type="hand_start", hand_no=self.hand_no, scores=list(self.scores))

    def _emit(self, **event) -> None:
        self._events.append(event)

    def _collect(self) -> list[dict]:
        ev, self._events = self._events, []
        return ev

    def drain_events(self) -> list[dict]:
        """Events emitted outside an action call (e.g. the initial deal)."""
        return self._collect()

    def _require(self, cond: bool, msg: str) -> None:
        if not cond:
            raise IllegalAction(msg)

    def _check_seat(self, seat: int) -> None:
        self._require(0 <= seat <= 3, "invalid seat")

    # ------------------------------------------------------------------ #
    # grand tichu

    def decide_grand(self, seat: int, call: bool) -> list[dict]:
        self._check_seat(seat)
        self._require(self.phase is Phase.GRAND_TICHU, "not the grand tichu phase")
        self._require(not self.picked_up[seat], "you already picked up your cards")
        self.picked_up[seat] = True
        if call:
            self.calls[seat] = "grand"
            self._emit(type="called", seat=seat, call="grand")
        else:
            self._emit(type="took_cards", seat=seat)
        if all(self.picked_up):
            self.phase = Phase.EXCHANGE
            self._emit(type="exchange_phase")
        return self._collect()

    # ------------------------------------------------------------------ #
    # tichu (the small one)

    def call_tichu(self, seat: int) -> list[dict]:
        self._check_seat(seat)
        self._require(
            self.phase in (Phase.GRAND_TICHU, Phase.EXCHANGE, Phase.PLAYING, Phase.DRAGON_GIFT),
            "cannot call tichu now",
        )
        self._require(self.picked_up[seat], "look at all 14 cards first")
        self._require(self.calls[seat] is None, "you already called")
        self._require(not self.played_any[seat], "you already played a card this hand")
        self.calls[seat] = "tichu"
        self._emit(type="called", seat=seat, call="tichu")
        return self._collect()

    # ------------------------------------------------------------------ #
    # exchange

    def submit_exchange(self, seat: int, cards: Sequence[Card]) -> list[dict]:
        self._check_seat(seat)
        self._require(self.phase is Phase.EXCHANGE, "not the exchange phase")
        self._require(self.exchanges[seat] is None, "you already passed your cards")
        self._require(len(cards) == 3 and len(set(cards)) == 3, "pass exactly three different cards")
        hand = self.hands[seat]
        for c in cards:
            self._require(c in hand, f"{c.display()} is not in your hand")
        for c in cards:
            hand.remove(c)
        self.exchanges[seat] = (cards[0], cards[1], cards[2])
        self._emit(type="exchanged", seat=seat)
        if all(e is not None for e in self.exchanges):
            self._distribute_exchange()
        return self._collect()

    def _distribute_exchange(self) -> None:
        for s in range(4):
            to_next, to_partner, to_prev = self.exchanges[s]
            self.hands[(s + 1) % 4].append(to_next)
            self.received[(s + 1) % 4].append((s, to_next))
            self.hands[(s + 2) % 4].append(to_partner)
            self.received[(s + 2) % 4].append((s, to_partner))
            self.hands[(s + 3) % 4].append(to_prev)
            self.received[(s + 3) % 4].append((s, to_prev))
        self._emit(type="exchange_done")
        for s in range(4):
            self._emit(
                type="received",
                to=s,
                gifts=[{"from": frm, "card": c.code} for frm, c in self.received[s]],
            )
        leader = next(s for s in range(4) if any(c.rank == MAHJONG for c in self.hands[s]))
        self.phase = Phase.PLAYING
        self.leader = leader
        self.turn = leader
        self._emit(type="play_phase", leader=leader)
        self._emit(type="turn", seat=leader, leading=True)

    # ------------------------------------------------------------------ #
    # playing

    def _active(self, seat: int) -> bool:
        return len(self.hands[seat]) > 0

    def _next_active(self, seat: int) -> int:
        s = (seat + 1) % 4
        while not self._active(s):
            s = (s + 1) % 4
        return s

    def play(
        self,
        seat: int,
        cards: Sequence[Card],
        wish: Optional[int] = None,
        phoenix_as: Optional[int] = None,
    ) -> list[dict]:
        self._check_seat(seat)
        self._require(self.phase is Phase.PLAYING, "you cannot play now")
        hand = self.hands[seat]
        self._require(len(cards) > 0, "play at least one card")
        self._require(len(set(cards)) == len(cards), "duplicate card in play")
        for c in cards:
            self._require(c in hand, f"{c.display()} is not in your hand")

        in_turn = seat == self.turn
        combo, reason = find_play(cards, self.top, phoenix_as)
        self._require(combo is not None, reason or "invalid play")

        if wish is not None:
            self._require(
                any(c.rank == MAHJONG for c in cards),
                "a wish requires playing the Mah Jong",
            )
            self._require(2 <= wish <= ACE, "wish a rank from 2 to A")

        if not in_turn:
            # Out of turn only a bomb may interject, and only on a live trick.
            self._require(combo.is_bomb, "not your turn (only a bomb may interject)")
            self._require(self.top is not None, "you can only bomb a live trick")
        else:
            if combo.kind is ComboKind.DOG:
                self._require(self.top is None, "the Dog can only be led")
            # The Mah Jong wish must be honoured when possible.
            if self.wish is not None and not combo.contains_rank(self.wish):
                if wish_plays(hand, self.top, self.wish):
                    raise IllegalAction(
                        f"you must play the wished rank ({rank_to_str(self.wish)})"
                    )

        # ---- the Dog ----
        if combo.kind is ComboKind.DOG:
            hand.remove(cards[0])
            self.played_any[seat] = True
            self.piles[seat].append(cards[0])
            target = partner(seat)
            if self.hands[target]:
                new_leader = target
            elif self._someone_active():
                new_leader = self._next_active(target)
            else:
                new_leader = None
            self._emit(type="played", seat=seat, cards=[cards[0].code],
                       combo="the Dog", kind="dog", bomb=False, out_of_turn=False)
            if self._check_out(seat):
                return self._collect()
            if len(self.out_order) >= 3:
                self._score_hand(double_win=False)
                return self._collect()
            self.leader = new_leader
            self.turn = new_leader
            self._emit(type="dog", seat=seat, to=new_leader)
            self._emit(type="turn", seat=new_leader, leading=True)
            return self._collect()

        # ---- a normal play or bomb ----
        for c in cards:
            hand.remove(c)
        self.played_any[seat] = True
        self.trick_plays.append((seat, combo))
        self.top = combo
        self.last_play_seat = seat
        self._emit(
            type="played",
            seat=seat,
            cards=[c.code for c in sort_hand(cards)],
            combo=combo.describe(),
            kind=combo.kind.value,
            bomb=combo.is_bomb,
            out_of_turn=not in_turn,
        )

        if combo.contains_rank(MAHJONG) and wish is not None:
            self.wish = wish
            self._emit(type="wish_set", seat=seat, rank=wish)
        if self.wish is not None and combo.contains_rank(self.wish):
            self.wish = None
            self._emit(type="wish_fulfilled", seat=seat)

        if self._check_out(seat):
            return self._collect()
        self._advance_from(seat)
        return self._collect()

    def pass_turn(self, seat: int) -> list[dict]:
        self._check_seat(seat)
        self._require(self.phase is Phase.PLAYING, "you cannot pass now")
        self._require(seat == self.turn, "not your turn")
        self._require(self.top is not None, "the trick leader must play")
        if self.wish is not None and wish_plays(self.hands[seat], self.top, self.wish):
            raise IllegalAction(f"you must play the wished rank ({rank_to_str(self.wish)})")
        self._emit(type="passed", seat=seat)
        self._advance_from(seat)
        return self._collect()

    def _someone_active(self) -> bool:
        return any(self._active(s) for s in range(4))

    def _check_out(self, seat: int) -> bool:
        """Handle a possibly-emptied hand. Returns True if the hand ended
        immediately (double win) and no further advancement should occur."""
        if self.hands[seat]:
            return False
        self.out_order.append(seat)
        self._emit(type="went_out", seat=seat, place=len(self.out_order))
        if len(self.out_order) == 2 and TEAM_OF[self.out_order[0]] == TEAM_OF[self.out_order[1]]:
            self._score_hand(double_win=True)
            return True
        return False

    def _advance_from(self, actor: int) -> None:
        cand = (actor + 1) % 4
        while True:
            if cand == self.last_play_seat:
                self._close_trick()
                return
            if self._active(cand):
                self.turn = cand
                self._emit(type="turn", seat=cand, leading=False)
                return
            cand = (cand + 1) % 4

    def _close_trick(self) -> None:
        winner = self.last_play_seat
        cards = [c for _, combo in self.trick_plays for c in combo.cards]
        points = cards_points(cards)
        dragon_won = self.top is not None and self.top.contains_rank(DRAGON)
        self.trick_plays = []
        self.top = None
        self.last_play_seat = None
        if dragon_won:
            self.phase = Phase.DRAGON_GIFT
            self.dragon_chooser = winner
            self._dragon_trick = cards
            self.turn = None
            self._emit(type="dragon_pending", seat=winner, points=points)
            return
        self.piles[winner].extend(cards)
        self._emit(type="trick_won", seat=winner, points=points, cards=len(cards))
        self._after_trick(winner)

    def dragon_targets(self, seat: int) -> tuple[int, ...]:
        """Opponents a Dragon trick may be handed to: those still in the hand.

        Giving it to someone who is already out would bury the points -- their
        pile is swept to the first player out (or dropped entirely on a double
        win), so the trick would never reach the team that is meant to eat it.
        Both opponents being out cannot happen (they are teammates, so that is
        a double win and the hand is over); the fallback only keeps a corrupt
        state from wedging the game.
        """
        live = tuple(s for s in opponents(seat) if self._active(s))
        return live or opponents(seat)

    def give_dragon(self, seat: int, to_seat: int) -> list[dict]:
        self._check_seat(seat)
        self._require(self.phase is Phase.DRAGON_GIFT, "there is no Dragon trick to give")
        self._require(seat == self.dragon_chooser, "you did not win the Dragon trick")
        self._require(to_seat in opponents(seat), "the Dragon trick goes to an opponent")
        self._require(
            to_seat in self.dragon_targets(seat),
            f"{self.names[to_seat]} is already out - "
            "the Dragon trick goes to an opponent who still has cards",
        )
        cards = self._dragon_trick
        self._dragon_trick = []
        self.dragon_chooser = None
        self.phase = Phase.PLAYING
        self.piles[to_seat].extend(cards)
        self._emit(type="dragon_given", seat=seat, to=to_seat, points=cards_points(cards))
        self._after_trick(seat)
        return self._collect()

    def _after_trick(self, winner: int) -> None:
        if len(self.out_order) >= 3:
            self._score_hand(double_win=False)
            return
        leader = winner if self._active(winner) else self._next_active(winner)
        self.leader = leader
        self.turn = leader
        self._emit(type="turn", seat=leader, leading=True)

    # ------------------------------------------------------------------ #
    # scoring

    def _score_hand(self, double_win: bool) -> None:
        first = self.out_order[0]
        team_points = [0, 0]
        if double_win:
            team_points[TEAM_OF[first]] += DOUBLE_WIN_BONUS
        else:
            # Normally one player is left holding cards; if the 4th player
            # shed their last cards on the final trick they count as "last"
            # with an empty hand.
            remaining = [s for s in range(4) if s not in self.out_order]
            last = remaining[0] if remaining else self.out_order[3]
            # The last player's tricks go to whoever went out first...
            self.piles[first].extend(self.piles[last])
            self.piles[last] = []
            # ...and their remaining hand cards to the opposing team.
            hand_points = cards_points(self.hands[last])
            team_points[1 - TEAM_OF[last]] += hand_points
            for s in range(4):
                team_points[TEAM_OF[s]] += cards_points(self.piles[s])
        bonuses = []
        for s in range(4):
            if self.calls[s] is None:
                continue
            value = GRAND_BONUS if self.calls[s] == "grand" else TICHU_BONUS
            delta = value if s == first else -value
            team_points[TEAM_OF[s]] += delta
            bonuses.append({"seat": s, "call": self.calls[s], "delta": delta})
        self.scores[0] += team_points[0]
        self.scores[1] += team_points[1]
        self.last_hand_summary = {
            "hand_no": self.hand_no,
            "double_win": double_win,
            "first_out": first,
            "team_points": team_points,
            "bonuses": bonuses,
            "scores": list(self.scores),
        }
        self._emit(type="hand_end", **self.last_hand_summary)
        if max(self.scores) >= self.target and self.scores[0] != self.scores[1]:
            self.winner = 0 if self.scores[0] > self.scores[1] else 1
            self.phase = Phase.GAME_OVER
            self.turn = None
            self._emit(type="game_over", winner=self.winner, scores=list(self.scores))
        else:
            self._deal_hand()

    # ------------------------------------------------------------------ #
    # views

    def view(self, seat: Optional[int]) -> dict:
        """Redacted state snapshot for one seat (None = spectator)."""
        def hand_codes(s: int) -> list[str]:
            cards = self.hands[s] if self.picked_up[s] else self.first8[s]
            return [c.code for c in sort_hand(cards)]

        def visible_count(s: int) -> int:
            if not self.picked_up[s]:
                return 8
            n = len(self.hands[s])
            if self.phase is Phase.EXCHANGE and self.exchanges[s] is not None:
                n += 3  # gifted cards are in escrow until everyone has passed
            return n

        v = {
            "phase": self.phase.value,
            "hand_no": self.hand_no,
            "target": self.target,
            "scores": list(self.scores),
            "names": list(self.names),
            "you": seat,
            "hand_counts": [visible_count(s) for s in range(4)],
            "picked_up": list(self.picked_up),
            "calls": list(self.calls),
            "exchanged": [e is not None for e in self.exchanges],
            "turn": self.turn,
            "leader": self.leader,
            "wish": self.wish,
            "out_order": list(self.out_order),
            "trick": [
                {
                    "seat": s,
                    "cards": [c.code for c in sort_hand(combo.cards)],
                    "combo": combo.describe(),
                    "bomb": combo.is_bomb,
                }
                for s, combo in self.trick_plays
            ],
            "top": (
                {
                    "seat": self.last_play_seat,
                    "cards": [c.code for c in sort_hand(self.top.cards)],
                    "combo": self.top.describe(),
                    "kind": self.top.kind.value,
                    "power": self.top.power,
                    "size": self.top.size,
                    "bomb": self.top.is_bomb,
                }
                if self.top is not None
                else None
            ),
            "trick_points": cards_points(
                [c for _, combo in self.trick_plays for c in combo.cards]
            ),
            "pile_points": [cards_points(p) for p in self.piles],
            "dragon_chooser": self.dragon_chooser,
            "dragon_targets": (
                list(self.dragon_targets(self.dragon_chooser))
                if self.dragon_chooser is not None
                else []
            ),
            "last_hand": self.last_hand_summary,
            "winner": self.winner,
        }
        if seat is not None:
            v["hand"] = hand_codes(seat)
            v["received"] = [
                {"from": frm, "card": c.code} for frm, c in self.received[seat]
            ]
            v["exchange_submitted"] = self.exchanges[seat] is not None
        return v
