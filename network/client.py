import argparse
import json
import socket
import threading
import time

from network import protocol
from network.protocol import (Connection, ConnectionClosed, ProtocolError, log)
from model.card_database import card_database
from network import pdu as pdu_builders

seq_num = 1

# Client-side state to track the last received sequence numbers for various events, 
# as well as the player's hand and mulligan count.
client_state = {
    "player_id": None,
    "hand": [],
    "mulligan_count": 0,
    "last_game_state": None,
    "last_game_state_update_seq": None,
    "last_priority_grant_seq": None,
    "last_phase_transition_seq": None,
    "last_any_server_seq": None,
}

def _handle_game_state_update(conn: Connection, pdu: dict) -> None:
    state = pdu.get("state", {})
    client_state["last_game_state"] = pdu
    client_state["last_game_state_update_seq"] = pdu.get("seq_num")
    phase = state.get("phase")

    if phase == "LOBBY":
        render_lobby_cli(state)

    elif phase == "MULLIGAN":
        render_mulligan_cli(state, client_state["player_id"])

        # remember last game state so the interactive prompt can act accordingly (e.g. for mulligan decisions)
        client_state["last_game_state"] = pdu
        client_state["last_game_state_update_seq"] = pdu.get("seq_num")

    elif phase == "UNTAP":
        render_in_game_cli(state, client_state["player_id"])

    else:
        log(f"[client] GAME_STATE_UPDATE with unhandled phase '{phase}': {state}")


def handle_mulligan_command(conn: Connection, parts: list) -> None:
    """Handle the 'mulligan' command from the interactive CLI."""
    if len(parts) < 2:
        log("[client] usage: mulligan <mull|keep> [auto|<card_ids>]")
        return

    verb = parts[1].lower()

    player_id = client_state.get("player_id")

    if player_id is None:
        log("[client] you must `ready <player_id>` first")
        return

    if client_state["last_game_state"] is None:
        log("[client] no game state available yet")
        return

    # takes the current hand from the last game state update, which is used to determine which cards can be bottomed during a mulligan decision.
    state = client_state["last_game_state"].get("state", {})
    hand_map = state.get("hand", {})
    hand = hand_map.get(player_id, [])

    if verb in ("mull", "mulligan"):
        # send keep=False
        send_mulligan_choice(conn, keep=False, cards_to_bottom=[])
        log(f"[client] sent MULLIGAN_CHOICE keep=False")
        return

    elif verb == "keep":
        # Case 1: Player chose automatic selection for cards to put at the bottom
        if len(parts) >= 3 and parts[2].lower() == "auto":
            mulligan_count = client_state["mulligan_count"]
            cards_to_bottom = hand[:mulligan_count]
            send_mulligan_choice(conn, keep=True, cards_to_bottom=cards_to_bottom)
            log(f"[client] sent MULLIGAN_CHOICE keep=True auto bottom {cards_to_bottom}")

        # Case 2: Player explicitly listed specific cards to put at the bottom
        elif len(parts) >= 3:
            cards = parts[2:]
            send_mulligan_choice(conn, keep=True, cards_to_bottom=cards)
            log(f"[client] sent MULLIGAN_CHOICE keep=True bottom {cards}")

        # Case 3: Player hasn't mulliganed (0 count), so no cards need to go to the bottom
        elif client_state["mulligan_count"] == 0:
            send_mulligan_choice(conn, keep=True, cards_to_bottom=[])
            log("[client] sent MULLIGAN_CHOICE keep=True (no cards to bottom)")

        # Case 4: Player has mulliganed but didn't specify cards to bottom
        else:
            log("[client] must specify cards to bottom or use 'mulligan keep auto'")

    else:
        log(f"[client] unknown mulligan verb '{verb}' -- use 'mull' or 'keep'")

    print("[client] > ", end="", flush=True)

