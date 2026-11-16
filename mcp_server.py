#!/usr/bin/env python3
import base64
import json
import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from websockets.asyncio.client import connect

CONTROL = f"ws://127.0.0.1:{os.environ.get('RC_CONTROL_PORT', '8766')}"
MAX_SIZE = 64 * 1024 * 1024
LINES_PER_READ = 2000

RemotePath = Annotated[str, Field(description="Path relative to the allowed folder on the other machine")]
RemoteFolder = Annotated[str, Field(description="Folder or file to search, relative to the allowed folder")]

server = MCPServer(
    "agent-x",
    instructions=(
        "These tools read and change files on the user's other machine, not on this one. "
        "Paths are relative to the allowed folder there. Find files with remote_glob or "
        "remote_grep, read only the lines you need with remote_read, and change files with "
        "remote_replace. Use remote_write only for new files or full rewrites."
    ),
)


async def request(**fields):
    try:
        async with connect(CONTROL, max_size=MAX_SIZE) as ws:
            await ws.send(json.dumps(fields))
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] != "done":
                    continue
                if not msg["ok"]:
                    raise ToolError(msg["error"])
                return msg
    except OSError as e:
        raise ToolError("can't reach server.py; is it running?") from e
    raise ToolError("connection to server.py closed")


def number_lines(data, first_line):
    lines = data.splitlines()
    return "\n".join(
        f"{number:>6}\t{line.decode('utf-8', errors='replace')}"
        for number, line in enumerate(lines, first_line)
    ), len(lines)


def truncation_notice(reply):
    return "\n(more results were cut off; narrow the search)" if reply["truncated"] else ""


@server.tool(description="List the files and folders in a folder.")
async def remote_ls(path: RemoteFolder = ".") -> str:
    reply = await request(cmd="ls", path=path)
    return "\n".join(
        f"{item['name']}/" if item["dir"] else f"{item['name']}  ({item['size']} bytes)"
        for item in reply["items"]
    ) or "(empty folder)"


@server.tool(description=(
    f"Read up to {LINES_PER_READ} lines of a text file. Each output line starts with its line "
    "number and a tab; that prefix is not part of the file."
))
async def remote_read(
    path: RemotePath,
    start_line: Annotated[int, Field(ge=1, description="First line to read, counting from 1")] = 1,
    line_count: Annotated[int, Field(ge=1, le=LINES_PER_READ, description="How many lines to read")] = LINES_PER_READ,
) -> str:
    end_line = start_line + line_count - 1
    reply = await request(cmd="read", path=path, start=start_line, end=end_line)
    text, lines_read = number_lines(base64.b64decode(reply["data"]), start_line)
    if lines_read == 0:
        return f"(nothing at line {start_line}; the file is empty or shorter than that)"
    if lines_read == line_count:
        text += f"\n(the file may continue; read again with start_line={end_line + 1})"
    return text


@server.tool(description=(
    "Search inside files for a regular expression. Returns path:line:text for each match. "
    "Start the pattern with (?i) to ignore case. Skips hidden and binary files."
))
async def remote_grep(
    pattern: Annotated[str, Field(description="Regular expression to search for")],
    path: RemoteFolder = ".",
    glob: Annotated[str | None, Field(description="Only search files matching this, e.g. *.py or src/*.py")] = None,
) -> str:
    reply = await request(cmd="grep", pattern=pattern, path=path, glob=glob)
    lines = [f"{match['path']}:{match['line']}:{match['text']}" for match in reply["matches"]]
    return ("\n".join(lines) or "(no matches)") + truncation_notice(reply)


@server.tool(description="Find files whose path matches a pattern such as **/*.py. Newest first.")
async def remote_glob(
    pattern: Annotated[str, Field(description="Path pattern, e.g. **/*.py")],
    path: RemoteFolder = ".",
) -> str:
    reply = await request(cmd="glob", pattern=pattern, path=path)
    return ("\n".join(reply["files"]) or "(no files found)") + truncation_notice(reply)


@server.tool(description=(
    "Replace one exact piece of text in a file. Fails if the text is missing or appears "
    "more than once; then include more surrounding text so it is unique. Do not include the "
    "line-number prefix from remote_read."
))
async def remote_replace(
    path: RemotePath,
    old_text: Annotated[str, Field(description="Exact text to find, including spaces and line breaks")],
    new_text: Annotated[str, Field(description="Text to put in its place")],
) -> str:
    await request(cmd="replace", path=path, old=old_text, new=new_text)
    return f"saved {path}"


@server.tool(description="Create a file, or overwrite it completely. Creates missing folders.")
async def remote_write(
    path: RemotePath,
    content: Annotated[str, Field(description="The full file contents")],
) -> str:
    await request(cmd="write", path=path, data=base64.b64encode(content.encode("utf-8")).decode())
    return f"saved {path}"


if __name__ == "__main__":
    server.run()
