import asyncio
import json
import os
from typing import Any

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url

_HTTP_TIMEOUT = httpx.Timeout(connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 50
_ASYNC_POLL_INTERVAL_SEC = 10.0

_ALLOWED_IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_from_response(resp: httpx.Response) -> Any:
    content_type = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in content_type:
        return resp.json()

    trimmed = _strip_utf8_bom_and_leading_ws(raw)
    text = trimmed.decode("utf-8", errors="replace").strip()
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc

    # Some PDF4me actions can return octet-stream/plain text despite semantic JSON/text output.
    # Accept non-empty text payloads as OCR text instead of failing on content-type.
    if text:
        return {"text": text}

    raise ValueError(f"Unexpected API response content type: {content_type!r}")


def _summary_text_from_result(data: Any, *, max_len: int = 8000) -> str:
    """Best-effort human-readable text from varying OCR JSON shapes."""
    if isinstance(data, str) and data.strip():
        s = data.strip()
        return s if len(s) <= max_len else s[: max_len - 3] + "..."

    if not isinstance(data, dict):
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        return raw if len(raw) <= max_len else raw[: max_len - 3] + "..."

    for key in (
        "extractedText",
        "text",
        "ocrText",
        "plainText",
        "content",
        "result",
        "message",
    ):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            s = val.strip()
            return s if len(s) <= max_len else s[: max_len - 3] + "..."

    nested = data.get("document") or data.get("Document") or data.get("output")
    if isinstance(nested, dict):
        inner = _summary_text_from_result(nested, max_len=max_len)
        if inner:
            return inner

    raw = json.dumps(data, ensure_ascii=False, indent=2)
    return raw if len(raw) <= max_len else raw[: max_len - 3] + "..."


async def _poll_image_extract_text_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> Any:
    for _ in range(max_attempts):
        await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _json_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "ImageExtractText did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart, delay before each poll)"
    )


async def _call_image_extract_text_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    use_async: bool,
) -> Any:
    payload: dict[str, Any] = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "isAsync": use_async,
    }

    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ImageExtractText",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 202:
            location = (
                resp.headers.get("Location")
                or resp.headers.get("location")
                or ""
            ).strip()
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_image_extract_text_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )

        resp.raise_for_status()
        return _json_from_response(resp)


@tool(
    name="extract_text_from_image",
    description=(
        "Extract text from a local image using the PDF4me ImageExtractText API (POST /api/v2/ImageExtractText). "
        "Uses OCR; supports async processing with polling on 202. "
        "Returns the parsed JSON response in structured_content and a text summary when possible."
    ),
)
async def extract_text_from_image_http(
    file_path: str,
    use_async: bool = True,
) -> ToolResult:
    """Extract text from an image via PDF4me ImageExtractText.

    Args:
        file_path: Local path to the image file (e.g. JPG, PNG, TIFF, WEBP).
        use_async: When True, sends isAsync: true and polls the Location URL on 202
            (50 attempts, 10s between polls, delay before each poll).
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() not in _ALLOWED_IMAGE_EXT:
        return ToolResult(
            content=(
                f"Input file must be a supported image type "
                f"({', '.join(sorted(_ALLOWED_IMAGE_EXT))}), got '{extension}' instead."
            )
        )

    doc_name = os.path.basename(file_path)

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        result_data = await _call_image_extract_text_api(
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

    summary = _summary_text_from_result(result_data)
    return ToolResult(
        content=f"Text extracted from image successfully.\n\n{summary}",
        structured_content={
            "file_name": doc_name,
            "extract_result": result_data,
        },
    )
