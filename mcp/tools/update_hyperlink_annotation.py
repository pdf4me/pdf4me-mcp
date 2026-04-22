import asyncio
import os
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_HTTP_TIMEOUT = httpx.Timeout(connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 40
_ASYNC_POLL_INTERVAL_SEC = 2.5


def _bytes_from_response(resp: httpx.Response) -> bytes:
    """Binary PDF from UpdateHyperlinkAnnotation success response."""
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


async def _call_update_hyperlink_annotation_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    updates: list[dict[str, Any]],
    use_async: bool,
) -> bytes:
    """POST UpdateHyperlinkAnnotation; return raw PDF bytes (200 body or 202 + poll)."""
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "updatehyperlinkannotationlist": updates,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/UpdateHyperlinkAnnotation",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or "").strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_update_hyperlink_annotation_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_update_hyperlink_annotation_job(
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
        "UpdateHyperlinkAnnotation did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="update_hyperlink_annotation",
    description=(
        "Update hyperlink annotations in a PDF using PDF4me UpdateHyperlinkAnnotation "
        "API (POST /api/v2/UpdateHyperlinkAnnotation). "
        "Provide a local PDF path and all hyperlink update fields "
        "(SearchOn, SearchValue, IsExpression, TextCurrentValue, TextNewValue, "
        "URLCurrentValue, URLNewValue). "
        "Supports async via isAsync and 202 Location polling. "
        "Saves the updated PDF to disk."
    ),
)
async def update_hyperlink_annotation_http(
    file_path: str,
    search_on: str,
    search_value: str,
    is_expression: bool,
    text_current_value: str,
    text_new_value: str,
    url_current_value: str,
    url_new_value: str,
    output_dir: str = "",
    output_file_name: str = "",
) -> ToolResult:
    """Update hyperlink text/URL annotations in a PDF via PDF4me.

    Args:
        file_path: Local path to the input PDF.
        search_on: Search criteria type (e.g. Text).
        search_value: Value to search for.
        is_expression: Whether the search is treated as an expression.
        text_current_value: Existing display text to replace.
        text_new_value: New display text.
        url_current_value: Existing hyperlink URL to replace.
        url_new_value: New hyperlink URL destination.
        use_async: When True, sends isAsync and polls the Location URL on 202.
        output_dir: Directory for the output PDF (required).
        output_file_name: Output filename (required).
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    if not search_on.strip():
        return ToolResult(content="search_on is required.")
    if not search_value.strip():
        return ToolResult(content="search_value is required.")
    if not text_current_value.strip():
        return ToolResult(content="text_current_value is required.")
    if not text_new_value.strip():
        return ToolResult(content="text_new_value is required.")
    if not url_current_value.strip():
        return ToolResult(content="url_current_value is required.")
    if not url_new_value.strip():
        return ToolResult(content="url_new_value is required.")

    updatehyperlinkannotationlist: list[dict[str, Any]] = [
        {
            "SearchOn": search_on.strip(),
            "SearchValue": search_value.strip(),
            "IsExpression": is_expression,
            "TextCurrentValue": text_current_value.strip(),
            "TextNewValue": text_new_value.strip(),
            "URLCurrentValue": url_current_value.strip(),
            "URLNewValue": url_new_value.strip(),
        }
    ]

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
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_update_hyperlink_annotation_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            updates=updatehyperlinkannotationlist,
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
    except httpx.ReadTimeout as exc:
        return ToolResult(
            content=f"HTTP read timed out waiting for PDF4me (payload may be large): {exc}"
        )
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")
    except (ValueError, TimeoutError) as exc:
        return ToolResult(content=str(exc))

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ToolResult(
            content="Unexpected API response — updated PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Hyperlink annotations updated successfully. Saved to {output_path}",
        structured_content={"output_path": output_path},
    )

