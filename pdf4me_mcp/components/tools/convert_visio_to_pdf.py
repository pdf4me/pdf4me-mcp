import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _output_pdf_filename(input_name: str) -> str:
    stem, _ = os.path.splitext(input_name)
    return f"{stem}.pdf" if stem else "output.pdf"


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


async def _call_convert_visio_to_pdf_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    is_pdf_compliant: bool,
    page_index: int,
    page_count: int,
    include_hidden_pages: bool,
    save_foreground_page: bool,
    save_tool_bar: bool,
    auto_fit: bool,
    use_async: bool,
) -> bytes:
    """POST ConvertVisio; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "OutputFormat": "PDF",
        "IsPdfCompliant": is_pdf_compliant,
        "PageIndex": page_index,
        "PageCount": page_count,
        "IncludeHiddenPages": include_hidden_pages,
        "SaveForegroundPage": save_foreground_page,
        "SaveToolBar": save_tool_bar,
        "AutoFit": auto_fit,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertVisio?schemaVal=PDF",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            return await _poll_convert_visio_to_pdf_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_visio_to_pdf_job(
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
        f"ConvertVisio did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="convert_visio_to_pdf",
    description=(
        "Convert a local Visio file (.vsdx/.vsd/.vsdm) to PDF using the PDF4me ConvertVisio API. "
        "Supports PDF options (compliance, page range, hidden pages, foreground/toolbar, auto-fit), "
        "sync/async processing, and optional output directory/file name."
    ),
)
async def convert_visio_to_pdf_http(
    file_path: str,
    is_pdf_compliant: bool = True,
    page_index: int = 0,
    page_count: int = 5,
    include_hidden_pages: bool = True,
    save_foreground_page: bool = True,
    save_tool_bar: bool = True,
    auto_fit: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert a Visio file to PDF via PDF4me ConvertVisio.

    Args:
        file_path: Local path to the Visio file (.vsdx/.vsd/.vsdm).
        is_pdf_compliant: Make PDF compliant with standards.
        page_index: Start page index (0-based).
        page_count: Number of pages to convert.
        include_hidden_pages: Include hidden pages.
        save_foreground_page: Keep foreground page content.
        save_tool_bar: Include toolbar content.
        auto_fit: Auto-fit content to page.
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the PDF. Defaults to the same directory as the input file.
        output_file_name: Name for the output PDF. Defaults to <input_basename>.pdf.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    ext = extension.lower()
    if ext not in (".vsdx", ".vsd", ".vsdm"):
        return ToolResult(
            content=f"Input file must be a Visio file (.vsdx/.vsd/.vsdm), got '{extension}' instead."
        )

    input_name = os.path.basename(file_path)
    default_pdf_name = _output_pdf_filename(input_name)

    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else default_pdf_name
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_convert_visio_to_pdf_api(
            doc_content_base64,
            default_pdf_name,
            PDF4ME_API_KEY,
            is_pdf_compliant=is_pdf_compliant,
            page_index=page_index,
            page_count=page_count,
            include_hidden_pages=include_hidden_pages,
            save_foreground_page=save_foreground_page,
            save_tool_bar=save_tool_bar,
            auto_fit=auto_fit,
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
        content=f"Visio converted to PDF successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
