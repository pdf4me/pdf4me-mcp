import asyncio
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

# Match sample client: large base64 + slow reads; same budget as other heavy PDF4me tools.
_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
#  maxRetries=10, retryDelay=10s, delay before every poll (including first).
_ASYNC_POLL_MAX_ATTEMPTS = 10
_ASYNC_POLL_INTERVAL_SEC = 10.0
_ALLOWED_IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}


def _barcode_data_from_response(resp: httpx.Response) -> Any:
    content_type = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in content_type:
        return resp.json()

    text = raw.decode("utf-8", errors="replace").strip()
    if not text.startswith(("{", "[")):
        raise ValueError(
            f"Unexpected API response content type: {content_type}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc


_ImageType = Literal["JPG", "PNG", "GIF", "BMP", "TIFF", "WEBP"]


def _infer_image_type_from_extension(extension: str) -> _ImageType:
    """Infer API imageType from extension ."""
    ext = extension.lower()
    if ext in {".jpg", ".jpeg"}:
        return "JPG"
    if ext == ".png":
        return "PNG"
    if ext == ".gif":
        return "GIF"
    if ext == ".bmp":
        return "BMP"
    if ext in {".tif", ".tiff"}:
        return "TIFF"
    if ext == ".webp":
        return "WEBP"
    return "JPG"


async def _call_read_barcodes_from_image_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    image_type: _ImageType,
) -> Any:
    """Call ReadBarcodesfromImage and return parsed barcode JSON data."""
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "imageType": image_type,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ReadBarcodesfromImage",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location")
                        or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_read_barcodes_from_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _barcode_data_from_response(resp)


async def _poll_read_barcodes_from_image_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> Any:
    #  Task.Delay before each GET, including the first poll.
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _barcode_data_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "ReadBarcodesfromImage did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="read_barcodes_from_image",
    description=(
        "Read barcodes and QR codes from a local image using the PDF4me ReadBarcodesfromImage API. "
        "Provide the image path and optionally image type (JPG, PNG, GIF, BMP, TIFF, WEBP). "
        " (payload key isAsync). "
        "Returns structured barcode data as JSON."
    ),
)
async def read_barcodes_from_image_http(
    file_path: str,
    image_type: Optional[_ImageType] = None,
) -> ToolResult:
    """Read barcodes from an image via PDF4me ReadBarcodesfromImage.

    Args:
        file_path: Local path to the image file.
        image_type: Image type passed to API payload. If omitted, inferred from extension (unknown defaults to JPG).
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=(
                f"Input file must be a supported image type "
                f"({', '.join(sorted(_ALLOWED_IMAGE_EXT))}), got '{extension}' instead."
            )
        )

    resolved_image_type = image_type if image_type else _infer_image_type_from_extension(
        extension
    )
    doc_name = os.path.basename(file_path)

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        barcode_data = await _call_read_barcodes_from_image_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            image_type=resolved_image_type,
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

    if barcode_data is None:
        return ToolResult(content="Unexpected API response — no barcode data returned.")

    return ToolResult(
        content="Barcode data from image retrieved successfully.",
        structured_content={
            "file_name": doc_name,
            "barcode_data": barcode_data,
        },
    )
