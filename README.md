# Terminal Tichu

The classic partnership card game **Tichu**, playable in your terminal by
four players over the network. Full official rules — Grand Tichu, the card
exchange, all combinations, bombs (in and out of turn), the Mah Jong wish,
Dog, Phoenix, Dragon, double wins, and scoring to 1000.

Pure Python 3.10+ standard library. Nothing to install.

```
──────────────────────────────────────────────────────────────
  hand 3 · WE 240 : THEY 185 (playing to 1000)
      partner: Maria* · 11🂠 tichu!
  prev: Alex · 8🂠    next: Kostas · 14🂠
      you · 11🂠
  table: pair [9♥ 9♣] by Kostas · 15 pts in trick
  your hand (11): Dog 1 2♥ 5♠ 5♦ 8♣ 10♥ J♦ Q♠ K♥ Phx
──────────────────────────────────────────────────────────────
  → your turn: beat it or pass
```

## Quick start

Host a table (one of you, on a machine the others can reach):

```bash
python -m tichu.server --port 4271
```

Everyone joins:

```bash
python -m tichu.client --host <server-address> --port 4271 --name Maria
```

The game starts automatically when four players are seated. Seats 0+2 play
against seats 1+3 (partners sit opposite); move seats in the lobby with
`sit <n>`. Missing players? Fill seats with bots:

```bash
python -m tichu.server --bots 3        # you against/with three bots
python -m tichu.server --bots 4        # bots only - join with --spectate to watch
```

Useful server flags: `--target 500` (shorter game), `--bot-delay 0.3`
(faster bots), `--seed 42` (reproducible shuffles).

If your connection drops mid-game, just restart the client from the same
directory — a reconnect token is kept in `.tichu_session.json` and you resume
your seat with full state. Extra connections beyond four join as spectators.
After a finished game, everyone types `rematch` to play again.

## Players with nothing installed: host for browsers

Your friends don't need Python, the repo, or a terminal — only a browser
(phones work). One of you hosts:

```bash
python -m tichu.web
```

and everyone opens `http://<host-address>:8080`, types a name, and plays —
same commands, same terminal look, with tap buttons for the common ones.
Under the hood each visitor gets a real client session on the host, so a
closed tab or dropped connection just resumes on reload. Terminal players
can still join the same table the classic way on the game port (4271), and
`--bots`, `--target`, `--bot-delay` work as usual.

Friends outside your network? Either forward port 8080 on your router, or
tunnel it with no router changes, e.g.:

```bash
cloudflared tunnel --url http://localhost:8080
```

(or `ngrok http 8080`, or share the machine over Tailscale) — then send
everyone the URL it prints.

## How to play

Cards are typed as **rank + suit letter** — ranks `2`–`10`, `J`, `Q`, `K`,
`A` (`T` is accepted shorthand for `10`); suits `s`♠ `h`♥ `d`♦ `c`♣ (standing in for Tichu's
Sword, Star, Pagoda and Jade). The specials are `1` (Mah Jong), `dog`,
`phx` (Phoenix) and `drg` (Dragon).

| command | meaning |
| --- | --- |
| `grand` / `take` | call Grand Tichu on your first 8 cards, or pick up all 14 |
| `t` | call Tichu (any time before you play your first card) |
| `x 2s Kh 5c` | exchange: first card to the next player, second to your partner, third to the previous player |
| `p 5s 5d` | play cards (`p 8s 8h phx` plays a Phoenix triple) |
| `p 1 wish K` | play the Mah Jong and wish for kings |
| `p 3h 4s 5d 6c phx as 7` | choose what the Phoenix stands for when ambiguous |
| `p 5s 5h 5d 5c` | bombs are just plays — legal even out of turn |
| `pass` (or `.`) | pass |
| `dragon <name>` (or `next`/`prev`) | give a Dragon-won trick to an opponent |
| `hand`, `board`, `score`, `who` | show your cards / the table / totals / seats |
| `say <text>` | table chat |
| `help`, `quit` | the rest |

Typing bare card codes (`5s 5d`) is also accepted as a play.

## The rules, as implemented

The full Fata Morgana rules:

