import asyncio
import os
from enum import StrEnum
from typing import Any, Literal, Optional, TypeVar

import httpx

from fastmcp.tools import ToolResult
from fastmcp.tools.function_tool import tool

from pdf4me_mcp.config import config
from pdf4me_mcp.helper import file_to_base64, resolve_polling_url, write_file_from_bytes

_HTTP_TIMEOUT = httpx.Timeout(
    connect=120.0, read=900.0, write=900.0, pool=120.0)
_ASYNC_POLL_MAX_ATTEMPTS = 40
_ASYNC_POLL_INTERVAL_SEC = 2.5


# OpenAPI / Power Automate enums (same strings as YAML).
class AddressTypeSK(StrEnum):
    """crAddressType and udAddressType."""

    S = "S"
    K = "K"


class CurrencyCode(StrEnum):
    """currency."""

    CHF = "CHF"
    EUR = "EUR"


class LanguageType(StrEnum):
    """languageType."""

    GERMAN = "German"
    FRENCH = "French"
    ITALIAN = "Italian"
    ENGLISH = "English"


class ReferenceType(StrEnum):
    """referenceType."""

    QRR = "QRR"
    SCOR = "SCOR"
    NON = "NON"


class SeperatorLine(StrEnum):
    """seperatorLine (API spelling). Third value is the string \"None\", not JSON null."""

    LINE_WITH_SCISSOR = "LineWithScissor"
    LINE = "Line"
    NONE = "None"


LAddressType = Literal["S", "K"]
LCurrency = Literal["CHF", "EUR"]
LLanguageType = Literal["German", "French", "Italian", "English"]
LReferenceType = Literal["QRR", "SCOR", "NON"]
LSeperatorLine = Literal["LineWithScissor", "Line", "None"]

_SWISS_QR_DEFAULT_OUTPUT_BASENAME = "swiss_qr_bill.pdf"


_E = TypeVar("_E", bound=StrEnum)


def _coerce_enum(
    cls: type[_E],
    raw: Any,
    field_label: str,
    optional: bool,
) -> tuple[Optional[_E], Optional[ToolResult]]:
    """Validate JSON/MCP string inputs against backend enum values."""
    if raw is None:
        return (None, None) if optional else (None, ToolResult(content=f"{field_label} is required."))
    if isinstance(raw, cls):
        return raw, None
    try:
        return cls(str(raw).strip()), None
    except ValueError:
        allowed = ", ".join(sorted(e.value for e in cls))
        return None, ToolResult(
            content=f"{field_label} must be one of: {allowed} (backend OpenAPI enum)."
        )


def _output_headers_from_response(resp: httpx.Response) -> tuple[Optional[str], Optional[str]]:
    """OpenAPI 200 headers: FileName, PageCount (case-insensitive lookup)."""
    fn = resp.headers.get("FileName") or resp.headers.get("filename")
    pc = resp.headers.get("PageCount") or resp.headers.get("pagecount")
    return (
        fn.strip() if isinstance(fn, str) and fn.strip() else None,
        pc.strip() if isinstance(pc, str) and pc.strip() else None,
    )


def _bytes_from_pdf_response(resp: httpx.Response) -> bytes:
    ct = (resp.headers.get("content-type") or "").lower()
    if "application/pdf" in ct or "application/octet-stream" in ct:
        return resp.content
    raise ValueError(f"Expected PDF binary response, got content-type {ct!r}")


