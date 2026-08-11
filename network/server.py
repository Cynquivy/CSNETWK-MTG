import argparse
import random
import socket
import threading

from network import protocol
from network.protocol import Connection, ConnectionClosed, MalformedPDU, ProtocolError, log
from network import pdu as pdu_builders

from model.phase import Phase, TURN_SEQUENCE
from model.player import player
from model.creature import creature
from model.instant import instant
from model.card_database import card_database
from model.stack import StackItem, StackItemType
from model.mana import payment_covers_cost, card_id_base
from model.effects import effect_for
from model.triggers import TriggerEvent, trigger_definition_for
from model.game_state import GameState
from model.lifecycle import LifecycleState

DEMO_SEED = 4444

DEMO_OPENING_HANDS = {
    "chisa": [
        "mountain_001",
        "mountain_002",
        "mountain_003",
        "monastery_swiftspear_001",
        "lightning_bolt_001",
        "shock_001",
        "sol_ring_001",
    ],
    "wow": [
        "island_001",
        "ponder_001",
        "unsummon_001",
        "merfolk_looter_001",
        "cancel_001",
        "air_elemental_001",
        "ornithopter_001",
    ],
}

DEMO_MULLIGAN_HANDS = {
    "wow": [
        [
            "island_001",
            "island_002",
            "ponder_001",
            "unsummon_001",
            "cancel_001",
            "merfolk_looter_001",
            "air_elemental_001",
        ],
        [
            "island_001",
            "island_002",
            "counterspell_001",
            "phantasmal_bear_001",
            "air_elemental_001",
            "ponder_001",
            "unsummon_001",
        ],
    ]
}

DEMO_DRAW_ORDER = {
    "wow": [
        "searing_spear_001",
        "goblin_guide_001",
        "mountain_004",
        "flame_slash_001",
        "lava_spike_001",
    ],
    "wow": [
        "island_003",
        "ponder_002",
        "unsummon_002",
        "cancel_002",
        "island_004",
    ],
}

MAX_PLAYERS = 2
# RFC 4.2 / 10.2.5: the time_limit_ms advertised in every PRIORITY_GRANT.
PRIORITY_TIME_LIMIT_MS = 60000
# RFC 6.1 ("Any state MUST transition immediately to GAME_OVER if a player
# disconnects and fails to reconnect within the implementation-defined
# timeout") / 6.6: the RFC leaves the actual duration up to the
# implementation. 30s matches the RFC 4.3 RECOMMENDED client PING interval,
# so a client that is merely between heartbeats is never mistaken for gone.
RECONNECT_TIMEOUT_S = 30


class _PendingTrigger:
    """
    Server-internal bookkeeping for one triggered ability that has fired
    but is not yet on the Stack (RFC 8.6.1-8.6.4). Not RFC wire-visible
    itself -- trigger_id is the same id used in TRIGGER_ORDER /
    TRIGGER_CHOICE / STACK_PUSH once it actually gets there.
    """
    def __init__(self, trigger_id, source_id, controller_id, definition):
        self.trigger_id = trigger_id
        self.source_id = source_id
        self.controller_id = controller_id
        self.requires_target = definition.requires_target
        self.optional = definition.optional
        self.summary = definition.summary


