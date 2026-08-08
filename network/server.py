import argparse
import random
import socket
import threading

from model import land
from network import protocol
from network.protocol import Connection, ConnectionClosed, ProtocolError, log
from network import pdu as pdu_builders

from model.phase import Phase, TURN_SEQUENCE
from model.player import player
from model.creature import creature
from model.artifact import artifact
from model.enchantment import enchantment
from model.instant import instant
from model.sorcery import sorcery
from model.card_database import card_database
from model.stack import StackItem, StackItemType
from model.mana import payment_covers_cost
from model.triggers import TriggerEvent, trigger_definition_for
# Aliased: this file also defines its own (dead, unreachable -- see
# GameController below) in-file `class GameState`, later in this same
# module. Since that class definition executes at module-load time, it
# would silently overwrite an unaliased `GameState` import in this
# module's namespace before any method ever ran.
from model.game_state import GameState as ModelGameState
from model.lifecycle import LifecycleState

MAX_PLAYERS = 2


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
    def __init__(self, host: str, port: int, verbose: bool):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.clients = {}
        self.lock = threading.Lock()
        # controls flow of game
        self.session = GameSession()

        # RFC 0001 Section 4.2: the server's single authoritative Game
        # State (model/game_state.py, Milestone #3). Starts in LOBBY
        # (RFC 6.2: "The server enters the LOBBY state upon startup").
        # This replaces the old ad-hoc self.state = "LOBBY" string tracker
        # -- that only modeled 3 states (LOBBY/SETUP/IN_GAME) where the
        # RFC's own Section 6.1 lifecycle machine has 5 (LOBBY/GAME_SETUP/
        # MULLIGAN/IN_GAME/GAME_OVER).
        self.game_state = ModelGameState()

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
            log("[server] both players ready -- running GAME_SETUP (RFC 6.3)")
            self._run_game_setup()
            return

        conn.send_pdu(pdu_builders.build_game_state_update_lobby(
            seq_num=conn.next_seq(),
            players_ready=players_ready,
            waiting_for=waiting_for,
        ))

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
                p.initialize_library(deck_list)   # step 2 (life=20 default) + step 3 (shuffle)
                p.draw_from_lib(7)                 # step 4
                self.game_state.add_player(p)

            self.game_state.turn = 0  # RFC 6.5: turn becomes 1 only once IN_GAME begins
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
            p.library.extend(p.hand)
            p.hand.clear()
            random.shuffle(p.library)
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
            current_index = TURN_SEQUENCE.index(current_phase)
            at_end_of_turn = current_index == len(TURN_SEQUENCE) - 1
            next_phase = TURN_SEQUENCE[0] if at_end_of_turn else TURN_SEQUENCE[current_index + 1]

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

    def _run_phase_entry(self, phase) -> None:
        if phase is Phase.UNTAP:
            self._run_untap_step()
        elif phase is Phase.DRAW:
            self._run_draw_step()
        elif phase is Phase.CLEANUP:
            self._run_cleanup_step()
        elif phase in (Phase.UPKEEP, Phase.PRECOMBAT_MAIN, Phase.BEGIN_COMBAT,
                       Phase.POSTCOMBAT_MAIN, Phase.END_STEP):
            # RFC 7.3 / 7.5 / 9.2 / 7.7: no automatic action -- just open a
            # priority window with the Active Player first (RFC 8.1 rule 1).
            self._open_priority_window()
        # else: DECLARE_ATTACKERS and later (Milestone #12). Nothing to do
        # here; the PHASE_TRANSITION already broadcast above is the only
        # signal RFC 9.3 defines for that step.

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
        """RFC 7.4: draw one card for the Active Player, except on the very
        first turn of the game, where no card is drawn but the
        PHASE_TRANSITION and priority window still happen normally."""
        with self.lock:
            active = self.game_state.active_player
            skip_draw = self.game_state.turn == 1
            drew_successfully = True
            if not skip_draw:
                drew_successfully = active.draw_from_lib(1)
            conn = self.clients[self.player_id_to_label[active.player_id]]

        if not drew_successfully:
            # RFC 6.5: "A player is required to draw a card from an empty
            # library" is an IN_GAME loss condition. Detecting it here so
            # the engine does not silently continue as if the draw
            # succeeded; broadcasting GAME_OVER for it is Milestone #14's
            # job, so the turn engine intentionally halts rather than
            # opening a priority window on top of an unresolved loss.
            log(f"[server] {active.player_id} must draw from an empty library "
                f"-- GAME_OVER handling is Milestone #14, halting here")
            return

        if not skip_draw:
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
            conn = self.clients[self.player_id_to_label[active.player_id]] if needs_discard else None

        if needs_discard:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=active.player_id,
            ))
            log(f"[server] {active.player_id} must discard to 7 "
                f"-- awaiting DISCARD (Milestone #13)")
            return

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
            conn = self.clients[self.player_id_to_label[self.game_state.active_player_id]]

        conn.send_pdu(pdu_builders.build_priority_grant(
            seq_num=conn.next_seq(),
            player_id=self.game_state.active_player_id,
        ))

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
            next_holder_conn.send_pdu(pdu_builders.build_priority_grant(
                seq_num=next_holder_conn.next_seq(),
                player_id=next_holder_id,
            ))

    def _next_stack_item_id(self) -> str:
        self._stack_id_counter += 1
        return f"stk_{self._stack_id_counter:02d}"

    def _targets_still_legal(self, targets) -> bool:
        """
        RFC 8.4 step 2 / ERROR code ILLEGAL_TARGET: a target is legal if
        it names a still-seated player or a permanent currently on some
        player's battlefield. This card set has no per-effect target-type
        metadata yet (e.g. "target creature" vs "any target" vs "target
        player"), so this is a structural existence check only -- real
        per-effect target restrictions are Milestone #19's job once
        actual card effects (and their target types) exist.
        """
        with self.lock:
            permanent_ids = {c.card_id for p in self.game_state.players.values() for c in p.board}
            player_ids = set(self.game_state.players.keys())
        return all(t in player_ids or t in permanent_ids for t in targets)

    def _check_state_based_actions(self) -> bool:
        """
        RFC 8.4: checked after every game event, before any priority is
        granted. Applies repeatedly until stable: creatures with lethal
        damage or non-positive toughness die; a player at 0 or less life
        loses (simultaneous zero-life: the Active Player loses, RFC 8.4).
        Placing resulting triggers on the Stack is Milestone #10's job
        (no trigger detection exists yet, so there is nothing to place).

        Returns True if a game-ending condition was found. Actually
        broadcasting GAME_OVER and resetting to LOBBY is Milestone #14's
        job (same boundary as the empty-library check in
        GameServer._run_draw_step) -- this only detects and logs, so
        callers know to stop advancing as if nothing happened.
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
            return False

        # RFC 8.4: "If both players' life totals reach zero or less
        # simultaneously ... the Active Player loses and the Non-Active
        # Player wins."
        loser_id = active_id if len(losers) == 2 else losers[0]
        winner_id = self.game_state.opponent_of(loser_id).player_id
        log(f"[server] LIFE_ZERO: {loser_id} has lost, {winner_id} would win "
            f"-- GAME_OVER broadcast + LOBBY reset is Milestone #14, halting here")
        return True

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
        # Milestone #19: real per-card effect application plugs in here
        # once card effects exist. No effect is implemented yet -- see
        # model/creature.py, instant.py, sorcery.py -- so a legal-target
        # resolution is currently a structural no-op: this milestone
        # delivers the resolution MECHANISM (push, target re-check, pop,
        # broadcast, SBA check, re-grant), not any specific card's effect.
        state_changes = []
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

        with self.lock:
            recipients2 = [(pid, self.clients[self.player_id_to_label[pid]])
                           for pid in self.game_state.players]
        for player_id, conn in recipients2:
            conn.send_pdu(pdu_builders.build_game_state_update_in_game(
                seq_num=conn.next_seq(),
                game_state=self.game_state,
                viewer_player_id=player_id,
            ))

        if game_over:
            return

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

        if not payment_covers_cost(mana_payment, spell) or not caster.pay_mana(mana_payment):
            conn.send_pdu(pdu_builders.build_error(
                seq_num=conn.next_seq(), code="INSUFFICIENT_MANA",
                message="mana_payment does not satisfy this spell's cost.",
                rejected_action=pdu))
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

        if not triggered:
            conn.send_pdu(pdu_builders.build_priority_grant(
                seq_num=conn.next_seq(), player_id=player_id))

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

        conn.send_pdu(pdu_builders.build_priority_grant(
            seq_num=conn.next_seq(), player_id=player_id))

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
                    conn = self.clients[self.player_id_to_label[recipient]]
                conn.send_pdu(pdu_builders.build_priority_grant(
                    seq_num=conn.next_seq(), player_id=recipient))
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

    def _handle_declare_attackers(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_declare_blockers(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

    def _handle_assign_damage_order(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

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

        active_conn = self.clients[self.player_id_to_label[self.game_state.active_player_id]]
        active_conn.send_pdu(pdu_builders.build_priority_grant(
            seq_num=active_conn.next_seq(),
            player_id=self.game_state.active_player_id,
        ))

    def _handle_discard(self, label: str, conn: Connection, pdu: dict) -> None:
        pass

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

        log(f"[server] {player_id} conceded -- winner={winner_id}")

        with self.lock:
            recipients = [self.clients[self.player_id_to_label[pid]]
                        for pid in self.game_state.players]

        for recv_conn in recipients:
            recv_conn.send_pdu(pdu_builders.build_game_over(
                seq_num=recv_conn.next_seq(),
                winner_id=winner_id,
                loser_id=player_id,
                reason="CONCEDE",
            ))

        with self.lock:
            self.game_state = GameState()          # creates a fresh state for the next match
            self.player_id_to_label.clear()
            self.pending_decks.clear()
            self.game_state.lifecycle_state = LifecycleState.LOBBY

        log("[server] returned to LOBBY state after CONCEDE")

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
