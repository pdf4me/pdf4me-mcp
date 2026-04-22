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

_ALLOWED_INPUT_EXTENSIONS = frozenset({".pdf"})
_DEFAULT_DOC_NAME = "mortgage.pdf"
_PDF_BASE64_PREFIX = "JVBERi0x"


def _strip_data_url_prefix(content: str) -> str:
    """If doc_content is a data URL, return only the part after the first comma."""
    s = content.strip()
    if s.lower().startswith("data:") and "," in s:
        return s.split(",", 1)[1].strip()
    return s


def _mortgage_doc_content_pdf_prefix_error(after_data_url: str) -> Optional[str]:
    """Require PDF base64 prefix for long base64 payloads; skip for URLs and short/blob-like values."""
    s = after_data_url.strip()
    if s.lower().startswith(("http://", "https://")):
        return None
    if len(s) < 48:
        return None
    normalized = re.sub(r"\s+", "", s)
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", normalized):
        return None
    if not normalized.startswith(_PDF_BASE64_PREFIX):
        return (
            "For PDF base64 doc_content, after stripping any data: URL prefix the payload "
            f"must start with {_PDF_BASE64_PREFIX!r} (standard PDF base64 prefix)."
        )
    return None


def _file_starts_with_pdf_magic(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(5).startswith(b"%PDF")


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ProcessMortgageDocument, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ProcessMortgageDocument")
    return parsed


def _effective_mortgage_dict(result: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "processMortgageDocumentModel",
        "ProcessMortgageDocumentModel",
        "mortgageDocumentData",
        "MortgageDocumentData",
    ):
        inner = result.get(key)
        if isinstance(inner, dict):
            return inner
    return result


def _slug_from_doc_name(doc_name: str) -> str:
    stem = Path(doc_name).stem or "mortgage"
    slug = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return slug or "mortgage"


def _default_output_dir_for_file(file_path: str) -> str:
    p = Path(file_path).resolve()
    return str(p.parent / f"process_mortgage_document_{p.stem}")


def _default_output_dir_for_doc_name(doc_name: str) -> str:
    return str(Path.cwd() / f"process_mortgage_document_{_slug_from_doc_name(doc_name)}")


async def _poll_process_mortgage_document_job(
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
        f"ProcessMortgageDocument did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_process_mortgage_document_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ProcessMortgageDocument"
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
            final = await _poll_process_mortgage_document_job(
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
    name="process_mortgage_document",
    title="AI-Process Mortgage Document",
    description=(
        "AI-Process Mortgage Document: extract structured data via PDF4me "
        "POST /api/v2/ProcessMortgageDocument. Uses camelCase isAsync (true), not IsAsync. "
        "Body: docContent, docName, isAsync; optional documentType (hint, e.g. loan—omitted when empty); "
        "CustomFieldKeys (PascalCase) only when custom_field_keys is non-empty. "
        "Exactly one of pdf_file_path (local .pdf only, must be a PDF file) or "
        "doc_content (PDF base64, blob id, or URL). For base64 after stripping a data: URL prefix, "
        f"long base64 payloads must start with {_PDF_BASE64_PREFIX!r}. "
        "doc_name optional (basename or mortgage.pdf); 202 + Location poll; saves process_mortgage_document.json."
    ),
)
async def process_mortgage_document(
    pdf_file_path: Optional[str] = None,
    doc_name: Optional[str] = None,
    document_type: Optional[str] = None,
    custom_field_keys: Optional[list[str]] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    has_path = bool(pdf_file_path and str(pdf_file_path).strip())
    has_content = False

    if not has_path:
        return ToolResult(
            content=(
                "Provide exactly one of pdf_file_path (local PDF file) or "
                "doc_content (PDF base64, blob id, or URL per your integration)."
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
            return ToolResult(content=f"Could not read file: {exc}")
        ext_lower = ext.lower()
        if ext_lower not in _ALLOWED_INPUT_EXTENSIONS:
            return ToolResult(
                content=(
                    f"This tool expects a PDF file; got extension '{ext}'. "
                    f"Use a path ending in {', '.join(sorted(_ALLOWED_INPUT_EXTENSIONS))}."
                )
            )
        if not _file_starts_with_pdf_magic(path):
            return ToolResult(
                content="Local file does not look like a PDF (missing %PDF- header)."
            )
        content_for_api = encoded
        resolved_doc_name = (doc_name or "").strip() or os.path.basename(path)
        resolved_out = output_dir if output_dir else _default_output_dir_for_file(
            path)
    else:
        raw_content = str(doc_content).strip()
        after_url = _strip_data_url_prefix(raw_content)
        prefix_err = _mortgage_doc_content_pdf_prefix_error(after_url)
        if prefix_err:
            return ToolResult(content=prefix_err)
        content_for_api = after_url.strip()
        resolved_doc_name = (doc_name or "").strip() or _DEFAULT_DOC_NAME
        resolved_out = output_dir if output_dir else _default_output_dir_for_doc_name(
            resolved_doc_name)

    _suffixes = tuple(_ALLOWED_INPUT_EXTENSIONS)
    if not resolved_doc_name.lower().endswith(_suffixes):
        if "." not in resolved_doc_name.lower():
            resolved_doc_name = f"{resolved_doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": content_for_api,
        "docName": resolved_doc_name,
        "isAsync": True,
    }
    if document_type and str(document_type).strip():
        payload["documentType"] = str(document_type).strip()
    if custom_field_keys:
        keys = [k for k in custom_field_keys if isinstance(
            k, str) and k.strip()]
        if keys:
            payload["CustomFieldKeys"] = keys

    os.makedirs(resolved_out, exist_ok=True)

    try:
        result = await _call_process_mortgage_document_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "process_mortgage_document.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    data = _effective_mortgage_dict(result)
    success = data.get("success") if "success" in data else data.get("Success")
    message = data.get("message") if "message" in data else data.get("Message")

    summary = (
        f"ProcessMortgageDocument result saved to {json_path}. "
        f"success={success!r}, message={message!r}."
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "success": success,
            "message": message,
            "mortgage_document": data,
        },
    )
