
class Card:
    def __init__(self, card_id_base, card_name, card_type, subtype, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness):
        self.card_id_base = card_id_base
        self.card_name = card_name
        self.card_type = card_type
        self.subtype = subtype
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