* 56 cards: four suits of 2–A plus Mah Jong (1), Dog, Phoenix, Dragon.
* **Deal & Grand Tichu**: everyone sees 8 cards and may call Grand Tichu
  (±200) before picking up the remaining 6. Plain Tichu (±100) can be called
  by anyone until they play their first card. A call succeeds only if the
  caller goes out first.
* **Exchange**: each player passes one card face-down to each other player;
  whoever then holds the Mah Jong leads the first trick.
* **Combinations**: singles, pairs, triples, full houses, straights of 5+,
  and consecutive pairs; a following play must match the type *and* length
  and be higher. Full houses compare by their triple; the Mah Jong may bottom
  a straight (1-2-3-4-5).
* **Bombs**: four of a kind, or a straight flush of 5+. Bombs beat anything
  and may be thrown **out of turn**; any straight flush beats any four of a
  kind, longer straight flushes beat shorter ones. After a bomb, play
  continues from the bomber.
* **Mah Jong wish**: playing the 1 lets you wish a rank (2–A). The wish
  stays in force until fulfilled: any player who *can* legally play a card
  of that rank — including inside a combination or by sacrificing a bomb —
  *must*. The engine enforces this (an illegal pass is rejected), and the
  Phoenix standing in for the rank does not count.
* **Dog**: only led; the lead passes straight to your partner (or the next
  player with cards if they are out). It cannot be beaten or bombed.
* **Phoenix**: wild card in combinations (never in bombs, never as the 1).
  As a single it beats the previous single by half a step (1.5 when led)
  and never beats the Dragon. Worth −25.
* **Dragon**: highest single, worth +25 — but a trick won with the Dragon
  must be given to an opponent of your choice.
* **Going out & scoring**: the hand ends when the third player sheds their
  last card (the final trick is still fought out). The last player's
  remaining hand goes to the opposing team and their tricks to whoever went
  out first. Kings and tens score 10, fives score 5 (100 points per hand).
  If both partners go out before either opponent, the hand is a **double
  win**: 200 points, no card counting. First team to 1000 wins; ties play on.

## Architecture

```
tichu/
  cards.py     the 56-card deck, card codes, points
  combos.py    combination identification & comparison, Phoenix readings,
               fast legal-move enumeration (powers wish enforcement & bots)
  game.py      the rules state machine: phases, tricks, wish, Dog/Dragon,
               going out, transfers, scoring - transport-free & deterministic
  bot.py       a rules-abiding bot (fills seats, drives the test-suite)
  protocol.py  newline-delimited JSON framing
  server.py    authoritative asyncio TCP server: lobby, seats, reconnect
               tokens, spectators, bot seats, rematch votes
  client.py    the terminal UI: event log + board renders + command parser
  ansi.py      colors (honours NO_COLOR / non-TTY)
```

Design notes:

* **The server is authoritative.** Clients only ever *request* actions; the
  engine validates every one (turn order, combination legality, wish
  obligations, bomb interjections) and rejected actions never touch state.
  Each player receives a **redacted view** — you cannot see other hands in
  the protocol traffic, only counts.
* **The engine is transport-free.** `game.TichuGame` is a synchronous state
  machine: action methods either raise `IllegalAction` or return event
  dicts. That makes the whole rule set unit-testable without sockets, and
  the server a thin asyncio shell around it.
* **Out-of-turn bombs** work because clients can send a play at any moment;
  the server serializes actions and re-validates against live state.
* **Wire protocol**: one JSON object per line over TCP. Client → server:
  `hello`, `sit`, `grand`, `tichu`, `exchange`, `play`, `pass`, `dragon`,
  `chat`, `rematch`, `who`, `ping`. Server → client: `welcome`, `lobby`,
  `started`, `game` (engine events), `state` (your redacted snapshot after
  every action), `chat`, `error`, join/leave notices.

## Tests

```bash
python -m unittest discover -s tests -v
```

~45 tests cover combination identification (cross-checked against a
brute-force enumerator on random hands), trick mechanics, the wish, Dog,
Dragon gifting, bombs in and out of turn, double wins, scoring transfers
and bonuses, full bot-vs-bot games on many seeds with per-hand scoring
invariants (every hand's card points total exactly 100), a complete game
played over real sockets, and the client's renderer and command parser.
