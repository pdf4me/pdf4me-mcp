import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0

_ALLOWED_INPUT_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
_DEFAULT_DOC_NAME = "cheque.pdf"


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ProcessBankCheque, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ProcessBankCheque")
    return parsed


def _effective_cheque_dict(result: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "processBankChequeModel",
        "ProcessBankChequeModel",
        "bankChequeData",
        "BankChequeData",
        "chequeData",
        "ChequeData",
    ):
        inner = result.get(key)
        if isinstance(inner, dict):
            return inner
    return result


def _slug_from_doc_name(doc_name: str) -> str:
    stem = Path(doc_name).stem or "cheque"
    slug = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return slug or "cheque"


def _default_output_dir_for_file(cheque_file_path: str) -> str:
    p = Path(cheque_file_path).resolve()
    return str(p.parent / f"process_bank_cheque_{p.stem}")


def _default_output_dir_for_doc_name(doc_name: str) -> str:
    return str(Path.cwd() / f"process_bank_cheque_{_slug_from_doc_name(doc_name)}")


async def _poll_process_bank_cheque_job(
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
        f"ProcessBankCheque did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_process_bank_cheque_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ProcessBankCheque"
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
            final = await _poll_process_bank_cheque_job(
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
    name="process_bank_cheque",
    title="AI-Process Bank Cheque",
    description=(
        "AI-Process Bank Cheque: extract structured data from a bank cheque via PDF4me "
        "POST /api/v2/ProcessBankCheque (isAsync true: 202 + Location poll until JSON result). "
        "Request body uses isAsync (camelCase) and CustomFieldKeys (PascalCase) when custom keys are sent—"
        "not the IsAsync/customFieldKeys shape used by AI-Invoice Parser. "
        "Provide exactly one of: pdf_file_path (local .pdf/.png/.jpg/.jpeg as Base64) "
        "or doc_content (Base64, blob id, or URL per your PDF4me setup). "
        "doc_name: logical file name (e.g. cheque.pdf); optional with pdf_file_path (defaults to basename), "
        f"defaults to {_DEFAULT_DOC_NAME!r} when using doc_content if omitted. "
        "Optional custom_field_keys: include only as a non-empty list (property omitted when unused). "
        "Saves the full API JSON to process_bank_cheque.json."
    ),
)
async def process_bank_cheque(
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
                "Provide exactly one of pdf_file_path (local cheque image/PDF) or "
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
            return ToolResult(content=f"Could not read cheque file: {exc}")
        ext_lower = ext.lower()
        if ext_lower not in _ALLOWED_INPUT_EXTENSIONS:
            return ToolResult(
                content=(
                    f"Unsupported file type '{ext}'. "
                    f"Use one of: {', '.join(sorted(_ALLOWED_INPUT_EXTENSIONS))}."
                )
            )
        content_for_api = encoded
        resolved_doc_name = (doc_name or "").strip() or os.path.basename(path)
        resolved_out = output_dir if output_dir else _default_output_dir_for_file(path)
    else:
        content_for_api = str(doc_content).strip()
        resolved_doc_name = (doc_name or "").strip() or _DEFAULT_DOC_NAME
        resolved_out = output_dir if output_dir else _default_output_dir_for_doc_name(resolved_doc_name)

    _suffixes = tuple(_ALLOWED_INPUT_EXTENSIONS)
    if not resolved_doc_name.lower().endswith(_suffixes):
        if "." not in resolved_doc_name.lower():
            resolved_doc_name = f"{resolved_doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": content_for_api,
        "docName": resolved_doc_name,
        "isAsync": True,
    }
    if custom_field_keys:
        keys = [k for k in custom_field_keys if isinstance(k, str) and k.strip()]
        if keys:
            payload["CustomFieldKeys"] = keys

    os.makedirs(resolved_out, exist_ok=True)

    try:
        result = await _call_process_bank_cheque_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "process_bank_cheque.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    data = _effective_cheque_dict(result)
    success = data.get("success") if "success" in data else data.get("Success")
    message = data.get("message") if "message" in data else data.get("Message")

    summary = (
        f"ProcessBankCheque result saved to {json_path}. "
        f"success={success!r}, message={message!r}."
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "success": success,
            "message": message,
            "cheque": data,
        },
    )
