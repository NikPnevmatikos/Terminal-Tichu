"""Combination identification, comparison and legal-move enumeration.

Combination kinds and their ordering rules follow the official Tichu rules:

* single, pair, triple, full house, straight (>=5), consecutive pairs (>=2 pairs)
* bombs: four of a kind, straight flush (>=5, one suit). Bombs beat every
  non-bomb; any straight flush beats any four of a kind; longer straight
  flushes beat shorter ones; otherwise bombs compare by rank/top card.
* The Phoenix substitutes for any normal card (2..A) in combinations, never
  in bombs. As a single it is worth half a step more than the card it is
  played on (1.5 when led) and never beats the Dragon.
* The Mah Jong (1) may only be played as a single or as the bottom of a
  straight; the Dog only ever on an empty trick; the Dragon only as a single.

A same-suit run of 5+ real cards is always a straight-flush *bomb* — it is
never interpreted as a plain straight (standard digital-play behaviour).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Optional, Sequence

from .cards import ACE, Card, DOG, DRAGON, MAHJONG, PHOENIX, rank_to_str, sort_hand

DRAGON_POWER = 20.0  # above every possible single (Phoenix on an Ace is 14.5)
PHOENIX_LEAD_POWER = 1.5


class ComboKind(Enum):
    DOG = "dog"
    SINGLE = "single"
    PAIR = "pair"
    TRIPLE = "triple"
    FULLHOUSE = "full house"
    STRAIGHT = "straight"
    PAIRSEQ = "consecutive pairs"
    BOMB4 = "bomb"
    STRAIGHTFLUSH = "straight flush bomb"


BOMB_KINDS = (ComboKind.BOMB4, ComboKind.STRAIGHTFLUSH)


@dataclass(frozen=True)
class Combo:
    kind: ComboKind
    cards: tuple[Card, ...]
    power: Optional[float]  # None only for an unresolved single Phoenix
    phoenix_as: Optional[int] = None  # rank the Phoenix stands in for

    @property
    def size(self) -> int:
        return len(self.cards)

    @property
    def is_bomb(self) -> bool:
        return self.kind in BOMB_KINDS

    def contains_rank(self, rank: int) -> bool:
        """True if a *real* card of this rank is part of the combo (the
        Phoenix standing in for a rank does not count, e.g. for wishes)."""
        return any(c.rank == rank for c in self.cards)

    def describe(self) -> str:
        cards = " ".join(c.display() for c in sort_hand(self.cards))
        if self.kind is ComboKind.DOG:
            return "the Dog"
        if self.kind is ComboKind.SINGLE:
            return f"single {cards}"
        if self.kind in (ComboKind.PAIR, ComboKind.TRIPLE, ComboKind.BOMB4):
            return f"{self.kind.value} ({cards})"
        if self.kind is ComboKind.FULLHOUSE:
            return f"full house on {rank_to_str(int(self.power))} ({cards})"
        return f"{self.kind.value} to {rank_to_str(int(self.power))} ({cards})"


def _ranks(cards: Sequence[Card]) -> list[int]:
    return sorted(c.rank for c in cards)


def _rank_counts(cards: Sequence[Card]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for c in cards:
        counts[c.rank] = counts.get(c.rank, 0) + 1
    return counts


def interpretations(cards: Sequence[Card]) -> list[Combo]:
    """All valid combination readings of this exact set of cards.

    Multiple readings exist only when the Phoenix is involved (e.g. it can
    extend a straight at either end, or complete either pair of a full
    house). A lone Phoenix yields a SINGLE with power=None which must be
    resolved against the trick via `finalize`.
    """
    cards = tuple(sort_hand(cards))
    n = len(cards)
    if n == 0 or len(set(cards)) != n:
        return []

    has_dog = any(c.rank == DOG for c in cards)
    has_dragon = any(c.rank == DRAGON for c in cards)
    has_phx = any(c.rank == PHOENIX for c in cards)
    has_mah = any(c.rank == MAHJONG for c in cards)
    normals = [c for c in cards if c.rank != PHOENIX]

    if has_dog:
        return [Combo(ComboKind.DOG, cards, 0.0)] if n == 1 else []

    if n == 1:
        c = cards[0]
        if c.rank == DRAGON:
            return [Combo(ComboKind.SINGLE, cards, DRAGON_POWER)]
        if c.rank == PHOENIX:
            return [Combo(ComboKind.SINGLE, cards, None)]
        return [Combo(ComboKind.SINGLE, cards, float(c.rank))]

    if has_dragon:
        return []  # the Dragon is only ever a single

    out: list[Combo] = []
    counts = _rank_counts(normals)
    norm_ranks = sorted(counts)

    # --- pair / triple -------------------------------------------------
    if n in (2, 3) and not has_mah:
        need = n if not has_phx else n - 1
        if len(norm_ranks) == 1 and counts[norm_ranks[0]] == need:
            r = norm_ranks[0]
            kind = ComboKind.PAIR if n == 2 else ComboKind.TRIPLE
            out.append(Combo(kind, cards, float(r), phoenix_as=r if has_phx else None))

    # --- four-of-a-kind bomb -------------------------------------------
    if n == 4 and not has_phx and not has_mah:
        if len(norm_ranks) == 1 and counts[norm_ranks[0]] == 4:
            out.append(Combo(ComboKind.BOMB4, cards, float(norm_ranks[0])))

    # --- full house ------------------------------------------------------
    if n == 5 and not has_mah:
        if not has_phx:
            if len(norm_ranks) == 2 and sorted(counts.values()) == [2, 3]:
                triple = norm_ranks[0] if counts[norm_ranks[0]] == 3 else norm_ranks[1]
                out.append(Combo(ComboKind.FULLHOUSE, cards, float(triple)))
        else:
            if len(norm_ranks) == 2:
                a, b = norm_ranks
                if sorted(counts.values()) == [1, 3]:
                    # Phoenix completes the pair; the existing triple rules.
                    triple = a if counts[a] == 3 else b
                    single = b if triple == a else a
                    out.append(Combo(ComboKind.FULLHOUSE, cards, float(triple), phoenix_as=single))
                elif counts[a] == 2 and counts[b] == 2:
                    # Phoenix joins either pair to form the triple.
                    out.append(Combo(ComboKind.FULLHOUSE, cards, float(a), phoenix_as=a))
                    out.append(Combo(ComboKind.FULLHOUSE, cards, float(b), phoenix_as=b))

    # --- straights (and straight-flush bombs) ----------------------------
    if n >= 5 and all(v == 1 for v in counts.values()):
        if not has_phx:
            if norm_ranks[-1] - norm_ranks[0] == n - 1 and norm_ranks[-1] <= ACE:
                suits = {c.suit for c in normals}
                if len(suits) == 1 and not has_mah:
                    out.append(Combo(ComboKind.STRAIGHTFLUSH, cards, float(norm_ranks[-1])))
                else:
                    out.append(Combo(ComboKind.STRAIGHT, cards, float(norm_ranks[-1])))
        else:
            # Phoenix stands in for exactly one missing rank (2..A, never 1).
            span_lo = max(2, norm_ranks[-1] - (n - 1))
            span_hi = min(ACE, norm_ranks[0] + (n - 1))
            for fill in range(span_lo, span_hi + 1):
                if fill in counts:
                    continue
                rs = sorted(norm_ranks + [fill])
                if rs[-1] - rs[0] == n - 1 and rs[-1] <= ACE and rs[0] >= MAHJONG:
                    out.append(
                        Combo(ComboKind.STRAIGHT, cards, float(rs[-1]), phoenix_as=fill)
                    )

    # --- consecutive pairs ------------------------------------------------
    if n >= 4 and n % 2 == 0 and not has_mah:
        if not has_phx:
            if all(v == 2 for v in counts.values()) and norm_ranks[-1] - norm_ranks[0] == n // 2 - 1:
                out.append(Combo(ComboKind.PAIRSEQ, cards, float(norm_ranks[-1])))
        else:
            singles = [r for r, v in counts.items() if v == 1]
            if len(singles) == 1 and all(v in (1, 2) for v in counts.values()):
                if norm_ranks[-1] - norm_ranks[0] == n // 2 - 1 and norm_ranks[0] >= 2:
                    out.append(
                        Combo(
                            ComboKind.PAIRSEQ, cards, float(norm_ranks[-1]),
                            phoenix_as=singles[0],
                        )
                    )

    return out


def finalize(combo: Combo, top: Optional[Combo]) -> Optional[Combo]:
    """Resolve context-dependent power (the lone Phoenix). Returns None when
    the combo cannot exist in this context (Phoenix single on the Dragon)."""
    if combo.power is not None:
        return combo
    # Lone Phoenix.
    if top is None:
        return replace(combo, power=PHOENIX_LEAD_POWER)
    if top.kind is not ComboKind.SINGLE or top.is_bomb:
        return None
    if top.contains_rank(DRAGON):
        return None
    return replace(combo, power=top.power + 0.5)


def _bomb_order(c: Combo) -> tuple:
    if c.kind is ComboKind.BOMB4:
        return (0, 0, c.power)
    return (1, c.size, c.power)


def beats(candidate: Combo, top: Combo) -> bool:
    """True if `candidate` may be played on `top`. `candidate` must be
    finalized (no unresolved Phoenix power)."""
    if candidate.power is None:
        raise ValueError("candidate combo not finalized")
    if candidate.kind is ComboKind.DOG or top.kind is ComboKind.DOG:
        return False
    if candidate.is_bomb:
        if top.is_bomb:
            return _bomb_order(candidate) > _bomb_order(top)
        return True
    if top.is_bomb:
        return False
    if candidate.kind is not top.kind or candidate.size != top.size:
        return False
    if top.kind is ComboKind.SINGLE and top.contains_rank(DRAGON):
        return False  # only bombs beat the Dragon
    return candidate.power > top.power


def find_play(
    cards: Sequence[Card],
    top: Optional[Combo],
    phoenix_hint: Optional[int] = None,
) -> tuple[Optional[Combo], str]:
    """Validate an explicit play of `cards` against the current trick top.

    Returns (combo, "") on success or (None, reason). When the Phoenix makes
    several readings possible, `phoenix_hint` (the rank the Phoenix should
    stand for) selects one; otherwise the strongest legal reading is chosen.
    """
    interps = interpretations(cards)
    if not interps:
        return None, "those cards do not form a valid combination"
    if phoenix_hint is not None:
        hinted = [c for c in interps if c.phoenix_as == phoenix_hint]
        if not hinted:
            return None, f"the Phoenix cannot stand for {rank_to_str(phoenix_hint)} here"
        interps = hinted
    finalized = [f for c in interps if (f := finalize(c, top)) is not None]
    if not finalized:
        if top is not None and top.contains_rank(DRAGON):
            return None, "the Phoenix cannot beat the Dragon"
        return None, "a lone Phoenix can only be played on a single card"
    if top is None:
        legal = finalized
    else:
        legal = [c for c in finalized if beats(c, top)]
        if not legal:
            reason = f"that does not beat {top.describe()}"
            return None, reason
    return max(legal, key=lambda c: (c.is_bomb, c.power)), ""


def _hand_combos(hand: Sequence[Card]) -> list[Combo]:
    """Every combination the hand can form, one representative per distinct
    rank pattern (choices between equal-ranked suits are collapsed, which is
    all that matters for legality, wish checks and the bots)."""
    by_rank: dict[int, list[Card]] = {}
    mah = dog = phx = drg = None
    for c in hand:
        if c.rank == MAHJONG:
            mah = c
        elif c.rank == DOG:
            dog = c
        elif c.rank == PHOENIX:
            phx = c
        elif c.rank == DRAGON:
            drg = c
        else:
            by_rank.setdefault(c.rank, []).append(c)
    out: list[Combo] = []

    # singles
    if dog is not None:
        out.append(Combo(ComboKind.DOG, (dog,), 0.0))
    if mah is not None:
        out.append(Combo(ComboKind.SINGLE, (mah,), 1.0))
    if drg is not None:
        out.append(Combo(ComboKind.SINGLE, (drg,), DRAGON_POWER))
    if phx is not None:
        out.append(Combo(ComboKind.SINGLE, (phx,), None))
    for r, cs in by_rank.items():
        out.append(Combo(ComboKind.SINGLE, (cs[0],), float(r)))
        # pairs / triples / four-bombs
        if len(cs) >= 2:
            out.append(Combo(ComboKind.PAIR, tuple(cs[:2]), float(r)))
        if len(cs) >= 3:
            out.append(Combo(ComboKind.TRIPLE, tuple(cs[:3]), float(r)))
        if len(cs) == 4:
            out.append(Combo(ComboKind.BOMB4, tuple(cs), float(r)))
        if phx is not None:
            out.append(Combo(ComboKind.PAIR, (cs[0], phx), float(r), phoenix_as=r))
            if len(cs) >= 2:
                out.append(Combo(ComboKind.TRIPLE, (cs[0], cs[1], phx), float(r), phoenix_as=r))

    # full houses
    for a, ca in by_rank.items():
        for b, cb in by_rank.items():
            if a == b:
                continue
            if len(ca) >= 3 and len(cb) >= 2:
                out.append(Combo(ComboKind.FULLHOUSE, tuple(ca[:3] + cb[:2]), float(a)))
            if phx is not None:
                if len(ca) >= 3 and len(cb) >= 1:
                    out.append(
                        Combo(ComboKind.FULLHOUSE, tuple(ca[:3] + [cb[0], phx]),
                              float(a), phoenix_as=b)
                    )
                if len(ca) >= 2 and len(cb) >= 2:
                    out.append(
                        Combo(ComboKind.FULLHOUSE, tuple(ca[:2] + [phx] + cb[:2]),
                              float(a), phoenix_as=a)
                    )

    # straights (plain; same-suit runs are emitted as bombs below instead)
    present = set(by_rank)
    if mah is not None:
        present.add(MAHJONG)

    def pick(r: int) -> Card:
        return mah if r == MAHJONG else by_rank[r][0]

    for lo in range(MAHJONG, ACE + 1):
        for hi in range(lo + 4, ACE + 1):
            window = range(lo, hi + 1)
            missing = [r for r in window if r not in present]
            if not missing:
                cards = [pick(r) for r in window]
                suits = {c.suit for c in cards}
                if len(suits) == 1:
                    # try to de-flush by swapping in an alternate suit
                    for r in window:
                        if r != MAHJONG and len(by_rank[r]) > 1:
                            cards[r - lo] = by_rank[r][1]
                            break
                    else:
                        continue  # only exists as a straight-flush bomb
                out.append(Combo(ComboKind.STRAIGHT, tuple(cards), float(hi)))
            elif len(missing) == 1 and phx is not None and missing[0] >= 2:
                cards = [pick(r) for r in window if r != missing[0]] + [phx]
                out.append(
                    Combo(ComboKind.STRAIGHT, tuple(cards), float(hi), phoenix_as=missing[0])
                )

    # consecutive pairs
    max_len = len(hand) // 2
    for lo in range(2, ACE + 1):
        for k in range(2, max_len + 1):
            hi = lo + k - 1
            if hi > ACE:
                break
            window = range(lo, hi + 1)
            short = [r for r in window if len(by_rank.get(r, ())) < 2]
            if not short:
                cards = [c for r in window for c in by_rank[r][:2]]
                out.append(Combo(ComboKind.PAIRSEQ, tuple(cards), float(hi)))
            elif (
                phx is not None
                and len(short) == 1
                and len(by_rank.get(short[0], ())) == 1
            ):
                cards = [c for r in window for c in by_rank[r][:2] if r != short[0]]
                cards += [by_rank[short[0]][0], phx]
                out.append(
                    Combo(ComboKind.PAIRSEQ, tuple(cards), float(hi), phoenix_as=short[0])
                )

    # straight-flush bombs
    by_suit: dict[object, dict[int, Card]] = {}
    for r, cs in by_rank.items():
        for c in cs:
            by_suit.setdefault(c.suit, {})[r] = c
    for suit_cards in by_suit.values():
        for lo in range(2, ACE + 1):
            for hi in range(lo + 4, ACE + 1):
                window = range(lo, hi + 1)
                if all(r in suit_cards for r in window):
                    out.append(
                        Combo(
                            ComboKind.STRAIGHTFLUSH,
                            tuple(suit_cards[r] for r in window),
                            float(hi),
                        )
                    )
    return out


def legal_plays(hand: Sequence[Card], top: Optional[Combo]) -> list[Combo]:
    """Every legal play from `hand` in this trick context, one representative
    per rank pattern.

    Leading (top is None): every combination the hand can form, the Dog
    included. Following: every combination that beats `top`, bombs included.
    Used for wish enforcement and by the bots.
    """
    plays: list[Combo] = []
    for combo in _hand_combos(hand):
        fin = finalize(combo, top)
        if fin is None:
            continue
        if top is None or beats(fin, top):
            plays.append(fin)
    return plays


def wish_plays(hand: Sequence[Card], top: Optional[Combo], wish: int) -> list[Combo]:
    """Legal plays that fulfil the wish, i.e. contain a real card of the
    wished rank. The Mah Jong wish must be honoured whenever any such play
    exists — including by breaking up combinations or playing a bomb."""
    if not any(c.rank == wish for c in hand):
        return []
    return [p for p in legal_plays(hand, top) if p.contains_rank(wish)]
