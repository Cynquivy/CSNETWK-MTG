# CSNETWK-MTG

very umazing MTG card game !!!!!!

## Requirements

- Python 3.10 or newer. Standard library only; no third-party
  packages.

## Build & run

`server.py` and `client.py` live inside the `network/` package and use
package-relative imports (`from network import protocol`,
`from model.player import player`, etc.), so they **must** be launched with
`python -m` from the project root — running them as plain scripts
(`python network/server.py`) fails with `ModuleNotFoundError: No module
named 'network'`.

From the project root, in two terminals:

```
# Terminal 1 — start the server (verbose)
python -m network.server --verbose

# Terminal 2 — start a client (verbose)
python -m network.client --verbose
```

Flags:

```
python -m network.server [--host 0.0.0.0] [--port 4444] [-v | --verbose]
python -m network.client [--host 127.0.0.1] [--port 4444] [-v | --verbose]
```

To connect across two machines on a LAN, leave the server on `--host 0.0.0.0`
and start the client with `--host <server-ip>`.

## Enabling verbose mode

Pass `-v` / `--verbose` to either program. Every PDU sent and received is
printed with a timestamp and a `source -> destination` tag, for example:

```
[10:07:05.331] [CLIENT -> SERVER] {"type":"PLAYER_READY","seq_num":1,...}
[10:07:05.332] [SERVER -> CLIENT] {"type":"PLAYER_READY","seq_num":1,...}
```

## Project status

