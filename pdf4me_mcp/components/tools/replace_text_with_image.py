import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 40
_ASYNC_POLL_INTERVAL_SEC = 2.5

_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png",
                      ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """Binary PDF from ReplaceTextWithImage success response."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


async def _call_replace_text_with_image_api(
    doc_content_base64: str,
    image_content_base64: str,
    PDF4ME_API_KEY: str,
    replace_text: str,
    page_sequence: str,
    image_height: int,
    image_width: int,
) -> bytes:
    """POST ReplaceTextWithImage; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "replaceText": replace_text,
        "pageSequence": page_sequence,
        "imageContent": image_content_base64,
        "imageHeight": image_height,
        "imageWidth": image_width,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ReplaceTextWithImage",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_replace_text_with_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_replace_text_with_image_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
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
        "Replace text with image did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="replace_text_with_image",
    description=(
        "Replace occurrences of a text string in a PDF with an image using the PDF4me "
        "ReplaceTextWithImage API (POST /api/v2/ReplaceTextWithImage). "
        "Provide the PDF path, image path, text to replace, page sequence (e.g. all, 1, 1,3,5, 2-5), "
        "and image width/height in pixels. Supports async via isAsync and 202 Location polling. "
        "Optional output directory and file name."
    ),
)
async def replace_text_with_image_http(
    file_path: str,
    image_file_path: str,
    replace_text: str,
    page_sequence: str = "all",
    image_height: int = 50,
    image_width: int = 100,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Replace text in a PDF with an image via PDF4me ReplaceTextWithImage.

    Args:
        file_path: Local path to the input PDF.
        image_file_path: Local path to the replacement image (e.g. PNG, JPG).
        replace_text: Literal text in the PDF to replace with the image.
        page_sequence: Pages to search (e.g. \"all\", \"1\", \"1,3,5\", \"2-5\").
        image_height: Display height of the placed image in pixels (API integer).
        image_width: Display width of the placed image in pixels (API integer).
    """
    pdf_b64, pdf_ext = file_to_base64(file_path)
    if pdf_ext.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{pdf_ext}' instead.")

    img_b64, img_ext = file_to_base64(image_file_path)
    if img_ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=f"Image file must be a supported image type, got '{img_ext}' instead."
        )

    pdf_basename = os.path.basename(file_path)
    if not replace_text.strip():
        return ToolResult(content="replace_text must be a non-empty string.")

    resolved_output_dir = (
        output_dir.strip()
        if isinstance(output_dir, str) and output_dir.strip()
        else os.path.dirname(os.path.abspath(file_path))
    )
    if not output_file_name or not output_file_name.strip():
        return ToolResult(
            content="output_file_name is required. Please provide an output file name."
        )
    resolved_output_name = output_file_name.strip()
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_replace_text_with_image_api(
            pdf_b64,
            img_b64,
            PDF4ME_API_KEY,
            replace_text=replace_text.strip(),
            page_sequence=page_sequence.strip() or "all",
            image_height=image_height,
            image_width=image_width,
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
            content="Unexpected API response — PDF bytes missing or invalid after replace."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Text replaced with image successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
