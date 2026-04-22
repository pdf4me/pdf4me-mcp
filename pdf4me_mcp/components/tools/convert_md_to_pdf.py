import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes

# Long reads/writes for large MD base64 JSON + slow ConvertMdToPdf processing.
_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _pdf_name_from_doc_name(doc_name: str) -> str:
    stem, _ = os.path.splitext(doc_name)
    if stem:
        return f"{stem}.pdf"
    return "output.pdf"


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


async def _call_convert_md_to_pdf_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    use_async: bool,
) -> bytes:
    """POST ConvertMdToPdf; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "mdFilePath": "",
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertMdToPdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or "").strip()
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")

            return await _poll_convert_md_to_pdf_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_md_to_pdf_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """First GET immediately, then poll every interval_sec until 200 or give up."""
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"Markdown to PDF did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="convert_md_to_pdf",
    description=(
        "Convert a local Markdown file (.md) to PDF using the PDF4me ConvertMdToPdf API. "
        "Provide the file path to the Markdown file and output_dir where the PDF will be saved. "
        "Long HTTP timeouts on requests. "
        "When use_async is true, the API may return 202; the tool polls the Location URL until "
        "complete. Optional output file name (defaults to <input_basename>.pdf)."
    ),
)
async def convert_md_to_pdf_http(
    file_path: str,
    output_dir: str,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert Markdown to PDF via PDF4me ConvertMdToPdf.

    Args:
        file_path: Local path to the Markdown file (.md).
        output_dir: Directory to save the PDF (required).
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_file_name: Name for the output PDF. Defaults to <input_basename>.pdf.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".md":
        return ToolResult(
            content=f"Input file must be Markdown (.md), got '{extension}' instead."
        )

    doc_name = os.path.basename(file_path)

    if not output_dir or not output_dir.strip():
        return ToolResult(
            content="output_dir is required. Please provide an output directory path."
        )

    resolved_output_dir = output_dir.strip()
    resolved_output_name = (
        output_file_name if output_file_name else _pdf_name_from_doc_name(
            doc_name)
    )

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_convert_md_to_pdf_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            use_async=use_async,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(
                content="Authentication failed: the API key is invalid or missing."
            )
        return ToolResult(
            content=f"API error {exc.response.status_code}: {exc.response.text}"
        )
    except httpx.ReadTimeout as exc:
        return ToolResult(
            content=f"HTTP read timed out waiting for PDF4me (payload may be large): {exc}"
        )
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ToolResult(
            content="Unexpected API response — PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Markdown converted to PDF successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
