# agent-x-server

A cloud VM works in one folder on another machine. The other machine dials out to the VM over a WebSocket, so it needs no open port.

## Files

| File | Runs on | Job |
| --- | --- | --- |
| `agent.py` | other machine | Connects to the VM and carries out the commands. All file and process logic is here. |
| `server.py` | VM | Relays requests from the local control port to the agent. Knows nothing about individual commands. |
| `rc.py` | VM | Command line client. |
| `mcp_server.py` | VM | The same commands as MCP tools for an AI agent. |
| `test_smoke.py` | anywhere | Starts a real server and agent on localhost and tries every command. |
| `service/` | other machine | Templates that start the agent at login (systemd, launchd, Task Scheduler). |

## Run and test

```
make setup            # or: pip install -r requirements.txt
make test             # or: python3 test_smoke.py
python3 agent.py --show-config   # each setting and where it came from
```

The test needs the `openssl` command. There is no linter configured.

## Protocol

JSON over WebSocket. A client sends one request to the control port (`ws://127.0.0.1:8766`): `{"cmd": ..., ...fields}`. `server.py` adds an `id` and forwards it to the agent. The agent answers with zero or more `{"type": "out", "data": ...}` messages (process output) and one `{"type": "done", "ok": true|false, ...}`. The README's Protocol section lists every command and its fields.

## Adding a command

1. Add an `elif cmd == "..."` branch in `handle()` in `agent.py`. Pass every path through `safe()`.
2. Add it to `rc.py`: the usage text at the top, `parse_args()`, and a branch in `main()`.
3. Add a tool in `mcp_server.py`.
4. Add a test in `test_smoke.py`, and a row in the README tables (Usage, MCP tools, Protocol).

`server.py` needs no change unless the handshake changes. If a change makes old agents and new servers incompatible, raise `PROTOCOL` in both `agent.py` and `server.py`.

## Conventions

- The agent must run on Windows, Linux and macOS: use `pathlib`, send paths with `/`, and branch on the `WINDOWS` constant only where the systems differ.
- Only the standard library plus `websockets` on the agent. `mcp` is used on the VM only.
- Settings are read in `configure()`, not at import time, so tests can import `agent`.
- The agent's config is `kucing-ag.json` (local) and `~/.kucing-ag/config.json` (global); `CONFIG_NAME` in `agent.py` holds the name.
