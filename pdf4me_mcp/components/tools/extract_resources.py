import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


def _strip_utf8_bom_and_leading_ws(data: bytes) -> bytes:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.lstrip()


def _json_dict_from_response(resp: httpx.Response) -> dict[str, Any]:
    body = _strip_utf8_bom_and_leading_ws(resp.content)
    if not body.startswith(b"{"):
        raise ValueError(
            f"Expected JSON object from ExtractResources, got content-type "
            f"{(resp.headers.get('content-type') or '')!r}"
        )
    try:
        parsed: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object from ExtractResources")
    return parsed


def _coerce_text_list(data: dict[str, Any]) -> list[str]:
    for key in ("textList", "TextList", "texts", "Texts"):
        raw = data.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return [str(raw)]
    return []


def _image_b64_and_name(item: Any, index: int) -> tuple[str, str]:
    if not isinstance(item, dict):
        if isinstance(item, str) and len(item) > 50:
            return item, f"extracted_image_{index + 1}.png"
        raise ValueError(f"Unexpected imageList entry type: {type(item)!r}")

    name_keys = ("fileName", "FileName", "name", "Name", "docName", "DocName")
    content_keys = (
        "imageContent",
        "ImageContent",
        "content",
        "Content",
        "data",
        "Data",
        "base64",
        "imageData",
        "ImageData",
        "docContent",
        "DocContent",
    )

    b64: Optional[str] = None
    for ck in content_keys:
        v = item.get(ck)
        if isinstance(v, str) and v:
            b64 = v
            break
    if not b64:
        raise ValueError(
            f"Image object has no base64 content (keys: {list(item.keys())})")

    name: Optional[str] = None
    for nk in name_keys:
        v = item.get(nk)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    if not name:
        name = f"extracted_image_{index + 1}.png"

    return b64, name


def _coerce_image_entries(data: dict[str, Any]) -> list[tuple[str, str]]:
    for key in ("imageList", "ImageList", "images", "Images"):
        raw = data.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ValueError(f"Expected list for {key!r}, got {type(raw)!r}")
        out: list[tuple[str, str]] = []
        for i, item in enumerate(raw):
            out.append(_image_b64_and_name(item, i))
        return out
    return []


async def _poll_extract_resources_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> httpx.Response:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            return poll
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        f"ExtractResources did not finish after {max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_extract_resources_api(
    payload: dict[str, Any],
    pdf4me_api_key: str,
) -> dict[str, Any]:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/ExtractResources"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            final = await _poll_extract_resources_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return _json_dict_from_response(final)
        resp.raise_for_status()
        return _json_dict_from_response(resp)


def _default_output_dir(pdf_file_path: str) -> str:
    p = Path(pdf_file_path).resolve()
    return str(p.parent / f"extracted_resources_{p.stem}")


@tool(
    name="extract_resources",
    description=(
        "Extract text and/or embedded images from a PDF via PDF4me ExtractResources (/api/v2/ExtractResources). "
        "pdf_file_path, extract_text, extract_images; optional output_dir (defaults next to PDF). "
        "Writes extracted_resources.json, extracted_text.txt when text is extracted, and image files."
    ),
)
async def extract_resources(
    pdf_file_path: str,
    extract_text: bool = True,
    extract_images: bool = True,
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_b64, pdf_ext = file_to_base64(pdf_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read PDF file: {exc}")

    if pdf_ext.lower() != ".pdf":
        return ToolResult(content=f"Source file must be a PDF, got '{pdf_ext}' instead.")

    doc_name = request_doc_name or os.path.basename(pdf_file_path)
    if not doc_name.lower().endswith(".pdf"):
        doc_name = f"{doc_name}.pdf"

    payload: dict[str, Any] = {
        "docContent": pdf_b64,
        "docName": doc_name,
        "extractText": extract_text,
        "extractImages": extract_images,
        "isAsync": True,
    }

    resolved_out = output_dir if output_dir else _default_output_dir(
        pdf_file_path)

    try:
        resource_data = await _call_extract_resources_api(payload, pdf4me_api_key)
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

    text_list = _coerce_text_list(resource_data) if extract_text else []
    image_entries = _coerce_image_entries(
        resource_data) if extract_images else []

    os.makedirs(resolved_out, exist_ok=True)
    json_path = os.path.join(resolved_out, "extracted_resources.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(resource_data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write JSON metadata: {exc}")

    text_path: Optional[str] = None
    if extract_text and text_list:
        text_path = os.path.join(resolved_out, "extracted_text.txt")
        try:
            with open(text_path, "w", encoding="utf-8") as f:
                f.write("\n".join(text_list))
        except OSError as exc:
            return ToolResult(content=f"Failed to write extracted text file: {exc}")

    image_paths: list[str] = []
    for b64, fname in image_entries:
        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            return ToolResult(content=f"Invalid base64 for image {fname!r}: {exc}")
        try:
            path = write_file_from_bytes(raw, resolved_out, fname)
            image_paths.append(path)
        except OSError as exc:
            return ToolResult(
                content=f"Failed to write image {fname!r} in '{resolved_out}': {exc}"
            )

    joined_text = "\n".join(text_list)
    preview = joined_text[:2000] if extract_text else ""
    if extract_text and text_list and len(joined_text) > 2000:
        preview = preview + "\n… (truncated; see extracted_text.txt)"

    summary = (
        f"Extracted resources saved under {resolved_out}: "
        f"{len(text_list)} text segment(s), {len(image_paths)} image(s). "
        f"JSON: {json_path}"
    )

    return ToolResult(
        content=summary,
        structured_content={
            "output_directory": resolved_out,
            "json_path": json_path,
            "text_file_path": text_path,
            "image_paths": image_paths,
            "text_segment_count": len(text_list),
            "image_count": len(image_paths),
            "text_preview": preview if extract_text else "",
        },
    )
