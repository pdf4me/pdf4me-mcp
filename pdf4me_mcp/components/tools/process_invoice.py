import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0

_ALLOWED_INPUT_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg"})


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ProcessInvoice, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ProcessInvoice")
    return parsed


def _effective_invoice_dict(result: dict[str, Any]) -> dict[str, Any]:
    """Prefer nested model payload if the API wraps the invoice fields."""
    for key in ("processInvoiceModel", "ProcessInvoiceModel", "invoiceData", "InvoiceData"):
        inner = result.get(key)
        if isinstance(inner, dict):
            return inner
    return result


def _slug_from_doc_name(doc_name: str) -> str:
    stem = Path(doc_name).stem or "invoice"
    slug = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return slug or "invoice"


def _default_output_dir_for_file(invoice_file_path: str) -> str:
    p = Path(invoice_file_path).resolve()
    return str(p.parent / f"process_invoice_{p.stem}")


def _default_output_dir_for_doc_name(doc_name: str) -> str:
    return str(Path.cwd() / f"process_invoice_{_slug_from_doc_name(doc_name)}")


async def _poll_process_invoice_job(
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
        f"ProcessInvoice did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_process_invoice_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ProcessInvoice"
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
            final = await _poll_process_invoice_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return _json_dict_from_response(final)
        resp.raise_for_status()
        return _json_dict_from_response(resp)


@tool(
    name="process_invoice",
    title="AI-Invoice Parser",
    description=(
        "AI-Invoice Parser: extract structured invoice data from a document via PDF4me "
        "POST /api/v2/ProcessInvoice (async: 202 + Location poll until JSON result). "
        "Provide exactly one of: pdf_file_path (local .pdf/.png/.jpg/.jpeg read as Base64) "
        "or doc_content (Base64 file content, upload blob id, or public URL string—per your PDF4me setup). "
        "doc_name: logical document name for the API (e.g. invoice.pdf); optional when using pdf_file_path "
        "(defaults to the file basename), required when using doc_content. "
        "Optional custom_field_keys: non-empty list of extra field names for the model to extract; "
        "omit the parameter when you have no custom keys (empty lists are not sent). "
        "Saves the full API JSON to process_invoice.json and returns key fields (invoiceNumber, vendorName, total, success, …)."
    ),
)
async def process_invoice(
    pdf_file_path: Optional[str] = None,
    doc_name: Optional[str] = None,
    custom_field_keys: Optional[list[str]] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    has_path = bool(pdf_file_path and str(pdf_file_path).strip())
    has_content = False

    if not has_path:
        return ToolResult(
            content=(
                "Provide exactly one of pdf_file_path (local invoice file) or "
                "doc_content (Base64, blob id, or URL per your integration)."
            )
        )

    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    if has_path:
        path = str(pdf_file_path).strip()
        try:
            encoded, ext = file_to_base64(path)
        except OSError as exc:
            return ToolResult(content=f"Could not read invoice file: {exc}")
        ext_lower = ext.lower()
        if ext_lower not in _ALLOWED_INPUT_EXTENSIONS:
            return ToolResult(
                content=(
                    f"Unsupported file type '{ext}'. "
                    f"Use one of: {', '.join(sorted(_ALLOWED_INPUT_EXTENSIONS))}."
                )
            )
        content_for_api = encoded
        base = (doc_name or "").strip() or os.path.basename(path)
        resolved_doc_name = base
        resolved_out = output_dir if output_dir else _default_output_dir_for_file(
            path)
    else:
        content_for_api = str(doc_content).strip()
        resolved_doc_name = (doc_name or "").strip()
        if not resolved_doc_name:
            return ToolResult(
                content=(
                    "doc_name is required when using doc_content (logical name for the invoice, "
                    "e.g. invoice.pdf)."
                )
            )
        resolved_out = output_dir if output_dir else _default_output_dir_for_doc_name(
            resolved_doc_name)

    _suffixes = tuple(_ALLOWED_INPUT_EXTENSIONS)
    if not resolved_doc_name.lower().endswith(_suffixes):
        if "." not in resolved_doc_name.lower():
            resolved_doc_name = f"{resolved_doc_name}.pdf"

    payload: dict[str, Any] = {
        "docName": resolved_doc_name,
        "docContent": content_for_api,
        "IsAsync": True,
    }
    if custom_field_keys:
        keys = [k for k in custom_field_keys if isinstance(
            k, str) and k.strip()]
        if keys:
            payload["customFieldKeys"] = keys

    os.makedirs(resolved_out, exist_ok=True)

    try:
        result = await _call_process_invoice_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "process_invoice.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    inv = _effective_invoice_dict(result)
    success = inv.get("success") if "success" in inv else inv.get("Success")
    message = inv.get("message") if "message" in inv else inv.get("Message")
    invoice_number = inv.get("invoiceNumber") or inv.get("InvoiceNumber")
    vendor_name = inv.get("vendorName") or inv.get("VendorName")
    total = inv.get("total") if "total" in inv else inv.get("Total")
    currency = inv.get("currency") or inv.get("Currency")

    summary = (
        f"ProcessInvoice result saved to {json_path}. "
        f"success={success!r}, message={message!r}, "
        f"invoiceNumber={invoice_number!r}, vendorName={vendor_name!r}, "
        f"total={total!r}, currency={currency!r}."
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "success": success,
            "message": message,
            "invoice_number": invoice_number,
            "vendor_name": vendor_name,
            "total": total,
            "currency": currency,
            "invoice": inv,
        },
    )
