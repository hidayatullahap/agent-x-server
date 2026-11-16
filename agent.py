#!/usr/bin/env python3
"""agent.py - runs on the machine you want to control.

It dials OUT to the VM, so no port forwarding is needed here.

Settings (environment variables):
  RC_SERVER  wss://<vm-public-ip>:8765
  RC_TOKEN   shared secret (same value as on the VM)
  RC_ROOT    the only folder the VM may touch (default: ~/remote-work)
  RC_CERT    path to the VM's cert.pem (default: ./cert.pem)
"""
import asyncio
import base64
import codecs
import fnmatch
import json
import os
import re
import shutil
import ssl
import sys
from pathlib import Path

from websockets.asyncio.client import connect

SERVER = os.environ.get("RC_SERVER", "wss://YOUR_VM_IP:8765")
TOKEN = os.environ["RC_TOKEN"]
ROOT = Path(os.environ.get("RC_ROOT", "~/remote-work")).expanduser().resolve()
CERT = os.environ.get("RC_CERT", "cert.pem")
MAX_SIZE = 64 * 1024 * 1024  # biggest file/message: 64 MB
MAX_RESULTS = 1000
BINARY_CHECK_BYTES = 8192
RG_NO_MATCHES = 1

procs = {}  # request id -> running subprocess


def inside_root(full):
    return full == ROOT or ROOT in full.parents


def safe(path):
    """Resolve a path and refuse anything outside ROOT."""
    full = (ROOT / path).resolve()
    if not inside_root(full):
        raise ValueError("path is outside the allowed folder")
    return full


def slice_lines(data, start, end):
    return b"".join(data.splitlines(keepends=True)[start - 1:end])


def replace_once(text, old, new):
    if not old:
        raise ValueError("the text to replace is empty")
    count = text.count(old)
    if count == 0:
        raise ValueError("text not found")
    if count > 1:
        raise ValueError(f"text appears {count} times; include more of it so it is unique")
    return text.replace(old, new)


def name_matches(relative, name_glob):
    if "/" in name_glob:
        return fnmatch.fnmatch(relative, name_glob)
    return fnmatch.fnmatch(relative.rsplit("/", 1)[-1], name_glob)


def visible_files(folder):
    if folder.is_file():
        yield folder
        return
    for parent, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            path = Path(parent, filename)
            if not filename.startswith(".") and not path.is_symlink():
                yield path


def grep_with_python(pattern, folder, name_glob):
    regex = re.compile(pattern)
    matches = []
    for file in visible_files(folder):
        relative = file.relative_to(ROOT).as_posix()
        if name_glob and not name_matches(relative, name_glob):
            continue
        data = file.read_bytes()
        if b"\0" in data[:BINARY_CHECK_BYTES]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append({"path": relative, "line": number, "text": line})
                if len(matches) > MAX_RESULTS:
                    return matches
    return matches


def rg_text(field):
    if "text" in field:
        return field["text"]
    return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")


async def grep_with_rg(rg, pattern, folder, name_glob):
    command = [rg, "--json", "-e", pattern]
    if name_glob:
        command += ["-g", name_glob]
    proc = await asyncio.create_subprocess_exec(
        *command, str(folder),
        cwd=ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    matches = []
    for line in stdout.splitlines():
        event = json.loads(line)
        if event["type"] != "match":
            continue
        found = event["data"]
        matches.append({
            "path": Path(rg_text(found["path"])).relative_to(ROOT).as_posix(),
            "line": found["line_number"],
            "text": rg_text(found["lines"]).rstrip("\r\n"),
        })
        if len(matches) > MAX_RESULTS:
            break
    if not matches and proc.returncode != RG_NO_MATCHES and proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip())
    return matches


def find_files(pattern, folder):
    found = [
        path for path in folder.glob(pattern)
        if path.is_file() and inside_root(path.resolve())
    ]
    found.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [path.relative_to(ROOT).as_posix() for path in found]


async def handle(ws, msg):
    rid = msg.get("id")

    async def reply(**fields):
        await ws.send(json.dumps({"id": rid, **fields}))

    try:
        cmd = msg["cmd"]

        if cmd == "ls":
            folder = safe(msg.get("path") or ".")
            items = [
                {"name": e.name, "dir": e.is_dir(), "size": e.stat().st_size}
                for e in sorted(folder.iterdir())
            ]
            await reply(type="done", ok=True, items=items)

        elif cmd == "read":
            data = safe(msg["path"]).read_bytes()
            if "start" in msg:
                data = slice_lines(data, msg["start"], msg.get("end"))
            await reply(type="done", ok=True, data=base64.b64encode(data).decode())

        elif cmd == "grep":
            folder = safe(msg.get("path") or ".")
            rg = shutil.which("rg")
            if rg:
                matches = await grep_with_rg(rg, msg["pattern"], folder, msg.get("glob"))
            else:
                matches = await asyncio.to_thread(grep_with_python, msg["pattern"], folder, msg.get("glob"))
            await reply(type="done", ok=True, matches=matches[:MAX_RESULTS],
                        truncated=len(matches) > MAX_RESULTS)

        elif cmd == "glob":
            files = await asyncio.to_thread(find_files, msg["pattern"], safe(msg.get("path") or "."))
            await reply(type="done", ok=True, files=files[:MAX_RESULTS],
                        truncated=len(files) > MAX_RESULTS)

        elif cmd == "replace":
            target = safe(msg["path"])
            text = replace_once(target.read_bytes().decode("utf-8"), msg["old"], msg["new"])
            target.write_bytes(text.encode("utf-8"))
            await reply(type="done", ok=True)

        elif cmd == "write":
            target = safe(msg["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(msg["data"]))
            await reply(type="done", ok=True)

        elif cmd == "run":
            script = safe(msg["path"])
            if not script.is_file():
                raise FileNotFoundError(f"no such script: {msg['path']}")
            args = [str(a) for a in msg.get("args", [])]
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(script), *args,
                cwd=script.parent,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            procs[rid] = proc
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                while chunk := await proc.stdout.read(4096):
                    await reply(type="out", data=decoder.decode(chunk))
                code = await proc.wait()
            finally:
                procs.pop(rid, None)
                if proc.returncode is None:
                    proc.kill()
            await reply(type="done", ok=True, code=code)

        elif cmd == "kill":
            proc = procs.get(msg.get("target"))
            if proc:
                proc.kill()

        else:
            raise ValueError(f"unknown command: {cmd}")

    except Exception as e:
        try:
            await reply(type="done", ok=False, error=f"{type(e).__name__}: {e}")
        except Exception:
            pass  # connection is gone; nothing to report to


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    tls = ssl.create_default_context(cafile=CERT)  # trust ONLY the VM's cert
    print(f"allowed folder: {ROOT}")
    while True:
        try:
            async with connect(SERVER, ssl=tls, max_size=MAX_SIZE) as ws:
                await ws.send(json.dumps({"token": TOKEN}))
                print(f"connected to {SERVER}")
                tasks = set()
                async for raw in ws:
                    task = asyncio.create_task(handle(ws, json.loads(raw)))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except Exception as e:
            print(f"not connected ({type(e).__name__}: {e}); retrying in 5s")
        for proc in list(procs.values()):
            proc.kill()  # don't leave scripts running with nobody watching
        await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
