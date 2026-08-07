"""
Mana production and payment validation per RFC 0001 (MTGNP v1.0) Section 7.5.

MTGNP has no persistent floating mana pool. Tapping a permanent for mana
does not use the stack and does not require priority, and no separate PDU
exists for activating a mana ability: the client declares a full
mana_payment dict directly on CAST_SPELL / ACTIVATE_ABILITY, and the server
validates it against the spell's cost and atomically taps whichever
mana-producing permanents are needed to realize it, in a single step.
See model/player.py's Player.pay_mana() for the atomic-tap half of this.
"""

# card_id_base -> mana produced by a single tap, using the same WUBRG /
# generic ("X") keys as the wire-format mana_payment field (RFC 10.2.7).
# Colorless output (Sol Ring's "Add {C}{C}") has no dedicated key in that
# schema, so it is counted as generic mana.
MANA_SOURCES = {
    "mountain":       {"R": 1},
    "forest":         {"G": 1},
    "plains":         {"W": 1},
    "island":         {"U": 1},
    "swamp":          {"B": 1},
    "llanowar_elves": {"G": 1},
    "elvish_mystic":  {"G": 1},
    "sol_ring":       {"X": 2},
}

COLORS = ("W", "U", "B", "R", "G")


def card_id_base(card_id: str) -> str:
    """Strip the numeric instance suffix, e.g. 'mountain_001' -> 'mountain'."""
    return card_id.rsplit("_", 1)[0]


def mana_output_of(card_id: str) -> dict:
    """Mana a single tap of this permanent produces; {} if it is not a mana source."""
    return MANA_SOURCES.get(card_id_base(card_id), {})


def payment_covers_cost(mana_payment: dict, spell) -> bool:
    """
    RFC 7.5 / 11 (INSUFFICIENT_MANA): does a declared mana_payment satisfy a
    card's mana cost? Colored pips must be met by matching colors; whatever
    is left over -- plus anything paid under the generic "X" key -- must
    cover the cost's generic portion.
    """
    cost_by_color = {
        "W": spell.mana_white,
        "U": spell.mana_blue,
        "B": spell.mana_black,
        "R": spell.mana_red,
        "G": spell.mana_green,
    }

    remaining = dict(mana_payment)
    for color in COLORS:
        required = cost_by_color[color]
        available = remaining.get(color, 0)
        if available < required:
            return False
        remaining[color] = available - required

    leftover = sum(remaining.get(c, 0) for c in COLORS) + remaining.get("X", 0)
    return leftover >= spell.mana_generic
