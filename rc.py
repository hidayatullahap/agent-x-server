#!/usr/bin/env python3
"""rc.py - the command you type on the VM. Needs server.py running.

  rc.py status                              is the agent connected, and what does it have
  rc.py ls [folder]                         list files
  rc.py tree [folder] [-d depth]            list files in all subfolders (default depth 3)
  rc.py stat <path>                         size, modified time and line count
  rc.py cat <file> [range]                  print a file, or only lines 10-20, 10- or 10
  rc.py grep [-C n] [-m n] [-l] <pattern> [folder] [name-glob]
                                            search file contents with a regular expression
                                            -C lines around each match, -m matches per file,
                                            -l file names only
  rc.py glob <pattern> [folder]             find files by name, e.g. '**/*.py', newest first
  rc.py pull <remote> [local]               copy a file from the other machine
  rc.py push <local> [remote]               copy a file to the other machine
  rc.py edit <file>                         open in $EDITOR, save back when you quit
  rc.py replace [--all] <file> <old> <new>  replace text that appears exactly once (or all of it)
  rc.py mv <from> <to>                      move or rename
  rc.py rm [-r] <path>                      delete a file or empty folder; -r for a full folder
  rc.py mkdir <folder>                      create a folder
  rc.py git [-C folder] <command> [args]    read-only git: status, diff, log, show, blame, ls-files
  rc.py exec [-C folder] [name] [args]      run a command named in the agent's config;
                                            without a name, list them
  rc.py run <script.py> [args]              run it there, watch the output live

Put --json right after rc.py to print the agent's raw reply as JSON.
All remote paths are relative to the agent's allowed folder.
"""
import argparse
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

as_json = False  # set by --json


class RemoteError(Exception):
    pass


async def call(**request):
    output = []
    try:
        async with connect(CONTROL, max_size=MAX_SIZE, proxy=None) as ws:
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "out":
                    if as_json:
                        output.append(msg["data"])
                    else:
                        print(msg["data"], end="", flush=True)
                    continue
                if not msg["ok"]:
                    raise RemoteError(msg["error"])
                if output:
                    msg["output"] = "".join(output)
                return msg
    except OSError:
        sys.exit("error: can't reach server.py - is it running?")
    sys.exit("error: connection to server.py closed")


def printed_as_json(reply):
    if as_json:
        print(json.dumps(reply, indent=2))
    return as_json


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


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="rc.py", add_help=False)
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="cmd", required=True)

    def command(name, *arguments):
        sub = commands.add_parser(name, add_help=False)
        for flags, options in arguments:
            sub.add_argument(*flags, **options)

    def arg(*flags, **options):
        return flags, options

    rest = arg("args", nargs=argparse.REMAINDER)
    folder = arg("-C", dest="folder", default=".")

    command("status")
    command("ls", arg("path", nargs="?", default="."))
    command("tree", arg("path", nargs="?", default="."), arg("-d", "--depth", type=int))
    command("stat", arg("path"))
    command("cat", arg("path"), arg("range", nargs="?"))
    command("grep", arg("-C", dest="context", type=int), arg("-m", dest="max_per_file", type=int),
            arg("-l", dest="files_only", action="store_true"),
            arg("pattern"), arg("path", nargs="?", default="."), arg("glob", nargs="?"))
    command("glob", arg("pattern"), arg("path", nargs="?", default="."))
    command("pull", arg("remote"), arg("local", nargs="?"))
    command("push", arg("local"), arg("remote", nargs="?"))
    command("edit", arg("path"))
    command("replace", arg("--all", action="store_true"), arg("path"), arg("old"), arg("new"))
    command("mv", arg("path"), arg("to"))
    command("rm", arg("-r", dest="recursive", action="store_true"), arg("path"))
    command("mkdir", arg("path"))
    command("git", folder, rest)
    command("exec", folder, arg("name", nargs="?"), rest)
    command("run", arg("path"), rest)
    return parser.parse_args(argv)


