#!/usr/bin/env python3
"""agent.py - runs on the machine you want to control.

It dials OUT to the VM, so no port forwarding is needed here.

  agent.py                 connect and wait for commands
  agent.py --show-config   print each setting and where it came from, then exit

Settings are looked up in this order (first one found wins):
  1. local config   ./kucing-ag.json   (current directory)
  2. global config  ~/.kucing-ag/config.json
  3. environment variables

  config key  env var    meaning
  server      RC_SERVER  wss://<vm-public-ip>:8765
  token       RC_TOKEN   shared secret (same value as on the VM)
  root        RC_ROOT    the only folder the VM may touch (default: ~/remote-work)
  cert        RC_CERT    path to the VM's cert.pem; leave unset when connecting
                         through Cloudflare, so normal public certificates are
                         trusted
  commands    (none)     named command lines the VM may run with "exec",
                         e.g. {"test": "pytest -q"}
"""
import asyncio
import base64
import codecs
import datetime
import difflib
import fnmatch
import json
import locale
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
from pathlib import Path

from websockets.asyncio.client import connect

PROTOCOL = 2  # must match server.py
CONFIG_NAME = "kucing-ag"
SETTINGS = (  # config key, environment variable, default
    ("server", "RC_SERVER", "wss://YOUR_VM_IP:8765"),
    ("token", "RC_TOKEN", None),
    ("root", "RC_ROOT", "~/remote-work"),
    ("cert", "RC_CERT", None),
)
MAX_SIZE = 64 * 1024 * 1024  # biggest file/message: 64 MB
PING_SECONDS = 10
MAX_RESULTS = 1000
MAX_DIFF_LINES = 200
DEFAULT_TREE_DEPTH = 3
BINARY_CHECK_BYTES = 8192
RG_NO_MATCHES = 1
UTF16_BOMS = ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
WINDOWS_LINE_ENDING = "\r\n"
WINDOWS = os.name == "nt"
UTF8_SCRIPT_OUTPUT = {"PYTHONIOENCODING": "utf-8"}
GIT_READ_ONLY = ("status", "diff", "log", "show", "blame", "ls-files")
GIT_WITHOUT_LOCKS = {"GIT_OPTIONAL_LOCKS": "0"}  # so "git status" does not write to the repo
OTHERS_CAN_READ = 0o077

LOGGED_FIELDS = ("cmd", "path", "to", "pattern", "glob", "name", "args")

log = logging.getLogger("agent")

SERVER = TOKEN = ROOT = CERT = None  # filled in by configure()
COMMANDS = {}  # name -> command line the VM may run with "exec"

procs = {}  # request id -> running subprocess


def local_config_path():
    return Path.cwd() / f"{CONFIG_NAME}.json"


def global_config_path():
    return Path.home() / f".{CONFIG_NAME}" / "config.json"


def read_config(path):
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
        # Windows editors often save with a byte order mark; both codecs strip it.
        encoding = "utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        data = json.loads(raw.decode(encoding))
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read config {path}: {exc}\n"
                 "(in JSON, write Windows paths as C:/Users/me or C:\\\\Users\\\\me)")
    if not isinstance(data, dict):
        sys.exit(f"config {path} must be a JSON object")
    return data


def resolve_settings(local_path, global_path, env):
    """Return {key: (value, where it came from)}: local config, global config, environment, default."""
    configs = [(f"local config {local_path}", read_config(local_path)),
               (f"global config {global_path}", read_config(global_path))]
    resolved = {}
    for key, env_name, default in SETTINGS:
        for source, config in configs:
            if config.get(key) not in (None, ""):
                resolved[key] = (str(config[key]), source)
                break
        else:
            if env.get(env_name):
                resolved[key] = (env[env_name], f"environment variable {env_name}")
            else:
                resolved[key] = (default, "default")

    commands, sources = {}, []
    for source, config in reversed(configs):  # local names replace global ones
        found = config.get("commands") or {}
        if not isinstance(found, dict) or not all(isinstance(line, str) for line in found.values()):
            sys.exit(f'"commands" in {source} must look like {{"test": "pytest -q"}}')
        if found:
            commands.update(found)
            sources.append(source)
    resolved["commands"] = (commands, " + ".join(sources) or "default")
    return resolved


def warn_if_token_is_readable(path):
    if WINDOWS or "token" not in read_config(path):
        return
    if path.stat().st_mode & OTHERS_CAN_READ:
        log.warning("%s holds the token and other users can read it; run: chmod 600 %s", path, path)


