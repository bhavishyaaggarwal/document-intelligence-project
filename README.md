# Document Intelligence — AI Engineer Internship Case Study

A practical FastAPI-based document extraction and financial validation application for invoices and consolidated financial statements.

## Scope

The implementation follows the supplied assessment requirements: PDF/JPG/PNG input, maximum 3 pages, four document types, OCR for scanned/image documents, structured extraction, financial validation, database persistence, REST APIs, dashboard, tests, and deployment configuration.

The supplied dataset contains **20 invoice JPGs** plus **30 financial-statement PDFs** covering Balance Sheet, Profit & Loss, and Cash Flow Statement documents for 2017–2026. Visual inspection shows substantial variation: small thermal receipts, photographed/rotated receipts, scanned invoices, clean business invoices, and image-only financial statement pages. Most financial PDFs are image-based; one 2022 cash-flow PDF exposes selectable text, while other files require OCR.

## Architecture

See `docs/ARCHITECTURE.md`.

The main processing path is:

`upload → validation → native text/OCR → structured extraction → Pydantic validation → financial validation → persistence → API/dashboard`

### Why these technologies?

- **FastAPI**: concise REST API, automatic OpenAPI/Swagger, good multipart handling.
- **PyMuPDF**: lightweight PDF inspection/rendering and native text extraction.
- **Tesseract**: free/local OCR fallback for scanned PDFs and image uploads.
- **Qwen2.5-VL 3B via Ollama**: local multimodal structured extraction for invoices and financial statements. OCR text is supplied alongside rendered page images so the model has both textual and visual context.
- **Gemini 3.5 Flash vision API (optional/hosted)**: multimodal extraction for the public deployment. The API key is read only from `GEMINI_API_KEY`; no secret is committed.
- **OpenAI vision API (optional)**: retained as a provider for environments where an API key is intentionally configured.
- **Pydantic**: enforces the shape of structured extraction and API responses.
- **SQLAlchemy + SQLite**: minimal persistence for local development. `DATABASE_URL` is configurable so a managed PostgreSQL database can be used for deployment.
- **Vanilla HTML/CSS/JS**: sufficient for the required dashboard and keeps the assessment easy to explain.

## Extraction strategy

1. Validate extension, size, readability and PDF page count.
2. For PDFs, use native PDF text when enough selectable text is present.
3. Otherwise render each page and run Tesseract OCR. Image EXIF orientation is respected and OCR orientation detection is attempted.
4. Send OCR text plus page images to the configured vision provider (Gemini 3.5 Flash for hosted use, or local Qwen2.5-VL through Ollama) using a JSON-only extraction prompt.
5. For invoices, use Tesseract word bounding boxes to reconstruct Quantity/Price/Total column associations and perform a high-resolution retry when decimal punctuation is lost. This coordinate layer corrects visual column association without calculating missing values.
6. Normalize common VLM value variants and validate the returned JSON with Pydantic.
7. If the selected AI provider is unavailable, fall back to deterministic OCR parsing rather than returning fake/predefined dataset answers.

The fallback is deliberately conservative. It is a recovery path, not a claim that regex/OCR can match a vision model on every invoice layout.

### Hallucination controls

- Missing/unreadable values should be `null`.
- The extraction prompt explicitly forbids unsupported inference.
- Evidence includes source text and page number when available.
- Financial calculations operate only on extracted fields; missing inputs yield `NOT_APPLICABLE`.
- No dataset filename is used to select a hardcoded answer.

Confidence scoring is intentionally **not implemented** because arbitrary LLM confidence values would be misleading.

## Financial validation

Validation checks return:

- check name
- formula
- operands
- calculated value
- reported value
- variance
- PASS / FAIL / NOT_APPLICABLE
- comparative period where applicable

Tolerance is configurable through `VALIDATION_TOLERANCE` and defaults to an **absolute 0.01 monetary-unit tolerance**. A $1 discrepancy therefore remains a failure even on a large invoice. Parenthesized values are negative.

Supported checks include:

### Invoice
- quantity × unit price ≈ line amount
- sum of line items ≈ subtotal
- subtotal + tax + shipping/handling − discount ≈ total where all inputs exist
- line items + tax ≈ total where subtotal is unavailable
- cash paid − total ≈ change

GST/tax-inclusive receipts are not forced into a tax-exclusive formula when the required separate fields are absent.

