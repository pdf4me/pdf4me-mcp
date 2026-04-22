import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0

SplitTextPage = Literal["before", "after", "Before", "After"]
FileNaming = Literal["NameAsPerOrder"]


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_obj_from_response(resp: httpx.Response) -> Any:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith((b"{", b"[")):
        raise ValueError(
            f"Expected JSON response from SplitByText, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc


def _extract_split_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        raw = (
            payload.get("splitedDocuments")
            or payload.get("splited Documents")
            or payload.get("splitDocuments")
            or payload.get("outputDocuments")
        )
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        return [payload]
    return []


def _decode_split_item(item: dict[str, Any], index: int) -> tuple[bytes, str]:
    b64 = (
        item.get("streamFile")
        or item.get("StreamFile")
        or item.get("docContent")
        or item.get("DocContent")
        or item.get("File Content")
        or item.get("fileContent")
    )
    if not isinstance(b64, str) or not b64:
        raise ValueError(f"Split item #{index + 1} has no base64 PDF content")

    name = (
        item.get("fileName")
        or item.get("File Name")
        or item.get("docName")
        or item.get("DocName")
        or f"split_by_text_{index + 1}.pdf"
    )
    if not isinstance(name, str) or not name.strip():
        name = f"split_by_text_{index + 1}.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"

    try:
        return base64.b64decode(b64), name
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Invalid base64 content for split item #{index + 1}: {exc}") from exc


async def _poll_split_pdf_by_text_job(
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
        f"SplitByText did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_split_pdf_by_text_api(payload: dict[str, Any], pdf4me_api_key: str) -> Any:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/SplitByText"
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
                    "API returned 202 Accepted but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            final = await _poll_split_pdf_by_text_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return _json_obj_from_response(final)
        resp.raise_for_status()
        return _json_obj_from_response(resp)


def _default_output_dir(pdf_file_path: str) -> str:
    p = Path(pdf_file_path).resolve()
    return str(p.parent / f"split_pdf_by_text_{p.stem}")


@tool(
    name="split_pdf_by_text",
    description=(
        "Split a PDF by matching text via PDF4me SplitByText (/api/v2/SplitByText). "
        "Provide pdf_file_path, text, split_text_page (before/after), and file_naming."
    ),
)
async def split_pdf_by_text(
    pdf_file_path: str,
    text: str,
    split_text_page: SplitTextPage = "after",
    file_naming: FileNaming = "NameAsPerOrder",
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(content="Authentication failed: no API key provided in the request.")

    try:
        pdf_b64, pdf_ext = file_to_base64(pdf_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read PDF file: {exc}")

    if pdf_ext.lower() != ".pdf":
        return ToolResult(content=f"Source file must be a PDF, got '{pdf_ext}' instead.")

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": pdf_b64,
        "docName": doc_name,
        "text": text,
        "splitTextPage": split_text_page,
        "fileNaming": file_naming,
        "isAsync": True,
    }

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        response_payload = await _call_split_pdf_by_text_api(payload, pdf4me_api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(content="Authentication failed: the API key is invalid or missing.")
        return ToolResult(content=f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    raw_json_path = os.path.join(
        resolved_out, "split_pdf_by_text_response.json")
    try:
        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump(response_payload, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write raw split response JSON: {exc}")

    items = _extract_split_items(response_payload)
    if not items:
        return ToolResult(
            content=(
                f"SplitByText response saved to {raw_json_path}, but no split documents were found "
                "in known fields (splitedDocuments/outputDocuments/list)."
            ),
            structured_content={
                "output_directory": resolved_out, "raw_json_path": raw_json_path},
        )

    output_paths: list[str] = []
    for i, item in enumerate(items):
        try:
            pdf_bytes, file_name = _decode_split_item(item, i)
        except ValueError as exc:
            return ToolResult(content=str(exc))
        try:
            output_paths.append(write_file_from_bytes(
                pdf_bytes, resolved_out, file_name))
        except OSError as exc:
            return ToolResult(content=f"Failed to write split output file: {exc}")

    return ToolResult(
        content=f"Split by text completed successfully. Saved {len(output_paths)} PDF file(s) to {resolved_out}",
        structured_content={
            "output_directory": resolved_out,
            "raw_json_path": raw_json_path,
            "split_count": len(output_paths),
            "output_paths": output_paths,
            "text": text,
            "split_text_page": split_text_page,
        },
    )
