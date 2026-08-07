from model.card import card

class artifact(card):
    
    def __init__(self, card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness):
        super().__init__(card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness)
        self.set_card_type()
        self.is_tapped = False

    def set_card_type(self):
        self.card_type = "Artifact"
    
    def effect(self):
        pass