import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

# Long reads/writes for large base64 JSON + slow SignPdf processing.
_HTTP_TIMEOUT = httpx.Timeout(connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 40
_ASYNC_POLL_INTERVAL_SEC = 2.5

_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


def _str_field(value: str | int | float) -> str:
    return str(value).strip()


async def _call_sign_pdf_api(
    doc_content_base64: str,
    doc_name: str,
    image_file_base64: str,
    image_name: str,
    PDF4ME_API_KEY: str,
    *,
    pages: str,
    align_x: str,
    align_y: str,
    width_in_mm: str,
    height_in_mm: str,
    width_in_px: str,
    height_in_px: str,
    margin_x_mm: str,
    margin_y_mm: str,
    margin_x_px: str,
    margin_y_px: str,
    opacity: str,
    show_only_in_print: bool,
    is_background: bool,
    use_async: bool,
) -> bytes:
    """POST SignPdf; return raw PDF bytes (handles 200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "imageFile": image_file_base64,
        "imageName": image_name,
        "pages": pages,
        "alignX": align_x,
        "alignY": align_y,
        "widthInMM": width_in_mm,
        "heightInMM": height_in_mm,
        "widthInPx": width_in_px,
        "heightInPx": height_in_px,
        "marginXInMM": margin_x_mm,
        "marginYInMM": margin_y_mm,
        "marginXInPx": margin_x_px,
        "marginYInPx": margin_y_px,
        "opacity": opacity,
        "showOnlyInPrint": show_only_in_print,
        "isBackground": is_background,
        "isAsync": use_async,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/SignPdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_sign_pdf_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_sign_pdf_job(
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
        f"Sign PDF did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="sign_pdf",
    description=(
        "Add a signature image to a PDF using the PDF4me SignPdf API. "
        "Provide paths to the PDF and signature image (e.g. JPG/PNG). "
        "Supports page ranges, alignment, size, margins, opacity, and async 202 polling. "
        "Default use_async is false to avoid MCP client request timeouts on large payloads; set true if needed. "
        "Optional output directory and file name; saves the signed PDF to disk."
    ),
)
async def sign_pdf_http(
    file_path: str,
    signature_file_path: str,
    pages: str = "1-3",
    align_x: str = "right",
    align_y: str = "bottom",
    width_in_mm: str = "50",
    height_in_mm: str = "25",
    width_in_px: str = "142",
    height_in_px: str = "71",
    margin_x_mm: str = "20",
    margin_y_mm: str = "20",
    margin_x_px: str = "57",
    margin_y_px: str = "57",
    opacity: str = "100",
    show_only_in_print: bool = True,
    is_background: bool = False,
    use_async: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Sign a PDF with an image using the PDF4me SignPdf API.

    Args:
        file_path: Local path to the PDF to sign.
        signature_file_path: Local path to the signature image file.
        pages: Page spec (e.g. \"1\", \"1,3,5\", \"2-5\", \"1-3\").
        align_x: Horizontal alignment (e.g. Left, Center, Right — API may accept any casing).
        align_y: Vertical alignment (e.g. Top, Middle, Bottom).
        width_in_mm, height_in_mm, width_in_px, height_in_px: Size as string values for the API.
        margin_x_mm, margin_y_mm, margin_x_px, margin_y_px: Margins as string values for the API.
        opacity: Opacity 0–100 as a string.
        show_only_in_print: Maps to showOnlyInPrint.
        is_background: Maps to isBackground.
        use_async: When True, request async processing and poll the Location URL on 202.
        output_dir: Directory for the signed PDF. Defaults to the PDF's directory.
        output_file_name: Output filename. Defaults to signed_<input_pdf_basename>.pdf.
    """
    pdf_b64, pdf_ext = file_to_base64(file_path)
    if pdf_ext.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{pdf_ext}' instead.")

    sig_b64, sig_ext = file_to_base64(signature_file_path)
    if sig_ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=f"Signature file must be a common image type, got '{sig_ext}' instead."
        )

    pdf_basename = os.path.basename(file_path)
    sig_name = os.path.basename(signature_file_path)

    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = (
        output_file_name if output_file_name else f"signed_{pdf_basename}"
    )
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_sign_pdf_api(
            pdf_b64,
            resolved_output_name,
            sig_b64,
            sig_name,
            PDF4ME_API_KEY,
            pages=pages.strip() or "1",
            align_x=_str_field(align_x),
            align_y=_str_field(align_y),
            width_in_mm=_str_field(width_in_mm),
            height_in_mm=_str_field(height_in_mm),
            width_in_px=_str_field(width_in_px),
            height_in_px=_str_field(height_in_px),
            margin_x_mm=_str_field(margin_x_mm),
            margin_y_mm=_str_field(margin_y_mm),
            margin_x_px=_str_field(margin_x_px),
            margin_y_px=_str_field(margin_y_px),
            opacity=_str_field(opacity),
            show_only_in_print=show_only_in_print,
            is_background=is_background,
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
            content="Unexpected API response — signed PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF signed successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
