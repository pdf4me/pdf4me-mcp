import asyncio
import csv
import json
import os
from pathlib import Path
from typing import Any, Optional, Union

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


def _parse_json_body(resp: httpx.Response) -> Optional[Union[dict[str, Any], list[Any]]]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith((b"{", b"[")):
        return None
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if isinstance(parsed, (dict, list)):
        return parsed
    raise ValueError("Expected JSON object or array from ExtractTableFromPdf")


async def _poll_extract_table_job(
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
        f"ExtractTableFromPdf did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_extract_table_from_pdf_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> httpx.Response:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ExtractTableFromPdf"
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
            return await _poll_extract_table_job(
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
    return str(p.parent / f"extracted_tables_{p.stem}")


def _write_csv_for_rows(table_rows: Any, csv_path: str) -> bool:
    if not isinstance(table_rows, list) or not table_rows:
        return False
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            if isinstance(table_rows[0], list):
                writer = csv.writer(csvfile)
                for row in table_rows:
                    if isinstance(row, (list, tuple)):
                        writer.writerow(list(row))
                    else:
                        writer.writerow([row])
                return True
            if isinstance(table_rows[0], dict):
                fieldnames = list(table_rows[0].keys())
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for row in table_rows:
                    if isinstance(row, dict):
                        writer.writerow(row)
                return True
    except (OSError, IndexError, TypeError, KeyError):
        return False
    return False


def _rows_from_table_item(table: Any) -> Optional[list[Any]]:
    if isinstance(table, dict):
        rows = table.get("rows") or table.get("Rows")
        if isinstance(rows, list):
            return rows
        return None
    if isinstance(table, list):
        return table
    return None


def _normalize_tables(table_data: Union[dict[str, Any], list[Any]]) -> list[Any]:
    """Return a list of table objects (each dict with rows, or a list-of-rows for one table)."""
    if isinstance(table_data, dict):
        raw = table_data.get("tables") or table_data.get("Tables")
        return list(raw) if isinstance(raw, list) else []
    if isinstance(table_data, list):
        if not table_data:
            return []
        # Top-level list of row arrays → one table (matches official sample)
        if isinstance(table_data[0], list):
            return [table_data]
        return table_data
    return []


@tool(
    name="extract_table_from_pdf",
    description=(
        "Extract tables from a PDF via PDF4me ExtractTableFromPdf (/api/v2/ExtractTableFromPdf). "
        "pdf_file_path; optional output_dir (defaults next to PDF). "
        "Saves extracted_tables.json, per-table table_N.json, and table_N.csv when rows are available."
    ),
)
async def extract_table_from_pdf(
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

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        final_resp = await _call_extract_table_from_pdf_api(payload, pdf4me_api_key)
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

    parsed = _parse_json_body(final_resp)
    if parsed is None:
        ext = ".bin"
        ct = (final_resp.headers.get("content-type") or "").lower()
        if "csv" in ct:
            ext = ".csv"
        elif "excel" in ct or "spreadsheet" in ct:
            ext = ".xlsx"
        try:
            bin_path = write_file_from_bytes(
                final_resp.content, resolved_out, f"extracted_tables{ext}"
            )
        except OSError as exc:
            return ToolResult(content=f"Failed to write binary response: {exc}")
        return ToolResult(
            content=f"Table extraction returned non-JSON data; saved to {bin_path}",
            structured_content={
                "output_directory": resolved_out,
                "binary_path": bin_path,
                "table_count": 0,
            },
        )

    json_path = os.path.join(resolved_out, "extracted_tables.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    tables = _normalize_tables(parsed)
    table_count = len(tables)
    table_json_paths: list[str] = []
    csv_paths: list[str] = []

    for i, table in enumerate(tables):
        tpath = os.path.join(resolved_out, f"table_{i + 1}.json")
        try:
            with open(tpath, "w", encoding="utf-8") as f:
                json.dump(table, f, indent=2, ensure_ascii=False)
            table_json_paths.append(tpath)
        except OSError:
            continue

        rows = _rows_from_table_item(table)
        if rows:
            cpath = os.path.join(resolved_out, f"table_{i + 1}.csv")
            if _write_csv_for_rows(rows, cpath):
                csv_paths.append(cpath)

    summary = (
        f"Extracted {table_count} table(s) under {resolved_out}. "
        f"Full payload: {json_path}"
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "table_count": table_count,
            "table_json_paths": table_json_paths,
            "csv_paths": csv_paths,
        },
    )
