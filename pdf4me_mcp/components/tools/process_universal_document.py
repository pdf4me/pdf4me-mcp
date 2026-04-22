import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0

_ALLOWED_INPUT_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
_DEFAULT_DOC_NAME = "document.pdf"


def _strip_data_url_prefix(content: str) -> str:
    """If doc_content is a data URL, return only the part after the first comma."""
    s = content.strip()
    if s.lower().startswith("data:") and "," in s:
        return s.split(",", 1)[1].strip()
    return s


def _sanitize_profiles(profiles: Optional[str]) -> Optional[str]:
    """Trim; omit empty. If non-empty and not already JSON-like with { or [, wrap in braces."""
    if profiles is None:
        return None
    s = str(profiles).strip()
    if not s:
        return None
    if s.startswith("{") or s.startswith("["):
        return s
    return "{" + s + "}"


def _merged_field_names(
    fields_list: Optional[list[str]],
    fields_csv: Optional[str],
) -> list[str]:
    raw: list[str] = []
    if fields_list:
        raw.extend(f.strip()
                   for f in fields_list if isinstance(f, str) and f.strip())
    if fields_csv and str(fields_csv).strip():
        raw.extend(
            p.strip()
            for p in str(fields_csv).split(",")
            if isinstance(p, str) and p.strip()
        )
    seen: set[str] = set()
    out: list[str] = []
    for name in raw:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _input_doc_name_universal(
    user_doc_name: Optional[str],
    has_path: bool,
    path: Optional[str],
    content_for_api: str,
) -> str:
    """inputDocName: binary basename or user; URL segment or user; base64 user doc name."""
    user = (user_doc_name or "").strip()
    if has_path and path:
        base = os.path.basename(path).strip()
        return base or user
    u = content_for_api.strip()
    if u.lower().startswith(("http://", "https://")):
        seg = unquote(os.path.basename(urlparse(u).path.rstrip("/"))).strip()
        return seg or user
    return user


def _resolve_universal_doc_name(
    user_doc_name: Optional[str],
    has_path: bool,
    path: Optional[str],
    content_for_api: str,
) -> str:
    """docName = trimmed inputDocName if set, else trimmed user docName, else document.pdf."""
    inner = _input_doc_name_universal(
        user_doc_name, has_path=has_path, path=path, content_for_api=content_for_api
    ).strip()
    user = (user_doc_name or "").strip()
    if inner:
        return inner
    if user:
        return user
    return _DEFAULT_DOC_NAME


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ProcessUniversalDocument, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ProcessUniversalDocument")
    return parsed


def _effective_universal_dict(result: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "processUniversalDocumentModel",
        "ProcessUniversalDocumentModel",
        "universalDocumentData",
        "UniversalDocumentData",
        "extractedData",
        "ExtractedData",
    ):
        inner = result.get(key)
        if isinstance(inner, dict):
            return inner
    return result


def _slug_from_doc_name(doc_name: str) -> str:
    stem = Path(doc_name).stem or "document"
    slug = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return slug or "document"


def _default_output_dir_for_file(file_path: str) -> str:
    p = Path(file_path).resolve()
    return str(p.parent / f"process_universal_document_{p.stem}")


def _default_output_dir_for_doc_name(doc_name: str) -> str:
    return str(Path.cwd() / f"process_universal_document_{_slug_from_doc_name(doc_name)}")


async def _poll_process_universal_document_job(
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
        f"ProcessUniversalDocument did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_process_universal_document_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ProcessUniversalDocument"
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
            final = await _poll_process_universal_document_job(
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
    name="process_universal_document",
    title="AI - Universal Document Data Extraction",
    description=(
        "AI - Universal Document Data Extraction (processUniversalDocument): extract named fields via PDF4me "
        "POST /api/v2/ProcessUniversalDocument. docName = trimmed inputDocName if set, else doc_name, else "
        f"{_DEFAULT_DOC_NAME!r}. inputDocName: file basename, or URL last path segment (decoded) or doc_name, "
        "or base64 uses doc_name. fields (required): at least one field name—use fields and/or fields_csv "
        "(comma-separated, trimmed). mode: 0 Standard (default) or 1 Strict. isAsync true. "
        "Optional documentType (omit if empty); optional profiles (sanitized). "
        "Exactly one of pdf_file_path or doc_content (base64, blob id, or URL). Data URL: strip prefix before comma. "
        "202 + Location poll; saves process_universal_document.json."
    ),
)
async def process_universal_document(
    pdf_file_path: Optional[str] = None,
    doc_name: Optional[str] = None,
    fields: Optional[list[str]] = None,
    fields_csv: Optional[str] = None,
    mode: int = 0,
    document_type: Optional[str] = None,
    profiles: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    has_path = bool(pdf_file_path and str(pdf_file_path).strip())
    has_content = False

    if not has_path:
        return ToolResult(
            content=(
                "Provide exactly one of pdf_file_path (local document file) or "
                "doc_content (Base64, data URL, blob id, or URL per your integration)."
            )
        )

    merged_fields = _merged_field_names(fields, fields_csv)
    if not merged_fields:
        return ToolResult(
            content=(
                "fields is required: provide at least one non-empty field name in "
                "fields (list) and/or fields_csv (comma-separated)."
            )
        )

    if mode not in (0, 1):
        return ToolResult(content="mode must be 0 (Standard) or 1 (Strict).")

    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    path: Optional[str] = None
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
                    f"Unsupported file type '{ext}'. "
                    f"Use one of: {', '.join(sorted(_ALLOWED_INPUT_EXTENSIONS))}."
                )
            )
        content_for_api = encoded
    else:
        raw_content = str(doc_content).strip()
        content_for_api = _strip_data_url_prefix(raw_content)

    resolved_doc_name = _resolve_universal_doc_name(
        doc_name, has_path=has_path, path=path, content_for_api=content_for_api
    )

    _suffixes = tuple(_ALLOWED_INPUT_EXTENSIONS)
    if not resolved_doc_name.lower().endswith(_suffixes):
        if "." not in resolved_doc_name.lower():
            resolved_doc_name = f"{resolved_doc_name}.pdf"

    if has_path and path:
        resolved_out = output_dir if output_dir else _default_output_dir_for_file(
            path)
    else:
        resolved_out = output_dir if output_dir else _default_output_dir_for_doc_name(
            resolved_doc_name)

    payload: dict[str, Any] = {
        "docName": resolved_doc_name,
        "docContent": content_for_api,
        "fields": merged_fields,
        "mode": mode,
        "isAsync": True,
    }
    dt = (document_type or "").strip()
    if dt:
        payload["documentType"] = dt
    sanitized_profiles = _sanitize_profiles(profiles)
    if sanitized_profiles is not None:
        payload["profiles"] = sanitized_profiles

    os.makedirs(resolved_out, exist_ok=True)

    try:
        result = await _call_process_universal_document_api(payload, pdf4me_api_key)
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

    json_path = os.path.join(resolved_out, "process_universal_document.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON: {exc}")

    data = _effective_universal_dict(result)
    success = data.get("success") if "success" in data else data.get("Success")
    message = data.get("message") if "message" in data else data.get("Message")

    summary = (
        f"ProcessUniversalDocument result saved to {json_path}. "
        f"success={success!r}, message={message!r}."
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "success": success,
            "message": message,
            "universal_document": data,
        },
    )
