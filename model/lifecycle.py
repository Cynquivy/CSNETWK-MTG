from enum import Enum

class LifecycleState(Enum):
    """
    The five top-level game lifecycle states (RFC 0001 Section 6.1,
    Figure 3):

        LOBBY --> GAME_SETUP --> MULLIGAN --> IN_GAME --> GAME_OVER
          ^                                                   |
          +---------------------------------------------------+

    Distinct from model.phase.Phase, which only enumerates the fourteen
    turn phases/steps that exist while this state is IN_GAME. A
    GAME_STATE_UPDATE's state.phase field (RFC 10.2.2) is "LOBBY" during
    LOBBY, "MULLIGAN" during MULLIGAN, or one of the Phase values during
    IN_GAME -- GAME_SETUP is fully automatic (RFC 6.3) and its own
    broadcast already reports the following MULLIGAN state, and GAME_OVER
    is reported via a dedicated GAME_OVER PDU rather than a
    GAME_STATE_UPDATE.
    """
    LOBBY = "LOBBY"
    GAME_SETUP = "GAME_SETUP"
    MULLIGAN = "MULLIGAN"
    IN_GAME = "IN_GAME"
    GAME_OVER = "GAME_OVER"
