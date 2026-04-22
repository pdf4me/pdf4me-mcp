import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _parse_json_body(resp: httpx.Response) -> Optional[dict[str, Any]]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith((b"{", b"[")):
        return None
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ExtractTextFromWord")
    return parsed


def _extract_text_and_name(resp: httpx.Response) -> tuple[str, Optional[str], Optional[dict[str, Any]]]:
    parsed = _parse_json_body(resp)
    if isinstance(parsed, dict):
        text = parsed.get("extractedText") or parsed.get("ExtractedText")
        file_name = parsed.get("fileName") or parsed.get("FileName")
        if isinstance(text, str):
            return text, (file_name if isinstance(file_name, str) else None), parsed

    ct = (resp.headers.get("content-type") or "").lower()
    if "text/plain" in ct or "text/" in ct:
        return resp.text, None, parsed

    raw = _strip_utf8_bom_and_leading_ws(resp.content)
    if raw:
        try:
            return raw.decode("utf-8"), None, parsed
        except UnicodeDecodeError:
            raise ValueError(
                f"Expected JSON or text response from ExtractTextFromWord, got content-type {ct!r}"
            )

    raise ValueError("ExtractTextFromWord returned an empty response body")


async def _poll_extract_text_from_word_job(
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
        "ExtractTextFromWord did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_extract_text_from_word_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
    *,
    is_async: bool,
) -> httpx.Response:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ExtractTextFromWord"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }

    async with httpx.AsyncClient(timeout=300) as client:
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
            return await _poll_extract_text_from_word_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return resp


def _default_output_dir(word_file_path: str) -> str:
    p = Path(word_file_path).resolve()
    return str(p.parent / f"extracted_text_from_word_{p.stem}")


@tool(
    name="extract_text_from_word",
    title="Extract Text from Word",
    description=(
        "Extract text from a Word document via PDF4me /api/v2/ExtractTextFromWord. "
        "Supports page range and content filtering options (comments, header/footer, tracked changes). "
        "Saves extracted text and JSON response metadata to disk."
    ),
)
async def extract_text_from_word(
    word_file_path: str,
    start_page_number: int = 1,
    end_page_number: int = 1,
    remove_comments: bool = True,
    remove_header_footer: bool = True,
    accept_changes: bool = True,
    request_doc_name: Optional[str] = None,
    is_async: bool = False,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        word_b64, word_ext = file_to_base64(word_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read Word file: {exc}")

    if word_ext.lower() not in (".docx", ".doc"):
        return ToolResult(
            content=f"Source file must be a Word document (.docx/.doc), got '{word_ext}'."
        )

    if start_page_number < 1 or end_page_number < 1:
        return ToolResult(content="start_page_number and end_page_number must be >= 1.")
    if end_page_number < start_page_number:
        return ToolResult(
            content="end_page_number must be greater than or equal to start_page_number."
        )

    doc_name = (request_doc_name or os.path.basename(word_file_path)).strip()
    if not doc_name.lower().endswith((".docx", ".doc")):
        doc_name = f"{doc_name}.docx"

    payload: dict[str, Any] = {
        "docContent": word_b64,
        "docName": doc_name,
        "StartPageNumber": start_page_number,
        "EndPageNumber": end_page_number,
        "RemoveComments": remove_comments,
        "RemoveHeaderFooter": remove_header_footer,
        "AcceptChanges": accept_changes,
    }
    if is_async:
        payload["async"] = True

    resolved_out = output_dir if output_dir else _default_output_dir(word_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        final_resp = await _call_extract_text_from_word_api(
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

    try:
        extracted_text, api_file_name, response_json = _extract_text_and_name(final_resp)
    except ValueError as exc:
        return ToolResult(content=str(exc))

    resolved_output_name = (
        output_file_name
        if output_file_name
        else (api_file_name if api_file_name else f"extracted_text_{Path(doc_name).stem}.txt")
    )
    if not resolved_output_name.lower().endswith(".txt"):
        resolved_output_name = f"{resolved_output_name}.txt"

    text_path = os.path.join(resolved_out, resolved_output_name)
    try:
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(extracted_text)
    except OSError as exc:
        return ToolResult(content=f"Failed to write extracted text file: {exc}")

    json_path: Optional[str] = None
    if response_json is not None:
        json_path = os.path.join(resolved_out, "extracted_text_from_word.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(response_json, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            return ToolResult(content=f"Failed to write JSON response file: {exc}")

    preview = extracted_text[:2000]
    if len(extracted_text) > 2000:
        preview += "\n… (truncated)"

    return ToolResult(
        content=f"Text extracted successfully. Saved to {text_path}",
        structured_content={
            "output_directory": resolved_out,
            "text_path": text_path,
            "json_path": json_path,
            "doc_name": doc_name,
            "start_page_number": start_page_number,
            "end_page_number": end_page_number,
            "remove_comments": remove_comments,
            "remove_header_footer": remove_header_footer,
            "accept_changes": accept_changes,
            "text_preview": preview,
        },
    )

