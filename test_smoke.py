#!/usr/bin/env python3
"""test_smoke.py - starts server.py and agent.py on this machine and tries every command.

  python3 test_smoke.py

Needs the packages in requirements.txt and the openssl command (for a throwaway certificate).
"""
import asyncio
import codecs
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import agent

HERE = Path(__file__).resolve().parent
TOKEN = "smoke-test-token"
START_TIMEOUT_SECONDS = 20
BAD_TOKEN, BAD_PROTOCOL = 4001, 4002

work = processes = mcp = None
root = agent_port = None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rc(*args):
    """Run rc.py --json and return (exit code, parsed reply)."""
    done = subprocess.run([sys.executable, str(HERE / "rc.py"), "--json", *args],
                          capture_output=True, text=True)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, {"ok": False, "error": done.stderr or done.stdout}


def tool(coroutine):
    return asyncio.run(coroutine)


def put(path, text):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")


def setUpModule():
    global work, processes, mcp, root, agent_port
    if not shutil.which("openssl"):
        raise unittest.SkipTest("the openssl command is needed to make a test certificate")
    work = Path(tempfile.mkdtemp(prefix="smoke-"))
    root, home, start_folder = work / "root", work / "home", work / "start"
    for folder in (root, home, start_folder):
        folder.mkdir()
    cert, key = work / "cert.pem", work / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=rc",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)

    agent_port, control_port = free_port(), free_port()
    os.environ["RC_CONTROL_PORT"] = str(control_port)  # read by rc.py and mcp_server.py
    import mcp_server
    mcp = mcp_server

    # Everything the agent needs comes from its local config file; the environment has none of it.
    (start_folder / "kucing-ag.json").write_text(json.dumps({
        "server": f"wss://127.0.0.1:{agent_port}", "token": TOKEN,
        "root": root.as_posix(), "cert": cert.as_posix(),
        "commands": {"hello": "echo hello"},
    }))
    server_env = {**os.environ, "RC_TOKEN": TOKEN, "RC_CERT": str(cert), "RC_KEY": str(key),
                  "RC_PORT": str(agent_port)}
    agent_env = {name: value for name, value in os.environ.items() if not name.startswith("RC_")}
    agent_env.update(HOME=str(home), USERPROFILE=str(home))
    log = open(work / "log.txt", "w")
    processes = [
        subprocess.Popen([sys.executable, str(HERE / "server.py")], env=server_env, stdout=log, stderr=log),
        subprocess.Popen([sys.executable, str(HERE / "agent.py")], env=agent_env, cwd=start_folder,
                         stdout=log, stderr=log),
    ]
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while rc("status")[0] != 0:
        if time.monotonic() > deadline:
            tearDownModule()
            raise RuntimeError("agent did not connect:\n" + (work / "log.txt").read_text())
        time.sleep(0.2)


def tearDownModule():
    for process in processes or []:
        process.kill()
        process.wait()
    shutil.rmtree(work, ignore_errors=True)


