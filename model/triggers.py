"""
Triggered-ability registry (RFC 0001 Section 8.6): which permanents in
the fixed card set have a "When"/"Whenever"/"At" triggered ability, and
what event fires it. Sourced directly from docs/mtgnp_master_card_list.xlsx's
"Simplified Effect" column -- only cards whose effect text actually
contains one of those three keywords are triggered abilities; activated
("Tap:"/"{cost}:") abilities and static abilities (e.g. "Flying",
"Defender") are out of scope here, same as they were out of scope for
model/mana.py's mana-source registry.

As with MANA_SOURCES (Milestone #2) and the stack-resolution effect hook
(Milestone #9), this records WHICH permanent/event pairs trigger -- it
does not implement what firing actually DOES to game state. No card's
effect() method is implemented yet, so a fired trigger currently resolves
as a structural no-op once placed on the Stack, exactly like a resolved
spell: this milestone delivers the trigger detection/ordering/choice
MECHANISM, not any specific card's effect (Milestone #19).
"""

from enum import Enum


class TriggerEvent(Enum):
    """
    RFC 8.6.1 lists these (and others) as events that MUST cause a
    trigger check. Only the events actually used by TRIGGER_SOURCES below
    are enumerated -- add more here if a future card needs one.
    """
    ENTERS_BATTLEFIELD = "ENTERS_BATTLEFIELD"
    ATTACKS = "ATTACKS"
    BECOMES_TARGET = "BECOMES_TARGET"
    CAST_NONCREATURE_SPELL = "CAST_NONCREATURE_SPELL"


class TriggerDefinition:
    def __init__(self, event: TriggerEvent, requires_target: bool, optional: bool, summary: str):
        self.event = event
        self.requires_target = requires_target
        self.optional = optional
        self.summary = summary


# card_id_base -> TriggerDefinition. Every entry's summary is quoted
# directly from the master card list's Simplified Effect column.
#
# Note: RFC 8.6.3's own illustrative example uses gray_merchant_001 as a
# "you may gain life" OPTIONAL trigger -- but the actual master card list
# describes Gray Merchant's life change as mandatory ("You gain that much
# life", no "may"). The card database is the authoritative source for
# what a real card does, so it is registered here as optional=False; the
# RFC's own example is only followed as an illustrative choice of vehicle
# in this project's docs/tests, not as evidence the real card is optional.
TRIGGER_SOURCES = {
    "goblin_guide": TriggerDefinition(
        event=TriggerEvent.ATTACKS, requires_target=False, optional=False,
        summary="Whenever Goblin Guide attacks, defending player reveals top "
                "card of library. If it's a land, that player puts it into "
                "their hand.",
    ),
    "monastery_swiftspear": TriggerDefinition(
        event=TriggerEvent.CAST_NONCREATURE_SPELL, requires_target=False, optional=False,
        summary="Prowess: whenever you cast a noncreature spell, this "
                "creature gets +1/+1 until end of turn.",
    ),
    "phantasmal_bear": TriggerDefinition(
        event=TriggerEvent.BECOMES_TARGET, requires_target=False, optional=False,
        summary="When Phantasmal Bear becomes the target of a spell or "
                "ability, sacrifice it.",
    ),
    "gray_merchant": TriggerDefinition(
        event=TriggerEvent.ENTERS_BATTLEFIELD, requires_target=False, optional=False,
        summary="When Gray Merchant enters, each opponent loses X life "
                "(X = your devotion to black). You gain that much life.",
    ),
    "gravedigger": TriggerDefinition(
        event=TriggerEvent.ENTERS_BATTLEFIELD, requires_target=True, optional=False,
        summary="When Gravedigger enters, return target creature card from "
                "your graveyard to your hand.",
    ),
}


def trigger_definition_for(card_id: str):
    """The TriggerDefinition for this card instance, or None if it has none."""
    base = card_id.rsplit("_", 1)[0]
    return TRIGGER_SOURCES.get(base)
