"""The Tichu terminal client.

Connects to a ``tichu.server`` table and plays one seat (or spectates).
Pure stdlib: asyncio networking plus an input thread; events stream in as
log lines, and whenever it is your move a full board is rendered.

Run:  python -m tichu.client --host example.com --port 4271 --name Maria
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from . import ansi
from .cards import Card, parse_card, parse_rank, rank_to_str, sort_hand
from .combos import interpretations, legal_plays
from .protocol import DEFAULT_PORT, encode, read_message

SESSION_FILE = Path(".tichu_session.json")

HELP = """\
commands
  p|play <cards> [wish <rank>] [as <rank>]   play cards (also out-of-turn bombs)
        e.g.  p 5s 5d        p 1 wish K        p 8s 8h phx as 8
  pass | .                                   pass (when following a trick)
  grand | gt                                 call Grand Tichu (first 8 cards)
  take | ok                                  decline Grand Tichu, pick up all 14
  t | tichu                                  call Tichu (before your first play)
  x <c1> <c2> <c3>                           exchange: to next, partner, previous
  dragon <name|next|prev>                    give a Dragon trick to an opponent
                                             (one who still has cards)
  hand | h      show your hand               board | s     show the full board
  say <text>                                 chat          who    seats
  rematch                                    vote to play again
  help | ?      this text                    quit          leave the table

