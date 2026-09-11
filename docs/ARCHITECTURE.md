# Architecture

```text
┌──────────────────────────────┐
│ HTML / CSS / JavaScript UI   │
│ upload + dashboard + result  │
└──────────────┬───────────────┘
               │ multipart / JSON
               ▼
┌──────────────────────────────┐
│ FastAPI routes               │
│ request validation + errors  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Document validation service  │
│ type / size / integrity /    │
│ page count                   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ OCR / text extraction        │
│ PyMuPDF native text +        │
│ Tesseract OCR + coordinates  │
│ bounding-box table geometry  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Structured extraction        │
│ Ollama + Qwen2.5-VL 3B      │
│ coordinate-aware table merge │
│ optional OpenAI provider     │
│ OCR heuristic fallback       │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Pydantic schema validation   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Financial validation         │
│ document-type-specific       │
│ calculations + tolerance     │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ SQLAlchemy repository        │
│ SQLite locally; configurable │
│ DATABASE_URL for deployment  │
└──────────────┬───────────────┘
               ▼
       consistent JSON response
```

The pipeline keeps extraction separate from validation. Local full-AI mode uses Ollama/Qwen2.5-VL 3B; provider selection is configuration-driven. A financial validation failure is evidence about the extracted/source values, not a reason to hide the extraction result.


## Coordinate-aware invoice table handling

Invoice extraction uses a two-source strategy. Qwen2.5-VL reads the complete document and supplies semantic fields/descriptions, while Tesseract `image_to_data` supplies word bounding boxes. For invoices, the service discovers the visual Quantity, Price and Total column positions from the table header and associates numeric cells by row coordinates. A small high-resolution OCR retry is used when decimal punctuation is lost in a monetary cell.

The coordinate layer does **not** calculate line amounts from quantity × price. It only recovers values that are visibly present in the corresponding table cells and leaves unreadable cells as `null`. This is important for source documents whose printed arithmetic is internally inconsistent: the system should expose the printed values and let the financial validator report the discrepancy instead of changing the extraction to make a formula pass.
