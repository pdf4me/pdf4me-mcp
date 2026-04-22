import asyncio
import json
import os
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url

_ASYNC_POLL_MAX_ATTEMPTS = 25
_ASYNC_POLL_INTERVAL_SEC = 2.0


async def _poll_get_tracking_changes_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    *,
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
        "GetTrackingChangesInWord did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


async def _call_get_tracking_changes_api(
    doc_name: str,
    doc_content_base64: str,
    pdf4me_api_key: str,
) -> httpx.Response:
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/GetTrackingChangesInWord"
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
        "isAsync": True,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 202:
            location = resp.headers.get("Location")
            if not location:
                raise ValueError(
                    "API returned 202 Accepted but no Location header for polling"
                )
            poll_url = resolve_polling_url(api_base_url, location)
            return await _poll_get_tracking_changes_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
        resp.raise_for_status()
        return resp


@tool(
    name="get_tracking_changes_in_word",
    title="Get Tracking Changes In Word",
    description=(
        "Extract tracking changes data from a Word file via PDF4me "
        "/api/v2/GetTrackingChangesInWord. Input is a local .docx/.doc file. "
        "Saves the API response as JSON (or raw text fallback)."
    ),
)
async def get_tracking_changes_in_word(
    word_file_path: str,
    request_doc_name: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        doc_b64, ext = file_to_base64(word_file_path)
    except OSError as exc:
        return ToolResult(content=f"Could not read Word file: {exc}")

    if ext.lower() not in (".docx", ".doc"):
        return ToolResult(
            content=f"Source file must be a Word document (.docx/.doc), got '{ext}'."
        )

    doc_name = (request_doc_name or os.path.basename(word_file_path)).strip()
    if not doc_name.lower().endswith((".docx", ".doc")):
        doc_name = f"{doc_name}.docx"

    resolved_output_dir = (
        output_dir if output_dir else os.path.dirname(
            os.path.abspath(word_file_path))
    )
    resolved_output_name = (
        output_file_name if output_file_name else f"{os.path.splitext(doc_name)[0]}.tracking_changes.json"
    )

    try:
        resp = await _call_get_tracking_changes_api(doc_name, doc_b64, pdf4me_api_key)
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

    os.makedirs(resolved_output_dir, exist_ok=True)
    output_path = os.path.join(resolved_output_dir, resolved_output_name)

    try:
        parsed: Any = resp.json()
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
    except ValueError:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(resp.text)
        except OSError as exc:
            return ToolResult(
                content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
            )
    except OSError as exc:
        return ToolResult(
            content=f"Failed to write output file in '{resolved_output_dir}': {exc}"
        )

    return ToolResult(
        content=f"Tracking changes extracted successfully. Saved to {output_path}",
        structured_content={"output_path": output_path, "doc_name": doc_name},
    )