async def main(argv):
    global as_json
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    try:
        opts = parse_args(argv)
    except SystemExit:
        sys.exit(__doc__)
    cmd, as_json = opts.cmd, opts.json

    if cmd == "status" or (cmd == "exec" and not opts.name):
        reply = await call(cmd="info")
        if printed_as_json(reply):
            return
        if cmd == "status":
            tools = ", ".join(name for name, found in reply["tools"].items() if found) or "none"
            print(f"agent     connected (protocol {reply['protocol']})")
            print(f"folder    {reply['root']}")
            print(f"system    {reply['os']}, Python {reply['python']}")
            print(f"tools     {tools}")
        if not reply["commands"]:
            print('commands  none (add "commands" to the agent\'s config)')
        for name, line in reply["commands"].items():
            print(f"commands  {name}: {line}")

    elif cmd == "ls":
        reply = await call(cmd="ls", path=opts.path)
        if printed_as_json(reply):
            return
        for item in reply["items"]:
            size = "<dir>" if item["dir"] else item["size"]
            print(f"{size:>12}  {item['name']}")

    elif cmd == "tree":
        reply = await call(cmd="tree", path=opts.path, depth=opts.depth)
        if printed_as_json(reply):
            return
        for entry in reply["entries"]:
            print(f"{entry['path']}  ({entry['files']} files)" if "files" in entry else entry["path"])
        print_truncated_notice(reply)

    elif cmd == "stat":
        reply = await call(cmd="stat", path=opts.path)
        if printed_as_json(reply):
            return
        print(f"type      {'folder' if reply['dir'] else 'file'}")
        print(f"size      {reply['size']} bytes")
        print(f"modified  {reply['modified']}")
        if "lines" in reply:
            print(f"lines     {reply['lines']}")

    elif cmd == "cat":
        line_range = {}
        if opts.range:
            try:
                start, end = parse_range(opts.range)
            except ValueError:
                sys.exit(RANGE_HELP)
            line_range = {"start": start, "end": end}
        reply = await call(cmd="read", path=opts.path, **line_range)
        if printed_as_json(reply):
            return
        sys.stdout.buffer.write(base64.b64decode(reply["data"]))

    elif cmd == "grep":
        reply = await call(cmd="grep", pattern=opts.pattern, path=opts.path, glob=opts.glob,
                           context=opts.context, max_per_file=opts.max_per_file,
                           files_only=opts.files_only)
        found = reply["files"] if opts.files_only else reply["matches"]
        if not printed_as_json(reply):
            for match in found:
                if opts.files_only:
                    print(match)
                else:
                    separator = "-" if match.get("context") else ":"
                    print(f"{match['path']}{separator}{match['line']}{separator}{match['text']}")
            print_truncated_notice(reply)
        if not found:
            sys.exit(NO_MATCHES_EXIT_CODE)

    elif cmd == "glob":
        reply = await call(cmd="glob", pattern=opts.pattern, path=opts.path)
        if not printed_as_json(reply):
            for path in reply["files"]:
                print(path)
            print_truncated_notice(reply)
        if not reply["files"]:
            sys.exit(NO_MATCHES_EXIT_CODE)

    elif cmd == "replace":
        reply = await call(cmd="replace", path=opts.path, old=opts.old, new=opts.new, all=opts.all)
        if printed_as_json(reply):
            return
        print(reply["diff"])
        print(f"saved {opts.path}")

    elif cmd in ("mv", "rm", "mkdir"):
        fields = {name: value for name, value in vars(opts).items() if name in ("path", "to", "recursive")}
        reply = await call(cmd=cmd, **fields)
        if printed_as_json(reply):
            return
        print({"mv": f"moved {opts.path} to {fields.get('to')}",
               "rm": f"deleted {opts.path}",
               "mkdir": f"created {opts.path}"}[cmd])

    elif cmd == "pull":
        local = Path(opts.local or Path(opts.remote).name)
        local.write_bytes(await read(opts.remote))
        print(f"saved {local}")

    elif cmd == "push":
        remote = opts.remote or Path(opts.local).name
        await write(remote, Path(opts.local).read_bytes())
        print(f"sent to {remote}")

    elif cmd == "edit":
        try:
            before = await read(opts.path)
        except RemoteError as e:
            if "FileNotFoundError" not in str(e):
                raise
            before = b""  # new file
        with tempfile.NamedTemporaryFile(suffix=Path(opts.path).suffix, delete=False) as tmp:
            tmp.write(before)
        try:
            subprocess.call([*shlex.split(os.environ.get("EDITOR", "nano")), tmp.name])
            after = Path(tmp.name).read_bytes()
        finally:
            os.unlink(tmp.name)
        if after == before:
            print("no changes")
        else:
            await write(opts.path, after)
            print(f"saved {opts.path}")

    else:  # git, exec, run: output is streamed, and rc.py exits with the remote exit code
        if cmd == "git":
            reply = await call(cmd="git", path=opts.folder, args=opts.args)
        elif cmd == "exec":
            reply = await call(cmd="exec", path=opts.folder, name=opts.name, args=opts.args)
        else:
            reply = await call(cmd="run", path=opts.path, args=opts.args)
        printed_as_json(reply)
        sys.exit(reply["code"])


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except RemoteError as e:
        if as_json:
            print(json.dumps({"type": "done", "ok": False, "error": str(e)}, indent=2))
            sys.exit(1)
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
