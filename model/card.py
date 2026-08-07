from abc import ABC, abstractmethod

class card(ABC):
    def __init__(self, card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness):
        self.card_id = card_id
        self.card_name = ""
        self.set_card_name()
        self.card_type = ""
        # self.subtype = ""
        self.color = color
        self.CMC = CMC
        self.mana_white = mana_white
        self.mana_blue = mana_blue
        self.mana_black = mana_black
        self.mana_red = mana_red
        self.mana_green = mana_green
        self.mana_generic = mana_generic
        self.power = power
        self.toughness = toughness
        self.is_hidden = True
        self.is_selected = False
        self.owner_player_idx = -1

    def set_card_name(self):
        name = self.card_id.rsplit("_", 1)[0]
        name = name.replace("_", " ")
        self.card_name = name.title()
    
    @abstractmethod
    def effect(self):
        pass