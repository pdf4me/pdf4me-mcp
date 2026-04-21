import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from config import config


def _default_output_dir() -> str:
    return str(Path.cwd() / "pdf4me_webhook_subscriptions")


@tool(
    name="get_document_from_pdf4me",
    description=(
        "Subscribe a callback webhook via PDF4me WebhookSubscribe (/api/v2/WebhookSubscribe). "
        "Provide callback_url and doc_name. Saves API response as JSON."
    ),
)
async def get_document_from_pdf4me(
    callback_url: str,
    doc_name: str,
    output_dir: Optional[str] = None,
    output_file_name: str = "webhook_subscribe_response.json",
) -> ToolResult:
    pdf4me_api_key = config.api_key
    if not pdf4me_api_key:
        return ToolResult(content="Authentication failed: no API key provided in the request.")

    payload: dict[str, Any] = {
        "CallBackUrl": callback_url,
        "docName": doc_name,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {pdf4me_api_key}",
    }
    api_base_url = config.pdf4me_base_url.rstrip("/")
    url = f"{api_base_url}/api/v2/WebhookSubscribe"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return ToolResult(content="Authentication failed: the API key is invalid or missing.")
        return ToolResult(content=f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.RequestError as exc:
        return ToolResult(content=f"Request failed: {exc}")

    try:
        response_data: Any = resp.json()
    except ValueError:
        response_data = {"raw_response": resp.text}

    resolved_output_dir = output_dir if output_dir else _default_output_dir()
    os.makedirs(resolved_output_dir, exist_ok=True)
    output_path = os.path.join(resolved_output_dir, output_file_name)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(response_data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        return ToolResult(content=f"Failed to write webhook response JSON: {exc}")

    return ToolResult(
        content=f"Webhook subscription request sent. Response saved to {output_path}",
        structured_content={
            "output_path": output_path,
            "callback_url": callback_url,
            "doc_name": doc_name,
            "response": response_data,
        },
    )
