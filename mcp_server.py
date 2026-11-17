#!/usr/bin/env python3
import asyncio
import base64
import json
import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect

CONTROL = f"ws://127.0.0.1:{os.environ.get('RC_CONTROL_PORT', '8766')}"
MAX_SIZE = 64 * 1024 * 1024
LINES_PER_READ = 2000

MAX_OUTPUT_CHARS = 30000
LINES_PER_FILE_IN_BATCH = 400
DEFAULT_TIMEOUT_SECONDS = 120

RemotePath = Annotated[str, Field(description="Path relative to the allowed folder on the other machine")]
RemoteFolder = Annotated[str, Field(description="Folder or file to search, relative to the allowed folder")]
WorkFolder = Annotated[str, Field(description="Folder to run in, relative to the allowed folder")]
Arguments = Annotated[list[str], Field(description="Extra arguments, one list item each")]
Timeout = Annotated[int, Field(ge=1, le=3600, description="Stop the command after this many seconds")]

server = MCPServer(
    "agent-x",
    instructions=(
        "These tools read, change and run files on the user's other machine, not on this one. "
        "Paths are relative to the allowed folder there. Start with remote_status to see whether "
        "the machine is connected and what it can run, and remote_tree to see the project. Find "
        "files with remote_glob or remote_grep, read only the lines you need with remote_read, "
        "and change files with remote_replace or remote_edit. Use remote_write only for new files "
        "or full rewrites. Check your work with remote_git, remote_exec and remote_run."
    ),
)


class Edit(BaseModel):
    old_text: str = Field(description="Exact text to find, including spaces and line breaks")
    new_text: str = Field(description="Text to put in its place")
    replace_all: bool = Field(default=False, description="Replace every occurrence instead of requiring exactly one")


async def request(timeout=None, **fields):
    """Send one request to the agent. Process output is collected into reply["output"]."""
    output = []

    async def exchange():
        async with connect(CONTROL, max_size=MAX_SIZE, proxy=None) as ws:
            await ws.send(json.dumps(fields))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "out":
                    output.append(msg["data"])
                    continue
                if not msg["ok"]:
                    raise ToolError(msg["error"])
                return {**msg, "output": "".join(output)}
        raise ToolError("connection to server.py closed")

    try:
        # Leaving exchange() closes the connection, which makes server.py stop the remote process.
        return await asyncio.wait_for(exchange(), timeout)
    except asyncio.TimeoutError as e:
        if timeout is None:
            raise ToolError("can't reach server.py; is it running?") from e
        return {"timed_out": True, "output": "".join(output)}
    except OSError as e:
        raise ToolError("can't reach server.py; is it running?") from e


def number_lines(data, first_line):
    lines = data.splitlines()
    return "\n".join(
        f"{number:>6}\t{line.decode('utf-8', errors='replace')}"
        for number, line in enumerate(lines, first_line)
    ), len(lines)


def truncation_notice(reply):
    return "\n(more results were cut off; narrow the search)" if reply["truncated"] else ""


def process_result(reply, timeout):
    output = reply["output"].rstrip("\n")
    if len(output) > MAX_OUTPUT_CHARS:
        output = "(earlier output was cut off)\n" + output[-MAX_OUTPUT_CHARS:]
    if reply.get("timed_out"):
        return f"{output}\n(stopped: still running after {timeout} seconds)"
    return f"{output}\n(exit code {reply['code']})"


def saved_with_diff(path, reply):
    return f"saved {path}\n{reply.get('diff', '')}".rstrip()


async def read_lines(path, start_line, line_count):
    """Numbered lines plus a note saying which lines these are and whether more follow."""
    end_line = start_line + line_count - 1
    reply = await request(cmd="read", path=path, start=start_line, end=end_line)
    text, lines_read = number_lines(base64.b64decode(reply["data"]), start_line)
    total = reply.get("lines")  # agents older than protocol 2 do not send it
    if lines_read == 0:
        return f"(nothing at line {start_line}; the file has {total if total is not None else 'fewer'} lines)"
    last_line = start_line + lines_read - 1
    if total is None:
        more = lines_read == line_count
        note = f"(lines {start_line}-{last_line}"
    else:
        more = last_line < total
        note = f"(lines {start_line}-{last_line} of {total}"
    return f"{text}\n{note}; read again with start_line={last_line + 1})" if more else f"{text}\n{note})"


