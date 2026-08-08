from model.card import card
from model.land import land

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
        self.has_haste = False
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
    
    def place_to_board(self):
        pass
    
    def effect(self, controller):
        
        atk_player = controller.players[controller.state.AP_idx]
        def_player = controller.players[controller.state.NAP_idx]
        
        match self.card_id:
            # RED CREATURES
            case "goblin_guide":
                # Haste
                if not self.has_haste:
                    self.has_haste = True
                
                # Reveal lib top card
                if self.will_attack:
                    top_card = def_player.library.top()
                    top_card.is_hidden = False
                    if isinstance(top_card, land):
                        def_player.hand.append(top_card)
                        def_player.library.pop()
                
            case "goblin_bushwhacker":
                # Kicker {1}{R}
                if self.did_kick:
                    for card in atk_player.board:
                        if isinstance(card, creature):
                            card.power += 1
                            if not card.has_haste:
                                card.has_haste = True
            case "reckless_wurm":
                # Madness {2}{R}
                return 3
            case "monastery_swiftspear":
                # Haste
                if not self.has_haste:
                    self.has_haste = True
                    
                # Prowess
                return 4
            case "wall_of_stone":
                pass
            
            # BLUE CREATURES
            case "merfolk_looter":
                # Draw and Discard
                if self.is_tapped:
                    top_card = atk_player.library.pop()
                    atk_player.hand.append(top_card)
                    controller.discardCard(atk_player)
            case "prodigal_sorcerer":
                # 1 dmg any target
                return 6
            case "air_elemental":
                # Flying status
                if not self.is_flying:
                    self.is_flying = True
                return 7
            case "phantasmal_bear":
                # Illusion
                return 8
            
            # GREEN CREATURES
            case "llanowar_elves":
                # Tap: Add {G} -- a mana-ability cost payment (RFC 0001
                # Section 7.5), not a stack effect; see
                # model/player.py's pay_mana().
                pass
            case "elvish_mystic":
                # Tap: Add {G} -- same as above.
                pass
            case "grizzly_bears":
                pass
            case "leatherback_baloth":
                pass
            case "troll_ascetic":
                return 8
            
            # WHITE CREATURES
            case "white_knight":
                return 9
            case "serra_angel":
                return 10
            case "savannah_lions":
                return 11
            case "mother_of_runes":
                return 12
            
            
            # BLACK CREATURES
            case "gray_merchant":
                return 13
            case "gravedigger":
                return 14
            case "royal_assassin":
                return 15
            case "black_knight":
                return 16
            
            # COLORLESS CREATURES
            
            case "ornithopter":
                return 17
                
