import argparse
import socket
import time

import protocol
from protocol import (Connection, ConnectionClosed, ProtocolError, log)

# Dummy PDU for echo testing
def run_self_test(conn: Connection) -> None:
    dummy = {
        "type": "PLAYER_READY",
        "seq_num": 1,
        "player_id": "player_1",
        "deck_list": ["test_01", "test_02", "test_03"],
    }
    log("[client] self-test: sending dummy PLAYER_READY ...")
    conn.send_pdu(dummy)
    echo = conn.recv_pdu()
    if echo == dummy:
        log("[client] self-test PASSED: server echoed an identical PDU")
    else:
        log("[client] self-test FAILED: echo did not match what was sent")
        log(f"          sent: {dummy}")
        log(f"          got:  {echo}")


def interactive_loop(conn: Connection) -> None:
    log("[client] press Enter to send a PING, or type 'q' then Enter to quit")
    ping_seq = 1
    while True:
        try:
            command = input()
        except EOFError: # e.g. stdin closed
            break
        if command.strip().lower() in ("q", "quit", "exit"):
            break
        ping = {"type": "PING", "seq_num": ping_seq,
                "timestamp": int(time.time() * 1000)}
        conn.send_pdu(ping)
        conn.recv_pdu() # the echoed PING (logged if verbose)
        ping_seq += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="MTGNP Player Client (Milestone 0)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=protocol.DEFAULT_PORT,
                        help=f"server TCP port (default: {protocol.DEFAULT_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every PDU sent and received")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((args.host, args.port))
    except OSError as exc:
        log(f"[client] could not connect to {args.host}:{args.port}: {exc}")
        return

    conn = Connection(sock, local="CLIENT", peer="SERVER", verbose=args.verbose)
    log(f"[client] connected to {args.host}:{args.port}")
    try:
        run_self_test(conn)
        interactive_loop(conn)
    except ConnectionClosed:
        # Happens to the third client, the server accepts then
        # closes the socket cus two players are already present.
        log("[client] server closed the connection (it may already be full)")
    except ProtocolError as exc:
        log(f"[client] protocol error: {exc}")
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        conn.close()
        log("[client] disconnected")


if __name__ == "__main__":
    main()
