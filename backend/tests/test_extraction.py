import pytest

from app.schemas.extraction import ExtractionResult


def test_extraction_schema_accepts_structured_fields():
    result = ExtractionResult.model_validate({
        "header": {"company_name": "Example"},
        "fields": {"total_amount": {"value": 100, "evidence": {"source_text": "Total 100", "page_number": 1}}},
        "periods": [],
        "line_items": [{"description": "A", "quantity": 1, "unit_price": 100, "amount": 100}],
        "financial_line_items": [],
    })
    assert result.fields["total_amount"].value == 100


def test_qwen_invoice_output_is_normalized_to_project_schema():
    from app.services.extraction_service import _normalize_ai_result

    result = _normalize_ai_result({
        "invoice_number": "6825",
        "subtotal": "$135.00",
        "tax": "$12.48",
        "shipping": "$10.00",
        "total": "$157.48",
        "line_items": [{
            "description": "Item A", "qty": "2", "price": "$8.00", "line_total": "$16.00"
        }],
    }, "invoice")

    assert result["fields"]["subtotal"]["value"] == 135.0
    assert result["fields"]["tax_amount"]["value"] == 12.48
    assert result["fields"]["shipping"]["value"] == 10.0
    assert result["fields"]["total_amount"]["value"] == 157.48
    assert result["line_items"][0]["quantity"] == 2.0
    assert result["line_items"][0]["amount"] == 16.0


@pytest.mark.asyncio
async def test_extract_uses_ollama_provider(monkeypatch):
    from app.services import extraction_service
    from app.services.ocr_service import OCRPage, OCRResult
    from PIL import Image
    from app.schemas.document import DocumentType

    async def fake_ollama(document_type, ocr):
        return {
            "invoice_number": "6825",
            "subtotal": "$135.00",
            "tax": "$12.48",
            "shipping": "$10.00",
            "total": "$157.48",
            "line_items": [
                {"description": "Item A", "qty": "2", "price": "$8.00", "line_total": "$16.00"}
            ],
        }

    monkeypatch.setattr(extraction_service, "_ollama_extract", fake_ollama)
    monkeypatch.setattr(extraction_service.get_settings(), "extraction_provider", "ollama")

    ocr = OCRResult([OCRPage(1, "Invoice 6825", Image.new("RGB", (20, 20)))], True)
    data, method = await extraction_service.extract(DocumentType.invoice, ocr)

    assert method == "ollama_qwen2.5vl"
    assert data["fields"]["total_amount"]["value"] == 157.48
    assert data["line_items"][0]["amount"] == 16.0


from app.services.extraction_service import _invoice_needs_review


def test_invoice_review_not_triggered_for_small_document_subtotal_mismatch():
    # A source invoice can legitimately contain an inconsistent printed subtotal;
    # do not force a second pass merely to make arithmetic balance.
    data = {
        "fields": {"subtotal": {"value": 135}},
        "line_items": [
            {"quantity": 2, "unit_price": 8, "amount": 16},
            {"quantity": 4, "unit_price": 14, "amount": 56},
            {"quantity": 5, "unit_price": 6, "amount": 30},
            {"quantity": 4, "unit_price": 7, "amount": 28},
            {"quantity": 5, "unit_price": 3, "amount": 15},
        ],
    }
    assert _invoice_needs_review(data) is False


def test_invoice_review_triggered_for_large_subtotal_mismatch():
    data = {
        "fields": {"subtotal": {"value": 804}},
        "line_items": [
            {"quantity": 2, "unit_price": 346, "amount": 692},
            {"quantity": 4, "unit_price": 226, "amount": 904},
            {"quantity": 6, "unit_price": 136, "amount": 816},
            {"quantity": 8, "unit_price": 96, "amount": 768},
        ],
    }
    assert _invoice_needs_review(data) is True


def test_invoice_quality_and_review_accepts_materially_better_table():
    from app.services.extraction_service import _invoice_needs_review, _invoice_line_quality

    bad = {
        "fields": {"subtotal": {"value": 804}},
        "line_items": [
            {"quantity": 2, "unit_price": 346, "amount": 692},
            {"quantity": 4, "unit_price": 226, "amount": 904},
            {"quantity": 6, "unit_price": 136, "amount": 816},
            {"quantity": 8, "unit_price": 96, "amount": 768},
        ],
    }
    assert _invoice_needs_review(bad) is True
    assert _invoice_line_quality(bad) == (4, 0.0, 4)


