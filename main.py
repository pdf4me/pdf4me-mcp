import os
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.providers import FileSystemProvider

import config

import uvicorn
from uvicorn.logging import DefaultFormatter

import logging
# ---------------------------------------------------------------------------
# Logging — use uvicorn's coloured formatter so logs match uvicorn's style
# ---------------------------------------------------------------------------


provider = FileSystemProvider(
    root=Path(__file__).parent / "mcp"
)

config = config.Config()

mcp = FastMCP("PDF4me MCP Server", providers=[provider])

if __name__ == "__main__":
    mcp.run()