def _handle_phase_transition(conn: Connection, pdu: dict) -> None:
    log(f"[client] PHASE_TRANSITION: {pdu.get('from_phase')} -> {pdu.get('to_phase')} "
        f"(active_player={pdu.get('active_player')} turn={pdu.get('turn')})")

    client_state["last_phase_transition_seq"] = pdu.get("seq_num")

    to_phase = pdu.get("to_phase")
    active_player = pdu.get("active_player")
    my_id = client_state["player_id"]

    if to_phase == "DECLARE_ATTACKERS" and active_player == my_id:
        state = client_state["last_game_state"].get("state", {})
        battlefield = state.get("battlefield", {}).get(my_id, [])
        log(f"[client] your battlefield: {battlefield}")
        log("[client] type: attack <id1> <id2> ...  (or just 'attack' for none)")

    elif to_phase == "DECLARE_BLOCKERS" and active_player != my_id:
        state = client_state["last_game_state"].get("state", {})
        battlefield = state.get("battlefield", {}).get(my_id, [])
        log(f"[client] your battlefield: {battlefield}")
        log("[client] type: block <attacker_id> <blocker_id>  (or just 'block' for none)")


def _handle_priority_grant(conn: Connection, pdu: dict) -> None:
    log(f"[client] PRIORITY_GRANT: {pdu.get('player_id')} has priority "
        f"for {pdu.get('time_limit_ms')}ms")

    if client_state["player_id"] == pdu.get("player_id"):
        client_state["has_priority"] = True
        client_state["last_priority_grant_seq"] = pdu.get("seq_num")

    print("[client] > ", end="", flush=True)

def _handle_stack_push(conn: Connection, pdu: dict) -> None:
    log(f"[client] STACK_PUSH: {pdu}")

def _handle_trigger_order(conn: Connection, pdu: dict) -> None:
    log(f"[client] TRIGGER_ORDER: {pdu}")

def _handle_trigger_choice(conn: Connection, pdu: dict) -> None:
    log(f"[client] TRIGGER_CHOICE: {pdu}")

def _handle_stack_resolve(conn: Connection, pdu: dict) -> None:
    log(f"[client] STACK_RESOLVE: {pdu}")

def _handle_combat_damage_result(conn: Connection, pdu: dict) -> None:
    log(f"[client] COMBAT_DAMAGE_RESULT: {pdu}")

def _handle_game_over(conn: Connection, pdu: dict) -> None:
    # Since the game is over, a fresh match will be created and fields should'nt carry over.
    # However, it is still worth resetting these fields to ensure that the client is in a 
    # clean state for the next match.
    client_state["mulligan_count"] = 0
    client_state["has_priority"] = False

    log(f"[client] GAME_OVER: winner={pdu.get('winner_id')} "
        f"loser={pdu.get('loser_id')} reason={pdu.get('reason')}")
    log("[client] returning to LOBBY -- type 'ready <player_id>' to start a new match")

def _handle_error(conn: Connection, pdu: dict) -> None:
    log(f"[client] ERROR: {pdu.get('code')} - {pdu.get('message')}")

def _handle_pong(conn: Connection, pdu: dict) -> None:
    now = int(time.time() * 1000)
    rtt = now - pdu["timestamp"]
    log(f"[client] PONG received (seq_num={pdu['seq_num']}), rtt={rtt}ms")
    print("[client] > ", end="", flush=True)


# IN_GAME PDU send helpers
def send_priority_pass(conn: Connection) -> None:
    seq = client_state["last_priority_grant_seq"]
    if seq is None:
        log("[client] no PRIORITY_GRANT recorded -- cannot pass priority")
        return
    conn.send_pdu(pdu_builders.build_priority_pass(seq))
    client_state["has_priority"] = False


def send_cast_spell(conn: Connection, card_id: str, targets: list) -> None:
    seq = client_state["last_priority_grant_seq"]
    if seq is None:
        log("[client] no PRIORITY_GRANT recorded -- cannot cast spell")
        return
    conn.send_pdu(pdu_builders.build_cast_spell(seq, card_id, list(targets), {}))


def send_play_land(conn: Connection, card_id: str) -> None:
    seq = client_state["last_priority_grant_seq"]
    if seq is None:
        log("[client] no PRIORITY_GRANT recorded -- cannot play land")
        return
    conn.send_pdu(pdu_builders.build_play_land(seq, card_id))
    log(f"[client] sent PLAY_LAND {card_id}")

