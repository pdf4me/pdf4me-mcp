import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0
_IMAGE_FORMATS = ("BMP", "GIF", "JPG", "PNG", "TIFF")


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept straight binary image bytes from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if ct.startswith("image/") or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected image binary response, got content-type {ct!r}")


def _extension_for_format(image_format: str) -> str:
    if image_format == "JPG":
        return ".jpg"
    return f".{image_format.lower()}"


async def _call_convert_image_format_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    current_image_format: Literal["BMP", "GIF", "JPG", "PNG", "TIFF"],
    new_image_format: Literal["BMP", "GIF", "JPG", "PNG", "TIFF"],
    use_async: bool,
) -> bytes:
    """POST ConvertImageFormat; return image bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "currentImageFormat": current_image_format,
        "newImageFormat": new_image_format,
        "isAsync": use_async,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertImageFormat",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_convert_image_format_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_image_format_job(
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
        f"ConvertImageFormat did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="convert_image_format",
    description=(
        "Convert a local image between formats (BMP, GIF, JPG, PNG, TIFF) using the PDF4me "
        "ConvertImageFormat API. Provide the local image path plus current and new format. "
        "Supports sync/async processing and optional output directory and output file name."
    ),
)
async def convert_image_format_http(
    file_path: str,
    current_image_format: Literal["BMP", "GIF", "JPG", "PNG", "TIFF"] = "JPG",
    new_image_format: Literal["BMP", "GIF", "JPG", "PNG", "TIFF"] = "PNG",
    use_async: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert an image to a different format via PDF4me ConvertImageFormat.

    Args:
        file_path: Local path to the source image.
        current_image_format: Source image format (BMP, GIF, JPG, PNG, TIFF).
        new_image_format: Target image format (BMP, GIF, JPG, PNG, TIFF).
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the converted image. Defaults to input file directory.
        output_file_name: Name for the output file. Defaults to converted_<input_basename><new_ext>.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    allowed_extensions = {".bmp", ".gif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    if extension.lower() not in allowed_extensions:
        return ToolResult(
            content=f"Input file must be an image (BMP/GIF/JPG/PNG/TIFF), got '{extension}' instead."
        )

    if current_image_format not in _IMAGE_FORMATS or new_image_format not in _IMAGE_FORMATS:
        return ToolResult(
            content=(
                "Invalid image format. current_image_format and new_image_format "
                "must be one of BMP, GIF, JPG, PNG, TIFF."
            )
        )

    doc_name = os.path.basename(file_path)
    input_stem, _ = os.path.splitext(doc_name)
    default_name = f"converted_{input_stem or 'image'}{_extension_for_format(new_image_format)}"
    resolved_output_dir = output_dir if output_dir else os.path.dirname(os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else default_name

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        image_bytes = await _call_convert_image_format_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            current_image_format=current_image_format,
            new_image_format=new_image_format,
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

    if not image_bytes:
        return ToolResult(
            content="Unexpected API response — converted image bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(image_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Image format converted successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
