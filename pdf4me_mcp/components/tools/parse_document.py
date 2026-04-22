import asyncio
import json
import os
import uuid
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
            f"Expected JSON object from ParseDocument, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ParseDocument")
    return parsed


async def _poll_parse_document_job(
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
        f"ParseDocument did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_parse_document_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ParseDocument"
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
            final = await _poll_parse_document_job(
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
    return str(p.parent / f"parsed_document_{p.stem}")


@tool(
    name="parse_document",
    description=(
        "Parse a PDF with a PDF4me parse template via ParseDocument (/api/v2/ParseDocument). "
        "Requires pdf_file_path, doc_name (or uses PDF basename), parse_id (or auto-generated UUID), "
        "and either template_id (GUID) or template_name. Saves parsed_document.json under output_dir."
    ),
)
async def parse_document(
    pdf_file_path: str,
    parse_id: Optional[str] = None,
    template_id: Optional[str] = None,
    template_name: Optional[str] = None,
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    if not template_id and not template_name:
        return ToolResult(
            content="Provide either template_id (GUID) or template_name from the PDF4me dashboard."
        )

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

    op_parse_id = parse_id or str(uuid.uuid4())

    payload: dict[str, Any] = {
        "docName": doc_name,
        "docContent": pdf_b64,
        "ParseId": op_parse_id,
        "isAsync": True,
    }
    if template_id:
        payload["TemplateId"] = template_id
    else:
        payload["TemplateName"] = template_name

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)
    os.makedirs(resolved_out, exist_ok=True)

    try:
        result = await _call_parse_document_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "parsed_document.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    parsed_data = result.get("parsedData") or result.get("ParsedData")
    doc_type = result.get("documentType") or result.get("DocumentType")
    page_count = result.get(
        "pageCount") if "pageCount" in result else result.get("PageCount")
    confidence = result.get(
        "confidence") if "confidence" in result else result.get("Confidence")

    summary = (
        f"Parse complete; results saved to {json_path}. "
        f"documentType={doc_type!r}, pageCount={page_count!r}, confidence={confidence!r}."
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "parse_id": op_parse_id,
            "document_type": doc_type,
            "page_count": page_count,
            "confidence": confidence,
            "parsed_data": parsed_data if isinstance(parsed_data, (dict, list)) else None,
        },
    )
