import functools
import logging
import os

import anyio.to_thread
from dotenv import find_dotenv, load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from trading212_mcp_server.api.client import T212Client

load_dotenv(find_dotenv())

log = logging.getLogger("trading212")

mcp = FastMCP(
    name="trading212",
    dependencies=["pydantic"],
)

client = T212Client()

READ_ONLY = os.getenv("T212_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
if READ_ONLY:
    log.info("T212_READ_ONLY is set: trading, pie and export tools are disabled.")


def tool(name: str, annotations: ToolAnnotations):
    """Register a synchronous tool with FastMCP.

    The client blocks (HTTP calls and rate-limit sleeps), so each call runs in a
    worker thread instead of on the event loop. Tools not marked read-only are
    skipped entirely when T212_READ_ONLY is set.
    """
    def decorator(fn):
        if READ_ONLY and not annotations.readOnlyHint:
            return fn

        def call(*args, **kwargs):
            # Worker threads are reused: start each call with a clean wait counter.
            client.reset_wait_time()
            return fn(*args, **kwargs)

        @functools.wraps(fn)
        async def run_in_thread(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(call, *args, **kwargs))

        mcp.tool(name, annotations=annotations)(run_in_thread)
        return fn

    return decorator
