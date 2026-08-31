"""Card model for Tichu.

The deck has 56 cards: four suits (Sword, Star, Pagoda, Jade) with ranks
2..Ace, plus the four special cards Mah Jong (the "1"), Dog, Phoenix and
Dragon.

Internal rank encoding:
    0  = Dog          (never part of a combination, only led on its own)
    1  = Mah Jong     (lowest single, may start a straight: 1-2-3-4-5)
    2..14 = normal ranks (11=J, 12=Q, 13=K, 14=A)
    15 = Phoenix      (wildcard / half-step single)
    16 = Dragon       (highest single)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

DOG = 0
MAHJONG = 1
JACK, QUEEN, KING, ACE = 11, 12, 13, 14
PHOENIX = 15
DRAGON = 16

NORMAL_RANKS = range(2, 15)  # 2..A


class Suit(Enum):
    SWORD = "s"   # displayed ♠  (black)
    STAR = "h"    # displayed ♥  (red)
    PAGODA = "d"  # displayed ♦  (blue)
    JADE = "c"    # displayed ♣  (green)


SUIT_GLYPHS = {Suit.SWORD: "♠", Suit.STAR: "♥", Suit.PAGODA: "♦", Suit.JADE: "♣"}

_RANK_TO_STR = {10: "T", 11: "J", 12: "Q", 13: "K", 14: "A"}
_STR_TO_RANK = {v: k for k, v in _RANK_TO_STR.items()}

# Canonical codes for the special cards, plus accepted input aliases.
SPECIAL_CODES = {DOG: "Dog", MAHJONG: "1", PHOENIX: "Phx", DRAGON: "Drg"}
_SPECIAL_ALIASES = {
    "dog": DOG, "hound": DOG,
    "1": MAHJONG, "mah": MAHJONG, "mahjong": MAHJONG, "mj": MAHJONG, "bird": MAHJONG,
    "phx": PHOENIX, "ph": PHOENIX, "phoenix": PHOENIX, "p": PHOENIX,
    "drg": DRAGON, "dra": DRAGON, "dr": DRAGON, "dragon": DRAGON,
}


def rank_to_str(rank: int) -> str:
    if rank in SPECIAL_CODES:
        return SPECIAL_CODES[rank]
    return _RANK_TO_STR.get(rank, str(rank))


def parse_rank(token: str) -> Optional[int]:
    """Parse a rank token like '2'..'10', 'T', 'J', 'Q', 'K', 'A' (2..14 only)."""
    t = token.strip().upper()
    if t == "10":
        return 10
    if t in _STR_TO_RANK:
        return _STR_TO_RANK[t]
    if t.isdigit():
        r = int(t)
        if 2 <= r <= 10:
            return r
    return None


@dataclass(frozen=True, order=True)
class Card:
    rank: int
    suit: Optional[Suit] = None

    @property
    def is_special(self) -> bool:
        return self.suit is None

    @property
    def points(self) -> int:
        if self.rank == 5:
            return 5
        if self.rank in (10, KING):
            return 10
        if self.rank == DRAGON:
            return 25
        if self.rank == PHOENIX:
            return -25
        return 0

    @property
    def code(self) -> str:
        """Stable wire/input code, e.g. 'Ks', 'Th', '5c', 'Dog', 'Phx'."""
        if self.is_special:
            return SPECIAL_CODES[self.rank]
        return f"{rank_to_str(self.rank)}{self.suit.value}"

    def display(self) -> str:
        """Human-readable form with a suit glyph, e.g. 'K♠'."""
        if self.is_special:
            return SPECIAL_CODES[self.rank]
        return f"{rank_to_str(self.rank)}{SUIT_GLYPHS[self.suit]}"

    def __repr__(self) -> str:  # keeps test failures readable
        return self.code


_SUIT_BY_LETTER = {s.value: s for s in Suit}


def parse_card(token: str) -> Optional[Card]:
    """Parse one card token. Suits use the familiar letters s/h/d/c.

    Examples: 'Ks', 'th', '10d', '5C', 'dog', 'phx', 'drg', '1'.
    """
    t = token.strip().lower()
    if not t:
        return None
    if t in _SPECIAL_ALIASES:
        return Card(_SPECIAL_ALIASES[t])
    suit = _SUIT_BY_LETTER.get(t[-1])
    if suit is None:
        return None
    rank = parse_rank(t[:-1])
    if rank is None:
        return None
    return Card(rank, suit)


def parse_cards(tokens: Iterable[str]) -> Optional[list[Card]]:
    out = []
    for tok in tokens:
        c = parse_card(tok)
        if c is None:
            return None
        out.append(c)
    return out


def full_deck() -> list[Card]:
    deck = [Card(rank, suit) for suit in Suit for rank in NORMAL_RANKS]
    deck += [Card(DOG), Card(MAHJONG), Card(PHOENIX), Card(DRAGON)]
    assert len(deck) == 56
    return deck


def shuffled_deck(rng: Optional[random.Random] = None) -> list[Card]:
    deck = full_deck()
    (rng or random).shuffle(deck)
    return deck


_SORT_SUIT_ORDER = {Suit.SWORD: 0, Suit.STAR: 1, Suit.PAGODA: 2, Suit.JADE: 3, None: 4}


def sort_hand(cards: Iterable[Card]) -> list[Card]:
    """Dog first, then Mah Jong, ranks ascending, Phoenix, Dragon last."""
    return sorted(cards, key=lambda c: (c.rank, _SORT_SUIT_ORDER[c.suit]))


def cards_points(cards: Iterable[Card]) -> int:
    return sum(c.points for c in cards)
