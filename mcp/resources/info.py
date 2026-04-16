from fastmcp.resources.function_resource import resource


@resource("info://tools")
def available_tools() -> str:
    """Describes all available tools in this MCP server."""

    return (
        "This MCP server has the following tools:\n"
        "- compress_pdf: Compress a PDF using the PDF4me API. "
        "Accepts a source PDF file path and returns compressed PDF output. "
        "Supports optimization profiles: Web, Print, and Screen.\n"
        "- create_barcode: Create a barcode or QR code PNG via PDF4me CreateBarcode. "
        "Parameters: text, barcodeType (e.g. qrCode, code128), hideText; optional output path.\n"
        "- add_attachment_to_pdf: Embed files into a PDF via AddAttachmentToPdf. "
        "pdf_file_path, attachment_file_paths (list), optional request_doc_name, output path.\n"
        "- add_barcode_to_pdf: Draw a barcode/QR on PDF pages via /api/v2/addbarcode. "
        "pdf_file_path, text, barcodeType, pages, alignX, alignY, hideText; optional sizing.\n"
        "- add_form_fields_to_pdf: Add a TextBox or CheckBox field via /api/v2/AddFormField. "
        "pdf_file_path, field_name, initial_value, position_x/y, size, pages, form_field_type.\n"
        "- add_html_header_footer_to_pdf: Add HTML header/footer via /api/v2/AddHtmlHeaderFooter. "
        "pdf_file_path, html_content, header_footer_location (Header/Footer/Both); optional pages, margins.\n"
        "- add_image_stamp_to_pdf: Image stamp/watermark via /api/v2/ImageStamp. "
        "pdf_file_path, image_file_path, alignX, alignY; optional pages, sizes, margins, opacity.\n"
        "- add_image_watermark_to_image: Overlay watermark image on image via AddImageWatermarkToImage. "
        "image_file_path, watermark_image_file_path, position; optional opacity, offsets, position_x/y, rotation.\n"
    )
