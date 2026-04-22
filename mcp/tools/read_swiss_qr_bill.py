import asyncio
import json
import os
from typing import Any

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url

# Large PDF base64 + slow reads .
_HTTP_TIMEOUT = httpx.Timeout(connect=120.0, read=900.0, write=900.0, pool=120.0)
# maxRetries=20, retryDelay=10s, delay before every poll (including first).
_ASYNC_POLL_MAX_ATTEMPTS = 20
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _swiss_qr_data_from_response(resp: httpx.Response) -> Any:
    content_type = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in content_type:
        return resp.json()

    text = raw.decode("utf-8", errors="replace").strip()
    if not text.startswith(("{", "[")):
        raise ValueError(f"Unexpected API response content type: {content_type}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc


async def _call_read_swiss_qr_bill_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    use_async: bool,
) -> Any:
    """Call ReadSwissQRBill and return parsed Swiss QR data JSON."""
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
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ReadSwissQRBill",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_read_swiss_qr_bill_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _swiss_qr_data_from_response(resp)


async def _poll_read_swiss_qr_bill_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> Any:
    # Task.Delay before each GET, including the first poll.
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _swiss_qr_data_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"ReadSwissQRBill did not finish after {max_attempts} polls "
        f"({interval_sec}s apart, delay before each poll)"
    )


@tool(
    name="read_swiss_qr_bill",
    description=(
        "Read Swiss QR bill data from a local PDF using the PDF4me ReadSwissQRBill API. "
        "Provide the file path to the PDF. "
        "Supports both sync and async processing via use_async. "
        "Returns structured Swiss QR data as JSON."
    ),
)
async def read_swiss_qr_bill_http(
    file_path: str,
) -> ToolResult:
    """Read Swiss QR bill data from a PDF via PDF4me ReadSwissQRBill.

    Args:
        file_path: Local path to the PDF file.
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
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
        swiss_qr_data = await _call_read_swiss_qr_bill_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
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

    if swiss_qr_data is None:
        return ToolResult(content="Unexpected API response — no Swiss QR data returned.")

    return ToolResult(
        content="Swiss QR bill data retrieved successfully.",
        structured_content={
            "file_name": doc_name,
            "swiss_qr_data": swiss_qr_data,
        },
    )