def test_coordinate_aware_invoice_table_preserves_printed_totals():
    from app.services.extraction_service import _apply_invoice_geometry
    from app.services.ocr_service import OCRPage, OCRResult, OCRWord
    from PIL import Image

    words = [
        OCRWord("DESCRIPTION", 150, 100, 100, 20),
        OCRWord("QUANTITY", 780, 100, 120, 20),
        OCRWord("PRICE,", 1040, 100, 70, 20),
        OCRWord("TOTAL,", 1240, 100, 80, 20),
        OCRWord("01", 90, 160, 25, 20), OCRWord("2", 850, 160, 15, 20), OCRWord("346.00", 1080, 160, 60, 20), OCRWord("346.00", 1290, 160, 60, 20),
        OCRWord("02", 90, 220, 25, 20), OCRWord("4", 850, 220, 15, 20), OCRWord("226.00", 1080, 220, 60, 20), OCRWord("226.00", 1290, 220, 60, 20),
        OCRWord("03", 90, 280, 25, 20), OCRWord("6", 850, 280, 15, 20), OCRWord("136.00", 1080, 280, 60, 20), OCRWord("136.00", 1290, 280, 60, 20),
        OCRWord("04", 90, 340, 25, 20), OCRWord("8", 850, 340, 15, 20), OCRWord("96.00", 1080, 340, 50, 20), OCRWord("96.00", 1290, 340, 50, 20),
    ]
    ocr = OCRResult([OCRPage(1, "", Image.new("RGB", (1448, 600)), words)], True)
    data = {"line_items": [
        {"description": "A", "quantity": 2, "unit_price": 346, "amount": 692},
        {"description": "B", "quantity": 4, "unit_price": 226, "amount": 904},
        {"description": "C", "quantity": 6, "unit_price": 136, "amount": 816},
        {"description": "D", "quantity": 8, "unit_price": 96, "amount": 768},
    ]}
    corrected = _apply_invoice_geometry(data, ocr)
    assert [(x["quantity"], x["unit_price"], x["amount"]) for x in corrected["line_items"]] == [
        (2.0, 346.0, 346.0), (4.0, 226.0, 226.0), (6.0, 136.0, 136.0), (8.0, 96.0, 96.0)
    ]


def test_coordinate_table_reconstruction_matches_batch2_0999_ground_truth():
    from app.services.extraction_service import _apply_invoice_geometry
    from app.services.ocr_service import OCRPage, OCRResult, OCRWord
    from PIL import Image

    words = [
        OCRWord("DESCRIPTION", 155, 845, 175, 19), OCRWord("QUANTITY", 795, 840, 140, 34),
        OCRWord("PRICE,", 1052, 845, 80, 22), OCRWord("TOTAL,", 1244, 842, 97, 24),
        OCRWord("01", 93, 914, 22, 19), OCRWord("2", 858, 912, 12, 19), OCRWord("346.00", 1086, 917, 58, 16), OCRWord("346.00", 1296, 916, 58, 15),
        OCRWord("02", 93, 982, 26, 20), OCRWord("4", 857, 983, 11, 16), OCRWord("226.00", 1086, 984, 58, 16), OCRWord("226.00", 1276, 987, 59, 15),
        OCRWord("03", 93, 1052, 25, 20), OCRWord("6", 859, 1050, 11, 19), OCRWord("136.00", 1089, 1053, 57, 16), OCRWord("136.00", 1278, 1056, 57, 15),
        OCRWord("04", 93, 1135, 25, 20), OCRWord("8", 858, 1123, 10, 17), OCRWord("96.00", 1295, 1122, 58, 15),
    ]
    ocr = OCRResult([OCRPage(1, "", Image.new("RGB", (1448, 1500)), words)], True)
    data = {"line_items": [
        {"description": "r1", "quantity": 2, "unit_price": 346, "amount": 692},
        {"description": "r2", "quantity": 4, "unit_price": 226, "amount": 904},
        {"description": "r3", "quantity": 6, "unit_price": 136, "amount": 816},
        {"description": "r4", "quantity": 8, "unit_price": 96, "amount": 768},
    ]}
    corrected = _apply_invoice_geometry(data, ocr)
    assert [(x["quantity"], x["unit_price"], x["amount"]) for x in corrected["line_items"]] == [
        (2.0, 346.0, 346.0), (4.0, 226.0, 226.0), (6.0, 136.0, 136.0), (8.0, 96.0, 96.0)
    ]


@pytest.mark.parametrize("filename", ["batch2-0999.jpg"])
def test_coordinate_invoice_guardrail_corrects_shifted_numeric_columns(filename):
    from app.services.ocr_service import extract_text
    from app.services.extraction_service import _enforce_invoice_geometry

    path = "/mnt/data/batch2-0999.jpg"
    if not __import__("os").path.exists(path):
        pytest.skip("reference invoice image is unavailable in the test environment")
    ocr = extract_text(open(path, "rb").read(), filename)
    data = {
        "line_items": [
            {"description": "row 1", "quantity": 4, "unit_price": 226, "amount": 226},
            {"description": "row 2", "quantity": 6, "unit_price": 136, "amount": 136},
            {"description": "row 3", "quantity": 8600, "unit_price": 136, "amount": 96},
            {"description": "row 4", "quantity": 8, "unit_price": 96, "amount": 768},
        ]
    }
    fixed = _enforce_invoice_geometry(data, ocr)
    assert [(x["quantity"], x["unit_price"], x["amount"]) for x in fixed["line_items"]] == [
        (2.0, 346.0, 346.0),
        (4.0, 226.0, 226.0),
        (6.0, 136.0, 136.0),
        (8.0, 96, 96.0),
    ]

