import argparse
import json
import socket
import threading
import time

from network import protocol
from network.protocol import (Connection, ConnectionClosed, ProtocolError, log)
from model.card_database import card_database

seq_num = 1

# Client-side state to track the last received sequence numbers for various events, 
# as well as the player's hand and mulligan count.
client_state = {
    "hand": [],
    "last_priority_grant_seq": None,
    "last_phase_transition_seq": None,
    "last_game_state_update_seq": None,
    "mulligan_count": 0,
}

def _handle_game_state_update(conn: Connection, pdu: dict) -> None:
    state = pdu.get("state")
    phase = state.get("phase")

    if phase == "LOBBY":
        ready = state.get("players_ready", 0)
        waiting_for = state.get("waiting_for", [])

        log(f"[client] LOBBY: {ready}/2 players ready")
        for name in waiting_for:
            log(f"[client]   waiting on: {name}")

    elif phase == "MULLIGAN":
        log("[client] MULLIGAN: game setup complete, waiting for mulligan decisions")
        log(f"[client] active_player={state.get('active_player')} turn={state.get('turn')}")
        hand = state.get("hand", {})
        for pid, cards in hand.items():
            log(f"[client]   hand ({pid}) = {cards}")
        log(f"[client]   opponent hand counts = {state.get('hand_counts')}")

    elif phase == "IN_GAME":
        log("[client] IN_GAME: game active")
        log(f"[client] active_player={state.get('active_player')} turn={state.get('turn')} phase={state.get('phase')}")
        hand = state.get("hand", {})
        for pid, cards in hand.items():
            log(f"[client]   hand ({pid}) = {cards}")
        log(f"[client]   opponent hand counts = {state.get('hand_counts')}")
        log(f"[client]   battlefield = {state.get('battlefield')}")

    else:
        log(f"[client] GAME_STATE_UPDATE with unhandled phase '{phase}': {state}")


def _handle_phase_transition(conn: Connection, pdu: dict) -> None:
    log(f"[client] PHASE_TRANSITION: {pdu.get('from_phase')} -> {pdu.get('to_phase')} "
        f"(active_player={pdu.get('active_player')} turn={pdu.get('turn')})")
    client_state["last_phase_transition_seq"] = pdu.get("seq_num")

def _handle_priority_grant(conn: Connection, pdu: dict) -> None:
    log(f"[client] PRIORITY_GRANT: {pdu.get('player_id')} has priority for {pdu.get('time_limit_ms')}ms")

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
    log(f"[client] GAME_OVER: {pdu}")

def _handle_error(conn: Connection, pdu: dict) -> None:
    log(f"[client] ERROR: {pdu.get('code')} - {pdu.get('message')}")

def _handle_pong(conn: Connection, pdu: dict) -> None:
    now = int(time.time() * 1000)
    rtt = now - pdu["timestamp"]
    log(f"[client] PONG received (seq_num={pdu['seq_num']}), rtt={rtt}ms")


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

        formatted_pdu = json.dumps(response, indent=2)

        log(f"[client] Debug received: {formatted_pdu}")
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
    log("[client] commands: 'ready <id>', 'ping', 'help', 'q'")
    ping_seq = 1
    while True:
        try:
            command = input("[client] > ")
        except EOFError: # e.g. stdin closed
            break

        normalized = command.strip().lower()

        if not normalized or normalized == "ping":
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
                continue

            player_id = parts[1]
            deck_list = _build_sample_deck(50)
            log(f"[client] sending PLAYER_READY player_id={player_id} deck_size={len(deck_list)}")
            send_player_ready(conn, player_id, deck_list)
            continue

        if normalized == "help":
            log("[client] commands:")
            log("  ping               send a PING")
            log("  ready <player_id>  send PLAYER_READY with a sample deck")
            log("  q                  quit")
            continue

        log(f"[client] unknown command: {command}")

def send_player_ready(conn: Connection, player_id: str, deck_list: list) -> None:
    global seq_num

    conn.send_pdu({
        "type": "PLAYER_READY", 
        "seq_num": seq_num,
        "player_id" : player_id,
        "deck_list": deck_list
    })

    seq_num += 1


def send_mulligan_choice(conn: Connection, hand: list, last_game_state_seq: int, mulligan_count: int) -> None:
    log(f"[client] your hand ({len(hand)} cards): {hand}")
    log(f"[client] you have mulliganed {mulligan_count} time(s) so far")

    choice = input("[client] keep this hand? (y/n): ").strip().lower()
    keep = choice in ("y", "yes")

    cards_to_bottom = []

    # TODO: implement mulligan logic
            
    conn.send_pdu({
        "type": "MULLIGAN_CHOICE",
        "seq_num": last_game_state_seq,   # echoes the setup/redraw GAME_STATE_UPDATE
        "keep": keep,
        "cards_to_bottom": cards_to_bottom,
    })


def receive_and_handle(conn: Connection) -> None:
    # Receives a PDU from the connection, logs it, and dispatches it to the appropriate handler based on its type.
    response = conn.recv_pdu()
    log(f"[client] Debug received: {response}")

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

    log(f"[client] connected to {args.host}:{args.port}")
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


if __name__ == "__main__":
    main()
