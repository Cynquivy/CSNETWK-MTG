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
        # Tapping a land for mana is a cost payment (RFC 0001 Section 7.5),
        # not a stack-resolved effect -- see model/player.py's pay_mana().
        # None of the basic lands have an enter-the-battlefield effect.
        pass