def configure():
    global SERVER, TOKEN, ROOT, CERT, COMMANDS
    paths = local_config_path(), global_config_path()
    resolved = resolve_settings(*paths, os.environ)
    SERVER, TOKEN, root, CERT, COMMANDS = (resolved[key][0] for key in ("server", "token", "root", "cert", "commands"))
    if not TOKEN:
        sys.exit(f"token is not set: add it to {paths[0].name}, {paths[1]} or RC_TOKEN")
    ROOT = Path(root).expanduser().resolve()
    for path in paths:
        warn_if_token_is_readable(path)


def show_config():
    resolved = resolve_settings(local_config_path(), global_config_path(), os.environ)
    for key, (value, source) in resolved.items():
        if key == "token" and value:
            value = value[:4] + "..." if len(value) > 8 else "(set)"
        elif key == "commands":
            value = ", ".join(value) or None
        print(f"{key:<9} {value if value is not None else '(not set)'}  [{source}]")


def inside_root(full):
    return full == ROOT or ROOT in full.parents


def safe(path):
    """Resolve a path and refuse anything outside ROOT."""
    full = (ROOT / path).resolve()
    if not inside_root(full):
        raise ValueError("path is outside the allowed folder")
    return full


def replace_text(text, old, new, everywhere=False):
    if not old:
        raise ValueError("the text to replace is empty")
    count = text.count(old)
    if count == 0:
        raise ValueError(f"text not found: {old[:60]!r}")
    if count > 1 and not everywhere:
        raise ValueError(f"text appears {count} times; include more of it so it is unique, or replace all")
    return text.replace(old, new)


def short_diff(before, after):
    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2))[2:]
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"(diff cut after {MAX_DIFF_LINES} lines)"]
    return "\n".join(lines)


def is_binary(data):
    return b"\0" in data[:BINARY_CHECK_BYTES]


def describe_file(target):
    info = target.stat()
    modified = datetime.datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="seconds")
    fields = {"dir": target.is_dir(), "size": info.st_size, "modified": modified}
    if target.is_file() and info.st_size <= MAX_SIZE:
        data = target.read_bytes()
        if not is_binary(data):
            fields["lines"] = len(data.splitlines())
    return fields


def text_encoding(data, fallback):
    for bom, encoding in UTF16_BOMS:
        if data.startswith(bom):
            return encoding
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return fallback
    return "utf-8"


def to_file_line_endings(text, file_text):
    if WINDOWS_LINE_ENDING not in file_text:
        return text
    return text.replace(WINDOWS_LINE_ENDING, "\n").replace("\n", WINDOWS_LINE_ENDING)


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


def lines_to_show(lines, regex, context, max_per_file):
    """Yield (index, is a match) for matching lines and the context lines around them."""
    hits = [index for index, line in enumerate(lines) if regex.search(line)]
    if max_per_file:
        hits = hits[:max_per_file]
    shown = {near for hit in hits
             for near in range(max(0, hit - context), min(len(lines), hit + context + 1))}
    hits = set(hits)
    return [(index, index in hits) for index in sorted(shown)]


def match_record(path, line, text, is_match):
    record = {"path": path, "line": line, "text": text}
    if not is_match:
        record["context"] = True
    return record


def grep_with_python(pattern, folder, name_glob, context=0, max_per_file=None):
    regex = re.compile(pattern)
    matches = []
    for file in visible_files(folder):
        relative = file.relative_to(ROOT).as_posix()
        if name_glob and not name_matches(relative, name_glob):
            continue
        data = file.read_bytes()
        if is_binary(data):
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        for index, is_match in lines_to_show(lines, regex, context, max_per_file):
            matches.append(match_record(relative, index + 1, lines[index], is_match))
            if len(matches) > MAX_RESULTS:
                return matches
    return matches


def rg_text(field):
    if "text" in field:
        return field["text"]
    return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")


async def grep_with_rg(rg, pattern, folder, name_glob, context=0, max_per_file=None):
    command = [rg, "--json", "-e", pattern]
    if name_glob:
        command += ["-g", name_glob]
    if context:
        command += ["-C", str(context)]
    if max_per_file:
        command += ["-m", str(max_per_file)]
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
        if event["type"] not in ("match", "context"):
            continue
        found = event["data"]
        matches.append(match_record(
            Path(rg_text(found["path"])).relative_to(ROOT).as_posix(),
            found["line_number"],
            rg_text(found["lines"]).rstrip("\r\n"),
            event["type"] == "match",
        ))
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


