import asyncio
import base64
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    if depth > 12 or not isinstance(obj, dict):
        return None
    for dk in ("File Content", "fileContent", "docContent", "DocContent", "docData", "DocData"):
        v = obj.get(dk)
        if isinstance(v, str) and v:
            return v
    for doc_key in ("document", "Document"):
        sub = obj.get(doc_key)
        if isinstance(sub, dict):
            found = _docdata_b64_from_json(sub, depth=depth + 1)
            if found:
                return found
    return None


def _bytes_from_image_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if any(t in ct for t in ("image/", "application/octet-stream")):
        return raw

    trimmed = _strip_utf8_bom_and_leading_ws(raw)
    if trimmed.startswith((b"{", b"[")):
        try:
            payload = json.loads(trimmed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if isinstance(payload, dict):
            b64 = _docdata_b64_from_json(payload)
            if b64:
                return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected image binary or JSON with base64, got content-type {ct!r}"
    )


def _build_payload(
    doc_name: str,
    doc_content_b64: str,
    watermark_file_name: str,
    watermark_file_b64: str,
    position: str,
    opacity: Optional[float],
    horizontal_offset: Optional[int],
    vertical_offset: Optional[int],
    position_x: Optional[float],
    position_y: Optional[float],
    rotation: Optional[float],
    use_async: bool,
) -> dict:
    payload: dict = {
        "docName": doc_name,
        "docContent": doc_content_b64,
        "WatermarkFileName": watermark_file_name,
        "WatermarkFileContent": watermark_file_b64,
        "Position": position,
        "isAsync": True,
    }
    if opacity is not None:
        payload["Opacity"] = opacity
    if horizontal_offset is not None:
        payload["HorizontalOffset"] = horizontal_offset
    if vertical_offset is not None:
        payload["VerticalOffset"] = vertical_offset
    if position_x is not None:
        payload["PositionX"] = position_x
    if position_y is not None:
        payload["PositionY"] = position_y
    if rotation is not None:
        payload["Rotation"] = rotation
    return payload


async def _call_add_image_watermark_api(
    payload: dict,
    PDF4ME_API_KEY: str,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/AddImageWatermarkToImage"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_add_image_watermark_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_image_response(resp)


async def _poll_add_image_watermark_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_image_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"AddImageWatermarkToImage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


PositionOption = Literal[
    "topright",
    "topleft",
    "bottomright",
    "bottomleft",
    "centralhorizontal",
    "diagonal",
    "centralvertical",
    "custom",
]


@tool(
    name="add_image_watermark_to_image",
    description=(
        "Overlay a watermark image on a source image via PDF4me AddImageWatermarkToImage (/api/v2/AddImageWatermarkToImage). "
        "Provide image_file_path, watermark_image_file_path, and position "
        "(topright, topleft, bottomright, bottomleft, centralhorizontal, diagonal, centralvertical, custom). "
        "Optional: opacity (0.0–1.0), horizontal/vertical offset, position_x/y for custom, rotation (degrees), output path."
    ),
)
async def add_image_watermark_to_image(
    image_file_path: str,
    watermark_image_file_path: str,
    position: PositionOption,
    doc_name: Optional[str] = None,
    watermark_file_name: Optional[str] = None,
    opacity: Optional[float] = None,
    horizontal_offset: Optional[int] = None,
    vertical_offset: Optional[int] = None,
    position_x: Optional[float] = None,
    position_y: Optional[float] = None,
    rotation: Optional[float] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        src_b64, _ = file_to_base64(image_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read source image: {exc}")

    try:
        wm_b64, _ = file_to_base64(watermark_image_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read watermark image: {exc}")

    resolved_doc_name = doc_name or os.path.basename(image_file_path)
    resolved_wm_name = watermark_file_name or os.path.basename(
        watermark_image_file_path)

    payload = _build_payload(
        doc_name=resolved_doc_name,
        doc_content_b64=src_b64,
        watermark_file_name=resolved_wm_name,
        watermark_file_b64=wm_b64,
        position=position,
        opacity=opacity,
        horizontal_offset=horizontal_offset,
        vertical_offset=vertical_offset,
        position_x=position_x,
        position_y=position_y,
        rotation=rotation,
        use_async=use_async,
    )

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(
            os.path.abspath(image_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name
        else f"watermarked_{os.path.basename(image_file_path)}"
    )

    try:
        image_bytes = await _call_add_image_watermark_api(payload, PDF4ME_API_KEY)
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
        return ToolResult(content="Unexpected API response — no image data returned.")

    try:
        output_path = write_file_from_bytes(
            image_bytes, resolved_output_dir, resolved_output_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"Watermarked image saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "position": position,
        },
    )
