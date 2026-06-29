import json
import socket
import struct
import threading
from datetime import datetime

# Fixed constants from Section 5.1
LENGTH_PREFIX_BYTES = 4         # 4-byte big-endian length header
MAX_PDU_BYTES = 65_535          # A PDU MUST NOT exceed 65,535 bytes
DEFAULT_PORT = 4444             # Default server port is 4444
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
    """Thread-safe stdout print used for all server/client output."""
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
    def __init__(self, sock: socket.socket, local: str, peer: str,
                 verbose: bool = False):
        self.sock = sock
        self.local = local
        self.peer = peer
        self.verbose = verbose

    def send_pdu(self, pdu: dict) -> None:
        self.sock.sendall(encode_pdu(pdu))
        self._log("SEND", pdu)

    def recv_pdu(self) -> dict:
        pdu = decode_pdu(self.sock)
        self._log("RECV", pdu)
        return pdu

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
