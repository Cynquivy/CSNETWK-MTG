import random
from model.card_database import card_database
from model.mana import mana_output_of, COLORS

class player:
    def __init__(self, player_name, player_id):
        self.player_name = player_name
        self.player_id = player_id
        self.life = 20
        self.is_active_player = False
        self.keep_cards = True
        self.mulligan_count = 0
        self.hand = []
        self.board = []
        self.library = []
        self.exiled = []
        self.graveyard = []
        self.attackers = []
        self.defenders = []
        self.has_priority = False
        self.land_played_this_turn = False  # RFC 10.2.2 field name; reset every Untap Step (RFC 7.2)
    
    @staticmethod
    def validate_deck(deck_list):
        """
        RFC 0001 Section 6.3 step 1 / Section 11 ILLEGAL_DECK: "The
        submitted deck_list is empty, contains more than 50 cards, or
        includes one or more cards not in the legal card set." A
        staticmethod (no self) so LOBBY handling can validate a deck
        before any Player object exists (RFC 6.2: decks are validated at
        PLAYER_READY time, before GAME_SETUP ever constructs a player).

        Returns None if legal, or a human-readable reason string
        otherwise -- the ILLEGAL_DECK example in RFC 6.2 ("Deck contains
        51 cards; maximum is 50.") is a specific message, not just a
        boolean, so callers need the reason to report it faithfully.

        Duplicate card_id entries are also rejected, even though not
        spelled out verbatim in the ILLEGAL_DECK definition above: each
        card_id in the fixed catalog (docs/mtgnp_master_card_list.xlsx,
        "Card Instances") names one unique physical card instance, so
        repeating an id would mean claiming two copies of the exact same
        physical card, which the fixed 312-card set does not allow.
        """
        if not isinstance(deck_list, list) or len(deck_list) == 0:
            return "Deck is empty; minimum is 1 card."
        if len(deck_list) > 50:
            return f"Deck contains {len(deck_list)} cards; maximum is 50."

        cards_seen = set()
        for card_id in deck_list:
            if card_id not in card_database.CARD_DATABASE:
                return f"'{card_id}' is not a card in the fixed card set."
            if card_id in cards_seen:
                return f"'{card_id}' appears more than once in the deck."
            cards_seen.add(card_id)

        return None

    def initialize_library(self, deck_list: list):
        if player.validate_deck(deck_list) is None:
            self.deck_list = deck_list
            self.library = list(self.deck_list)
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

    def untapped_mana_sources(self):
        """Permanents on this player's battlefield currently tappable for mana (RFC 7.5)."""
        return [permanent for permanent in self.board
                if not getattr(permanent, "is_tapped", True)
                and mana_output_of(permanent.card_id)]

    def pay_mana(self, mana_payment: dict) -> bool:
        """
        Atomically tap enough untapped mana-producing permanents to realize a
        declared mana_payment (RFC 0001 Section 7.5: mana production is
        implicit -- no separate mana-ability PDU exists). Either every source
        needed is found and tapped, or nothing on the board changes and
        False is returned.
        """
        needed = {color: mana_payment.get(color, 0) for color in COLORS}
        needed["X"] = mana_payment.get("X", 0)

        sources = self.untapped_mana_sources()
        to_tap = []

        # Satisfy each declared colored amount first, one matching source per unit.
        for color in COLORS:
            while needed[color] > 0:
                source = next((s for s in sources if s not in to_tap
                               and mana_output_of(s.card_id).get(color, 0) > 0), None)
                if source is None:
                    return False
                to_tap.append(source)
                needed[color] -= 1

        # Satisfy the declared generic ("X") amount from whatever is left untapped.
        for source in sources:
            if needed["X"] <= 0:
                break
            if source in to_tap:
                continue
            to_tap.append(source)
            needed["X"] -= sum(mana_output_of(source.card_id).values())

        if needed["X"] > 0:
            return False

        for source in to_tap:
            source.is_tapped = True
        return True

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
        