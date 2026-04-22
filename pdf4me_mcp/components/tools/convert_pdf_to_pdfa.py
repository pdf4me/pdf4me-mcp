import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes

PdfACompliance = Literal[
    "PdfA1a",
    "PdfA1b",
    "PdfA2a",
    "PdfA2b",
    "PdfA2u",
    "PdfA3a",
    "PdfA3b",
    "PdfA3u",
]

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


async def _call_pdf_a_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    compliance: str,
    allow_upgrade: bool,
    allow_downgrade: bool,
    use_async: bool,
) -> bytes:
    """POST to PdfA; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "compliance": compliance,
        "allowUpgrade": allow_upgrade,
        "allowDowngrade": allow_downgrade,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/PdfA",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            return await _poll_pdf_a_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_pdf_a_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    """Poll the job URL until we get the PDF or time out."""
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
        f"PDF/A conversion did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="convert_pdf_to_pdfa",
    description=(
        "Convert a PDF file to PDF/A (archival ISO format) using the PDF4me PdfA API. "
        "Provide the local file path to the PDF. "
        "Choose compliance (e.g. PdfA1b), allow_upgrade / allow_downgrade, and optional output path. "
        "When use_async is true, the API may return 202 and the tool polls until the PDF is ready."
    ),
)
async def convert_pdf_to_pdfa_http(
    file_path: str,
    compliance: PdfACompliance = "PdfA1b",
    allow_upgrade: bool = True,
    allow_downgrade: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Convert a PDF to PDF/A via the PDF4me PdfA API.

    Args:
        file_path: Local path to the PDF file to convert.
        compliance: PDF/A level (PdfA1b is common for basic conformance).
        allow_upgrade: Allow upgrading to higher compliance when supported by the API.
        allow_downgrade: Allow downgrading to lower compliance when supported by the API.
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the PDF/A file. Defaults to the same directory as the input file.
        output_file_name: Name for the output file. Defaults to pdfa_<input_filename>.pdf.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else f"pdfa_{doc_name}"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_pdf_a_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            compliance=compliance,
            allow_upgrade=allow_upgrade,
            allow_downgrade=allow_downgrade,
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
            content="Unexpected API response — PDF/A bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF converted to PDF/A successfully. Saved to {output_path}",
        structured_content={
            "output_path": output_path,
            "compliance": compliance,
        },
    )
