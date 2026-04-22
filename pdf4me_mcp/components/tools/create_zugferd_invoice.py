import os
from typing import Any, Literal, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes

_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)

InputFormat = Literal["XML", "JSON", "CSV"]
OutputMode = Literal["XmlWithPdf", "XmlOnly"]
ConformanceLevel = Literal["BASIC", "COMFORT", "EXTENDED"]
ZugferdVersion = Literal["1.0"]


def _bytes_from_response(resp: httpx.Response) -> tuple[bytes, str]:
    """Accept binary PDF/XML output. Returns (bytes, suggested_extension)."""
    ct = (resp.headers.get("content-type") or "").lower()
    data = resp.content

    if "application/pdf" in ct:
        return data, ".pdf"
    if "application/xml" in ct or "text/xml" in ct:
        return data, ".xml"
    if "application/octet-stream" in ct:
        # If API gives octet-stream, infer from content signature.
        if data.startswith(b"%PDF"):
            return data, ".pdf"
        return data, ".xml"
    raise ValueError(
        f"Expected PDF/XML binary response, got content-type {ct!r}")


async def _call_create_zugferd_invoice_api(
    payload: dict[str, Any],
    PDF4ME_API_KEY: str,
) -> tuple[bytes, str]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/CreateZugferdInvoice",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        body, ext = _bytes_from_response(resp)
        return body, ext


@tool(
    name="create_zugferd_invoice",
    description=(
        "Create a ZUGFeRD e-invoice using PDF4me CreateZugferdInvoice API (POST /api/v2/CreateZugferdInvoice). "
        "Supports inputFormat XML/JSON/CSV, outputMode XmlWithPdf or XmlOnly, conformance level, and language. "
        "For XmlWithPdf, provide source PDF via source_pdf_path (docContent/document.Name are derived from the file). "
        "Saves output as PDF or XML and returns output metadata."
    ),
)
async def create_zugferd_invoice_http(
    input_format: InputFormat = "XML",
    output_mode: OutputMode = "XmlWithPdf",
    conformance_level: ConformanceLevel = "BASIC",
    zugferd_version: ZugferdVersion = "1.0",
    render_invoice_on_pdf: bool = True,
    invoice_xml_data: Optional[str] = None,
    invoice_json_data: Optional[str] = None,
    invoice_csv_data: Optional[str] = None,
    language: Optional[str] = None,
    source_pdf_path: Optional[str] = None,
    output_dir: str = "",
    output_file_name: str = "",
) -> ToolResult:
    """Create ZUGFeRD invoice via PDF4me.

    Args:
        input_format: XML, JSON, or CSV.
        output_mode: XmlWithPdf (PDF with embedded XML) or XmlOnly.
        conformance_level: BASIC, COMFORT, or EXTENDED.
        zugferd_version: ZUGFeRD version (currently only "1.0" per schema).
        render_invoice_on_pdf: Applies to XmlWithPdf output mode.
        invoice_xml_data: Local XML file path when input_format is XML (file is base64-encoded).
        invoice_json_data: Local JSON file path when input_format is JSON (file is base64-encoded).
        invoice_csv_data: Local CSV file path when input_format is CSV (file is base64-encoded).
        language: Optional localization language code (e.g. de, en, fr).
        source_pdf_path: Local source PDF path for XmlWithPdf (used to derive docContent and document.Name).
        output_dir: Directory to save the output file (required).
        output_file_name: Output file name (required).
    """
    selected_data_path: Optional[str] = None
    if input_format == "XML":
        selected_data_path = invoice_xml_data
    elif input_format == "JSON":
        selected_data_path = invoice_json_data
    elif input_format == "CSV":
        selected_data_path = invoice_csv_data

    if not selected_data_path or not selected_data_path.strip():
        return ToolResult(
            content=(
                "Missing invoice file path for selected input_format. "
                "Provide invoice_xml_data, invoice_json_data, or invoice_csv_data accordingly."
            )
        )
    try:
        selected_data_b64, _ = file_to_base64(selected_data_path.strip())
    except FileNotFoundError:
        return ToolResult(
            content=f"Invoice file not found: {selected_data_path.strip()}"
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to read invoice file '{selected_data_path.strip()}': {exc}"
        )

    resolved_doc_content: Optional[str] = None
    resolved_document_name: Optional[str] = None
    if output_mode == "XmlWithPdf":
        if not source_pdf_path or not source_pdf_path.strip():
            return ToolResult(
                content=(
                    "For output_mode XmlWithPdf, source_pdf_path is required."
                )
            )
        pdf_b64, ext = file_to_base64(source_pdf_path)
        if ext.lower() != ".pdf":
            return ToolResult(
                content=f"source_pdf_path must be a PDF, got '{ext}' instead."
            )
        resolved_doc_content = pdf_b64
        resolved_document_name = os.path.basename(source_pdf_path)

    zugferd_creator_action: dict[str, Any] = {
        "inputFormat": input_format,
        "outputMode": output_mode,
        "conformanceLevel": conformance_level,
        "zugferdVersion": zugferd_version,
        "renderInvoiceOnPdf": render_invoice_on_pdf,
    }
    if input_format == "XML":
        zugferd_creator_action["invoiceXmlData"] = selected_data_b64
    elif input_format == "JSON":
        zugferd_creator_action["invoiceJsonData"] = selected_data_b64
    else:
        zugferd_creator_action["invoiceCsvData"] = selected_data_b64
    if language and language.strip():
        zugferd_creator_action["language"] = language.strip()

    payload: dict[str, Any] = {"zugferdCreatorAction": zugferd_creator_action}
    if resolved_doc_content is not None:
        payload["docContent"] = resolved_doc_content
    if resolved_document_name is not None:
        payload["document"] = {"Name": resolved_document_name}

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        out_bytes, inferred_ext = await _call_create_zugferd_invoice_api(
            payload, PDF4ME_API_KEY
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(
                content="Authentication failed: the API key is invalid or missing."
            )
        return ToolResult(
            content=f"API error {exc.response.status_code}: {exc.response.text}"
        )
    except httpx.ReadTimeout as exc:
        return ToolResult(
            content=f"HTTP read timed out waiting for PDF4me (payload may be large): {exc}"
        )
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except ValueError as exc:
        return ToolResult(content=str(exc))

    if not out_bytes:
        return ToolResult(content="Unexpected API response — output bytes are empty.")

    if not output_dir or not output_dir.strip():
        return ToolResult(
            content="output_dir is required. Please provide an output directory path."
        )
    if not output_file_name or not output_file_name.strip():
        return ToolResult(
            content="output_file_name is required. Please provide an output file name."
        )

    resolved_output_dir = output_dir.strip()
    resolved_output_name = output_file_name.strip()

    _, ext = os.path.splitext(resolved_output_name)
    if not ext:
        resolved_output_name = f"{resolved_output_name}{inferred_ext}"

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            out_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    meta: dict[str, Any] = {
        "output_path": output_path,
        "output_mode": output_mode,
        "input_format": input_format,
    }

    return ToolResult(
        content=f"ZUGFeRD invoice created successfully. Saved to {output_path}",
        structured_content=meta,
    )
