# CSNETWK - Magic: The Gathering Network Protocol (MTGNP)

A client/server implementation of Magic: The Gathering Network Protocol (MTGNP), a 2-player Magic: The Gathering (MTG) engine played over a TCP connection and JSON PDUs.

**By:** Agraviador, Nash, Tongco, Amiel, Vanguardia, Nykko, & Villafuerte, Mark Justin
**Date:** August 12, 2026  

---

## Overview

MTGNP is a server-authoritative protocol wherein the server holds the single source of truth, be it data, computations, and game states such as hands, battlefield, and life totals. The server also holds the responsibility in validating every action a client sends before performing the respective actions. Clients never modify the game state directly; instead, they send an action PDU and the server decides whether to apply it, or reject it with a corresponding ERROR PDU.

Every message is a JSON object that is being sent over a plain TCP socket, framed with a 4-byte big-endian length prefix, and encoded in UTF-8. The message length prefix allows each message to be sent and received in whole, removing ambiguity.

Only two clients may connect to one server at a time and be processed for a match. The following is the fixed game lifecycle that will be administered by the server:
```
LOBBY --> GAME_SETUP --> MULLIGAN --> IN_GAME --> GAME_OVER
  ^                                                   |
  +---------------------------------------------------+
```

Moreover, the IN_GAME phase follows the standard MTG structure as follows:
```
       UNTAP STEP
           |
      UPKEEP STEP
           |
       DRAW STEP
           |
   PRECOMBAT MAIN PHASE
           |
      COMBAT PHASE
           |
  POSTCOMBAT MAIN PHASE
           |
        END STEP
           |
      CLEANUP STEP
```
A priority window will be opened at each step where players may decide to perform an action. To detect stale or out-of-order client actions, a `seq_num` is used on every PDU

## Requirements

-   Python 3.10 or newer. Standard library only; no third-party packages.
    
## Project Structure
```
.
├── model/
│ ├── artifact.py
│ ├── card_database.py
│ ├── card.py
│ ├── creature.py
│ ├── enchantment.py
│ ├── game_state.py
│ ├── instant.py
│ ├── land.py
│ ├── lifecycle.py
│ ├── mana.py
│ ├── phase.py
│ ├── player.py
│ ├── sorcery.py
│ ├── stack.py
│ └── triggers.py
└── network/
├── client.py
├── pdu.py
├── protocol.py
└── server.py
```
## Build & Run
`server.py` and `client.py` live inside the network/ package and use package-relative imports (`from network import protocol`, `from model.player import player`, etc.), so they must be launched with `python -m` from the project root — running them as plain scripts (`python network/server.py`) fails with `ModuleNotFoundError: No module named 'network'`.

### Server

From the project root,
```
 # Start the server
 python \-m network.server
```

**Optional Flags:**

```
python -m network.server [--host 0.0.0.0] [--port 4444] [-v | --verbose]
```

The server accepts exactly 2 player connections and a 3rd connection attempt will be refused. Connections are reused across multiple matches, that is, the server returns to LOBBY state after `GAME_OVER` (by concede, life-total loss, disconnection, etc.) rather than closing the socket. Hence, a rematch may be made without both players reconnecting.

To connect across two machines on a LAN, leave the server on `--host 0.0.0.0` and start the client with `--host <server-ip>`.

### Client

From the project root in a separate terminal (one per player),

```
# Start a client
python -m network.client
```

**Optional Flags:**

```
python -m network.client [--host 127.0.0.1] [--port 4444] [-v | --verbose]
```

Once a client is connected, type `help` at the client prompt for the full list of in-game commands (`ready`, `mulligan`, `pass`, `cast`, `playland`, `activate`, `attack`, `block`, `damageorder`, `order`, `trigger`, `discard`, `concede`, `state`, `phase`, `q`). 

## Enabling verbose mode

Pass `-v` / `--verbose` to either program. 

```
# Start the server (verbose)
python -m network.server --verbose

# Start a client (verbose)
python -m network.client --verbose
```
Every PDU sent and received is printed with a timestamp, a source \-\> destination tag, and the indented JSON payload, for example:
```
[18:55:11.527] [SERVER -> P1]
{
  "type": "GAME_STATE_UPDATE",
  "seq_num": 1,
  "state": {
    "phase": "LOBBY",
    "players_ready": 0,
    "waiting_for": [
      "P1"
    ]
  }
}
```
```
[18:55:44.913] [P1 -> SERVER]
{
  "type": "PLAYER_READY",
  "seq_num": 1,
  "player_id": "l",
  "deck_list": [...]
  …
}
```
Without the verbose flag, `-v`, only the human-readable game log for phase transitions, board state, and errors are shown. The raw PDU will not be displayed. Verbose mode is independent on the client and server. It can be enabled on one, both, or neither.

