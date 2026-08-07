import argparse
import random
import socket
import threading

from network import protocol
from network.protocol import (Connection, ConnectionClosed, ProtocolError, log)
from network import pdu as pdu_builders

from model.phase import Phase, TURN_SEQUENCE
from model.player import player
from model.creature import creature
from model.artifact import artifact
from model.enchantment import enchantment
from model.instant import instant
from model.sorcery import sorcery
# Aliased: this file also defines its own (dead, unreachable -- see
# GameController below) in-file `class GameState`, later in this same
# module. Since that class definition executes at module-load time, it
# would silently overwrite an unaliased `GameState` import in this
# module's namespace before any method ever ran.
from model.game_state import GameState as ModelGameState
from model.lifecycle import LifecycleState

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
        next_holder_id = None
        next_holder_conn = None

        with self.lock:
            self._priority_passes += 1
            # RFC 8.1 rule 6: both players passed consecutively with an
            # empty stack -> the step ends. (A non-empty stack would
            # instead resolve its top item per rule 5 -- Milestone #9;
            # the stack cannot yet be non-empty since nothing pushes to
            # it until CAST_SPELL/ACTIVATE_ABILITY are implemented.)
            if self._priority_passes >= 2 and self.game_state.stack_is_empty:
                advance = True
            else:
                # RFC 8.1 rule 4: priority passes to the other player.
                opponent = self.game_state.opponent_of(player_id)
                next_holder_id = opponent.player_id
                self.game_state.priority_holder_id = next_holder_id
                next_holder_conn = self.clients[self.player_id_to_label[next_holder_id]]

        if advance:
            self._advance_phase()
        else:
            next_holder_conn.send_pdu(pdu_builders.build_priority_grant(
                seq_num=next_holder_conn.next_seq(),
                player_id=next_holder_id,
            ))

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
