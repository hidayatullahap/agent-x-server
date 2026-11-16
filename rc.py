#!/usr/bin/env python3
"""rc.py - the command you type on the VM. Needs server.py running.

  rc.py ls [folder]                         list files
  rc.py cat <file> [range]                  print a file, or only lines 10-20, 10- or 10
  rc.py grep <pattern> [folder] [name-glob] search file contents with a regular expression
  rc.py glob <pattern> [folder]             find files by name, e.g. '**/*.py', newest first
  rc.py pull <remote> [local]               copy a file from the other machine
  rc.py push <local> [remote]               copy a file to the other machine
  rc.py edit <file>                         open in $EDITOR, save back when you quit
  rc.py replace <file> <old> <new>          replace text that appears exactly once
  rc.py run <script.py> [args]              run it there, watch the output live

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
RANGE_HELP = "error: range must look like 10-20, 10- or 10"
NO_MATCHES_EXIT_CODE = 1


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


def parse_range(text):
    first, dash, last = text.partition("-")
    start = int(first)
    end = int(last) if last else (None if dash else start)
    if start < 1 or (end is not None and end < start):
        raise ValueError(text)
    return start, end


def print_truncated_notice(reply):
    if reply["truncated"]:
        print("(more results were cut off; narrow the search)", file=sys.stderr)


async def read(path, **line_range):
    return base64.b64decode((await call(cmd="read", path=path, **line_range))["data"])


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
        line_range = {}
        if len(args) > 1:
            try:
                start, end = parse_range(args[1])
            except ValueError:
                sys.exit(RANGE_HELP)
            line_range = {"start": start, "end": end}
        sys.stdout.buffer.write(await read(args[0], **line_range))

    elif cmd == "grep" and args:
        reply = await call(cmd="grep", pattern=args[0],
                           path=args[1] if len(args) > 1 else ".",
                           glob=args[2] if len(args) > 2 else None)
        for match in reply["matches"]:
            print(f"{match['path']}:{match['line']}:{match['text']}")
        print_truncated_notice(reply)
        if not reply["matches"]:
            sys.exit(NO_MATCHES_EXIT_CODE)

    elif cmd == "glob" and args:
        reply = await call(cmd="glob", pattern=args[0], path=args[1] if len(args) > 1 else ".")
        for path in reply["files"]:
            print(path)
        print_truncated_notice(reply)
        if not reply["files"]:
            sys.exit(NO_MATCHES_EXIT_CODE)

    elif cmd == "replace" and len(args) == 3:
        await call(cmd="replace", path=args[0], old=args[1], new=args[2])
        print(f"saved {args[0]}")

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
