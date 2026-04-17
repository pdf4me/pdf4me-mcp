import asyncio
import os
from typing import Annotated, Literal, Optional

import httpx
from pydantic import AliasChoices, Field

from fastmcp.tools.function_tool import tool
from fastmcp.tools import ToolResult

from config import config
from helper import write_file_from_bytes


def _resolve_save_directory(output_dir: Optional[str]) -> str:
    """Absolute directory for saving; cwd only when unset or blank after strip."""
    if output_dir is None:
        return os.getcwd()
    s = str(output_dir).strip()
    if not s:
        return os.getcwd()
    expanded = os.path.expanduser(s)
    return os.path.abspath(os.path.normpath(expanded))

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


async def _call_convert_url_to_pdf_api(
    web_url: str,
    PDF4ME_API_KEY: str,
    *,
    auth_type: str,
    username: str,
    password: str,
    layout: str,
    page_format: str,
    scale: float,
    top_margin: str,
    left_margin: str,
    right_margin: str,
    bottom_margin: str,
    print_background: bool,
    display_header_footer: bool,
    use_async: bool,
) -> bytes:
    """POST ConvertUrlToPdf; return raw PDF bytes (200 or 202 + poll)."""
    payload = {
        "webUrl": web_url,
        "authType": auth_type,
        "username": username,
        "password": password,
        "docContent": "",
        "layout": layout,
        "format": page_format,
        "scale": scale,
        "topMargin": top_margin,
        "leftMargin": left_margin,
        "rightMargin": right_margin,
        "bottomMargin": bottom_margin,
        "printBackground": print_background,
        "displayHeaderFooter": display_header_footer,
        "isAsync": True,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/ConvertUrlToPdf",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError("API returned 202 but no Location header for polling")
            return await _poll_convert_url_to_pdf_job(
                client,
                location,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return _bytes_from_response(resp)


async def _poll_convert_url_to_pdf_job(
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
            return _bytes_from_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"URL to PDF did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


def _bytes_from_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


@tool(
    name="convert_url_to_pdf",
    description=(
        "Convert a web page to PDF using the PDF4me ConvertUrlToPdf API. "
        "Required input is only web_url (https://...); no local file path is read. "
        "Optional: layout, page format, margins, scale, print background, auth (NoAuth or credentials), "
        "output_dir/output_file_name for where to save the PDF (default file name output.pdf). "
        "When use_async is true, the API may return 202 and the tool polls until the PDF is ready."
    ),
)
async def convert_url_to_pdf_http(
    web_url: str,
    auth_type: str = "NoAuth",
    username: str = "",
    password: str = "",
    layout: Literal["portrait", "landscape"] = "portrait",
    page_format: str = "A4",
    scale: float = 1.0,
    top_margin: str = "20px",
    left_margin: str = "20px",
    right_margin: str = "20px",
    bottom_margin: str = "20px",
    print_background: bool = True,
    display_header_footer: bool = False,
    use_async: bool = True,
    output_dir: Annotated[
        Optional[str],
        Field(
            default=None,
            validation_alias=AliasChoices(
                "output_dir", "outputDir", "output_directory", "outputDirectory"
            ),
            description="Directory to save the PDF.",
        ),
    ] = None,
    output_file_name: Annotated[
        Optional[str],
        Field(
            default=None,
            validation_alias=AliasChoices(
                "output_file_name", "outputFileName", "file_name", "fileName"
            ),
            description="File name for the saved PDF.",
        ),
    ] = None,
) -> ToolResult:
    """Convert a URL to PDF via the PDF4me ConvertUrlToPdf API.

    No local input file is used—only web_url identifies the page to render.

    Args:
        web_url: Page to render (https://...).
        auth_type: Site auth mode (e.g. NoAuth; use Basic + username/password if needed).
        username: Optional username when the target URL requires authentication.
        password: Optional password when the target URL requires authentication.
        layout: Page orientation (portrait or landscape).
        page_format: Paper size (e.g. A4, Letter, Tabloid per API docs).
        scale: Render scale (1.0 = 100%).
        top_margin, left_margin, right_margin, bottom_margin: CSS-like margin strings (e.g. 20px).
        print_background: Include backgrounds in the PDF.
        display_header_footer: Print browser header/footer region if supported.
        use_async: When True, request async processing and poll the Location URL on 202
            using fixed internal retry settings (not configurable by the caller).
        output_dir: Optional directory to save the PDF. Defaults to the current working directory.
        output_file_name: Optional name for the saved PDF. Defaults to output.pdf.
    """
    resolved_output_dir = _resolve_save_directory(output_dir)
    name_raw = (
        output_file_name.strip()
        if isinstance(output_file_name, str) and output_file_name.strip()
        else None
    )
    resolved_output_name = name_raw if name_raw else "output.pdf"
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes = await _call_convert_url_to_pdf_api(
            web_url,
            PDF4ME_API_KEY,
            auth_type=auth_type,
            username=username,
            password=password,
            layout=layout,
            page_format=page_format,
            scale=scale,
            top_margin=top_margin,
            left_margin=left_margin,
            right_margin=right_margin,
            bottom_margin=bottom_margin,
            print_background=print_background,
            display_header_footer=display_header_footer,
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
            content="Unexpected API response — PDF bytes missing or invalid."
        )

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    return ToolResult(
        content=f"Web page converted to PDF successfully. Saved to {output_path}",
        structured_content={
            "output_path": output_path,
            "web_url": web_url,
        },
    )
