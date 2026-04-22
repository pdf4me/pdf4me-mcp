import asyncio
import base64
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

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


async def _poll_crop_image_job(
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
        f"CropImage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_crop_image_api(
    payload: dict[str, Any],
    crop_type: str,
    pdf4me_api_key: str,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/CropImage?schemaVal={crop_type}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
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
            return await _poll_crop_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_image_response(resp)


CropTypeOption = Literal["Border", "Rectangle"]


@tool(
    name="crop_image",
    description=(
        "Crop an image via PDF4me CropImage (/api/v2/CropImage). "
        "Choose crop_type Border or Rectangle. "
        "Border: set left/right/top/bottom border in pixels. "
        "Rectangle: set upper_left_x/y and width/height."
    ),
)
async def crop_image(
    image_file_path: str,
    crop_type: CropTypeOption = "Border",
    left_border: Optional[int] = None,
    right_border: Optional[int] = None,
    top_border: Optional[int] = None,
    bottom_border: Optional[int] = None,
    upper_left_x: Optional[int] = None,
    upper_left_y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        img_b64, _ = file_to_base64(image_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read source image: {exc}")

    if crop_type == "Border":
        if any(v is None for v in (left_border, right_border, top_border, bottom_border)):
            return ToolResult(
                content=(
                    "For crop_type='Border', provide left_border, right_border, "
                    "top_border, and bottom_border."
                )
            )
    else:
        if any(v is None for v in (upper_left_x, upper_left_y, width, height)):
            return ToolResult(
                content=(
                    "For crop_type='Rectangle', provide upper_left_x, upper_left_y, "
                    "width, and height."
                )
            )

    resolved_doc_name = doc_name or os.path.basename(image_file_path)
    payload: dict[str, Any] = {
        "docName": resolved_doc_name,
        "docContent": img_b64,
        "CropType": crop_type,
        "isAsync": True,
    }

    if left_border is not None:
        payload["LeftBorder"] = str(left_border)
    if right_border is not None:
        payload["RightBorder"] = str(right_border)
    if top_border is not None:
        payload["TopBorder"] = str(top_border)
    if bottom_border is not None:
        payload["BottomBorder"] = str(bottom_border)
    if upper_left_x is not None:
        payload["UpperLeftX"] = upper_left_x
    if upper_left_y is not None:
        payload["UpperLeftY"] = upper_left_y
    if width is not None:
        payload["Width"] = width
    if height is not None:
        payload["Height"] = height

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(
            os.path.abspath(image_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"cropped_{os.path.basename(image_file_path)}"
    )

    try:
        image_bytes = await _call_crop_image_api(payload, crop_type, pdf4me_api_key)
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
        content=f"Cropped image saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "crop_type": crop_type,
        },
    )
