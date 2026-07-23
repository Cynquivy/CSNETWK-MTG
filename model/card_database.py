from model.land import land
from model.creature import creature
from model.instant import instant
from model.sorcery import sorcery
from model.enchantment import enchantment
from model.artifact import artifact


class CardDatabase:
    CARD_DATABASE = {}
    
    
    def add_card_copies(self, card_class, card_id_base, copies, color, CMC, white, blue, black, red, green, generic, power = 0, toughness = 0):
        for i in range(1, copies + 1):
            card_id = f"{card_id_base}_{i:03d}"
            self.CARD_DATABASE[card_id] = card_class(card_id, color, CMC, white, blue, black, red, green, generic, power, toughness)

    # =========================
    # BASIC LANDS
    # =========================
    
    add_card_copies(land, "mountain", 20, "R", 0, 0, 0, 0, 0, 0, 0)
    add_card_copies(land, "forest",   20, "G", 0, 0, 0, 0, 0, 0, 0)
    add_card_copies(land, "plains",   20, "W", 0, 0, 0, 0, 0, 0, 0)
    add_card_copies(land, "island",   20, "U", 0, 0, 0, 0, 0, 0, 0)
    add_card_copies(land, "swamp",    20, "B", 0, 0, 0, 0, 0, 0, 0)
    
    
    # =========================
    # RED
    # =========================
    
    add_card_copies(instant, "lightning_bolt", 4, "R", 1, 0, 0, 0, 1, 0, 0)
    add_card_copies(instant, "shock",          4, "R", 1, 0, 0, 0, 1, 0, 0)
    
    add_card_copies(sorcery, "lava_spike",     4, "R", 1, 0, 0, 0, 1, 0, 0)
    add_card_copies(sorcery, "flame_slash",    4, "R", 1, 0, 0, 0, 1, 0, 0)
    
    add_card_copies(instant, "searing_spear",  4, "R", 2, 0, 0, 0, 1, 0, 1)
    add_card_copies(instant, "skullcrack",     4, "R", 2, 0, 0, 0, 1, 0, 1)
    
    add_card_copies(sorcery, "rift_bolt",      4, "R", 3, 0, 0, 0, 1, 0, 2)
    add_card_copies(instant, "incinerate",     4, "R", 2, 0, 0, 0, 1, 0, 1)
    
    add_card_copies(creature, "goblin_guide",         4, "R", 1, 0, 0, 0, 1, 0, 0, 2, 2)
    add_card_copies(creature, "goblin_bushwhacker",   4, "R", 1, 0, 0, 0, 1, 0, 0, 1, 1)
    add_card_copies(creature, "reckless_wurm",        4, "R", 5, 0, 0, 0, 1, 0, 3, 4, 4)
    add_card_copies(creature, "monastery_swiftspear", 4, "R", 1, 0, 0, 0, 1, 0, 0, 1, 2)
    add_card_copies(creature, "wall_of_stone",        4, "R", 3, 0, 0, 0, 2, 0, 1, 0, 8)
    
    
    # =========================
    # BLUE
    # =========================
    
    add_card_copies(instant, "counterspell", 4, "U", 2, 0, 2, 0, 0, 0, 0)
    add_card_copies(instant, "cancel",       4, "U", 3, 0, 2, 0, 0, 0, 1)
    add_card_copies(instant, "unsummon",     4, "U", 1, 0, 1, 0, 0, 0, 0)
    
    add_card_copies(sorcery, "ponder",       4, "U", 1, 0, 1, 0, 0, 0, 0)
    
    add_card_copies(instant, "negate",       4, "U", 2, 0, 1, 0, 0, 0, 1)
    add_card_copies(instant, "mana_leak",    4, "U", 2, 0, 1, 0, 0, 0, 1)
    
    add_card_copies(creature, "merfolk_looter",      4, "U", 2, 0, 1, 0, 0, 0, 1, 1, 1)
    add_card_copies(creature, "prodigal_sorcerer",   4, "U", 3, 0, 1, 0, 0, 0, 2, 1, 1)
    add_card_copies(creature, "air_elemental",       4, "U", 5, 0, 2, 0, 0, 0, 3, 4, 4)
    add_card_copies(creature, "phantasmal_bear",     4, "U", 1, 0, 1, 0, 0, 0, 0, 2, 2)
    
    
    # =========================
    # GREEN
    # =========================
    
    add_card_copies(instant, "giant_growth",       4, "G", 1, 0, 0, 0, 0, 1, 0)
    
    add_card_copies(sorcery, "rampant_growth",     4, "G", 2, 0, 0, 0, 0, 1, 1)
    
    add_card_copies(instant, "naturalize",          4, "G", 2, 0, 0, 0, 0, 1, 1)
    add_card_copies(instant, "vines_of_vastwood",  4, "G", 1, 0, 0, 0, 0, 1, 0)
    
    add_card_copies(creature, "llanowar_elves",      4, "G", 1, 0, 0, 0, 0, 1, 0, 1, 1)
    add_card_copies(creature, "elvish_mystic",       4, "G", 1, 0, 0, 0, 0, 1, 0, 1, 1)
    add_card_copies(creature, "grizzly_bears",       4, "G", 2, 0, 0, 0, 0, 1, 1, 2, 2)
    add_card_copies(creature, "leatherback_baloth",  4, "G", 3, 0, 0, 0, 0, 3, 0, 4, 5)
    add_card_copies(creature, "troll_ascetic",       4, "G", 3, 0, 0, 0, 0, 2, 1, 3, 2)
    
    
    # =========================
    # WHITE
    # =========================
    
    add_card_copies(instant, "swords_to_plowshares", 4, "W", 1, 1, 0, 0, 0, 0, 0)
    add_card_copies(instant, "path_to_exile",        4, "W", 1, 1, 0, 0, 0, 0, 0)
    add_card_copies(instant, "healing_salve",        4, "W", 1, 1, 0, 0, 0, 0, 0)
    
    add_card_copies(enchantment, "pacifism",         4, "W", 2, 1, 0, 0, 0, 0, 1)
    
    add_card_copies(creature, "white_knight",    4, "W", 2, 2, 0, 0, 0, 0, 0, 2, 2)
    add_card_copies(creature, "serra_angel",     4, "W", 5, 2, 0, 0, 0, 0, 3, 4, 4)
    add_card_copies(creature, "savannah_lions",  4, "W", 1, 1, 0, 0, 0, 0, 0, 2, 1)
    add_card_copies(creature, "mother_of_runes", 4, "W", 1, 1, 0, 0, 0, 0, 0, 1, 1)
    
    
    # =========================
    # BLACK
    # =========================
    
    add_card_copies(instant, "dark_ritual", 4, "B", 1, 0, 0, 1, 0, 0, 0)
    add_card_copies(instant, "terror",      4, "B", 2, 0, 0, 1, 0, 0, 1)
    add_card_copies(instant, "doom_blade",  4, "B", 2, 0, 0, 1, 0, 0, 1)
    
    add_card_copies(sorcery, "raise_dead", 4, "B", 1, 0, 0, 1, 0, 0, 0)
    add_card_copies(sorcery, "mind_rot",   4, "B", 3, 0, 0, 1, 0, 0, 2)
    
    add_card_copies(creature, "gray_merchant",  4, "B", 5, 0, 0, 2, 0, 0, 3, 2, 4)
    add_card_copies(creature, "gravedigger",    4, "B", 4, 0, 0, 1, 0, 0, 3, 2, 2)
    add_card_copies(creature, "royal_assassin", 4, "B", 3, 0, 0, 2, 0, 0, 1, 1, 1)
    add_card_copies(creature, "black_knight",   4, "B", 2, 0, 0, 2, 0, 0, 0, 2, 2)
    
    
    # =========================
    # COLORLESS ARTIFACTS
    # =========================
    
    add_card_copies(artifact, "sol_ring",    4, "C", 1, 0, 0, 0, 0, 0, 1)
    add_card_copies(artifact, "millstone",   4, "C", 2, 0, 0, 0, 0, 0, 2)
    add_card_copies(artifact, "rod_of_ruin", 4, "C", 4, 0, 0, 0, 0, 0, 4)
    
    add_card_copies(creature, "ornithopter", 4, "C", 0, 0, 0, 0, 0, 0, 0, 0, 2)
