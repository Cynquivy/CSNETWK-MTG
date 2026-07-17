
class Player:
    def __init__(self, player_name):
        self.player_name = player_name
        self.life = 20
        self.is_active = False
        self.white_mpool = 0
        self.blue_mpool = 0
        self.black_mpool = 0
        self.red_mpool = 0
        self.green_mpool = 0
        self.hand = []
        self.board = []
        self.library = []
        self.exiled = []