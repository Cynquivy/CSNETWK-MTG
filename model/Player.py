import random
from model.card_database import card_database

class player:
    def __init__(self, player_name, player_id):
        self.player_name = player_name
        self.player_id = player_id
        self.life = 20
        self.is_active_player = False
        self.keep_cards = True
        self.mulligan_count = 0
        self.white_mpool = 0
        self.blue_mpool = 0
        self.black_mpool = 0
        self.red_mpool = 0
        self.green_mpool = 0
        self.hand = []
        self.board = []
        self.library = []
        self.exiled = []
        self.graveyard = []
        self.attackers = []
        self.defenders = []
        self.has_priority = False
        self.has_played_land = False
    
    def validate_deck(self, deck_list: list):
        cards_seen = set()
        valid = True
        
        for card_id in deck_list:
            if valid:
                if card_id not in card_database.CARD_DATABASE:
                    valid = False               # CARD DOES NOT EXIST
                elif card_id in cards_seen:
                    valid = False               # THERE IS A DUPLICATE CARD
                else:
                    cards_seen.add(card_id)     # ADD CARD TO SEEN
        
        return valid
    
    def initialize_library(self, deck_list: list):
        if self.validate_deck(deck_list):
            self.library = self.deck_list.copy()
            random.shuffle(self.library)
    
    def draw_from_lib(self, count):
        for _ in range(count):
            if len(self.library) == 0:
                return False
    
            self.hand.append(self.library.pop())
    
        return True
            
    def take_mulligan(self):
        self.mulligan_count += 1
        
        self.library.extend(self.hand)
        self.hand.clear()
        random.shuffle(self.library)
        
        self.draw_from_lib(7)
        
        selected_cards = self.select_and_remove_cards(self.mulligan_count)
        
        for card in selected_cards:
            self.library.insert(0, card)
    
    def keep_hand(self):
        selected_cards = self.select_and_remove_cards(self.mulligan_count)
    
        for card in selected_cards:
            self.library.insert(0, card)
    
    def select_and_remove_cards(self, count):
        selected = []
        
        for _ in range(count):
            card = self.game_ui.get_card_selection(self.hand)
            self.hand.remove(card)
            selected.append(card)

        return selected

    def select_card_to_stack(self):
        pass
    
    def select_cards_to_board(self):
        pass
    
    def set_board(self):
        pass
    
    def declare_attackers(self):
        pass
    
    def declare_blockers(self):
        pass
    
    def select_land(self):
        pass
    
    def select_creature(self):
        pass
        