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
import json
import os
import ssl
import sys
from pathlib import Path

from websockets.asyncio.client import connect

SERVER = os.environ.get("RC_SERVER", "wss://YOUR_VM_IP:8765")
TOKEN = os.environ["RC_TOKEN"]
ROOT = Path(os.environ.get("RC_ROOT", "~/remote-work")).expanduser().resolve()
CERT = os.environ.get("RC_CERT", "cert.pem")
MAX_SIZE = 64 * 1024 * 1024  # biggest file/message: 64 MB

procs = {}  # request id -> running subprocess


def safe(path):
    """Resolve a path and refuse anything outside ROOT."""
    full = (ROOT / path).resolve()
    if full != ROOT and ROOT not in full.parents:
        raise ValueError("path is outside the allowed folder")
    return full


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
            await reply(type="done", ok=True, data=base64.b64encode(data).decode())

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
