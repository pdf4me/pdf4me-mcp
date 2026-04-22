import asyncio
import base64
import json
import os
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

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
    for key in (
        "docContent",
        "DocContent",
        "docData",
        "DocData",
        "fileContent",
        "FileContent",
        "File Content",
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
            raise ValueError("Expected JSON object with output PDF content")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no output PDF base64 field")
        return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected PDF binary or JSON with output content, got content-type {ct!r}"
    )


async def _poll_find_and_replace_job(
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
        f"FindAndReplace did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_find_and_replace_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
    *,
    is_async: bool,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/FindAndReplace"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            if not is_async:
                raise ValueError("API returned 202 Accepted while async mode was disabled.")
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_find_and_replace_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_pdf_response(resp)


@tool(
    name="find_and_replace_text",
    title="Find And Replace Text",
    description=(
        "Find and replace text in a PDF via PDF4me /api/v2/FindAndReplace. "
        "Inputs: pdf_file_path, old_text, new_text, page_sequence; optional async and output path."
    ),
)
async def find_and_replace_text(
    pdf_file_path: str,
    old_text: str,
    new_text: str,
    page_sequence: str = "1",
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_b64, pdf_ext = file_to_base64(pdf_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read PDF file: {exc}")

    if pdf_ext.lower() != ".pdf":
        return ToolResult(content=f"Source file must be a PDF, got '{pdf_ext}' instead.")
    if not old_text:
        return ToolResult(content="Parameter 'old_text' must not be empty.")
    if not page_sequence.strip():
        return ToolResult(content="Parameter 'page_sequence' must not be empty.")

    doc_name = (request_doc_name or os.path.basename(pdf_file_path)).strip()
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": pdf_b64,
        "docName": doc_name,
        "oldText": old_text,
        "newText": new_text,
        "pageSequence": page_sequence.strip(),
    }
    payload["async"] = True

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(os.path.abspath(pdf_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"find_replace_{doc_name}"
    )

    try:
        pdf_bytes = await _call_find_and_replace_api(
            payload, pdf4me_api_key, is_async=is_async
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
        content=f"Find-and-replace completed successfully. Saved to {output_path}",
        structured_content={
            "output_path": output_path,
            "doc_name": doc_name,
            "old_text": old_text,
            "new_text": new_text,
            "page_sequence": page_sequence.strip(),
        },
    )

