import json
import socket
import struct
import threading
from datetime import datetime

# Aliased to avoid clashing with the ubiquitous local variable name `pdu`
# used throughout this module (send_pdu(self, pdu), etc.).
from network import pdu as pdu_builders

# Fixed constants from Section 5.1
LENGTH_PREFIX_BYTES = 4         # 4-byte big-endian length header
MAX_PDU_BYTES = 65_535          # A PDU MUST NOT exceed 65,535 bytes
DEFAULT_PORT = 4444             # Default GameServer port is 4444
ENCODING = "utf-8"              # All JSON MUST be valid UTF-8


# EXCEPTIONS
class ProtocolError(Exception):
    """Base class for all transport-layer errors."""


class ConnectionClosed(ProtocolError):
    """Peer closed the TCP connection (a recv returned zero bytes)."""


class PDUTooLarge(ProtocolError):
    """A payload exceeds MAX_PDU_BYTES."""


class MalformedPDU(ProtocolError):
    """Bytes could not be decoded as a valid base PDU."""


# Thread logging safety net for multiple printfs

_print_lock = threading.Lock()

def log(message: str) -> None:
    """Thread-safe stdout print used for all GameServer/client output."""
    with _print_lock:
        print(message, flush=True)

# Encode

def encode_pdu(pdu: dict) -> bytes:
    """Serialise a PDU dict to length-prefixed bytes ready for the wire."""
    payload = json.dumps(pdu, separators=(",", ":")).encode(ENCODING)
    if len(payload) > MAX_PDU_BYTES:
        raise PDUTooLarge(f"payload is {len(payload)} bytes; max is {MAX_PDU_BYTES}")
    # ">I" = big-endian (network byte order) unsigned 32-bit integer.
    header = struct.pack(">I", len(payload))
    return header + payload


# Receive

def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    chunks = []
    bytes_remaining = n
    while bytes_remaining > 0:
        chunk = sock.recv(bytes_remaining)
        if not chunk:
            raise ConnectionClosed("peer closed the connection")
        chunks.append(chunk)
        bytes_remaining -= len(chunk)
    return b"".join(chunks)


