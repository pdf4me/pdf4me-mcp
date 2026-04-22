import asyncio
import json
import os
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config
from helper import file_to_base64, resolve_polling_url, write_file_from_base64

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 10.0


def _page_nrs_to_int_list(page_nrs: str) -> Optional[list[int]]:
    """Parse pageNrs strings like '1', '1-3', '1,3,5-7' into sorted unique page numbers.

    Returns None for 'all', open ranges ('2-'), or segments that cannot be resolved to integers.
    """
    s = page_nrs.strip().lower()
    if not s or s == "all":
        return None
    pages: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            return None
        if "-" in part:
            a, b = part.split("-", 1)
            a_st, b_st = a.strip(), b.strip()
            if a_st.isdigit() and b_st.isdigit():
                start, end = int(a_st), int(b_st)
                if end < start:
                    return None
                pages.update(range(start, end + 1))
            elif a_st.isdigit() and not b_st:
                return None
            else:
                return None
        elif part.isdigit():
            pages.add(int(part))
        else:
            return None
    return sorted(pages) if pages else None


def _images_from_result_data(result_data: Any) -> list[tuple[str, str]]:
    """Return list of (base64_content, file_name) from CreateImages JSON body."""
    if isinstance(result_data, list):
        out: list[tuple[str, str]] = []
        for item in result_data:
            if not isinstance(item, dict):
                continue
            b64 = item.get("docContent")
            name = item.get("docName")
            if isinstance(b64, str) and b64 and isinstance(name, str) and name:
                out.append((b64, name))
        if out:
            return out

    if isinstance(result_data, dict):
        docs = result_data.get("outputDocuments")
        if isinstance(docs, list):
            out = []
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                b64 = doc.get("streamFile")
                name = doc.get("fileName")
                if isinstance(b64, str) and b64 and isinstance(name, str) and name:
                    out.append((b64, name))
            if out:
                return out

        b64 = result_data.get("docContent")
        name = result_data.get("docName")
        if isinstance(b64, str) and b64 and isinstance(name, str) and name:
            return [(b64, name)]

    raise ValueError(
        "Unexpected CreateImages response shape: expected a JSON list of "
        "{docContent, docName}, outputDocuments with {streamFile, fileName}, "
        "or a single {docContent, docName} object."
    )


def _parse_images_response(resp: httpx.Response) -> list[tuple[str, str]]:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/json" not in ct and "json" not in ct:
        raise ValueError(f"Expected JSON response for CreateImages, got content-type {ct!r}")
    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in CreateImages response: {exc}") from exc
    return _images_from_result_data(data)


async def _poll_create_images_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
    max_attempts: int,
    interval_sec: float,
) -> list[tuple[str, str]]:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return _parse_images_response(poll)
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"CreateImages did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_create_images_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
    *,
    width_pixel: str,
    image_extension: str,
    page_nrs: str,
    use_async: bool,
) -> list[tuple[str, str]]:
    image_action: dict[str, Any] = {
        "WidthPixel": width_pixel,
        "ImageExtension": image_extension.lower(),
    }
    page_ints = _page_nrs_to_int_list(page_nrs)
    if page_ints is not None:
        image_action["PageSelection"] = {"PageNrs": page_ints}

    payload: dict[str, Any] = {
        "docContent": doc_content_base64,
        "docname": doc_name,
        "pageNrs": page_nrs.strip(),
        "imageAction": image_action,
        "isasync": use_async,
    }

    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/CreateImages",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 202:
            location = (resp.headers.get("Location") or resp.headers.get("location") or "").strip()
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_create_images_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )

        resp.raise_for_status()
        return _parse_images_response(resp)


@tool(
    name="create_images_from_pdf",
    description=(
        "Render PDF pages to image files using the PDF4me CreateImages API (POST /api/v2/CreateImages). "
        "Controls width in pixels, image format (jpeg, png, tiff, etc.), and page selection (top-level pageNrs "
        "plus imageAction.PageSelection.PageNrs when the expression parses to integers; use 'all' for all pages). "
        "When use_async is true, sends isasync true; the API may return 202 and the tool polls until images are ready. "
        "Writes one file per page to output_dir (defaults to the input PDF directory)."
    ),
)
async def create_images_from_pdf_http(
    file_path: str,
    page_number: str = "1",
    width_pixel: str = "800",
    image_extension: str = "jpeg",
    use_async: bool = True,
    output_dir: Optional[str] = None,
) -> ToolResult:
    """Convert PDF pages to images via PDF4me CreateImages.

    Args:
        file_path: Local path to the PDF file.
        page_number: Page selection string for top-level pageNrs (e.g. '1', '1-2', '1,3,5'). When parseable, also
            sent as imageAction.PageSelection.PageNrs. 'all' sends pageNrs only (no PageSelection block).
        width_pixel: Output image width in pixels (passed as string to imageAction.WidthPixel).
        image_extension: Output format: jpg, jpeg, bmp, gif, png, tif, tiff, etc. (imageAction.ImageExtension).
        use_async: When True, sends isasync true and polls on 202 until complete.
        output_dir: Directory for image files. Defaults to the input file's directory.
    """
    doc_content_base64, extension = file_to_base64(file_path)
    if extension.lower() != ".pdf":
        return ToolResult(content=f"Input file must be a PDF, got '{extension}' instead.")

    doc_name = os.path.basename(file_path)
    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(os.path.abspath(file_path))
    )

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    if not page_number.strip():
        return ToolResult(content="page_number must be a non-empty string.")

    try:
        images = await _call_create_images_api(
            doc_content_base64,
            doc_name,
            PDF4ME_API_KEY,
            width_pixel=width_pixel,
            image_extension=image_extension,
            page_nrs=page_number.strip(),
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

    if not images:
        return ToolResult(content="API returned no images to save.")

    saved: list[str] = []
    try:
        for b64, name in images:
            saved.append(write_file_from_base64(b64, resolved_output_dir, name))
    except OSError as exc:
        return ToolResult(content=f"Failed to write image output: {exc}")
    except (TypeError, ValueError) as exc:
        return ToolResult(content=f"Failed to decode image data: {exc}")

    return ToolResult(
        content=(
            f"Created {len(images)} image(s) from PDF. "
            f"Output directory: {resolved_output_dir}"
        ),
        structured_content={"output_paths": saved, "output_dir": resolved_output_dir},
    )
