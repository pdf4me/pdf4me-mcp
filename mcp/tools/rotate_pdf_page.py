import asyncio
import os
from typing import Literal, Optional

import httpx

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


async def _call_rotate_page_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    rotation_type: Literal[
        "NoRotation", "Clockwise", "CounterClockwise", "UpsideDown"
    ],
    page: str,
    use_async: bool,
) -> bytes:
    """POST RotatePage; return raw PDF bytes (handles 200 body or 202 + poll)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "rotationType": rotation_type,
        "page": page,
        "isAsync": use_async,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/RotatePage",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_rotate_page_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_rotate_page_job(
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
        f"RotatePage did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """We only accept a straight binary PDF response from this endpoint."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="rotate_pdf_page",
    description=(
        "Rotate selected pages of a PDF using the PDF4me RotatePage API. "
        "Provide the PDF path, page spec (e.g. \"1\", \"1,3,5\", \"2-4\"), and rotationType. "
        "Optional output directory and file name. "
        "When use_async is true, the API may return 202 and the tool polls until the PDF is ready."
    ),
)
async def rotate_pdf_page_http(
    file_path: str,
    page: str,
    rotation_type: Literal[
        "NoRotation", "Clockwise", "CounterClockwise", "UpsideDown"
    ] = "Clockwise",
    use_async: bool = True,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Rotate specific pages of a PDF using the PDF4me RotatePage API.

    Args:
        file_path: Local path to the PDF file.
        page: Pages to rotate (e.g. \"1\", \"1,3,5\", \"2-4\").
        rotation_type: Rotation per API (NoRotation, Clockwise, CounterClockwise, UpsideDown).
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Directory to save the output. Defaults to the same directory as the input file.
        output_file_name: Name for the output file. Defaults to rotated_pages_<input_filename>.pdf.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    page_spec = page.strip()
    if not page_spec:
        return ToolResult(content="page is required (e.g. \"1\" or \"1,3,5\" or \"2-4\").")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = output_dir if output_dir else os.path.dirname(
        os.path.abspath(file_path))
    resolved_output_name = (
        output_file_name if output_file_name else f"rotated_pages_{doc_name}"
    )
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_rotate_page_api(
            doc_content_base64,
            resolved_output_name,
            PDF4ME_API_KEY,
            rotation_type=rotation_type,
            page=page_spec,
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
            content="Unexpected API response — rotated PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"PDF pages rotated successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )
