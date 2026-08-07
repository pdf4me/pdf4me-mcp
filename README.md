# PDF4me MCP Server

<!-- mcp-name: io.github.pdf4me/pdf4me-mcp -->

PDF4me MCP Server provides <a href="https://dev.pdf4me.com" target="_blank" rel="noopener noreferrer">PDF4me API</a> functionality through the Model Context Protocol (MCP), enabling AI assistants to easily perform a wide range of PDF, document, image, and barcode processing tasks.

---

## 🚀 Key Features

### 📄 PDF Conversion
- **PDF → Office**: Convert PDFs to Word (DOCX), Excel (XLSX), PowerPoint (PPTX)
- **PDF → Other Formats**: Convert PDFs to PDF/A, searchable OCR PDF, HTML, Markdown
- **Office → PDF**: Convert DOCX, PPTX, XLSX, CSV, HTML, Markdown, Visio, and images to PDF
- **Word → PDF Form**: Convert Word documents into fillable PDF forms
- **JSON → Excel**: Convert JSON data files into Excel workbooks

### 🛠️ PDF Editing & Modification
- **Stamps & Watermarks**: Add text or image stamps/watermarks to PDFs
- **Headers & Footers**: Inject HTML-based headers and footers
- **Annotations**: Add page numbers, margins, hyperlink annotations
- **Attachments**: Embed any file as an attachment inside a PDF
- **Signing**: Apply signature images to PDF pages
- **Find & Replace**: Search and replace text, or replace text with images

### 📐 PDF Organization
- **Merge**: Combine multiple PDFs or overlay two PDFs
- **Split**: Split by page, page range, barcode, Swiss QR code, or matching text
- **Delete / Extract**: Remove or extract specific pages, delete blank pages
- **Rotate**: Rotate all pages or selected pages of a PDF

### 🔍 PDF Extraction & Analysis
- **Form Data**: Extract values from fillable PDF form fields
- **Tables**: Detect and extract tables with coordinates
- **Text**: Extract text by regex expression; find and replace text
- **Attachments**: Pull embedded file attachments out of a PDF
- **Resources**: Extract text and images from PDF content
- **Classification**: Classify a PDF by document type, category, and confidence

### 🤖 AI Document Processing
- **Invoices, Orders, Receipts**: Structured data extraction using AI
- **Bank Statements & Cheques**: Parse financial documents automatically
- **Contracts & Mortgage Documents**: Extract key fields from legal documents
- **Tax Documents & Pay Stubs**: Parse tax forms and payroll documents
- **ID & Cards**: Process health cards, credit cards, marriage certificates, and shipping labels
- **Universal Extraction**: Generic AI data extraction from any document type

### 🏷️ Barcode & QR Codes
- **Create**: Generate barcodes and QR codes (PNG)
- **Add to PDF**: Stamp a barcode or QR code onto PDF pages
- **Read from PDF / Image**: Detect and decode barcodes and QR codes
- **Swiss QR Bill**: Create and read Swiss QR Bill payment sections; split PDFs by Swiss QR

### 🖼️ Image Processing
- **Format Conversion**: Convert between BMP, GIF, JPG, PNG, TIFF
- **Editing**: Resize, rotate, flip, and crop images
- **Watermarks**: Add image or text watermarks to images
- **OCR**: Extract text from images using OCR
- **Metadata**: Read or strip EXIF metadata
- **Compression**: Compress images with configurable quality levels

### 📝 Word & Document Tools
- **Track Changes**: Enable, disable, or extract tracked changes in Word documents
- **Replace Text with Image**: Substitute text placeholders with images in Word
- **Extract Text**: Pull text from Word documents with header/footer and comment control

### 📦 Document Generation
- **Single Document**: Generate one document from a template and data (JSON, XML, or plain text)
- **Batch Documents**: Generate multiple documents from a single template and dataset

### 🔒 Security
- **Protect PDF**: Apply password protection with configurable permissions
- **Unlock PDF**: Remove password protection from a PDF
- **ZUGFeRD Invoice**: Create standards-compliant ZUGFeRD e-invoices (XML or PDF+XML)

### ⚙️ Utilities
- **Upload File**: Upload local files to PDF4me cloud storage
- **PDF Metadata**: Read full metadata (title, author, page count, security info, dates)
- **Image Metadata**: Read image properties and EXIF fields
- **Repair PDF**: Repair corrupted or malformed PDFs
- **Optimize PDF**: Compress, linearize (web-optimize), or flatten PDFs

---

## ⚙️ Configuration

### 🔑 Get API Key

1. Sign up at <a href="https://dev.pdf4me.com" target="_blank" rel="noopener noreferrer">dev.pdf4me.com</a>
2. Get your API key from the dashboard

