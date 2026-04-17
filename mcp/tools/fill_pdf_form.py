import asyncio
import json
import os
from typing import Any, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


def _input_form_data_from_dict(form_data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"fieldName": field_name, "fieldValue": field_value}
        for field_name, field_value in form_data.items()
    ]


async def _call_fill_pdf_form_api(
    template_doc_content_base64: str,
    template_doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    form_data: dict[str, Any],
    use_async: bool,
) -> bytes:
    """POST to FillPdfForm; return raw PDF bytes (handles 200 body or 202 + poll)."""
    payload = {
        "templateDocName": template_doc_name,
        "templateDocContent": template_doc_content_base64,
        "dataArray": json.dumps(form_data),
        "outputType": "pdf",
        "inputDataType": "json",
        "metaData": "",
        "metaDataJson": "",
        "InputFormData": _input_form_data_from_dict(form_data),
        "async": use_async,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/FillPdfForm",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            return await _poll_fill_pdf_form_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_fill_pdf_form_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """First GET immediately, then poll every interval_sec until 200 or give up."""
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"FillPdfForm did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="fill_pdf_form",
    description=(
        "Fill form fields in a local PDF using the PDF4me FillPdfForm API. "
        "Provide the local PDF path and a JSON object of field/value pairs in form_data. "
        "Optionally specify output directory and output file name. "
        "When use_async is true, the API may return 202 and the tool polls until the PDF is ready."
    ),
)
async def fill_pdf_form_http(
    file_path: str,
    form_data: dict[str, Any],
    use_async: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Fill a PDF form using the PDF4me FillPdfForm API.

    Args:
        file_path: Local path to the template PDF file to fill.
        form_data: Form field values as key/value pairs (for example {"firstname": "John"}).
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the filled PDF. Defaults to the same directory as input file.
        output_file_name: Name for the output file. Defaults to filled_<input_filename>.pdf.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")
    if not form_data:
        return ToolResult(content="form_data is required and must contain at least one field.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else f"filled_{doc_name}"
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_fill_pdf_form_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            form_data=form_data,
            use_async=use_async,
        )
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

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ToolResult(
            content="Unexpected API response — filled PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF form filled successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
