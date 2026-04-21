import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 20
_ASYNC_POLL_INTERVAL_SEC = 10.0
_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept straight binary image bytes from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if ct.startswith("image/") or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected image binary response, got content-type {ct!r}")


async def _call_rotate_image_by_exif_data_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    use_async: bool,
) -> bytes:
    """POST RotateImageByExifData; return image bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        # API samples/tester for this action use `async`.
        "async": use_async,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/RotateImageByExifData",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_rotate_image_by_exif_data_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_rotate_image_by_exif_data_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """Sleep before each GET (including first), mirroring sample retry flow."""
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "RotateImageByExifData did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="rotate_image_by_exif_data",
    description=(
        "Automatically rotate a local image according to EXIF orientation metadata using "
        "the PDF4me RotateImageByExifData API. Supports async 202 polling. "
        "Requires output_dir; saves the rotated image under that directory."
    ),
)
async def rotate_image_by_exif_data_http(
    file_path: str,
    output_dir: str,
    use_async: bool = True,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Rotate an image based on EXIF orientation using PDF4me RotateImageByExifData.

    Args:
        file_path: Local path to the source image.
        output_dir: Directory for the output image (required).
        use_async: When True, sends async and polls the Location URL on 202.
        output_file_name: Output filename. Defaults to exif_rotated_<input_basename>.
    """
    doc_content_base64, ext = file_to_base64(file_path)
    if ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=f"Input file must be a supported image type, got '{ext}' instead."
        )

    doc_name = os.path.basename(file_path)
    default_out = f"exif_rotated_{doc_name}"

    if not output_dir or not output_dir.strip():
        return ToolResult(
            content="output_dir is required. Please provide an output directory path."
        )

    resolved_output_dir = output_dir.strip()
    resolved_output_name = output_file_name if output_file_name else default_out

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        image_bytes = await _call_rotate_image_by_exif_data_api(
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
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    if not image_bytes:
        return ToolResult(
            content="Unexpected API response — rotated image bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(image_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Image rotated by EXIF data successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )

