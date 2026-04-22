import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

# Large base64 payloads + server-side processing can exceed short read defaults.
_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)

_ASYNC_POLL_MAX_ATTEMPTS = 50
_ASYNC_POLL_INTERVAL_SEC = 5.0
_ALLOWED_IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
}


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept straight binary image bytes from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if ct.startswith("image/") or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(
        f"Expected image binary response, got content-type {ct!r}")


_ImageType = Literal["JPG", "PNG"]


def _infer_image_type_from_extension(extension: str) -> _ImageType:
    """Infer API imageType from extension (unknown extension -> JPG)."""
    ext = extension.lower()
    if ext in {".jpg", ".jpeg"}:
        return "JPG"
    if ext == ".png":
        return "PNG"
    return "JPG"


async def _call_remove_exif_tags_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    image_type: _ImageType,
    use_async: bool,
) -> bytes:
    """POST RemoveEXIFTagsFromImage; return image bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
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
            f"{api_base_url}/api/v2/RemoveEXIFTagsFromImage",
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
            return await _poll_remove_exif_tags_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_remove_exif_tags_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """Sleep before each GET (including first)."""
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "RemoveEXIFTagsFromImage did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="remove_exif_tags_from_image",
    description=(
        "Remove EXIF metadata tags from a local image using the PDF4me RemoveEXIFTagsFromImage API. "
        "Supports sync and async processing with 202 polling. "
        "Uses long HTTP timeouts for large uploads and slow jobs. "
        "Input supports JPG/PNG; imageType is optional. "
        "output_file_name is required; output_dir is optional."
    ),
)
async def remove_exif_tags_from_image_http(
    file_path: str,
    output_file_name: str,
    image_type: Optional[_ImageType] = None,
    output_dir: Optional[str] = None
) -> ToolResult:
    """Remove EXIF metadata from an image using PDF4me RemoveEXIFTagsFromImage.

    Args:
        file_path: Local path to the source image.
        image_type: Payload image type ("JPG" or "PNG"). If omitted, inferred from file extension.
        use_async: When True, request async processing and poll the Location URL on 202.
        output_dir: Directory for the output image. Defaults to the input file directory.
    """
    resolved_output_name = output_file_name.strip()
    if not resolved_output_name:
        return ToolResult(content="output_file_name is required and must not be empty.")

    doc_content_base64, ext = file_to_base64(file_path)
    if ext.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=(
                f"Input file must use a supported image extension "
                f"({', '.join(sorted(_ALLOWED_IMAGE_EXT))}), got '{ext}' instead."
            )
        )

    resolved_image_type = image_type if image_type else _infer_image_type_from_extension(
        ext)
    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        image_bytes = await _call_remove_exif_tags_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            image_type=resolved_image_type,
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

    if not image_bytes:
        return ToolResult(
            content="Unexpected API response — cleaned image bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            image_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"EXIF tags removed successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
