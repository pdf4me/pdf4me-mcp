import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

logger = logging.getLogger(__name__)

# Large PDF base64 + slow reads (align with other heavy PDF4me tools).
_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 50
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _barcodes_from_response(resp: httpx.Response) -> Any:
    content_type = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in content_type:
        return resp.json()

    text = raw.decode("utf-8", errors="replace").strip()
    if not text.startswith(("{", "[")):
        raise ValueError(
            f"Unexpected API response content type: {content_type}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc


async def _call_read_barcodes_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    barcode_type: list[str],
    pages: str,
) -> Any:
    """Call ReadBarcodes and return parsed barcode JSON data."""
    payload = {
        "docContent": doc_content_base64,
        "docName": doc_name,
        "barcodeType": barcode_type,
        "pages": pages,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        logger.info(
            "ReadBarcodes: POST ReadBarcodes isAsync=true docName=%s pages=%s barcodeType=%s",
            doc_name,
            pages,
            barcode_type,
        )
        resp = await client.post(
            f"{api_base_url}/api/v2/ReadBarcodes",
            json=payload,
            headers=headers,
        )
        logger.info(
            "ReadBarcodes: initial POST finished status=%s",
            resp.status_code,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location")
                        or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            logger.info(
                "ReadBarcodes: async job accepted (202), polling up to %s times every %ss; poll_url=%s",
                _ASYNC_POLL_MAX_ATTEMPTS,
                _ASYNC_POLL_INTERVAL_SEC,
                poll_url,
            )
            return await _poll_read_barcodes_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        if resp.status_code == 200:
            logger.info(
                "ReadBarcodes: barcode payload returned on initial POST (no Location polling)"
            )
        resp.raise_for_status()
        return _barcodes_from_response(resp)


async def _poll_read_barcodes_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> Any:
    # Sleep before each GET, including the first poll (same pattern as a server-side Task.Delay).
    logger.info(
        "ReadBarcodes poll: started url=%s max_attempts=%s interval_sec=%s (sleep before each GET)",
        location_url,
        max_attempts,
        interval_sec,
    )
    for attempt in range(max_attempts):
        logger.info(
            "ReadBarcodes poll: attempt %s/%s — sleeping %ss then GET",
            attempt + 1,
            max_attempts,
            interval_sec,
        )
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        logger.info(
            "ReadBarcodes poll: attempt %s/%s GET done status=%s",
            attempt + 1,
            max_attempts,
            poll.status_code,
        )
        if poll.status_code == 200:
            logger.info(
                "ReadBarcodes poll: finished successfully (200) on attempt %s/%s",
                attempt + 1,
                max_attempts,
            )
            return _barcodes_from_response(poll)
        if poll.status_code == 202:
            logger.info(
                "ReadBarcodes poll: attempt %s/%s job still running (202), will retry",
                attempt + 1,
                max_attempts,
            )
            continue
        poll.raise_for_status()
    logger.warning(
        "ReadBarcodes poll: timed out after %s attempts (%ss between polls)",
        max_attempts,
        interval_sec,
    )
    raise TimeoutError(
        f"ReadBarcodes did not finish after {max_attempts} polls "
        f"({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="read_barcodes",
    description=(
        "Read barcodes and QR codes from a local PDF using the PDF4me ReadBarcodes API. "
        "Provide the file path, barcode types (e.g. all, qrCode, dataMatrix, code128), and pages. "
        "Always requests async processing (isAsync true); on HTTP 202 the tool polls the Location URL. "
        "Returns structured barcode data as JSON."
    ),
)
async def read_barcodes_http(
    file_path: str,
    barcode_type: Optional[list[str]] = None,
    pages: str = "all",
) -> ToolResult:
    """Read barcode / QR data from a PDF via PDF4me ReadBarcodes.

    Args:
        file_path: Local path to the PDF file.
        barcode_type: Barcode type filters sent to the API (defaults to ["all"]).
        pages: Pages expression like "all", "1", "1,3,5", "2-5", "1,3,7-10", "2-".
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_barcode_type = barcode_type if barcode_type else ["all"]

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        barcode_data = await _call_read_barcodes_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            barcode_type=resolved_barcode_type,
            pages=pages.strip() or "all",
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

    if barcode_data is None:
        return ToolResult(content="Unexpected API response — no barcode data returned.")

    return ToolResult(
        content="Barcode data retrieved successfully.",
        structured_content={
            "file_name": doc_name,
            "barcode_data": barcode_data,
        },
    )