def test_statement_period_repair_and_cashflow_sign():
    from app.services.extraction_service import _repair_financial_result
    from app.services.ocr_service import OCRPage, OCRResult
    from PIL import Image
    img = Image.new("RGB", (100, 100), "white")
    ocr = OCRResult([OCRPage(1, "Year ended Year ended\n31-Mar-17 31-Mar-16\nNet cash generated from financing activities (68,929,743) 378,151,341\n= in 000", img, [])], True)
    data = {"header": {}, "fields": {"Financing Activities": {"Net cash generated from financing activities": 68929743}}, "periods": ["March 31, 2017", "31-Mar-17"], "financial_line_items": [], "line_items": [], "raw_text": None}
    fixed = _repair_financial_result(data, "cash_flow_statement", ocr)
    assert fixed["periods"] == ["31-Mar-17", "31-Mar-16"]
    rows = {r["label"]: r["values"] for r in fixed["financial_line_items"]}
    assert rows["Net cash generated from financing activities"]["31-Mar-17"] == -68929743


def test_financial_repair_uses_real_comparative_periods_and_keeps_pnl_totals_in_right_column():
    from app.services.extraction_service import _repair_financial_result
    from app.services.ocr_service import OCRPage, OCRResult
    from PIL import Image
    ocr = OCRResult([OCRPage(1, """Consolidated Statement of Profit and Loss
For the year ended March 31, 2017
= in 000
Year ended Year ended
Schedule 31-Mar-17 31-Mar-16
Interest earned 13 732,713,529 631,615,614
Other income 14 128,776,329 112,116,541
Total 743,732,155
Interest expended 15 380,415,844 340,695,748
Operating expenses 16 207,510,707 178,318,808
Provisions and contingencies 120,689,285 96,544,349
Total 615,558,905
""", Image.new("RGB", (100, 100)), [])], True)
    data = {"header": {}, "fields": {}, "periods": ["March 31, 2017", "31-Mar-17"],
            "financial_line_items": [
                {"label": "Interest earned 13", "values": {"March 31, 2017": 732713529, "31-Mar-17": 631615614}},
                {"label": "Other income 14", "values": {"March 31, 2017": 128776329, "31-Mar-17": 112116541}},
                {"label": "total_income", "values": {"March 31, 2017": 743732155}},
                {"label": "Interest expended 15", "values": {"March 31, 2017": 380415844, "31-Mar-17": 340695748}},
                {"label": "Operating expenses 16", "values": {"March 31, 2017": 207510707, "31-Mar-17": 178318808}},
                {"label": "Provisions and contingencies", "values": {"March 31, 2017": 120689285, "31-Mar-17": 96544349}},
                {"label": "total_expenditure", "values": {"March 31, 2017": 615558905}},
            ], "line_items": [], "raw_text": None}
    fixed = _repair_financial_result(data, "profit_and_loss", ocr)
    rows = {r["label"]: r["values"] for r in fixed["financial_line_items"] if r["label"] in {"total_income", "total_expenditure"}}
    assert fixed["periods"] == ["31-Mar-17", "31-Mar-16"]
    assert rows["total_income"] == {"31-Mar-16": 743732155.0}
    assert rows["total_expenditure"] == {"31-Mar-16": 615558905.0}


def test_cash_flow_nested_fields_are_flattened_and_parentheses_preserved():
    from app.services.extraction_service import _repair_financial_result
    from app.services.ocr_service import OCRPage, OCRResult
    from PIL import Image
    text = """Consolidated Cash Flow Statement
Year ended Year ended
31-Mar-17 31-Mar-16
Net cash flow (used in) / from operating activities 172,815,931 (344,353,663)
Net cash used in investing activities (8,655,510)
Net cash generated from financing activities (68,929,743) 378,151,341
Effect of exchange fluctuation on translation reserve (282,622) 282,433
Cash and cash equivalents on amalgamation 295,617 -
Net increase / (decrease) in cash and cash equivalents 102,422,381 25,424,601
Cash and cash equivalents as at April 1st 390,688,815 365,264,214
Cash and cash equivalents as at March 31st 493,111,196 390,688,815
"""
    ocr = OCRResult([OCRPage(1, text, Image.new("RGB", (100, 100)), [])], True)
    data = {"header": {}, "fields": {"financing_activities": {
        "cash_flow_from_financing_activities": {"year_ended_31_mar_17": "68,929,743", "year_ended_31_mar_16": "378,151,341"}
    }}, "periods": [], "financial_line_items": [], "line_items": [], "raw_text": None}
    fixed = _repair_financial_result(data, "cash_flow_statement", ocr)
    rows = {r["label"]: r["values"] for r in fixed["financial_line_items"]}
    assert rows["net_cash_flows_from_financing_activities"]["31-Mar-17"] == -68929743.0
    assert rows["net_cash_flows_from_financing_activities"]["31-Mar-16"] == 378151341.0