class GameServer:
    def __init__(self, host: str, port: int, verbose: bool, seed: int | None = None):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.clients = {}
        self.lock = threading.Lock()
        self.seed = seed
        self.rng = random.Random(seed)
        self._demo_mulligan_stage = {}
        self._demo_decks = {}

        # RFC 0001 Section 4.2: the server's single authoritative Game
        # State (model/game_state.py, Milestone #3). Starts in LOBBY
        # (RFC 6.2: "The server enters the LOBBY state upon startup").
        # This replaces the old ad-hoc self.state = "LOBBY" string tracker
        # -- that only modeled 3 states (LOBBY/SETUP/IN_GAME) where the
        # RFC's own Section 6.1 lifecycle machine has 5 (LOBBY/GAME_SETUP/
        # MULLIGAN/IN_GAME/GAME_OVER).
        self.game_state = GameState()

        # LOBBY-only bookkeeping (RFC 6.2), reset whenever the server
        # re-enters LOBBY (on startup, and after every GAME_OVER --
        # Milestone #14). player_id -> deck_list, for players who have
        # submitted a *valid* PLAYER_READY but the game hasn't started yet
        # -- GAME_SETUP (Milestone #7) is what turns these into real
        # Player objects and shuffles/deals.
        self.pending_decks = {}
        # player_id -> connection label ("P1"/"P2"), so a resubmission
        # from the SAME connection can be told apart from a genuine
        # DUPLICATE_ID claimed by a DIFFERENT connection (RFC 6.2).
        self.player_id_to_label = {}

        # RFC 8.1 rule 6: server-internal bookkeeping (not part of the
        # RFC's own wire-visible GameState) tracking how many consecutive
        # PRIORITY_PASS PDUs have been received for the currently open
        # priority window. Reset to 0 every time a new window opens.
        self._priority_passes = 0
        # RFC 8.3: server-assigned unique Stack item ids ("stk_01", ...).
        self._stack_id_counter = 0
        # RFC 8.6: server-assigned unique trigger ids ("trg_01", ...).
        self._trigger_id_counter = 0

        # RFC 8.6.2: while a TRIGGER_ORDER is outstanding, this holds
        # {"by_player": {player_id: [_PendingTrigger, ...]},
        #  "awaiting_order_from": {player_id, ...}}. None when no
        # ordering decision is currently pending.
        self._trigger_placement = None
        # RFC 8.6.3/8.6.4: while a TRIGGER_CHOICE is outstanding, this
        # holds {"trigger": _PendingTrigger, "rest": [remaining queue]}.
        # None when no choice is currently pending.
        self._trigger_choice_pending = None
        # Who should receive priority once the current trigger-placement
        # workflow finishes (RFC 8.1 rule 3: a caster who triggered
        # something retains priority); None means "grant to the Active
        # Player as usual" (RFC 8.1 rule 1/5).
        self._trigger_priority_recipient = None

        self._damage_order_pending = set()
        self._discard_pending_for = None

        # RFC 4.2 ("Enforce the time_limit_ms advertised in each
        # PRIORITY_GRANT. If the priority-holding client does not respond
        # before the deadline, the server MUST broadcast GAME_OVER with
        # reason DISCONNECT..."): _priority_timer is the pending
        # threading.Timer for the most recently issued PRIORITY_GRANT (or
        # None if no deadline is currently armed). _priority_epoch is
        # bumped every time the timer is (re)armed or cancelled, so a
        # timer callback that fires after being superseded can recognize
        # it is stale and no-op instead of acting on a closed window.
        self._priority_timer = None
        self._priority_epoch = 0

        # RFC 6.1/6.6: a seat whose TCP connection just died mid-match
        # (GAME_SETUP/MULLIGAN/IN_GAME) is not freed immediately -- it
        # stays reserved (still counts toward MAX_PLAYERS, still keeps its
        # player_id_to_label/game_state.players entries) for up to
        # RECONNECT_TIMEOUT_S, so the same player can resume on a new TCP
        # connection. label -> (threading.Timer, player_id).
        self._awaiting_reconnect = {}

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
            reconnect_label = next(iter(self._awaiting_reconnect), None)

            if reconnect_label is None and len(self.clients) >= MAX_PLAYERS:
                # Refuse >2 players
                log(f"[server] refusing extra connection from "
                    f"{addr[0]}:{addr[1]} (already {MAX_PLAYERS} players)")
                conn_sock.close()
                return

            if reconnect_label is not None:
                # RFC 6.1: a seat is in its reconnect grace period -- the
                # next incoming connection resumes it rather than taking a
                # fresh seat (there's no separate reconnect PDU defined;
                # the new TCP connection itself IS the reconnect signal).
                label = reconnect_label
                timer, reconnected_player_id = self._awaiting_reconnect.pop(label)
                timer.cancel()
            else:
                label = self._next_free_label()
                reconnected_player_id = None

            conn = Connection(conn_sock, local="SERVER", peer=label,
                              verbose=self.verbose)
            if reconnected_player_id is not None:
                conn.player_id = reconnected_player_id
            self.clients[label] = conn
            seated = len(self.clients)

        if reconnected_player_id is not None:
            log(f"[server] {label} reconnected from {addr[0]}:{addr[1]} "
                f"as '{reconnected_player_id}' (RFC 6.1)")
        else:
            log(f"[server] {label} connected from {addr[0]}:{addr[1]} "
                f"({seated}/{MAX_PLAYERS} players)")

        threading.Thread(target=self._serve_client, args=(label, conn),
                         daemon=True).start()

        if reconnected_player_id is not None:
            # RFC 4.3: clients accept GAME_STATE_UPDATE as authoritative --
            # a personalized resync is all a reconnecting client needs to
            # catch back up; build_game_state_update_in_game degrades
            # gracefully for MULLIGAN/GAME_SETUP too (see its docstring).
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=reconnected_player_id,
            ))
        elif self.game_state.lifecycle_state == LifecycleState.LOBBY:
            ready_labels = set(self.player_id_to_label.values())
            waiting_for = [l for l in self.clients if l not in ready_labels]

            conn.send_pdu(pdu_builders.build_game_state_update_lobby(
                seq_num=conn.next_seq(),
                players_ready=len(self.pending_decks),
                waiting_for=waiting_for,
            ))

    def _next_free_label(self) -> str:
        for i in range(1, MAX_PLAYERS + 1):
            label = f"P{i}"
            if label not in self.clients:
                return label

    def _serve_client(self, label: str, conn: Connection) -> None:
        try:
            while True:
                try:
                    pdu = conn.recv_pdu()      # blocks until a full PDU arrives
                except MalformedPDU as exc:
                    conn.send_pdu(pdu_builders.build_error(
                        seq_num=conn.next_seq(),
                        code="INVALID_JSON",
                        message=str(exc),
                        rejected_action=None,
                    ))
                    continue

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
            self._handle_disconnect(label, conn)

    def _handle_disconnect(self, label: str, conn: Connection) -> None:
        """
        RFC 6.1: "Any state MUST transition immediately to GAME_OVER if a
        player disconnects and fails to reconnect within the
        implementation-defined timeout." Outside of an actual match
        (LOBBY, including the post-GAME_OVER waiting state -- RFC 6.6 "the
        server MAY close that connection and re-enter a waiting state"),
        there is no match to protect, so the seat is simply freed, exactly
        as before. Mid-match (GAME_SETUP/MULLIGAN/IN_GAME), the seat stays
        reserved for RECONNECT_TIMEOUT_S so the same player can resume.
        """
        with self.lock:
            # A stale thread for a connection this label no longer owns
            # (superseded by a reconnect, or already cleaned up by a prior
            # disconnect/timeout/GAME_OVER) has nothing left to do.
            if self.clients.get(label) is not conn:
                return

            player_id = getattr(conn, "player_id", None)
            mid_match = (
                self.game_state.lifecycle_state != LifecycleState.LOBBY
                and player_id in self.player_id_to_label
            )

            if not mid_match:
                self.clients.pop(label, None)
                remaining = len(self.clients)
                log(f"[server] {label} slot freed ({remaining}/{MAX_PLAYERS} players)")
                return

            log(f"[server] {label} ('{player_id}') disconnected mid-match -- "
                f"opening a {RECONNECT_TIMEOUT_S}s reconnect window (RFC 6.1)")
            timer = threading.Timer(RECONNECT_TIMEOUT_S, self._on_reconnect_timeout,
                                     args=(label, player_id))
            timer.daemon = True
            self._awaiting_reconnect[label] = (timer, player_id)
            timer.start()

    def _on_reconnect_timeout(self, label: str, player_id: str) -> None:
        with self.lock:
            entry = self._awaiting_reconnect.get(label)
            still_awaiting = entry is not None and entry[1] == player_id
            if still_awaiting:
                del self._awaiting_reconnect[label]
                self.clients.pop(label, None)
            winner = self.game_state.opponent_of(player_id) if still_awaiting else None

        if winner is None:
            return

        log(f"[server] {label} ('{player_id}') failed to reconnect within "
            f"{RECONNECT_TIMEOUT_S}s -- treating as DISCONNECT (RFC 6.1)")
        self._end_game(winner_id=winner.player_id, loser_id=player_id, reason="DISCONNECT")

    ### HANDLERS ###
    def _handle_player_ready(self, label: str, conn: Connection, pdu: dict) -> None:
        # RFC 6.1/6.2: PLAYER_READY is only meaningful while the server is
        # in the LOBBY state.
        if self.game_state.lifecycle_state != LifecycleState.LOBBY:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="PLAYER_READY is only valid in the LOBBY state.",
                rejected_action=pdu,
            ))
            return

        player_id = pdu.get("player_id")
        deck_list = pdu.get("deck_list")

        # RFC 6.2: "The player_id field in PLAYER_READY is client-chosen
        # and MUST be a non-empty string." No RFC error code is dedicated
        # to this specific violation; ILLEGAL_ACTION ("syntactically valid
        # but violates game rules", RFC 11) is the closest defined fit.
        if not isinstance(player_id, str) or not player_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="player_id must be a non-empty string.",
                rejected_action=pdu,
            ))
            return

        deck_error = player.validate_deck(deck_list)

        with self.lock:
            claimed_by = self.player_id_to_label.get(player_id)
            # RFC 6.2: rejected only if the id is already claimed by the
            # OTHER connected player -- the SAME connection resubmitting
            # under an id it already holds is the explicitly-allowed
            # replace-before-ready case handled below, not a duplicate.
            duplicate = claimed_by is not None and claimed_by != label

            if not duplicate and deck_error is None:
                # RFC 6.2: "A player MAY send a subsequent PLAYER_READY in
                # the LOBBY state before both players are ready; the
                # server MUST replace the earlier submission." If this
                # connection previously registered a different player_id,
                # drop that stale mapping first.
                for old_id, old_label in list(self.player_id_to_label.items()):
                    if old_label == label and old_id != player_id:
                        del self.player_id_to_label[old_id]
                        self.pending_decks.pop(old_id, None)

                self.player_id_to_label[player_id] = label
                self.pending_decks[player_id] = deck_list
                conn.player_id = player_id

            both_ready = len(self.pending_decks) >= MAX_PLAYERS
            players_ready = len(self.pending_decks)
            ready_labels = set(self.player_id_to_label.values())
            # RFC 6.2's own worked example lists the not-yet-ready
            # opponent's eventual player_id ("waiting_for": ["player_2"]),
            # but that id is entirely client-chosen and genuinely unknown
            # to the server until that PDU actually arrives -- there is no
            # way to predict it in general. Using the connection label
            # ("P1"/"P2") as a placeholder is the closest honest
            # approximation available before that player has readied up.
            waiting_for = [l for l in self.clients if l not in ready_labels]

            if not duplicate and deck_error is None and both_ready:
                # RFC 6.2: "When both players have sent a valid
                # PLAYER_READY PDU, the server transitions to GAME_SETUP."
                # GAME_SETUP's own procedure (shuffle/deal/coin-flip, RFC
                # 6.3) is Milestone #7's job.
                self.game_state.lifecycle_state = LifecycleState.GAME_SETUP

        if duplicate:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="DUPLICATE_ID",
                message=f"player_id '{player_id}' is already claimed by the other player.",
                rejected_action=pdu,
            ))
            return

        if deck_error is not None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_DECK",
                message=deck_error,
                rejected_action=pdu,
            ))
            return

        log(f"[server] {label} ready as '{player_id}' "
            f"({players_ready}/{MAX_PLAYERS} players ready)")
        
        if both_ready:
            recipients = list(self.clients.values())
        
            for recv_conn in recipients:
                recv_conn.send_pdu(pdu_builders.build_game_state_update_lobby(
                    seq_num=recv_conn.next_seq(),
                    players_ready=players_ready,
                    waiting_for=[],
                ))
        
            log("[server] both players ready -- running GAME_SETUP (RFC 6.3)")
            self._run_game_setup()
            return
        
        conn.send_pdu(pdu_builders.build_game_state_update_lobby(
            seq_num=conn.next_seq(),
            players_ready=players_ready,
            waiting_for=waiting_for,
        ))
        
    def _using_demo_seed(self) -> bool:
        return self.seed == DEMO_SEED
    
    def _set_demo_hand(self, p, cards: list[str]) -> None:
        full_deck = self._demo_decks[p.player_id]
    
        missing = [card_id for card_id in cards if card_id not in full_deck]
    
        if missing:
            raise RuntimeError(
                f"Demo deck for {p.player_id} is missing required cards: {missing}"
            )
    
        remaining = list(full_deck)
    
        for card_id in cards:
            remaining.remove(card_id)
    
        draws = [
            card_id
            for card_id in DEMO_DRAW_ORDER.get(p.player_id, [])
            if card_id in remaining
        ]
    
        for card_id in draws:
            remaining.remove(card_id)
    
        p.hand = list(cards)
        p.library = remaining + list(reversed(draws))

    def _run_game_setup(self) -> None:
        """
        RFC 6.3 GAME_SETUP: "The server performs the following operations
        automatically, without requiring player input" -- so this runs
        immediately once both players are ready (called from the tail of
        _handle_player_ready), not in response to any further PDU. Deck
        legality (step 1) was already validated at PLAYER_READY time
        (Milestone #6); this covers steps 2-6.
        """
        with self.lock:
            for player_id, deck_list in self.pending_decks.items():
                # RFC's PLAYER_READY carries no separate display-name
                # field, so player_id doubles as both.
                p = player(player_id, player_id)
                
                if self._using_demo_seed():
                    self._demo_decks[player_id] = list(deck_list)
                
                if self._using_demo_seed() and player_id in DEMO_OPENING_HANDS:
                    p.initialize_library(deck_list)
                    self._set_demo_hand(p, DEMO_OPENING_HANDS[player_id])
                    self._demo_mulligan_stage[player_id] = 0
                else:
                    p.initialize_library(deck_list)   # step 2 (life=20 default) + step 3 (shuffle)
                    p.draw_from_lib(7)                 # step 4
                
                self.game_state.add_player(p)

            self.game_state.turn = 0  # RFC 6.5: turn becomes 1 only once IN_GAME begins
            
            if self._using_demo_seed() and "chisa" in self.game_state.players:
                self.game_state.active_player_id = "chisa"
            else:
                # step 5: coin flip
                self.game_state.active_player_id = random.choice(list(self.game_state.players.keys()))
                
            self.game_state.lifecycle_state = LifecycleState.MULLIGAN
            self.pending_decks.clear()

            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        # step 6: personalized GAME_STATE_UPDATE per player, sent after
        # releasing the lock (network I/O should not hold it).
        for player_id, conn in recipients:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))

        log(f"[server] GAME_SETUP complete -- lifecycle_state=MULLIGAN, "
            f"active_player={self.game_state.active_player_id}")

    def _handle_mulligan_choice(self, label: str, conn: Connection, pdu: dict) -> None:
        # RFC 6.1/6.4: MULLIGAN_CHOICE is only meaningful in the MULLIGAN
        # lifecycle state.
        if self.game_state.lifecycle_state != LifecycleState.MULLIGAN:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="MULLIGAN_CHOICE is only valid in the MULLIGAN state.",
                rejected_action=pdu,
            ))
            return

        player_id = getattr(conn, "player_id", None)
        p = self.game_state.players.get(player_id)
        if p is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="No registered player for this connection.",
                rejected_action=pdu,
            ))
            return

        # Not an RFC-literal case (the RFC assumes each player decides
        # exactly once per hand), but without this guard a duplicate
        # MULLIGAN_CHOICE resending the same already-consumed seq_num
        # would pass the echo check below and try to remove already-
        # bottomed cards a second time.
        if p.keep_cards:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="You have already kept your hand.",
                rejected_action=pdu,
            ))
            return

        # RFC 5.4: MULLIGAN_CHOICE echoes the seq_num of the GAME_STATE_UPDATE
        # sent at MULLIGAN's start or after this player's last redraw --
        # exactly what Connection tracks automatically (Milestone #5).
        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('MULLIGAN_CHOICE')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu,
            ))
            # RFC 11 step 3 ("if the player still holds priority, re-issue
            # PRIORITY_GRANT") does not apply here -- mulligan decisions
            # never involve priority, so there is nothing to re-issue.
            return

        keep = pdu.get("keep")
        if not isinstance(keep, bool):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="'keep' must be a boolean.",
                rejected_action=pdu,
            ))
            return

        cards_to_bottom = pdu.get("cards_to_bottom")

        if not keep:
            # RFC 6.4 / Examples.pdf footnote: cards_to_bottom MUST be
            # empty when keep is false.
            if cards_to_bottom:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message="cards_to_bottom must be empty when keep is false.",
                    rejected_action=pdu,
                ))
                return

            # London Mulligan redraw (RFC 6.4): shuffle the hand back in,
            # draw a fresh 7. The bottoming happens only once they keep.
            p.mulligan_count += 1

            if self._using_demo_seed() and player_id in DEMO_MULLIGAN_HANDS:
                stage = self._demo_mulligan_stage.get(player_id, 0)
                scripted_hands = DEMO_MULLIGAN_HANDS[player_id]
            
                if stage < len(scripted_hands):
                    self._set_demo_hand(p, scripted_hands[stage])
                    self._demo_mulligan_stage[player_id] = stage + 1
                else:
                    p.library.extend(p.hand)
                    p.hand.clear()
                    self.rng.shuffle(p.library)
                    p.draw_from_lib(7)
            else:
                p.library.extend(p.hand)
                p.hand.clear()
                self.rng.shuffle(p.library)
                p.draw_from_lib(7)

            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))
            return

        # keep == True: RFC 6.4 -- cards_to_bottom MUST contain exactly N
        # distinct card ids from the player's current hand, where N is
        # how many times they have mulliganed.
        valid_bottom = (
            isinstance(cards_to_bottom, list)
            and len(cards_to_bottom) == p.mulligan_count
            and len(set(cards_to_bottom)) == len(cards_to_bottom)
            and all(c in p.hand for c in cards_to_bottom)
        )
        if not valid_bottom:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message=(f"cards_to_bottom must contain exactly {p.mulligan_count} "
                         f"distinct card id(s) from your hand."),
                rejected_action=pdu,
            ))
            return

        for card_id in cards_to_bottom:
            p.hand.remove(card_id)
            p.library.insert(0, card_id)
        
        p.keep_cards = True
        
        conn.send_pdu(pdu_builders.build_game_state_update_in_game(
            seq_num=conn.next_seq(),
            game_state=self.game_state,
            viewer_player_id=player_id,
        ))
        
        log(f"[server] {player_id} kept their hand "
            f"(mulligan_count={p.mulligan_count})")
        
        self._maybe_begin_in_game()

    def _maybe_begin_in_game(self) -> None:
        """
        RFC 6.4: "When both players have sent MULLIGAN_CHOICE with keep:
        true, the server transitions to IN_GAME and begins the first
        player's turn." RFC 6.5: the turn counter is set to 1 here.
        """
        with self.lock:
            if self.game_state.lifecycle_state != LifecycleState.MULLIGAN:
                return
            if len(self.game_state.players) < MAX_PLAYERS:
                return
            if not all(pl.keep_cards for pl in self.game_state.players.values()):
                return

            self.game_state.lifecycle_state = LifecycleState.IN_GAME
            self.game_state.turn = 1
            self.game_state.phase = Phase.UNTAP
            active_player_id = self.game_state.active_player_id
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for conn in recipients:
            conn.send_pdu(pdu_builders.build_phase_transition(
                seq_num=conn.next_seq(),
                from_phase="MULLIGAN",
                to_phase=Phase.UNTAP,
                active_player_id=active_player_id,
                turn=1,
            ))

        log(f"[server] both players kept -- IN_GAME turn 1, active_player={active_player_id}")
        self._run_phase_entry(Phase.UNTAP)

    ### TURN / PHASE ENGINE (RFC Section 7, Figure 4) ###
    #
    # _advance_phase() moves the turn engine forward one step in
    # model.phase.TURN_SEQUENCE (wrapping Cleanup -> next turn's Untap)
    # and broadcasts the PHASE_TRANSITION for that move; _run_phase_entry()
    # then performs whatever automatic action (if any) that newly-entered
    # step requires, per RFC Section 7's per-step descriptions.
    #
    # Steps with no priority window (Untap; Cleanup's no-discard-needed
    # fast path) call _advance_phase() again immediately when done, per
    # RFC 7.2's "transition immediately" -- so a single PRIORITY_PASS can
    # cascade through several silent steps before the next PRIORITY_GRANT
    # is actually sent.
    #
    # DECLARE_ATTACKERS onward (the rest of the Combat Phase sub-state
    # machine, RFC Section 9) is Milestone #12's job: TURN_SEQUENCE
    # already knows the correct order for those steps too, but no handler
    # yet drives the engine through them -- _run_phase_entry() broadcasts
    # their PHASE_TRANSITION (RFC 9.3: that broadcast IS the signal, "no
    # separate request PDU is defined") and then legitimately stops,
    # exactly as it does today while waiting on a real player.

    def _advance_phase(self) -> None:
        with self.lock:
            current_phase = self.game_state.phase
            if current_phase is Phase.END_OF_COMBAT:
                self._clear_combat_state()

            next_phase = self._next_phase_after(current_phase)

            self.game_state.phase = next_phase
            self.game_state.priority_holder_id = None  # RFC 10.2.2: null outside a priority window
            self._priority_passes = 0

            active_player_id = self.game_state.active_player_id
            turn = self.game_state.turn
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for conn in recipients:
            conn.send_pdu(pdu_builders.build_phase_transition(
                seq_num=conn.next_seq(),
                from_phase=current_phase,
                to_phase=next_phase,
                active_player_id=active_player_id,
                turn=turn,
            ))

        self._run_phase_entry(next_phase)

    # I needed to add this because combat has two conditional skip logic
    def _next_phase_after(self, current_phase: Phase) -> Phase:
        """
        CONDITIONS FOR SPECIAL LOCIC:
          - Declare Blockers -> Assign Damage Order is skipped if nothing
            is multiply-blocked.
          - Whatever comes after Assign Damage Order (or after Declare
            Blockers, if that step was itself skipped) goes to First Strike
            Damage only if a first/double-strike creature is actually
            participating this combat; otherwise straight to Combat Damage.
          - After first strike damage
        """
        if current_phase is Phase.DECLARE_BLOCKERS and not self._combat_needs_damage_order():
            return Phase.FIRST_STRIKE_DAMAGE if self._combat_has_first_strike() else Phase.COMBAT_DAMAGE
        if current_phase is Phase.ASSIGN_DAMAGE_ORDER:
            return Phase.FIRST_STRIKE_DAMAGE if self._combat_has_first_strike() else Phase.COMBAT_DAMAGE
        if current_phase is Phase.FIRST_STRIKE_DAMAGE:
            return Phase.COMBAT_DAMAGE

        index = TURN_SEQUENCE.index(current_phase)
        at_end = index == len(TURN_SEQUENCE) - 1
        return TURN_SEQUENCE[0] if at_end else TURN_SEQUENCE[index + 1]

    # combat phase cleanup, when combat ends
    def _clear_combat_state(self) -> None:
        for p in self.game_state.players.values():
            for c in p.board:
                if isinstance(c, creature):
                    c.will_attack = False
                    c.will_block = False
                    c.attack_target = None
                    c.blocked_by = []
                    c.damage_order = []
                    c.was_blocked = False
                    c.damage_marked = 0

    def _run_phase_entry(self, phase) -> None:
        if phase is Phase.UNTAP:
            self._run_untap_step()
        elif phase is Phase.DRAW:
            self._run_draw_step()
        elif phase is Phase.CLEANUP:
            self._run_cleanup_step()
        elif phase in (Phase.UPKEEP, Phase.PRECOMBAT_MAIN, Phase.BEGIN_COMBAT,
                       Phase.END_OF_COMBAT, Phase.POSTCOMBAT_MAIN, Phase.END_STEP):
            # RFC 7.3 / 7.5 / 9.2 / 7.7: no automatic action -- just open a
            # priority window with the Active Player first (RFC 8.1 rule 1).
            self._open_priority_window()
        elif phase is Phase.ASSIGN_DAMAGE_ORDER:
            self._begin_damage_order_step()
        elif phase is Phase.FIRST_STRIKE_DAMAGE:
            self._run_first_strike_damage_step()
        elif phase is Phase.COMBAT_DAMAGE:
            self._run_combat_damage_step()
        # DECLARE_ATTACKERS and DECLARE_BLOCKERS wait for their corresponding
        # client declaration PDUs. The PHASE_TRANSITION itself signals the
        # appropriate player to send the declaration.

    def _run_untap_step(self) -> None:
        """RFC 7.2: untap the Active Player's permanents, reset their land
        drop, broadcast the result, then move on with no priority window."""
        with self.lock:
            active = self.game_state.active_player
            for permanent in active.board:
                permanent.is_tapped = False
            active.land_played_this_turn = False
            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        for player_id, conn in recipients:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))

        self._advance_phase()

    def _run_draw_step(self) -> None:
        """
        RFC 7.4: draw one card for the Active Player, except on the very
        first turn of the game, where no card is drawn but the
        PHASE_TRANSITION and priority window still happen normally.
        """
        with self.lock:
            active = self.game_state.active_player
            skip_draw = self.game_state.turn == 1
            drew_successfully = True
    
            if not skip_draw:
                drew_successfully = active.draw_from_lib(1)
    
            conn = self.clients[self.player_id_to_label[active.player_id]]
    
        if not drew_successfully:
            # RFC 6.5: "A player is required to draw a card from an empty
            # library" is an immediate win/loss condition -- the winner is
            # the player who did not trigger it (RFC 6.6).
            winner_id = self.game_state.opponent_of(active.player_id).player_id
            self._end_game(winner_id=winner_id, loser_id=active.player_id, reason="DECK_EMPTY")
            return
    
        # RFC 7.4: send a personalized GAME_STATE_UPDATE to the Active Player.
        # On turn 1, the first player's draw is skipped, so the hand remains unchanged.
        conn.send_pdu(pdu_builders.build_game_state_update_in_game(
            seq_num=conn.next_seq(),
            game_state=self.game_state,
            viewer_player_id=active.player_id,
        ))
    
        self._open_priority_window()

    def _run_cleanup_step(self) -> None:
        """RFC 7.8: discard down to 7 if needed (Milestone #13 supplies the
        actual DISCARD handling and resumes from here); otherwise clear
        damage/until-end-of-turn effects, advance the turn counter, flip
        the Active Player, and begin the next turn's Untap Step."""
        with self.lock:
            active = self.game_state.active_player
            needs_discard = len(active.hand) > 7
            if needs_discard:
                self._discard_pending_for = active.player_id
            conn = self.clients[self.player_id_to_label[active.player_id]] if needs_discard else None

        if needs_discard:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=active.player_id,
            ))
            log(f"[server] {active.player_id} must discard to 7 -- awaiting DISCARD")
            return

        self._finish_cleanup_step()

    # this needed to be split for card discard cleanup
    def _finish_cleanup_step(self) -> None:
        """
        RFC 7.8 tail end: clear damage and until-end-of-turn stat changes,
        broadcast the cleared state to both players, then increment the turn
        counter, flip the Active Player, and begin the next turn's Untap
        Step. Shared by the no-discard-needed path in _run_cleanup_step and
        by _handle_discard once a discard round brings the hand to <= 7.
        """
        with self.lock:
            for p in self.game_state.players.values():
                for permanent in p.board:
                    if isinstance(permanent, creature):
                        permanent.damage_marked = 0
                        permanent.power = permanent.base_power
                        permanent.toughness = permanent.base_toughness

            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        for player_id, conn2 in recipients:
            conn2.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn2.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))

        with self.lock:
            self.game_state.turn += 1
            self.game_state.active_player_id = self.game_state.opponent_of(
                self.game_state.active_player_id).player_id

        self._advance_phase()

    def _open_priority_window(self) -> None:
        """RFC 8.1 rule 1: the Active Player receives priority first at the
        start of every step that grants a priority window."""
        with self.lock:
            self._priority_passes = 0
            self.game_state.priority_holder_id = self.game_state.active_player_id
            active_player_id = self.game_state.active_player_id

        self._send_priority_grant(active_player_id)

    def _send_priority_grant(self, player_id: str, conn: Connection | None = None) -> None:
        """RFC 10.2.5: issue a PRIORITY_GRANT to player_id, then arm (or
        re-arm) the RFC 4.2 response deadline the server MUST enforce
        against it. Any previously pending deadline is superseded."""
        if conn is None:
            with self.lock:
                conn = self.clients[self.player_id_to_label[player_id]]

        conn.send_pdu(pdu_builders.build_priority_grant(
            seq_num=conn.next_seq(),
            player_id=player_id,
            time_limit_ms=PRIORITY_TIME_LIMIT_MS,
        ))
        self._arm_priority_timeout(player_id)

    def _arm_priority_timeout(self, player_id: str) -> None:
        with self.lock:
            if self._priority_timer is not None:
                self._priority_timer.cancel()
            self._priority_epoch += 1
            epoch = self._priority_epoch
            timer = threading.Timer(
                PRIORITY_TIME_LIMIT_MS / 1000.0,
                self._on_priority_timeout,
                args=(player_id, epoch),
            )
            timer.daemon = True
            self._priority_timer = timer
            timer.start()

    def _cancel_priority_timeout(self) -> None:
        """Stop enforcing the current PRIORITY_GRANT deadline without
        arming a replacement -- used when a priority window closes
        without an immediate re-grant (e.g. RFC 8.6.1: "Priority MUST NOT
        be granted until all pending trigger ordering decisions and
        optional trigger choices have been resolved")."""
        with self.lock:
            if self._priority_timer is not None:
                self._priority_timer.cancel()
                self._priority_timer = None
            self._priority_epoch += 1

    def _on_priority_timeout(self, player_id: str, epoch: int) -> None:
        with self.lock:
            still_current = (
                epoch == self._priority_epoch
                and self.game_state.lifecycle_state == LifecycleState.IN_GAME
                and self.game_state.priority_holder_id == player_id
            )
            winner = self.game_state.opponent_of(player_id) if still_current else None

        if winner is None:
            return

        log(f"[server] priority timeout: {player_id} did not respond within "
            f"{PRIORITY_TIME_LIMIT_MS}ms of its PRIORITY_GRANT -- treating as "
            f"DISCONNECT (RFC 4.2)")
        self._end_game(winner_id=winner.player_id, loser_id=player_id, reason="DISCONNECT")

    def _handle_priority_pass(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="PRIORITY_PASS is only valid during IN_GAME.",
                rejected_action=pdu,
            ))
            return

        player_id = getattr(conn, "player_id", None)
        if player_id != self.game_state.priority_holder_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="NOT_YOUR_PRIORITY",
                message="You do not currently hold priority.",
                rejected_action=pdu,
            ))
            return

        if conn.is_stale(pdu):
            error, grant = conn.build_stale_action_response(pdu, player_id=player_id)
            conn.send_pdu(error)
            conn.send_pdu(grant)
            return

        # RFC 4.2: a legitimate PRIORITY_PASS is always a timely response
        # to the outstanding PRIORITY_GRANT -- it either hands priority to
        # the other player, resolves the Stack, or ends the step, never
        # "try again in this same window". Stop enforcing the deadline
        # this pass just satisfied; whichever branch below runs next will
        # re-arm it if (and only if) a new window/grant follows.
        self._cancel_priority_timeout()

        advance = False
        resolve = False
        next_holder_id = None
        next_holder_conn = None

        with self.lock:
            self._priority_passes += 1
            if self._priority_passes >= 2:
                if self.game_state.stack_is_empty:
                    # RFC 8.1 rule 6: step ends, move to the next step.
                    advance = True
                else:
                    # RFC 8.1 rule 5: resolve the top Stack item, then
                    # re-grant priority to the Active Player.
                    resolve = True
            else:
                # RFC 8.1 rule 4: priority passes to the other player.
                opponent = self.game_state.opponent_of(player_id)
                next_holder_id = opponent.player_id
                self.game_state.priority_holder_id = next_holder_id
                next_holder_conn = self.clients[self.player_id_to_label[next_holder_id]]

        if advance:
            self._advance_phase()
        elif resolve:
            self._resolve_top_of_stack()
        else:
            self._send_priority_grant(next_holder_id, conn=next_holder_conn)

    def _next_stack_item_id(self) -> str:
        self._stack_id_counter += 1
        return f"stk_{self._stack_id_counter:02d}"

    def _targets_still_legal(self, targets) -> bool:
        """
        RFC 8.4 step 2 / ERROR code ILLEGAL_TARGET: a target is legal if
        it names a still-seated player, a permanent currently on some
        player's battlefield, or a Stack item still on the Stack (needed
        for "counter target spell" effects, RFC 10.2.7's targets field
        being a bare array of ids with no declared type). This card set
        has no per-effect target-type metadata (e.g. "target creature" vs
        "any target" vs "target player"), so this is a structural
        existence check only -- real per-effect target restrictions are
        model/effects.py's job.
        """
        with self.lock:
            permanent_ids = {c.card_id for p in self.game_state.players.values() for c in p.board}
            player_ids = set(self.game_state.players.keys())
            stack_item_ids = {item.stack_item_id for item in self.game_state.stack}
        return all(t in player_ids or t in permanent_ids or t in stack_item_ids for t in targets)

    def _end_game(self, winner_id: str, loser_id: str, reason: str) -> None:
        """RFC 6.6: broadcast GAME_OVER (reason MUST be one of LIFE_ZERO |
        DECK_EMPTY | CONCEDE | DISCONNECT) to both players, then
        immediately reset to LOBBY state on the same TCP connections."""
        # Whatever ended the game makes any still-pending RFC 4.2 priority
        # deadline moot; invalidate it so a race with an in-flight
        # _on_priority_timeout() can't fire a second, conflicting
        # GAME_OVER (e.g. a DISCONNECT timeout racing a LIFE_ZERO SBA).
        self._cancel_priority_timeout()

        with self.lock:
            # .get() rather than direct indexing: a DISCONNECT reason means
            # the loser's own connection may already be gone (their seat
            # was freed when their reconnect grace period expired, see
            # _on_reconnect_timeout) -- there is nobody there to notify,
            # but the surviving player still MUST hear about it.
            recipients = [conn for pid in self.game_state.players
                          if (recv_label := self.player_id_to_label.get(pid)) is not None
                          and (conn := self.clients.get(recv_label)) is not None]

        for conn in recipients:
            self._safe_send(conn, pdu_builders.build_game_over(
                seq_num=conn.next_seq(),
                winner_id=winner_id,
                loser_id=loser_id,
                reason=reason,
            ))

        with self.lock:
            self.game_state = GameState()          # creates a fresh state for the next match
            self.player_id_to_label.clear()
            self.pending_decks.clear()
            self.game_state.lifecycle_state = LifecycleState.LOBBY
            recipients = list(self.clients.values())

        # RFC 6.6: After broadcasting GAME_OVER, the server MUST return to the LOBBY
        # state and await new PLAYER_READY PDUs on the same TCP connections, allowing
        # the same two players to start a new game without reconnecting.
        for conn in recipients:
            self._safe_send(conn, pdu_builders.build_game_state_update_lobby(
                seq_num=conn.next_seq(),
                players_ready=0,
                waiting_for=list(self.clients.keys()),
            ))

        log(f"[server] GAME_OVER ({reason}): winner={winner_id} loser={loser_id} "
            f"-- returned to LOBBY state")

    @staticmethod
    def _safe_send(conn: Connection, pdu: dict) -> None:
        """Best-effort send for notification paths (GAME_OVER, the LOBBY
        reset that follows it) that must not let one dead connection abort
        cleanup for the other player -- e.g. both players disconnecting
        near-simultaneously, or the DISCONNECT loser's own socket already
        being gone by the time their reconnect grace period expires."""
        try:
            conn.send_pdu(pdu)
        except OSError as exc:
            log(f"[server] {conn.peer} send failed during GAME_OVER cleanup: {exc}")

    def _check_state_based_actions(self) -> tuple[str, str] | None:
        """
        RFC 8.4: checked after every game event, before any priority is
        granted. Applies repeatedly until stable: creatures with lethal
        damage or non-positive toughness die; a player at 0 or less life
        loses (simultaneous zero-life: the Active Player loses, RFC 8.4).
        Placing resulting triggers on the Stack is Milestone #10's job
        (no trigger detection exists yet, so there is nothing to place).

        Returns (winner_id, loser_id) if a LIFE_ZERO condition was found,
        else None. This only detects the condition and cleans up dead
        creatures -- callers are responsible for passing the result to
        _end_game() (reason="LIFE_ZERO") per the RFC 8.4/Examples.pdf
        Step 32 ordering: no further GAME_STATE_UPDATE is broadcast once
        a game-ending condition is found, GAME_OVER follows immediately.
        """
        with self.lock:
            changed = True
            while changed:
                changed = False
                for p in self.game_state.players.values():
                    dead = [c for c in p.board if isinstance(c, creature)
                            and (c.toughness <= 0 or c.damage_marked >= c.toughness)]
                    for c in dead:
                        p.board.remove(c)
                        p.graveyard.append(c.card_id)
                        changed = True

            losers = [pid for pid, p in self.game_state.players.items() if p.life <= 0]
            active_id = self.game_state.active_player_id

        if not losers:
            return None

        # RFC 8.4: "If both players' life totals reach zero or less
        # simultaneously ... the Active Player loses and the Non-Active
        # Player wins."
        loser_id = active_id if len(losers) == 2 else losers[0]
        winner_id = self.game_state.opponent_of(loser_id).player_id
        return winner_id, loser_id

    def _resolve_top_of_stack(self) -> None:
        """
        RFC 8.4 steps 1-4: pop the top Stack item, re-check target
        legality, apply the effect (or fizzle), broadcast STACK_RESOLVE,
        run state-based actions, broadcast the resulting GAME_STATE_UPDATE,
        then re-grant priority to the Active Player.
        """
        with self.lock:
            item = self.game_state.pop_stack_item()

        # _targets_still_legal() acquires self.lock itself (threading.Lock
        # is not reentrant), so it must be called outside the block above.
        legal = self._targets_still_legal(item.targets)

        state_changes = []
        if legal and item.item_type == StackItemType.SPELL:
            effect_fn = effect_for(card_id_base(item.source_id))
            if effect_fn is not None:
                with self.lock:
                    state_changes = effect_fn(self.game_state, item.targets)

        result = "RESOLVED" if legal else "FIZZLE"

        with self.lock:
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for conn in recipients:
            conn.send_pdu(pdu_builders.build_stack_resolve(
                seq_num=conn.next_seq(),
                stack_item_id=item.stack_item_id,
                result=result,
                state_changes=state_changes,
            ))

        game_over = self._check_state_based_actions()

        if game_over:
            winner_id, loser_id = game_over
            self._end_game(winner_id=winner_id, loser_id=loser_id, reason="LIFE_ZERO")
            return

        with self.lock:
            recipients2 = [(pid, self.clients[self.player_id_to_label[pid]])
                           for pid in self.game_state.players]
        for player_id, conn in recipients2:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))

        # RFC 8.1 rule 5: "The Active Player then receives priority again."
        self._open_priority_window()

    def _handle_cast_spell(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="WRONG_PHASE",
                message="CAST_SPELL is only valid during IN_GAME.",
                rejected_action=pdu))
            return

        player_id = getattr(conn, "player_id", None)
        caster = self.game_state.players.get(player_id)
        if caster is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No registered player for this connection.",
                rejected_action=pdu))
            return

        # RFC 8.1 rule 2: only whoever currently holds priority may act --
        # this may be the Non-Active Player responding with an instant.
        if player_id != self.game_state.priority_holder_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="NOT_YOUR_PRIORITY",
                message="You do not currently hold priority.",
                rejected_action=pdu))
            return

        if conn.is_stale(pdu):
            error, grant = conn.build_stale_action_response(pdu, player_id=player_id)
            conn.send_pdu(error)
            conn.send_pdu(grant)
            return

        card_id = pdu.get("card_id")
        targets = pdu.get("targets") or []
        mana_payment = pdu.get("mana_payment") or {}

        if card_id not in caster.hand:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message=f"'{card_id}' is not in your hand.",
                rejected_action=pdu))
            return

        spell = card_database.CARD_DATABASE.get(card_id)
        if spell is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message=f"'{card_id}' is not a card in the fixed card set.",
                rejected_action=pdu))
            return

        # RFC 7.5: non-instants are sorcery-speed -- Active Player only,
        # Main Phase only, empty stack only. RFC 11 WRONG_PHASE's own
        # example is literally "casting a sorcery outside a Main Phase".
        if not isinstance(spell, instant):
            sorcery_speed_ok = (
                player_id == self.game_state.active_player_id
                and self.game_state.phase in (Phase.PRECOMBAT_MAIN, Phase.POSTCOMBAT_MAIN)
                and self.game_state.stack_is_empty
            )
            if not sorcery_speed_ok:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="WRONG_PHASE",
                    message="Non-instant spells may only be cast by the Active "
                            "Player, at sorcery speed, with an empty stack.",
                    rejected_action=pdu))
                return

        if not self._targets_still_legal(targets):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_TARGET",
                message="One or more targets are not legal.",
                rejected_action=pdu))
            return

        if not payment_covers_cost(mana_payment, spell):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="INSUFFICIENT_MANA",
                message="mana_payment does not satisfy this spell's cost.",
                rejected_action=pdu
            ))
            return
        
        if not caster.pay_mana(mana_payment):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="INSUFFICIENT_MANA",
                message="Not enough untapped mana sources to satisfy mana_payment.",
                rejected_action=pdu
            ))
            return

        with self.lock:
            caster.hand.remove(card_id)
            item = StackItem(
                stack_item_id=self._next_stack_item_id(),
                item_type=StackItemType.SPELL,
                source_id=card_id,
                controller_id=player_id,
                targets=targets,
            )
            self.game_state.push_stack_item(item)
            # RFC 8.1 rule 3: "that player retains priority" -- the pass
            # streak resets since a new event just occurred.
            self._priority_passes = 0
            self.game_state.priority_holder_id = player_id
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for c in recipients:
            c.send_pdu(pdu_builders.build_stack_push(seq_num=c.next_seq(), stack_item=item))

        with self.lock:
            recipients2 = [(pid, self.clients[self.player_id_to_label[pid]])
                           for pid in self.game_state.players]

        for pid, recv_conn in recipients2:
            recv_conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=recv_conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=pid,
            ))

        # RFC 8.6.1: "A spell or ability is cast" MUST trigger a check --
        # e.g. Prowess ("whenever YOU cast a noncreature spell"), which is
        # why the candidates are scoped to the caster's own permanents.
        # If anything fired, the trigger workflow (RFC 8.6.1: "Priority
        # MUST NOT be granted until all pending trigger ordering decisions
        # and optional trigger choices have been resolved") is now
        # responsible for eventually granting priority back to this
        # caster (RFC 8.1 rule 3); otherwise grant it immediately as
        # before.
        triggered = False
        if not isinstance(spell, creature):
            triggered = self._check_triggers(
                TriggerEvent.CAST_NONCREATURE_SPELL,
                [(c.card_id, player_id) for c in caster.board],
                retain_priority_for=player_id,
            )

        if triggered:
            # RFC 8.6.1: "Priority MUST NOT be granted until all pending
            # trigger ordering decisions ... have been resolved" -- no
            # grant means no deadline to enforce until that workflow
            # (which itself calls _send_priority_grant) finishes.
            self._cancel_priority_timeout()
        else:
            self._send_priority_grant(player_id, conn=conn)

    def _handle_activate_ability(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="WRONG_PHASE",
                message="ACTIVATE_ABILITY is only valid during IN_GAME.",
                rejected_action=pdu))
            return

        player_id = getattr(conn, "player_id", None)
        controller = self.game_state.players.get(player_id)
        if controller is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No registered player for this connection.",
                rejected_action=pdu))
            return

        if player_id != self.game_state.priority_holder_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="NOT_YOUR_PRIORITY",
                message="You do not currently hold priority.",
                rejected_action=pdu))
            return

        if conn.is_stale(pdu):
            error, grant = conn.build_stale_action_response(pdu, player_id=player_id)
            conn.send_pdu(error)
            conn.send_pdu(grant)
            return

        source_id = pdu.get("source_id")
        # ability_index is part of the RFC 10.2.8 schema but cannot yet be
        # validated against anything: no card in this project carries a
        # structured per-ability list (only free-text "Simplified Effect"
        # strings, docs/mtgnp_master_card_list.xlsx) -- that data model is
        # Milestone #19's job. Every activation is accepted structurally
        # regardless of index.
        targets = pdu.get("targets") or []
        cost_payment = pdu.get("cost_payment") or {}
        tap_cost = bool(cost_payment.get("tap"))
        mana_cost = cost_payment.get("mana") or {}

        source = next((c for c in controller.board if c.card_id == source_id), None)
        if source is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message=f"'{source_id}' is not a permanent you control.",
                rejected_action=pdu))
            return

        # RFC 10.2.8 comment: "Server rejects with ILLEGAL_ACTION if
        # permanent is already tapped".
        if tap_cost and getattr(source, "is_tapped", False):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message=f"'{source_id}' is already tapped.",
                rejected_action=pdu))
            return

        if not self._targets_still_legal(targets):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_TARGET",
                message="One or more targets are not legal.",
                rejected_action=pdu))
            return

        if mana_cost and not controller.pay_mana(mana_cost):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="INSUFFICIENT_MANA",
                message="cost_payment.mana does not satisfy this ability's cost.",
                rejected_action=pdu))
            return

        with self.lock:
            if tap_cost:
                source.is_tapped = True
            item = StackItem(
                stack_item_id=self._next_stack_item_id(),
                item_type=StackItemType.ABILITY,
                source_id=source_id,
                controller_id=player_id,
                targets=targets,
            )
            self.game_state.push_stack_item(item)
            self._priority_passes = 0
            self.game_state.priority_holder_id = player_id
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for c in recipients:
            c.send_pdu(pdu_builders.build_stack_push(seq_num=c.next_seq(), stack_item=item))

        self._send_priority_grant(player_id, conn=conn)

    ### TRIGGERED ABILITIES (RFC Section 8.6) ###

    def _next_trigger_id(self) -> str:
        self._trigger_id_counter += 1
        return f"trg_{self._trigger_id_counter:02d}"

    def _all_target_candidates(self) -> list:
        """
        Every id a trigger (or CAST_SPELL/ACTIVATE_ABILITY, RFC 8.4) could
        plausibly target: seated players, battlefield permanents, and
        graveyard cards. This card set has no per-effect target-type
        metadata (e.g. gravedigger's real restriction is specifically
        "target creature card in YOUR graveyard", not any graveyard card
        of either player's) -- same documented simplification as
        GameServer._targets_still_legal (Milestone #9).
        """
        with self.lock:
            ids = list(self.game_state.players.keys())
            for p in self.game_state.players.values():
                ids.extend(c.card_id for c in p.board)
                ids.extend(p.graveyard)
        return ids

    def _check_triggers(self, event, candidates, retain_priority_for=None) -> bool:
        """
        RFC 8.6.1: after a qualifying event, check the given (card_id,
        controller_id) candidates against the trigger registry. `candidates`
        is caller-supplied rather than "every permanent on the
        battlefield" because which permanents can even possibly match a
        given event is inherently event-specific (e.g. only the attacking
        creature itself can have a "whenever ~ attacks" trigger; only the
        caster's own permanents can have a "whenever YOU cast" trigger).

        Returns True if one or more triggers fired, meaning responsibility
        for eventually granting priority has been handed to the trigger
        workflow (see _process_trigger_queue) -- the caller must not also
        grant priority itself in that case. `retain_priority_for`, if
        given, is who priority should return to once the workflow
        finishes (RFC 8.1 rule 3); otherwise it goes to the Active Player
        as usual.
        """
        fired = []
        for card_id, controller_id in candidates:
            definition = trigger_definition_for(card_id)
            if definition and definition.event is event:
                fired.append(_PendingTrigger(
                    trigger_id=self._next_trigger_id(),
                    source_id=card_id,
                    controller_id=controller_id,
                    definition=definition,
                ))

        if not fired:
            return False

        self._trigger_priority_recipient = retain_priority_for
        self._begin_trigger_placement(fired)
        return True

    def _begin_trigger_placement(self, fired) -> None:
        by_player = {}
        for t in fired:
            by_player.setdefault(t.controller_id, []).append(t)

        # RFC 8.6.2 rule 3: a player who controls 2+ simultaneous triggers
        # must choose their own placement order.
        needs_order = [pid for pid, ts in by_player.items() if len(ts) >= 2]

        self._trigger_placement = {
            "by_player": by_player,
            "awaiting_order_from": set(needs_order),
        }

        if needs_order:
            for pid in needs_order:
                self._send_trigger_order(pid, by_player[pid])
            return

        self._finish_trigger_ordering()

    def _send_trigger_order(self, player_id, triggers_for_player) -> None:
        conn = self.clients[self.player_id_to_label[player_id]]
        conn.send_pdu(pdu_builders.build_trigger_order(
            seq_num=conn.next_seq(),
            player_id=player_id,
            trigger_ids=[t.trigger_id for t in triggers_for_player],
        ))

    def _handle_trigger_order_response(self, label: str, conn: Connection, pdu: dict) -> None:
        if self._trigger_placement is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No TRIGGER_ORDER is currently pending.",
                rejected_action=pdu))
            return

        player_id = getattr(conn, "player_id", None)
        if player_id not in self._trigger_placement["awaiting_order_from"]:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No TRIGGER_ORDER is pending for you.",
                rejected_action=pdu))
            return

        # RFC 5.4 / 10.2.11: seq_num must match the corresponding
        # TRIGGER_ORDER's own seq_num -- this does not consume priority
        # (RFC 8.6.2 NOTE), so a stale response gets a plain ERROR with no
        # PRIORITY_GRANT re-issue (there is no grant to re-issue).
        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('TRIGGER_ORDER_RESPONSE')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu))
            return

        pending = self._trigger_placement["by_player"][player_id]
        pending_by_id = {t.trigger_id: t for t in pending}
        ordered_ids = pdu.get("ordered_trigger_ids")

        # RFC 11 TRIGGER_ORDER_INVALID: "does not contain exactly the
        # trigger IDs that were sent in the corresponding TRIGGER_ORDER".
        if not isinstance(ordered_ids, list) or set(ordered_ids) != set(pending_by_id):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="TRIGGER_ORDER_INVALID",
                message="ordered_trigger_ids must contain exactly the ids from TRIGGER_ORDER.",
                rejected_action=pdu))
            return

        self._trigger_placement["by_player"][player_id] = [pending_by_id[tid] for tid in ordered_ids]
        self._trigger_placement["awaiting_order_from"].discard(player_id)

        if not self._trigger_placement["awaiting_order_from"]:
            self._finish_trigger_ordering()

    def _finish_trigger_ordering(self) -> None:
        ap_id = self.game_state.active_player_id
        nap = self.game_state.opponent_of(ap_id)
        nap_id = nap.player_id if nap else None
        by_player = self._trigger_placement["by_player"]
        self._trigger_placement = None

        # RFC 8.6.2 rules 1-2: the Active Player's triggers are placed on
        # the Stack FIRST (so they resolve LAST, at the bottom); the
        # Non-Active Player's are placed on top (resolve first). Pushing
        # AP's before NAP's achieves exactly this (Stack = a list; the
        # most recently appended item is the top -- RFC 8.3).
        queue = list(by_player.get(ap_id, [])) + list(by_player.get(nap_id, []))
        self._process_trigger_queue(queue)

    def _process_trigger_queue(self, queue) -> None:
        if not queue:
            # RFC 8.6.1: "Priority MUST NOT be granted until all pending
            # trigger ordering decisions and optional trigger choices
            # have been resolved" -- they now have been.
            recipient = self._trigger_priority_recipient
            self._trigger_priority_recipient = None
            if recipient is None:
                self._open_priority_window()
            else:
                with self.lock:
                    self._priority_passes = 0
                    self.game_state.priority_holder_id = recipient
                self._send_priority_grant(recipient)
            return

        trigger, *rest = queue

        if trigger.requires_target:
            candidates = self._all_target_candidates()
            if not candidates:
                # RFC 8.6.4: "If no legal targets exist, the trigger is
                # discarded immediately with no effect."
                self._process_trigger_queue(rest)
                return
        else:
            candidates = []

        if trigger.requires_target or trigger.optional:
            # RFC 8.6.3/8.6.4: a target-requiring and/or optional trigger
            # needs a TRIGGER_CHOICE round trip before it can be placed.
            self._trigger_choice_pending = {"trigger": trigger, "rest": rest}
            conn = self.clients[self.player_id_to_label[trigger.controller_id]]
            conn.send_pdu(pdu_builders.build_trigger_choice(
                seq_num=conn.next_seq(),
                trigger_id=trigger.trigger_id,
                source_id=trigger.source_id,
                effect_summary=trigger.summary,
                requires_target=trigger.requires_target,
                legal_targets=candidates,
            ))
            return

        # Mandatory, no target needed -- straight onto the Stack.
        self._push_trigger(trigger)
        self._process_trigger_queue(rest)

    def _handle_trigger_choice_response(self, label: str, conn: Connection, pdu: dict) -> None:
        if self._trigger_choice_pending is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No TRIGGER_CHOICE is currently pending.",
                rejected_action=pdu))
            return

        trigger = self._trigger_choice_pending["trigger"]
        player_id = getattr(conn, "player_id", None)
        if player_id != trigger.controller_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="No TRIGGER_CHOICE is pending for you.",
                rejected_action=pdu))
            return

        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('TRIGGER_CHOICE_RESPONSE')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu))
            return

        # RFC 11 TRIGGER_CHOICE_INVALID: "references an unknown
        # trigger_id, or chosen_target is absent when a target is
        # required."
        if pdu.get("trigger_id") != trigger.trigger_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="TRIGGER_CHOICE_INVALID",
                message="trigger_id does not match the pending TRIGGER_CHOICE.",
                rejected_action=pdu))
            return

        accept = pdu.get("accept")
        chosen_target = pdu.get("chosen_target")

        # A mandatory trigger's TRIGGER_CHOICE exists only to gather a
        # target (RFC 8.6.4) -- there is no real "decline" option, since
        # RFC 8.6.3's accept=false path is specifically for "you may"
        # triggers.
        if not trigger.optional and accept is not True:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="TRIGGER_CHOICE_INVALID",
                message="This trigger is mandatory and cannot be declined.",
                rejected_action=pdu))
            return

        if accept and trigger.requires_target:
            candidates = self._all_target_candidates()
            if chosen_target is None or chosen_target not in candidates:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="TRIGGER_CHOICE_INVALID",
                    message="chosen_target is missing or not a legal target.",
                    rejected_action=pdu))
                return

        rest = self._trigger_choice_pending["rest"]
        self._trigger_choice_pending = None

        if accept:
            self._push_trigger(trigger, target=chosen_target if trigger.requires_target else None)

        self._process_trigger_queue(rest)

    def _push_trigger(self, trigger, target=None) -> None:
        with self.lock:
            item = StackItem(
                stack_item_id=self._next_stack_item_id(),
                item_type=StackItemType.TRIGGER_ABILITY,
                source_id=trigger.source_id,
                controller_id=trigger.controller_id,
                targets=[target] if target else [],
            )
            self.game_state.push_stack_item(item)
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        for c in recipients:
            c.send_pdu(pdu_builders.build_stack_push(seq_num=c.next_seq(), stack_item=item))

    # HELPERS FOR COMBAT:

    # Finds the actual creature object
    def _find_creature(self, player_obj, card_id) -> creature | None:
        for c in player_obj.board:
            if isinstance(c, creature) and c.card_id == card_id:
                return c

        return None

    # this is just to determine if we need to do damage ordering
    # aka when an attacker is being blocked by 2+ cards
    def _combat_needs_damage_order(self) -> bool:
        for p in self.game_state.players.values():
            for c in p.board:
                if isinstance(c, creature) and c.will_attack:
                    if len(c.blocked_by) >= 2:
                        return True

        return False

    # checks if we need first/double strike logic
    def _combat_has_first_strike(self) -> bool:
        for p in self.game_state.players.values():
            for c in p.board:
                if not isinstance(c, creature):
                    continue
                participating = c.will_attack or c.will_block
                if participating and (c.has_first_strike or c.has_double_strike):
                    return True

        return False

    def _creatures_with_lethal_damage(self) -> list:
        """
        snapshot of which creatures are about to die, taken BEFORE _check_state_based_actions() removes them. 
        Needed for COMBAT_DAMAGE_RESULT's creatures_died list (RFC 10.2.18), 
        since the existing SBA checker only reports whether someone
        lost the whole game, not which permanents died along the way.
        """
        return [
            c.card_id
            for p in self.game_state.players.values()
            for c in p.board
            if isinstance(c, creature) and (c.toughness <= 0 or c.damage_marked >= c.toughness)
        ]

    def _handle_declare_attackers(self, label: str, conn: Connection, pdu: dict) -> None:
        # guard check
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME or self.game_state.phase is not Phase.DECLARE_ATTACKERS:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="DECLARE_ATTACKERS is only valid during the Declare Attackers Step.",
                rejected_action=pdu
            ))
            return

        player_id = getattr(conn, "player_id", None)
        # RFC 9.3: nobody holds priority at this exact moment -- the
        # this checks "are you the active player", not "do you hold priority".
        if player_id != self.game_state.active_player_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="NOT_YOUR_PRIORITY",
                message="Only the Active Player declares attackers.",
                rejected_action=pdu
            ))
            return

        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('DECLARE_ATTACKERS')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu))
            return

        attackers_field = pdu.get("attackers")
        if not isinstance(attackers_field, list):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="'attackers' must be a list.",
                rejected_action=pdu
            ))
            return

        active = self.game_state.players[player_id]
        opponent = self.game_state.opponent_of(player_id)
        seen_ids = set()
        resolved = []

        for entry in attackers_field:
            creature_id = entry.get("creature_id") if isinstance(entry, dict) else None
            target = entry.get("target") if isinstance(entry, dict) else None
            attacker = self._find_creature(active, creature_id)

            if attacker is None:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' is not a creature you control.",
                    rejected_action=pdu
                ))
                return
            if creature_id in seen_ids:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' was declared as an attacker more than once.",
                    rejected_action=pdu
                ))
                return
            if attacker.is_tapped:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' is tapped and cannot attack.",
                    rejected_action=pdu
                ))
                return
            if attacker.summoning_sick and not attacker.has_haste:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' has summoning sickness and cannot attack.",
                    rejected_action=pdu
                ))
                return
            if opponent is None or target != opponent.player_id:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_TARGET",
                    message=f"'{target}' is not a legal attack target.",
                    rejected_action=pdu
                ))
                return

            seen_ids.add(creature_id)
            resolved.append((attacker, target))

        with self.lock:
            for attacker, target in resolved:
                attacker.will_attack = True
                attacker.is_tapped = True
                attacker.attack_target = target

            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        if not resolved:
            # empty attackers == skip
            # straight to End of Combat. No GAME_STATE_UPDATE, no priority
            log(f"[server] {player_id} declares no attackers -- skipping to End of Combat")
            with self.lock:
                from_phase = self.game_state.phase
                self.game_state.phase = Phase.END_OF_COMBAT
                self.game_state.priority_holder_id = None
                self._priority_passes = 0
                active_player_id = self.game_state.active_player_id
                turn = self.game_state.turn
            for _, c in recipients:
                c.send_pdu(pdu_builders.build_phase_transition(
                    seq_num=c.next_seq(), from_phase=from_phase, to_phase=Phase.END_OF_COMBAT,
                    active_player_id=active_player_id, turn=turn))
            self._run_phase_entry(Phase.END_OF_COMBAT)
            return

        log(f"[server] {player_id} declares {len(resolved)} attacker(s)")
        for pid, c in recipients:
            c.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=c.next_seq(), game_state=self.game_state, viewer_player_id=pid))

        self._open_priority_window()

    def _handle_declare_blockers(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME or self.game_state.phase is not Phase.DECLARE_BLOCKERS:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="DECLARE_BLOCKERS is only valid during the Declare Blockers Step.",
                rejected_action=pdu
            ))
            return

        player_id = getattr(conn, "player_id", None)
        # the non-active player declares blockers.
        if player_id is None or player_id == self.game_state.active_player_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="NOT_YOUR_PRIORITY",
                message="Only the Non-Active Player declares blockers.",
                rejected_action=pdu))
            return

        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('DECLARE_BLOCKERS')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu
            ))
            return

        blockers_field = pdu.get("blockers")
        if not isinstance(blockers_field, list):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="'blockers' must be a list.",
                rejected_action=pdu
            ))
            return

        # NON ATCIVE PLAYER
        nap = self.game_state.players[player_id]
        seen_blockers = set()
        resolved = []

        for entry in blockers_field:
            creature_id = entry.get("creature_id") if isinstance(entry, dict) else None
            blocking_id = entry.get("blocking_id") if isinstance(entry, dict) else None
            blocker = self._find_creature(nap, creature_id)
            attacker = self._find_creature(self.game_state.active_player, blocking_id)

            if blocker is None:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' is not a creature you control.",
                    rejected_action=pdu))
                return
            # RFC 9.4: "lists which UNTAPPED creatures block".
            if blocker.is_tapped:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' is tapped and cannot block.",
                    rejected_action=pdu))
                return
            # RFC 9.4: "a single creature may block only one attacker".
            if creature_id in seen_blockers:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                    message=f"'{creature_id}' can only block one attacker.",
                    rejected_action=pdu))
                return
            if attacker is None or not attacker.will_attack:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                    message=f"'{blocking_id}' is not an attacking creature.",
                    rejected_action=pdu))
                return

            seen_blockers.add(creature_id)
            resolved.append((attacker, blocker))

        with self.lock:
            for attacker, blocker in resolved:
                # multiple creatures MAY block the same attacker
                # appending, not overwriting, is what makes that legal.
                attacker.blocked_by.append(blocker)
                blocker.will_block = True
            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        log(f"[server] {player_id} declares {len(resolved)} blocker(s)")
        for pid, c in recipients:
            c.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=c.next_seq(), game_state=self.game_state, viewer_player_id=pid))

        self._open_priority_window()

    def _begin_damage_order_step(self) -> None:
        """
        RFC 9.5: nothing automatic happens here
        the PHASE_TRANSITION into ASSIGN_DAMAGE_ORDER
        already broadcast by _advance_phase() is itself
        the implicit signal for the Active Player to start sending one
        ASSIGN_DAMAGE_ORDER PDU per multiply-blocked attacker. This just
        records which attacker ids still need one.
        """
        with self.lock:
            self._damage_order_pending = {
                c.card_id
                for p in self.game_state.players.values()
                for c in p.board
                if isinstance(c, creature) and c.will_attack and len(c.blocked_by) >= 2
            }

    def _handle_assign_damage_order(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME or \
           self.game_state.phase is not Phase.ASSIGN_DAMAGE_ORDER:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="ASSIGN_DAMAGE_ORDER is only valid during the Assign Damage Order Step.",
                rejected_action=pdu
            ))
            return

        player_id = getattr(conn, "player_id", None)
        if player_id != self.game_state.active_player_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), 
                code="NOT_YOUR_PRIORITY",
                message="Only the Active Player assigns damage order.",
                rejected_action=pdu
            ))
            return

        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('ASSIGN_DAMAGE_ORDER')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu
            ))
            return

        attacker_id = pdu.get("attacker_id")
        blocker_order = pdu.get("blocker_order")

        if attacker_id not in self._damage_order_pending:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message=f"'{attacker_id}' is not awaiting a damage order.",
                rejected_action=pdu))
            return

        attacker = self._find_creature(self.game_state.active_player, attacker_id)
        expected_ids = {b.card_id for b in attacker.blocked_by}

        if not isinstance(blocker_order, list) or len(blocker_order) != len(expected_ids) \
           or set(blocker_order) != expected_ids:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="ILLEGAL_ACTION",
                message="blocker_order must contain exactly this attacker's blockers, once each.",
                rejected_action=pdu))
            return

        by_id = {b.card_id: b for b in attacker.blocked_by}
        with self.lock:
            attacker.damage_order = [by_id[bid] for bid in blocker_order]
            self._damage_order_pending.discard(attacker_id)
            remaining = len(self._damage_order_pending)

        log(f"[server] {player_id} ordered damage for '{attacker_id}': {blocker_order}")

        if remaining == 0:
            # After all orderings have been received, the server
            # opens a final priority window before proceeding to the damage step.
            self._open_priority_window()

    # HELPER for computing combat dmg
    def _compute_combat_damage(self, include_creature) -> list:
        damage_events = []

        for p in self.game_state.players.values():
            for attacker in p.board:
                if not isinstance(attacker, creature) or not attacker.will_attack:
                    continue

                if not attacker.blocked_by:
                    if include_creature(attacker):
                        defender = self.game_state.players[attacker.attack_target]
                        defender.life -= attacker.power
                        damage_events.append({"source": attacker.card_id,
                                               "target": attacker.attack_target,
                                               "amount": attacker.power})
                    continue

                # IS BLOCKED
                if include_creature(attacker):
                    order = attacker.damage_order or attacker.blocked_by
                    remaining = attacker.power
                    for blocker in order:
                        if remaining <= 0:
                            break
                        lethal_left = max(0, blocker.toughness - blocker.damage_marked)
                        amount = min(remaining, lethal_left)
                        if amount <= 0:
                            continue
                        blocker.damage_marked += amount
                        damage_events.append({"source": attacker.card_id,
                                               "target": blocker.card_id,
                                               "amount": amount})
                        remaining -= amount

                # blocker still hits a non-first-strike attacker in this step.
                for blocker in attacker.blocked_by:
                    if include_creature(blocker) and any(blocker in p.board for p in self.game_state.players.values()):
                        attacker.damage_marked += blocker.power
                        damage_events.append({"source": blocker.card_id,
                                               "target": attacker.card_id,
                                               "amount": blocker.power})

        return damage_events

    def _run_first_strike_damage_step(self) -> None:
        with self.lock:
            self._compute_combat_damage(
                include_creature=lambda c: c.has_first_strike or c.has_double_strike)
            recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                          for pid in self.game_state.players]

        game_over = self._check_state_based_actions()

        if game_over:
            winner_id, loser_id = game_over
            self._end_game(winner_id=winner_id, loser_id=loser_id, reason="LIFE_ZERO")
            return

        for player_id, c in recipients:
            c.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=c.next_seq(), game_state=self.game_state, viewer_player_id=player_id))

        self._open_priority_window()

    def _run_combat_damage_step(self) -> None:
        with self.lock:
            damage_events = self._compute_combat_damage(
                include_creature=lambda c: not c.has_first_strike or c.has_double_strike)
            life_totals = dict(self.game_state.life_totals)
            recipients = [self.clients[self.player_id_to_label[pid]]
                          for pid in self.game_state.players]

        creatures_died = self._creatures_with_lethal_damage()
        game_over = self._check_state_based_actions()

        for c in recipients:
            c.send_pdu(pdu_builders.build_combat_damage_result(
                seq_num=c.next_seq(), damage_events=damage_events,
                life_totals=life_totals, creatures_died=creatures_died))

        if game_over:
            winner_id, loser_id = game_over
            self._end_game(winner_id=winner_id, loser_id=loser_id, reason="LIFE_ZERO")
            return

        with self.lock:
            recipients2 = [(pid, self.clients[self.player_id_to_label[pid]])
                           for pid in self.game_state.players]
        for player_id, c in recipients2:
            c.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=c.next_seq(), game_state=self.game_state, viewer_player_id=player_id))

        self._advance_phase()

    def _handle_play_land(self, label: str, conn: Connection, pdu: dict) -> None:
        # RFC 7.1: PLAY_LAND is only meaningful once the game is in progress i.e. IN_GAME phase.
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="PLAY_LAND is only valid during IN_GAME.",
                rejected_action=pdu,
            ))
            return

        # error handling for unregistered players
        player_id = getattr(conn, "player_id", None)
        p = self.game_state.players.get(player_id)
        if p is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="No registered player for this connection.",
                rejected_action=pdu,
            ))
            return

        # PLAY_LAND is priority-bearing: seq_num must echo the most recent
        # PRIORITY_GRANT issued to this player.
        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                        f"{conn.expected_seq_for('PLAY_LAND')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu,
            ))
            return

        # Example Step 15: PLAY_LAND does not use the stack; one per turn limit; Main phase only.
        if self.game_state.phase not in (Phase.PRECOMBAT_MAIN, Phase.POSTCOMBAT_MAIN):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="PLAY_LAND is only legal during a Main phase.",
                rejected_action=pdu,
            ))
            return

        # Stack must be empty to play a land. Although the RFC 7.5 does not explicitly say this
        # for PLAY_LAND, the Main Phase actions are described as "sorcery speed for AP," which
        # in standard MTG rules means that the stack must be empty. The statement "Playing a land 
        # does not use the stack" from Step 15 of Examples, means the land itself isn't a stack object,
        # but does not necessarily mean that stack state is ignored. Hence, it is assumed in this 
        # implementation that the stack must be empty to play a land.
        if self.game_state.stack:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="Cannot play a land while the stack is non-empty.",
                rejected_action=pdu,
            ))
            return

        if player_id != self.game_state.active_player_id:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="You may only play a land on your own turn.",
                rejected_action=pdu,
            ))
            return

        # RFC 7.5: A player may play one land per turn. 
        # This is tracked by the Player.land_played_this_turn attribute.
        if p.land_played_this_turn:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="You have already played a land this turn.",
                rejected_action=pdu,
            ))
            return

        card_id = pdu.get("card_id")

        if card_id not in p.hand:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message=f"'{card_id}' is not in your hand.",
                rejected_action=pdu,
            ))
            return

        card_obj = card_database.CARD_DATABASE.get(card_id)
        if card_obj is None or card_obj.card_type != "Land":
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message=f"'{card_id}' is not a land.",
                rejected_action=pdu,
            ))
            return

        with self.lock:
            p.hand.remove(card_id)        # hand holds strings -- remove the string
            p.board.append(card_obj)      # board holds objects -- append the object
            p.land_played_this_turn = True

        log(f"[server] {player_id} played land {card_id}")

        # RFC 7.5: after playing a land, broadcast the resulting GAME_STATE_UPDATE
        # to all players (followed by re-issuing PRIORITY_GRANT to the Active Player).
        recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                    for pid in self.game_state.players]
        for pid, recv_conn in recipients:
            recv_conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=recv_conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=pid,
            ))

        self._send_priority_grant(self.game_state.active_player_id)

    def _handle_discard(self, label: str, conn: Connection, pdu: dict) -> None:
        if self.game_state.lifecycle_state != LifecycleState.IN_GAME or \
           self.game_state.phase is not Phase.CLEANUP:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="WRONG_PHASE",
                message="DISCARD is only valid during the Cleanup Step.",
                rejected_action=pdu
            ))
            return

        player_id = getattr(conn, "player_id", None)
        if player_id is None or player_id != self._discard_pending_for:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="No discard is currently pending for you.",
                rejected_action=pdu
            ))
            return

        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                         f"{conn.expected_seq_for('DISCARD')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu))
            return

        p = self.game_state.players[player_id]
        card_ids = pdu.get("card_ids")

        if not isinstance(card_ids, list) or len(card_ids) == 0:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="card_ids must be a non-empty list.",
                rejected_action=pdu
            ))
            return

        hand_copy = list(p.hand)
        for card_id in card_ids:
            if card_id not in hand_copy:
                conn.send_pdu(pdu_builders.build_error(
                    seq_num=conn.next_seq(),
                    code="ILLEGAL_ACTION",
                    message=f"'{card_id}' is not in your hand.",
                    rejected_action=pdu))
                return
            hand_copy.remove(card_id)

        with self.lock:
            for card_id in card_ids:
                p.hand.remove(card_id)
                p.graveyard.append(card_id)

        log(f"[server] {player_id} discarded {card_ids} "
            f"(hand now {len(p.hand)} card(s))")

        # IF NOT REDUCED BY 7, STILL NEEDS MORE DISCARDS
        if len(p.hand) > 7:
            with self.lock:
                recipients = [(pid, self.clients[self.player_id_to_label[pid]])
                              for pid in self.game_state.players]
            for pid, c in recipients:
                c.send_pdu(pdu_builders.build_game_state_update_in_game(
                    seq_num=c.next_seq(), game_state=self.game_state, viewer_player_id=pid))
            return

        # this is only reached when hand is <= 7
        self._discard_pending_for = None
        self._finish_cleanup_step()

    def _handle_concede(self, label: str, conn: Connection, pdu: dict) -> None:
        # RFC 5.4: CONCEDE may be sent at any time regardless of which player holds 
        # priority; its seq_num MUST be the value from the most recently received 
        # server PDU of any type (not necessarily a PRIORITY_GRANT)
        if conn.is_stale(pdu):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="STALE_ACTION",
                message=(f"seq_num mismatch. Expected "
                        f"{conn.expected_seq_for('CONCEDE')}, got {pdu.get('seq_num')}."),
                rejected_action=pdu,
            ))
            return

        player_id = getattr(conn, "player_id", None)
        p = self.game_state.players.get(player_id)
        if p is None:
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(),
                code="ILLEGAL_ACTION",
                message="No registered player for this connection.",
                rejected_action=pdu,
            ))
            return

        # determine the winner: the other registered player.
        winner_id = next((pid for pid in self.game_state.players if pid != player_id), None)

        self._end_game(winner_id=winner_id, loser_id=player_id, reason="CONCEDE")

    def _handle_ping(self, label: str, conn: Connection, pdu: dict) -> None:
        conn.send_pdu({
            "type" : "PONG",
            "seq_num": pdu["seq_num"],
            "timestamp": pdu["timestamp"]
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="MTGNP Game Server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: 0.0.0.0 = all)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"TCP port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU sent and received")
    parser.add_argument("--seed", type=int, default=None,
                        help="deterministic game seed")
    args = parser.parse_args()
    GameServer(args.host, args.port, args.verbose, args.seed).start()

if __name__ == "__main__":
    main()