### 📦 Install UV

You need <a href="https://docs.astral.sh/uv/" target="_blank" rel="noopener noreferrer">UV</a> (a fast Python packaging tool) to run this MCP server.

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Alternative methods**

```bash
# Homebrew (macOS)
brew install uv

# pipx
pipx install uv

# pip
pip install uv
```

For more options, see the <a href="https://docs.astral.sh/uv/getting-started/installation/" target="_blank" rel="noopener noreferrer">UV installation guide</a>.

---

## 🔧 MCP Client Setup

### Cursor

Open Cursor Settings → MCP, or edit `~/.cursor/mcp.json` (macOS/Linux) / `%USERPROFILE%\.cursor\mcp.json` (Windows):

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "PDF4ME_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Claude Desktop

Open the Claude Desktop config file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "PDF4ME_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### VS Code

Open `~/.config/Code/User/mcp.json` (macOS/Linux) or `%APPDATA%\Code\User\mcp.json` (Windows):

```json
{
  "servers": {
    "pdf4me-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "PDF4ME_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "pdf4me-mcp": {
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "PDF4ME_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Continue (VS Code / JetBrains extension)

Add to your `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "pdf4me-mcp",
      "command": "uvx",
      "args": ["pdf4me-mcp"],
      "env": {
        "PDF4ME_API_KEY": "your-api-key-here"
      }
    }
  ]
}
```

---

## 🪟 Windows Note

On Windows, `uvx` may need to be called with its full path if it is not on your `PATH`. Replace `"command": "uvx"` with the full path, e.g.:

```json
"command": "C:\\Users\\<YourUser>\\.local\\bin\\uvx"
```

---

## 🛠️ Available Tools

### 🏷️ Barcode Tools

| Tool | Description |
|------|-------------|
| `add_barcode_to_pdf` | Draw a barcode or QR code onto PDF pages with configurable alignment and size |
| `create_barcode` | Generate a barcode or QR code as a PNG image (supports Code128, QR, and more) |
| `read_barcodes` | Read and decode barcode/QR data from a local PDF with barcode type and page filtering |
| `read_barcodes_from_image` | Detect and decode barcodes and QR codes from a local image file |
| `create_swiss_qr_bill` | Add a Swiss QR Bill payment section to a PDF (IBAN, creditor details, currency, reference) |
| `read_swiss_qr_bill` | Parse and extract Swiss QR Bill payment data from a local PDF |
| `split_pdf_by_barcode` | Split a PDF into multiple files at pages that contain a specific barcode value or type |
| `split_pdf_by_swiss_qr` | Split a PDF by Swiss QR code positions into separate PDF files or a ZIP archive |

---

### 🔄 Conversion Tools

| Tool | Description |
|------|-------------|
| `convert_to_pdf` | Convert documents (DOCX, PPTX, XLSX, images, text formats) to PDF |
| `convert_html_to_pdf` | Convert a local HTML file to PDF with layout, margins, and print options |
| `convert_url_to_pdf` | Convert a web page URL to PDF with auth, layout, and margin settings |
| `convert_md_to_pdf` | Convert a local Markdown file to PDF |
| `convert_visio_to_pdf` | Convert a Visio file (.vsdx/.vsd/.vsdm) to PDF |
| `convert_word_to_pdf_form` | Convert a Word document (DOCX) into a fillable PDF form |
| `convert_pdf_to_word` | Convert a PDF to DOCX with quality settings, language, and optional OCR |
| `convert_pdf_to_excel` | Convert a PDF to XLSX with quality, merge sheets, language, and optional OCR |
| `convert_pdf_to_powerpoint` | Convert a PDF to PPTX with quality, language, and optional OCR |
| `convert_pdf_to_pdfa` | Convert a PDF to PDF/A (PdfA1a through PdfA3u) with upgrade/downgrade control |
| `convert_ocr_pdf` | Make a scanned PDF searchable by running OCR and embedding a text layer |
| `convert_json_to_excel` | Convert a local JSON file to an Excel workbook (XLSX) with worksheet name and title options |
| `convert_image_format` | Convert images between BMP, GIF, JPG, PNG, and TIFF formats |
| `flatten_pdf` | Flatten a PDF (merge form fields and annotations into the page content) |
| `linearize_pdf` | Linearize (web-optimize) a PDF for fast browser loading with configurable optimization presets |

---

### ✏️ PDF Editing Tools

| Tool | Description |
|------|-------------|
| `add_attachment_to_pdf` | Embed one or more files as attachments inside a PDF |
| `add_html_header_footer_to_pdf` | Add an HTML-formatted header, footer, or both to PDF pages |
| `add_margin_to_pdf` | Add page margins (in millimeters) to all pages of a PDF |
| `add_page_number_to_pdf` | Insert page numbers with configurable alignment, format, and font styling |
| `add_image_stamp_to_pdf` | Place an image stamp or watermark on PDF pages with alignment, opacity, and size controls |
| `add_text_stamp_to_pdf` | Place a text stamp or watermark on PDF pages with font, opacity, rotation, and alignment options |
| `sign_pdf` | Apply a signature image to a PDF with layout and margin options |
| `find_and_replace_text` | Find and replace text across PDF pages |
| `replace_text_with_image` | Replace text occurrences in a PDF with an image at configurable dimensions |
| `update_hyperlink_annotation` | Update or replace hyperlink annotations in a PDF by search criteria |
| `repair_pdf` | Repair a corrupted or malformed PDF file |

---

### 📤 Extract Tools

| Tool | Description |
|------|-------------|
| `extract_pages_from_pdf` | Extract specific pages from a PDF into a new PDF file |
| `extract_form_data_from_pdf` | Extract all form field names and their current values from a PDF |
| `extract_attachment_from_pdf` | Extract embedded file attachments from a PDF and save them to disk |
| `extract_resources` | Extract text content and images embedded in a PDF |
| `extract_table_from_pdf` | Detect and extract tables from a PDF, saving results as JSON and CSV |
| `extract_text_by_expression` | Extract text from a PDF using a regular expression pattern |
| `extract_text_from_word` | Extract text from a Word document, with options to remove headers, footers, or comments |
| `extract_text_from_image` | Extract text from an image using OCR |
| `classify_document` | Classify a PDF by document type and category with a confidence score |
| `parse_document` | Extract structured data from a PDF using a predefined PDF4me parsing template |

---

### 📋 Forms Tools

| Tool | Description |
|------|-------------|
| `add_form_fields_to_pdf` | Add new TextBox or CheckBox form fields to a PDF at specified coordinates |
| `fill_pdf_form` | Fill existing form fields in a PDF using a key/value data map |

---

### 📝 Document Generation Tools

| Tool | Description |
|------|-------------|
| `generate_document_single` | Generate one document from a template (DOCX/HTML/PDF) and a data payload (JSON/XML/text) |
| `generate_documents_multiple` | Generate multiple documents in batch from one template and a multi-record dataset |
| `enable_tracking_changes_in_word` | Enable Track Changes mode in a Word document |
| `disable_tracking_changes_in_word` | Disable Track Changes mode in a Word document |
| `get_tracking_changes_in_word` | Extract all tracked change data from a Word document as structured JSON |
| `replace_text_with_image_in_word` | Replace text placeholders in a Word document with an image |

---

### 🖼️ Image Tools

| Tool | Description |
|------|-------------|
| `add_image_watermark_to_image` | Overlay a watermark image onto another image with opacity and position controls |
| `add_text_watermark_to_image` | Add a text watermark to an image with font, color, opacity, and rotation options |
| `compress_image` | Compress a JPG, PNG, or WebP image with configurable compression level |
| `convert_image_format` | Convert an image between BMP, GIF, JPG, PNG, and TIFF formats |
| `create_images_from_pdf` | Render PDF pages to image files (JPEG, PNG, TIFF) with page selection and width options |
| `crop_image` | Crop an image by border or by a specific rectangle region |
| `flip_image` | Flip an image horizontally or vertically |
| `get_image_metadata` | Extract image properties and EXIF metadata from a JPG or PNG file |
| `extract_text_from_image` | Run OCR on an image to extract its text content |
| `resize_image` | Resize an image by percentage or to specific dimensions, with aspect ratio control |
| `rotate_image` | Rotate an image by a specified angle with background color and resize options |
| `rotate_image_by_exif_data` | Auto-rotate an image to its correct orientation based on its EXIF metadata |
| `remove_exif_tags_from_image` | Strip all EXIF metadata from a JPG or PNG image |

---

### 🔗 Merge & Split Tools

| Tool | Description |
|------|-------------|
| `merge_multiple_pdfs` | Merge two or more PDF files into a single PDF |
| `merge_pdf_overlay` | Overlay one PDF on top of another (e.g. stamp a letterhead) |
| `split_pdf` | Split a PDF by page number, recurring interval, sequence, or page ranges |
| `split_pdf_by_barcode` | Split a PDF at pages containing a specified barcode |
| `split_pdf_by_swiss_qr` | Split a PDF at Swiss QR code positions |
| `split_pdf_by_text` | Split a PDF at pages containing a specific text string |

---

### ⚡ Optimize Tools

| Tool | Description |
|------|-------------|
| `compress_pdf` | Compress a PDF with optimization profiles for Web, Print, or Screen |
| `delete_blank_pages_from_pdf` | Remove blank pages from a PDF (no text, no images, or both) |
| `linearize_pdf` | Linearize a PDF for fast incremental loading in web browsers |

---

### 📂 Organize Tools

| Tool | Description |
|------|-------------|
| `delete_pdf_pages` | Remove specific pages from a PDF by page number or range |
| `extract_pages_from_pdf` | Extract selected pages from a PDF into a new file |
| `rotate_pdf` | Rotate all pages of a PDF (Clockwise, CounterClockwise, UpsideDown) |
| `rotate_pdf_page` | Rotate selected pages of a PDF independently |

---

### 📊 PDF Information Tools

| Tool | Description |
|------|-------------|
| `get_pdf_metadata` | Read full PDF metadata: title, author, page count, security info, and dates |
| `get_image_metadata` | Read image properties and available EXIF fields from a JPG or PNG |
| `repair_pdf` | Attempt to repair a corrupted or malformed PDF |

---

### 🔒 Security Tools

| Tool | Description |
|------|-------------|
| `protect_pdf` | Password-protect a PDF with configurable permissions (print, copy, edit, etc.) |
| `unlock_pdf` | Remove password protection from a PDF using the current password |

---

### 🤖 AI Document Processing Tools

| Tool | Description |
|------|-------------|
| `process_invoice` | Extract structured invoice data (line items, totals, dates, vendor info) from a PDF or image using AI |
| `process_bank_cheque` | Parse bank cheque images and extract cheque details using AI |
| `process_contract` | Extract key clauses and fields from contract documents using AI |
| `process_credit_card` | Extract credit card information from card images using AI |
| `process_health_card` | Extract data from health insurance card images using AI |
| `process_marriage_certificate` | Extract fields from marriage certificates using AI, with optional authenticity verification |
| `process_mortgage_document` | Extract key financial and property details from mortgage documents using AI |
| `process_pay_stub` | Extract earnings, deductions, and employee details from pay stub documents using AI |
| `process_bank_statement` | Parse bank statements for transactions, balances, and patterns using AI |
| `process_order` | Extract order details (items, quantities, prices, addresses) from purchase order documents using AI |
| `process_receipt` | Extract merchant info, line items, and totals from receipt images or PDFs using AI |
| `process_shipping_label` | Extract carrier, tracking number, and address data from shipping labels using AI |
| `process_tax_document` | Extract tax form fields and computed values from tax documents using AI |
| `process_universal_document` | Extract any specified fields from any document type using AI (configurable field list) |

---

### 📜 ZUGFeRD / E-Invoice Tools

| Tool | Description |
|------|-------------|
| `create_zugferd_invoice` | Create a ZUGFeRD-compliant e-invoice from XML, JSON, or CSV data — as PDF+XML or XML-only |

---

### 🗂️ File Management Tools

| Tool | Description |
|------|-------------|
| `upload_file` | Upload a local file to PDF4me cloud storage and receive a file reference for further operations |
| `get_document_from_pdf4me` | Register a webhook callback URL to receive documents from PDF4me |

---

## 💡 Usage Examples

**Convert a PDF to Word:**
```
Convert this PDF to a Word document: /path/to/document.pdf
```

**Merge multiple PDFs:**
```
Merge these PDFs into one: report.pdf, appendix.pdf, cover.pdf
```

**Extract invoice data using AI:**
```
Extract all invoice details from this file: invoice.pdf
```

**Add a Swiss QR Bill to a PDF:**
```
Add a Swiss QR Bill to invoice.pdf with IBAN CH93-0076-2011-6238-5295-7, creditor name "Acme AG"
```

**Split a PDF by barcode:**
```
Split this PDF at every page containing a Code128 barcode: batch.pdf
```

**Generate a document from a template:**
```
Generate a contract PDF from contract_template.docx with this data: {"client": "ACME", "date": "2024-01-01"}
```

**Compress an image:**
```
Compress this PNG image with maximum compression: photo.png
```

**OCR a scanned PDF:**
```
Make this scanned PDF searchable: scanned_report.pdf
```

---

## 🛠️ Manual Run (without a client)

```bash
uvx pdf4me-mcp
```

Or, if installed locally:

```bash
PDF4ME_API_KEY=your-api-key-here pdf4me-mcp
```

---

## 📞 Support & Contact

- **PDF4me**: <a href="https://dev.pdf4me.com" target="_blank" rel="noopener noreferrer">pdf4me.com</a>
- **API Documentation**: <a href="https://docs.pdf4me.com" target="_blank" rel="noopener noreferrer">docs.pdf4me.com</a>
- **Issue Reports**: <a href="https://github.com/pdf4me/pdf4me-mcp/issues" target="_blank" rel="noopener noreferrer">GitHub Issues</a>

---

## 📄 License

This project is distributed under the MIT License.
