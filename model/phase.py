from enum import Enum

class Phase(Enum):
    """
    The fourteen turn phases/steps of RFC 0001 (MTGNP v1.0) Section 7
    (Figure 4) and Section 9 (Figure 6, combat sub-state machine). Member
    *values* are the exact wire strings used in PHASE_TRANSITION's
    from_phase/to_phase and GAME_STATE_UPDATE's state.phase (RFC 10.2.4)
    while the game is IN_GAME -- see model/lifecycle.py for the broader
    LOBBY / GAME_SETUP / MULLIGAN / IN_GAME / GAME_OVER state machine
    (RFC 6.1) that these turn phases only exist within.
    """
    UNTAP = "UNTAP"
    UPKEEP = "UPKEEP"
    DRAW = "DRAW"
    PRECOMBAT_MAIN = "PRECOMBAT_MAIN"
    BEGIN_COMBAT = "BEGIN_COMBAT"
    DECLARE_ATTACKERS = "DECLARE_ATTACKERS"
    DECLARE_BLOCKERS = "DECLARE_BLOCKERS"
    ASSIGN_DAMAGE_ORDER = "ASSIGN_DAMAGE_ORDER"
    FIRST_STRIKE_DAMAGE = "FIRST_STRIKE_DAMAGE"
    COMBAT_DAMAGE = "COMBAT_DAMAGE"
    END_OF_COMBAT = "END_OF_COMBAT"
    POSTCOMBAT_MAIN = "POSTCOMBAT_MAIN"
    END_STEP = "END_STEP"
    CLEANUP = "CLEANUP"


# The fixed, unconditional turn sequence (RFC Figure 4 / Figure 6).
# FIRST_STRIKE_DAMAGE is deliberately excluded here: RFC 9.6 -- "This step
# occurs only if at least one attacking or blocking creature has first
# strike or double strike" -- so it is conditionally spliced in between
# ASSIGN_DAMAGE_ORDER and COMBAT_DAMAGE by the combat engine, not a fixed
# member of the linear sequence.
TURN_SEQUENCE = (
    Phase.UNTAP,
    Phase.UPKEEP,
    Phase.DRAW,
    Phase.PRECOMBAT_MAIN,
    Phase.BEGIN_COMBAT,
    Phase.DECLARE_ATTACKERS,
    Phase.DECLARE_BLOCKERS,
    Phase.ASSIGN_DAMAGE_ORDER,
    Phase.COMBAT_DAMAGE,
    Phase.END_OF_COMBAT,
    Phase.POSTCOMBAT_MAIN,
    Phase.END_STEP,
    Phase.CLEANUP,
)

# Steps that open no priority window at all (RFC 7.2 Untap, 7.8 Cleanup).
NO_PRIORITY_PHASES = (Phase.UNTAP, Phase.CLEANUP)