@server.tool(description=(
    "Check that the other machine is connected. Shows its allowed folder, operating system, "
    "Python version, whether ripgrep and git are installed, and the named commands remote_exec can run."
))
async def remote_status() -> str:
    reply = await request(cmd="info")
    tools = ", ".join(name for name, found in reply["tools"].items() if found) or "none"
    commands = "\n".join(f"  {name}: {line}" for name, line in reply["commands"].items()) or "  (none configured)"
    return (f"connected\nallowed folder: {reply['root']}\nsystem: {reply['os']}, Python {reply['python']}\n"
            f"tools: {tools}\ncommands for remote_exec:\n{commands}")


@server.tool(description="List the files and folders in a folder.")
async def remote_ls(path: RemoteFolder = ".") -> str:
    reply = await request(cmd="ls", path=path)
    return "\n".join(
        f"{item['name']}/" if item["dir"] else f"{item['name']}  ({item['size']} bytes)"
        for item in reply["items"]
    ) or "(empty folder)"


@server.tool(description=(
    "List the files in a folder and its subfolders. Folders deeper than `depth` are shown as one "
    "line with a file count. Skips hidden files, and files ignored by git when ripgrep is installed."
))
async def remote_tree(
    path: RemoteFolder = ".",
    depth: Annotated[int, Field(ge=1, le=20, description="How many folder levels to list in full")] = 3,
) -> str:
    reply = await request(cmd="tree", path=path, depth=depth)
    lines = [f"{entry['path']}  ({entry['files']} files)" if "files" in entry else entry["path"]
             for entry in reply["entries"]]
    return ("\n".join(lines) or "(no files)") + truncation_notice(reply)


@server.tool(description="Show a file's size, last modified time and number of lines.")
async def remote_stat(path: RemotePath) -> str:
    reply = await request(cmd="stat", path=path)
    lines = f", {reply['lines']} lines" if "lines" in reply else ""
    return f"{'folder' if reply['dir'] else 'file'}, {reply['size']} bytes{lines}, modified {reply['modified']}"


@server.tool(description=(
    f"Read up to {LINES_PER_READ} lines of a text file. Each output line starts with its line "
    "number and a tab; that prefix is not part of the file."
))
async def remote_read(
    path: RemotePath,
    start_line: Annotated[int, Field(ge=1, description="First line to read, counting from 1")] = 1,
    line_count: Annotated[int, Field(ge=1, le=LINES_PER_READ, description="How many lines to read")] = LINES_PER_READ,
) -> str:
    return await read_lines(path, start_line, line_count)


@server.tool(description=(
    f"Read the first {LINES_PER_FILE_IN_BATCH} lines of several files in one call. "
    "Lines are numbered as in remote_read. A file that cannot be read shows its error and does not stop the others."
))
async def remote_read_many(
    paths: Annotated[list[str], Field(min_length=1, max_length=20, description="Paths relative to the allowed folder")],
) -> str:
    async def section(path):
        try:
            return f"== {path} ==\n{await read_lines(path, 1, LINES_PER_FILE_IN_BATCH)}"
        except ToolError as e:
            return f"== {path} ==\n(error: {e})"

    return "\n\n".join(await asyncio.gather(*map(section, paths)))


@server.tool(description=(
    "Search inside files for a regular expression. Returns path:line:text for each match, and "
    "path-line-text for context lines. Start the pattern with (?i) to ignore case. Skips hidden "
    "and binary files."
))
async def remote_grep(
    pattern: Annotated[str, Field(description="Regular expression to search for")],
    path: RemoteFolder = ".",
    glob: Annotated[str | None, Field(description="Only search files matching this, e.g. *.py or src/*.py")] = None,
    context_lines: Annotated[int, Field(ge=0, le=20, description="Lines to show before and after each match")] = 0,
    max_per_file: Annotated[int | None, Field(ge=1, description="Stop after this many matches in each file")] = None,
    files_only: Annotated[bool, Field(description="Return only the paths of files that match")] = False,
) -> str:
    reply = await request(cmd="grep", pattern=pattern, path=path, glob=glob, context=context_lines,
                          max_per_file=max_per_file, files_only=files_only)
    if files_only:
        lines = reply["files"]
    else:
        lines = [
            "{path}{mark}{line}{mark}{text}".format(mark="-" if match.get("context") else ":", **match)
            for match in reply["matches"]
        ]
    return ("\n".join(lines) or "(no matches)") + truncation_notice(reply)


