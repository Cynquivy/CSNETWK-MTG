from enum import Enum, auto

class Phase(Enum):
    START = auto()
    UNTAP = auto()
    UPKEEP = auto()
    DRAW = auto()
    MAIN_ONE = auto()
    COMBAT_BEGINNING = auto()
    DECLARE_ATTACKERS = auto() 
    DECLARE_BLOCKERS = auto()
    COMBAT_EXECUTE = auto()
    COMBAT_ENDING = auto()
    MAIN_TWO = auto()
    END_STEP = auto()
    CLEANUP = auto()