def decode_pdu(sock: socket.socket) -> dict:
    header = _recv_exactly(sock, LENGTH_PREFIX_BYTES)
    (length,) = struct.unpack(">I", header)
    if length > MAX_PDU_BYTES:
        raise PDUTooLarge(f"declared length {length} exceeds max {MAX_PDU_BYTES}")
    payload = _recv_exactly(sock, length)
    try:
        pdu = json.loads(payload.decode(ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedPDU(f"could not parse PDU: {exc}") from exc
    validate_base_pdu(pdu)
    return pdu

# Format checker for Section 5.4
def validate_base_pdu(pdu: object) -> None:
    if not isinstance(pdu, dict):
        raise MalformedPDU("PDU is not a JSON object")
    if not isinstance(pdu.get("type"), str) or not pdu["type"]:
        raise MalformedPDU("PDU missing required non-empty string field 'type'")
    
    seq = pdu.get("seq_num")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise MalformedPDU("PDU missing required integer field 'seq_num'")


# Connection wrapper

class Connection:
    # RFC 0001 Section 5.4: which tracked seq_num a given client->server PDU
    # type MUST echo. Types not listed here are handled specially in
    # expected_seq_for() (CONCEDE) or are exempt from the echo rule
    # entirely (PING -- RFC 5.4; PLAYER_READY -- RFC 6.2).
    _ECHO_SOURCE = {
        # Default rule (RFC 5.4 para 2, first sentence): echoes the most
        # recently received/sent PRIORITY_GRANT.
        "CAST_SPELL": "last_priority_grant_seq",
        "ACTIVATE_ABILITY": "last_priority_grant_seq",
        "PRIORITY_PASS": "last_priority_grant_seq",
        "PLAY_LAND": "last_priority_grant_seq",
        # Explicit overrides (RFC 5.4 para 2, subsequent sentences):
        "MULLIGAN_CHOICE": "last_game_state_update_seq",
        "DISCARD": "last_game_state_update_seq",
        "DECLARE_ATTACKERS": "last_phase_transition_seq",
        "DECLARE_BLOCKERS": "last_phase_transition_seq",
        "ASSIGN_DAMAGE_ORDER": "last_phase_transition_seq",
        # RFC 8.6.2 ("TRIGGER_ORDER does not consume the player's priority
        # ... resolved ... before any PRIORITY_GRANT is issued") and its
        # worked example / 10.2.11 schema comment both show
        # TRIGGER_ORDER_RESPONSE echoing TRIGGER_ORDER's OWN seq_num, not a
        # PRIORITY_GRANT. Same reasoning for TRIGGER_CHOICE (RFC 8.6.3) and
        # its RESPONSE (10.2.13 schema comment).
        "TRIGGER_ORDER_RESPONSE": "last_trigger_order_seq",
        "TRIGGER_CHOICE_RESPONSE": "last_trigger_choice_seq",
    }

    # PDU types that trigger tracking on send/receive, and the attribute
    # each one updates. Server-originated types only; client-originated
    # types are never tracked this way because nothing echoes them.
    _TRACKED_TYPES = {
        "PRIORITY_GRANT": "last_priority_grant_seq",
        "GAME_STATE_UPDATE": "last_game_state_update_seq",
        "PHASE_TRANSITION": "last_phase_transition_seq",
        "TRIGGER_ORDER": "last_trigger_order_seq",
        "TRIGGER_CHOICE": "last_trigger_choice_seq",
    }

    def __init__(self, sock: socket.socket, local: str, peer: str,
                 verbose: bool = False):
        self.sock = sock
        self.local = local
        self.peer = peer
        self.verbose = verbose

        # the global counter for the GameServer's seq_num
        self.out_seq = 0

        # RFC 5.4 echo-rule bookkeeping. This same set of fields serves
        # both roles depending on which side of the connection this object
        # represents: on the server's Connection (one per client), these
        # are updated by send_pdu() and used to validate seq_num on
        # incoming client action PDUs; on the client's Connection, they are
        # updated by recv_pdu() and used to know what seq_num to echo back.
        self.last_sent_seq = None       # most recent seq_num THIS side sent
        self.last_received_seq = None   # most recent seq_num THIS side received
        self.last_priority_grant_seq = None
        self.last_game_state_update_seq = None
        self.last_phase_transition_seq = None
        self.last_trigger_order_seq = None
        self.last_trigger_choice_seq = None

    def next_seq(self) -> int:
        """Allocate (but do not send) the next outgoing seq_num."""
        self.out_seq += 1
        return self.out_seq

    def send_pdu(self, pdu: dict) -> None:
        if "seq_num" not in pdu:
            self.out_seq += 1
            pdu["seq_num"] = self.out_seq
        else:
            # A caller may have pre-allocated seq_num (e.g. via next_seq(),
            # or a network.pdu builder called with an explicit value).
            # Keep the counter monotonic either way so a later PDU sent
            # without an explicit seq_num never collides with or precedes
            # one already on the wire.
            self.out_seq = max(self.out_seq, pdu["seq_num"])

        self.sock.sendall(encode_pdu(pdu))
        self._track_seq(pdu, outgoing=True)
        self._log("SEND", pdu)

    def recv_pdu(self) -> dict:
        pdu = decode_pdu(self.sock)
        self._track_seq(pdu, outgoing=False)
        self._log("RECV", pdu)
        return pdu

    def _track_seq(self, pdu: dict, outgoing: bool) -> None:
        seq = pdu.get("seq_num")
        if seq is None:
            return
        if outgoing:
            self.last_sent_seq = seq
        else:
            self.last_received_seq = seq
        attr = self._TRACKED_TYPES.get(pdu.get("type"))
        if attr:
            setattr(self, attr, seq)

    def expected_seq_for(self, pdu_type: str):
        """
        The seq_num an incoming PDU of this type MUST echo, per RFC 5.4, or
        None if the type is exempt from the echo rule (PING, PLAYER_READY)
        or isn't one of the tracked/echoing types at all.
        """
        if pdu_type == "CONCEDE":
            # RFC 5.4 para 3: "CONCEDE MAY be sent at any time regardless
            # of which player holds priority; its seq_num MUST be the
            # value from the most recently received server PDU of any
            # type (not necessarily a PRIORITY_GRANT)."
            return self.last_sent_seq
        attr = self._ECHO_SOURCE.get(pdu_type)
        return getattr(self, attr) if attr else None

    def is_stale(self, pdu: dict) -> bool:
        """
        True if this PDU's seq_num fails RFC 5.4's echo rule for its type.
        Always False for exempt types (PING, PLAYER_READY) and any type
        the echo rule doesn't apply to.
        """
        expected = self.expected_seq_for(pdu.get("type"))
        if expected is None:
            return False
        return pdu.get("seq_num") != expected

    def build_stale_action_response(self, rejected_pdu: dict, player_id: str,
                                     time_limit_ms: int = 60000):
        """
        RFC 5.4 / 11, and the worked example in Section 5.4: on a seq_num
        mismatch the server sends an ERROR (code STALE_ACTION) and, if the
        player still holds priority, re-issues the SAME PRIORITY_GRANT
        seq_num so they can retry. Returns (error_pdu, priority_grant_pdu)
        ready to send; does not send them itself, and does not decide
        whether re-issuing a grant is actually appropriate for the
        rejected PDU's type -- that is the caller's (handler's) call.
        """
        expected = self.expected_seq_for(rejected_pdu.get("type"))
        error = pdu_builders.build_error(
            seq_num=self.next_seq(),
            code="STALE_ACTION",
            message=(f"Priority token mismatch. Expected seq_num {expected}, "
                     f"got {rejected_pdu.get('seq_num')}."),
            rejected_action=rejected_pdu,
        )
        grant = pdu_builders.build_priority_grant(
            seq_num=expected,
            player_id=player_id,
            time_limit_ms=time_limit_ms,
        )
        return error, grant

    def _log(self, direction: str, pdu: dict) -> None:
        if not self.verbose:
            return
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if direction == "SEND":
            src, dst = self.local, self.peer
        else:
            src, dst = self.peer, self.local
        log(f"[{stamp}] [{src} -> {dst}] {json.dumps(pdu)}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
