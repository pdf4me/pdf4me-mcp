import asyncio
import base64
import json
import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_HTTP_TIMEOUT = httpx.Timeout(connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 20
_ASYNC_POLL_INTERVAL_SEC = 10.0

_TemplateFileType = Literal["Docx", "Html", "Pdf", "MailMerge", "GoogleDocs"]
_DocumentDataType = Literal["Json", "XML", "Text"]
_OutputType = Literal["PDF", "Docx", "Html"]


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    """Best-effort JSON base64 extraction for actions that return Document.DocData."""
    if depth > 12 or not isinstance(obj, dict):
        return None
    for key in ("docData", "DocData", "documentData", "DocumentData"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    for node in ("document", "Document", "outputDocument", "OutputDocument"):
        sub = obj.get(node)
        if isinstance(sub, dict):
            found = _docdata_b64_from_json(sub, depth=depth + 1)
            if found:
                return found
    return None


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """Prefer binary payload; fallback to JSON docData if API wraps output."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in ct or "text/json" in ct:
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object response for GenerateDocumentSingle")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no Document.DocData/docData base64 field")
        return base64.b64decode(b64)

    # Binary (PDF/Word/Excel/etc) or text/html are both returned as bytes.
    return raw


async def _call_generate_document_single_api(
    payload: dict[str, Any],
    PDF4ME_API_KEY: str,
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/GenerateDocumentSingle",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_generate_document_single_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_generate_document_single_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """Poll with initial delay, mirroring sample flow."""
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "GenerateDocumentSingle did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="generate_document_single",
    description=(
        "Generate a single document from a template using PDF4me GenerateDocumentSingle API "
        "(POST /api/v2/GenerateDocumentSingle). Supports template file, document data text/file, "
        "output type, and async polling."
    ),
)
async def generate_document_single_http(
    template_file_path: str,
    template_file_type: _TemplateFileType,
    template_file_name: str,
    document_data_type: _DocumentDataType,
    output_type: _OutputType,
    output_dir: str,
    output_file_name: str,
    document_data_text_path: Optional[str] = None,
    document_data_file_path: Optional[str] = None,
    file_meta_data: Optional[str] = None,
    meta_data_json: Optional[str] = None,
) -> ToolResult:
    """Generate one document from template + data via PDF4me.

    Args:
        template_file_path: Local path to template file (html/docx/pdf/etc).
        template_file_type: One of backend-supported values: Docx, Html, Pdf, MailMerge, GoogleDocs.
        template_file_name: Template file name sent to API.
        document_data_type: One of backend-supported values: Json, XML, or Text (plain text data).
        output_type: One of backend-supported values: PDF, Docx, Html.
        output_dir: Directory to save output (required).
        output_file_name: Output file name (required).
        document_data_text_path: Local path to text data (JSON/XML text) for documentDataText.
        document_data_file_path: Local data file path to send as base64 documentDataFile.
        file_meta_data: Optional fileMetaData string.
        meta_data_json: Optional metaDataJson string.
        use_async: When True, sends isasync true and polls on 202.
    """
    if not output_dir.strip():
        return ToolResult(content="output_dir is required. Please provide an output directory path.")
    if not output_file_name.strip():
        return ToolResult(content="output_file_name is required. Please provide an output file name.")
    if not template_file_name.strip():
        return ToolResult(content="template_file_name is required.")
    if not template_file_type.strip():
        return ToolResult(content="template_file_type is required.")
    if not document_data_type.strip():
        return ToolResult(content="document_data_type is required.")
    if not output_type.strip():
        return ToolResult(content="output_type is required.")
    if not document_data_text_path and not document_data_file_path:
        return ToolResult(
            content="Provide at least one of document_data_text_path or document_data_file_path."
        )

    try:
        template_file_data, _ = file_to_base64(template_file_path)
    except FileNotFoundError:
        return ToolResult(content=f"Template file not found: {template_file_path}")
    except OSError as exc:
        return ToolResult(content=f"Failed to read template file '{template_file_path}': {exc}")

    document_data_text: Optional[str] = None
    if document_data_text_path:
        try:
            document_data_text = _read_text_file(document_data_text_path)
        except FileNotFoundError:
            return ToolResult(content=f"Document data text file not found: {document_data_text_path}")
        except OSError as exc:
            return ToolResult(
                content=f"Failed to read document data text file '{document_data_text_path}': {exc}"
            )

    document_data_file: Optional[str] = None
    if document_data_file_path:
        try:
            document_data_file, _ = file_to_base64(document_data_file_path)
        except FileNotFoundError:
            return ToolResult(content=f"Document data file not found: {document_data_file_path}")
        except OSError as exc:
            return ToolResult(
                content=f"Failed to read document data file '{document_data_file_path}': {exc}"
            )

    # Enum strings match PDF4me samples (lowercase), e.g. templateFileType html, documentDataType text.
    mj = meta_data_json.strip() if meta_data_json is not None else ""
    payload: dict[str, Any] = {
        "templateFileType": template_file_type.strip().lower(),
        "templateFileName": template_file_name.strip(),
        "templateFileData": template_file_data,
        "documentDataType": document_data_type.strip().lower(),
        "outputType": output_type.strip().lower(),
        "metaDataJson": mj if mj else "{}",
        "isasync": use_async,
    }
    if document_data_text is not None:
        payload["documentDataText"] = document_data_text
    if document_data_file is not None:
        payload["documentDataFile"] = document_data_file
    if file_meta_data is not None:
        payload["fileMetaData"] = file_meta_data

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(content="Authentication failed: no API key provided in the request.")

    try:
        output_bytes = await _call_generate_document_single_api(payload, PDF4ME_API_KEY)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(content="Authentication failed: the API key is invalid or missing.")
        return ToolResult(content=f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.ReadTimeout as exc:
        return ToolResult(content=f"HTTP read timed out waiting for PDF4me (payload may be large): {exc}")
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    if not output_bytes:
        return ToolResult(content="Unexpected API response — generated output is empty.")

    resolved_output_dir = output_dir.strip()
    resolved_output_name = output_file_name.strip()
    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(output_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Document generated successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )

