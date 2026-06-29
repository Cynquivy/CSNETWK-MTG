# CSNETWK-MTG

very umazing MTG card game !!!!!!

## Requirements

- Python 3.8 or newer. Standard library only; no third-party packages.

## Build & run

From the project directory, in two terminals:

```
# Terminal 1 — start the server (verbose)
python server.py --verbose

# Terminal 2 — start a client (verbose)
python client.py --verbose
```

Flags:

```
server.py [--host 0.0.0.0] [--port 4444] [-v | --verbose]
client.py [--host 127.0.0.1] [--port 4444] [-v | --verbose]
```

To connect across two machines on a LAN, leave the server on `--host 0.0.0.0`
and start the client with `--host <server-ip>`.

## Enabling verbose mode

Pass `-v` / `--verbose` to either program. Every PDU sent and received is
printed with a timestamp and a `source -> destination` tag, for example:

```
[10:07:05.331] [CLIENT -> SERVER] {"type":"PLAYER_READY","seq_num":1,...}
[10:07:05.332] [SERVER -> CLIENT] {"type":"PLAYER_READY","seq_num":1,...}
```
