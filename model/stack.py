from enum import Enum


class StackItemType(Enum):
    """RFC 0001 Section 8.3: every Stack item is one of these three kinds."""
    SPELL = "SPELL"
    ABILITY = "ABILITY"
    TRIGGER_ABILITY = "TRIGGER_ABILITY"


class StackItem:
    """
    A single entry on the Stack (RFC 0001 Section 8.3): "Each Stack item
    contains: stack_item_id ... item_type ... source_id ... controller_id
    ... targets". Attribute names here follow that prose. The wire-format
    STACK_PUSH / GAME_STATE_UPDATE.state.stack schemas (RFC 10.2.9 /
    10.2.2) use the shorter keys "source" and "controller" for the same
    two values -- mapping this object to that JSON shape is the PDU
    serializer's job (Milestone #4), not this class's.
    """

    def __init__(self, stack_item_id, item_type: StackItemType, source_id, controller_id, targets=None):
        self.stack_item_id = stack_item_id
        self.item_type = item_type
        self.source_id = source_id
        self.controller_id = controller_id
        self.targets = list(targets) if targets else []

    def __repr__(self):
        return (f"StackItem({self.stack_item_id}, {self.item_type.value}, "
                f"source={self.source_id}, controller={self.controller_id}, "
                f"targets={self.targets})")
