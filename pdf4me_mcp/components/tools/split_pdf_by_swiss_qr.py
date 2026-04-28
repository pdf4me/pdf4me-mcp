import asyncio
import base64
import json
import os
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0

BarcodeFilter = Literal["startsWith", "endsWith", "contains", "exact"]
BarcodeType = Literal["any", "datamatrix", "qrcode"]
SplitBarcodePage = Literal["before", "after", "Before", "After"]
PdfRenderDpi = Literal["100", "150", "200", "250"]


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_obj_from_response(resp: httpx.Response) -> Any:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith((b"{", b"[")):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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
        or f"split_by_swiss_qr_{index + 1}.pdf"
    )
    if not isinstance(name, str) or not name.strip():
        name = f"split_by_swiss_qr_{index + 1}.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"

    try:
        return base64.b64decode(b64), name
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Invalid base64 content for split item #{index + 1}: {exc}") from exc


async def _poll_split_pdf_by_swiss_qr_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
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
        f"SplitPdfBySwissQR did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_split_pdf_by_swiss_qr_api(
    payload: dict[str, Any], pdf4me_api_key: str
) -> httpx.Response:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/SplitPdfByBarcode"
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
            return await _poll_split_pdf_by_swiss_qr_job(
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
    return str(p.parent / f"split_pdf_by_swiss_qr_{p.stem}")


def _normalize_barcode_filter(value: str) -> str:
    key = value.strip().lower()
    mapping = {
        "startswith": "startsWith",
        "endswith": "endsWith",
        "contains": "contains",
        "exact": "exact",
    }
    if key not in mapping:
        raise ValueError(
            f"barcode_filter must be one of {sorted(mapping)}, got {value!r}"
        )
    return mapping[key]


def _normalize_split_barcode_page(value: str) -> str:
    key = value.strip().lower()
    if key not in ("before", "after"):
        raise ValueError("split_qr_page must be 'before' or 'after'")
    return key


@tool(
    name="split_pdf_by_swiss_qr",
    description=(
        "Split a PDF by Swiss QR via PDF4me  SplitPdfByBarcode (/api/v2/SplitPdfByBarcode) "
        "(SplitDocBySwissQrCode). pdf_file_path, barcode_string (default SPC), "
        "barcode_filter, barcode_type, split_qr_page (before/after), pdf_render_dpi, "
        "combine_pages_with_same_barcodes (maps to combinePagesWithSameConsecutiveBarcodes)."
    ),
)
async def split_pdf_by_swiss_qr(
    pdf_file_path: str,
    split_qr_page: SplitBarcodePage = "after",
    pdf_render_dpi: PdfRenderDpi = "200",
    combine_pages_with_same_barcodes: bool = False,
    barcode_filter: BarcodeFilter = "contains",
    barcode_type: BarcodeType = "qrcode",
    barcode_string: str = "SPC",
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

    try:
        split_page = _normalize_split_barcode_page(split_qr_page)
        filter_norm = _normalize_barcode_filter(barcode_filter)
    except ValueError as exc:
        return ToolResult(content=str(exc))

    payload: dict[str, Any] = {
        "docName": doc_name,
        "docContent": pdf_b64,
        "barcodeString": barcode_string,
        "barcodeFilter": filter_norm,
        "barcodeType": barcode_type,
        "splitBarcodePage": split_page,
        "combinePagesWithSameConsecutiveBarcodes": combine_pages_with_same_barcodes,
        "pdfRenderDpi": pdf_render_dpi,
        "isAsync": True,
    }

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        final_resp = await _call_split_pdf_by_swiss_qr_api(payload, pdf4me_api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(content="Authentication failed: the API key is invalid or missing.")
        return ToolResult(content=f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    parsed = _json_obj_from_response(final_resp)
    if parsed is not None:
        raw_json_path = os.path.join(
            resolved_out, "split_pdf_by_swiss_qr_response.json")
        try:
            with open(raw_json_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            return ToolResult(content=f"Failed to write raw split response JSON: {exc}")

        items = _extract_split_items(parsed)
        if not items:
            return ToolResult(
                content=(
                    f"SplitPdfBySwissQR response saved to {raw_json_path}, but no split documents were found "
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
            content=(
                f"Split by Swiss QR completed successfully. Saved {len(output_paths)} PDF file(s) "
                f"to {resolved_out}"
            ),
            structured_content={
                "output_directory": resolved_out,
                "raw_json_path": raw_json_path,
                "split_count": len(output_paths),
                "output_paths": output_paths,
                "split_barcode_page": split_page,
                "pdf_render_dpi": pdf_render_dpi,
            },
        )

    # Binary/zip response path
    zip_path: Optional[str] = None
    extracted_names: list[str] = []
    try:
        zip_path = write_file_from_bytes(
            final_resp.content, resolved_out, "split_pdf_by_swiss_qr.zip")
    except OSError as exc:
        return ToolResult(content=f"Failed to write binary split output: {exc}")

    if len(final_resp.content) >= 4 and final_resp.content[:2] == b"PK":
        try:
            with zipfile.ZipFile(BytesIO(final_resp.content), "r") as zf:
                zf.extractall(resolved_out)
                extracted_names = zf.namelist()
        except zipfile.BadZipFile:
            extracted_names = []

    return ToolResult(
        content=(
            f"Split by Swiss QR returned binary output; saved to {zip_path}. "
            f"Extracted {len(extracted_names)} file(s)."
            if extracted_names
            else f"Split by Swiss QR returned binary output; saved to {zip_path}."
        ),
        structured_content={
            "output_directory": resolved_out,
            "zip_path": zip_path,
            "zip_member_names": extracted_names,
            "split_barcode_page": split_page,
            "pdf_render_dpi": pdf_render_dpi,
        },
    )