cards: rank + suit letter.  ranks 2-10, J, Q, K, A ('T' also accepted for 10)
       suits s=spades(sword) h=hearts(star) d=diamonds(pagoda) c=clubs(jade)
       specials: 1 (Mah Jong), dog, phx (Phoenix), drg (Dragon)"""

SUIT_COLOR = {"s": ansi.white, "h": ansi.red, "d": ansi.blue, "c": ansi.green}
SPECIAL_COLOR = {"Dog": ansi.cyan, "1": ansi.cyan, "Phx": ansi.magenta, "Drg": ansi.byellow}


def card_str(code: str) -> str:
    card = parse_card(code)
    if card is None:
        return code
    if card.is_special:
        return SPECIAL_COLOR.get(card.code, str)(card.code)
    return SUIT_COLOR[card.suit.value](card.display())


def cards_str(codes: list[str]) -> str:
    return " ".join(card_str(c) for c in codes)


def combo_label(combo: str) -> str:
    """A combination's name for the log, minus the cards (they are printed
    right next to it): 'single A♦' -> 'single', 'pair (9♥ 9♣)' -> 'pair'.
    'straight to 7 (...)' keeps its 'to 7' - that part is information."""
    label = combo.split("(")[0].strip()
    return "single" if label.startswith("single ") else label


BOARD_WIDTH = 62      # the rules the board is drawn between
SIDE_W, BOX_W = 17, 24  # seats left/right of the table box: 2+17+1+24+1+17 = 62
FLASH_SECONDS = 0.35  # how long the window stays green / red

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def vis(s: str) -> str:
    """The text as it will appear on screen: color codes removed."""
    return _ANSI_RE.sub("", s)


def ljust(s: str, width: int) -> str:
    return s + " " * max(0, width - len(vis(s)))


def wrap(tokens: list[str], width: int) -> list[str]:
    """Pack (possibly colored) tokens into lines no wider than `width`."""
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        cand = f"{cur} {tok}" if cur else tok
        if cur and len(vis(cand)) > width:
            lines.append(cur)
            cur = tok
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


class Client:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.seat: Optional[int] = None
        self.name = args.name
        self.token: Optional[str] = args.token
        self.state: Optional[dict] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._last_prompt_sig = None
        self.no_flash = bool(getattr(args, "no_flash", False)
                             or os.environ.get("TICHU_NO_FLASH"))

    # ------------------------------------------------------------- #
    # small helpers

    def send(self, obj: dict) -> None:
        if self.writer is not None and not self.writer.is_closing():
            self.writer.write(encode(obj))

    def names(self) -> list[str]:
        return self.state["names"] if self.state else ["?"] * 4

    def pname(self, seat: Optional[int]) -> str:
        """A player's name: your side in green (you in bold, your partner
        starred), the opponents in the plain text color."""
        if seat is None:
            return "?"
        n = self.names()[seat]
        color = self.team_color(seat % 2)
        if self.seat is None:
            return color(n)
        if seat == self.seat:
            return ansi.bold(color("you"))
        if seat == (self.seat + 2) % 4:
            return color(f"{n}*")  # partner
        return color(n)

    def my_team(self) -> int:
        return (self.seat or 0) % 2

    def team_label(self, team: int) -> str:
        if self.seat is None:
            return ["N/S", "E/W"][team]
        return "WE" if team == self.my_team() else "THEY"

    def team_color(self, team: int):
        """Green for your side (N/S when spectating); the other side is left
        uncolored, so red stays reserved for bombs, errors and hearts."""
        return ansi.green if team == self.my_team() else str

    def team_name(self, team: int) -> str:
        return self.team_color(team)(self.team_label(team))

    def hand_cards(self) -> list[Card]:
        if not self.state or "hand" not in self.state:
            return []
        return [parse_card(c) for c in self.state["hand"]]

    def top_combo(self):
        top = self.state.get("top") if self.state else None
        if not top:
            return None
        cards = [parse_card(c) for c in top["cards"]]
        for interp in interpretations(cards):
            if interp.kind.value == top["kind"] and (
                interp.power == top["power"] or interp.power is None
            ):
                if interp.power is None:
                    from dataclasses import replace
                    return replace(interp, power=top["power"])
                return interp
        return None

    # ------------------------------------------------------------- #
    # rendering

    def show_scores_line(self) -> str:
        st = self.state
        mine, theirs = self.my_team(), 1 - self.my_team()
        return (
            f"hand {st['hand_no']} · "
            f"{self.team_name(mine)} {ansi.bold(str(st['scores'][mine]))} : "
            f"{self.team_name(theirs)} {ansi.bold(str(st['scores'][theirs]))} "
            f"(playing to {st['target']})"
        )

    def render_board(self) -> None:
        st = self.state
        if not st:
            print(ansi.dim("no game state yet"))
            return
        print()
        print(ansi.dim("─" * BOARD_WIDTH))
        print(f"  {self.show_scores_line()}")
        if self.seat is None:
            print(ansi.dim("  (spectating)"))
        for line in self.seating():
            print(line)
        if self.seat is not None and "hand" in st:
            hand = st["hand"]
            print(f"  your hand ({len(hand)}): {cards_str(hand)}")
        print(ansi.dim("─" * BOARD_WIDTH))

    def seating(self) -> list[str]:
        """The table as seen from your chair: partner across, the opponents to
        your left (plays next) and right (played before you), you at the
        bottom - and what lies on the table in the middle. Spectators look
        over seat 0's shoulder."""
        me = self.seat if self.seat is not None else 0
        roles = {me: "", (me + 2) % 4: "partner", (me + 1) % 4: "next", (me + 3) % 4: "prev"}
        if self.seat is None:  # spectators: seat numbers as in the lobby, no roles
            roles = {s: "" for s in range(4)}
        waiting = self._waiting_on()
        across, left, right, you = (
            self._seat_block((me + k) % 4, roles[(me + k) % 4], waiting) for k in (2, 1, 3, 0)
        )
        box = self._table_box(min_rows=max(3, len(left) - 2, len(right) - 2))
        lines = self._centered(across)
        lo, ro = (len(box) - len(left)) // 2, (len(box) - len(right)) // 2
        for i, row in enumerate(box):
            l = left[i - lo] if 0 <= i - lo < len(left) else ""
            r = right[i - ro] if 0 <= i - ro < len(right) else ""
            lines.append(f"  {ljust(l, SIDE_W)} {row} {r}".rstrip())
        lines += self._centered(you)
        return lines

    def _waiting_on(self) -> set[int]:
        """Seats whose action the table is waiting for (marked ▸)."""
        st = self.state
        phase = st["phase"]
        if phase == "playing":
            return {st["turn"]} if st.get("turn") is not None else set()
        if phase == "dragon_gift":
            return {st["dragon_chooser"]} if st.get("dragon_chooser") is not None else set()
        if phase == "grand_tichu":
            return {s for s in range(4) if not st["picked_up"][s]}
        if phase == "exchange":
            return {s for s in range(4) if not st.get("exchanged", [True] * 4)[s]}
        return set()

    def _seat_block(self, s: int, role: str, waiting: set[int]) -> list[str]:
        """One player: name (team-colored, ▸ if it is their move), role and
        card count, then any tichu / out / offline tags. Every line fits the
        side columns, so the table box never moves."""
        st = self.state
        color = self.team_color(s % 2)
        seat_no = f"[{s}] " if self.seat is None else ""
        name = "you" if s == self.seat else self.names()[s]
        cap = SIDE_W - 2 - len(seat_no)
        if len(name) > cap:
            name = name[:cap - 1] + "…"
        mark = ansi.cyan("▸ ") if s in waiting else "  "
        n = st["hand_counts"][s]
        info = (ansi.dim(f"{role} · ") if role else "") + f"{n} card{'s' if n != 1 else ''}"
        lines = [mark + (ansi.dim(seat_no) if seat_no else "")
                 + (ansi.bold(color(name)) if s == self.seat else color(name)),
                 "  " + info]
        tags = []
        if st["calls"][s] == "grand":
            tags.append(ansi.byellow("GRAND!"))
        elif st["calls"][s]:
            tags.append(ansi.yellow("tichu!"))
        if s in st["out_order"]:
            tags.append(ansi.dim(f"out#{st['out_order'].index(s) + 1}"))
        if not st.get("connected", [True] * 4)[s]:
            tags.append(ansi.bred("offline"))
        if tags:
            joined = " · ".join(tags)
            lines += ["  " + joined] if len(vis(joined)) + 2 <= SIDE_W else ["  " + t for t in tags]
        return lines

    def _table_box(self, min_rows: int) -> list[str]:
        """The middle of the table: the combination to beat (cards once, who
        played it, points in the trick), an active wish, or why it is empty."""
        st = self.state
        inner = BOX_W - 4
        rows: list[str] = []
        top = st.get("top")
        if top:
            label = combo_label(top["combo"])
            cards = [card_str(c) for c in top["cards"]]
            cards[0] = "[" + cards[0]
            cards[-1] = cards[-1] + "]"
            one_line = " ".join([label, *cards])
            if len(vis(one_line)) <= inner:
                rows.append(one_line)
            else:
                rows += wrap(label.split(), inner) + wrap(cards, inner)
            rows += wrap(["by", self.pname(top["seat"]), "·", f"{st.get('trick_points', 0)} pts"], inner)
        elif st["phase"] == "playing":
            who = "your lead" if st["turn"] == self.seat else self.pname(st["turn"]) + " leads"
            rows += [ansi.dim("empty"), who]
        elif st["phase"] == "dragon_gift":
            rows += [ansi.dim("Dragon trick"), ansi.dim("to give away")]
        else:
            rows.append(ansi.dim({"grand_tichu": "Grand Tichu?", "exchange": "card exchange",
                                  "game_over": "game over"}.get(st["phase"], st["phase"])))
        if st.get("wish"):
            rows.append(ansi.magenta(f"wish: {rank_to_str(st['wish'])}"))
        rows += [""] * (min_rows - len(rows))
        bar = ansi.dim("│")
        return [ansi.dim("┌" + "─" * (BOX_W - 2) + "┐"),
                *(f"{bar} {ljust(r, inner)} {bar}" for r in rows),
                ansi.dim("└" + "─" * (BOX_W - 2) + "┘")]

    @staticmethod
    def _centered(block: list[str]) -> list[str]:
        """A block of lines, left-aligned to each other, centered on the board."""
        width = max(len(vis(l)) for l in block)
        pad = " " * ((BOARD_WIDTH - width) // 2)
        return [(pad + l).rstrip() for l in block]

    def flash(self, color: str, text: str) -> None:
        """Shout: a full-width colored bar in the log and - where the terminal
        understands OSC 11 - a brief flash of the whole window (the browser
        page flashes too). --no-flash / TICHU_NO_FLASH=1 keeps only the bar."""
        paint = ansi.on_green if color == "green" else ansi.on_red
        if not self.no_flash:
            sys.stdout.write(ansi.flash_on(color))
        print(paint(f"  {text}".ljust(BOARD_WIDTH)))
        if not self.no_flash and ansi.enabled():
            try:
                asyncio.get_running_loop().call_later(FLASH_SECONDS, self._unflash)
            except RuntimeError:  # no event loop running (tests): undo at once
                self._unflash()

    @staticmethod
    def _unflash() -> None:
        sys.stdout.write(ansi.flash_off())
        sys.stdout.flush()

    def prompt_hint(self) -> None:
        """Context-sensitive one-liner about what is expected of you now."""
        st = self.state
        if not st or self.seat is None:
            return
        me = self.seat
        phase = st["phase"]
        if phase == "grand_tichu" and not st["picked_up"][me]:
            print(f"  {ansi.cyan('→ call')} {ansi.bold('grand')} "
                  f"{ansi.cyan('or')} {ansi.bold('take')} {ansi.cyan('your last 6 cards')}")
        elif phase == "exchange" and not st.get("exchange_submitted"):
            nxt, prt, prv = (me + 1) % 4, (me + 2) % 4, (me + 3) % 4
            print(f"  {ansi.cyan('→ pass three cards:')} "
                  f"x <to {self.names()[nxt]}> <to {self.names()[prt]}> <to {self.names()[prv]}>"
                  f"{ansi.dim('   (tichu still possible)')}")
        elif phase == "playing" and st["turn"] == me:
            top = self.top_combo()
            if top is None:
                print(f"  {ansi.cyan('→ your lead — play any combination')}")
            else:
                can = bool(legal_plays(self.hand_cards(), top))
                extra = "" if can else ansi.dim(" (nothing beats it — pass, or bomb)")
                print(f"  {ansi.cyan('→ your turn: beat it or')} {ansi.bold('pass')}{extra}")
        elif phase == "dragon_gift" and st.get("dragon_chooser") == me:
            choices = "  |  ".join(
                f"dragon {self.names()[s]}" for s in self._dragon_choices()
            )
            print(f"  {ansi.cyan('→ give the Dragon trick away:')} {choices}")

    def maybe_prompt(self) -> None:
        st = self.state
        if not st or self.seat is None:
            return
        me = self.seat
        demand = None
        if st["phase"] == "grand_tichu" and not st["picked_up"][me]:
            demand = ("grand", st["hand_no"])
        elif st["phase"] == "exchange" and not st.get("exchange_submitted"):
            demand = ("exchange", st["hand_no"])
        elif st["phase"] == "playing" and st["turn"] == me:
            top = st.get("top")
            demand = ("play", st["hand_no"], tuple(top["cards"]) if top else None,
                      st.get("wish"))
        elif st["phase"] == "dragon_gift" and st.get("dragon_chooser") == me:
            demand = ("dragon", st["hand_no"])
        if demand is not None and demand != self._last_prompt_sig:
            self.render_board()
            self.prompt_hint()
        self._last_prompt_sig = demand

    # ------------------------------------------------------------- #
    # server events

    def on_message(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "welcome":
            self.seat = msg["seat"]
            self.name = msg["name"]
            if msg.get("token"):
                self.token = msg["token"]
                self._save_session()
            where = "spectating" if self.seat is None else f"seat {self.seat}"
            print(ansi.green(f"✔ connected as {self.name} ({where}); playing to {msg['target']}"))
            if self.seat is not None:
                print(ansi.dim("  (a reconnect token was saved; if you drop, just restart the client)"))
        elif ev == "lobby":
            seats = msg["seats"]
            row = []
            for i, s in enumerate(seats):
                if s is None:
                    row.append(ansi.dim(f"[{i}] empty"))
                else:
                    tags = "🤖" if s.get("bot") else ("" if s.get("connected") else "⚠")
                    row.append(f"[{i}] {s['name']}{tags}")
            print("  seats: " + "   ".join(row) +
                  ansi.dim("   (teams: 0+2 vs 1+3; move with 'sit N')"))
        elif ev == "started":
            tag = " (rematch)" if msg.get("rematch") else ""
            print(ansi.bold(f"\n★ game on{tag}! {' & '.join(msg['names'][0::2])} vs "
                            f"{' & '.join(msg['names'][1::2])} — first to {msg['target']}"))
        elif ev == "state":
            self.state = msg["data"]
            self.maybe_prompt()
        elif ev == "game":
            self.on_game_event(msg["data"])
        elif ev == "chat":
            who = msg.get("name", "?")
            print(f"  {ansi.cyan('[' + who + ']')} {msg['text']}")
        elif ev == "error":
            print(ansi.bred(f"✗ {msg['msg']}"))
            self.prompt_hint()
        elif ev == "joined":
            print(ansi.green(f"+ {msg['name']} sat down (seat {msg['seat']})"))
        elif ev == "rejoined":
            print(ansi.green(f"+ {msg['name']} reconnected"))
        elif ev == "left":
            if msg.get("in_game"):
                print(ansi.bred(f"- {msg['name']} disconnected — seat held for reconnect"))
            else:
                print(ansi.dim(f"- {msg['name']} left"))
        elif ev == "moved":
            self.seat = msg["seat"]
            print(ansi.green(f"✔ you moved to seat {self.seat}"))
        elif ev == "rematch_vote":
            waiting = msg.get("waiting") or []
            if waiting:
                print(f"  {msg['name']} wants a rematch — waiting for {', '.join(waiting)}")
        elif ev in ("pong", "ping"):
            pass

    def on_game_event(self, e: dict) -> None:
        t = e.get("type")
        if t == "hand_start":
            print(ansi.bold(f"\n──── hand {e['hand_no']} "
                            f"({self.team_name(0)} {e['scores'][0]} : "
                            f"{self.team_name(1)} {e['scores'][1]}) ────"))
        elif t == "called":
            word = "GRAND TICHU" if e["call"] == "grand" else "TICHU"
            print(ansi.byellow(f"  ★ {self.pname(e['seat'])} calls {word}!"))
        elif t == "took_cards":
            if e["seat"] == self.seat:
                print(ansi.dim("  you take up your last six cards"))
            else:
                print(ansi.dim(f"  {self.pname(e['seat'])} takes their cards"))
        elif t == "exchange_phase":
            print(ansi.cyan("  everyone has their 14 cards — exchange time"))
        elif t == "exchanged":
            if e["seat"] == self.seat:
                print(ansi.dim("  you passed your three cards"))
            else:
                print(ansi.dim(f"  {self.pname(e['seat'])} passed their three cards"))
        elif t == "received":
            gifts = ", ".join(
                f"{card_str(g['card'])} from {self.pname(g['from'])}" for g in e["gifts"]
            )
            print(f"  {ansi.green('you received:')} {gifts}")
        elif t == "play_phase":
            if e["leader"] == self.seat:
                print("  you hold the Mah Jong and lead")
            else:
                print(f"  {self.pname(e['leader'])} holds the Mah Jong and leads")
        elif t == "played":
            print(f"  {self.pname(e['seat'])}: {combo_label(e['combo'])} "
                  f"[{cards_str(e['cards'])}]")
            who = "you" if e["seat"] == self.seat else self.names()[e["seat"]]
            if e.get("bomb"):
                extra = " — out of turn!" if e.get("out_of_turn") else ""
                self.flash("red", f"💥  BOMB  by {who}{extra}")
            elif "Drg" in e["cards"]:
                self.flash("green", f"🐉  DRAGON  played by {who}")
        elif t == "passed":
            print(ansi.dim(f"  {self.pname(e['seat'])} passes"))
        elif t == "wish_set":
            print(ansi.magenta(f"  Mah Jong wish: {rank_to_str(e['rank'])}"))
        elif t == "wish_fulfilled":
            print(ansi.magenta(f"  wish fulfilled by {self.pname(e['seat'])}"))
        elif t == "dog":
            print(f"  🐕 the Dog: lead passes to {self.pname(e['to'])}")
        elif t == "went_out":
            place = ["1st", "2nd", "3rd", "4th"][e["place"] - 1]
            print(ansi.bold(f"  ✓ {self.pname(e['seat'])} is out ({place})"))
        elif t == "trick_won":
            pts = f" (+{e['points']} pts)" if e["points"] else ""
            print(f"  → {self.pname(e['seat'])} takes the trick{pts}")
        elif t == "dragon_pending":
            print(ansi.byellow(f"  🐉 {self.pname(e['seat'])} wins the trick with the Dragon "
                               f"({e['points']} pts) and must give it to an opponent"))
        elif t == "dragon_given":
            print(ansi.byellow(f"  🐉 Dragon trick ({e['points']} pts) goes to {self.pname(e['to'])}"))
        elif t == "turn":
            if self.seat is not None and e["seat"] == self.seat:
                pass  # maybe_prompt renders the full board
            else:
                verb = "leads" if e.get("leading") else "to play"
                print(ansi.dim(f"  · {self.pname(e['seat'])} {verb}"))
        elif t == "hand_end":
            self.print_hand_end(e)
        elif t == "game_over":
            w = e["winner"]
            print(ansi.bold(ansi.green(
                f"\n════ GAME OVER — {self.team_name(w)} win "
                f"{e['scores'][w]} : {e['scores'][1 - w]} ════")))
            print(ansi.dim("  type 'rematch' to play again"))

    def print_hand_end(self, e: dict) -> None:
        print(ansi.bold("  ── hand result ──"))
        if e["double_win"]:
            team = self.team_name(e["first_out"] % 2)
            print(ansi.byellow(f"  DOUBLE WIN for {team} (+{200})!"))
        else:
            print(f"  first out: {self.pname(e['first_out'])}")
        for b in e.get("bonuses", []):
            sign = "+" if b["delta"] > 0 else ""
            what = "grand tichu" if b["call"] == "grand" else "tichu"
            print(f"  {self.pname(b['seat'])} {what}: {sign}{b['delta']}")
        t0, t1 = e["team_points"]
        print(f"  this hand: {self.team_name(0)} {t0:+} · {self.team_name(1)} {t1:+}")
        s0, s1 = e["scores"]
        print(ansi.bold(f"  totals:    {self.team_name(0)} {s0} · {self.team_name(1)} {s1}"))

    # ------------------------------------------------------------- #
    # user input

    def handle_line(self, line: str) -> bool:
        """Returns False to quit."""
        words = line.strip().split()
        if not words:
            if self.state:
                self.render_board()
                self.prompt_hint()
            return True
        cmd, args = words[0].lower(), words[1:]

        if cmd in ("quit", "exit"):
            return False
        if cmd in ("help", "?"):
            print(HELP)
        elif cmd in ("h", "hand"):
            if self.state and "hand" in self.state:
                print(f"  your hand: {cards_str(self.state['hand'])}")
        elif cmd in ("s", "show", "board"):
            self.render_board()
            self.prompt_hint()
        elif cmd == "score":
            if self.state:
                print("  " + self.show_scores_line())
        elif cmd == "who":
            self.send({"cmd": "who"})
        elif cmd == "sit" and args:
            try:
                self.send({"cmd": "sit", "seat": int(args[0])})
            except ValueError:
                print(ansi.bred("✗ sit takes a seat number 0-3"))
        elif cmd in ("say", "chat", "c"):
            if args:
                self.send({"cmd": "chat", "text": " ".join(args)})
        elif cmd in ("t", "tichu"):
            self.send({"cmd": "tichu"})
        elif cmd in ("gt", "grand"):
            self.send({"cmd": "grand", "call": True})
        elif cmd in ("take", "ok", "no"):
            self.send({"cmd": "grand", "call": False})
        elif cmd in ("pass", "."):
            self.send({"cmd": "pass"})
        elif cmd in ("x", "e", "exchange"):
            if len(args) != 3:
                print(ansi.bred("✗ exchange exactly three cards: x <next> <partner> <prev>"))
            else:
                self.send({"cmd": "exchange", "cards": args})
        elif cmd == "dragon":
            seat = self._dragon_target(args)
            if seat is None:
                print(ansi.bred("✗ dragon <opponent name|next|prev>"))
            else:
                self.send({"cmd": "dragon", "to": seat})
        elif cmd in ("p", "play", "b", "bomb"):
            self._cmd_play(args)
        elif cmd == "rematch":
            self.send({"cmd": "rematch"})
        else:
            # bare card codes are treated as a play, so "5s 5d" just works
            if parse_card(cmd) is not None:
                self._cmd_play(words)
            else:
                print(ansi.bred(f"✗ unknown command '{cmd}' — try 'help'"))
        return True

    def _dragon_choices(self) -> list[int]:
        """Opponents the server will accept the Dragon trick for."""
        assert self.seat is not None
        both = [(self.seat + 1) % 4, (self.seat + 3) % 4]
        allowed = (self.state or {}).get("dragon_targets")
        return [s for s in both if s in allowed] if allowed else both

    def _dragon_target(self, args: list[str]) -> Optional[int]:
        if self.seat is None:
            return None
        choices = self._dragon_choices()
        if len(choices) == 1:
            return choices[0]  # the other opponent is out; no ambiguity to name
        if not args:
            return None
        a, b = (self.seat + 1) % 4, (self.seat + 3) % 4
        word = args[0].lower()
        if word in ("next", "left"):
            return a
        if word in ("prev", "previous", "right"):
            return b
        for s in (a, b):
            if self.names()[s].lower().startswith(word):
                return s
        return None

    def _cmd_play(self, args: list[str]) -> None:
        cards: list[str] = []
        wish = phoenix_as = None
        i = 0
        while i < len(args):
            tok = args[i].lower()
            if tok in ("wish", "w") and i + 1 < len(args):
                wish = args[i + 1]
                i += 2
                continue
            if tok == "as" and i + 1 < len(args):
                phoenix_as = args[i + 1]
                i += 2
                continue
            if tok.startswith("wish=") or tok.startswith("w="):
                wish = tok.split("=", 1)[1]
                i += 1
                continue
            if tok.startswith("as="):
                phoenix_as = tok.split("=", 1)[1]
                i += 1
                continue
            if parse_card(tok) is None:
                print(ansi.bred(f"✗ '{args[i]}' is not a card (see 'help')"))
                return
            cards.append(tok)
            i += 1
        if not cards:
            print(ansi.bred("✗ play what? e.g.  p 5s 5d"))
            return
        msg = {"cmd": "play", "cards": cards}
        if wish is not None:
            msg["wish"] = wish
        if phoenix_as is not None:
            msg["phoenix_as"] = phoenix_as
        self.send(msg)

    # ------------------------------------------------------------- #
    # session persistence (reconnect)

    def _session_key(self) -> str:
        return f"{self.args.host}:{self.args.port}"

    def _load_session(self) -> Optional[str]:
        if self.args.fresh or self.token:
            return self.token
        try:
            data = json.loads(SESSION_FILE.read_text())
            return data.get(self._session_key(), {}).get("token")
        except (OSError, ValueError):
            return None

    def _save_session(self) -> None:
        try:
            data = {}
            if SESSION_FILE.exists():
                data = json.loads(SESSION_FILE.read_text())
            data[self._session_key()] = {"token": self.token, "name": self.name}
            SESSION_FILE.write_text(json.dumps(data, indent=1))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------- #
    # main loop

    async def run(self) -> None:
        try:
            reader, writer = await asyncio.open_connection(self.args.host, self.args.port)
        except OSError as exc:
            print(ansi.bred(f"✗ cannot connect to {self.args.host}:{self.args.port} — {exc}"))
            return
        self.writer = writer
        hello = {"cmd": "hello", "name": self.args.name}
        token = self._load_session()
        if token:
            hello["token"] = token
        if self.args.spectate:
            hello["spectate"] = True
        self.send(hello)

        print(ansi.dim("type 'help' for commands"))
        loop = asyncio.get_running_loop()
        stdin_task = asyncio.ensure_future(loop.run_in_executor(None, sys.stdin.readline))
        net_task = asyncio.ensure_future(read_message(reader))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {stdin_task, net_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if net_task in done:
                    msg = net_task.result()
                    if msg is None:
                        print(ansi.bred("\n✗ server closed the connection"))
                        break
                    if msg:
                        self.on_message(msg)
                    net_task = asyncio.ensure_future(read_message(reader))
                if stdin_task in done:
                    line = stdin_task.result()
                    if line == "" or not self.handle_line(line):
                        print(ansi.dim("bye"))
                        break
                    stdin_task = asyncio.ensure_future(
                        loop.run_in_executor(None, sys.stdin.readline)
                    )
                try:
                    await writer.drain()
                except (ConnectionError, RuntimeError):
                    break
        finally:
            for t in (stdin_task, net_task):
                t.cancel()
            try:
                writer.close()
            except RuntimeError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal Tichu client")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--name", default=os.environ.get("USER", "player"))
    ap.add_argument("--spectate", action="store_true", help="watch without a seat")
    ap.add_argument("--token", default=None, help="reconnect token (usually automatic)")
    ap.add_argument("--fresh", action="store_true", help="ignore any saved session token")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--no-flash", action="store_true",
                    help="don't flash the window on bombs / the Dragon (or TICHU_NO_FLASH=1)")
    args = ap.parse_args()
    if args.no_color:
        ansi.set_enabled(False)
    try:
        asyncio.run(Client(args).run())
    except KeyboardInterrupt:
        print()
    # a blocked stdin thread must not keep the process alive
    os._exit(0)


if __name__ == "__main__":
    main()
