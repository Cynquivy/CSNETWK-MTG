from model.lifecycle import LifecycleState


class GameState:
    """
    The server's single authoritative Game State (RFC 0001 Section 4.2:
    "The Game Server MUST maintain the single authoritative copy of the
    Game State"; Section 3: "Game State: The complete authoritative set
    of all game information: all zones ... life totals, turn number, and
    current phase").

    Per-player zones (hand, library, battlefield/board, graveyard, life)
    are NOT duplicated here -- they already live on each Player instance
    (model/player.py, Milestone #2). GameState is the single place that
    ties a lifecycle/turn/phase/priority/stack view together with the two
    Player objects, and exposes read-only aggregate views (life_totals,
    hand_counts, library_counts, battlefield, graveyard) matching the
    GAME_STATE_UPDATE schema (RFC 10.2.2) -- without ever exposing both
    players' hands through one accessor; see hand_of().

    Turning this object into the actual personalized GAME_STATE_UPDATE
    JSON (which additionally hides the *opponent's* hand from each
    viewer, and reshapes battlefield permanents into their wire dicts) is
    the PDU serializer's job (Milestone #4), not this class's.
    """

    def __init__(self):
        self.lifecycle_state = LifecycleState.LOBBY
        self.turn = 0                       # RFC 6.5: set to 1 only once IN_GAME begins
        self.phase = None                   # a Phase member; meaningful only while IN_GAME
        self.active_player_id = None
        self.priority_holder_id = None      # None during Untap/Cleanup (RFC 10.2.2 comment)
        self.stack = []                     # list[StackItem]; index 0 = bottom, last = top (RFC 8.3)
        self.players = {}                   # player_id -> Player

    # -- player registry --------------------------------------------------

    def add_player(self, player) -> None:
        self.players[player.player_id] = player

    @property
    def active_player(self):
        return self.players.get(self.active_player_id)

    @property
    def priority_holder(self):
        return self.players.get(self.priority_holder_id)

    def opponent_of(self, player_id):
        """The other seated player, or None if only one/zero are seated."""
        others = [p for pid, p in self.players.items() if pid != player_id]
        return others[0] if others else None

    # -- Stack (RFC 8.3) ---------------------------------------------------

    def push_stack_item(self, item) -> None:
        self.stack.append(item)

    def pop_stack_item(self):
        """Removes and returns the top (most recently pushed) Stack item, or None if empty."""
        return self.stack.pop() if self.stack else None

    @property
    def stack_is_empty(self) -> bool:
        return len(self.stack) == 0

    # -- Aggregate views matching GAME_STATE_UPDATE (RFC 10.2.2) ----------

    @property
    def life_totals(self) -> dict:
        return {pid: p.life for pid, p in self.players.items()}

    @property
    def hand_counts(self) -> dict:
        return {pid: len(p.hand) for pid, p in self.players.items()}

    @property
    def library_counts(self) -> dict:
        return {pid: len(p.library) for pid, p in self.players.items()}

    @property
    def battlefield(self) -> dict:
        return {pid: p.board for pid, p in self.players.items()}

    @property
    def graveyard(self) -> dict:
        return {pid: p.graveyard for pid, p in self.players.items()}

    def hand_of(self, player_id) -> list:
        """
        One player's hand, by explicit ID only -- deliberately not exposed
        as a blanket "all hands" property, since a hand is hidden
        information (RFC 3 "Visible State"; RFC 12 "Confidentiality": the
        server MUST withhold hidden information from GAME_STATE_UPDATE).
        """
        player = self.players.get(player_id)
        return player.hand if player else []

    @property
    def land_played_this_turn(self) -> bool:
        """
        RFC 10.2.2: a single top-level bool, "true if AP has already
        played a land this turn". The underlying flag lives per-player on
        Player.land_played_this_turn (reset every Untap Step, RFC 7.2) --
        this reports it for whoever is currently the Active Player.
        """
        active = self.active_player
        return active.land_played_this_turn if active else False
