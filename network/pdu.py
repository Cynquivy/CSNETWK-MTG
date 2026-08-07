"""
PDU builder functions for all 25 MTGNP message types (RFC 0001 v1.0,
Section 10 "PDU Reference" / Section 10.2 "PDU Schemas" -- the RFC's own
words: "The following subsections provide a complete JSON schema for
every PDU defined in this document").

Scope: these functions ONLY build a dict matching the exact wire shape for
each PDU type. They do not decide *what* seq_num to use (RFC 5.4's echo
rules -- Milestone #5), do not validate incoming PDUs, and do not contain
any game-rule logic (targeting legality, mana validation, combat math --
those stay in the model/ layer and later handlers). Every builder takes
seq_num as an explicit argument for a uniform, unsurprising call
interface; network.protocol.Connection.send_pdu() will still auto-assign
one if a caller ever omits the key, but nothing here relies on that.

Two known RFC internal inconsistencies, resolved by favoring Section 10.2
(the document's own designated formal schema reference) over the
narrative examples in Section 6.3 and the separate Examples.pdf:

  1. GAME_STATE_UPDATE's in-game "hand" field: Section 10.2.2 shows it as
     a single-key dict {"<viewer_player_id>": [...]}; Section 6.3's prose
     example and Examples.pdf both show a flat array instead. Implemented
     here as the Section 10.2.2 dict shape.
  2. STACK_RESOLVE's state_changes entries: Section 10.2.14 keys the
     variant tag "change_type"; Examples.pdf's Step 16 walkthrough uses
     "type" instead. This module does not construct state_changes content
     at all (see build_stack_resolve) -- that is Milestone #9/#19's
     concern -- so this inconsistency is simply passed through as-is;
     flagging it here for whoever builds that content.
"""

from model.lifecycle import LifecycleState
from model.creature import creature


# ---------------------------------------------------------------------
# internal helpers (not part of the public 25-PDU surface)
# ---------------------------------------------------------------------

def _card_id_of(entry):
    """
    Zones may hold either bare card_id strings (hand/library/graveyard,
    per how Player.initialize_library/draw_from_lib work today) or actual
    card objects (battlefield, which needs per-instance is_tapped/damage
    state). Accept either so callers don't have to care.
    """
    return entry.card_id if hasattr(entry, "card_id") else entry


def _permanent_wire(card) -> dict:
    """
    One battlefield permanent's wire shape (RFC 10.2.2): non-creatures
    get {"id", "tapped"}; creatures additionally get {"damage", "power",
    "toughness", "summoning_sick"}.
    """
    wire = {"id": card.card_id, "tapped": bool(getattr(card, "is_tapped", False))}
    if isinstance(card, creature):
        wire["damage"] = card.damage_marked
        wire["power"] = card.power
        wire["toughness"] = card.toughness
        wire["summoning_sick"] = card.summoning_sick
    return wire


def _stack_item_wire(item) -> dict:
    """
    A Stack item's wire shape (RFC 10.2.2 / 10.2.9). model.stack.StackItem
    uses the RFC 8.3 prose names source_id/controller_id; the wire schema
    uses the shorter source/controller for the same values.
    """
    return {
        "stack_item_id": item.stack_item_id,
        "item_type": item.item_type.value,
        "source": item.source_id,
        "targets": list(item.targets),
        "controller": item.controller_id,
    }


# ---------------------------------------------------------------------
# 10.2.1 PLAYER_READY (C->S)
# ---------------------------------------------------------------------

def build_player_ready(seq_num, player_id, deck_list) -> dict:
    return {
        "type": "PLAYER_READY",
        "seq_num": seq_num,
        "player_id": player_id,
        "deck_list": list(deck_list),
    }


# ---------------------------------------------------------------------
# 10.2.2 GAME_STATE_UPDATE (S->C) -- two variants
# ---------------------------------------------------------------------

