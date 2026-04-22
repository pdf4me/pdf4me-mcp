import asyncio
import base64
import json
import os
from typing import Any, Optional

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
    for key in (
        "File Content",
        "fileContent",
        "docContent",
        "DocContent",
        "docData",
        "DocData",
    ):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    for nested_key in ("document", "Document"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            found = _docdata_b64_from_json(nested, depth=depth + 1)
            if found:
                return found
    return None


def _output_file_name_from_json(payload: dict[str, Any]) -> Optional[str]:
    for key in ("File Name", "fileName", "docName", "DocName"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _bytes_and_name_from_merge_response(resp: httpx.Response) -> tuple[bytes, Optional[str]]:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/pdf" in ct or "application/octet-stream" in ct:
        return raw, None

    body = _strip_utf8_bom_and_leading_ws(raw)
    if body.startswith(b"%PDF"):
        return body, None

    if body.startswith((b"{", b"[")):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with output PDF content")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no output PDF base64 field")
        return base64.b64decode(b64), _output_file_name_from_json(payload)

    if raw:
        return raw, None

    raise ValueError(
        f"Expected PDF binary or JSON with output content, got content-type {ct!r}"
    )


async def _poll_merge_job(
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
            pdf_bytes, _ = _bytes_and_name_from_merge_response(poll)
            return pdf_bytes
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"Merge did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_merge_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> tuple[bytes, Optional[str]]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/Merge"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            if False:
                raise ValueError(
                    "API returned 202 Accepted while async mode was disabled.")
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            pdf_bytes = await _poll_merge_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return pdf_bytes, None
        resp.raise_for_status()
        return _bytes_and_name_from_merge_response(resp)


@tool(
    name="merge_multiple_pdfs",
    title="Merge Multiple PDFs",
    description=(
        "Merge multiple PDF files into one via PDF4me /api/v2/Merge. "
        " Inputs: pdf_file_paths (list of local PDFs), optional request_doc_name, and output path."
    ),
)
async def merge_multiple_pdfs(
    pdf_file_paths: list[str],
    request_doc_name: str = "merged_output.pdf",
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    if len(pdf_file_paths) < 2:
        return ToolResult(
            content="At least two PDF files are required in pdf_file_paths."
        )

    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    docs_b64: list[str] = []
    for path in pdf_file_paths:
        try:
            pdf_b64, pdf_ext = file_to_base64(path)
        except OSError as exc:
            return ToolResult(content=f"Could not read PDF file '{path}': {exc}")
        if pdf_ext.lower() != ".pdf":
            return ToolResult(content=f"Source file must be a PDF, got '{pdf_ext}' in '{path}'.")
        docs_b64.append(pdf_b64)

    doc_name = request_doc_name.strip()
    if not doc_name:
        doc_name = "merged_output.pdf"
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": docs_b64,
        "docName": doc_name,
    }
    payload["isAsync"] = True

    first_input_abs = os.path.abspath(pdf_file_paths[0])
    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(first_input_abs)
    )

    try:
        pdf_bytes, api_file_name = await _call_merge_api(
            payload, pdf4me_api_key
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

    resolved_output_name = output_file_name or api_file_name or doc_name

    try:
        output_path = write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"Merged PDF saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "input_count": len(pdf_file_paths),
            "doc_name": doc_name,
        },
    )
