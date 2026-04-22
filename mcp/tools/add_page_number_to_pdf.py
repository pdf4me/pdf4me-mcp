import asyncio
import base64
import json
import os
from typing import Annotated, Any, Literal, Optional

import httpx
from pydantic import Field

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _docdata_b64_from_json(obj: Any, *, depth: int = 0) -> Optional[str]:
    if depth > 12 or not isinstance(obj, dict):
        return None
    for dk in ("docContent", "DocContent", "docData", "DocData"):
        v = obj.get(dk)
        if isinstance(v, str) and v:
            return v
    for doc_key in ("document", "Document"):
        sub = obj.get(doc_key)
        if isinstance(sub, dict):
            found = _docdata_b64_from_json(sub, depth=depth + 1)
            if found:
                return found
    return None


def _bytes_from_pdf_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/pdf" in ct or "application/octet-stream" in ct:
        return raw

    body = _strip_utf8_bom_and_leading_ws(raw)
    if body.startswith(b"%PDF"):
        return body

    if body.startswith((b"{", b"[")):
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object with docContent")
        b64 = _docdata_b64_from_json(payload)
        if not b64:
            raise ValueError("Response JSON has no docContent/DocData base64 field")
        return base64.b64decode(b64)

    if raw:
        return raw

    raise ValueError(
        f"Expected PDF binary or JSON with docContent, got content-type {ct!r}"
    )


async def _poll_add_page_number_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> bytes:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _bytes_from_pdf_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"AddPageNumber did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_add_page_number_api(
    doc_name: str,
    doc_content_base64: str,
    page_number_format: str,
    align_x: str,
    align_y: str,
    PDF4ME_API_KEY: str,
    *,
    margin_x_in_mm: Optional[int],
    margin_y_in_mm: Optional[int],
    font_size: Optional[int],
    is_bold: Optional[bool],
    is_italic: Optional[bool],
    skip_first_page: Optional[bool],
) -> bytes:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/AddPageNumber"
    payload: dict[str, Any] = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "pageNumberFormat": page_number_format,
        "alignX": align_x,
        "alignY": align_y,
        "isAsync": True,
    }
    if margin_x_in_mm is not None:
        payload["marginXinMM"] = margin_x_in_mm
    if margin_y_in_mm is not None:
        payload["marginYinMM"] = margin_y_in_mm
    if font_size is not None:
        payload["fontSize"] = font_size
    if is_bold is not None:
        payload["isBold"] = is_bold
    if is_italic is not None:
        payload["isItalic"] = is_italic
    if skip_first_page is not None:
        payload["skipFirstPage"] = skip_first_page

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_add_page_number_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_pdf_response(resp)


_ADD_PAGE_NUMBER_TOOL_DESCRIPTION = (
    "Add page numbers to a PDF via PDF4me POST /api/v2/AddPageNumber. "
    "When helping a user, explain how to set page_number_format before calling: "
    "use the literal character # for the current page (1, 2, 3, …) and the literal "
    "substring {1} for the total page count. Those are placeholders, not Python format "
    "specifiers—pass them exactly as strings (e.g. '# of {1}' renders as '1 of 10', "
    "'Page #' as 'Page 1', '# / {1}' as '1 / 10', '(#)' as '(1)', '#' alone as '1'). "
    "align_x must be left, center, or right; align_y must be top, middle, or bottom. "
    "Optional: margin_x_in_mm and margin_y_in_mm (integers 0–100 mm from the chosen edge), "
    "font_size (8–72), is_bold, is_italic, skip_first_page. "
    "Requires a local pdf_file_path; output defaults next to the source file."
)


@tool(
    name="add_page_number_to_pdf",
    description=_ADD_PAGE_NUMBER_TOOL_DESCRIPTION,
)
async def add_page_number_to_pdf(
    pdf_file_path: Annotated[
        str,
        Field(description="Absolute or relative path to the input PDF file."),
    ],
    page_number_format: Annotated[
        str,
        Field(
            description=(
                "Text pattern for each footer/header line. Use # for current page number "
                "and {1} for total pages (both are literal characters in the string). "
                "Examples: '# of {1}', 'Page #', '# / {1}', '(#)', '#'."
            ),
        ),
    ],
    align_x: Annotated[
        Literal["left", "center", "right"],
        Field(description="Horizontal position of the page number text."),
    ] = "right",
    align_y: Annotated[
        Literal["top", "middle", "bottom"],
        Field(description="Vertical position of the page number text."),
    ] = "bottom",
    margin_x_in_mm: Annotated[
        Optional[int],
        Field(
            default=None,
            description="Horizontal margin from the aligned edge, in millimeters (0–100).",
        ),
    ] = None,
    margin_y_in_mm: Annotated[
        Optional[int],
        Field(
            default=None,
            description="Vertical margin from the aligned edge, in millimeters (0–100).",
        ),
    ] = None,
    font_size: Annotated[
        Optional[int],
        Field(
            default=None,
            description="Font size for the page number (allowed range 8–72 when set).",
        ),
    ] = None,
    is_bold: Annotated[
        Optional[bool],
        Field(default=None, description="If true, render page numbers in bold."),
    ] = None,
    is_italic: Annotated[
        Optional[bool],
        Field(default=None, description="If true, render page numbers in italic."),
    ] = None,
    skip_first_page: Annotated[
        Optional[bool],
        Field(
            default=None,
            description="If true, do not print a page number on the first page.",
        ),
    ] = None,
    request_doc_name: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Output document name sent to the API (should end with .pdf). "
                "Defaults to the input file basename."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Directory for the saved PDF; defaults to the input file's directory.",
        ),
    ] = None,
    output_file_name: Annotated[
        Optional[str],
        Field(
            default=None,
            description="File name for the saved PDF (e.g. numbered_report.pdf).",
        ),
    ] = None,
) -> ToolResult:
    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_b64, pdf_ext = file_to_base64(pdf_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read PDF file: {exc}")

    if pdf_ext.lower() != ".pdf":
        return ToolResult(
            content=f"Source file must be a PDF, got '{pdf_ext}' instead."
        )

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(os.path.abspath(pdf_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"page_numbered_{doc_name}"
    )

    try:
        pdf_bytes = await _call_add_page_number_api(
            doc_name=doc_name,
            doc_content_base64=pdf_b64,
            page_number_format=page_number_format,
            align_x=align_x,
            align_y=align_y,
            PDF4ME_API_KEY=PDF4ME_API_KEY,
            margin_x_in_mm=margin_x_in_mm,
            margin_y_in_mm=margin_y_in_mm,
            font_size=font_size,
            is_bold=is_bold,
            is_italic=is_italic,
            skip_first_page=skip_first_page,
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

    if not pdf_bytes:
        return ToolResult(content="Unexpected API response — no PDF content returned.")

    try:
        output_path = write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name
        )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"PDF with page numbers saved successfully to {output_path}",
        structured_content={
            "output_path": output_path,
            "doc_name": doc_name,
            "page_number_format": page_number_format,
            "align_x": align_x,
            "align_y": align_y,
        },
    )
