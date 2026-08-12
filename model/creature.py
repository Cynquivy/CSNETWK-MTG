from model.card import card

class creature(card):
    
    def __init__(self, card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness):
        super().__init__(card_id, color, CMC, mana_white, mana_blue, mana_black, mana_red, mana_green, mana_generic, power, toughness)
        self.set_card_type()
        self.base_power = power
        self.base_toughness = toughness
        self.will_attack = False
        self.will_block = False
        self.is_tappable = False
        self.is_tapped = False
        self.has_haste = (
            card_id.startswith("monastery_swiftspear_")
            or card_id.startswith("goblin_guide_")
        )
        self.is_flying = False
        self.did_kick = False
        self.has_madness = False
        self.blocked_by = []
        self.damage_order = []
        self.damage_marked = 0
        self.was_blocked = False
        self.has_first_strike = False
        self.has_double_strike = False
        self.attack_target = None
        # RFC 0001 Section 3 "Summoning Sickness" / Section 10.2.2 wire field:
        # true from the moment a creature enters the battlefield until its
        # controller's next Untap Step (cleared there, not here -- that
        # transition is the turn engine's job, Milestone #8).
        self.summoning_sick = True
    
    def set_card_type(self):
        self.card_type = "Creature"

    def effect(self, controller):
        pass