### Example Session
Start the server, then connect two clients into ready state (in two more terminals).

**Server:**
```
python -m network.server -v
```

**Client 1:**
```
python -m network.client
[client] > ready alice
```

**Client 2:**
```
python -m network.client
[client] > ready bob
```

Once both players have readied up, the server deals opening hands and both clients enter the MULLIGAN phase:
```
[client] > mulligan keep auto
```

Once both players keep, the match begins. A typical turn for the active player might look like:
```
[client] > playland mountain_001
[client] > cast lightning_bolt_002 opponent
[client] > pass
```
When the opponent has priority, they can respond or pass to let the turn continue:
```
[client] > pass
```
When combat comes around, the active player is prompted automatically to declare attackers, and the defending player to declare blockers:

```
[client] > attack creature_001
[client] > block creature_001:blocker_002
```

At any point, either player can concede to end the match and return both clients to the LOBBY for a rematch:
```
[client] > concede
```

Type `help` at any time to see the full list of available commands, or `state` / `phase` to print the last known game state / current phase for debugging.

## Work Distribution Matrix

The table below shows the contribution of each member towards the implementation of this project:
| Task / Feature | Agraviador, Nash | Tongco, Amiel | Vanguardia, Nykko | Villafuerte, Mark Justin |
| ----- | :---: | :---: | :---: | :---: |
| TCP Server: connection handling, framing, dispatch | ✅ | ✅ |  |  |
| Game lifecycle: LOBBY, GAME\_SETUP, MULLIGAN logic | ✅ | ✅ |  | ✅ |
| Turn & phase engine (all phases/steps, transitions) |  | ✅ | ✅ | ✅ |
| Priority & Stack logic, spell/ability resolution | ✅ |  | ✅ | ✅ |
| Combat system (attackers, blockers, damage) |  |  | ✅ | ✅ |
| Client implementation & state rendering |  | ✅ |  | ✅ |
| PDU serialisation/deserialisation (all 25 PDU types) | ✅ |  |  |  |
| Error handling, PING/PONG heartbeat, disconnect logic |  | ✅ | ✅ |  |
| Verbose mode (client \+ server PDU logging, toggle on/off) | ✅ |  |  |  |
| Testing & interoperability |  |  | ✅ | ✅ |
| README / documentation / AI disclosure |  | ✅ | ✅ |  |
| Card effects (27 implemented, 8 partial, 23 unimplemented) | ✅ |  |  |  |

## AI Usage Disclosure

ChatGPT was used to generate a combat test script for regression testing. Also, Claude was used for assistance with some parts of the stylesheet for this README document for formatting purposes only. Claude was also used in generating test scripts for thorough bash-testing of the flow of the program.

Every line of AI-generated code, whether related to the code or not, was reviewed and manually tested by the members to ensure adherence to the project specifications. Despite the group’s use of AI in their work, the members take full responsibility for the final logic, integrity, and deliverables.

## Known Limitations / Deviations from RFC

### Limitations
- **Command-Line Interface (CLI)** \- The client relies on CLI to render game updates. In a card-based game such as MTG, it would be more beneficial to have a graphical user interface to display more information. Moreover, having an interface based only on texts, frequent sending of messages would essentially flood the client’s screen.  
- **Connection Scalability** \- This program is specifically designed for a 1v1 play. Hence, only 2 clients may join one server where additional client connection attempts will be rejected. An alternative to this implementation is to add a queue so the next connected client/s would be up for play. This could also open up for spectatorship during a game.  
- **Fixed Port Configuration** \- The server port is set to `4444` by default and requires a command-line argument to be set manually.  
- **Unencrypted Sockets** \- The data or payload are being transmitted as plain-text JSON frames over a raw TCP connection without utilizing any sort of encryption.
- **(Bonus) Partial Card Effect Implementation** \- Only around half of the card effects were implemented for the sake of the demo.

### Assumptions
- **Empty Stack on PLAY\_LAND** - PLAY\_LAND is required to have an empty stack as inferred from the RFC's "sorcery speed for AP" rather than an explicit statement. This assumption was also based on Rule 305.1 of MTG.
- **Connection Labels** - LOBBY `GAME_STATE_UPDATE`'s `waiting_for` field uses connection labels (P1/P2) for any player who hasn't yet sent `PLAYER_READY`, since their eventual `player_id` is unknown to the server until that PDU arrives.