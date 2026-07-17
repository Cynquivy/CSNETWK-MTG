from model import Card
from model import Land

class Creature(Card):
    
    def __init__(self, power, toughness):
        self.power = power
        self.toughness = toughness
        self.card_id = ""
        self.will_attack = False
        self.will_block = False
        self.is_tappable = False
        self.is_tapped = False
        self.has_haste = False
        self.is_flying = False
        self.did_kick = False
        self.has_madness = False
    
    def effect(self, controller):
        
        atk_player = controller.players[controller.atk_player_index]
        def_player = controller.players[controller.def_player_index]
        
        
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
                    if isinstance(top_card, Land):
                        def_player.hand.append(top_card)
                        def_player.library.pop()
                
            case "goblin_bushwhacker":
                # Kicker {1}{R}
                if self.did_kick:
                    for card in atk_player.board:
                        if isinstance(card, Creature):
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
                # Add {G}
                if self.is_tapped:
                    atk_player.green_mpool += 1
            case "elvish_mystic":
                # Add {G}
                if self.is_tapped:
                    atk_player.green_mpool += 1
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
                