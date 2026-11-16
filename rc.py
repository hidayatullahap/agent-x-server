#!/usr/bin/env python3
"""rc.py - the command you type on the VM. Needs server.py running.

  rc.py ls [folder]              list files
  rc.py cat <file>               print a file
  rc.py pull <remote> [local]    copy a file from the other machine
  rc.py push <local> [remote]    copy a file to the other machine
  rc.py edit <file>              open in $EDITOR, save back when you quit
  rc.py run <script.py> [args]   run it there, watch the output live

All remote paths are relative to the agent's allowed folder.
"""
import asyncio
import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from websockets.asyncio.client import connect

CONTROL = f"ws://127.0.0.1:{os.environ.get('RC_CONTROL_PORT', '8766')}"
MAX_SIZE = 64 * 1024 * 1024


class RemoteError(Exception):
    pass


async def call(**request):
    try:
        async with connect(CONTROL, max_size=MAX_SIZE) as ws:
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "out":
                    print(msg["data"], end="", flush=True)
                    continue
                if not msg["ok"]:
                    raise RemoteError(msg["error"])
                return msg
    except OSError:
        sys.exit("error: can't reach server.py - is it running?")
    sys.exit("error: connection to server.py closed")


async def read(path):
    return base64.b64decode((await call(cmd="read", path=path))["data"])


async def write(path, data):
    await call(cmd="write", path=path, data=base64.b64encode(data).decode())


async def main(argv):
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]

    if cmd == "ls":
        reply = await call(cmd="ls", path=args[0] if args else ".")
        for item in reply["items"]:
            size = "<dir>" if item["dir"] else item["size"]
            print(f"{size:>12}  {item['name']}")

    elif cmd == "cat" and args:
        sys.stdout.buffer.write(await read(args[0]))

    elif cmd == "pull" and args:
        local = Path(args[1] if len(args) > 1 else Path(args[0]).name)
        local.write_bytes(await read(args[0]))
        print(f"saved {local}")

    elif cmd == "push" and args:
        remote = args[1] if len(args) > 1 else Path(args[0]).name
        await write(remote, Path(args[0]).read_bytes())
        print(f"sent to {remote}")

    elif cmd == "edit" and args:
        try:
            before = await read(args[0])
        except RemoteError as e:
            if "FileNotFoundError" not in str(e):
                raise
            before = b""  # new file
        with tempfile.NamedTemporaryFile(suffix=Path(args[0]).suffix, delete=False) as tmp:
            tmp.write(before)
        try:
            subprocess.call([*shlex.split(os.environ.get("EDITOR", "nano")), tmp.name])
            after = Path(tmp.name).read_bytes()
        finally:
            os.unlink(tmp.name)
        if after == before:
            print("no changes")
        else:
            await write(args[0], after)
            print(f"saved {args[0]}")

    elif cmd == "run" and args:
        reply = await call(cmd="run", path=args[0], args=args[1:])
        sys.exit(reply["code"])

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except RemoteError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