### Balance Sheet
- total capital & liabilities ≈ total assets, independently per period

### Profit & Loss
- interest earned + other income ≈ total income
- interest expended + operating expenses + provisions & contingencies ≈ total expenditure
- profit before minority interest − minority interest ≈ group net profit

### Cash Flow
- operating + investing + financing + FX/translation adjustment ≈ net increase in cash
- opening cash + net increase ≈ closing cash

The validator does not manufacture missing inputs.

## API

Base path: `/api/v1`

### Health

`GET /api/v1/health`

### Process

`POST /api/v1/documents/process`

`multipart/form-data`:
- `file`: PDF/JPG/PNG
- `document_type`: `invoice`, `balance_sheet`, `profit_and_loss`, `cash_flow_statement`

Example:

```bash
curl -X POST "http://localhost:8000/api/v1/documents/process" \
  -F "file=@sample_invoice.jpg" \
  -F "document_type=invoice"
```

### Retrieve latest result

`GET /api/v1/documents/{document_name}`

### Dashboard list

`GET /api/v1/documents`

### Swagger

`/docs`

Error responses use a controlled structure such as:

```json
{
  "error": {
    "code": "UNSUPPORTED_FILE_TYPE",
    "message": "Only PDF / JPG / PNG documents are supported."
  }
}
```

## Response shape

The processing response contains:

- `document_name`
- `document_type`
- `processing_status`
- `file_validation`
- `extracted_data`
- `validation`
- `processing_metadata`

`processing_metadata` records whether OCR was used, processing timestamp, processing time, extraction method, and pages processed.

## Local setup

### 1. Install Tesseract

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
```

Windows: install Tesseract and ensure `tesseract.exe` is on PATH.

### 2. Create environment

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Install Ollama and pull `qwen2.5vl:3b` for local development. For hosted Gemini extraction, set `GEMINI_API_KEY` and `EXTRACTION_PROVIDER=gemini`. Keep all API keys out of Git.

### 4. Run

```bash
cd backend
uvicorn app.main:app --reload
```

Open the application at `http://localhost:8000` and Swagger at `http://localhost:8000/docs`.

## Database / persistence

Local default: SQLite file `document_intelligence.db`.

The repository upserts by document name, so a later processing run becomes the latest result returned by `GET /api/v1/documents/{document_name}`. Historical versions are not retained, which is allowed by the case study.

For a public deployment, configure `DATABASE_URL` to a managed PostgreSQL database if durable persistence across service restarts is required. SQLite on ephemeral hosting is suitable for a short-lived demo but should not be presented as durable production storage.

## Deployment

A Dockerfile is included and installs Tesseract. `render.yaml` deploys the dashboard and API together as one free Render web service and connects it to a managed Render PostgreSQL database. No secret is committed. The Render Blueprint injects the database connection string. The public Render configuration uses Gemini 3.5 Flash when `GEMINI_API_KEY` is supplied; it falls back safely to OCR if Gemini is unavailable. See `docs/DEPLOYMENT.md` for provider configuration.

The Render free database is suitable for this submission/demo but currently expires after 30 days and has no backups. Upgrade it or use another managed PostgreSQL provider before treating it as long-term production storage. The free Render web service does not host Qwen2.5-VL; the public Blueprint therefore uses Gemini when configured, with OCR fallback. See `docs/DEPLOYMENT.md`.

The project intentionally does **not** claim a live deployment URL. Replace the placeholders below only after the URLs have actually been deployed and tested.

- Frontend: `<LIVE_FRONTEND_URL>`
- Backend: `<LIVE_BACKEND_URL>`
- Swagger: `<LIVE_BACKEND_URL>/docs`
- GitHub: `<PUBLIC_GITHUB_REPOSITORY_URL>`

For deployment:
1. Push the repository to a public GitHub repository.
2. Create the web service from the repository.
3. For hosted AI extraction, configure `EXTRACTION_PROVIDER=gemini` and `GEMINI_API_KEY`. For a self-hosted local Qwen deployment, use `EXTRACTION_PROVIDER=ollama` and a reachable `OLLAMA_BASE_URL`.
4. Configure a durable `DATABASE_URL` if the platform filesystem is ephemeral.
5. Verify `/api/v1/health`, `/docs`, upload processing and dashboard persistence using the live URL.
6. Only then add the live URLs to this README.