def build_game_state_update_lobby(seq_num, players_ready, waiting_for) -> dict:
    """The LOBBY-phase variant: lobby metadata only, no game state yet."""
    return {
        "type": "GAME_STATE_UPDATE",
        "seq_num": seq_num,
        "state": {
            "phase": "LOBBY",
            "players_ready": players_ready,
            "waiting_for": list(waiting_for),
        },
    }


def build_game_state_update_in_game(seq_num, game_state, viewer_player_id) -> dict:
    """
    The MULLIGAN / IN_GAME variant (RFC 10.2.2), personalized for one
    viewing player: only that player's own hand is included; the
    opponent appears solely as a card count in hand_counts (RFC 3
    "Visible State": "Each player's hand is hidden from the opponent").
    """
    if game_state.lifecycle_state == LifecycleState.IN_GAME and game_state.phase is not None:
        phase_str = game_state.phase.value
    else:
        # MULLIGAN reports "MULLIGAN" directly (RFC 6.3 example); this
        # also degrades gracefully for any other non-IN_GAME state.
        phase_str = game_state.lifecycle_state.value

    opponent = game_state.opponent_of(viewer_player_id)

    battlefield = {
        pid: [_permanent_wire(card) for card in board]
        for pid, board in game_state.battlefield.items()
    }
    graveyard = {
        pid: [_card_id_of(c) for c in gy]
        for pid, gy in game_state.graveyard.items()
    }

    state = {
        "turn": game_state.turn,
        "active_player": game_state.active_player_id,
        "phase": phase_str,
        "priority_holder": game_state.priority_holder_id,
        "life_totals": game_state.life_totals,
        "stack": [_stack_item_wire(item) for item in game_state.stack],
        "battlefield": battlefield,
        "graveyard": graveyard,
        "hand": {viewer_player_id: list(game_state.hand_of(viewer_player_id))},
        "hand_counts": (
            {opponent.player_id: len(opponent.hand)} if opponent is not None else {}
        ),
        "library_counts": game_state.library_counts,
        "land_played_this_turn": game_state.land_played_this_turn,
    }
    return {"type": "GAME_STATE_UPDATE", "seq_num": seq_num, "state": state}


# ---------------------------------------------------------------------
# 10.2.3 MULLIGAN_CHOICE (C->S)
# ---------------------------------------------------------------------

def build_mulligan_choice(seq_num, keep, cards_to_bottom) -> dict:
    return {
        "type": "MULLIGAN_CHOICE",
        "seq_num": seq_num,
        "keep": bool(keep),
        "cards_to_bottom": list(cards_to_bottom),
    }


# ---------------------------------------------------------------------
# 10.2.4 PHASE_TRANSITION (S->ALL)
# ---------------------------------------------------------------------

def build_phase_transition(seq_num, from_phase, to_phase, active_player_id, turn) -> dict:
    """from_phase/to_phase accept either a Phase member or its raw string value."""
    return {
        "type": "PHASE_TRANSITION",
        "seq_num": seq_num,
        "from_phase": getattr(from_phase, "value", from_phase),
        "to_phase": getattr(to_phase, "value", to_phase),
        "active_player": active_player_id,
        "turn": turn,
    }


# ---------------------------------------------------------------------
# 10.2.5 PRIORITY_GRANT (S->C)
# ---------------------------------------------------------------------

def build_priority_grant(seq_num, player_id, time_limit_ms=60000) -> dict:
    return {
        "type": "PRIORITY_GRANT",
        "player_id": player_id,
        "seq_num": seq_num,
        "time_limit_ms": time_limit_ms,
    }


# ---------------------------------------------------------------------
# 10.2.6 PRIORITY_PASS (C->S)
# ---------------------------------------------------------------------

def build_priority_pass(seq_num) -> dict:
    return {"type": "PRIORITY_PASS", "seq_num": seq_num}


# ---------------------------------------------------------------------
# 10.2.7 CAST_SPELL (C->S)
# ---------------------------------------------------------------------

def build_cast_spell(seq_num, card_id, targets, mana_payment) -> dict:
    return {
        "type": "CAST_SPELL",
        "seq_num": seq_num,
        "card_id": card_id,
        "targets": list(targets),
        "mana_payment": dict(mana_payment),
    }


