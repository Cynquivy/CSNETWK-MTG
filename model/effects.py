from model.creature import creature
from model.artifact import artifact
from model.enchantment import enchantment
from model.card_database import card_database


def _find_permanent(game_state, permanent_id):
    for owner in game_state.players.values():
        for permanent in owner.board:
            if permanent.card_id == permanent_id:
                return owner, permanent
    return None, None


def _damage(game_state, targets, amount, players_only=False):
    changes = []
    for target_id in targets:
        if target_id in game_state.players:
            game_state.players[target_id].life -= amount
            changes.append({"change_type": "DAMAGE", "target": target_id, "amount": amount})
        elif not players_only:
            _, permanent = _find_permanent(game_state, target_id)
            if isinstance(permanent, creature):
                permanent.damage_marked += amount
                changes.append({"change_type": "DAMAGE", "target": target_id, "amount": amount})
    return changes


def _gain_life(game_state, targets, amount):
    changes = []
    for target_id in targets:
        target_player = game_state.players.get(target_id)
        if target_player is not None:
            target_player.life += amount
            changes.append({"change_type": "LIFE_GAIN", "target": target_id, "amount": amount})
    return changes


def _pump(game_state, targets, power_delta, toughness_delta):
    changes = []
    for target_id in targets:
        _, permanent = _find_permanent(game_state, target_id)
        if isinstance(permanent, creature):
            permanent.power += power_delta
            permanent.toughness += toughness_delta
            changes.append({"change_type": "PUMP", "target": target_id,
                             "power": power_delta, "toughness": toughness_delta})
    return changes


def _destroy(game_state, targets, allowed_types=(creature,)):
    changes = []
    for target_id in targets:
        owner, permanent = _find_permanent(game_state, target_id)
        if permanent is None or not isinstance(permanent, allowed_types):
            continue
        owner.board.remove(permanent)
        owner.graveyard.append(permanent.card_id)
        changes.append({"change_type": "DESTROY", "target": target_id})
    return changes

def _sacrifice(game_state, source_id):
    owner, permanent = _find_permanent(game_state, source_id)

    if permanent is None:
        return []

    owner.board.remove(permanent)
    owner.graveyard.append(permanent.card_id)

    return [{
        "change_type": "SACRIFICE",
        "target": source_id
    }]


def _exile(game_state, targets, gain_life_equal_to_power=False):
    changes = []
    for target_id in targets:
        owner, permanent = _find_permanent(game_state, target_id)
        if not isinstance(permanent, creature):
            continue
        owner.board.remove(permanent)
        owner.exiled.append(permanent.card_id)
        changes.append({"change_type": "EXILE", "target": target_id})
        if gain_life_equal_to_power and permanent.power > 0:
            owner.life += permanent.power
            changes.append({"change_type": "LIFE_GAIN", "target": owner.player_id,
                             "amount": permanent.power})
    return changes


def _counter_spell(game_state, targets):
    changes = []

    for target_id in targets:
        for index, stack_item in enumerate(game_state.stack):
            if stack_item.stack_item_id != target_id:
                continue

            game_state.stack.pop(index)

            controller = game_state.players.get(stack_item.controller_id)

            if controller is not None:
                controller.graveyard.append(stack_item.source_id)

            changes.append({
                "change_type": "COUNTER",
                "target": target_id,
                "card_id": stack_item.source_id
            })

            break

    return changes


def _discard(game_state, targets, count):
    changes = []
    for target_id in targets:
        target_player = game_state.players.get(target_id)
        if target_player is None:
            continue
        to_discard = target_player.hand[:count]
        for card_id in to_discard:
            target_player.hand.remove(card_id)
            target_player.graveyard.append(card_id)
        if to_discard:
            changes.append({"change_type": "DISCARD", "target": target_id, "card_ids": to_discard})
    return changes


