import asyncio
import json
import os
from typing import Any

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _metadata_from_response(resp: httpx.Response) -> dict[str, Any]:
    content_type = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in content_type:
        parsed = resp.json()
    else:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text.startswith(("{", "[")):
            raise ValueError(
                f"Unexpected API response content type: {content_type}")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc

    if isinstance(parsed, dict):
        return parsed
    raise ValueError("Expected JSON object for metadata response.")


async def _call_metadata_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
) -> dict[str, Any]:
    """Call GetPdfMetadata and return metadata JSON (sync or async polling)."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/GetPdfMetadata",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_metadata_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _metadata_from_response(resp)


async def _poll_metadata_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> dict[str, Any]:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _metadata_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"GetPdfMetadata did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="get_pdf_metadata",
    description=(
        "Extract metadata from a local PDF using the PDF4me GetPdfMetadata API. "
        "Provide the file path to the PDF. "
        " "
        "Returns metadata such as title, author, page count, size, dates, and security properties."
    ),
)
async def get_pdf_metadata_http(
    file_path: str,
) -> ToolResult:
    """Extract metadata from a PDF via PDF4me GetPdfMetadata.

    Args:
        file_path: Local path to the PDF file.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        metadata = await _call_metadata_api(
            doc_content_base64,
            doc_name,
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

    if not metadata:
        return ToolResult(content="Unexpected API response — no metadata returned.")

    return ToolResult(
        content="PDF metadata retrieved successfully.",
        structured_content={
            "file_name": doc_name,
            "metadata": metadata,
        },
    )