async def list_files(folder):
    """Paths of the visible files under a folder, relative to ROOT. ripgrep also skips .gitignore'd files."""
    rg = shutil.which("rg")
    if not rg:
        return [file.relative_to(ROOT).as_posix() for file in await asyncio.to_thread(lambda: list(visible_files(folder)))]
    proc = await asyncio.create_subprocess_exec(
        rg, "--files", str(folder),
        cwd=ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode not in (0, RG_NO_MATCHES):
        raise RuntimeError(stderr.decode(errors="replace").strip())
    return sorted(Path(line).relative_to(ROOT).as_posix()
                  for line in stdout.decode("utf-8", errors="replace").splitlines())


def summarize_tree(files, folder, depth):
    """Files up to `depth` levels below the folder; deeper ones are counted per folder."""
    cut = len(folder.relative_to(ROOT).parts) + depth
    entries = {}
    for file in files:
        parts = file.split("/")
        if len(parts) <= cut:
            entries[file] = None
        else:
            deep_folder = "/".join(parts[:cut]) + "/"
            entries[deep_folder] = (entries.get(deep_folder) or 0) + 1
    return [{"path": path, "files": count} if count else {"path": path}
            for path, count in sorted(entries.items())]


def shell_quote(args):
    return subprocess.list2cmdline(args) if WINDOWS else shlex.join(args)


def kill_tree(proc):
    """Stop a process and everything it started."""
    if proc.returncode is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # the process leads its own group, see stream_process
    except OSError:
        pass  # already gone


async def stream_process(reply, rid, command, cwd, shell=False, env=None):
    """Run a command, send its output as it appears, and return its exit code."""
    start = asyncio.create_subprocess_shell if shell else asyncio.create_subprocess_exec
    proc = await start(
        *command,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **({} if WINDOWS else {"start_new_session": True}),
    )
    procs[rid] = proc
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while chunk := await proc.stdout.read(4096):
            await reply(type="out", data=decoder.decode(chunk))
        code = await proc.wait()
    finally:
        procs.pop(rid, None)
        kill_tree(proc)
    log.info("request %s: process exited with code %s", str(rid)[:8], code)
    return code


def working_folder(msg):
    folder = safe(msg.get("path") or ".")
    if not folder.is_dir():
        raise NotADirectoryError(f"not a folder: {msg.get('path')}")
    return folder


def describe(msg):
    return {field: msg[field] for field in LOGGED_FIELDS if field in msg}


async def handle(ws, msg):
    rid = msg.get("id")
    log.info("request %s: %s", str(rid)[:8], describe(msg))

    async def reply(**fields):
        await ws.send(json.dumps({"id": rid, **fields}))

    try:
        cmd = msg["cmd"]

        if cmd == "info":
            await reply(type="done", ok=True, protocol=PROTOCOL, root=str(ROOT),
                        os=f"{platform.system()} {platform.release()}",
                        python=platform.python_version(),
                        tools={name: bool(shutil.which(name)) for name in ("rg", "git")},
                        commands=COMMANDS)

        elif cmd == "ls":
            folder = safe(msg.get("path") or ".")
            items = [
                {"name": e.name, "dir": e.is_dir(), "size": e.stat().st_size}
                for e in sorted(folder.iterdir())
            ]
            await reply(type="done", ok=True, items=items)

        elif cmd == "tree":
            folder = working_folder(msg)
            entries = summarize_tree(await list_files(folder), folder, msg.get("depth") or DEFAULT_TREE_DEPTH)
            await reply(type="done", ok=True, entries=entries[:MAX_RESULTS],
                        truncated=len(entries) > MAX_RESULTS)

        elif cmd == "stat":
            await reply(type="done", ok=True, **describe_file(safe(msg["path"])))

        elif cmd == "read":
            data = safe(msg["path"]).read_bytes()
            fields = {}
            if "start" in msg:
                lines = data.splitlines(keepends=True)
                fields["lines"] = len(lines)  # so the caller knows if there is more
                data = b"".join(lines[msg["start"] - 1:msg.get("end")])
            await reply(type="done", ok=True, data=base64.b64encode(data).decode(), **fields)

        elif cmd == "grep":
            folder = safe(msg.get("path") or ".")
            files_only = msg.get("files_only")
            options = (msg["pattern"], folder, msg.get("glob"),
                       0 if files_only else msg.get("context") or 0,
                       1 if files_only else msg.get("max_per_file"))
            rg = shutil.which("rg")
            if rg:
                matches = await grep_with_rg(rg, *options)
            else:
                matches = await asyncio.to_thread(grep_with_python, *options)
            truncated = len(matches) > MAX_RESULTS
            if files_only:
                await reply(type="done", ok=True, truncated=truncated,
                            files=[match["path"] for match in matches[:MAX_RESULTS]])
            else:
                await reply(type="done", ok=True, truncated=truncated, matches=matches[:MAX_RESULTS])

        elif cmd == "glob":
            files = await asyncio.to_thread(find_files, msg["pattern"], safe(msg.get("path") or "."))
            await reply(type="done", ok=True, files=files[:MAX_RESULTS],
                        truncated=len(files) > MAX_RESULTS)

        elif cmd == "replace":
            target = safe(msg["path"])
            data = target.read_bytes()
            encoding = text_encoding(data, locale.getpreferredencoding(False))
            before = after = data.decode(encoding)
            edits = msg.get("edits") or [{"old": msg["old"], "new": msg["new"], "all": msg.get("all")}]
            for edit in edits:  # nothing is saved unless every edit applies
                old, new = (to_file_line_endings(part, before) for part in (edit["old"], edit["new"]))
                after = replace_text(after, old, new, edit.get("all"))
            target.write_bytes(after.encode(encoding))
            await reply(type="done", ok=True, diff=short_diff(before, after))

        elif cmd == "write":
            target = safe(msg["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(msg["data"]))
            await reply(type="done", ok=True)

        elif cmd == "mkdir":
            safe(msg["path"]).mkdir(parents=True, exist_ok=True)
            await reply(type="done", ok=True)

        elif cmd == "mv":
            source, target = safe(msg["path"]), safe(msg["to"])
            if source == ROOT:
                raise ValueError("refusing to move the allowed folder itself")
            if not source.exists():
                raise FileNotFoundError(f"no such file or folder: {msg['path']}")
            if target.exists():
                raise FileExistsError(f"{msg['to']} already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)
            await reply(type="done", ok=True)

        elif cmd == "rm":
            target = safe(msg["path"])
            if target == ROOT:
                raise ValueError("refusing to delete the allowed folder itself")
            if not target.is_dir():
                target.unlink()
            elif msg.get("recursive"):
                shutil.rmtree(target)
            elif any(target.iterdir()):
                raise OSError(f"{msg['path']} is a folder that is not empty; delete it recursively")
            else:
                target.rmdir()
            await reply(type="done", ok=True)

        elif cmd == "git":
            git = shutil.which("git")
            if not git:
                raise FileNotFoundError("git is not installed on the other machine")
            args = [str(a) for a in msg.get("args", [])]
            if not args or args[0] not in GIT_READ_ONLY:
                raise ValueError(f"only these git commands are allowed: {', '.join(GIT_READ_ONLY)}")
            if any(a.startswith("--output") for a in args):
                raise ValueError("--output is not allowed; git commands here only read")
            code = await stream_process(reply, rid, [git, "--no-pager", *args], working_folder(msg),
                                        env=GIT_WITHOUT_LOCKS)
            await reply(type="done", ok=True, code=code)

        elif cmd == "exec":
            name = msg["name"]
            if name not in COMMANDS:
                available = ", ".join(COMMANDS) or "none; add \"commands\" to the agent's config"
                raise ValueError(f"no command named {name!r} (available: {available})")
            args = [str(a) for a in msg.get("args", [])]
            line = " ".join(filter(None, [COMMANDS[name], shell_quote(args)]))
            code = await stream_process(reply, rid, [line], working_folder(msg), shell=True,
                                        env=UTF8_SCRIPT_OUTPUT)
            await reply(type="done", ok=True, code=code)

        elif cmd == "run":
            script = safe(msg["path"])
            if not script.is_file():
                raise FileNotFoundError(f"no such script: {msg['path']}")
            args = [str(a) for a in msg.get("args", [])]
            code = await stream_process(reply, rid, [sys.executable, "-u", str(script), *args],
                                        script.parent, env=UTF8_SCRIPT_OUTPUT)
            await reply(type="done", ok=True, code=code)

        elif cmd == "kill":
            proc = procs.get(msg.get("target"))
            if proc:
                kill_tree(proc)

        else:
            raise ValueError(f"unknown command: {cmd} (the agent may be older than the server)")

    except Exception as e:
        log.warning("request %s failed: %s: %s", str(rid)[:8], type(e).__name__, e)
        try:
            await reply(type="done", ok=False, error=f"{type(e).__name__}: {e}")
        except Exception:
            pass  # connection is gone; nothing to report to


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    tls = ssl.create_default_context(cafile=CERT)
    log.info("allowed folder: %s", ROOT)
    while True:
        try:
            async with connect(SERVER, ssl=tls, max_size=MAX_SIZE,
                             ping_interval=PING_SECONDS, ping_timeout=PING_SECONDS) as ws:
                await ws.send(json.dumps({"token": TOKEN, "protocol": PROTOCOL}))
                log.info("connected to %s", SERVER)
                tasks = set()
                async for raw in ws:
                    task = asyncio.create_task(handle(ws, json.loads(raw)))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except Exception as e:
            log.warning("not connected (%s: %s); retrying in 5s", type(e).__name__, e)
        for proc in list(procs.values()):
            kill_tree(proc)  # don't leave scripts running with nobody watching
        await asyncio.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:] == ["--show-config"]:
        show_config()
        sys.exit()
    if sys.argv[1:]:
        sys.exit(__doc__)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    configure()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
