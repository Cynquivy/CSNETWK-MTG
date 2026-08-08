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

        # remember last game state so the interactive prompt can act accordingly (e.g. for mulligan decisions)
        client_state["last_game_state"] = pdu
        client_state["last_game_state_update_seq"] = pdu.get("seq_num")

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
    log("[client] commands: 'ready <id>', 'ping', 'mulligan', 'help', 'q'")
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

            # remember our player id locally so handlers can identify the player in future PDUs
            client_state["player_id"] = player_id
            send_player_ready(conn, player_id, deck_list)
            continue

        if normalized.startswith("mulligan"):
            parts = command.strip().split()
            if len(parts) < 2:
                log("[client] usage: mulligan <mull|keep> [auto|<card_ids>]")
                continue
            verb = parts[1].lower()

            player_id = client_state.get("player_id")
            if player_id is None:
                log("[client] you must `ready <player_id>` first")
                continue

            if client_state["last_game_state"] is None:
                log("[client] no game state available yet")
                continue

            # takes the current hand from the last game state update, which is used to determine which cards can be bottomed during a mulligan decision.
            state = client_state["last_game_state"].get("state", {})
            hand_map = state.get("hand", {})
            hand = hand_map.get(player_id, [])

            if verb in ("mull", "mulligan"):
                # send keep=False
                send_mulligan_choice(conn, keep=False, cards_to_bottom=[])
                log(f"[client] sent MULLIGAN_CHOICE keep=False")
                continue

            if verb == "keep":
                # Case 1: Player chose automatic selection for cards to put at the bottom
                if len(parts) >= 3 and parts[2].lower() == "auto":
                    mulligan_count = client_state["mulligan_count"]
                    cards_to_bottom = hand[:mulligan_count]
                    send_mulligan_choice(conn, keep=True, cards_to_bottom=cards_to_bottom)
                    log(f"[client] sent MULLIGAN_CHOICE keep=True auto bottom {cards_to_bottom}")
                    continue

                # Case 2: Player explicitly listed specific cards to put at the bottom
                elif len(parts) >= 3:
                    cards = parts[2:]
                    send_mulligan_choice(conn, keep=True, cards_to_bottom=cards)
                    log(f"[client] sent MULLIGAN_CHOICE keep=True bottom {cards}")
                    continue

                # Case 3: Player hasn't mulliganed (0 count), so no cards need to go to the bottom
                elif client_state["mulligan_count"] == 0:
                    send_mulligan_choice(conn, keep=True, cards_to_bottom=[])
                    log("[client] sent MULLIGAN_CHOICE keep=True (no cards to bottom)")
                    continue

                # Case 4: Player has mulliganed but didn't specify cards to bottom
                else:
                    log("[client] must specify cards to bottom or use 'mulligan keep auto'")
                    continue

        if normalized == "help":
            log("[client] commands:")
            log("  ping                 send a PING")
            log("  ready <player_id>     send PLAYER_READY with a sample deck")
            log("  mulligan mull         redraw (keep=false)")
            log("  mulligan keep auto    keep and bottom N random cards")
            log("  mulligan keep <ids>   keep and bottom the listed card ids")
            log("  q                    quit")
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
