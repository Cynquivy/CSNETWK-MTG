import argparse
import socket
import threading

from network import protocol
from network.protocol import (Connection, ConnectionClosed, ProtocolError, log)

from model.phase import Phase
from model.player import player
from model.creature import creature
from model.artifact import artifact
from model.enchantment import enchantment
from model.instant import instant
from model.sorcery import sorcery

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
                    # RFC 0001 Section 11 / 10.2.23: ERROR echoes the seq_num
                    # of the rejected action and includes a copy of it.
                    conn.send_pdu({
                        "type" : "ERROR",
                        "seq_num" : pdu["seq_num"],
                        "code" : "UNKNOWN_TYPE",
                        "message" : f"Unknown PDU type '{pdu['type']}'.",
                        "rejected_action" : pdu,
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

    def run(self):
        while not self.controller.state.game_over:
            phase = self.controller.state.phase

            print(
                f"\nTurn {self.controller.state.turn_number}"
                f" | Phase: {phase.name}"
            )

            match phase:
                case Phase.UNTAP:
                    self.controller.do_untap()

                case Phase.UPKEEP:
                    self.controller.do_upkeep()

                case Phase.DRAW:
                    self.controller.do_draw()

                case Phase.MAIN_ONE:
                    self.controller.do_main_one()

                case Phase.COMBAT_BEGINNING:
                    self.controller.begin_combat()

                case Phase.DECLARE_ATTACKERS:
                    has_attackers = self.controller.declare_attackers()

                    if not has_attackers:
                        self.controller.state.phase = Phase.COMBAT_ENDING

                case Phase.DECLARE_BLOCKERS:
                    self.controller.declare_blockers()

                case Phase.ASSIGN_DAMAGE_ORDER:
                    self.controller.assign_damage_order()

                case Phase.COMBAT_EXECUTE:
                    self.controller.execute_combat()

                case Phase.COMBAT_ENDING:
                    self.controller.end_combat()

                case Phase.MAIN_TWO:
                    self.controller.do_main_two()

                case Phase.END_STEP:
                    self.controller.do_end_step()

                case Phase.CLEANUP:
                    self.controller.do_cleanup()

                case _:
                    raise ValueError(f"Unknown phase: {phase}")

            if not self.controller.state.game_over:
                if self.controller.state.phase == phase:
                    self.controller.empty_mana_pools()
                    self.controller.state.next_phase()

class GameController:
    def __init__(self):
        self.state = GameState()
        self.players = []
        self.stack = []
    
    def do_untap(self):
        active_player = self.players[self.state.AP_idx]

        for card in active_player.board:
            if card.is_tapped:
                card.is_tapped = False
    
    def do_upkeep(self):
        # no upkeep triggers
        self.open_priority_window()
    
    def do_draw(self):
        skip_first_draw = (
            self.state.turn_number == 1
            and self.state.AP_idx == self.state.starting_player_idx
        )
    
        if not skip_first_draw:
            draw_successful = self.players[self.state.AP_idx].draw_from_lib(1)
    
            if not draw_successful:
                self.state.set_to_game_over()
                return
    
        self.open_priority_window()
    
    def do_main_one(self):
        self.open_priority_window()
    
    def begin_combat(self):
        self.open_priority_window()
    
    def declare_attackers(self):
        num_of_attackers = self.players[self.state.AP_idx].declare_attackers()
    
        if num_of_attackers == 0:
            return False
    
        self.open_priority_window()
        return True
    
    def declare_blockers(self):
        self.players[self.state.NAP_idx].declare_blockers()
    
    def assign_damage_order(self):
        active_player = self.players[self.state.AP_idx]
    
        for card in active_player.board:
            if isinstance(card, creature):
                if card.is_attacking:
                    if len(card.blocked_by) == 1:
                        card.damage_order = card.blocked_by.copy()
    
                    elif len(card.blocked_by) >= 2:
                        card.damage_order = self.game_ui.get_damage_order(card, card.blocked_by)
    
        self.open_priority_window()
    
    def execute_combat(self):
        active_player = self.players[self.state.AP_idx]
        defending_player = self.players[self.state.NAP_idx]
        damage_assignments = []
    
        for attacker in active_player.board:
            if isinstance(attacker, creature) and attacker.is_attacking:
                if not attacker.was_blocked:
                    damage_assignments.append((defending_player, attacker.power))
                else:
                    damage_assignments.extend(self.assign_attacker_damage(attacker))
    
                for blocker in attacker.blocked_by:
                    if blocker in defending_player.board:
                        damage_assignments.append((attacker, blocker.power))
    
        for target, damage in damage_assignments:
            if isinstance(target, player):
                target.life -= damage
            else:
                target.damage_marked += damage
    
        self.check_state_based_actions()
    
        if not self.state.game_over:
            self.open_priority_window()
        
    def end_combat(self):
        self.open_priority_window()
    
        for current_player in self.players:
            for card in current_player.board:
                if isinstance(card, creature):
                    card.is_attacking = False
                    card.is_blocking = False
                    card.was_blocked = False
                    card.blocked_by.clear()
                    card.damage_order.clear()
    
    def do_main_two(self):
        self.open_priority_window()
    
    def do_end_step(self):
        self.open_priority_window()
    
    def do_cleanup(self):
        active_player = self.players[self.state.AP_idx]

        while len(active_player.hand) > 7:
            discarded_card = self.game_ui.get_card_selection(active_player.hand)
    
            active_player.hand.remove(discarded_card)
            active_player.graveyard.append(discarded_card)
    
        for current_player in self.players:
            for card in current_player.board:
                if isinstance(card, creature):
                    card.power = card.base_power
                    card.toughness = card.base_toughness
                    card.damage_marked = 0
        
        active_player.land_played_this_turn = False
        
        self.state.AP_idx, self.state.NAP_idx = (self.state.NAP_idx, self.state.AP_idx)
    
        self.state.turn_number += 1
    
    def open_priority_window(self):
        prio = self.state.AP_idx
        pass_ctr = 0
            
        while len(self.stack) != 0 or pass_ctr != 2:
            
            player = self.players[prio]
            
            if prio == self.state.AP_idx and len(self.stack) == 0 and self.state.phase in (Phase.MAIN_ONE, Phase.MAIN_TWO):
                action = self.game_ui.get_main_phase_action(player)
            else:
                action = self.game_ui.get_priority_action(player)
            
            if action == "play_land":
                land = player.select_land()
            
                can_play_land = (
                    len(self.stack) == 0
                    and prio == self.state.AP_idx
                    and self.state.phase in (Phase.MAIN_ONE, Phase.MAIN_TWO)
                    and land is not None
                    and land in player.hand
                    and not player.land_played_this_turn
                )

                if can_play_land:
                    player.hand.remove(land)
                    player.board.append(land)
                    land.is_tapped = False
                    player.land_played_this_turn = True
                    pass_ctr = 0
                else:
                    print("Cannot play land.")
            
            elif action == "cast_creature":
                creature = self.players[prio].select_creature()
                
                if creature is not None:
                    if self.can_cast_creature(self.players[prio], creature):
                        if self.pay_mana(self.players[prio], creature):
                            self.players[prio].hand.remove(creature)
                            self.stack.append(creature)
            
                            pass_ctr = 0
            
                            print(
                                f"{self.players[prio].player_name} cast "
                                f"{creature.card_name}."
                            )
                    else:
                        print("You cannot cast that creature.")
                
            
            elif action == "cast_spell":
                spell = player.select_card_to_stack()
            
                if spell is not None and self.can_cast_spell(player, spell):
                    if self.pay_mana(player, spell):
                        player.hand.remove(spell)
                        self.stack.append(spell)
                        pass_ctr = 0
                else:
                    print("You cannot cast that spell.")
            
            elif action == "pass":
                pass_ctr += 1
            
                if pass_ctr == 2:
                    if len(self.stack) != 0:
                        self.resolve_stack()
                        pass_ctr = 0
                        prio = self.state.AP_idx
                    else:
                        break
                else:
                    prio = (prio + 1) % 2
    
    def resolve_stack(self):
        card = self.stack.pop()
    
        if isinstance(card, (creature, artifact, enchantment)):
            owner = self.players[card.owner_player_idx]
            owner.board.append(card)
            card.is_tapped = False
            card.effect()
    
        else:
            card.effect()
            owner = self.players[card.owner_player_idx]
            owner.graveyard.append(card)
    
        self.check_state_based_actions()
    
    def assign_attacker_damage(self, attacker):
        damage_assignments = []
        remaining_damage = attacker.power
    
        defending_player = self.players[self.state.NAP_idx]
    
        for blocker in attacker.damage_order:
            if remaining_damage > 0 and blocker in defending_player.board:
                lethal_damage = max(0, blocker.toughness - blocker.damage_marked)
                damage = min(remaining_damage, lethal_damage)
    
                if damage > 0:
                    damage_assignments.append((blocker, damage))
    
                remaining_damage -= damage
    
        return damage_assignments
    
    def check_state_based_actions(self):
        state_changed = True
    
        while state_changed and not self.state.game_over:
            state_changed = False
    
            for current_player in self.players:
                creatures_to_remove = []
    
                for card in current_player.board:
                    if isinstance(card, creature):
                        has_zero_toughness = card.toughness <= 0
                        has_lethal_damage = card.damage_marked >= card.toughness
    
                        if has_zero_toughness or has_lethal_damage:
                            creatures_to_remove.append(card)
    
                for card in creatures_to_remove:
                    current_player.board.remove(card)
                    current_player.graveyard.append(card)
                    state_changed = True
    
            losing_players = []
    
            for index, current_player in enumerate(self.players):
                if current_player.life <= 0:
                    losing_players.append(index)
    
            if len(losing_players) > 0:
                self.state.set_to_game_over()
    
    def can_cast_creature(self, player, card):
        return (
            player == self.players[self.state.AP_idx]
            and self.state.phase in (Phase.MAIN_ONE, Phase.MAIN_TWO)
            and len(self.stack) == 0
            and isinstance(card, creature)
            and card in player.hand
            and self.can_pay_mana(player, card)
        )
    
    def can_pay_mana(self, player, card):
        if player.white_mpool < card.white:
            return False
    
        if player.blue_mpool < card.blue:
            return False
    
        if player.black_mpool < card.black:
            return False
    
        if player.red_mpool < card.red:
            return False
    
        if player.green_mpool < card.green:
            return False
    
        remaining_mana = (
            player.white_mpool - card.white
            + player.blue_mpool - card.blue
            + player.black_mpool - card.black
            + player.red_mpool - card.red
            + player.green_mpool - card.green
        )
    
        return remaining_mana >= card.generic
    
    def pay_mana(self, player, card):
        if not self.can_pay_mana(player, card):
            return False
    
        player.white_mpool -= card.white
        player.blue_mpool -= card.blue
        player.black_mpool -= card.black
        player.red_mpool -= card.red
        player.green_mpool -= card.green
    
        generic_remaining = card.generic
    
        mana_pools = [
            "white_mpool",
            "blue_mpool",
            "black_mpool",
            "red_mpool",
            "green_mpool"
        ]
    
        for mana_pool in mana_pools:
            available = getattr(player, mana_pool)
            mana_to_spend = min(available, generic_remaining)
            setattr(player, mana_pool, available - mana_to_spend)
            generic_remaining -= mana_to_spend
    
        return generic_remaining == 0
    
    def can_cast_spell(self, player, card):
        if card not in player.hand:
            return False
    
        if not self.can_pay_mana(player, card):
            return False
    
        if isinstance(card, instant):
            return True
    
        if isinstance(card, (sorcery, artifact, enchantment)):
            return (
                player == self.players[self.state.AP_idx]
                and self.state.phase in (Phase.MAIN_ONE, Phase.MAIN_TWO)
                and len(self.stack) == 0
            )
    
        return False
    
    def empty_mana_pools(self):
        for player in self.players:
            player.white_mpool = 0
            player.blue_mpool = 0
            player.black_mpool = 0
            player.red_mpool = 0
            player.green_mpool = 0
    
class GameState:
    def __init__(self):
        self.phase = Phase.UNTAP
        self.game_start = True
        self.game_over = False
        self.AP_idx = 0
        self.NAP_idx = 1
        self.turn_number = 1
        self.starting_player_idx = 0
        
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
                self.phase = Phase.ASSIGN_DAMAGE_ORDER
            case Phase.ASSIGN_DAMAGE_ORDER:
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
                self.phase = Phase.UNTAP
        
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