# ---------------------------------------------------------------------
# 10.2.8 ACTIVATE_ABILITY (C->S)
# ---------------------------------------------------------------------

def build_activate_ability(seq_num, source_id, ability_index, targets, tap, mana) -> dict:
    """
    Note the shape here is deliberately NOT the same as CAST_SPELL's
    mana_payment: RFC 10.2.8 nests mana under cost_payment.mana alongside
    a separate cost_payment.tap bool.
    """
    return {
        "type": "ACTIVATE_ABILITY",
        "seq_num": seq_num,
        "source_id": source_id,
        "ability_index": ability_index,
        "targets": list(targets),
        "cost_payment": {"tap": bool(tap), "mana": dict(mana)},
    }


# ---------------------------------------------------------------------
# 10.2.9 STACK_PUSH (S->ALL)
# ---------------------------------------------------------------------

def build_stack_push(seq_num, stack_item) -> dict:
    wire = _stack_item_wire(stack_item)
    wire["type"] = "STACK_PUSH"
    wire["seq_num"] = seq_num
    return wire


# ---------------------------------------------------------------------
# 10.2.10 TRIGGER_ORDER (S->C)
# ---------------------------------------------------------------------

def build_trigger_order(seq_num, player_id, trigger_ids) -> dict:
    return {
        "type": "TRIGGER_ORDER",
        "seq_num": seq_num,
        "player_id": player_id,
        "trigger_ids": list(trigger_ids),
    }


# ---------------------------------------------------------------------
# 10.2.11 TRIGGER_ORDER_RESPONSE (C->S)
# ---------------------------------------------------------------------

def build_trigger_order_response(seq_num, ordered_trigger_ids) -> dict:
    return {
        "type": "TRIGGER_ORDER_RESPONSE",
        "seq_num": seq_num,
        "ordered_trigger_ids": list(ordered_trigger_ids),
    }


# ---------------------------------------------------------------------
# 10.2.12 TRIGGER_CHOICE (S->C)
# ---------------------------------------------------------------------

def build_trigger_choice(seq_num, trigger_id, source_id, effect_summary,
                          requires_target, legal_targets) -> dict:
    return {
        "type": "TRIGGER_CHOICE",
        "seq_num": seq_num,
        "trigger_id": trigger_id,
        "source_id": source_id,
        "effect_summary": effect_summary,
        "requires_target": bool(requires_target),
        "legal_targets": list(legal_targets),
    }


# ---------------------------------------------------------------------
# 10.2.13 TRIGGER_CHOICE_RESPONSE (C->S)
# ---------------------------------------------------------------------

def build_trigger_choice_response(seq_num, trigger_id, accept, chosen_target=None) -> dict:
    return {
        "type": "TRIGGER_CHOICE_RESPONSE",
        "seq_num": seq_num,
        "trigger_id": trigger_id,
        "accept": bool(accept),
        "chosen_target": chosen_target,
    }


# ---------------------------------------------------------------------
# 10.2.14 STACK_RESOLVE (S->ALL)
# ---------------------------------------------------------------------

def build_stack_resolve(seq_num, stack_item_id, result, state_changes) -> dict:
    """result MUST be "RESOLVED" or "FIZZLE" (RFC 8.4)."""
    return {
        "type": "STACK_RESOLVE",
        "seq_num": seq_num,
        "stack_item_id": stack_item_id,
        "result": result,
        "state_changes": list(state_changes),
    }


# ---------------------------------------------------------------------
# 10.2.15 DECLARE_ATTACKERS (C->S)
# ---------------------------------------------------------------------

def build_declare_attackers(seq_num, attackers) -> dict:
    """attackers: list of {"creature_id": ..., "target": ...}; [] = no attack."""
    return {
        "type": "DECLARE_ATTACKERS",
        "seq_num": seq_num,
        "attackers": list(attackers),
    }


# ---------------------------------------------------------------------
# 10.2.16 DECLARE_BLOCKERS (C->S)
# ---------------------------------------------------------------------

