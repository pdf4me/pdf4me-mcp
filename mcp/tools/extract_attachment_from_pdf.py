import asyncio
import base64
import json
import os
import zipfile
from io import BytesIO
from pathlib import Path
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


async def _poll_extract_attachment_job(
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
        f"ExtractAttachmentFromPdf did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_extract_attachment_from_pdf_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> httpx.Response:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ExtractAttachmentFromPdf"
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
            return await _poll_extract_attachment_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return resp


def _default_output_dir(pdf_file_path: str) -> str:
    p = Path(pdf_file_path).resolve()
    return str(p.parent / f"extracted_attachments_{p.stem}")


def _output_documents(data: dict[str, Any]) -> list[Any]:
    for key in ("outputDocuments", "OutputDocuments"):
        raw = data.get(key)
        if isinstance(raw, list):
            return raw
    return []


def _decode_attachment_item(item: Any, index: int) -> tuple[bytes, str]:
    if not isinstance(item, dict):
        raise ValueError(f"Unexpected outputDocuments entry type: {type(item)!r}")

    name = (
        item.get("fileName")
        or item.get("FileName")
        or item.get("docName")
        or item.get("DocName")
        or f"attachment_{index + 1}.bin"
    )
    if not isinstance(name, str) or not name.strip():
        name = f"attachment_{index + 1}.bin"
    else:
        name = name.strip()

    b64 = item.get("streamFile") or item.get("StreamFile")
    if not isinstance(b64, str) or not b64:
        raise ValueError(f"Attachment {name!r} has no streamFile base64 data")

    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid base64 for {name!r}: {exc}") from exc

    return raw, name


def _response_looks_json(resp: httpx.Response) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        return True
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    return body.startswith(b"{")


@tool(
    name="extract_attachment_from_pdf",
    description=(
        "Extract embedded file attachments from a PDF via PDF4me ExtractAttachmentFromPdf "
        "(/api/v2/ExtractAttachmentFromPdf). pdf_file_path; optional output_dir (defaults next to PDF). "
        "Saves extracted_attachments.json when JSON, decodes outputDocuments to files, or saves/extracts ZIP."
    ),
)
async def extract_attachment_from_pdf(
    pdf_file_path: str,
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
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

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": pdf_b64,
        "docName": doc_name,
        "isAsync": True,
    }

    resolved_out = output_dir if output_dir else _default_output_dir(pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        final_resp = await _call_extract_attachment_from_pdf_api(payload, pdf4me_api_key)
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

    saved_paths: list[str] = []
    zip_path: Optional[str] = None
    extracted_names: list[str] = []

    if _response_looks_json(final_resp):
        try:
            data: Any = json.loads(
                _strip_utf8_bom_and_leading_ws(final_resp.content).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ToolResult(content=f"Invalid JSON in API response: {exc}")

        if not isinstance(data, dict):
            return ToolResult(content="Expected JSON object with outputDocuments from API")

        json_path = os.path.join(resolved_out, "extracted_attachments.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            return ToolResult(content=f"Failed to write JSON: {exc}")
        saved_paths.append(json_path)

        docs = _output_documents(data)
        for i, item in enumerate(docs):
            try:
                raw, fname = _decode_attachment_item(item, i)
                path = write_file_from_bytes(raw, resolved_out, fname)
                saved_paths.append(path)
            except ValueError as exc:
                return ToolResult(content=str(exc))
            except OSError as exc:
                return ToolResult(content=f"Failed to write attachment: {exc}")

        summary = (
            f"Extracted {len(docs)} attachment document(s) from PDF; "
            f"output in {resolved_out} (see extracted_attachments.json and files)."
        )
        return ToolResult(
            content=summary,
            structured_content={
                "output_directory": resolved_out,
                "json_path": json_path,
                "saved_paths": saved_paths,
                "attachment_count": len(docs),
                "response_mode": "json",
            },
        )

    raw = final_resp.content
    try:
        zip_path = write_file_from_bytes(raw, resolved_out, "extracted_attachments.zip")
    except OSError as exc:
        return ToolResult(content=f"Failed to write ZIP/binary response: {exc}")

    if len(raw) >= 4 and raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(raw), "r") as zf:
                zf.extractall(resolved_out)
                extracted_names = zf.namelist()
        except zipfile.BadZipFile:
            extracted_names = []

    if extracted_names:
        summary = (
            f"API returned a ZIP bundle; saved to {zip_path} and extracted "
            f"{len(extracted_names)} file(s) into {resolved_out}."
        )
    else:
        summary = (
            f"API returned binary data; saved to {zip_path}. "
            "Could not unpack as ZIP or archive had no entries."
        )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "zip_path": zip_path,
            "zip_member_names": extracted_names,
            "attachment_count": len(extracted_names),
            "response_mode": "zip",
        },
    )
