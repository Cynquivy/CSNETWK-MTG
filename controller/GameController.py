from model.Phase import Phase

    class GameController:
        def __init__(self):
            self.phase = Phase.START
            self.game_start = True
            self.game_over = False
            self.atk_player_index = 0
            self.def_player_index = 1
            self.players = []
            
            self.initialize_library()
            self.draw_from_lib(0, 7)
            self.draw_from_lib(1, 7)
            
    def initialize_library(self):
        pass
    
    def draw_from_lib(self, player_index, card_num):
        for _ in range(card_num):
            self.players[player_index].hand.append(self.players[player_index].library[-1])
            self.players[player_index].library.pop()
            
    
    def do_game_start(self):
        pass
    
    def do_beginning(self):
        pass
    
    def do_main_one(self):
        pass
    
    def do_combat(self):
        pass
    
    def do_main_two(self):
        pass
    
    def do_end(self):
        pass
       
    def next_phase(self):
        match self.phase:
            case Phase.START:
                self.phase = Phase.UNTAP
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
                self.active_player_index = (self.active_player_index + 1) % 2
                self.phase = Phase.UNTAP
        
        