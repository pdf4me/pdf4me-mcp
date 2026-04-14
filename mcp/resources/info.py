from fastmcp.resources.function_resource import resource


@resource("info://tools")
def available_tools() -> str:
    """Describes all available tools in this MCP server."""

    return (
        "This MCP server has the following tools:\n"
        "- compress_pdf: Compress a PDF using the PDF4me API. "
        "Accepts a source PDF file path and returns compressed PDF output. "
        "Supports optimization profiles: Web, Print, and Screen.\n"
    )
