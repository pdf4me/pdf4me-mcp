import asyncio
import base64
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _docx_filename_hint(doc_name: str) -> str:
    if doc_name.lower().endswith(".pdf"):
        return doc_name[:-4] + ".docx"
    if doc_name.lower().endswith(".docx"):
        return doc_name
    return f"{doc_name}.docx"


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    """Read base64 from Document.DocData / document.docData ."""
    if depth > 12 or not isinstance(obj, dict):
        return None
    for dk in ("docData", "DocData"):
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


def _bytes_from_docx_response(resp: httpx.Response) -> bytes:
    """Binary DOCX from Content-Type or body; otherwise JSON with Document.DocData base64."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "wordprocessingml" in ct or "application/octet-stream" in ct:
        return raw

    body = _strip_utf8_bom_and_leading_ws(raw)
    if body.startswith(b"PK"):
        return body

    trimmed = _strip_utf8_bom_and_leading_ws(raw)
    if trimmed.startswith((b"{", b"[")):
        try:
            payload = json.loads(trimmed.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with Document.DocData")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError(
                "Response JSON has no Document.DocData / docData base64 field")
        return base64.b64decode(b64)

    raise ValueError(
        f"Expected Word binary or JSON with DocData, got content-type {ct!r}"
    )


async def _call_convert_pdf_to_word_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    quality_type: str,
    language: str,
    merge_all_sheets: bool,
    ocr_when_needed: bool,
) -> bytes:
    """POST ConvertPdfToWord; return raw DOCX bytes (200 or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "qualityType": quality_type,
        "language": language,
        "mergeAllSheets": merge_all_sheets,
        "outputFormat": "Docx",
        "ocrWhenNeeded": "true" if ocr_when_needed else "false",
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertPdfToWord",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_convert_pdf_to_word_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_docx_response(resp)


async def _poll_convert_pdf_to_word_job(
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
            return _bytes_from_docx_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"PDF to Word did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="convert_pdf_to_word",
    description=(
        "Convert a local PDF file to Word (DOCX) using the PDF4me ConvertPdfToWord API. "
        "Provide the file path to the PDF. "
        " Options: quality (Draft/High), merge_all_sheets, language, OCR when needed, and optional output path. "
        " "
        "Output is always DOCX."
    ),
)
async def convert_pdf_to_word_http(
    file_path: str,
    quality_type: Literal["Draft", "High"] = "Draft",
    language: str = "English",
    merge_all_sheets: bool = True,
    ocr_when_needed: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert PDF to Word via PDF4me ConvertPdfToWord.

    Args:
        file_path: Local path to the PDF file to convert.
        quality_type: Draft or High quality for extraction.
        language: Document language hint for OCR/extraction.
        merge_all_sheets: Merge content into a single output when supported.
        ocr_when_needed: Enable OCR when the API determines it is needed.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = (
        output_file_name if output_file_name else _docx_filename_hint(doc_name)
    )

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        docx_bytes = await _call_convert_pdf_to_word_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            quality_type=quality_type,
            language=language,
            merge_all_sheets=merge_all_sheets,
            ocr_when_needed=ocr_when_needed,
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

    if not docx_bytes or not docx_bytes.startswith(b"PK") or len(docx_bytes) < 100:
        return ToolResult(
            content="Unexpected API response — Word document missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            docx_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF converted to Word successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
