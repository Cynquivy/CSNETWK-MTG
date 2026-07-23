import argparse
import socket
import threading

from network import protocol
from network.protocol import (Connection, ConnectionClosed, ProtocolError, log)

from model.phase import Phase
from model.player import player

MAX_PLAYERS = 2


class GameServer:
    def __init__(self, host: str, port: int, verbose: bool):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.clients = {}
        self.lock = threading.Lock()
        # controls flow of game
        self.session = GameSession()

        # handler table to call the corresponding method of each pdu type
        self._handlers = {
            "PLAYER_READY" : self._handle_player_ready,
            "MULLIGAN_CHOICE" : self._handle_mulligan_choice,
            "PRIORITY_PASS" : self._handle_priority_pass,
            "CAST_SPELL" : self._handle_cast_spell,
            "ACTIVATE_ABILITY" : self._handle_activate_ability,
            "TRIGGER_ORDER_RESPONSE" : self._handle_trigger_order_response,
            "TRIGGER_CHOICE_RESPONSE" : self._handle_trigger_choice_response,
            "DECLARE_ATTACKERS" : self._handle_declare_attackers,
            "DECLARE_BLOCKERS" : self._handle_declare_blockers,
            "ASSIGN_DAMAGE_ORDER" : self._handle_assign_damage_order,
            "PLAY_LAND" : self._handle_play_land,
            "DISCARD" : self._handle_discard,
            "CONCEDE" : self._handle_concede,
            "PING" : self._handle_ping
        }

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(MAX_PLAYERS + 1)

        log(f"[server] listening on {self.host}:{self.port} "
            f"(verbose={'on' if self.verbose else 'off'})")

        try:
            while True:
                conn_sock, addr = listener.accept()
                self._on_accept(conn_sock, addr)

        except KeyboardInterrupt:
            log("\n[server] shutting down (Ctrl+C)")

        finally:
            listener.close()
            with self.lock:
                for conn in list(self.clients.values()):
                    conn.close()

    def _on_accept(self, conn_sock: socket.socket, addr) -> None:
        with self.lock:
            if len(self.clients) >= MAX_PLAYERS: 
                # Refuse >2 players
                log(f"[server] refusing extra connection from "
                    f"{addr[0]}:{addr[1]} (already {MAX_PLAYERS} players)")
                conn_sock.close()
                return
            
            label = self._next_free_label()
            conn = Connection(conn_sock, local="SERVER", peer=label,
                              verbose=self.verbose)
            self.clients[label] = conn
            seated = len(self.clients)

        log(f"[server] {label} connected from {addr[0]}:{addr[1]} "
            f"({seated}/{MAX_PLAYERS} players)")

        threading.Thread(target=self._serve_client, args=(label, conn),
                         daemon=True).start()

    def _next_free_label(self) -> str:
        for i in range(1, MAX_PLAYERS + 1):
            label = f"P{i}"
            if label not in self.clients:
                return label

    def _serve_client(self, label: str, conn: Connection) -> None:
        try:
            while True:
                pdu = conn.recv_pdu()      # blocks until a full PDU arrives
                handler = self._handlers.get(pdu["type"])
                if handler is None:
                    conn.send_pdu({
                        "type" : "ERROR",
                        "code" : "UNKNOWN TYPE",
                        "message" : f"Unknown '{pdu['type']}' action.",
                    })
                else:
                    handler(label, conn, pdu)

        except ConnectionClosed:
            log(f"[server] {label} disconnected")

        except ProtocolError as exc:
            log(f"[server] {label} protocol error: {exc} -- closing")

        except OSError as exc:
            log(f"[server] {label} socket error: {exc}")

        finally:
            conn.close()
            with self.lock:
                self.clients.pop(label, None)
                remaining = len(self.clients)
            log(f"[server] {label} slot freed ({remaining}/{MAX_PLAYERS} players)")

    ### HANDLERS ###
    def _handle_player_ready(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_mulligan_choice(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_priority_pass(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_cast_spell(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_activate_ability(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_trigger_order_response(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_trigger_choice_response(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_declare_attackers(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_declare_blockers(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_assign_damage_order(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_play_land(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_discard(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_concede(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_ping(self, label: str, conn: Connection, pdu: dict) -> None:
        conn.send_pdu({
            "type" : "PONG",
            "seq_num": pdu["seq_num"],
            "timestamp": pdu["timestamp"]
        })


class GameSession:
    def __init__(self):
        self.controller = GameController()
    
    while not self.controller.state.game_over:
        pass

class GameController:
    def __init__(self):
        self.state = GameState()
        self.players = []
        self.initialize_players()
    
    def initialize_players(self, p1: player, p2: player):
        self.players[0] = p1
        self.players[1] = p2
    
    def do_untap(self):
        for player in self.players:
            for card in player.board:
                if card.is_tapped:
                    card.is_tapped = False
    
    def do_upkeep(self):
        pass
    
    def do_draw(self):
        pass
    
    def do_main_one(self):
        pass
    
    def begin_combat(self):
        pass
    
    def declare_attackers(self):
        pass
    
    def declare_blockers(self):
        pass
    
    def execute_combat(self):
        pass
    
    def end_combat(self):
        pass
    
    def do_main_two(self):
        pass
    
    def do_end_step(self):
        pass
    
    def do_cleanup(self):
        pass

class GameState:
    def __init__(self):
        self.phase = Phase.UNTAP
        self.game_start = True
        self.game_over = False
        self.atk_idx = 0
        self.def_idx = 1
        
    def next_phase(self):
        match self.phase:
            case Phase.UNTAP:
                self.phase = Phase.UPKEEP
            case Phase.UPKEEP:
                self.phase = Phase.DRAW
            case Phase.DRAW:
                self.phase = Phase.MAIN_ONE
            case Phase.MAIN_ONE:
                self.phase = Phase.COMBAT_BEGINNING
            case Phase.COMBAT_BEGINNING:
                self.phase = Phase.DECLARE_ATTACKERS
            case Phase.DECLARE_ATTACKERS:
                self.phase = Phase.DECLARE_BLOCKERS
            case Phase.DECLARE_BLOCKERS:
                self.phase = Phase.COMBAT_EXECUTE
            case Phase.COMBAT_EXECUTE:
                self.phase = Phase.COMBAT_ENDING
            case Phase.COMBAT_ENDING:
                self.phase = Phase.MAIN_TWO
            case Phase.MAIN_TWO:
                self.phase = Phase.END_STEP
            case Phase.END_STEP:
                self.phase = Phase.CLEANUP
            case Phase.CLEANUP:
                self.active_player_index = (self.active_player_index + 1) % 2
                self.phase = Phase.UNTAP
    
    def switch_AP(self):
        self.atk_idx = (self.atk_idx + 1) % 2
        self.def_idx = (self.atk_idx + 1) % 2
        
    def set_to_game_over(self):
        self.game_over = True
    
def main() -> None:
    parser = argparse.ArgumentParser(description="MTGNP Game Server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0 = all)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"TCP port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU sent and received")
    args = parser.parse_args()
    GameServer(args.host, args.port, args.verbose).start()


if __name__ == "__main__":
    main()