def send_declare_attackers(conn: Connection, attackers: list) -> None:
    seq = client_state["last_phase_transition_seq"]
    if seq is None:
        log("[client] no PHASE_TRANSITION recorded -- cannot declare attackers")
        return
    conn.send_pdu(pdu_builders.build_declare_attackers(seq, attackers))


def send_declare_blockers(conn: Connection, blockers: list) -> None:
    seq = client_state["last_phase_transition_seq"]
    if seq is None:
        log("[client] no PHASE_TRANSITION recorded -- cannot declare blockers")
        return
    conn.send_pdu(pdu_builders.build_declare_blockers(seq, blockers))


def send_concede(conn: Connection) -> None:
    seq = client_state["last_any_server_seq"]
    if seq is None:
        log("[client] no server PDU recorded yet -- cannot concede")
        return
    pid = client_state["player_id"]
    conn.send_pdu(pdu_builders.build_concede(seq, pid))


def render_in_game_cli(state: dict, my_id: str) -> None:
    width = 80
    turn = state.get("turn")
    phase = state.get("phase")
    active = state.get("active_player")
    life = state.get("life_totals", {})
    hand = state.get("hand", {}).get(my_id, []) if my_id else []
    battlefield = state.get("battlefield", {})

    log("=" * width)
    log("IN_GAME".center(width))
    log("=" * width)
    log(f" Turn {turn}  |  Phase: {phase}  |  Active: {active}")
    log(f" Life totals: {life}")
    log(f" Your hand ({len(hand)}): {', '.join(hand)}")
    log(f" Battlefield: {battlefield}")
    log("-" * width)
    log(" Commands (in-game):")
    log("  pass                       -- send PRIORITY_PASS")
    log("  cast <card_id> [targets...] -- cast a spell with optional targets")
    log("  playland <card_id>         -- play a land from your hand")
    log("  attack <creature_ids...>   -- declare attackers (space-separated)")
    log("  block <attacker:blocker ...>-- declare blockers as pairs attacker:blocker")
    log("  concede                    -- concede the game")
    log("  state                      -- print last GAME_STATE_UPDATE")
    log("  phase                      -- print current phase")
    log("  help                       -- show commands")
    log("=" * width)
    print("[client] > ", end="", flush=True)


def _build_sample_deck(limit: int = 50) -> list:
    deck_ids = list(card_database.CARD_DATABASE.keys())
    if len(deck_ids) < limit:
        limit = len(deck_ids)
    return deck_ids[:limit]


def receive_loop(conn: Connection, stop_event: threading.Event) -> None:
    running = True

    while running and not stop_event.is_set():
        try:
            response = conn.recv_pdu()
        except ConnectionClosed:
            log("[client] connection closed by server")
            running = False
        except ProtocolError as exc:
            log(f"[client] protocol error during receive: {exc}")
            running = False
        except OSError as exc:
            log(f"[client] socket error during receive: {exc}")
            running = False

        client_state["last_any_server_seq"] = response.get("seq_num")

        handler = _handlers.get(response["type"])
        if handler is None:
            log(f"[client] Unknown pdu type {response['type']}!")
        else:
            handler(conn, response)


# Handler table to call the corresponding method of each pdu type
_handlers = {
    "GAME_STATE_UPDATE" : _handle_game_state_update,
    "PHASE_TRANSITION" : _handle_phase_transition,
    "PRIORITY_GRANT" : _handle_priority_grant,
    "STACK_PUSH" : _handle_stack_push,
    "TRIGGER_ORDER" : _handle_trigger_order,
    "TRIGGER_CHOICE" : _handle_trigger_choice,
    "STACK_RESOLVE" : _handle_stack_resolve,
    "COMBAT_DAMAGE_RESULT" : _handle_combat_damage_result,
    "GAME_OVER" : _handle_game_over,
    "ERROR" : _handle_error,
    "PONG" : _handle_pong
}

# Dummy PDU for echo testing
def run_self_test(conn: Connection) -> None:
    dummy = {
        "type": "PLAYER_READY",
        "seq_num": 1,
        "player_id": "player_1",
        "deck_list": ["test_01", "test_02", "test_03"],
    }
    log("[client] self-test: sending dummy PLAYER_READY ...")
    conn.send_pdu(dummy)
    echo = conn.recv_pdu()
    if echo == dummy:
        log("[client] self-test PASSED: GameServer echoed an identical PDU")
    else:
        log("[client] self-test FAILED: echo did not match what was sent")
        log(f"          sent: {dummy}")
        log(f"          got:  {echo}")