class Commands(unittest.TestCase):
    def test_status(self):
        code, reply = rc("status")
        self.assertEqual(code, 0)
        self.assertEqual(reply["protocol"], agent.PROTOCOL)
        self.assertEqual(Path(reply["root"]), root.resolve())
        self.assertEqual(reply["commands"], {"hello": "echo hello"})
        self.assertIn("connected", tool(mcp.remote_status()))

    def test_read_reports_total_lines(self):
        put("read/five.txt", "1\n2\n3\n4\n5\n")
        self.assertEqual(rc("cat", "read/five.txt", "2-3")[1]["lines"], 5)
        text = tool(mcp.remote_read("read/five.txt", start_line=2, line_count=2))
        self.assertIn("(lines 2-3 of 5; read again with start_line=4)", text)
        self.assertIn("(lines 1-5 of 5)", tool(mcp.remote_read("read/five.txt")))

    def test_read_many(self):
        put("many/a.txt", "alpha\n")
        text = tool(mcp.remote_read_many(["many/a.txt", "many/missing.txt"]))
        self.assertIn("alpha", text)
        self.assertIn("== many/missing.txt ==\n(error:", text)

    def test_tree_folds_deep_folders(self):
        put("tree/top.txt", "")
        put("tree/a/b/one.txt", "")
        put("tree/a/b/two.txt", "")
        put("tree/.hidden/skip.txt", "")
        entries = rc("tree", "tree", "-d", "2")[1]["entries"]
        self.assertEqual(entries, [{"path": "tree/a/b/", "files": 2}, {"path": "tree/top.txt"}])

    def test_stat(self):
        put("stat/three.txt", "a\nb\nc\n")
        reply = rc("stat", "stat/three.txt")[1]
        self.assertEqual((reply["dir"], reply["size"], reply["lines"]), (False, 6, 3))

    def test_grep_options(self):
        put("grep/one.txt", "before\nneedle 1\nafter\nneedle 2\n")
        put("grep/two.txt", "nothing\n")
        matches = rc("grep", "-C", "1", "-m", "1", "needle", "grep")[1]["matches"]
        self.assertEqual([(m["line"], m.get("context", False)) for m in matches],
                         [(1, True), (2, False), (3, True)])
        self.assertEqual(rc("grep", "-l", "needle", "grep")[1]["files"], ["grep/one.txt"])
        self.assertEqual(rc("grep", "absent", "grep")[0], 1)
        self.assertIn("grep/one.txt-1-before", tool(mcp.remote_grep("needle", "grep", context_lines=1)))

    def test_grep_without_ripgrep_gives_the_same_lines(self):
        put("grep2/one.txt", "before\nneedle 1\nafter\nneedle 2\n")
        agent.ROOT = root.resolve()
        matches = agent.grep_with_python("needle", agent.ROOT / "grep2", None, context=1, max_per_file=1)
        self.assertEqual([(m["line"], m.get("context", False)) for m in matches],
                         [(1, True), (2, False), (3, True)])

    def test_replace_all_and_diff(self):
        put("edit/all.txt", "x = 1\nx = 1\n")
        self.assertIn("appears 2 times", rc("replace", "edit/all.txt", "x = 1", "x = 2")[1]["error"])
        reply = rc("replace", "--all", "edit/all.txt", "x = 1", "x = 2")[1]
        self.assertIn("-x = 1\n-x = 1\n+x = 2\n+x = 2", reply["diff"])
        self.assertEqual((root / "edit/all.txt").read_text(), "x = 2\nx = 2\n")

    def test_several_edits_are_all_or_nothing(self):
        put("edit/multi.txt", "one\ntwo\n")
        good, bad = mcp.Edit(old_text="one", new_text="1"), mcp.Edit(old_text="absent", new_text="?")
        with self.assertRaises(mcp.ToolError):
            tool(mcp.remote_edit("edit/multi.txt", [good, bad]))
        self.assertEqual((root / "edit/multi.txt").read_text(), "one\ntwo\n")
        result = tool(mcp.remote_edit("edit/multi.txt", [good, mcp.Edit(old_text="two", new_text="2")]))
        self.assertIn("+1\n+2", result)

    def test_move_delete_mkdir(self):
        put("files/a.txt", "a")
        self.assertEqual(rc("mkdir", "files/new/deep")[0], 0)
        self.assertEqual(rc("mv", "files/a.txt", "files/new/b.txt")[0], 0)
        self.assertTrue((root / "files/new/b.txt").is_file())
        self.assertIn("not empty", rc("rm", "files/new")[1]["error"])
        self.assertEqual(rc("rm", "-r", "files/new")[0], 0)
        self.assertFalse((root / "files/new").exists())
        self.assertIn("allowed folder itself", rc("rm", "-r", ".")[1]["error"])

    def test_paths_outside_the_folder_are_refused(self):
        for args in (["cat", "../start/kucing-ag.json"], ["rm", "../start"], ["mv", "x", "../y"],
                     ["tree", ".."], ["git", "-C", "..", "status"]):
            self.assertIn("outside the allowed folder", rc(*args)[1]["error"], args)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_is_read_only(self):
        put("repo/file.txt", "hello\n")
        subprocess.run(["git", "init", "-q"], cwd=root / "repo", check=True)
        code, reply = rc("git", "-C", "repo", "status", "--short")
        self.assertEqual((code, reply["output"].strip()), (0, "?? file.txt"))
        self.assertIn("only these git commands", rc("git", "-C", "repo", "commit", "-m", "x")[1]["error"])
        self.assertIn("--output is not allowed", rc("git", "-C", "repo", "diff", "--output=x")[1]["error"])
        self.assertIn("?? file.txt\n(exit code 0)", tool(mcp.remote_git(["status", "--short"], "repo")))

    def test_exec_runs_only_named_commands(self):
        code, reply = rc("exec", "hello", "big world")
        self.assertEqual((code, reply["output"].split()), (0, ["hello", "big", "world"]))
        self.assertIn("no command named 'other' (available: hello)", rc("exec", "other")[1]["error"])
        self.assertIn("hello\n(exit code 0)", tool(mcp.remote_exec("hello")))

    def test_run_script_and_exit_code(self):
        put("run/ok.py", "import sys\nprint('ran', sys.argv[1])\nsys.exit(3)\n")
        code, reply = rc("run", "run/ok.py", "arg")
        self.assertEqual((code, reply["output"].strip()), (3, "ran arg"))
        self.assertIn("ran arg\n(exit code 3)", tool(mcp.remote_run("run/ok.py", ["arg"])))

    @unittest.skipIf(os.name == "nt", "checks the child process with a POSIX signal")
    def test_timeout_stops_the_script_and_its_children(self):
        put("run/slow.py", "import subprocess, sys, time\n"
                           "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                           "print(child.pid, flush=True)\ntime.sleep(60)\n")
        result = tool(mcp.remote_run("run/slow.py", timeout_seconds=2))
        self.assertIn("(stopped: still running after 2 seconds)", result)
        child = int(result.split()[0])
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("the script's child process is still running")


class Connecting(unittest.TestCase):
    def close_code_for(self, hello):
        async def attempt():
            tls = ssl.create_default_context(cafile=str(work / "cert.pem"))
            async with connect(f"wss://127.0.0.1:{agent_port}", ssl=tls) as ws:
                await ws.send(json.dumps(hello))
                try:
                    await ws.recv()
                except ConnectionClosed as closed:
                    return closed.rcvd.code
        return asyncio.run(attempt())

    def test_bad_token_is_rejected(self):
        self.assertEqual(self.close_code_for({"token": "wrong", "protocol": agent.PROTOCOL}), BAD_TOKEN)

    def test_old_agent_is_rejected(self):
        self.assertEqual(self.close_code_for({"token": TOKEN}), BAD_PROTOCOL)


class Settings(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="settings-"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.local, self.shared = self.folder / "kucing-ag.json", self.folder / "config.json"

    def resolve(self, env=None):
        return agent.resolve_settings(self.local, self.shared, env or {})

    def test_local_then_global_then_environment_then_default(self):
        self.local.write_text('{"server": "wss://local"}')
        self.shared.write_text('{"server": "wss://global", "token": "global"}')
        resolved = self.resolve({"RC_SERVER": "wss://env", "RC_TOKEN": "env", "RC_CERT": "env.pem"})
        self.assertEqual(resolved["server"], ("wss://local", f"local config {self.local}"))
        self.assertEqual(resolved["token"], ("global", f"global config {self.shared}"))
        self.assertEqual(resolved["cert"], ("env.pem", "environment variable RC_CERT"))
        self.assertEqual(resolved["root"], ("~/remote-work", "default"))

    def test_commands_from_both_files_are_merged(self):
        self.local.write_text('{"commands": {"test": "local"}}')
        self.shared.write_text('{"commands": {"test": "global", "lint": "ruff"}}')
        self.assertEqual(self.resolve()["commands"][0], {"test": "local", "lint": "ruff"})

    def test_files_saved_by_windows_editors_are_read(self):
        text = '{"token": "t"}'
        for raw in (codecs.BOM_UTF8 + text.encode(), text.encode("utf-16")):
            self.local.write_bytes(raw)
            self.assertEqual(self.resolve()["token"][0], "t")

    def test_single_backslashes_get_a_helpful_error(self):
        self.local.write_text(r'{"root": "C:\Users\me"}')
        with self.assertRaises(SystemExit) as failure:
            self.resolve()
        self.assertIn("C:/Users/me", str(failure.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