## Testing

Run:

```bash
cd backend
pytest -q
```

Automated tests cover input validation, empty/unsupported uploads, financial calculations including negative values, NOT_APPLICABLE behavior, schema validation, and an API flow.

The supplied dataset should be used for manual/integration evaluation. Recommended cases:
- one invoice receipt
- one clean business invoice
- one rotated/photographed invoice
- one balance sheet
- one profit & loss
- one cash flow statement
- one scanned/image-only PDF
- one validation failure
- one missing/unreadable field
- one unsupported file

Sample outputs in `sample_outputs/` are generated from actual local processing during project preparation; they are not hand-written expected answers.

## Dataset observations

The financial statement set contains one page Balance Sheets and Profit & Loss statements, while Cash Flow statements vary between one and two pages. The statements commonly contain two comparative periods and change presentation from thousands in older documents to crore in newer documents. Parentheses appear for negative values.

The invoice set ranges from thermal receipt-like images to clean multi-column invoices. Several Malaysian GST-era receipts use RM/MYR and tax-inclusive totals, while the business invoices include fields such as seller/client, tax IDs, due dates, shipping, VAT/tax and line-item tables.

These observations informed the pipeline but are not hardcoded into extraction decisions.

## Known limitations

- Tesseract fallback can struggle with severely blurred, rotated or low-contrast receipts.
- OCR errors can affect downstream financial calculations.
- The local Qwen2.5-VL path requires Ollama with `qwen2.5vl:3b` available.
- The Gemini path requires a configured `GEMINI_API_KEY` and an API-enabled Gemini model.
- The optional OpenAI path requires a configured API key and an available compatible model.
- The simple local fallback parser is intentionally less complete than the vision path.
- SQLite is not durable on an ephemeral deployment filesystem.
- This assessment prototype does not include authentication, asynchronous job queues, object storage or human review workflows.

## Production improvements

- Managed PostgreSQL plus object storage for source documents.
- Queue-based asynchronous processing for larger documents.
- Dedicated OCR/document AI service with confidence and bounding boxes.
- Page/field-level provenance using coordinates.
- Human review for low-quality or failed extraction.
- Rate limiting, authentication and authorization.
- Virus/malware scanning and stricter upload limits.
- Migration tooling and database connection pooling.
- Observability with structured logs, metrics and tracing.
- Versioned extraction schemas and model/prompt evaluation.

## AI/tool usage declaration

AI coding assistants were used during development for code drafting, debugging assistance, design discussion, test generation/review and documentation. The submitted implementation was inspected and locally tested; generated code is not treated as evidence that an external service or deployment works without verification.

## Assessment deliverables

The repository includes:
- source code
- tests
- README
- architecture documentation
- deployment configuration
- sample output directory
- presentation source in `docs/solution_presentation.pptx`

A live deployment URL and public GitHub URL are intentionally left as placeholders until they actually exist.


## v6 coordinate-aware invoice guardrail
For invoices, the pipeline uses OCR bounding-box geometry as the final authority for visible Quantity, Price, and Total cells. VLM output supplies semantic descriptions and other fields, but numeric table associations are corrected from visual row/column coordinates. The system never calculates a printed Total from quantity × price and preserves source-document inconsistencies for validation.


## Financial table extraction safeguards
The extraction pipeline preserves comparative period order, parenthesized negative values, statement units, and structured cash-flow rows. Financial validation is performed independently and may legitimately return FAIL when source calculations are inconsistent.

## V11 extraction fixes

V11.3 (`v11.3-gemini-vision`) keeps Gemini 3.5 Flash as the primary multimodal extractor and adds post-processing fixes for comparative financial statements. Balance-sheet `Total` rows are mapped to their section semantics, P&L income/expenditure totals are resolved using OCR section context, and cash-flow operating/investing labels with `(used in)` wording are canonicalized so validation can use the reported values. No missing financial values are calculated or fabricated.


### V11.1 frontend fix
The processed-document list uses delegated event handling for the **Open** button instead of embedding JSON inside an inline `onclick` attribute. This prevents filenames containing quotes or other HTML-sensitive characters from breaking the button.


### V11.3 validation fix
Cash-flow semantic aliases now cover the exact report labels `Net cash from operating activities` and `Net cash (used in) / from financing activities`. Validation remains period-aware and never copies a single-period value into a missing comparative period.