**Short version:** the server can already run a real game from the lobby all the way through
casting spells, activating abilities, resolving the Stack, checking win/loss conditions, and
firing a handful of triggered abilities — matching the RFC closely enough that raw test scripts
can play through it over real sockets. What's **not** built yet is combat, playing a land, a
real interactive client, and most of the actual card effects (cards exist as data with the
correct cost/stats, but resolving one doesn't yet *do* anything). So: **the protocol engine
works; there is no playable game via the CLI client yet.**

### What's done

| Area | Status |
|---|---|
| TCP transport (4-byte length-prefixed framing, JSON) | ✅ |
| Connection limits (exactly 2 players, 3rd refused) | ✅ |
| `PING` / `PONG` heartbeat | ✅ |
| `seq_num` echo/validation (`STALE_ACTION`, priority-grant re-issue) | ✅ |
| LOBBY & `PLAYER_READY` (deck validation, duplicate IDs, resubmission) | ✅ |
| `GAME_SETUP` (shuffle, deal 7, coin flip, life set to 20) | ✅ |
| `MULLIGAN` (London mulligan: redraw, then bottom N cards on keep) | ✅ |
| Turn/phase engine: Untap → Upkeep → Draw → Precombat Main → Begin Combat | ✅ |
| Priority & the Stack (`CAST_SPELL`, `ACTIVATE_ABILITY`, push/resolve) | ✅ |
| State-based actions (life ≤ 0 loses; lethal-damage creatures die) | ✅ |
| Triggered abilities (detection, AP/NAP ordering, `TRIGGER_ORDER`/`TRIGGER_CHOICE`) | ✅ |
| Combat (Declare Attackers through Combat Damage) | ❌ |
| Playing a land (`PLAY_LAND`) | ❌ |
| Discarding down to 7 cards at Cleanup | ❌ |
| Conceding / `GAME_OVER` / returning to LOBBY for a rematch | ❌ |
| Priority-grant timeouts, disconnect/reconnect handling | ❌ |
| Every `ERROR` code in every situation the RFC requires it | Partial |
| A real, playable client (`network/client.py`) | ❌ |
| Actual card effects (Lightning Bolt's damage, etc.) | ❌ |

### Where a live game currently stops

The server correctly drives a real turn through `MULLIGAN → UNTAP → UPKEEP → DRAW →
PRECOMBAT_MAIN → BEGIN_COMBAT`, with genuine `PRIORITY_GRANT`/`PRIORITY_PASS` exchanges and a
working Stack — see the demo script below. Once it broadcasts `PHASE_TRANSITION` into
`DECLARE_ATTACKERS`, nothing currently handles that PDU, so the game stops there. `Cleanup` and
the following turn's `Untap` step *are* fully implemented and tested (turn counter, active-player
swap, damage cleanup) but can currently only be reached by directly advancing the engine in a
test script, not through a live game, since nothing yet gets a real game past `DECLARE_ATTACKERS`.

### There is no playable client yet

Every scenario above has been verified with test scripts that speak MTGNP directly over a raw
socket (the same way the demo below does) — **not** through `network/client.py`. The actual
client program still only sends a `PING` and prints the reply; it doesn't send `PLAYER_READY`,
render any game state, or let a human make a real decision. That's a separate step, still to do.

## How to test what's implemented so far

### 1. Full pipeline demo: Lobby → Setup → Mulligan → a real turn → casting a spell

Run it from the project root:

```
python -c "
import socket, json, struct, threading, time
from network.server import GameServer
from model.card_database import card_database

server = GameServer('127.0.0.1', 47000, verbose=False)
threading.Thread(target=server.start, daemon=True).start()
time.sleep(0.3)

def client():
    s = socket.create_connection(('127.0.0.1', 47000), timeout=5)
    def send(pdu):
        p = json.dumps(pdu).encode()
        s.sendall(struct.pack('>I', len(p)) + p)
    def recv():
        (n,) = struct.unpack('>I', s.recv(4))
        return json.loads(s.recv(n).decode())
    return send, recv

send1, recv1 = client()
send2, recv2 = client()
deck = ['mountain_001', 'mountain_002', 'mountain_003', 'mountain_004',
        'lightning_bolt_001', 'lightning_bolt_002', 'shock_001', 'goblin_guide_001']

# Lobby
send1({'type': 'PLAYER_READY', 'seq_num': 1, 'player_id': 'player_1', 'deck_list': deck})
recv1()
send2({'type': 'PLAYER_READY', 'seq_num': 1, 'player_id': 'player_2', 'deck_list': deck})
setup1, setup2 = recv1(), recv2()
assert setup1['state']['phase'] == 'MULLIGAN' and len(setup1['state']['hand']['player_1']) == 7

# Mulligan: both keep their opening hand
send1({'type': 'MULLIGAN_CHOICE', 'seq_num': setup1['seq_num'], 'keep': True, 'cards_to_bottom': []})
send2({'type': 'MULLIGAN_CHOICE', 'seq_num': setup2['seq_num'], 'keep': True, 'cards_to_bottom': []})

active_id = None
for r in (recv1, recv2):
    t = r()
    assert t['to_phase'] == 'UNTAP' and t['turn'] == 1
    active_id = t['active_player']
recv1(); recv2()   # Untap Step's GAME_STATE_UPDATE
recv1(); recv2()   # UNTAP -> UPKEEP

send_ap, recv_ap = (send1, recv1) if active_id == 'player_1' else (send2, recv2)
send_nap, recv_nap = (send2, recv2) if active_id == 'player_1' else (send1, recv1)
other_id = server.game_state.opponent_of(active_id).player_id
active = server.game_state.players[active_id]

# Upkeep -> Draw -> Precombat Main (both players just pass each window)
g = recv_ap(); send_ap({'type': 'PRIORITY_PASS', 'seq_num': g['seq_num']})
g2 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g2['seq_num']})
recv_ap(); recv_nap()
g3 = recv_ap(); send_ap({'type': 'PRIORITY_PASS', 'seq_num': g3['seq_num']})
g4 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g4['seq_num']})
recv_ap(); recv_nap()
main_grant = recv_ap()
assert server.game_state.phase.value == 'PRECOMBAT_MAIN'

# PLAY_LAND isn't implemented yet (see status table above), so a mana
# source is placed directly on the board for this demonstration.
mountain = card_database.CARD_DATABASE['mountain_015']
mountain.is_tapped = False
active.board = [mountain]
if 'lightning_bolt_001' not in active.hand:
    active.hand.append('lightning_bolt_001')

# Cast Lightning Bolt at the opponent
send_ap({'type': 'CAST_SPELL', 'seq_num': main_grant['seq_num'], 'card_id': 'lightning_bolt_001',
         'targets': [other_id], 'mana_payment': {'R': 1}})
push_ap = recv_ap(); recv_nap()
assert push_ap['type'] == 'STACK_PUSH' and push_ap['source'] == 'lightning_bolt_001'
assert mountain.is_tapped is True
retain = recv_ap()
assert retain['type'] == 'PRIORITY_GRANT' and retain['player_id'] == active_id

# Both pass -> the spell resolves
send_ap({'type': 'PRIORITY_PASS', 'seq_num': retain['seq_num']})
g5 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g5['seq_num']})
resolve_ap = recv_ap(); recv_nap()
assert resolve_ap['type'] == 'STACK_RESOLVE' and resolve_ap['result'] == 'RESOLVED'
recv_ap(); recv_nap()
final_grant = recv_ap()
assert final_grant['type'] == 'PRIORITY_GRANT' and final_grant['player_id'] == active_id

print('Lobby -> Setup -> Mulligan -> real turn -> cast + resolve a spell: ALL CHECKS PASSED')
"
```

### 2. Manual two-terminal smoke test (transport layer only)

```
# Terminal 1
python -m network.server --verbose

# Terminal 2
python -m network.client --verbose
```

In the client terminal, press **Enter** to send a `PING` — you should see a matching `PONG`
logged on both sides. Type `q` + Enter to disconnect. Remember: the client doesn't do anything
beyond this yet (see "There is no playable client yet" above).

### 3. Connection-limit test (3rd client is refused)

Open a **3rd** terminal while the two above are still connected and run
`python -m network.client --verbose` again. The server log should show
`refusing extra connection ...` and the 3rd client's socket is closed
immediately — this exercises the RFC §5.1 "server MUST accept exactly two
clients ... additional attempts MUST be refused" requirement.

### 4. Model-layer unit checks

The model classes can also be exercised directly, without any networking:

```
python -c "
from model.player import player
from model.card_database import card_database

p = player('Alice', 'player_1')
p.initialize_library(['mountain_001', 'lightning_bolt_001'])
print('library:', p.library)
print('total cards in the fixed card set:', len(card_database.CARD_DATABASE))
"
```
Expect `library` to contain the two card IDs (shuffled) and the card set
total to print `312`, matching `docs/mtgnp_master_card_list.xlsx`.


### 5. Combat Test

```
python -c "
import socket, json, struct, threading, time
from network.server import GameServer
from model.card_database import card_database

server = GameServer('127.0.0.1', 47010, verbose=False)
threading.Thread(target=server.start, daemon=True).start()
time.sleep(0.3)

def client():
    s = socket.create_connection(('127.0.0.1', 47010), timeout=5)
    def send(pdu):
        p = json.dumps(pdu).encode()
        s.sendall(struct.pack('>I', len(p)) + p)
    def recv():
        (n,) = struct.unpack('>I', s.recv(4))
        return json.loads(s.recv(n).decode())
    return send, recv

send1, recv1 = client()
send2, recv2 = client()
deck = ['mountain_001', 'mountain_002', 'mountain_003', 'mountain_004']

send1({'type': 'PLAYER_READY', 'seq_num': 1, 'player_id': 'player_1', 'deck_list': deck})
recv1()
send2({'type': 'PLAYER_READY', 'seq_num': 1, 'player_id': 'player_2', 'deck_list': deck})
setup1, setup2 = recv1(), recv2()

send1({'type': 'MULLIGAN_CHOICE', 'seq_num': setup1['seq_num'], 'keep': True, 'cards_to_bottom': []})
send2({'type': 'MULLIGAN_CHOICE', 'seq_num': setup2['seq_num'], 'keep': True, 'cards_to_bottom': []})

active_id = None
for r in (recv1, recv2):
    t = r()
    active_id = t['active_player']
recv1(); recv2()   # Untap Step's GAME_STATE_UPDATE
recv1(); recv2()   # UNTAP -> UPKEEP

send_ap, recv_ap = (send1, recv1) if active_id == 'player_1' else (send2, recv2)
send_nap, recv_nap = (send2, recv2) if active_id == 'player_1' else (send1, recv1)
opponent = server.game_state.opponent_of(active_id)
other_id = opponent.player_id
active = server.game_state.players[active_id]

g = recv_ap(); send_ap({'type': 'PRIORITY_PASS', 'seq_num': g['seq_num']})
g2 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g2['seq_num']})
recv_ap(); recv_nap()
g3 = recv_ap(); send_ap({'type': 'PRIORITY_PASS', 'seq_num': g3['seq_num']})
g4 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g4['seq_num']})
recv_ap(); recv_nap()
main_grant = recv_ap()

# Attacker: troll_ascetic (power 3, toughness 2)
attacker_creature = card_database.CARD_DATABASE['troll_ascetic_001']
attacker_creature.is_tapped = False
attacker_creature.summoning_sick = False
attacker_creature.damage_marked = 0
active.board.append(attacker_creature)

# Blocker A: llanowar_elves (power 1, toughness 1) -- fragile
blockerA = card_database.CARD_DATABASE['llanowar_elves_001']
blockerA.is_tapped = False
blockerA.damage_marked = 0
opponent.board.append(blockerA)

# Blocker B: leatherback_baloth (power 4, toughness 5) -- beefy
blockerB = card_database.CARD_DATABASE['leatherback_baloth_001']
blockerB.is_tapped = False
blockerB.damage_marked = 0
opponent.board.append(blockerB)

send_ap({'type': 'PRIORITY_PASS', 'seq_num': main_grant['seq_num']})
g6 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g6['seq_num']})
t_begin_ap = recv_ap(); t_begin_nap = recv_nap()

g7 = recv_ap(); send_ap({'type': 'PRIORITY_PASS', 'seq_num': g7['seq_num']})
g8 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g8['seq_num']})
t_declare_ap = recv_ap(); t_declare_nap = recv_nap()

send_ap({'type': 'DECLARE_ATTACKERS', 'seq_num': t_declare_ap['seq_num'],
         'attackers': [{'creature_id': 'troll_ascetic_001', 'target': other_id}]})
recv_ap(); recv_nap()
declare_attackers_prio = recv_ap()

send_ap({'type': 'PRIORITY_PASS', 'seq_num': declare_attackers_prio['seq_num']})
g9 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g9['seq_num']})
t_blockers_ap = recv_ap(); t_blockers_nap = recv_nap()

# NAP declares BOTH blockers on the same attacker -- leatherback listed FIRST,
# llanowar SECOND, so the natural blocked_by append order is [leatherback, llanowar].
send_nap({'type': 'DECLARE_BLOCKERS', 'seq_num': t_blockers_nap['seq_num'],
          'blockers': [
              {'creature_id': 'leatherback_baloth_001', 'blocking_id': 'troll_ascetic_001'},
              {'creature_id': 'llanowar_elves_001', 'blocking_id': 'troll_ascetic_001'},
          ]})
recv_ap(); recv_nap()
assert len(attacker_creature.blocked_by) == 2
assert attacker_creature.blocked_by[0] is blockerB and attacker_creature.blocked_by[1] is blockerA
declare_blockers_prio = recv_ap()

# Two blockers -> ASSIGN_DAMAGE_ORDER must NOT be skipped.
send_ap({'type': 'PRIORITY_PASS', 'seq_num': declare_blockers_prio['seq_num']})
g10 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g10['seq_num']})
t_order_ap = recv_ap(); t_order_nap = recv_nap()
assert t_order_ap['to_phase'] == 'ASSIGN_DAMAGE_ORDER', t_order_ap

# Submit the REVERSE of the natural declare order: llanowar first, leatherback second.
send_ap({'type': 'ASSIGN_DAMAGE_ORDER', 'seq_num': t_order_ap['seq_num'],
         'attacker_id': 'troll_ascetic_001',
         'blocker_order': ['llanowar_elves_001', 'leatherback_baloth_001']})
ad_grant = recv_ap()
assert attacker_creature.damage_order == [blockerA, blockerB], \
    'ASSIGN_DAMAGE_ORDER did not override the natural blocked_by order!'

send_ap({'type': 'PRIORITY_PASS', 'seq_num': ad_grant['seq_num']})
g11 = recv_nap(); send_nap({'type': 'PRIORITY_PASS', 'seq_num': g11['seq_num']})
t_dmg_ap = recv_ap(); t_dmg_nap = recv_nap()
assert t_dmg_ap['to_phase'] == 'COMBAT_DAMAGE'

result_ap = recv_ap(); result_nap = recv_nap()
died = set(result_ap['creatures_died'])

# With order [llanowar(1), leatherback(5)] and attacker power 3:
#   llanowar takes 1 (lethal, dies), remaining 2 spills to leatherback (survives, dmg=2).
# Had the natural order [leatherback, llanowar] been used instead, leatherback would
# have absorbed all 3 (survives) and llanowar would take 0 (survives) -- a DIFFERENT
# outcome. So this assertion proves ASSIGN_DAMAGE_ORDER's value was actually honored.
assert died == {'llanowar_elves_001', 'troll_ascetic_001'}, died
assert blockerB.damage_marked == 2 and blockerB in opponent.board

recv_ap(); recv_nap()   # post-damage GAME_STATE_UPDATE
t_eoc_ap = recv_ap(); t_eoc_nap = recv_nap()
assert t_eoc_ap['to_phase'] == 'END_OF_COMBAT'
eoc_prio = recv_ap()
assert eoc_prio['type'] == 'PRIORITY_GRANT'

print('ASSIGN_DAMAGE_ORDER: reversed order honored, correct creature died: PASSED')
"
```

## What's left

Roughly in the order it makes sense to build it:

1. **Combat** — Declare Attackers, Declare Blockers, damage assignment, first strike, combat damage.
2. **`PLAY_LAND`** — the one Main Phase action that still doesn't exist.
3. **Cleanup's discard step** — the turn engine already stops and waits correctly when a hand
   exceeds 7 cards; nothing processes the resulting `DISCARD` PDU yet.
4. **`CONCEDE` / `GAME_OVER` / LOBBY restart** — state-based actions already *detect* a loss
   condition; nothing broadcasts `GAME_OVER` or resets the server back to LOBBY yet.
5. **Priority timeouts and reconnect handling.**
6. **Full `ERROR` code coverage** for every situation the RFC lists.
7. **A real client** — actually sending `PLAYER_READY`/`CAST_SPELL`/etc. and rendering the game
   state a human can read, instead of the current `PING`-only demo.
8. **Card effects** — at least 5 real ones for rubric credit (Lightning Bolt's damage, etc.), all
   58 for the bonus. Right now casting/resolving a spell or ability is structurally correct but
   doesn't change the game state.
9. **Concurrency review** of any new shared state the remaining milestones add.
10. **A full interoperability run-through** matching `docs/Examples.pdf` end to end.