def _strip_opt(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    t = s.strip()
    return t if t else None


def _build_payload(
    doc_content: str,
    document_name: str,
    iban: str,
    cr_name: str,
    cr_address_type: AddressTypeSK,
    amount: Optional[str] = None,
    av1_parameters: Optional[str] = None,
    av2_parameters: Optional[str] = None,
    billing_info: Optional[str] = None,
    cr_city: Optional[str] = None,
    cr_postal_code: Optional[str] = None,
    cr_street_or_address_line1: Optional[str] = None,
    cr_street_or_address_line2: Optional[str] = None,
    currency: Optional[CurrencyCode] = None,
    language_type: Optional[LanguageType] = None,
    reference: Optional[str] = None,
    reference_type: Optional[ReferenceType] = None,
    seperator_line: Optional[SeperatorLine] = None,
    ud_address_type: Optional[AddressTypeSK] = None,
    ud_city: Optional[str] = None,
    ud_name: Optional[str] = None,
    ud_postal_code: Optional[str] = None,
    ud_street_or_address_line1: Optional[str] = None,
    ud_street_or_address_line2: Optional[str] = None,
    unstructured_message: Optional[str] = None,
) -> dict[str, Any]:
    """Body keys match OpenAPI CreateSwissQrBill / Power Automate schema (no docName)."""
    body: dict[str, Any] = {
        "docContent": doc_content,
        "document": {"Name": document_name},
        "iban": iban.strip(),
        "crName": cr_name.strip(),
        "crAddressType": cr_address_type,
    }
    opt: list[tuple[str, Any]] = [
        ("amount", _strip_opt(amount)),
        ("av1Parameters", _strip_opt(av1_parameters)),
        ("av2Parameters", _strip_opt(av2_parameters)),
        ("billingInfo", _strip_opt(billing_info)),
        ("crCity", _strip_opt(cr_city)),
        ("crPostalCode", _strip_opt(cr_postal_code)),
        ("crStreetOrAddressLine1", _strip_opt(cr_street_or_address_line1)),
        ("crStreetOrAddressLine2", _strip_opt(cr_street_or_address_line2)),
        ("currency", currency),
        ("languageType", language_type),
        ("reference", _strip_opt(reference)),
        ("referenceType", reference_type),
        ("seperatorLine", seperator_line),
        ("udAddressType", ud_address_type),
        ("udCity", _strip_opt(ud_city)),
        ("udName", _strip_opt(ud_name)),
        ("udPostalCode", _strip_opt(ud_postal_code)),
        ("udStreetOrAddressLine1", _strip_opt(ud_street_or_address_line1)),
        ("udStreetOrAddressLine2", _strip_opt(ud_street_or_address_line2)),
        ("unstructuredMessage", _strip_opt(unstructured_message)),
    ]
    for key, val in opt:
        if val is None:
            continue
        if isinstance(val, str) and not val:
            continue
        body[key] = val
    return body


async def _call_create_swiss_qr_bill_api(
    payload: dict[str, Any],
    PDF4ME_API_KEY: str,
) -> tuple[bytes, Optional[str], Optional[str]]:
    """POST CreateSwissQrBill; return PDF bytes and optional FileName / PageCount headers."""
    api_base_url = config.pdf4me_base_url.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {PDF4ME_API_KEY}",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{api_base_url}/api/v2/CreateSwissQrBill",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 202:
            location = (
                resp.headers.get("Location") or resp.headers.get(
                    "location") or ""
            ).strip()
            if not location:
                raise ValueError(
                    "API returned 202 but no Location header for polling")
            poll_url = resolve_polling_url(api_base_url, location)
            body, fname, pcount = await _poll_create_swiss_qr_bill_job(
                client,
                poll_url,
                headers,
                max_attempts=_ASYNC_POLL_MAX_ATTEMPTS,
                interval_sec=_ASYNC_POLL_INTERVAL_SEC,
            )
            return body, fname, pcount
        resp.raise_for_status()
        fname, pcount = _output_headers_from_response(resp)
        return _bytes_from_pdf_response(resp), fname, pcount


async def _poll_create_swiss_qr_bill_job(
    client: httpx.AsyncClient,
    location_url: str,
    headers: dict[str, str],
    max_attempts: int,
    interval_sec: float,
) -> tuple[bytes, Optional[str], Optional[str]]:
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(interval_sec)
        poll = await client.get(location_url, headers=headers)
        if poll.status_code == 200:
            fname, pcount = _output_headers_from_response(poll)
            return _bytes_from_pdf_response(poll), fname, pcount
        if poll.status_code == 202:
            continue
        poll.raise_for_status()
    raise TimeoutError(
        "CreateSwissQrBill did not finish after "
        f"{max_attempts} polls ({interval_sec}s apart)"
    )


@tool(
    name="create_swiss_qr_bill",
    description=(
        "Create the Swiss QR bill payment part on a PDF using the PDF4me CreateSwissQrBill API "
        "(POST /api/v2/CreateSwissQrBill). "
        "Required inputs per OpenAPI: docContent, document.Name, iban, crName, crAddressType. "
        "Enums match backend: address S|K; currency CHF|EUR; language German|French|Italian|English; "
        "referenceType QRR|SCOR|NON; seperatorLine LineWithScissor|Line|None (string None). "
        "All other schema fields are optional (amount, creditor/debtor address lines, reference, etc.). "
        "Reads the source PDF from pdf_file_path (docContent base64); optional document_name overrides "
        "document.Name (defaults to the file basename). Output PDF name is output_file_name if set, "
        f"otherwise {_SWISS_QR_DEFAULT_OUTPUT_BASENAME!r}. Saves next to the PDF unless output_dir is set; "
        "optional isAsync / 202 polling."
    ),
)
async def create_swiss_qr_bill_http(
    pdf_file_path: str,
    iban: str,
    cr_name: str,
    document_name: Optional[str] = None,
    cr_address_type: LAddressType = "S",
    amount: Optional[str] = None,
    av1_parameters: Optional[str] = None,
    av2_parameters: Optional[str] = None,
    billing_info: Optional[str] = None,
    cr_city: Optional[str] = None,
    cr_postal_code: Optional[str] = None,
    cr_street_or_address_line1: Optional[str] = None,
    cr_street_or_address_line2: Optional[str] = None,
    currency: LCurrency = "CHF",
    language_type: LLanguageType = "English",
    reference: Optional[str] = None,
    reference_type: LReferenceType = "QRR",
    seperator_line: LSeperatorLine = "LineWithScissor",
    ud_address_type: Optional[LAddressType] = None,
    ud_city: Optional[str] = None,
    ud_name: Optional[str] = None,
    ud_postal_code: Optional[str] = None,
    ud_street_or_address_line1: Optional[str] = None,
    ud_street_or_address_line2: Optional[str] = None,
    unstructured_message: Optional[str] = None,
    output_dir: Optional[str] = None,
    output_file_name: Optional[str] = None,
) -> ToolResult:
    """Create Swiss QR bill on a PDF (OpenAPI body field names).

    docContent is read from pdf_file_path (base64). Required by API: docContent, document.Name,
    iban, crName, crAddressType.
    """
    if not pdf_file_path or not pdf_file_path.strip():
        return ToolResult(content="pdf_file_path is required.")
    if not iban.strip():
        return ToolResult(content="iban is required.")
    if not cr_name.strip():
        return ToolResult(content="cr_name is required.")

    cr_at, err = _coerce_enum(
        AddressTypeSK, cr_address_type, "cr_address_type", optional=False)
    if err:
        return err
    assert cr_at is not None
    curr, err = _coerce_enum(CurrencyCode, currency, "currency", optional=True)
    if err:
        return err
    lang, err = _coerce_enum(LanguageType, language_type,
                             "language_type", optional=True)
    if err:
        return err
    rt, err = _coerce_enum(ReferenceType, reference_type,
                           "reference_type", optional=True)
    if err:
        return err
    sep, err = _coerce_enum(SeperatorLine, seperator_line,
                            "seperator_line", optional=True)
    if err:
        return err
    ud_at, err = _coerce_enum(
        AddressTypeSK, ud_address_type, "ud_address_type", optional=True)
    if err:
        return err

    try:
        resolved_doc_content, ext = file_to_base64(pdf_file_path.strip())
    except FileNotFoundError:
        return ToolResult(content=f"PDF file not found: {pdf_file_path}")
    except OSError as exc:
        return ToolResult(content=f"Failed to read PDF file '{pdf_file_path}': {exc}")
    if ext.lower() != ".pdf":
        return ToolResult(content=f"pdf_file_path must be a PDF, got '{ext}' instead.")

    resolved_document_name = (
        document_name.strip()
        if document_name and document_name.strip()
        else os.path.basename(pdf_file_path.strip())
    )

    payload = _build_payload(
        doc_content=resolved_doc_content,
        document_name=resolved_document_name,
        iban=iban,
        cr_name=cr_name,
        cr_address_type=cr_at,
        amount=amount,
        av1_parameters=av1_parameters,
        av2_parameters=av2_parameters,
        billing_info=billing_info,
        cr_city=cr_city,
        cr_postal_code=cr_postal_code,
        cr_street_or_address_line1=cr_street_or_address_line1,
        cr_street_or_address_line2=cr_street_or_address_line2,
        currency=curr,
        language_type=lang,
        reference=reference,
        reference_type=rt,
        seperator_line=sep,
        ud_address_type=ud_at,
        ud_city=ud_city,
        ud_name=ud_name,
        ud_postal_code=ud_postal_code,
        ud_street_or_address_line1=ud_street_or_address_line1,
        ud_street_or_address_line2=ud_street_or_address_line2,
        unstructured_message=unstructured_message,
    )
    payload["isAsync"] = True

    resolved_output_dir = output_dir
    if not resolved_output_dir:
        resolved_output_dir = os.path.dirname(
            os.path.abspath(pdf_file_path.strip()))

    PDF4ME_API_KEY = config.api_key
    if not PDF4ME_API_KEY:
        return ToolResult(
            content="Authentication failed: no API key provided in the request."
        )

    try:
        pdf_bytes, header_file_name, header_page_count = await _call_create_swiss_qr_bill_api(
            payload, PDF4ME_API_KEY
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

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        return ToolResult(
            content="Unexpected API response — PDF bytes missing or invalid after CreateSwissQrBill."
        )

    if output_file_name and output_file_name.strip():
        resolved_output_name = output_file_name.strip()
    else:
        resolved_output_name = _SWISS_QR_DEFAULT_OUTPUT_BASENAME
    if not resolved_output_name.lower().endswith(".pdf"):
        resolved_output_name = f"{resolved_output_name}.pdf"

    output_path = os.path.join(resolved_output_dir, resolved_output_name)
    try:
        write_file_from_bytes(
            pdf_bytes, resolved_output_dir, resolved_output_name)
    except OSError as exc:
        return ToolResult(content=f"Failed to write output file '{output_path}': {exc}")

    meta: dict[str, Any] = {"output_path": output_path}
    if header_file_name:
        meta["response_file_name"] = header_file_name
    if header_page_count:
        meta["response_page_count"] = header_page_count

    return ToolResult(
        content=f"Swiss QR bill PDF created successfully. Saved to {output_path}",
        structured_content=meta,
    )