def build_declare_blockers(seq_num, blockers) -> dict:
    """blockers: list of {"creature_id": ..., "blocking_id": ...}; [] = no blocks."""
    return {
        "type": "DECLARE_BLOCKERS",
        "seq_num": seq_num,
        "blockers": list(blockers),
    }


# ---------------------------------------------------------------------
# 10.2.17 ASSIGN_DAMAGE_ORDER (C->S)
# ---------------------------------------------------------------------

def build_assign_damage_order(seq_num, attacker_id, blocker_order) -> dict:
    return {
        "type": "ASSIGN_DAMAGE_ORDER",
        "seq_num": seq_num,
        "attacker_id": attacker_id,
        "blocker_order": list(blocker_order),
    }


# ---------------------------------------------------------------------
# 10.2.18 COMBAT_DAMAGE_RESULT (S->ALL)
# ---------------------------------------------------------------------

def build_combat_damage_result(seq_num, damage_events, life_totals, creatures_died) -> dict:
    """damage_events: list of {"source": ..., "target": ..., "amount": ...}."""
    return {
        "type": "COMBAT_DAMAGE_RESULT",
        "seq_num": seq_num,
        "damage_events": list(damage_events),
        "life_totals": dict(life_totals),
        "creatures_died": list(creatures_died),
    }


# ---------------------------------------------------------------------
# 10.2.19 PLAY_LAND (C->S)
# ---------------------------------------------------------------------

def build_play_land(seq_num, card_id) -> dict:
    return {"type": "PLAY_LAND", "seq_num": seq_num, "card_id": card_id}


# ---------------------------------------------------------------------
# 10.2.20 DISCARD (C->S)
# ---------------------------------------------------------------------

def build_discard(seq_num, card_ids) -> dict:
    return {"type": "DISCARD", "seq_num": seq_num, "card_ids": list(card_ids)}


# ---------------------------------------------------------------------
# 10.2.21 CONCEDE (C->S)
# ---------------------------------------------------------------------

def build_concede(seq_num, player_id) -> dict:
    return {"type": "CONCEDE", "seq_num": seq_num, "player_id": player_id}


# ---------------------------------------------------------------------
# 10.2.22 GAME_OVER (S->ALL)
# ---------------------------------------------------------------------

def build_game_over(seq_num, winner_id, loser_id, reason) -> dict:
    """reason MUST be one of LIFE_ZERO | DECK_EMPTY | CONCEDE | DISCONNECT (RFC 6.6)."""
    return {
        "type": "GAME_OVER",
        "seq_num": seq_num,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "reason": reason,
    }


# ---------------------------------------------------------------------
# 10.2.23 ERROR (S->C)
# ---------------------------------------------------------------------

def build_error(seq_num, code, message, rejected_action=None) -> dict:
    """
    RFC 11 defines the full set of valid `code` values (INVALID_JSON,
    ILLEGAL_DECK, UNKNOWN_TYPE, STALE_ACTION, NOT_YOUR_PRIORITY,
    ILLEGAL_ACTION, ILLEGAL_TARGET, TRIGGER_ORDER_INVALID,
    TRIGGER_CHOICE_INVALID, INSUFFICIENT_MANA, WRONG_PHASE,
    DUPLICATE_ID) -- not enforced here, since this function only builds
    the envelope; callers (Milestone #16) supply the correct code.
    """
    pdu = {
        "type": "ERROR",
        "seq_num": seq_num,
        "code": code,
        "message": message,
    }
    if rejected_action is not None:
        pdu["rejected_action"] = rejected_action
    return pdu


# ---------------------------------------------------------------------
# 10.2.24 PING (C->S)
# ---------------------------------------------------------------------

def build_ping(seq_num, timestamp) -> dict:
    return {"type": "PING", "seq_num": seq_num, "timestamp": timestamp}


# ---------------------------------------------------------------------
# 10.2.25 PONG (S->C)
# ---------------------------------------------------------------------

def build_pong(seq_num, timestamp) -> dict:
    return {"type": "PONG", "seq_num": seq_num, "timestamp": timestamp}
