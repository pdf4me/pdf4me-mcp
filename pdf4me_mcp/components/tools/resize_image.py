import asyncio
import os
from typing import Literal, Optional

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


def _schema_query_for_resize(
    image_resize_type: Literal["Percentage", "Specific"],
) -> str:
    """URL query schemaVal: Percentange for Percentage mode (API spelling), Specific otherwise."""
    if image_resize_type == "Percentage":
        return "Percentange"
    return "Specific"


async def _call_resize_image_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    image_resize_type: Literal["Percentage", "Specific"],
    resize_percentage: str,
    width: int,
    height: int,
    maintain_aspect_ratio: bool,
    use_async: bool,
) -> bytes:
    """POST ResizeImage; return image bytes (200 body or 202 + poll)."""
    sv = _schema_query_for_resize(image_resize_type)
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "ImageResizeType": image_resize_type,
        "ResizePercentage": resize_percentage,
        "Width": width,
        "Height": height,
        "MaintainAspectRatio": maintain_aspect_ratio,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ResizeImage?schemaVal={sv}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_resize_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_resize_image_job(
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
        f"ResizeImage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="resize_image",
    description=(
        "Resize a local image using the PDF4me ResizeImage API (Percentage or Specific dimensions). "
        "Provide the image path, resize mode, and options (percentage or width/height, aspect ratio). "
        "Optional output directory and file name; saves the resized image to disk."
    ),
)
async def resize_image_http(
    file_path: str,
    image_resize_type: Literal["Percentage", "Specific"] = "Percentage",
    resize_percentage: str = "50.0",
    width: int = 800,
    height: int = 600,
    maintain_aspect_ratio: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Resize an image via PDF4me ResizeImage.

    Args:
        file_path: Local path to the source image.
        image_resize_type: Percentage (ResizePercentage) or Specific (Width/Height in pixels).
        resize_percentage: Used when image_resize_type is Percentage (decimal string, e.g. \"50.0\").
        width: Target width in pixels when using Specific mode.
        height: Target height in pixels when using Specific mode.
        maintain_aspect_ratio: Passed through to the API.
        use_async: When True, request async processing and poll the Location URL on 202.
        output_dir: Directory for the output image. Defaults to the input file directory.
        output_file_name: Output filename. Defaults to resized_<input_basename>.
    """
    doc_content_base64, ext = file_to_base64(file_path)
    if ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=f"Input file must be a supported image type, got '{ext}' instead."
        )

    doc_name = os.path.basename(file_path)
    stem, suffix = os.path.splitext(doc_name)
    default_out = f"resized_{doc_name}" if stem else f"resized{suffix or '.jpg'}"
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else default_out

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        image_bytes = await _call_resize_image_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            image_resize_type=image_resize_type,
            resize_percentage=resize_percentage.strip() or "100.0",
            width=width,
            height=height,
            maintain_aspect_ratio=maintain_aspect_ratio,
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
            content="Unexpected API response — resized image bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            image_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Image resized successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
