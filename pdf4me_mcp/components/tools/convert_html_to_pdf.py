import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


async def _call_convert_html_to_pdf_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    layout: str,
    page_format: str,
    scale: float,
    top_margin: str,
    bottom_margin: str,
    left_margin: str,
    right_margin: str,
    print_background: bool,
    display_header_footer: bool,
) -> bytes:
    """POST ConvertHtmlToPdf; return raw PDF bytes (200 or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "layout": layout,
        "format": page_format,
        "scale": scale,
        "topMargin": top_margin,
        "bottomMargin": bottom_margin,
        "leftMargin": left_margin,
        "rightMargin": right_margin,
        "printBackground": print_background,
        "displayHeaderFooter": display_header_footer,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertHtmlToPdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_convert_html_to_pdf_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_html_to_pdf_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> bytes:
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
        f"HTML to PDF did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="convert_html_to_pdf",
    description=(
        "Convert a local HTML file to PDF using the PDF4me ConvertHtmlToPdf API. "
        "Provide the file path to the HTML document. "
        "Configure layout, page format, scale, margins, print background, and header/footer. "
        "Optionally set output directory and output file name. "
        " "
    ),
)
async def convert_html_to_pdf_http(
    file_path: str,
    layout: Literal["Portrait", "Landscape"] = "Portrait",
    page_format: str = "A4",
    scale: float = 0.8,
    top_margin: str = "40px",
    bottom_margin: str = "40px",
    left_margin: str = "40px",
    right_margin: str = "40px",
    print_background: bool = True,
    display_header_footer: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert HTML to PDF via PDF4me ConvertHtmlToPdf.

    Args:
        file_path: Local path to the HTML file (.html or .htm).
        layout: Portrait or Landscape.
        page_format: Page size (e.g. A4, Letter).
        scale: Content scale (e.g. 0.8 = 80%).
        top_margin, bottom_margin, left_margin, right_margin: CSS-like values (e.g. 40px).
        print_background: Include backgrounds in the PDF.
        display_header_footer: Show header/footer when supported.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    ext = extension.lower()
    if ext not in (".html", ".htm"):
        return ToolResult(
            content=f"Input file must be HTML (.html or .htm), got '{extension}' instead."
        )

    doc_name = os.path.basename(file_path)

    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    stem, _ = os.path.splitext(doc_name)
    resolved_output_name = (
        output_file_name if output_file_name else f"{stem}.pdf"
    )

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_convert_html_to_pdf_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            layout=layout,
            page_format=page_format,
            scale=scale,
            top_margin=top_margin,
            bottom_margin=bottom_margin,
            left_margin=left_margin,
            right_margin=right_margin,
            print_background=print_background,
            display_header_footer=display_header_footer,
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
        content=f"HTML converted to PDF successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
