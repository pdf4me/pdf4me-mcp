import asyncio
import base64
import json
import os
from typing import Any, Optional

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

    if body.startswith((b"{", b"[")):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with docContent")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError(
                "Response JSON has no docContent/DocData base64 field")
        return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected PDF binary or JSON with docContent, got content-type {ct!r}"
    )


async def _poll_add_margin_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
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
        f"AddMargin did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_add_margin_api(
    doc_name: str,
    doc_content_base64: str,
    PDF4ME_API_KEY: str,
    margin_left: Optional[int],
    margin_right: Optional[int],
    margin_top: Optional[int],
    margin_bottom: Optional[int],
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/AddMargin"
    payload: dict[str, Any] = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "isAsync": True,
    }
    if margin_left is not None:
        payload["marginLeft"] = margin_left
    if margin_right is not None:
        payload["marginRight"] = margin_right
    if margin_top is not None:
        payload["marginTop"] = margin_top
    if margin_bottom is not None:
        payload["marginBottom"] = margin_bottom

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
            return await _poll_add_margin_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_pdf_response(resp)


@tool(
    name="add_margin_to_pdf",
    description=(
        "Add margins to a PDF using the PDF4me AddMargin API. "
        "Provide a local PDF path and optional margins in millimeters "
        "(margin_left/right/top/bottom, 0-100). Saves the resulting PDF to disk."
    ),
)
async def add_margin_to_pdf(
    pdf_file_path: str,
    margin_left: Optional[int] = None,
    margin_right: Optional[int] = None,
    margin_top: Optional[int] = None,
    margin_bottom: Optional[int] = None,
    request_doc_name: Optional[str] = None,
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

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(
            os.path.abspath(pdf_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"margin_{doc_name}"
    )

    try:
        pdf_bytes = await _call_add_margin_api(
            doc_name=doc_name,
            doc_content_base64=pdf_b64,
            PDF4ME_API_KEY=PDF4ME_API_KEY,
            margin_left=margin_left,
            margin_right=margin_right,
            margin_top=margin_top,
            margin_bottom=margin_bottom,
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
        content=f"PDF with margins saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "doc_name": doc_name,
            "margins_mm": {
                "left": margin_left,
                "right": margin_right,
                "top": margin_top,
                "bottom": margin_bottom,
            },
        },
    )
