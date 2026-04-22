from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.providers import FileSystemProvider


provider = FileSystemProvider(
    root=Path(__file__).parent / "components"
)

mcp = FastMCP("PDF4me MCP Server", providers=[provider])


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