def interactive_loop(conn: Connection) -> None:
    ping_seq = 1
    while True:
        try:
            # "[client] >" is printed inside the CLI methods because this input() is called first
            # before the CLI methods are called, so the "[client] > " prompt is printed after the CLI output. 
            command = input()
        except EOFError: # e.g. stdin closed
            break

        normalized = command.strip().lower()

        if not normalized:
            continue

        if normalized == "ping":
            ping = {"type": "PING", "seq_num": ping_seq,
                    "timestamp": int(time.time() * 1000)}
            conn.send_pdu(ping)
            log("[client] Sent PING")
            ping_seq += 1
            continue

        if normalized in ("q", "quit", "exit"):
            break

        if normalized.startswith("ready"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: ready <player_id>")
                print("[client] > ", end="", flush=True)
                continue
            player_id = parts[1]
            deck_list = _build_sample_deck(50)
            log(f"[client] sending PLAYER_READY player_id={player_id} deck_size={len(deck_list)}")

            client_state["player_id"] = player_id
            send_player_ready(conn, player_id, deck_list)
            continue

        if normalized.startswith("mulligan"):
            parts = command.strip().split()
            handle_mulligan_command(conn, parts)
            continue

        # In-game commands
        if normalized == "pass" or normalized == "priority_pass":
            send_priority_pass(conn)
            log("[client] sent PRIORITY_PASS")
            continue

        if normalized.startswith("cast"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: cast <card_id> [target1 target2 ...]")
                continue
            card_id = parts[1]
            targets = parts[2:]
            send_cast_spell(conn, card_id, targets)
            log(f"[client] sent CAST_SPELL {card_id} targets={targets}")
            continue

        if normalized.startswith("playland"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: playland <card_id>")
                continue
            send_play_land(conn, parts[1])
            continue

        if normalized.startswith("attack"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: attack <creature_id> [more_ids...]")
                continue
            attackers = [{"creature_id": cid, "target": None} for cid in parts[1:]]
            send_declare_attackers(conn, attackers)
            log(f"[client] sent DECLARE_ATTACKERS {attackers}")
            continue

        if normalized.startswith("block"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: block attacker:blocker [more pairs...]")
                continue
            blockers = []
            for pair in parts[1:]:
                if ":" not in pair:
                    log(f"[client] invalid pair '{pair}', must be attacker:blocker")
                    continue
                atk, blk = pair.split(":", 1)
                blockers.append({"creature_id": atk, "blocking_id": blk})
            send_declare_blockers(conn, blockers)
            log(f"[client] sent DECLARE_BLOCKERS {blockers}")
            continue

        if normalized == "concede":
            send_concede(conn)
            log("[client] sent CONCEDE")
            continue

        if normalized == "state":
            log(f"[client] last GAME_STATE_UPDATE: {client_state.get('last_game_state')}")
            continue

        if normalized == "phase":
            gs = client_state.get('last_game_state')
            if gs:
                log(f"[client] current phase: {gs.get('state', {}).get('phase')}")
            else:
                log('[client] no game state yet')
            continue

        if normalized == "help":
            log("[client] commands:")
            log("  ping                  send a PING")
            log("  ready <player_id>     send PLAYER_READY with a sample deck")
            log("  mulligan mull         redraw (keep=false)")
            log("  mulligan keep auto    keep and bottom N random cards")
            log("  mulligan keep <ids>   keep and bottom the listed card ids")
            log("  q                     quit")
            print("[client] > ", end="", flush=True)
            continue

        log(f"[client] unknown command: {command}")
        print("[client] > ", end="", flush=True)
        continue


def send_player_ready(conn: Connection, player_id: str, deck_list: list) -> None:
    global seq_num

    conn.send_pdu({
        "type": "PLAYER_READY", 
        "seq_num": seq_num,
        "player_id" : player_id,
        "deck_list": deck_list
    })

    # locally remember we are this player (might use when server replies)
    client_state["player_id"] = player_id

    seq_num += 1


def send_mulligan_choice(conn: Connection, keep: bool, cards_to_bottom: list) -> None:
    """Send a MULLIGAN_CHOICE PDU echoing the last GAME_STATE_UPDATE seq_num as per RFC 5.4"""
    seq = client_state["last_game_state_update_seq"]

    if seq is None:
        log("[client] no game state update recorded yet -- cannot send MULLIGAN_CHOICE")
        return

    conn.send_pdu({
        "type": "MULLIGAN_CHOICE",
        "seq_num": seq,
        "keep": bool(keep),
        "cards_to_bottom": list(cards_to_bottom),
    })

    if not keep:
        client_state["mulligan_count"] += 1


def receive_and_handle(conn: Connection) -> None:
    # receives a PDU from the connection, logs it, and dispatches it to the appropriate handler based on its type.
    response = conn.recv_pdu()

    handler = _handlers.get(response["type"])
    if handler is None:
        log(f"[client] Unknown pdu type {response['type']}!")
    else:
        handler(conn, response)

def main() -> None:
    parser = argparse.ArgumentParser(description="MTGNP Player Client (Milestone 0)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="GameServer host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"GameServer TCP port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU sent and received")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((args.host, args.port))
    except OSError as exc:
        log(f"[client] could not connect to {args.host}:{args.port}: {exc}")
        return

    conn = Connection(sock, local="CLIENT", peer="SERVER", verbose=args.verbose)
    stop_event = threading.Event()
    receiver = threading.Thread(target=receive_loop, args=(conn, stop_event), daemon=True)

    render_welcome_cli(args.host, args.port)
    receiver.start()
    try:
        interactive_loop(conn)
    except ConnectionClosed:
        log("[client] GameServer closed the connection (it may already be full)")
    except ProtocolError as exc:
        log(f"[client] protocol error: {exc}")
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        stop_event.set()
        conn.close()
        receiver.join(timeout=1.0)
        log("[client] disconnected")


def render_welcome_cli(host: str, port: int) -> None:
    width = 60
    log("=" * width)
    log("MTGNP CLIENT".center(width))
    log("=" * width)
    log(f" Connected to server: {host}:{port}")
    log("-" * width)
    log(" Commands:")
    log("   ready <player_id>   -- join with a sample deck")
    log("   ping                -- send a heartbeat PING")
    log("   help                -- show all commands")
    log("   q                   -- quit")
    log("=" * width)
    log(" Waiting in LOBBY -- type 'ready <player_id>' to begin")
    print("[client] > ", end="", flush=True)    


def render_lobby_cli(state: dict) -> None:
    width = 60
    ready = state.get("players_ready")
    waiting_for = state.get("waiting_for", [])

    log("=" * width)
    log("LOBBY".center(width))
    log("=" * width)
    log(f" Players ready: {ready}/2")
    if waiting_for:
        log(f" Waiting on: {', '.join(waiting_for)}")
    else:
        log(" All players ready -- starting game...")
    log("-" * 60)
    log(" Commands:")
    log("   ready <player_id>   -- send PLAYER_READY with a sample deck")
    log("=" * 60)
    print("[client] > ", end="", flush=True)


def render_mulligan_cli(state: dict, my_id: str) -> None:
    """Display the current mulligan-phase state to the player."""
    width = 60
    hand = state.get("hand", {}).get(my_id, [])
    mulligan_count = client_state["mulligan_count"]

    log("=" * width)
    log("MULLIGAN PHASE".center(width))
    log("=" * width)
    log(f" Turn {state.get('turn')}  |  Active player: {state.get('active_player')}")
    log(f" You have mulliganed {mulligan_count} time(s)")
    log(f" Your hand ({len(hand)}): {', '.join(hand) if hand else '(empty)'}")
    log("-" * width)
    log(" Commands:")
    log("   mulligan mull            -- take a mulligan, draw a fresh 7")
    log("   mulligan keep auto       -- keep, auto-bottom the required cards")
    log("   mulligan keep <card_ids> -- keep, bottom these specific cards")
    log("=" * width)
    print("[client] > ", end="", flush=True)


if __name__ == "__main__":
    main()
