from fastmcp.resources.function_resource import resource


@resource("info://tools")
def available_tools() -> str:
    """Describes all available tools in this MCP server."""

    return (
        "This MCP server has the following tools:\n"
        "- compress_pdf: Compress a PDF using the PDF4me API. "
        "Accepts a source PDF file path and returns compressed PDF output. "
        "Supports optimization profiles: Web, Print, and Screen.\n"
        "- flatten_pdf: Flatten a PDF using the PDF4me FlattenPdf API. "
        "Accepts a local PDF path; optional async 202 polling, output directory, and file name; saves to disk.\n"
        "- linearize_pdf: Linearize a PDF using the PDF4me LinearizePdf API. "
        "Accepts a local PDF path and saves a linearized PDF. "
        "Supports optimization presets (web, Max, Print, Default, WebMax, PrintMax, PrintGray, Compress, CompressMax), "
        "optional async 202 polling, and optional output directory and file name.\n"
        "- convert_pdf_to_pdfa: Convert a PDF to PDF/A using the PDF4me PdfA API. "
        "Accepts a local PDF path, compliance (PdfA1a–PdfA3u), allow upgrade/downgrade, optional async 202 polling, "
        "and optional output path; saves the PDF/A to disk.\n"
        "- convert_html_to_pdf: Convert a local HTML file (.html/.htm) to PDF using the PDF4me ConvertHtmlToPdf API. "
        "Layout, format, scale, margins, print options, optional async 202 polling (job URL resolved against API base), "
        "optional output directory and file name; saves the PDF to disk.\n"
        "- convert_url_to_pdf: Convert a web URL to PDF using the PDF4me ConvertUrlToPdf API. "
        "Required input is web_url only (no local source file); auth, layout, margins, scale, optional async 202 polling; "
        "optional output directory and file name (supports output_dir/outputDir aliases); saves the PDF to disk.\n"
        "- convert_json_to_excel: Convert a local JSON file to XLSX using the PDF4me ConvertJsonToExcel API. "
        "Worksheet name, title and number/date options, optional async 202 polling (Location resolved to absolute URL), "
        "optional output directory and file name; saves the workbook to disk.\n"
        "- convert_pdf_to_excel: Convert a local PDF file to XLSX using the PDF4me ConvertPdfToExcel API. "
        "Quality (Draft/High), merge sheets, language, OCR when needed, optional async 202 polling (Location resolved), "
        "optional output directory and file name; saves the workbook to disk.\n"
    )
