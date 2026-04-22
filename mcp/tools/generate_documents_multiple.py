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

_TemplateFileType = Literal["Docx", "Html", "Pdf", "MailMerge", "GoogleDocs", "Word"]
_DocumentDataType = Literal["Json", "XML"]
_OutputType = Literal["PDF", "Docx", "Html", "Excel", "Word"]


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _bytes_list_from_generate_document_multiple_response(resp: httpx.Response) -> list[bytes]:
    """Decode PDF4me GenerateDocumentMultiple body: JSON outputDocuments[].streamFile or raw bytes."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in ct or "text/json" in ct:
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        return _bytes_list_from_multiple_json(payload)

    text_preview = raw[:512].decode("utf-8", errors="ignore").lstrip()
    if text_preview.startswith("{"):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            pass
        else:
            return _bytes_list_from_multiple_json(payload)

    if raw:
        return [raw]
    raise ValueError("Empty response from GenerateDocumentMultiple")


def _bytes_list_from_multiple_json(payload: Any) -> list[bytes]:
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object response for GenerateDocumentMultiple")
    docs = payload.get("outputDocuments")
    if not isinstance(docs, list) or not docs:
        raise ValueError("Response JSON has no non-empty outputDocuments array")
    out: list[bytes] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        b64 = doc.get("streamFile")
        if isinstance(b64, str) and b64:
            out.append(base64.b64decode(b64))
    if not out:
        raise ValueError("No streamFile entries found in outputDocuments")
    return out


async def _call_generate_document_multiple_api(
    payload: dict[str, Any],
    PDF4ME_API_KEY: str,
) -> list[bytes]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/GenerateDocumentMultiple",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_generate_document_multiple_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_list_from_generate_document_multiple_response(resp)


async def _poll_generate_document_multiple_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> list[bytes]:
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_list_from_generate_document_multiple_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "GenerateDocumentMultiple did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="generate_documents_multiple",
    description=(
        "Generate multiple documents from one template and data using PDF4me "
        "GenerateDocumentMultiple API (POST /api/v2/GenerateDocumentMultiple). "
        "Supports async polling on 202; saves each output from outputDocuments[].streamFile."
    ),
)
async def generate_documents_multiple_http(
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
    use_async: bool = True,
) -> ToolResult:
    """Generate multiple documents from a template plus JSON/XML data via PDF4me.

    Args:
        template_file_path: Local path to the template file.
        template_file_type: Template kind sent to the API (e.g. Docx, Html, Pdf, Word).
        template_file_name: File name for the template in the request.
        document_data_type: Json or XML.
        output_type: Desired output format (PDF, Docx, Html, Excel, Word).
        output_dir: Directory where generated files are written.
        output_file_name: Base output file name; multiple files use name_1.ext, name_2.ext, etc.
        document_data_text_path: Path to a UTF-8 file whose contents are sent as documentDataText.
        document_data_file_path: Local file read as base64 for documentDataFile.
        file_meta_data: Optional fileMetaData string.
        meta_data_json: Optional metaDataJson string.
        use_async: When True, request async processing and poll on HTTP 202.
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

    payload: dict[str, Any] = {
        "templateFileType": template_file_type.strip(),
        "templateFileName": template_file_name.strip(),
        "templateFileData": template_file_data,
        "documentDataType": document_data_type.strip(),
        "outputType": output_type.strip(),
        "async": use_async,
    }
    if document_data_text is not None:
        payload["documentDataText"] = document_data_text
    if document_data_file is not None:
        payload["documentDataFile"] = document_data_file
    if file_meta_data is not None:
        payload["fileMetaData"] = file_meta_data
    if meta_data_json is not None:
        payload["metaDataJson"] = meta_data_json

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(content="Authentication failed: no API key provided in the request.")

    try:
        output_parts = await _call_generate_document_multiple_api(payload, PDF4ME_API_KEY)
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

    if not output_parts:
        return ToolResult(content="Unexpected API response — no generated documents.")

    resolved_output_dir = output_dir.strip()
    base_name = output_file_name.strip()
    stem, ext = os.path.splitext(base_name)
    if not ext:
        ext = ""

    saved_paths: list[str] = []
    try:
        if len(output_parts) == 1:
            write_file_from_bytes(output_parts[0], resolved_output_dir, base_name)
            saved_paths.append(os.path.join(resolved_output_dir, base_name))
        else:
            for index, part in enumerate(output_parts, start=1):
                name = f"{stem}_{index}{ext}"
                write_file_from_bytes(part, resolved_output_dir, name)
                saved_paths.append(os.path.join(resolved_output_dir, name))
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file(s): {exc}")

    return ToolResult(
        content=f"Generated {len(saved_paths)} document(s). Saved: {', '.join(saved_paths)}",
        structured_content={"output_paths": saved_paths, "count": len(saved_paths)},
    )
