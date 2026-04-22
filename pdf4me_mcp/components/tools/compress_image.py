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


def _output_file_name_from_json(payload: dict[str, Any]) -> Optional[str]:
    for key in ("File Name", "fileName", "docName", "DocName"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _bytes_and_name_from_compress_image_response(
    resp: httpx.Response,
) -> tuple[bytes, Optional[str]]:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if any(t in ct for t in ("image/", "application/octet-stream")):
        return raw, None

    trimmed = _strip_utf8_bom_and_leading_ws(raw)
    if trimmed.startswith((b"{", b"[")):
        try:
            payload = json.loads(trimmed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if isinstance(payload, dict):
            b64 = _docdata_b64_from_json(payload)
            if b64:
                return base64.b64decode(b64), _output_file_name_from_json(payload)

    if raw:
        return raw, None

    raise ValueError(
        f"Expected image binary or JSON with base64, got content-type {ct!r}"
    )


async def _poll_compress_image_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> httpx.Response:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return poll
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"CompressImage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_compress_image_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> tuple[bytes, Optional[str]]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/CompressImage"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            final = await _poll_compress_image_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return _bytes_and_name_from_compress_image_response(final)
        resp.raise_for_status()
        return _bytes_and_name_from_compress_image_response(resp)


ImageTypeOption = Literal["JPG", "PNG", "WebP"]
CompressionLevelOption = Literal["Max", "Medium", "Low"]


@tool(
    name="compress_image",
    description=(
        "Compress an image via PDF4me CompressImage (/api/v2/CompressImage). "
        "image_file_path, image_type (JPG, PNG, WebP), compression_level (Max, Medium, Low); "
        "optional doc_name, output_dir, output_file_name. Uses async 202 polling when applicable."
    ),
)
async def compress_image(
    image_file_path: str,
    image_type: ImageTypeOption = "JPG",
    compression_level: CompressionLevelOption = "Medium",
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
        img_b64, _ext = file_to_base64(image_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read image file: {exc}")

    resolved_doc_name = doc_name or os.path.basename(image_file_path)

    payload: dict[str, Any] = {
        "docContent": img_b64,
        "docName": resolved_doc_name,
        "imageType": image_type,
        "compressionLevel": compression_level,
        "isAsync": True,
    }

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(
            os.path.abspath(image_file_path))
    )
    resolved_output_name = output_file_name or None

    try:
        image_bytes, api_suggested_name = await _call_compress_image_api(payload, pdf4me_api_key)
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

    final_name = (
        resolved_output_name
        or api_suggested_name
        or f"compressed_{os.path.basename(image_file_path)}"
    )

    try:
        output_path = write_file_from_bytes(
            image_bytes, resolved_output_dir, final_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"Compressed image saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "image_type": image_type,
            "compression_level": compression_level,
        },
    )
