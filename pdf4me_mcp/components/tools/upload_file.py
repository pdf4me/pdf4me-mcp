import json
import os
from typing import Any

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64


def _upload_data_from_response(resp: httpx.Response) -> dict[str, Any]:
    """Parse UploadFile response as a JSON object ."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.content

    if "application/json" in ct:
        parsed = resp.json()
    else:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text.startswith(("{", "[")):
            raise ValueError(f"Unexpected API response content type: {ct}")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in response: {exc}") from exc

    if isinstance(parsed, dict):
        return parsed
    raise ValueError("Expected JSON object for UploadFile response.")


async def _call_upload_file_api(
    doc_content_base64: str,
    doc_name: str,
    PDF4ME_API_KEY: str,
) -> dict[str, Any]:
    """POST UploadFile and return parsed API response."""
    payload = {
        "docName": doc_name,
        "docContent": doc_content_base64,
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/UploadFile",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return _upload_data_from_response(resp)


@tool(
    name="upload_file",
    description=(
        "Upload a local file to PDF4me storage using the UploadFile API "
        "(POST /api/v2/UploadFile). Sends payload with docName and docContent (base64). "
        "Returns the API response JSON/text (for example uploaded file reference details)."
    ),
)
async def upload_file_http(file_path: str) -> ToolResult:
    """Upload a local file to PDF4me using docName and docContent.

    Args:
        file_path: Local path to any file to upload.
    """
    doc_content_base64, _ = file_to_base64(file_path)
    doc_name = os.path.basename(file_path)

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        upload_result = await _call_upload_file_api(
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
    except ValueError as exc:
        return ToolResult(content=str(exc))

    if not upload_result:
        return ToolResult(content="Unexpected API response — no upload result returned.")

    return ToolResult(
        content="File uploaded successfully.",
        structured_content={
            "file_name": doc_name,
            "upload_result": upload_result,
        },
    )
