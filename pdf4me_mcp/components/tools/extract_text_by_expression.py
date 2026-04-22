import asyncio
import csv
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ExtractTextByExpression, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ExtractTextByExpression")
    return parsed


def _coerce_text_list(data: dict[str, Any]) -> list[str]:
    for key in ("textList", "TextList", "texts", "Texts", "matches", "Matches", "results", "Results"):
        raw = data.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return [str(raw)]
    return []


async def _poll_extract_text_by_expression_job(
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
        f"ExtractTextByExpression did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_extract_text_by_expression_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ExtractTextByExpression"
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
            final = await _poll_extract_text_by_expression_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return _json_dict_from_response(final)
        resp.raise_for_status()
        return _json_dict_from_response(resp)


def _default_output_dir(pdf_file_path: str) -> str:
    p = Path(pdf_file_path).resolve()
    return str(p.parent / f"extracted_text_by_expression_{p.stem}")


@tool(
    name="extract_text_by_expression",
    description=(
        "Extract text from a PDF matching a regex via PDF4me ExtractTextByExpression (/api/v2/ExtractTextByExpression). "
        "pdf_file_path, expression, page_sequence (e.g. '1-' all pages, '1-3', '1,2,3'); optional output_dir. "
        "Saves JSON, a matches text file, and CSV."
    ),
)
async def extract_text_by_expression(
    pdf_file_path: str,
    expression: str,
    page_sequence: str = "1-",
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
        "expression": expression,
        "pageSequence": page_sequence,
        "isAsync": True,
    }

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        result_data = await _call_extract_text_by_expression_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "extracted_text_by_expression.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result_data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    matches = _coerce_text_list(result_data)

    txt_path = os.path.join(resolved_out, "extracted_matches.txt")
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(
                f"Expression: {expression}\nPages: {page_sequence}\nMatches: {len(matches)}\n\n")
            for i, m in enumerate(matches, 1):
                f.write(f"Match {i}: {m}\n")
    except OSError as exc:
        return ToolResult(content=f"Failed to write matches text file: {exc}")

    csv_path = os.path.join(resolved_out, "extracted_matches.csv")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["match_number", "text", "expression", "page_sequence"])
            for i, m in enumerate(matches, 1):
                w.writerow([i, m, expression, page_sequence])
    except OSError as exc:
        return ToolResult(content=f"Failed to write CSV: {exc}")

    preview_lines = "\n".join(matches[:5])
    if len(matches) > 5:
        preview_lines += f"\n… and {len(matches) - 5} more (see {txt_path})"

    summary = (
        f"Found {len(matches)} match(es) for expression on pages {page_sequence!r}. "
        f"Results under {resolved_out}"
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "matches_text_path": txt_path,
            "matches_csv_path": csv_path,
            "match_count": len(matches),
            "expression": expression,
            "page_sequence": page_sequence,
            "preview": preview_lines,
        },
    )
