#!/usr/bin/env python3
"""server.py - runs on the cloud VM (the side with the public IP).

Two listeners:
  0.0.0.0:8765    TLS, token-protected: the agent connects here.
  127.0.0.1:8766  local only: rc.py sends commands here.

Settings (environment variables):
  RC_TOKEN  shared secret (same value as on the agent)
  RC_CERT   TLS certificate (default: ./cert.pem)
  RC_KEY    TLS private key (default: ./key.pem)
"""
import asyncio
import hmac
import json
import logging
import os
import ssl
import uuid

from websockets.asyncio.server import serve

TOKEN = os.environ["RC_TOKEN"]
CERT = os.environ.get("RC_CERT", "cert.pem")
KEY = os.environ.get("RC_KEY", "key.pem")
AGENT_PORT = int(os.environ.get("RC_PORT", "8765"))
CONTROL_PORT = int(os.environ.get("RC_CONTROL_PORT", "8766"))
MAX_SIZE = 64 * 1024 * 1024
LOGGED_FIELDS = ("cmd", "path", "pattern", "glob", "args")

log = logging.getLogger("server")

agent = None   # the one connected agent
pending = {}   # request id -> queue of replies


def describe(request):
    return {field: request[field] for field in LOGGED_FIELDS if field in request}


async def agent_handler(ws):
    """The other machine connects here and stays connected."""
    global agent
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        ok = hmac.compare_digest(str(hello.get("token", "")).encode(), TOKEN.encode())
    except Exception:
        ok = False
    if not ok:
        log.warning("agent rejected (bad token) from %s", ws.remote_address[0])
        await ws.close(4001, "bad token")
        return

    if agent is not None:
        log.info("replacing the old agent connection")
        await agent.close()  # a newer connection replaces the old one
    agent = ws
    log.info("agent connected from %s", ws.remote_address[0])
    try:
        async for raw in ws:
            msg = json.loads(raw)
            queue = pending.get(msg.get("id"))
            if queue:
                queue.put_nowait(msg)
    finally:
        if agent is ws:
            agent = None
            for queue in pending.values():
                queue.put_nowait({"type": "done", "ok": False, "error": "agent disconnected"})
        log.info("agent disconnected")


async def control_handler(ws):
    """rc.py connects here, sends one request, gets the replies relayed."""
    request = json.loads(await ws.recv())
    if agent is None:
        log.warning("request refused, agent is not connected: %s", describe(request))
        await ws.send(json.dumps({"type": "done", "ok": False, "error": "agent is not connected"}))
        return

    target = agent
    rid = uuid.uuid4().hex
    queue = pending[rid] = asyncio.Queue()
    log.info("request %s sent to agent: %s", rid[:8], describe(request))
    closed = asyncio.create_task(ws.wait_closed())
    try:
        await target.send(json.dumps({**request, "id": rid}))
        while True:
            getter = asyncio.create_task(queue.get())
            await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
            if not getter.done():
                log.info("request %s cancelled by the caller", rid[:8])
                getter.cancel()
                try:
                    await target.send(json.dumps({"cmd": "kill", "target": rid}))
                except Exception:
                    pass
                return
            msg = getter.result()
            await ws.send(json.dumps(msg))
            if msg["type"] == "done":
                log.info("request %s done: ok=%s %s", rid[:8], msg.get("ok"), msg.get("error", ""))
                return
    except Exception:
        pass
    finally:
        closed.cancel()
        pending.pop(rid, None)


async def main():
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(CERT, KEY)
    async with serve(agent_handler, "0.0.0.0", AGENT_PORT, ssl=tls, max_size=MAX_SIZE), \
               serve(control_handler, "127.0.0.1", CONTROL_PORT, max_size=MAX_SIZE):
        log.info("waiting for agent on port %s, control on 127.0.0.1:%s", AGENT_PORT, CONTROL_PORT)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
