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


def _bytes_from_word_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in ct
        or "application/msword" in ct
        or "application/octet-stream" in ct
    ):
        return raw

    body = _strip_utf8_bom_and_leading_ws(raw)
    if body.startswith((b"{", b"[")):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with output file content")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no output file content field")
        return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected Word binary or JSON with base64 file content, got content-type {ct!r}"
    )


async def _poll_enable_tracking_job(
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
            return _bytes_from_word_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "EnableTrackingChangesInWord did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_enable_tracking_api(
    doc_name: str,
    doc_content_base64: str,
    pdf4me_api_key: str,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/EnableTrackingChangesInWord"
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_enable_tracking_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_word_response(resp)


@tool(
    name="enable_tracking_changes_in_word",
    title="Enable Track Changes in Word",
    description=(
        "Enable Word Track Changes via PDF4me /api/v2/EnableTrackingChangesInWord. "
        "Input is a local Word file path (.docx or .doc) which is sent as docContent Base64; "
        "docName defaults to the input filename. Saves a Word output file with track changes enabled."
    ),
)
async def enable_tracking_changes_in_word(
    word_file_path: str,
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
        doc_b64, ext = file_to_base64(word_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read Word file: {exc}")

    if ext.lower() not in (".docx", ".doc"):
        return ToolResult(
            content=f"Source file must be a Word document (.docx/.doc), got '{ext}'."
        )

    doc_name = (request_doc_name or os.path.basename(word_file_path)).strip()
    if not doc_name.lower().endswith((".docx", ".doc")):
        doc_name = f"{doc_name}.docx"

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(os.path.abspath(word_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"tracked_{doc_name}"
    )

    try:
        word_bytes = await _call_enable_tracking_api(doc_name, doc_b64, pdf4me_api_key)
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

    if not word_bytes:
        return ToolResult(
            content="Unexpected API response — no output document content returned."
        )

    try:
        output_path = write_file_from_bytes(
            word_bytes, resolved_output_dir, resolved_output_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"Track changes enabled successfully. Saved to {output_path}",
        structured_content={"output_path": output_path, "doc_name": doc_name},
    )

