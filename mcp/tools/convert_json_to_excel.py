import asyncio
import base64
import json
import os
from typing import Any, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _xlsx_output_filename(doc_name: str) -> str:
    if doc_name.lower().endswith(".xlsx"):
        return doc_name
    if doc_name.lower().endswith(".json"):
        return doc_name[:-5] + ".xlsx"
    return f"{doc_name}.xlsx"


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    """Read base64 from Document.DocData / document.docData (ASP.NET contract)."""
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


def _bytes_from_xlsx_response(resp: httpx.Response) -> bytes:
    """Binary XLSX from Content-Type or body; otherwise JSON with Document.DocData base64."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "spreadsheetml" in ct or "application/octet-stream" in ct:
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
            raise ValueError("Response JSON has no Document.DocData / docData base64 field")
        return base64.b64decode(b64)

    raise ValueError(
        f"Expected Excel binary or JSON with DocData, got content-type {ct!r}"
    )


async def _call_convert_json_to_excel_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    worksheet_name: str,
    is_title_wrap_text: bool,
    is_title_bold: bool,
    convert_number_and_date: bool,
    number_format: str,
    date_format: str,
    ignore_null_values: bool,
    first_row: int,
    first_column: int,
    use_async: bool,
) -> bytes:
    """POST ConvertJsonToExcel; return raw XLSX bytes (200 or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "worksheetName": worksheet_name,
        "isTitleWrapText": is_title_wrap_text,
        "isTitleBold": is_title_bold,
        "convertNumberAndDate": convert_number_and_date,
        "numberFormat": number_format,
        "dateFormat": date_format,
        "ignoreNullValues": ignore_null_values,
        "firstRow": first_row,
        "firstColumn": first_column,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertJsonToExcel",
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
            return await _poll_convert_json_to_excel_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_xlsx_response(resp)


async def _poll_convert_json_to_excel_job(
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
            return _bytes_from_xlsx_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"JSON to Excel did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="convert_json_to_excel",
    description=(
        "Convert a local JSON file to Excel (XLSX) using the PDF4me ConvertJsonToExcel API. "
        "Provide the file path to UTF-8 JSON. "
        "Options: worksheet_name, title formatting, number/date conversion and formats, "
        "ignore_null_values, first_row/first_column (1-based), use_async, and optional output path. "
        "When use_async is true, the API may return 202 and the tool polls until the XLSX is ready."
    ),
)
async def convert_json_to_excel_http(
    file_path: str,
    worksheet_name: str = "Sheet1",
    is_title_wrap_text: bool = True,
    is_title_bold: bool = True,
    convert_number_and_date: bool = False,
    number_format: str = "11",
    date_format: str = "01/01/2025",
    ignore_null_values: bool = False,
    first_row: int = 1,
    first_column: int = 1,
    use_async: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert JSON to Excel via PDF4me ConvertJsonToExcel.

    Args:
        file_path: Local path to the JSON file (.json).
        worksheet_name: Target worksheet name.
        is_title_wrap_text: Wrap text in title row when supported.
        is_title_bold: Bold title row when supported.
        convert_number_and_date: Enable number/date cell conversion.
        number_format: Number format string for the API.
        date_format: Date format string for the API.
        ignore_null_values: Skip null values when building the sheet.
        first_row: First data row (1-based).
        first_column: First data column (1-based).
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the XLSX. Defaults to the same directory as the input file.
        output_file_name: Name for the output file. Defaults from the input name (e.g. data.json → data.xlsx).
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".json":
        return ToolResult(
            content=f"Input file must be JSON (.json), got '{extension}' instead."
        )

    basename = os.path.basename(file_path)
    doc_name, _ = os.path.splitext(basename)
    if not doc_name:
        doc_name = "output"

    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = (
        output_file_name if output_file_name else _xlsx_output_filename(doc_name)
    )

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        xlsx_bytes = await _call_convert_json_to_excel_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            worksheet_name=worksheet_name,
            is_title_wrap_text=is_title_wrap_text,
            is_title_bold=is_title_bold,
            convert_number_and_date=convert_number_and_date,
            number_format=number_format,
            date_format=date_format,
            ignore_null_values=ignore_null_values,
            first_row=first_row,
            first_column=first_column,
            use_async=use_async,
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

    if not xlsx_bytes or not xlsx_bytes.startswith(b"PK") or len(xlsx_bytes) < 100:
        return ToolResult(
            content="Unexpected API response — Excel file missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            xlsx_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"JSON converted to Excel successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
