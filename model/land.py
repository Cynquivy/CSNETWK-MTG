from model.card import card

class land(card):
    
    def __init__(self, card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness):
        super().__init__(card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness)
        self.set_card_type()
        self.is_tappable = False
        self.is_tapped = False
    
    def set_card_type(self):
        self.card_type = "Land"
    
    def effect(self, controller):
        atk_player = controller.players[controller.state.AP_idx]
        
        match self.card_id:
            case "mountain":
                atk_player.red_mpool += 1
            
            case "forest":
                atk_player.green_mpool += 1
            
            case "plains":
                atk_player.white_mpool += 1
            
            case "island":
                atk_player.blue_mpool += 1
                
            case "swamp":
                atk_player.black_mpool += 1
        
        self.is_tapped = True