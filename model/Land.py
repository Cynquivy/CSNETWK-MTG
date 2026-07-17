from model import Card

class Land(Card):
    
    def effect(self, colored_mpool):
        colored_mpool += 1