@server.tool(description="Find files whose path matches a pattern such as **/*.py. Newest first.")
async def remote_glob(
    pattern: Annotated[str, Field(description="Path pattern, e.g. **/*.py")],
    path: RemoteFolder = ".",
) -> str:
    reply = await request(cmd="glob", pattern=pattern, path=path)
    return ("\n".join(reply["files"]) or "(no files found)") + truncation_notice(reply)


@server.tool(description=(
    "Replace one exact piece of text in a file and return a diff of the change. Fails if the text "
    "is missing, or appears more than once while replace_all is false; then include more "
    "surrounding text so it is unique. Do not include the line-number prefix from remote_read."
))
async def remote_replace(
    path: RemotePath,
    old_text: Annotated[str, Field(description="Exact text to find, including spaces and line breaks")],
    new_text: Annotated[str, Field(description="Text to put in its place")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring exactly one")] = False,
) -> str:
    reply = await request(cmd="replace", path=path, old=old_text, new=new_text, all=replace_all)
    return saved_with_diff(path, reply)


@server.tool(description=(
    "Make several replacements in one file in one call and return a diff. The edits are applied "
    "in order, and the file is saved only if every one of them applies."
))
async def remote_edit(
    path: RemotePath,
    edits: Annotated[list[Edit], Field(min_length=1, description="Replacements to make, in order")],
) -> str:
    reply = await request(cmd="replace", path=path, edits=[
        {"old": edit.old_text, "new": edit.new_text, "all": edit.replace_all} for edit in edits
    ])
    return saved_with_diff(path, reply)


@server.tool(description="Create a file, or overwrite it completely. Creates missing folders.")
async def remote_write(
    path: RemotePath,
    content: Annotated[str, Field(description="The full file contents")],
) -> str:
    await request(cmd="write", path=path, data=base64.b64encode(content.encode("utf-8")).decode())
    return f"saved {path}"


@server.tool(description="Create a folder, and any missing folders above it.")
async def remote_mkdir(path: RemotePath) -> str:
    await request(cmd="mkdir", path=path)
    return f"created {path}"


@server.tool(description="Move or rename a file or folder. Fails if the destination already exists.")
async def remote_move(
    path: RemotePath,
    new_path: Annotated[str, Field(description="Where to move it, relative to the allowed folder")],
) -> str:
    await request(cmd="mv", path=path, to=new_path)
    return f"moved {path} to {new_path}"


@server.tool(description="Delete a file or an empty folder. This cannot be undone.")
async def remote_delete(
    path: RemotePath,
    recursive: Annotated[bool, Field(description="Also delete a folder that still has files in it")] = False,
) -> str:
    await request(cmd="rm", path=path, recursive=recursive)
    return f"deleted {path}"


@server.tool(description=(
    "Run a read-only git command on the other machine: status, diff, log, show, blame or ls-files. "
    "The first argument is the git command, e.g. [\"diff\", \"--stat\"] or [\"log\", \"--oneline\", \"-10\"]."
))
async def remote_git(
    args: Arguments,
    path: WorkFolder = ".",
) -> str:
    reply = await request(DEFAULT_TIMEOUT_SECONDS, cmd="git", path=path, args=args)
    return process_result(reply, DEFAULT_TIMEOUT_SECONDS)


@server.tool(description=(
    "Run one of the named commands the user set up on the other machine, such as its tests. "
    "remote_status lists the names. Returns the output and the exit code."
))
async def remote_exec(
    name: Annotated[str, Field(description="Name of the command, as listed by remote_status")],
    args: Arguments = [],
    path: WorkFolder = ".",
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    reply = await request(timeout_seconds, cmd="exec", name=name, path=path, args=args)
    return process_result(reply, timeout_seconds)


@server.tool(description=(
    "Run a Python script that is inside the allowed folder, with the other machine's Python. "
    "Returns the output and the exit code."
))
async def remote_run(
    path: RemotePath,
    args: Arguments = [],
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    reply = await request(timeout_seconds, cmd="run", path=path, args=args)
    return process_result(reply, timeout_seconds)


if __name__ == "__main__":
    server.run()
