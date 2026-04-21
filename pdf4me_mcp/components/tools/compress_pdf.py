import base64
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes


async def _call_compress_api(
    doc_content_base64: str,
    doc_name: str,
    optimize_profile: str,
    PDF4ME_API_KEY: str,
) -> bytes:
    """Call the PDF4me Optimize API and return the compressed PDF bytes."""
    print("printing config values")
    print(f"PDF4ME_BASE_URL: {config.pdf4me_base_url}")
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "optimizeProfile": optimize_profile,
        "isAsync": False,
    }
    api_base_url = config.pdf4me_base_url
    # api_base_url = "https://api-dev.pdf4me.com"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/Optimize",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {PDF4ME_API_KEY}",
            },
        )
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        return base64.b64decode(data.get("docContent") or data.get("File Content", ""))
    return resp.content


@tool(
    name="compress_pdf",
    description=(
        "Compress a PDF file using the PDF4me API to reduce file size. "
        "Provide the local file path to the PDF. "
        "Choose an optimization profile: Web (fast download), Print (high-quality), or Screen (screen viewing). "
        "Optionally specify an output directory and output file name."
    ),
)
async def compress_pdf_http(
    file_path: str,
    optimize_profile: Literal["Web", "Print", "Screen"] = "Web",
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Compress a PDF using the PDF4me API to reduce file size.

    Args:
        file_path: Local path to the PDF file to compress.
        optimize_profile: Compression profile — one of Web, Print, or Screen.
        output_dir: Directory to save the compressed file. Defaults to the same directory as the input file.
        output_file_name: Name for the output file. Defaults to compressed_<input_filename>.pdf.
    """
    # --- read and validate input file ---
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else f"compressed_{doc_name}"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    # --- call API ---
    try:
        pdf_bytes = await _call_compress_api(
            doc_content_base64, doc_name, optimize_profile, PDF4ME_API_KEY
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(
                content="Authentication failed: the API key is invalid or missing."
            )
        return ToolResult(
            content=f"API error {exc.response.status_code}: {exc.response.text}"
        )
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")

    if not pdf_bytes:
        return ToolResult(
            content="Unexpected API response — no document content returned."
        )

    # --- write output file ---
    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF compressed successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
