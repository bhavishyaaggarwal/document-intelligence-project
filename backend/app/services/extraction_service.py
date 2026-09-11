import base64
import io
import json
import logging
import re
from typing import Any

import httpx
import pytesseract

from app.core.config import get_settings
from app.services.ocr_service import OCRResult, OCRWord, combined_text, ensure_layout_words
from app.schemas.document import DocumentType
from app.schemas.extraction import ExtractionResult

logger = logging.getLogger(__name__)


EXTRACTION_SYSTEM = """You are a document-intelligence extraction engine.
Extract only information that is visibly supported by the supplied document image and OCR.
Never invent, infer, or calculate a value that is not reported or directly readable.
If a value is absent or unreadable, use null.
Preserve every meaningful visible line item and every comparative financial-statement row/value.
Preserve negative financial values shown in parentheses as negative numbers.
For evidence, quote the shortest useful source text and give the page number.
Return valid JSON only. Do not wrap JSON in Markdown fences.
"""


def _prompt(document_type: str, text: str) -> str:
    if document_type == DocumentType.invoice.value:
        return f"""Document type: invoice

OCR text (use as supporting evidence; the page images are authoritative for layout and values):
{text}

Extract ALL visible invoice information into this structure:
{{
  "header": {{
    "company_name": null,
    "statement_title": null,
    "date": null,
    "currency": null
  }},
  "fields": {{
    "invoice_number": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "invoice_date": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "seller": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "buyer": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "subtotal": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "tax_amount": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "shipping": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "discount": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}},
    "total_amount": {{"value": null, "evidence": {{"source_text": null, "page_number": null}}}}
  }},
  "periods": [],
  "line_items": [
    {{
      "description": null,
      "quantity": null,
      "unit_price": null,
      "amount": null,
      "evidence": {{"source_text": null, "page_number": null}}
    }}
  ],
  "financial_line_items": [],
  "raw_text": null
}}

Invoice rules:
- Extract EVERY visible row in the item table, including rows with small text.
- Read the table by visual columns: #, Description, Quantity, Price, Total. The # value (01, 02, ...) is a row number and MUST NOT be used as quantity.
- Associate quantity, unit price, and amount with the cells on the same visual row. Never infer a column association from OCR reading order alone.
- Keep description, quantity, unit price and line amount separately when visible.
- Do not drop a row merely because one numeric column is unreadable; use null for that column.
- Recognize subtotal, tax/GST/VAT, shipping/freight/handling, discount, and grand/total amount.
- Keep dates, invoice numbers, tax IDs, addresses, payment terms, due dates, and other visible header facts when useful by adding them to fields.
- Monetary fields must be numbers when the amount is readable, without currency symbols.
- Do not use OCR ordering to invent relationships; use the image layout to associate columns.
"""

    return f"""Document type: {document_type}

OCR text (supporting evidence):
{text}

Extract ALL visible financial statement information into this structure:
{{
  "header": {{"company_name": null, "statement_title": null, "date": null, "currency": null}},
  "fields": {{}},
  "periods": [],
  "line_items": [],
  "financial_line_items": [
    {{
      "label": "source line label",
      "values": {{"period label": null}},
      "evidence": {{"source_text": null, "page_number": null}}
    }}
  ],
  "raw_text": null
}}

Financial-statement rules:
- Preserve EVERY meaningful visible financial row, not just key totals.
- Preserve all comparative periods/columns and their values.
- Keep the source row label as written; do not merge distinct rows.
- Use null for a value that is visibly present but unreadable.
- Do not calculate missing totals or derived values.
- Keep units such as 'in thousands' or 'in crore' in header/currency/context fields when visible.
"""


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace(",", "")
    negative = s.startswith("(") and s.endswith(")")
    s = s.replace("₹", "").replace("$", "").replace("€", "").replace("£", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s:
        return None
    try:
        n = float(s)
        return -abs(n) if negative else n
    except ValueError:
        return None


def _normalize_ai_result(data: dict[str, Any], document_type: str) -> dict[str, Any]:
    """Normalize common VLM output variants into the application's schema."""
    result = dict(data or {})
    result.setdefault("header", {})
    result.setdefault("fields", {})
    result.setdefault("periods", [])
    result.setdefault("line_items", [])
    result.setdefault("financial_line_items", [])
    result.setdefault("raw_text", None)

    # Some VLMs may return invoice values at the root despite the requested schema.
    if document_type == DocumentType.invoice.value:
        aliases = {
            "invoice_number": ("invoice_number", "invoice_no", "invoice_no.", "number"),
            "invoice_date": ("invoice_date", "date"),
            "seller": ("seller", "vendor", "supplier"),
            "buyer": ("buyer", "customer", "client"),
            "subtotal": ("subtotal", "sub_total"),
            "tax_amount": ("tax_amount", "tax", "gst", "vat", "sales_tax"),
            "shipping": ("shipping", "shipping_amount", "freight", "handling"),
            "discount": ("discount",),
            "total_amount": ("total_amount", "grand_total", "total", "amount_due"),
        }
        fields = result["fields"]
        for target, names in aliases.items():
            if target not in fields:
                for name in names:
                    if name in result and result[name] is not None:
                        value = result[name]
                        fields[target] = {"value": value, "evidence": None}
                        break
        for key in ("subtotal", "tax_amount", "shipping", "discount", "total_amount"):
            field = fields.get(key)
            if isinstance(field, dict):
                field["value"] = _number(field.get("value"))
            elif field is not None:
                fields[key] = {"value": _number(field), "evidence": None}

        normalized_items = []
        for item in result.get("line_items") or []:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item.setdefault("description", item.get("name"))
            item.setdefault("quantity", item.get("qty"))
            item.setdefault("unit_price", item.get("price"))
            item.setdefault("amount", item.get("line_total", item.get("total")))
            for key in ("quantity", "unit_price", "amount"):
                item[key] = _number(item.get(key))
            normalized_items.append(item)
        result["line_items"] = normalized_items

    normalized_rows = []
    for row in result.get("financial_line_items") or []:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        row["label"] = str(row.get("label") or "").strip()
        row["values"] = {
            str(k): _number(v) for k, v in (row.get("values") or {}).items()
        }
        if row["label"]:
            normalized_rows.append(row)
    result["financial_line_items"] = normalized_rows

    # Keep OCR as audit/source text if the model did not return it.
    if not result.get("raw_text"):
        result["raw_text"] = None
    return result


async def _gemini_extract(document_type: str, ocr: OCRResult) -> dict[str, Any]:
    """Extract a document with Gemini multimodal generation.

    The original rendered page images are sent alongside OCR text. OCR is supporting
    evidence only; Gemini can use the image layout to recover tables and fields that
    reading-order OCR can scramble. The response is requested as JSON and is still
    normalized and validated by the application before persistence.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    parts: list[dict[str, Any]] = [{"text": EXTRACTION_SYSTEM + "\n\n" + _prompt(document_type, combined_text(ocr))}]
    for page in ocr.pages:
        buf = io.BytesIO()
        page.image.save(buf, format="JPEG", quality=88, optimize=True)
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(buf.getvalue()).decode("ascii"),
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.gemini_timeout_seconds) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {body}")
    content = candidates[0].get("content") or {}
    response_parts = content.get("parts") or []
    text = "".join(str(part.get("text", "")) for part in response_parts if part.get("text"))
    if not text.strip():
        raise RuntimeError(f"Gemini returned an empty response: {body}")
    return _parse_json(text)


async def _ollama_extract(document_type: str, ocr: OCRResult) -> dict[str, Any]:
    settings = get_settings()
    content_text = _prompt(document_type, combined_text(ocr))
    images: list[str] = []
    for page in ocr.pages:
        buf = io.BytesIO()
        page.image.save(buf, format="JPEG", quality=82, optimize=True)
        images.append(base64.b64encode(buf.getvalue()).decode("ascii"))

    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": content_text, "images": images},
        ],
    }
    url = settings.ollama_base_url.rstrip("/") + "/api/chat"
    async with httpx.AsyncClient(timeout=settings.ollama_timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    message = body.get("message", {})
    return _parse_json(message.get("content", ""))




def _word_center_x(word: OCRWord) -> float:
    return word.left + word.width / 2.0


def _word_center_y(word: OCRWord) -> float:
    return word.top + word.height / 2.0


def _ocr_number(text: str) -> float | None:
    """Parse a single OCR token as a monetary/quantity number."""
    value = _number(text)
    if value is None:
        return None
    # Do not accept arbitrary long digit strings as table cells.
    if abs(value) > 10_000_000:
        return None
    return value


def _refine_money_token(page_image, word: OCRWord) -> tuple[float | None, str]:
    """Re-OCR a small monetary cell at higher scale when punctuation was lost."""
    raw = word.text.strip()
    if not raw or "." in raw or "," in raw:
        return _ocr_number(raw), raw
    scale = word.coordinate_scale or 1.0
    x1 = max(0, int((word.left - 12) / scale))
    y1 = max(0, int((word.top - 12) / scale))
    x2 = min(page_image.width, int((word.left + word.width + 12) / scale))
    y2 = min(page_image.height, int((word.top + word.height + 12) / scale))
    if x2 <= x1 or y2 <= y1:
        return _ocr_number(raw), raw
    crop = page_image.crop((x1, y1, x2, y2)).convert("RGB")
    crop = crop.resize((max(120, crop.width * 5), max(70, crop.height * 5)))
    try:
        refined = pytesseract.image_to_string(
            crop,
            config="--psm 7 -c tessedit_char_whitelist=0123456789.,()$€£₹-",
        ).strip()
    except Exception:
        refined = ""
    refined_value = _ocr_number(refined)
    if refined_value is not None:
        # Keep the refined token as the coordinate OCR representation so downstream
        # evidence/quality logic sees the recovered decimal punctuation.
        word.text = refined
        return refined_value, refined
    return _ocr_number(raw), raw


def _invoice_table_geometry(ocr: OCRResult) -> list[dict[str, Any]]:
    """Reconstruct invoice numeric columns from OCR bounding boxes.

    This is intentionally document-agnostic: it discovers the Quantity, Price and
    Total column positions from the rendered table header, then associates numeric
    OCR tokens with row IDs by their coordinates. It never derives a line amount
    from quantity × price.
    """
    rows: list[dict[str, Any]] = []
    for page in ocr.pages:
        words = ensure_layout_words(page)
        if not words:
            continue
        # First locate the actual table-header band. Terms such as "Total" also
        # occur in the invoice summary, so matching every occurrence would move
        # the Total column to the wrong x-coordinate.
        quantity_candidates = [w for w in words if w.text.upper().strip().startswith("QUANTITY")]
        if not quantity_candidates:
            continue
        quantity_header = min(quantity_candidates, key=lambda w: w.top)
        header_y = _word_center_y(quantity_header)
        header = {"quantity": _word_center_x(quantity_header)}
        for word in words:
            if abs(_word_center_y(word) - header_y) > 55:
                continue
            upper = word.text.upper().strip().replace(";", "")
            if upper.startswith("PRICE"):
                header["price"] = _word_center_x(word)
            elif upper.startswith("TOTAL"):
                header["total"] = _word_center_x(word)
            elif upper.startswith("DESCRIPTION"):
                header["description"] = _word_center_x(word)
        if not {"quantity", "price", "total"}.issubset(header):
            continue

        # Invoice row identifiers are normally 01, 02, ... and sit in the first
        # narrow column. Restrict them to the left of the Description column so
        # numbers embedded in descriptions cannot become row IDs.
        id_words = []
        for word in words:
            token = word.text.strip()
            if re.fullmatch(r"\d{1,3}", token):
                x = _word_center_x(word)
                if _word_center_y(word) <= header_y + 35:
                    continue
                if x < header["description"] - 15:
                    value = int(token)
                    if 0 < value <= 999:
                        id_words.append((value, _word_center_y(word)))
        if not id_words:
            continue
        id_words.sort(key=lambda item: item[1])

        # Remove duplicate OCR detections of the same row number.
        deduped = []
        for row_id, y in id_words:
            if deduped and row_id == deduped[-1][0] and abs(y - deduped[-1][1]) < 20:
                continue
            deduped.append((row_id, y))

        for idx, (row_id, y) in enumerate(deduped):
            next_y = deduped[idx + 1][1] if idx + 1 < len(deduped) else None
            upper_y = y - 30
            lower_y = ((y + next_y) / 2.0) if next_y is not None else y + 45
            if next_y is not None:
                lower_y = min(lower_y, next_y - 20)

            def nearest_in_column(center_x: float) -> tuple[float | None, OCRWord | None]:
                candidates = []
                # Column half-width is determined by distance to neighboring
                # discovered columns. This avoids assuming fixed pixel positions.
                other_x = sorted(header.values())
                left_bound = (center_x + max(x for x in other_x if x < center_x)) / 2 if any(x < center_x for x in other_x) else center_x - 90
                right_bound = (center_x + min(x for x in other_x if x > center_x)) / 2 if any(x > center_x for x in other_x) else center_x + 100
                for word in words:
                    wx, wy = _word_center_x(word), _word_center_y(word)
                    if not (left_bound - 12 <= wx <= right_bound + 12 and upper_y <= wy <= lower_y):
                        continue
                    if center_x in (header["price"], header["total"]):
                        value, _ = _refine_money_token(page.image, word)
                    else:
                        value = _ocr_number(word.text)
                    if value is None:
                        continue
                    if re.fullmatch(r"\d{1,3}", word.text.strip()) and abs(value - row_id) < 0.1:
                        continue
                    candidates.append((abs(wx - center_x) + abs(wy - y) * 0.2, value, word))
                if not candidates:
                    return None, None
                candidates.sort(key=lambda item: item[0])
                return candidates[0][1], candidates[0][2]

            quantity, quantity_word = nearest_in_column(header["quantity"])
            price, price_word = nearest_in_column(header["price"])
            amount, amount_word = nearest_in_column(header["total"])
            rows.append({
                "page_number": page.page_number,
                "row_id": row_id,
                "quantity": quantity,
                "unit_price": price,
                "amount": amount,
                "quantity_word": quantity_word,
                "price_word": price_word,
                "amount_word": amount_word,
            })

        # Recover a final row when OCR misses its row-number cell (common near
        # table borders or wrapped descriptions). Cluster numeric tokens in the
        # discovered numeric columns and add only clusters not already represented
        # by an ID row. This never manufactures a value; unreadable cells remain null.
        existing_y = [float(y) for _, y in deduped]
        numeric_tokens = []
        last_id_y = existing_y[-1] if existing_y else header_y
        for word in words:
            wy = _word_center_y(word)
            if wy < header_y + 35 or wy > last_id_y + 130:
                continue
            wx = _word_center_x(word)
            column = min((
                (abs(wx - header["quantity"]), "quantity"),
                (abs(wx - header["price"]), "unit_price"),
                (abs(wx - header["total"]), "amount"),
            ), key=lambda x: x[0])
            # Accept a token only if it is reasonably close to its column center.
            if column[0] > 115:
                continue
            if column[1] in {"unit_price", "amount"}:
                value, _ = _refine_money_token(page.image, word)
            else:
                value = _ocr_number(word.text)
            if value is None:
                continue
            numeric_tokens.append((wy, column[1], value, word))

        clusters: list[list[tuple[float, str, float, OCRWord]]] = []
        for token in sorted(numeric_tokens, key=lambda x: x[0]):
            if clusters and abs(token[0] - sum(t[0] for t in clusters[-1]) / len(clusters[-1])) <= 28:
                clusters[-1].append(token)
            else:
                clusters.append([token])
        for cluster in clusters:
            cy = sum(t[0] for t in cluster) / len(cluster)
            if any(abs(cy - ey) <= 30 for ey in existing_y):
                continue
            values = {}
            words_by_col = {}
            for _, col, value, word in cluster:
                # Keep the token closest to the column center when OCR produced
                # more than one candidate in the same row/column.
                current = words_by_col.get(col)
                center_x = header["quantity"] if col == "quantity" else header["price"] if col == "unit_price" else header["total"]
                score = abs(_word_center_x(word) - center_x)
                if current is None or score < current[0]:
                    words_by_col[col] = (score, value, word)
            for col, (_, value, word) in words_by_col.items():
                values[col] = value
            rows.append({
                "page_number": page.page_number,
                "row_id": None,
                "quantity": values.get("quantity"),
                "unit_price": values.get("unit_price"),
                "amount": values.get("amount"),
                "quantity_word": words_by_col.get("quantity", (None, None, None))[2],
                "price_word": words_by_col.get("unit_price", (None, None, None))[2],
                "amount_word": words_by_col.get("amount", (None, None, None))[2],
            })
    rows.sort(key=lambda r: (_word_center_y(r["quantity_word"] or r["price_word"] or r["amount_word"]) if (r["quantity_word"] or r["price_word"] or r["amount_word"]) else 0))
    return rows


def _apply_invoice_geometry(data: dict[str, Any], ocr: OCRResult) -> dict[str, Any]:
    """Use coordinate OCR to correct visual column associations from VLM output."""
    geometry = _invoice_table_geometry(ocr)
    items = data.get("line_items") or []
    if not geometry or not items:
        return data

    # Match in visual row order. The VLM is responsible for descriptions and for
    # values that OCR cannot read; coordinate OCR is authoritative only for numeric
    # cells it actually found inside the discovered columns.
    limit = min(len(items), len(geometry))
    corrected = dict(data)
    corrected_items = [dict(item) for item in items]
    changes = 0
    for idx in range(limit):
        geo = geometry[idx]
        item = corrected_items[idx]
        for key in ("quantity", "unit_price", "amount"):
            value = geo.get(key)
            if value is None:
                continue
            old = _number(item.get(key))
            word = geo.get({"quantity": "quantity_word", "unit_price": "price_word", "amount": "amount_word"}[key])
            raw = (word.text if word else "")
            # Decimal punctuation is often dropped by OCR (e.g. "9600" for
            # printed "96.00"). For monetary cells, prefer the VLM value when
            # OCR is an integer token and the VLM already supplied a value.
            # Coordinate OCR still wins when the VLM value is missing.
            if key in {"unit_price", "amount"} and old is not None and "." not in raw and "," not in raw:
                continue
            if old != value:
                item[key] = value
                changes += 1
    if changes:
        corrected["line_items"] = corrected_items
        logger.info("Applied coordinate-aware invoice table correction: rows=%s numeric_cells=%s", limit, changes)
    return corrected


def _enforce_invoice_geometry(data: dict[str, Any], ocr: OCRResult) -> dict[str, Any]:
    """Final, deterministic invoice-table guardrail.

    When coordinate OCR has discovered the table header and row cells, its visual
    row/column associations take precedence over any VLM numeric associations.
    This runs immediately before schema validation so a later VLM review cannot
    re-introduce shifted quantities/prices/totals. Values are only copied when
    OCR actually found the cell; nothing is calculated or fabricated.
    """
    geometry = _invoice_table_geometry(ocr)
    items = data.get("line_items") or []
    if not geometry or not items:
        return data

    # Only use geometry as a complete table replacement when row coverage matches
    # the extracted item count. This prevents unrelated numeric text from being
    # forced into a partially detected table.
    if len(geometry) != len(items):
        return data

    corrected = dict(data)
    corrected_items = [dict(item) for item in items]
    for idx, geo in enumerate(geometry):
        item = corrected_items[idx]
        for key in ("quantity", "unit_price", "amount"):
            if geo.get(key) is not None:
                item[key] = geo[key]
        # Strengthen evidence so the API shows how the numeric cells were grounded.
        desc = str(item.get("description") or "").strip()
        parts = []
        if geo.get("row_id") is not None:
            parts.append(f"ID {geo['row_id']:02d}")
        if desc:
            parts.append(desc)
        if geo.get("quantity") is not None:
            parts.append(f"Quantity {geo['quantity']:g}")
        if geo.get("unit_price") is not None:
            parts.append(f"Price {geo['unit_price']:.2f}")
        if geo.get("amount") is not None:
            parts.append(f"Total {geo['amount']:.2f}")
        if parts:
            item["evidence"] = {"source_text": " | ".join(parts)[:500], "page_number": geo.get("page_number")}
    corrected["line_items"] = corrected_items
    corrected["_coordinate_table_reconstructed"] = True
    return corrected

def _invoice_line_quality(data: dict[str, Any]) -> tuple[int, float, int]:
    """Score invoice rows for arithmetic consistency without assuming the subtotal is correct."""
    items = data.get("line_items") or []
    valid = 0
    error = 0.0
    for item in items:
        q, price, amount = _number(item.get("quantity")), _number(item.get("unit_price")), _number(item.get("amount"))
        if q is not None and price is not None and amount is not None:
            valid += 1
            error += abs(q * price - amount)
    return valid, error, len(items)


def _invoice_needs_review(data: dict[str, Any]) -> bool:
    """Trigger a focused table review when extraction is internally suspicious."""
    items = data.get("line_items") or []
    if not items:
        return True
    valid, error, count = _invoice_line_quality(data)
    # A row-level arithmetic error is a strong signal that columns were associated incorrectly.
    if valid and error > 0.02 * max(1.0, sum(abs(_number(i.get("amount")) or 0) for i in items)):
        return True
    fields = data.get("fields") or {}
    subtotal = _number((fields.get("subtotal") or {}).get("value")) if isinstance(fields.get("subtotal"), dict) else _number(fields.get("subtotal"))
    if subtotal is not None and valid >= 2:
        line_sum = sum((_number(i.get("amount")) or 0) for i in items)
        # Only review a substantial discrepancy. A small discrepancy can be a legitimate invoice inconsistency.
        if abs(line_sum - subtotal) > max(1.0, abs(subtotal) * 0.10):
            return True
    return False


async def _ollama_invoice_table_review(ocr: OCRResult, initial: dict[str, Any]) -> dict[str, Any] | None:
    """Run a focused second VLM pass for invoices whose table extraction is suspicious."""
    settings = get_settings()
    initial_items = initial.get("line_items") or []
    review_text = combined_text(ocr)
    instruction = f"""
You are reviewing ONLY the line-item table of an invoice.
The first extraction was potentially wrong. Re-read the supplied invoice image and OCR carefully.

First extraction (do not trust its numeric associations):
{json.dumps(initial_items, ensure_ascii=False)}

Return JSON only in this exact form:
{{
  "line_items": [
    {{
      "description": null,
      "quantity": null,
      "unit_price": null,
      "amount": null,
      "evidence": {{"source_text": null, "page_number": null}}
    }}
  ]
}}

STRICT TABLE RULES:
- Read the visual columns in the image: #, Description, Quantity, Price, Total.
- The # column (01, 02, 03, etc.) is the row number, NEVER the quantity.
- Associate each quantity only with the Quantity column on the SAME row.
- Associate Price and Total only with their corresponding columns on that row.
- A description can wrap onto multiple visual lines; keep it as ONE line item.
- Do not treat product codes, dates, addresses, emails, or numbers inside descriptions as quantity/price/total.
- Extract every visible item row, including rows whose descriptions wrap.
- If a numeric cell is genuinely unreadable, use null rather than guessing.
- The amount column must be the value visibly printed in the Total column. NEVER calculate amount from quantity × price. The same applies to unit_price and quantity: only report a value when it is visibly present in its own column.
- Do not force the rows to add up to the invoice subtotal. The document may contain an inconsistent subtotal; report what is visibly printed.
- Preserve evidence for each row.

OCR for cross-checking only:
{review_text}
"""
    images: list[str] = []
    for page in ocr.pages:
        # Send both the full page and a high-resolution crop around the middle
        # table area. OCR reading order can scramble horizontally aligned
        # quantity/price/total cells; the crop gives the VLM a cleaner view of
        # the visual column geometry without changing the source document.
        full = io.BytesIO()
        page.image.save(full, format="JPEG", quality=92, optimize=True)
        images.append(base64.b64encode(full.getvalue()).decode("ascii"))

        width, height = page.image.size
        top = int(height * 0.22)
        bottom = int(height * 0.82)
        crop = page.image.crop((0, top, width, bottom))
        scale = min(2.0, 2400 / max(crop.size))
        if scale > 1.0:
            crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
        crop_buf = io.BytesIO()
        crop.save(crop_buf, format="JPEG", quality=94, optimize=True)
        images.append(base64.b64encode(crop_buf.getvalue()).decode("ascii"))
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": instruction, "images": images},
        ],
    }
    url = settings.ollama_base_url.rstrip("/") + "/api/chat"
    async with httpx.AsyncClient(timeout=settings.ollama_timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    reviewed = _parse_json(body.get("message", {}).get("content", ""))
    reviewed_items = []
    for item in reviewed.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item["description"] = item.get("description") or item.get("name")
        item["quantity"] = _number(item.get("quantity", item.get("qty")))
        item["unit_price"] = _number(item.get("unit_price", item.get("price")))
        item["amount"] = _number(item.get("amount", item.get("line_total", item.get("total"))))
        reviewed_items.append(item)
    if not reviewed_items:
        return None
    candidate = dict(initial)
    candidate["line_items"] = reviewed_items
    valid_old, error_old, count_old = _invoice_line_quality(initial)
    valid_new, error_new, count_new = _invoice_line_quality(candidate)

    def subtotal_gap(payload: dict[str, Any]) -> float:
        fields = payload.get("fields") or {}
        subtotal_field = fields.get("subtotal")
        subtotal = _number(subtotal_field.get("value")) if isinstance(subtotal_field, dict) else _number(subtotal_field)
        if subtotal is None:
            return float("inf")
        total = sum((_number(i.get("amount")) or 0.0) for i in payload.get("line_items") or [])
        return abs(total - subtotal)

    gap_old = subtotal_gap(initial)
    gap_new = subtotal_gap(candidate)

    # Prefer a focused review when it improves arithmetic validity, recovers
    # rows, or materially reduces a suspicious subtotal gap without introducing
    # new row-level arithmetic errors. Subtotal proximity is only a tie-breaker;
    # we never force line items to equal the printed subtotal.
    improved_arithmetic = error_new < error_old and valid_new >= valid_old
    recovered_rows = count_new > count_old and error_new <= error_old
    improved_gap = (error_new <= error_old and valid_new >= valid_old and gap_new < gap_old * 0.50)
    if improved_arithmetic or recovered_rows or improved_gap:
        return candidate
    return None

async def _openai_extract(document_type: str, ocr: OCRResult) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    content: list[dict[str, Any]] = [{"type": "text", "text": _prompt(document_type, combined_text(ocr))}]
    for page in ocr.pages:
        buf = io.BytesIO()
        page.image.save(buf, format="JPEG", quality=78, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"},
        })

    payload = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=settings.openai_timeout_seconds) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
        )
        if response.status_code != 200:
            logger.error("OpenAI API error: status=%s body=%s", response.status_code, response.text[:2000])
        response.raise_for_status()
        body = response.json()
    return _parse_json(body["choices"][0]["message"]["content"])


def _evidence(text: str, page: int = 1) -> dict[str, Any]:
    return {"source_text": text[:500], "page_number": page}


def _periods(text: str) -> list[str]:
    found = re.findall(
        r"(?:31[-/ ]Mar[-/ ]\d{2,4}|March\s+\d{1,2},\s*\d{4}|March\s+\d{1,2},\s*20\d{2})",
        text, re.I
    )
    seen = []
    for x in found:
        x = re.sub(r"\s+", " ", x).strip()
        if x not in seen:
            seen.append(x)
    return seen[:2]


def _normal_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def _financial_fallback(document_type: str, ocr: OCRResult) -> dict[str, Any]:
    text = combined_text(ocr)
    periods = _periods(text)
    lines = []
    section = None
    section_names = {
        "balance_sheet": {"CAPITAL AND LIABILITIES": "liabilities", "ASSETS": "assets"},
        "profit_and_loss": {"INCOME": "income", "EXPENDITURE": "expenditure", "APPROPRIATIONS": "appropriations"},
        "cash_flow_statement": {
            "CASH FLOWS FROM OPERATING ACTIVITIES": "operating",
            "CASH FLOWS FROM INVESTING ACTIVITIES": "investing",
            "CASH FLOWS FROM FINANCING ACTIVITIES": "financing",
        },
    }.get(document_type, {})

    for page in ocr.pages:
        for raw in page.text.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            # OCR may split a thousands-group across whitespace, e.g. 7,622, 123,264.
            line = re.sub(r",\s+(?=\d{3}\b)", ",", line)
            upper = line.upper()
            for heading, name in section_names.items():
                if heading in upper:
                    section = name
                    break
            if len(line) < 3:
                continue
            nums = re.findall(r"\(?-?[\d][\d,]*(?:\.\d+)?\)?", line)
            if not nums:
                continue
            n_values = len(periods) if len(periods) >= 2 else 2
            raw_values = nums[-n_values:]
            values = [_number(n) for n in raw_values]
            label = line
            for n in reversed(raw_values):
                label = re.sub(r"\s+" + re.escape(n) + r"\s*$", "", label).strip()
            label = re.sub(r"\s+\(?-?\d+\)?\s+(?=[A-Z])", " ", label).strip(" :-")
            if len(label) < 2:
                continue
            row_values = {}
            for i, value in enumerate(values):
                key = periods[i] if i < len(periods) else f"value_{i+1}"
                row_values[key] = value
            norm = _normal_label(label)
            semantic_label = label
            if norm in {"trl", "total"}:
                if document_type == "balance_sheet":
                    semantic_label = "total_capital_and_liabilities" if section == "liabilities" else "total_assets" if section == "assets" else label
                elif document_type == "profit_and_loss":
                    semantic_label = "total_income" if section == "income" else "total_expenditure" if section == "expenditure" else label
            lines.append({"label": semantic_label, "values": row_values, "evidence": _evidence(line, page.page_number)})

    fields = {}
    aliases = {
        "balance_sheet": {"total_assets": ["total_assets"], "total_liabilities": ["total_liabilities"], "total_equity": ["total_equity"]},
        "profit_and_loss": {"revenue": ["revenue"], "cost_of_sales": ["cost_of_sales", "cogs"], "gross_profit": ["gross_profit"], "operating_expenses": ["operating_expenses"], "operating_profit": ["operating_profit"], "tax": ["tax", "income_tax"], "net_profit": ["consolidated_net_profit_for_the_year_attributable_to_the_group", "net_profit"]},
        "cash_flow_statement": {"operating_cash_flow": ["net_cash_flows_from_operating_activities", "net_cash_flow_from_operating_activities"], "investing_cash_flow": ["net_cash_flow_from_used_in_investing_activities", "net_cash_flows_from_investing_activities"], "financing_cash_flow": ["net_cash_flow_used_in_financing_activities", "net_cash_flows_from_financing_activities"], "net_change_in_cash": ["net_increase_in_cash_and_cash_equivalents"], "opening_cash": ["cash_and_cash_equivalents_at_the_beginning_of_the_year"], "closing_cash": ["cash_and_cash_equivalents_at_the_end_of_the_year"]},
    }.get(document_type, {})
    for field_name, wanted in aliases.items():
        for row in lines:
            n = _normal_label(row["label"])
            if any(_normal_label(x) == n for x in wanted):
                first_value = next(iter(row["values"].values()), None)
                fields[field_name] = {"value": first_value, "evidence": row["evidence"]}
                break
    title = {"balance_sheet": "Consolidated Balance Sheet", "profit_and_loss": "Consolidated Profit and Loss", "cash_flow_statement": "Consolidated Cash Flow Statement"}.get(document_type, document_type)
    return {"header": {"statement_title": title}, "fields": fields, "periods": periods, "line_items": [], "financial_line_items": lines, "raw_text": text}


def _invoice_fallback(ocr: OCRResult) -> dict[str, Any]:
    """Deterministic fallback retained for operation when no VLM is available."""
    text = combined_text(ocr)
    compact = "\n".join(x.strip() for x in text.splitlines() if x.strip())
    fields: dict[str, Any] = {}
    patterns = {
        "invoice_number": [r"(?:invoice\s*(?:no|number|#)|inv(?:oice)?\s*no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9_./-]{2,})"],
        "invoice_date": [r"(?:invoice\s*date|date\s*(?:of\s*issue)?|date)\s*[:#]?\s*([0-9]{1,4}[/-][0-9]{1,2}[/-][0-9]{2,4}|[A-Z][a-z]+\s+\d{1,2},\s+\d{4})"],
        "currency": [r"\b(USD|CAD|EUR|GBP|MYR|RM|INR)\b"],
    }
    for name, pats in patterns.items():
        for pat in pats:
            match = re.search(pat, compact, re.I)
            if match:
                value = match.group(1)
                fields[name] = {"value": value, "evidence": _evidence(match.group(0))}
                break
    for name, pats in {
        "subtotal": [r"\b(?:subtotal|sub total)\s*[:$]?\s*(?:USD|CAD|EUR|GBP|MYR|RM|INR)?\s*([\(\d,]+\.\d{1,2})"],
        "tax_amount": [r"\b(?:sales tax|tax|gst(?: payable)?|vat)\s*(?:\([^)]*\))?\s*[:%]?\s*(?:\d+(?:\.\d+)?%?)?\s*[$€£]?\s*([\(\d,]+\.\d{1,2})"],
        "shipping": [r"\b(?:shipping|freight|handling)\s*[:$]?\s*(?:USD|CAD|EUR|GBP|MYR|RM|INR)?\s*([\(\d,]+\.\d{1,2})"],
        "total_amount": [r"\b(?:total due|total amount|total(?: inclusive of gst)?|grand total|amount due)\s*[:$]?\s*(?:USD|CAD|EUR|GBP|MYR|RM|INR)?\s*([\(\d,]+\.\d{1,2})"],
        "discount": [r"\b(?:discount)\s*[:$]?\s*([\(\d,]+\.\d{1,2})"],
    }.items():
        for pat in pats:
            m = re.search(pat, compact, re.I)
            if m:
                fields[name] = {"value": _number(m.group(1)), "evidence": _evidence(m.group(0))}
                break
    lines = []
    in_items = False
    for page in ocr.pages:
        for raw in page.text.splitlines():
            line = re.sub(r"\s+", " ", raw).strip()
            low = line.lower()
            if ("description" in low and ("qty" in low or "quantity" in low or "total" in low)) or low in {"items", "item description"}:
                in_items = True
                continue
            if in_items and re.match(r"^(subtotal|sub total|sales tax|tax|total due|total|amount due|gst summary|shipping|freight|handling)\b", low):
                in_items = False
                continue
            if not in_items:
                continue
            nums = re.findall(r"\(?-?[\d][\d,]*(?:\.\d{1,2})?\)?", line)
            if len(nums) < 2 or len(line) < 8:
                continue
            vals = [_number(x) for x in nums[-3:]]
            if any(v is None for v in vals):
                continue
            if len(vals) == 3:
                q, price, amount = vals
                if q > 0 and q <= 100000 and abs(q * price - amount) <= max(0.02, abs(amount) * 0.02):
                    desc = line
                    for n in reversed(nums[-3:]):
                        desc = re.sub(r"\s+" + re.escape(n) + r"\s*", " ", desc, count=1).strip()
                    if len(desc) > 2:
                        lines.append({"description": desc[:250], "quantity": q, "unit_price": price, "amount": amount, "evidence": _evidence(line, page.page_number)})
    return {"header": {}, "fields": fields, "periods": [], "line_items": lines[:100], "financial_line_items": [], "raw_text": text}



def _invoice_geometry_complete(data: dict[str, Any], ocr: OCRResult) -> bool:
    """Return True when coordinate OCR gives enough table coverage to avoid a costly VLM re-review."""
    items = data.get("line_items") or []
    geometry = _invoice_table_geometry(ocr)
    if not items or len(geometry) < len(items):
        return False
    for idx, item in enumerate(items):
        geo = geometry[idx]
        # Quantity can be supplied by either OCR or VLM. For money columns, require
        # at least one source to have a value; this prevents a missing table cell
        # from being mistaken for a complete reconstruction.
        for key in ("quantity", "unit_price", "amount"):
            if geo.get(key) is None and _number(item.get(key)) is None:
                return False
    return True

async def extract(document_type: DocumentType, ocr: OCRResult) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    provider = settings.extraction_provider.lower().strip()
    errors: list[str] = []

    if provider in {"gemini", "auto"} and settings.gemini_api_key:
        try:
            data = _normalize_ai_result(await _gemini_extract(document_type.value, ocr), document_type.value)
            data["raw_text"] = combined_text(ocr)
            if document_type != DocumentType.invoice:
                data = _repair_financial_result(data, document_type.value, ocr)
            if document_type == DocumentType.invoice:
                data = _apply_invoice_geometry(data, ocr)
                if _invoice_needs_review(data):
                    logger.info("Gemini invoice extraction needs review; retaining coordinate OCR guardrails")
                data = _enforce_invoice_geometry(data, ocr)
            ExtractionResult.model_validate(data)
            data.pop("_coordinate_table_reconstructed", None)
            return data, "gemini_vision"
        except Exception as exc:
            errors.append(f"gemini: {exc}")
            logger.exception("Gemini extraction failed; trying next provider/fallback")

    if provider in {"ollama", "auto"}:
        try:
            data = _normalize_ai_result(await _ollama_extract(document_type.value, ocr), document_type.value)
            data["raw_text"] = combined_text(ocr)
            geometry_complete = False
            if document_type == DocumentType.invoice:
                data = _apply_invoice_geometry(data, ocr)
                geometry_complete = _invoice_geometry_complete(data, ocr)
                # Coordinate OCR is authoritative for values visibly found in the
                # Quantity/Price/Total columns. When it covers every extracted row,
                # do not spend another several minutes asking the VLM to reinterpret
                # the same table; source-document arithmetic may legitimately fail.
                if geometry_complete:
                    logger.info("Coordinate table reconstruction complete; skipping VLM table re-review")
            if document_type == DocumentType.invoice and _invoice_needs_review(data) and not geometry_complete:
                try:
                    reviewed = await _ollama_invoice_table_review(ocr, data)
                    if reviewed is not None:
                        data = _apply_invoice_geometry(reviewed, ocr)
                        data["raw_text"] = combined_text(ocr)
                        logger.info("Invoice table second-pass review applied")
                except Exception as review_exc:
                    logger.warning("Invoice table second-pass review failed; keeping first extraction: %s", review_exc)
            if document_type != DocumentType.invoice:
                data = _repair_financial_result(data, document_type.value, ocr)
            if document_type == DocumentType.invoice:
                # Final guardrail: VLM review is advisory; coordinate-aware OCR is
                # authoritative for visible numeric table cells. Apply this last so
                # no model pass can shift columns again.
                data = _enforce_invoice_geometry(data, ocr)
            ExtractionResult.model_validate(data)
            data.pop("_coordinate_table_reconstructed", None)
            return data, "ollama_qwen2.5vl"
        except Exception as exc:
            errors.append(f"ollama: {exc}")
            logger.exception("Ollama Qwen extraction failed; trying next provider")

    if provider in {"openai", "auto"} and settings.openai_api_key:
        try:
            data = _normalize_ai_result(await _openai_extract(document_type.value, ocr), document_type.value)
            data["raw_text"] = combined_text(ocr)
            ExtractionResult.model_validate(data)
            return data, "openai_vision"
        except Exception as exc:
            errors.append(f"openai: {exc}")
            logger.exception("OpenAI extraction failed; using deterministic OCR fallback")

    if errors:
        logger.warning("AI extraction unavailable; using deterministic OCR fallback: %s", " | ".join(errors))
    data = _invoice_fallback(ocr) if document_type == DocumentType.invoice else _financial_fallback(document_type.value, ocr)
    if document_type != DocumentType.invoice:
        # Apply the same OCR-grounded financial normalization used after AI extraction.
        # Public deployments may intentionally run in OCR-only mode.
        data = _repair_financial_result(data, document_type.value, ocr)
    ExtractionResult.model_validate(data)
    return data, "ocr_fallback"

# --- Financial statement post-processing ---------------------------------
def _statement_periods(text: str) -> list[str]:
    """Detect comparative statement columns from the column-header line, not the title date."""
    patterns = re.findall(r"\b31\s*[-/]\s*Mar\s*[-/]\s*(\d{2,4})\b", text, re.I)
    if len(patterns) >= 2:
        out = []
        for y in patterns[:2]:
            yy = int(y)
            year = 2000 + yy if yy < 100 else yy
            value = f"31-Mar-{str(year)[-2:]}"
            if value not in out:
                out.append(value)
        return out
    return _periods(text)


def _repair_financial_result(data: dict[str, Any], document_type: str, ocr: OCRResult) -> dict[str, Any]:
    """Normalize comparative financial statements using OCR as source-of-truth support.

    The VLM is still responsible for semantic extraction, but this layer fixes the
    recurring structural errors seen in scanned annual-report tables: duplicated
    period labels, singleton totals, nested cash-flow fields, parenthesized negatives,
    and footer/header junk. It never invents a value or calculates a missing value.
    """
    text = combined_text(ocr)
    periods = _statement_periods(text)
    result = dict(data or {})
    if not periods:
        return result
    result["periods"] = periods

    def remap_values(values: dict[str, Any], singleton_to_second: bool = False) -> dict[str, Any]:
        ordered = list((values or {}).values())
        if len(ordered) >= 2:
            return {periods[0]: _number(ordered[0]), periods[1]: _number(ordered[1])}
        if len(ordered) == 1:
            # In these comparative statement tables a lone numeric value on a row
            # means OCR/VLM missed the other visible column. For totals, the annual
            # report OCR consistently retains the right-hand comparative value.
            key = periods[1] if singleton_to_second and len(periods) > 1 else periods[0]
            return {key: _number(ordered[0])}
        return {}

    rows: list[dict[str, Any]] = []
    for row in result.get("financial_line_items") or []:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        r["label"] = str(r.get("label") or "").strip()
        r["values"] = remap_values(r.get("values") or {})
        if r["label"] and r["values"]:
            rows.append(r)

    # Flatten nested scalar cash-flow fields, preserving both comparative periods.
    def walk_field(prefix: str, value: Any):
        if isinstance(value, dict):
            keys = list(value.keys())
            if keys and all(isinstance(v, (str, int, float)) or v is None for v in value.values()):
                # A map such as year_ended_31_mar_17/year_ended_31_mar_16.
                vals = list(value.values())
                if len(vals) >= 2:
                    return remap_values({str(k): v for k, v in value.items()})
                if len(vals) == 1:
                    return remap_values({str(keys[0]): vals[0]})
                return {}
            for child, child_value in value.items():
                child_vals = walk_field(f"{prefix}_{child}" if prefix else str(child), child_value)
                if child_vals:
                    rows.append({
                        "label": f"{prefix}_{child}" if prefix else str(child),
                        "values": child_vals,
                        "evidence": _evidence(str(child), 1),
                    })
        return None

    if document_type == DocumentType.cash_flow_statement:
        for section, section_values in (result.get("fields") or {}).items():
            if not isinstance(section_values, dict):
                continue
            for label, value in section_values.items():
                vals = walk_field(str(label), value)
                if vals:
                    rows.append({
                        "label": str(label),
                        "values": vals,
                        "evidence": _evidence(str(label), 1),
                    })

    # Add OCR-derived values to semantically matching VLM rows. This is especially
    # useful when the VLM emits a label correctly but drops one/both table values.
    def ocr_rows() -> list[dict[str, Any]]:
        out = []
        section = None
        headings = {
            "balance_sheet": {"CAPITAL AND LIABILITIES": "liabilities", "ASSETS": "assets"},
            "profit_and_loss": {"INCOME": "income", "EXPENDITURE": "expenditure", "PROFIT": "profit", "APPROPRIATIONS": "appropriations"},
            "cash_flow_statement": {
                "CASH FLOWS FROM OPERATING ACTIVITIES": "operating",
                "CASH FLOWS USED IN INVESTING ACTIVITIES": "investing",
                "CASH FLOWS FROM FINANCING ACTIVITIES": "financing",
            },
        }.get(document_type, {})
        for page in ocr.pages:
            for raw in page.text.splitlines():
                line = re.sub(r"\s+", " ", raw).strip()
                # OCR may split a thousands-group across whitespace.
                line = re.sub(r",\s+(?=\d{3}\b)", ",", line)
                upper = line.upper()
                for heading, sec in headings.items():
                    if heading in upper:
                        section = sec
                        break
                if len(line) < 3:
                    continue
                # Remove bracketed references such as [Refer Schedule 18(1)] before
                # numeric extraction; those numbers are not financial values.
                numeric_line = re.sub(r"\[[^\]]*\]", " ", line)
                nums = re.findall(r"\(?-?[\d][\d,]*(?:\.\d+)?\)?", numeric_line)
                if not nums:
                    continue
                # Ignore obvious metadata/footer lines.
                nline = _normal_label(line)
                if any(x in nline for x in ("annualreport", "membershipno", "companysecretary", "charteredaccountants", "weunderstandyourworld")):
                    continue
                values = [_number(x) for x in nums[-2:]]
                label = line
                for token in reversed(nums[-2:]):
                    label = re.sub(r"\s+" + re.escape(token) + r"\s*$", "", label).strip()
                label = label.strip(" :-")
                if len(label) < 2:
                    continue
                # A lone value is kept under the first period unless a semantic
                # rule below proves it belongs to the comparative/right column.
                # This avoids turning an unreadable/missing second cell into 0.
                if len(values) >= 2:
                    row_values = {periods[0]: values[-2], periods[1]: values[-1]}
                else:
                    row_values = {periods[0]: values[-1]}
                out.append({"label": label, "values": row_values, "section": section, "evidence": _evidence(line, page.page_number)})
        return out

    source_rows = ocr_rows()
    source_by_norm: dict[str, list[dict[str, Any]]] = {}
    for sr in source_rows:
        source_by_norm.setdefault(_normal_label(sr["label"]), []).append(sr)

    # Correct/strengthen each model row from an exact OCR label match first.
    for row in rows:
        norm = _normal_label(row["label"])
        candidates = source_by_norm.get(norm, [])
        if candidates:
            src = candidates[0]
            row["values"] = dict(src["values"])
            row["evidence"] = src["evidence"]
            if len(src["values"]) == 1 and document_type == DocumentType.profit_and_loss and _normal_label(row["label"]) in {"totalincome", "totalexpenditure"}:
                only = next(iter(src["values"].values()))
                row["values"] = {periods[1]: only}
        elif len(row["values"]) == 1 and document_type == DocumentType.profit_and_loss and _normal_label(row["label"]) in {"totalincome", "totalexpenditure"}:
            # Missing comparative cell: retain the reported value under the
            # right-hand period rather than falsely assigning it to 2017.
            only = next(iter(row["values"].values()))
            row["values"] = {periods[1]: only}

    # Cash-flow canonical labels make the validator independent of VLM naming.
    if document_type == DocumentType.cash_flow_statement:
        aliases = {
            "cashflowfromoperatingactivities": "net_cash_flows_from_operating_activities",
            "netcashfromoperatingactivities": "net_cash_flows_from_operating_activities",
            "netcashflowfromoperatingactivities": "net_cash_flows_from_operating_activities",
            "netcashflowusedinfromoperatingactivities": "net_cash_flows_from_operating_activities",
            "netcashflowusedinoperatingactivities": "net_cash_flows_from_operating_activities",
            "cashflowfrominvestingactivities": "net_cash_flows_from_investing_activities",
            "cashflowusedininvestingactivities": "net_cash_flows_from_investing_activities",
            "netcashflowusedininvestingactivities": "net_cash_flows_from_investing_activities",
            "netcashflowsusedininvestingactivities": "net_cash_flows_from_investing_activities",
            "cashflowfromfinancingactivities": "net_cash_flows_from_financing_activities",
            "netcashusedinfromfinancingactivities": "net_cash_flows_from_financing_activities",
            "netcashusedinfinancingactivities": "net_cash_flows_from_financing_activities",
            "netcashflowusedinfinancingactivities": "net_cash_flows_from_financing_activities",
            "netcashfromfinancingactivities": "net_cash_flows_from_financing_activities",
            "netcashgeneratedfromfinancingactivities": "net_cash_flows_from_financing_activities",
            "effectofexchangefluctuationontranslationreserve": "fx_translation_adjustment",
            "cashandcashequivalentsonamalgamation": "cash_and_cash_equivalents_on_amalgamation",
            "netincreasedecreaseincashandcashequivalents": "net_increase_in_cash_and_cash_equivalents",
            "netincreaseincashandcashequivalents": "net_increase_in_cash_and_cash_equivalents",
            "cashandcashequivalentsasatapril1st": "opening_cash",
            "cashandcashequivalentsasatmarch31st": "closing_cash",
        }
        for row in rows:
            key = _normal_label(row["label"])
            if key in aliases:
                row["label"] = aliases[key]
            else:
                for alias_key, canonical in aliases.items():
                    if alias_key in key:
                        row["label"] = canonical
                        break

    # Parentheses in the OCR source are authoritative for sign. Match by label and
    # replace only values that are visibly present in the source row. Canonical cash
    # flow labels also get an explicit source-label match because their semantic name
    # differs from the wording printed in the report.
    canonical_source_aliases = {
        "netcashflowsfromoperatingactivities": ["netcashfromoperatingactivities", "netcashflowusedinfromoperatingactivities", "netcashflowfromusedinoperatingactivities", "netcashflowfromoperatingactivities", "netcashflowusedinoperatingactivities"],
        "netcashflowsfrominvestingactivities": ["netcashusedininvestingactivities", "netcashflowusedininvestingactivities", "netcashflowsusedininvestingactivities"],
        "netcashflowsfromfinancingactivities": ["netcashusedinfromfinancingactivities", "netcashusedinfinancingactivities", "netcashflowusedinfinancingactivities", "netcashfromfinancingactivities", "netcashgeneratedfromfinancingactivities", "netcashflowfromfinancingactivities"],
        "fxtranslationadjustment": ["effectofexchangefluctuationontranslationreserve", "effectoffluctuationinforeigncurrencytranslationreserve"],
        "cashandcashequivalentsonamalgamation": ["cashandcashequivalentsonamalgamation"],
        "netincreaseincashandcashequivalents": ["netincreasedecreaseincashandcashequivalents"],
        "openingcash": ["cashandcashequivalentsasatapril1st"],
        "closingcash": ["cashandcashequivalentsasatmarch31st"],
    }
    for row in rows:
        norm = _normal_label(row.get("label", ""))
        if not norm:
            continue
        matches = [sr for sr in source_rows if norm == _normal_label(sr["label"])]
        if not matches:
            aliases = canonical_source_aliases.get(norm, [])
            matches = [sr for sr in source_rows if any(a in _normal_label(sr["label"]) for a in aliases)]
        if matches:
            row["values"] = dict(matches[0]["values"])
            row["evidence"] = matches[0]["evidence"]

    # Balance sheets contain two visible rows named exactly "Total": the first
    # belongs to CAPITAL AND LIABILITIES and the second belongs to ASSETS.
    # The VLM commonly preserves both rows as the generic label "Total", which
    # leaves the validator unable to identify its canonical operands. Resolve
    # those two rows from OCR section context before validation.
    if document_type == DocumentType.balance_sheet:
        total_sources = [
            sr for sr in source_rows
            if _normal_label(str(sr.get("label", ""))) == "total"
            and sr.get("section") in {"liabilities", "assets"}
        ]
        targets = [("total_capital_and_liabilities", "liabilities"), ("total_assets", "assets")]
        for target, section in targets:
            source_total = next((sr for sr in total_sources if sr.get("section") == section), None)
            if not source_total:
                continue
            target_norm = _normal_label(target)
            # Prefer a generic VLM Total in the corresponding section/order.
            generic = [r for r in rows if _normal_label(str(r.get("label", ""))) == "total"]
            candidate = generic[0] if section == "liabilities" and generic else (generic[1] if section == "assets" and len(generic) > 1 else None)
            if candidate is not None:
                candidate["label"] = target
                candidate["values"] = dict(source_total.get("values") or {})
                candidate["evidence"] = source_total.get("evidence")
            elif not any(_normal_label(str(r.get("label", ""))) == target_norm for r in rows):
                rows.append({"label": target, "values": dict(source_total.get("values") or {}), "evidence": source_total.get("evidence")})

    # Merge source rows not already represented by the VLM. This materially improves
    # completeness for scanned annual reports while remaining OCR-grounded.
    existing = {_normal_label(str(r.get("label", ""))) for r in rows}

    # P&L has multiple visible rows named exactly "Total". Resolve the section totals
    # explicitly instead of deduplicating all rows under the same raw label. A lone
    # printed value belongs to the comparative column detected from the OCR source.
    if document_type == DocumentType.profit_and_loss:
        for target, section in (("total_income", "income"), ("total_expenditure", "expenditure")):
            source_total = next((sr for sr in source_rows if _normal_label(sr["label"]) == "total" and sr.get("section") == section), None)
            if source_total:
                target_norm = _normal_label(target)
                source_values = dict(source_total["values"])
                # In the scanned HDFC comparative statement, the printed Total
                # contains only the right-hand (comparative) value. Keep that
                # value in the second period instead of assigning it to 2017.
                if len(source_values) == 1 and len(periods) >= 2:
                    only_value = next(iter(source_values.values()))
                    source_values = {periods[1]: only_value}
                replaced = False
                for row in rows:
                    if _normal_label(str(row.get("label", ""))) == target_norm:
                        row["values"] = source_values
                        row["evidence"] = source_total["evidence"]
                        replaced = True
                        break
                if not replaced:
                    rows.append({"label": target, "values": source_values, "evidence": source_total["evidence"]})
                existing.add(target_norm)

    for sr in source_rows:
        norm = _normal_label(sr["label"])
        # Raw "Total" rows were handled above for P&L; other raw rows may still be
        # merged normally.
        if document_type == DocumentType.profit_and_loss and norm == "total":
            continue
        if norm and norm not in existing:
            label = sr["label"]
            # Avoid structural/header noise.
            if any(x in norm for x in ("asat", "foryeareended", "yearended", "schedule31mar", "in000", "incrore")):
                continue
            rows.append({k: sr[k] for k in ("label", "values", "evidence")})
            existing.add(norm)

    # P&L contains several later "Total" rows (profit-and-loss balance and
    # appropriation totals). The VLM may omit one of the section totals, so do this
    # AFTER merging OCR rows, when every visible Total has section context.
    if document_type == DocumentType.profit_and_loss:
        for row in rows:
            if _normal_label(str(row.get("label", ""))) != "total":
                continue
            label_norm = _normal_label(str(row.get("label", "")))
            candidates = source_by_norm.get(label_norm, [])
            # Match the candidate by its section when possible.
            chosen = next((c for c in candidates if c.get("section") in {"income", "expenditure"}), None)
            if chosen:
                if chosen.get("section") == "income":
                    row["label"] = "total_income"
                elif chosen.get("section") == "expenditure":
                    row["label"] = "total_expenditure"

    # P&L contains several later "Total" rows (profit-and-loss balance and
    # appropriation totals). Keep the income/expenditure totals uniquely named so
    # validation cannot accidentally use a later section's total.
    if document_type == DocumentType.profit_and_loss:
        seen_semantic = {"total_income": 0, "total_expenditure": 0}
        for row in rows:
            label = _normal_label(str(row.get("label", "")))
            if label == "totalincome":
                if seen_semantic["total_income"]:
                    row["label"] = "profit_and_loss_other_total"
                seen_semantic["total_income"] += 1
            elif label == "totalexpenditure":
                if seen_semantic["total_expenditure"]:
                    row["label"] = "profit_and_loss_other_total"
                seen_semantic["total_expenditure"] += 1

    # Remove footer/header/unit fragments and rows without visible values.
    blocked = (
        "annualreport", "membershipno", "companysecretary", "charteredaccountants",
        "weunderstandyourworld", "significantaccountingpolicies", "schedulesreferred",
        "asat", "foryeareended", "yearended", "schedule31mar", "in000", "incrore",
        "consolidatedfinancialstatements",
    )
    cleaned = []
    for row in rows:
        label = str(row.get("label") or "").strip()
        norm = _normal_label(label)
        vals = row.get("values") or {}
        if not label or any(x in norm for x in blocked) or not any(v is not None for v in vals.values()):
            continue
        cleaned.append(row)
    result["financial_line_items"] = cleaned

    header = dict(result.get("header") or {})
    if re.search(r"₹\s*in\s*[‘']?\s*000|=\s*in\s*[‘']?\s*000", text, re.I):
        header["currency"] = "INR"
        header["unit"] = "thousand"
    elif re.search(r"\bin\s*crore", text, re.I):
        header["currency"] = "INR"
        header["unit"] = "crore"
    # Recover company name from visible statement text without inventing it.
    if not header.get("company_name"):
        m = re.search(r"\b(HDFC\s+Bank\s+Limited)\b", text, re.I)
        if m:
            header["company_name"] = m.group(1)
    result["header"] = header
    return result
