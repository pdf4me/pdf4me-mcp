import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, write_file_from_bytes

OptimizeProfile = Literal[
    "web",
    "Max",
    "Print",
    "Default",
    "WebMax",
    "PrintMax",
    "PrintGray",
    "Compress",
    "CompressMax",
]

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


async def _call_linearize_api(
    doc_content_base64: str,
    doc_name: str,
    optimize_profile: str,
    PDF4ME_API_KEY: str,
) -> bytes:
    """POST to LinearizePdf; return raw PDF bytes (handles 200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "optimizeProfile": optimize_profile,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/LinearizePdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            return await _poll_linearize_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_linearize_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
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
        f"Linearize PDF did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="linearize_pdf",
    description=(
        "Linearize a PDF file with the PDF4me API for faster web loading and progressive display. "
        "Provide the local file path to the PDF. "
        "Choose optimize_profile: web, Max, Print, Default, WebMax, PrintMax, PrintGray, "
        "Compress, or CompressMax. "
        "Optionally specify an output directory and output file name. "
        " "
    ),
)
async def linearize_pdf_http(
    file_path: str,
    optimize_profile: OptimizeProfile = "web",
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Linearize a PDF using the PDF4me LinearizePdf API.

    Args:
        file_path: Local path to the PDF file to linearize.
        optimize_profile: Optimization preset (see PDF4me LinearizePdf docs).
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = output_file_name if output_file_name else f"linearized_{doc_name}"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_linearize_api(
            doc_content_base64,
            doc_name,
            optimize_profile,
            PDF4ME_API_KEY,
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
            content="Unexpected API response — linearized PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF linearized successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
