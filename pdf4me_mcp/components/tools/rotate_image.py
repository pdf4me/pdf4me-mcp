import asyncio
import os
from typing import Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0
_ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png",
                      ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept straight binary image bytes from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if ct.startswith("image/") or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(
        f"Expected image binary response, got content-type {ct!r}")


async def _call_rotate_image_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    background_color: str,
    proportionate_resize: bool,
    rotation_angle: int,
) -> bytes:
    """POST RotateImage; return image bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "Backgroundcolor": background_color,
        "ProportionateResize": proportionate_resize,
        "RotationAngle": rotation_angle,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/RotateImage",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_rotate_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_rotate_image_job(
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
        f"RotateImage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="rotate_image",
    description=(
        "Rotate a local image using the PDF4me RotateImage API. "
        "Supports rotation angle, background color, proportionate resize, and async 202 polling. "
        "Optional output directory and file name; saves the rotated image to disk."
    ),
)
async def rotate_image_http(
    file_path: str,
    rotation_angle: int = 90,
    background_color: str = "#FFFFFF",
    proportionate_resize: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Rotate an image using the PDF4me RotateImage API.

    Args:
        file_path: Local path to the source image.
        rotation_angle: Rotation in degrees (integer, e.g. 90).
        background_color: Fill color behind rotated bounds (e.g. #FFFFFF). Sent as API key Backgroundcolor.
        proportionate_resize: Whether to keep proportions during rotation (API ProportionateResize).
    """
    doc_content_base64, ext = file_to_base64(file_path)
    if ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=f"Input file must be a supported image type, got '{ext}' instead."
        )

    doc_name = os.path.basename(file_path)
    default_out = f"rotated_{doc_name}"
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else default_out

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        image_bytes = await _call_rotate_image_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            background_color=background_color.strip() or "#FFFFFF",
            proportionate_resize=proportionate_resize,
            rotation_angle=rotation_angle,
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
        write_file_from_bytes(
            image_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Image rotated successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
