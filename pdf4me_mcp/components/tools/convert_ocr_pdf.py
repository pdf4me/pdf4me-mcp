import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

# Same long-timeout / polling budget as sign_pdf.py (large base64 + slow OCR).
_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 40
_ASYNC_POLL_INTERVAL_SEC = 2.5


def _api_bool_str(value: bool) -> str:
    """API expects string 'true' / 'false' for ocrWhenNeeded."""
    return "true" if value else "false"


async def _call_convert_ocr_pdf_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    quality_type: Literal["Draft", "High"],
    ocr_when_needed: bool,
    language: str,
) -> bytes:
    """POST ConvertOcrPdf; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "qualityType": quality_type,
        "ocrWhenNeeded": _api_bool_str(ocr_when_needed),
        "language": language.strip() or "eng",
        "isAsync": True,
    }

    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertOcrPdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or "").strip()
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_convert_ocr_pdf_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_ocr_pdf_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """First GET immediately, then poll every interval_sec until 200 or give up."""
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"OCR PDF conversion did not finish after {max_attempts} polls ({interval_sec}s apart). "

    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="convert_ocr_pdf",
    description=(
        "Convert a PDF to a searchable, editable PDF using the PDF4me ConvertOcrPdf API (OCR). "
        "Provide the local path to the input PDF, quality (Draft or High), ocr_when_needed, and language (e.g. eng). "
        "The API request body only includes docContent, docName, qualityType, ocrWhenNeeded, and language. "
        "If the API returns 202, the tool polls the Location URL until the PDF is ready. "
        "Optional output directory and output file name."
    ),
)
async def convert_ocr_pdf_http(
    file_path: str,
    quality_type: Literal["Draft", "High"] = "Draft",
    ocr_when_needed: bool = True,
    language: str = "eng",
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert a PDF to an OCR / editable PDF via PDF4me ConvertOcrPdf.

    Args:
        file_path: Local path to the PDF to process.
        quality_type: Draft or High.
        ocr_when_needed: When true, skip OCR if text is already searchable (sent as \"true\"/\"false\").
        language: OCR language code (e.g. eng). Empty defaults to eng.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = (
        output_file_name if output_file_name else f"editable_{doc_name}"
    )
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_convert_ocr_pdf_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            quality_type=quality_type,
            ocr_when_needed=ocr_when_needed,
            language=language,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(
                content="Authentication failed: the API key is invalid or missing."
            )
        return ToolResult(
            content=f"API error {exc.response.status_code}: {exc.response.text}"
        )
    except httpx.ReadTimeout as exc:
        return ToolResult(
            content=f"HTTP read timed out waiting for PDF4me (payload may be large): {exc}"
        )
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ToolResult(
            content="Unexpected API response — OCR PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF OCR conversion completed. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