EFFECTS = {
    "lightning_bolt": lambda gs, targets: _damage(gs, targets, 3),
    "shock": lambda gs, targets: _damage(gs, targets, 2),
    "searing_spear": lambda gs, targets: _damage(gs, targets, 3),
    "incinerate": lambda gs, targets: _damage(gs, targets, 3),
    "lava_spike": lambda gs, targets: _damage(gs, targets, 3, players_only=True),
    "skullcrack": lambda gs, targets: _damage(gs, targets, 3, players_only=True),
    "rift_bolt": lambda gs, targets: _damage(gs, targets, 3),
    "healing_salve": lambda gs, targets: _gain_life(gs, targets, 3),
    "giant_growth": lambda gs, targets: _pump(gs, targets, 3, 3),
    "doom_blade": lambda gs, targets: _destroy(gs, targets),
    "terror": lambda gs, targets: _destroy(gs, targets),
    "naturalize": lambda gs, targets: _destroy(gs, targets, allowed_types=(artifact, enchantment)),
    "swords_to_plowshares": lambda gs, targets: _exile(gs, targets, gain_life_equal_to_power=True),
    "path_to_exile": lambda gs, targets: _exile(gs, targets),
    "counterspell": lambda gs, targets: _counter_spell(gs, targets),
    "cancel": lambda gs, targets: _counter_spell(gs, targets),
    "mind_rot": lambda gs, targets: _discard(gs, targets, 2),
}


def effect_for(card_id_base: str):
    return EFFECTS.get(card_id_base)


def _goblin_guide_attack(game_state, source_id, controller_id, targets):
    controller = game_state.players.get(controller_id)
    opponent = game_state.opponent_of(controller_id) if controller else None
    if opponent is None or not opponent.library:
        return []
    top_id = opponent.library[-1]
    top_card = card_database.CARD_DATABASE.get(top_id)
    changes = [{"change_type": "REVEAL", "target": top_id}]
    if top_card is not None and getattr(top_card, "card_type", None) == "Land":
        opponent.library.pop()
        opponent.hand.append(top_id)
        changes.append({"change_type": "HAND_ADD", "target": opponent.player_id, "card_id": top_id})
    return changes


def _prowess(game_state, source_id, controller_id, targets):
    _, permanent = _find_permanent(game_state, source_id)
    if not isinstance(permanent, creature):
        return []
    permanent.power += 1
    permanent.toughness += 1
    return [{"change_type": "PUMP", "target": source_id, "power": 1, "toughness": 1}]


def _sacrifice_self(game_state, source_id, controller_id, targets):
    return _sacrifice(game_state, source_id)


def _devotion_drain(game_state, source_id, controller_id, targets, color):
    controller = game_state.players.get(controller_id)
    opponent = game_state.opponent_of(controller_id) if controller else None
    if controller is None or opponent is None:
        return []
    field = {"W": "mana_white", "U": "mana_blue", "B": "mana_black",
              "R": "mana_red", "G": "mana_green"}[color]
    devotion = sum(getattr(permanent, field, 0) for permanent in controller.board)
    if devotion <= 0:
        return []
    opponent.life -= devotion
    controller.life += devotion
    return [
        {"change_type": "DAMAGE", "target": opponent.player_id, "amount": devotion},
        {"change_type": "LIFE_GAIN", "target": controller_id, "amount": devotion},
    ]


def _return_from_graveyard(game_state, source_id, controller_id, targets):
    controller = game_state.players.get(controller_id)
    if controller is None or not targets:
        return []
    target_id = targets[0]
    if target_id not in controller.graveyard:
        return []
    card_obj = card_database.CARD_DATABASE.get(target_id)
    if not isinstance(card_obj, creature):
        return []
    controller.graveyard.remove(target_id)
    controller.hand.append(target_id)
    return [{"change_type": "RETURN_TO_HAND", "target": target_id}]


TRIGGER_EFFECTS = {
    "goblin_guide": _goblin_guide_attack,
    "monastery_swiftspear": _prowess,
    "phantasmal_bear": _sacrifice_self,
    "gray_merchant": lambda gs, sid, cid, targets: _devotion_drain(gs, sid, cid, targets, "B"),
    "gravedigger": _return_from_graveyard,
}


def trigger_effect_for(card_id_base: str):
    return TRIGGER_EFFECTS.get(card_id_base)
