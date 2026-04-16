import asyncio
import base64
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    if depth > 12 or not isinstance(obj, dict):
        return None
    for dk in ("docContent", "DocContent", "docData", "DocData"):
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


def _bytes_from_pdf_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/pdf" in ct or "application/octet-stream" in ct:
        return raw

    body = _strip_utf8_bom_and_leading_ws(raw)
    if body.startswith(b"%PDF"):
        return body

    trimmed = _strip_utf8_bom_and_leading_ws(raw)
    if trimmed.startswith((b"{", b"[")):
        try:
            payload = json.loads(trimmed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with docContent")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no docContent/DocData base64 field")
        return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected PDF binary or JSON with docContent, got content-type {ct!r}"
    )


def _build_payload(
    doc_name: str,
    doc_content_base64: str,
    image_name: str,
    image_file_base64: str,
    align_x: str,
    align_y: str,
    pages: str,
    height_in_mm: Optional[str],
    width_in_mm: Optional[str],
    height_in_px: Optional[str],
    width_in_px: Optional[str],
    margin_x_in_mm: Optional[str],
    margin_y_in_mm: Optional[str],
    margin_x_in_px: Optional[str],
    margin_y_in_px: Optional[str],
    opacity: Optional[int],
    is_background: Optional[bool],
    show_only_in_print: Optional[bool],
    use_async: bool,
) -> dict:
    payload: dict = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "imageName": image_name,
        "imageFile": image_file_base64,
        "alignX": align_x,
        "alignY": align_y,
        "pages": pages,
        "isAsync": use_async,
    }
    if height_in_mm is not None:
        payload["heightInMM"] = height_in_mm
    if width_in_mm is not None:
        payload["widthInMM"] = width_in_mm
    if height_in_px is not None:
        payload["heightInPx"] = height_in_px
    if width_in_px is not None:
        payload["widthInPx"] = width_in_px
    if margin_x_in_mm is not None:
        payload["marginXInMM"] = margin_x_in_mm
    if margin_y_in_mm is not None:
        payload["marginYInMM"] = margin_y_in_mm
    if margin_x_in_px is not None:
        payload["marginXInPx"] = margin_x_in_px
    if margin_y_in_px is not None:
        payload["marginYInPx"] = margin_y_in_px
    if opacity is not None:
        payload["opacity"] = opacity
    if is_background is not None:
        payload["isBackground"] = is_background
    if show_only_in_print is not None:
        payload["showOnlyInPrint"] = show_only_in_print
    return payload


async def _call_image_stamp_api(
    payload: dict,
    PDF4ME_API_KEY: str,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ImageStamp"
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
            return await _poll_image_stamp_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_pdf_response(resp)


async def _poll_image_stamp_job(
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
            return _bytes_from_pdf_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"ImageStamp did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="add_image_stamp_to_pdf",
    description=(
        "Stamp or watermark a PDF with an image using PDF4me ImageStamp (/api/v2/ImageStamp). "
        "Provide pdf_file_path, image_file_path (PNG/JPG/etc.), alignX (Left/Center/Right), alignY (Top/Middle/Bottom). "
        "Optional: pages, size in MM or pixels, margins, opacity, is_background, show_only_in_print, output path."
    ),
)
async def add_image_stamp_to_pdf(
    pdf_file_path: str,
    image_file_path: str,
    align_x: Literal["Left", "Center", "Right"],
    align_y: Literal["Top", "Middle", "Bottom"],
    use_async: bool = True,
    image_name: Optional[str] = None,
    request_doc_name: Optional[str] = None,
    pages: str = "",
    height_in_mm: Optional[str] = None,
    width_in_mm: Optional[str] = None,
    height_in_px: Optional[str] = None,
    width_in_px: Optional[str] = None,
    margin_x_in_mm: Optional[str] = None,
    margin_y_in_mm: Optional[str] = None,
    margin_x_in_px: Optional[str] = None,
    margin_y_in_px: Optional[str] = None,
    opacity: Optional[int] = None,
    is_background: Optional[bool] = None,
    show_only_in_print: Optional[bool] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_b64, pdf_ext = file_to_base64(pdf_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read PDF file: {exc}")

    if pdf_ext.lower() != ".pdf":
        return ToolResult(
            content=f"Source file must be a PDF, got '{pdf_ext}' instead."
        )

    try:
        image_b64, _ = file_to_base64(image_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read image file: {exc}")

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    resolved_image_name = image_name or os.path.basename(image_file_path)

    payload = _build_payload(
        doc_name=doc_name,
        doc_content_base64=pdf_b64,
        image_name=resolved_image_name,
        image_file_base64=image_b64,
        align_x=align_x,
        align_y=align_y,
        pages=pages,
        height_in_mm=height_in_mm,
        width_in_mm=width_in_mm,
        height_in_px=height_in_px,
        width_in_px=width_in_px,
        margin_x_in_mm=margin_x_in_mm,
        margin_y_in_mm=margin_y_in_mm,
        margin_x_in_px=margin_x_in_px,
        margin_y_in_px=margin_y_in_px,
        opacity=opacity,
        is_background=is_background,
        show_only_in_print=show_only_in_print,
        use_async=use_async,
    )

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(os.path.abspath(pdf_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name
        else f"image_stamp_{os.path.basename(pdf_file_path)}"
    )

    try:
        pdf_bytes = await _call_image_stamp_api(payload, PDF4ME_API_KEY)
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

    if not pdf_bytes:
        return ToolResult(content="Unexpected API response — no PDF content returned.")

    try:
        output_path = write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"PDF with image stamp saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "doc_name": doc_name,
            "image_name": resolved_image_name,
        },
    )
